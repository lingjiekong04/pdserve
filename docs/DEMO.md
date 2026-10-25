# 演示与故障生命周期

## 无GPU演示

本地 Windows PowerShell：进入候选目录，执行 `python examples/mock_scheduler.py`。输出短短短长的合成队列顺序；所有预算均为合成对象。CPU测试覆盖取消竞态、清理ACK、上下文边界和证据校验，但它们不能代表 RDMA 或真实资源释放。

## 真实模型流式演示（用户手动执行）

先按 REPRODUCE 在专用Linux环境启动P/D与网关。新终端进入候选目录：

```bash
python examples/sse_client.py --request-id demo-001
curl --noproxy '*' http://127.0.0.1:8000/health/ready
curl --noproxy '*' http://127.0.0.1:8000/admin/state
```

`/admin/state` 仅在本地读取，不公开完整响应，因为包含内部ID、epoch和运行信息。

## 取消演示

终端一提交足够长的输出；终端二用相同 ID 取消：

```bash
curl --noproxy '*' -N http://127.0.0.1:8000/v1/completions \
  -H 'Content-Type: application/json' -H 'X-Request-ID: cancel-demo-001' \
  -d '{"model":"pdserve-qwen","prompt":"Explain autoregressive generation in detail.","max_tokens":4096,"ignore_eos":true,"temperature":0,"stream":true}'

curl --noproxy '*' -X POST \
  http://127.0.0.1:8000/requests/cancel-demo-001/cancel
curl --noproxy '*' http://127.0.0.1:8000/requests/cancel-demo-001
```

观察终态与 cleanup 分开推进，直到 P/D 都有 ACK 且 budget_held=false。取消 receipt 的202不等于资源已释放。通过客户端 Ctrl+C 断开流也会触发取消传播。普通人工命令无法保证命中特定执行阶段；历史 Stage2e1 使用引擎原生时间戳与回执严格证明阶段命中。

## 节点退出演示

只在专用环境中手动关闭某个由你启动的后端终端。保持网关运行。观察：ready=false、新请求503、未确认租约保留，已开始SSE明确失败；不能把已有HTTP200头视为请求成功。不要使用通配 `pkill` 或按端口盲杀进程。

停止另一后端并确认旧P/D都已退出，按原方式重新启动两个后端。仅在操作者确认旧实例退出后，明确请求恢复：

```bash
curl --noproxy '*' -X POST http://127.0.0.1:8000/admin/recover \
  -H 'Content-Type: application/json' -d '{"old_pair_stopped":true}'
```

检查新P/D epoch、旧租约标为epoch_retired而非engine_ack，再提交新请求。管理接口信任该人工确认，不提供强进程证明。公开版排除旧 checked-pid 杀进程工具；历史专项验收及其作用范围见 ITERATION_HISTORY。

## 超时

默认传输worker期限30s，Decode KV-ready期限60s。正常入口关闭test hooks。本轮没有注入新超时；历史Stage2c1受控测试验证隔离与成对恢复。若运行中guard触发，保留私有事件和失败结果，不能改ready或清零预算伪装恢复。
