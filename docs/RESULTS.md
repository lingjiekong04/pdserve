# 系统实验结果与统计口径

这些是私有原证据审核后生成的公开派生摘要。36次正式运行的summary本轮由原始frames重新计算，全部相符；37份raw（含1份失败）与远程哈希相符。原始响应和机器标识不在公开目录。

## 设置

Qwen2.5-7B-Instruct，BF16，context16384；两张A100-SXM4-80GB，TP=PP=1，seed2026，关闭前缀缓存。输入512/8192token、输出128/512token；每场景40请求，3次重复。到达率0.25/2.0req/s、短请求占20%/80%。每个配置/场景120请求；少数类别只有24请求。

## 总体吞吐与真实TTFT

| 场景 | 配置 | 完成/请求 | req/s | output tok/s | TTFT p50/p95/p99 ms | ITL +1帧覆盖率 |
|---|---|---:|---:|---:|---:|---:|
|20short-low|mixed_fifo|120/120|0.2551|81.63|587.34/626.01/944.23|99.90%|
|20short-saturated|mixed_fifo|120/120|1.2601|403.22|581.25/641.93/915.89|98.59%|
|80short-low|mixed_fifo|120/120|0.1778|56.89|65.38/621.59/635.11|99.97%|
|80short-saturated|mixed_fifo|120/120|2.0441|654.11|68.89/593.23/599.47|99.53%|
|20short-low|pd_fifo|120/120|0.2545|81.45|647.61/947.25/1073.93|83.78%|
|20short-saturated|pd_fifo|120/120|1.2125|388.01|1166.95/2258.34/2352.03|75.50%|
|80short-low|pd_fifo|120/120|0.1773|56.74|205.39/2018.76/3685.34|66.04%|
|80short-saturated|pd_fifo|120/120|1.8362|587.58|931.51/2297.31/3130.65|63.25%|
|20short-low|pd_fair|120/120|0.2536|81.17|2250.12/7287.51/8734.57|62.90%|
|20short-saturated|pd_fair|120/120|0.8124|259.97|5146.22/32183.35/36568.35|74.21%|
|80short-low|pd_fair|120/120|0.1776|56.84|108.29/670.76/687.03|79.60%|
|80short-saturated|pd_fair|120/120|1.9695|630.25|261.98/752.86/962.65|70.63%|

上述吞吐以首次实际发送到最后完成的时间为分母，包含开放环到达空闲和尾部排空。它不是稳态最大吞吐。正式结果中失败/拒绝均0，但失败尝试另存，不可省略。

## 分类延迟与观测区间

| 场景 | 配置 | 类别 / n | TTFT p50/p95/p99 ms | ITL p50/p95/p99 ms | queue p95 ms | transfer窗口 p95 ms |
|---|---|---|---:|---:|---:|---:|
|20short-low|mixed_fifo|short/24|61.71/64.85/65.35|10.42/10.91/12.07|0.206|n/a|
|20short-low|mixed_fifo|long/96|593.03/627.93/946.29|10.81/11.21/12.84|0.220|n/a|
|20short-saturated|mixed_fifo|short/24|68.10/180.76/186.35|11.18/13.08/48.37|0.215|n/a|
|20short-saturated|mixed_fifo|long/96|584.19/816.93/917.39|11.24/12.94/25.02|0.201|n/a|
|80short-low|mixed_fifo|short/96|64.24/69.52/73.48|10.41/10.94/11.97|0.219|n/a|
|80short-low|mixed_fifo|long/24|607.68/635.18/635.66|10.79/11.15/12.71|0.235|n/a|
|80short-saturated|mixed_fifo|short/96|67.53/99.90/309.22|11.12/12.93/33.30|0.192|n/a|
|80short-saturated|mixed_fifo|long/24|588.63/599.66/602.06|11.33/13.15/33.66|0.195|n/a|
|20short-low|pd_fifo|short/24|90.66/650.33/948.57|10.61/11.29/12.79|0.285|415.81|
|20short-low|pd_fifo|long/96|653.53/956.84/1076.48|11.09/11.64/16.16|0.289|174.44|
|20short-saturated|pd_fifo|short/24|228.25/2082.14/2168.01|12.16/14.03/39.75|0.300|1267.23|
|20short-saturated|pd_fifo|long/96|1273.75/2286.41/2365.91|12.37/14.37/37.43|0.289|1345.98|
|80short-low|pd_fifo|short/96|151.00/567.97/1675.00|10.65/11.22/12.29|0.295|443.63|
|80short-low|pd_fifo|long/24|1179.34/3704.83/4110.22|11.01/11.65/13.67|0.308|3039.98|
|80short-saturated|pd_fifo|short/96|801.48/2194.80/2916.31|11.71/13.56/35.74|0.292|1837.44|
|80short-saturated|pd_fifo|long/24|1548.00/3049.20/3445.20|11.81/13.73/32.89|0.442|2369.29|
|20short-low|pd_fair|short/24|295.41/5267.22/5851.73|10.77/11.56/14.73|0.278|4940.49|
|20short-low|pd_fair|long/96|2607.30/7731.69/8862.29|11.20/11.84/15.82|0.300|7028.57|
|20short-saturated|pd_fair|short/24|3327.54/27716.48/33639.09|12.16/13.75/24.53|0.267|27186.40|
|20short-saturated|pd_fair|long/96|6198.59/32518.07/36890.79|11.95/13.76/22.07|0.286|31642.42|
|80short-low|pd_fair|short/96|88.66/176.23/251.12|10.60/11.17/12.58|0.291|21.43|
|80short-low|pd_fair|long/24|659.49/687.39/694.70|10.99/11.61/16.13|0.290|54.21|
|80short-saturated|pd_fair|short/96|208.53/637.55/689.60|11.91/13.63/29.21|0.264|49.11|
|80short-saturated|pd_fair|long/24|670.07/962.85/1061.27|12.02/13.78/33.34|0.288|508.40|

