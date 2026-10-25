param(
    [Parameter(Mandatory=$true)][string]$Candidate,
    [Parameter(Mandatory=$true)][string]$Repository,
    [ValidateSet('New','Existing')][string]$Mode = 'New',
    [string]$RepositoryUrl,
    [string]$GitName,
    [string]$GitEmail,
    [switch]$Push,
    [switch]$ValidateOnly
)
$ErrorActionPreference = 'Stop'
$Candidate = (Resolve-Path -LiteralPath $Candidate).Path
$ManifestPath = Join-Path $Candidate 'provenance/public-manifest.json'
if (!(Test-Path -LiteralPath $ManifestPath)) { throw 'Candidate manifest missing' }
$Manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $ManifestPath | ConvertFrom-Json
function Get-CandidateSha256 {
    param([string]$Path)
    $Stream = [System.IO.File]::OpenRead($Path)
    $Hasher = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($Hasher.ComputeHash($Stream))).Replace('-', '')
    } finally {
        $Hasher.Dispose()
        $Stream.Dispose()
    }
}
$Files = @($Manifest.files | ForEach-Object { $_.path }) + @('provenance/public-manifest.json')
foreach ($Row in $Manifest.files) {
    $Source = Join-Path $Candidate $Row.path
    $Hash = Get-CandidateSha256 -Path $Source
    if ($Hash -ne $Row.sha256) { throw "Candidate hash mismatch: $($Row.path)" }
}
if ($ValidateOnly) {
    Write-Output "CANDIDATE_VALIDATION_PASSED files=$($Files.Count) PowerShell=$($PSVersionTable.PSVersion)"
    return
}
if (!$RepositoryUrl) { $RepositoryUrl = Read-Host 'Enter your GitHub repository HTTPS/SSH URL' }
if (!$GitName) { $GitName = Read-Host 'Git author name (repository local only)' }
if (!$GitEmail) { $GitEmail = Read-Host 'Git author email (repository local only)' }
if (!$RepositoryUrl -or !$GitName -or !$GitEmail) { throw 'Repository URL/name/email are required' }
$Repository = [System.IO.Path]::GetFullPath($Repository)
if ($Repository -eq $Candidate) { throw 'Use a separate repository directory to preserve candidate files' }
function Invoke-TargetGit {
    param([string[]]$GitArgs)
    & git -C $Repository @GitArgs
    $Code = $LASTEXITCODE
    if ($Code -ne 0) { throw "git exited $Code. Original Git output is preserved above." }
}
if ($Mode -eq 'New') {
    if (Test-Path -LiteralPath $Repository) { throw 'New mode requires a directory that does not exist' }
    New-Item -ItemType Directory -Path $Repository | Out-Null
    Invoke-TargetGit -GitArgs @('init','-b','main')
} else {
    if (!(Test-Path -LiteralPath $Repository)) { throw 'Existing repository directory missing' }
    $Top = & git -C $Repository rev-parse --show-toplevel
    $Code = $LASTEXITCODE
    if ($Code -ne 0) { throw "git exited $Code. Original Git output is preserved above." }
    if ([System.IO.Path]::GetFullPath($Top.Trim()) -ne $Repository) { throw 'Target is not the repository root' }
    $Status = & git -C $Repository status --porcelain
    $Code = $LASTEXITCODE
    if ($Code -ne 0) { throw "git exited $Code. Original Git output is preserved above." }
    if ($Status) { $Status | Write-Output; throw 'Existing repository has changes. Preserve them and finish them before incrementally importing.' }
}
Invoke-TargetGit -GitArgs @('config','--local','user.name',$GitName)
Invoke-TargetGit -GitArgs @('config','--local','user.email',$GitEmail)
# Candidate gitattributes preserves reviewed bytes without changing global Git settings.
foreach ($Name in $Files) {
    $Source = Join-Path $Candidate $Name
    $Target = Join-Path $Repository $Name
    $Parent = Split-Path -Parent $Target
    if (!(Test-Path -LiteralPath $Parent)) { New-Item -ItemType Directory -Path $Parent -Force | Out-Null }
    Copy-Item -LiteralPath $Source -Destination $Target -Force
}
Invoke-TargetGit -GitArgs @('status','--short')
Invoke-TargetGit -GitArgs (@('add','--') + $Files)
Invoke-TargetGit -GitArgs @('diff','--cached','--stat')
& git -C $Repository diff --cached --quiet
$DiffCode = $LASTEXITCODE
if ($DiffCode -notin @(0,1)) { throw "git diff exited $DiffCode. Original Git output is preserved above." }
if ($DiffCode -eq 1) {
    Invoke-TargetGit -GitArgs @('commit','-m','Publish audited PDServe candidate, evidence summaries and Chinese report')
} else { Write-Output 'No content changes to commit.' }
Invoke-TargetGit -GitArgs @('rev-parse','--verify','HEAD')
$Branch = & git -C $Repository symbolic-ref --short HEAD
$Code = $LASTEXITCODE
if ($Code -ne 0) { throw "git exited $Code; cannot push a detached HEAD. Original output is above." }
$Remotes = & git -C $Repository remote
$Code = $LASTEXITCODE
if ($Code -ne 0) { throw "git exited $Code. Original Git output is preserved above." }
if ($Remotes -contains 'origin') {
    $Origin = & git -C $Repository remote get-url origin
    $Code = $LASTEXITCODE
    if ($Code -ne 0) { throw "git exited $Code. Original Git output is preserved above." }
    if ($Origin.Trim() -ne $RepositoryUrl) { throw 'Existing origin differs; no remote was changed or pushed' }
} else { Invoke-TargetGit -GitArgs @('remote','add','origin',$RepositoryUrl) }
if ($Push) { Invoke-TargetGit -GitArgs @('push','-u','origin',$Branch.Trim()) }
else { Write-Output "Prepared local commit only. To push manually: git -C `"$Repository`" push -u origin $($Branch.Trim())" }
