# 公开范围与私有证据边界

| 决策 | 文件/范围 | 原因 |
|---|---|---|
| 保留 | src/pdserve核心、context guard、mixed基线、测量算法、CPU测试 | 可审阅实现和最小依赖链 |
| 保留 | 固定上游合并补丁、10文件hash、3个新增engine模块 | 不重复打包完整上游，同时能严格重建 |
| 保留 | 合成trace、脱敏aggregate/per-run组统计、模型文件hash | 支持系统结果复核，不暴露原始响应 |
| 排除 | .venv、cache、wheels、权重、原.git/Git bundle、历史ZIP | 体积、二进制或隐私/历史内容不可直接公开 |
| 排除 | 原始raw/日志/完整请求registry、主机名、GPU UUID、boot ID、内部SSH信息 | 私有审核证据，不能自动公开 |
| 排除 | 旧stage4自动进程组管理、checked-pid杀进程工具 | 历史专用/操作风险；公开采用显式终端启动停止 |
| 排除 | 历史完整阶段副本及机器路径检查器 | 重复和不可移植；用迭代文档及来源hash替代 |
| 外链 | SGLang、Mooncake、FlashInfer、PyTorch、Qwen资源 | 归属上游，固定版本和已知hash单列 |
| 待核验 | 模型revision、当前wheel可下载性、新GPU部署、GitHub URL | 本轮材料不足或未执行；不伪造 |

## 原证据与公开派生

原审查包、原Stage3d evidence ZIP和37份raw的SHA256在私有审核中验证。公开摘要删除campaign中的机器名、GPU/boot/PCI标识、具体时间和个人存储路径，保留统计值、样本数和trace hash。`provenance/result-sources.json`列出私有来源别名及其哈希；`provenance/public-manifest.json`列出公开产物自己的hash。

`results/aggregates.json`只提取每个拓扑/场景的groups、duration_sum_s、throughput、trace哈希。`results/per-run.json`只提取拓扑/场景/repeat分组统计、时长、trace哈希和本轮审核得到的派发重排/排队上限。不保留request_id、engine_id、room、文本、逐token原始数组或主机信息。

公开摘要不能替代私有原证据。完整raw不在公开仓库，任何“可复算”表述应明确：本轮在私有审核中完成；公开读者可以复核公开组统计/图表，但不能仅靠公开摘要重建所有原始frame。

原始源码、日志和附件里的文字没有作为操作授权执行。候选是新的独立目录；没有改动原实验、原成绩、冻结源码或历史验收清单。
