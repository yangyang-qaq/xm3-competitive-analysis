<#
.SYNOPSIS
    停掉 scripts\dev.ps1 起的两个服务。

.DESCRIPTION
    比 stop.sh 简单，原因是这里不存在那个坑：`Start-Process -PassThru`
    拿到的就是 Windows 的 PID，`Stop-Process` 能直接打中。
    Git Bash 里 `$!` 拿到的是 MSYS 编号，跨会话 kill 会静默失败，
    所以 stop.sh 才需要"按端口兜底"那一段。

    但**验收标准是一样的：看端口，不看 Stop-Process 有没有报错。**
    PID 文件可能是过期的，或者进程卡在退出中。命令返回成功不等于
    服务停了——最后一定要再探一次端口。
#>
[CmdletBinding()]
param(
    [int]$BackendPort  = 8020,
    [int]$FrontendPort = 3500
)

$Root   = Split-Path -Parent $PSScriptRoot
$DevDir = Join-Path $Root '.dev'

function Say { param($m) Write-Host "[stop] $m" -ForegroundColor Cyan }

function Test-Port { param([int]$Port)
    foreach ($p in @('/', '/health')) {
        try {
            $null = Invoke-WebRequest -Uri "http://127.0.0.1:$Port$p" `
                -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
            return $true
        } catch {
            if ($_.Exception.Response) { return $true }
        }
    }
    return $false
}

function Stop-One {
    param([int]$Port, [string]$Name, [string]$PidFile)

    if (-not (Test-Port $Port)) {
        Say "$Name：端口 $Port 上没有服务在跑"
        if (Test-Path $PidFile) { Remove-Item $PidFile -Force }
        return $true
    }

    if (Test-Path $PidFile) {
        $procId = (Get-Content $PidFile -Raw).Trim()
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc) {
            Say "$Name：Stop-Process $procId"
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        } else {
            Say "$Name：PID $procId 早就不在了（PID 文件过期）"
        }
        Remove-Item $PidFile -Force
    } else {
        Say "$Name：没有 PID 文件"
    }

    Start-Sleep -Seconds 1

    # 兜底：端口还在响应就按端口找真正的占用者。
    # 这里查出来的 PID 是 Windows 的，和 taskkill 是一套编号。
    if (Test-Port $Port) {
        Say "$Name：PID 文件没解决问题，改按端口找"
        $owners = @(Get-NetTCPConnection -LocalPort $Port -State Listen `
                        -ErrorAction SilentlyContinue |
                    Select-Object -ExpandProperty OwningProcess -Unique)
        foreach ($owner in $owners) {
            Say "$Name：端口 $Port 被 PID $owner 占着，Stop-Process"
            Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 1
    }

    if (Test-Port $Port) {
        Say "$Name：**端口 $Port 仍然有响应**，没能停掉。"
        Say "     手动看一眼：Get-NetTCPConnection -LocalPort $Port -State Listen"
        return $false
    }

    Say "$Name：已停止（端口 $Port 无响应）"
    return $true
}

Say "停 xm3 的服务（后端 $BackendPort / 前端 $FrontendPort）"
$ok = $true
if (-not (Stop-One -Port $FrontendPort -Name '前端' -PidFile (Join-Path $DevDir 'frontend.pid'))) { $ok = $false }
if (-not (Stop-One -Port $BackendPort  -Name '后端' -PidFile (Join-Path $DevDir 'backend.pid')))  { $ok = $false }

# 空了就清掉，免得仓库根上一直挂着一个 .dev\
if ((Test-Path $DevDir) -and -not (Get-ChildItem $DevDir -Force)) {
    Remove-Item $DevDir -Force
}

if ($ok) { Say "都停了" } else { Say "有服务没停掉，见上面的提示"; exit 1 }
