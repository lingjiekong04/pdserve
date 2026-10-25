# Windows PowerShell 手动上传 GitHub

本轮没有创建仓库、commit或push。GitHub仓库地址未提供；原对话最初用于学习skills的仓库不视为你的目标仓库。脚本会询问真实URL、姓名、邮箱，不读取凭据、不猜代理端口。

## 新建独立仓库

先由你在GitHub创建一个空仓库。执行位置：本地Windows PowerShell；候选目录保持原样，Git工作区另建在 `C:\src\PDServe-github`，该目录必须不存在。

```powershell
$Candidate = "C:\src\PDServe"
$Repository = "C:\src\PDServe-github"
& "$Candidate\scripts\manual_upload.ps1" -Candidate $Candidate -Repository $Repository -Mode New
```

执行脚本才会按你输入的信息在目标仓库本地配置Git身份、逐文件校验后复制公开清单、显式暂存、显示stat并提交。默认不推送。确认HEAD后手动：

```powershell
& git -C $Repository status --short
$Code = $LASTEXITCODE
if ($Code -ne 0) { throw "git status失败，退出码$Code；保留上方原始报错" }
& git -C $Repository rev-parse --verify HEAD
$Code = $LASTEXITCODE
if ($Code -ne 0) { throw "HEAD不存在，停止推送；退出码$Code" }
& git -C $Repository push -u origin main
$Code = $LASTEXITCODE
if ($Code -ne 0) { throw "push失败，退出码$Code；保留上方原始报错" }
```

也可在明确要提交并推送时，为同一用户手动执行命令增加 `-Push`。不要在代理端口未知时猜测配置。认证由你通过Git正常流程处理。

## 已有仓库的增量更新

把 `$Repository` 指向现有仓库的**绝对根目录**，先保留并提交或单独保存你已有的改动；脚本遇到非干净工作区会停止。然后运行：

```powershell
& "$Candidate\scripts\manual_upload.ps1" -Candidate $Candidate -Repository $Repository -Mode Existing
```

Existing模式不init、不reset、不force、不删除历史文件，保留现有分支和提交；只把公开清单文件作为增量更新。若同名旧文件已跟踪且工作区干净，其历史版本由原提交保留。本仓库其他文件不纳入暂存。origin不一致时停止并保留原配置。已有非空远端但本地没有其历史时，应先由你clone到独立目录再使用Existing模式，不能用force push覆盖。


## Windows PowerShell 5.1 编码兼容修订

本候选已显式按UTF-8读取清单。你可以在任何Git操作之前只运行文件验证：

```powershell
& "$Candidate\scripts\manual_upload.ps1" -Candidate $Candidate -Repository $Repository -ValidateOnly
```

该模式不会创建仓库、配置身份、提交或推送。详见 [RELEASE_FIX1.md](RELEASE_FIX1.md)。
