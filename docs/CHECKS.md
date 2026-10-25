# 本轮检查记录

检查日期：2026-10-25。审核基于远程盘点与私有审查包；未自动SSH、修改原项目、安装依赖、启动GPU、训练或推送GitHub。

| 检查 | 结果 | 证据范围 |
|---|---|---|
| 私有审查包 | 471成员，464采集文件hash全匹配，CRC正常 | 当前远程字节 |
| 历史复用 | 12个evidence ZIP与远程hash一致 | 复用前比对完成 |
| 原始结果 | 37份raw hash匹配；36份summary复算一致 | 含1份失败；原raw仍私有 |
| aggregate | 12份复算通过，容差1e-12 | 一处duration浮点差7.1e-15秒 |
| CPU测试 | 74项全部通过 | Python 3.12.14；标准库fake backend/模块测试 |
| Python3.10语法 | 44个.py AST通过 | 不是Python3.10执行记录 |
| 合成示例/统计读取 | 通过 | 无GPU、无网络、无模型 |
| 新启动入口 | prefill干运行与参数单测通过 | 未执行真正SGLang进程 |
| 合并补丁 | 10目标after SHA256完全匹配 | 私有stock字节上新目录重建 |
| PowerShell上传脚本 | 语法解析通过 | 没有执行其中的Git操作 |
| 隐私范围 | 已知内部路径、主机标识和密钥模式0命中 | 源码/文档扫描及PDF提取检查，不保证检测未知秘密 |
| PDF | 13页，已逐页渲染和查看；无替换字符 | 图表、代码、表格均无可见截断/重叠 |
| 发布清单/相对链接 | 最终check_release校验 | 清单与CI使用同一检查入口 |

## 重建工具的修正记录

首次新生成合并diff缺少新文件mode，已为3个新模块补齐；随后Windows Git的autocrlf造成CRLF偏差，工具改用单次命令参数core.autocrlf=false后所有hash匹配。原核心、原补丁、历史manifest均没有因消除提示而修改。私有失败的重建工作目录保留，候选只含修正后的工具。

## 统计解释修正

实际最大网关排队为0.728195ms，36次性能运行没有派发重排。不能再把会话差异直接写成公平算法的因果收益。专项Stage2f仍支持3:1、aging和背压的功能结论。ITL覆盖率不同、观察策略不对称、并发上限不同与repeat波动已披露。

## 历史与本轮分开

历史双A100真实P/D/取消/故障/恢复验收已通过，来源hash已核对；本轮未重新执行。新便携目录的真实GPU部署、fresh pip install、模型下载和候选依赖下的真实ASGI集成未运行。没有业务效果或生产可用性结论。

## 可复用检查命令

在Windows或Linux候选目录：

```text
python tools/check.py
python tools/check_release.py
```

PDF生成需要reportlab和中文字体，渲染需要Poppler。本轮使用已有运行时，没有安装系统依赖。GitHub CI配置尚未在GitHub实际运行，标为待用户上传后的检查。

完整本轮CPU输出：[cpu-tests.txt](../checks/cpu-tests.txt)。结构化结果：[verification.json](../checks/verification.json)。逐页PDF记录：[pdf-review.json](../checks/pdf-review.json)。


## 发布工具fix1

显式UTF-8清单读取和ValidateOnly已增加；在Windows PowerShell 5.1及PowerShell 7验证全部中文路径与哈希，不执行Git。结构化结果见 [powershell-utf8-fix1.json](../checks/powershell-utf8-fix1.json)。原74项CPU结果属于前一候选继承核心的检查，本修订未改变那些核心字节。
