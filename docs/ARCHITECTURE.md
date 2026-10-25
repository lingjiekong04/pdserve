# 架构与关键不变量

## 执行边界

SGLang 负责模型 forward、KV allocator 与 scheduler；Mooncake 负责实际传输。网关维护一对 P/D 的外部资源账本和请求生命周期。本版本单机单 P/D 配对、一个 FastAPI/asyncio 事件循环，不实现 TP/PP，不引入 AgentCache。两个混合副本是独立对照路径。

## 准入与公平性

输入是单个非空文本 prompt，使用同模型 tokenizer，`add_special_tokens=False`。P demand=ceil(input/page)*page；D demand=ceil((input+max_output)/page)*page。KV 预算与 slot 均保留至少 10% 余量。单事件循环、不包含 await 的 `Ledger.reserve` 同时预留 P/D；`release_ack` 按 node epoch、native ID 和 bootstrap room 核对。

短请求 ≤2048 token，长请求 >2048。队列持有两类队首，同类不跳过 FIFO。公平模式按 S,S,S,L 轮转，超过 5s 的可容纳长队首优先；不能容纳时仍可选择可容纳的另一类。严格 FIFO 模式不绕过最早队首。这些保证是网关层调度语义，不保证引擎内部 FIFO 或跨类别 token 公平。

queue_limit=128；queue_timeout=30s；history=1024；stream_buffer=128。默认 P/D active_limit 参数 32，实际 min(active_limit, floor(request_slots*0.9), floor(metadata_slots*0.9))。历史 request_slots=33，因此有效上限为29。网关 tokenization 与历史 registry 也有容量限制。

## 生命周期

```text
queued -> dispatched -> P/D phases -> completed/failed/cancelled
                                        |
                                        +-> cleanup_confirmed per role -> release that lease
                                        +-> unconfirmed -> retain -> quarantine -> explicit recovery
```

`completed` 是响应终态，不能推出 KV 已释放。引擎 observer 单独记录 allocator_release_seen、metadata_release_seen、compute_pending 和 transport retirement。只有完整清理记录通过核对后才能释放外部预留。

P 取消采用计算与传输排空；D 在 KV 未就绪时保持资源等待，KV 就绪后才触发 cooperative abort。接收 cancel receipt 只意味着取消意图已入队，不意味着已清理。

节点退出、清理超时、事件窗口丢失、未知请求或 epoch/capacity 改变时隔离整对 P/D。流式已输出请求不自动重试。恢复接口要求人工确认旧进程退出，以及需要退役租约时 P/D 均为新 epoch；`epoch_retired` 与 `engine_ack` 明确分开。接口本身不能证明操作者提供的进程退出确认，强身份检查属于历史故障测试工具。

## 补丁与来源

最终引擎共 10 个新增/修改文件，包括未被 Stage2c1 覆盖但仍来自 Stage2b2 的 `io_struct.py`。公开合并补丁基于已核验的 Stage1 stock 字节生成；before/after 哈希在 `patches/manifest.json`。通过 `tools/rebuild_engine.py` 只能在新目录重建，不覆盖原上游和现有 site-packages。

`engine_modules` 保留三个新增模块原字节，供不依赖 Torch 的单测导入；它们也包含于合并补丁以保证完整重建。生命周期模块部分继承 docstring 属早期阶段措辞，行为判断以最终 observer/cancel 逻辑和测试为准，本轮没有为文档修饰而修改冻结源码。

## 监测与失败关闭

P/D 每 0.25s 获取状态，超时30s；混合副本启动时读取静态配置，随后每1s读取 metrics。两种观测方式不完全对称，历史性能对比存在监测开销混杂。阶段事件来自引擎 monotonic clock；客户端 TTFT/ITL 来自客户端 clock；区间不跨来源相减。

本项目没有鉴权、TLS、跨机时钟同步、自动进程重启、服务发现、多配对调度或持久化请求租约。进程重启不是自动恢复协议。
