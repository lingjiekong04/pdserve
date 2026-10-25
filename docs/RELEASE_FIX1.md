# 发布修订：Windows PowerShell 5.1 UTF-8清单

这是发布工具修订，不是算法、模型或实验升级。

原脚本未指定Get-Content编码，Windows PowerShell 5.1会把UTF-8无BOM清单按系统编码读取，使中文PDF文件名乱码，文件哈希检查因此在任何Git操作之前失败。

本修订显式使用 `Get-Content -Raw -Encoding UTF8`，增加 `-ValidateOnly` 模式以检查全部文件且不提示Git身份、不操作Git、不联网。完整模型、核心服务、测量结果、补丁与PDF均与上一候选逐字节一致。

新目录、清单和来源映射独立生成；原候选与历史证据未覆盖。实际PowerShell 5.1/7验证结果记录在 checks/powershell-utf8-fix1.json。上传说明中的 -ValidateOnly 可先单独执行。

SHA256仍按原始文件字节计算；校验实现改为.NET SHA256，避免Windows PowerShell脚本模块自动加载导致的环境差异。不改变任何文件的预期hash。
