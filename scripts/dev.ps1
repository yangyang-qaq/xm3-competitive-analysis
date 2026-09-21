<#
.SYNOPSIS
    起本地开发环境：后端 8020 + 前端 3500。

.DESCRIPTION
    和 scripts/dev.sh 等价，给 PowerShell 用。**不是**把 sh 版逐行翻译过来——
    两个平台真正不一样的地方在这里改掉了：

      1. `python3` 在 Windows 上是坏的（本机实测）。必须先找
         `backend\.venv\Scripts\python.exe`，找不到才退回 `py -3`。
      2. 用 `Start-Process -PassThru` 拿到的才是 **Windows 的 PID**，
         写进 PID 文件之后 `Stop-Process` 能直接打中。
         Git Bash 里 `$!` 拿到的是 MSYS 编号，跨会话 kill 会静默失败——
         这是 stop.sh 里那个"按端口兜底"存在的原因，在这里不需要。
      3. **绝不用 `uvicorn --reload`。** 本机实测：SQLite 在
         `backend\data\` 下，`--reload` 监视整个工作目录，于是每次写库
         都触发一次重载，服务在跑任务的过程中反复重启，任务永远停在
         running。看起来像流水线卡死，实际是开发服务器在自杀。
         要热重载就 `-Watch`，它会加 `--reload-dir app`，把 data\ 排除在外。

.PARAMETER NoFrontend
    只起后端。

.PARAMETER Watch
    后端开热重载（只监视 app\）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\dev.ps1
    powershell -ExecutionPolicy Bypass -File scripts\dev.ps1 -Watch
#>
[CmdletBinding()]
param(
    [switch]$NoFrontend,
    [switch]$Watch,
    [int]$BackendPort  = 8020,
    [int]$FrontendPort = 3500
)

$ErrorActionPreference = 'Stop'
$Root    = Split-Path -Parent $PSScriptRoot
$DevDir  = Join-Path $Root '.dev'
$Backend = Join-Path $Root 'backend'
$Frontend = Join-Path $Root 'frontend'

function Say  { param($m) Write-Host "[dev] $m" -ForegroundColor Cyan }
function Fail { param($m) Write-Host "[dev] $m" -ForegroundColor Red; exit 1 }

function Test-Port { param([int]$Port)
    # 只认"HTTP 有响应"，不认"端口被占用"：TIME_WAIT 也会让 netstat 报占用。
    foreach ($p in @('/', '/health')) {
        try {
            $null = Invoke-WebRequest -Uri "http://127.0.0.1:$Port$p" `
                -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
            return $true
        } catch {
            if ($_.Exception.Response) { return $true }   # 有响应，只是状态码不 200
        }
    }
    return $false
}

New-Item -ItemType Directory -Force -Path $DevDir | Out-Null

# ---------- 找解释器 ----------
# venv 优先。`py -3` 兜底（它是 Windows 上的 launcher，不是 `python3`）。
$Python = $null
$candidates = @(
    (Join-Path $Backend '.venv\Scripts\python.exe'),
    'py'
)
foreach ($c in $candidates) {
    $exe = $c
    $pre = @()
    if ($c -eq 'py') { $pre = @('-3') }
    if ($c -ne 'py' -and -not (Test-Path $c)) { continue }
    if (-not (Get-Command $c -ErrorAction SilentlyContinue)) { continue }
    & $exe @pre -c "import fastapi" 2>$null
    if ($LASTEXITCODE -eq 0) { $Python = $c; $PythonPre = $pre; break }
}
if (-not $Python) {
    Fail @"
找不到可用的解释器。先建好环境：
    cd backend
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -e ".[dev]"
"@
}
Say "后端解释器：$Python"

# ---------- 后端 ----------
if (Test-Port $BackendPort) {
    Fail @"
端口 $BackendPort 上已经有服务在响应了。
      要么是你之前起的（先跑 scripts\stop.ps1），要么是别的程序。
      这个脚本不替你换端口：换完之后前端还指着 $BackendPort，页面会白屏，
      而报错信息会说"连接被拒绝"，指不到真正的原因。
"@
}

$uvicornArgs = @(
    '-m', 'uvicorn', 'app.main:app',
    '--host', '127.0.0.1', '--port', "$BackendPort"
)
if ($Watch) {
    $uvicornArgs += @('--reload', '--reload-dir', 'app')
    Say "热重载已开，**只监视 app\**（监视整目录会让每次写库都重启，见文件头注释）"
}

$env:PYTHONIOENCODING = 'utf-8'
$beOut = Join-Path $DevDir 'backend.log'
$beErr = Join-Path $DevDir 'backend.err.log'

# -PassThru 拿到的才是 Windows PID；写进文件后 Stop-Process 能直接打中。
$beArgs = @($PythonPre) + $uvicornArgs
$be = Start-Process -FilePath $Python -ArgumentList $beArgs `
    -WorkingDirectory $Backend -PassThru -NoNewWindow `
    -RedirectStandardOutput $beOut -RedirectStandardError $beErr
$be.Id | Set-Content (Join-Path $DevDir 'backend.pid')
Say "后端 pid $($be.Id)，日志 $beOut"

$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    if (Test-Port $BackendPort) { $ok = $true; break }
    if ($be.HasExited) { break }
    Start-Sleep -Seconds 1
}
if (-not $ok) {
    Fail @"
后端 30 秒内没有起来。日志最后 20 行：
$((Get-Content $beErr -Tail 20 -ErrorAction SilentlyContinue) -join "`n")
$((Get-Content $beOut -Tail 20 -ErrorAction SilentlyContinue) -join "`n")
"@
}
Say "后端就绪：http://127.0.0.1:$BackendPort （/health 有响应）"

# ---------- 前端 ----------
if (-not $NoFrontend) {
    if (Test-Port $FrontendPort) {
        Say "端口 $FrontendPort 上已经有东西（大概是上次留下的 vite），跳过启动"
    } else {
        if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
            Fail "找不到 npm。装 Node >= 20 之后再来。"
        }
        if (-not (Test-Path (Join-Path $Frontend 'node_modules'))) {
            Say "node_modules 不存在，先跑一次 npm install（可能要几分钟）"
            Push-Location $Frontend
            try { npm install } finally { Pop-Location }
            if ($LASTEXITCODE -ne 0) { Fail "npm install 失败" }
        }
        $feOut = Join-Path $DevDir 'frontend.log'
        $feErr = Join-Path $DevDir 'frontend.err.log'
        # npm 在 Windows 上是 npm.cmd，Start-Process 不能直接起 .ps1 shim。
        $fe = Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', 'npm', 'run', 'dev') `
            -WorkingDirectory $Frontend -PassThru -NoNewWindow `
            -RedirectStandardOutput $feOut -RedirectStandardError $feErr
        $fe.Id | Set-Content (Join-Path $DevDir 'frontend.pid')
        Say "前端 pid $($fe.Id)，日志 $feOut"

        # vite 就绪没有可靠探针（根路径返回 HTML，不是 JSON），
        # 所以只等端口连得上，不假装知道它"编译完成了"。
        for ($i = 0; $i -lt 30; $i++) {
            if (Test-Port $FrontendPort) { break }
            Start-Sleep -Seconds 1
        }
    }
    Say "前端：http://localhost:$FrontendPort"
}

Write-Host ""
Say "两个服务都在后台跑。"
Say "停服务：powershell -ExecutionPolicy Bypass -File scripts\stop.ps1"