各类吞吐没有重复显示为类别吞吐：原始aggregate只提供总体吞吐；这里保留正确口径。

## 逐repeat波动

| 场景 | 配置 | repeat | TTFT p95 ms | transfer窗口 p95 ms | queue最大 ms | 派发重排 |
|---|---|---:|---:|---:|---:|---|
|20short-low|mixed_fifo|1|624.31|n/a|0.220|False|
|20short-low|mixed_fifo|2|626.38|n/a|0.257|False|
|20short-low|mixed_fifo|3|618.99|n/a|0.224|False|
|20short-saturated|mixed_fifo|1|610.84|n/a|0.212|False|
|20short-saturated|mixed_fifo|2|641.93|n/a|0.211|False|
|20short-saturated|mixed_fifo|3|616.76|n/a|0.219|False|
|80short-low|mixed_fifo|1|615.00|n/a|0.224|False|
|80short-low|mixed_fifo|2|620.43|n/a|0.259|False|
|80short-low|mixed_fifo|3|621.59|n/a|0.246|False|
|80short-saturated|mixed_fifo|1|593.35|n/a|0.196|False|
|80short-saturated|mixed_fifo|2|593.37|n/a|0.210|False|
|80short-saturated|mixed_fifo|3|586.69|n/a|0.214|False|
|20short-low|pd_fifo|1|718.41|495.72|0.453|False|
|20short-low|pd_fifo|2|718.90|88.25|0.556|False|
|20short-low|pd_fifo|3|949.36|82.57|0.465|False|
|20short-saturated|pd_fifo|1|2231.13|1329.82|0.284|False|
|20short-saturated|pd_fifo|2|2305.76|1309.15|0.408|False|
|20short-saturated|pd_fifo|3|2127.77|1224.77|0.367|False|
|80short-low|pd_fifo|1|864.28|261.05|0.325|False|
|80short-low|pd_fifo|2|2736.11|2124.04|0.303|False|
|80short-low|pd_fifo|3|2048.11|1379.43|0.697|False|
|80short-saturated|pd_fifo|1|1909.38|1165.03|0.340|False|
|80short-saturated|pd_fifo|2|1783.55|963.51|0.470|False|
|80short-saturated|pd_fifo|3|3076.79|2425.64|0.728|False|
|20short-low|pd_fair|1|3412.32|2776.74|0.407|False|
|20short-low|pd_fair|2|4352.68|3438.16|0.314|False|
|20short-low|pd_fair|3|8507.01|7675.34|0.302|False|
|20short-saturated|pd_fair|1|35622.20|34732.16|0.285|False|
|20short-saturated|pd_fair|2|10710.05|9346.58|0.365|False|
|20short-saturated|pd_fair|3|1478.82|969.36|0.301|False|
|80short-low|pd_fair|1|652.83|40.46|0.365|False|
|80short-low|pd_fair|2|667.83|43.71|0.290|False|
|80short-low|pd_fair|3|678.85|48.40|0.352|False|
|80short-saturated|pd_fair|1|666.68|104.83|0.279|False|
|80short-saturated|pd_fair|2|685.81|115.70|0.290|False|
|80short-saturated|pd_fair|3|774.20|107.09|0.327|False|

## 能支持的结论

- 双混合副本在本次四类配置结果中总体更优；P/D未普遍提高吞吐。
- 80%短请求/2req/s下，fair会话TTFT p95比FIFO会话低约67.2%、吞吐高约7.3%，但相对双副本仍低约3.6%。这些只是会话观测差异。
- 36次运行无网关派发重排、排队不足1ms，无法将上述差异归因于3:1调度。功能正确性依据Stage2f专门排队验收。
- pd_fair长请求高到达率repeat的TTFT p95约35.62s、10.71s、1.48s，波动明显；第三次成功是在一次失败尝试并重启后获得，不能隐藏重启条件。
- Prefill完成至transfer_completed、Decode KV wait的长区间提示下游等待；不能直接断言网络线速/RDMA本身是根因。

## 不能支持的结论

不能证明算法因果提速、普遍饱和、无偏全token ITL、真实业务质量、H100/多机扩展或生产稳定性。样本尾分位只有描述意义，没有置信区间。P/D与mixed监测策略不同，时序未随机交错，总并发上限也不相等。原始记录及历史验收清单保持不变，这里明确修订解释。

## 来源与摘要提取

见 [PUBLIC_SCOPE.md](PUBLIC_SCOPE.md)、[result-sources.json](../provenance/result-sources.json)、[audit-origin.json](../provenance/audit-origin.json)。公开文件自己的hash在候选清单，不能冒充私有原文件hash。
