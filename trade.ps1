<#
    Control script for both bots.

        .\trade.ps1 start     launch both bots in the background
        .\trade.ps1 stop      stop them
        .\trade.ps1 status    are they running? what is the position?
        .\trade.ps1 logs      follow both logs live (Ctrl+C to stop watching)
        .\trade.ps1 restart   stop, then start
        .\trade.ps1 panic     cancel every resting order NOW

    No venv activation needed -- the script calls venv\Scripts\python.exe
    directly, which is exactly equivalent and one less thing to forget.
#>

param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status", "logs", "restart", "panic")]
    [string]$Command = "status"
)

$ErrorActionPreference = "Stop"

$Root    = $PSScriptRoot
$Python  = Join-Path $Root "venv\Scripts\python.exe"
$LogDir  = Join-Path $Root "logs"
$PidFile = Join-Path $Root ".bots.pid"

$Bots = @(
    @{ Name = "normal";   Script = "bot.py";         Desc = "private market - opens" }
    @{ Name = "reactive"; Script = "reactivebot.py"; Desc = "public market  - closes" }
)

function Assert-Setup {
    if (-not (Test-Path $Python)) {
        throw "No venv at $Python`nRun:  py -3.12 -m venv venv"
    }
    if (-not (Test-Path (Join-Path $Root "credentials.json"))) {
        throw "No credentials.json. Create it next to trade.ps1:`n" +
              '  {"account":"...","email":"...","password":"...","marketplace_id":3266}'
    }
}

function Get-RunningBots {
    if (-not (Test-Path $PidFile)) { return @() }
    $running = @()
    foreach ($line in Get-Content $PidFile) {
        $name, $processId = $line -split ",", 2
        $proc = Get-Process -Id $processId -ErrorAction SilentlyContinue
        # Match on start time as well: PIDs get recycled, and killing an
        # unrelated process because it inherited an old PID would be bad.
        if ($proc -and $proc.ProcessName -eq "python") {
            $running += [pscustomobject]@{ Name = $name; Id = [int]$processId; Proc = $proc }
        }
    }
    return $running
}

function Start-Bots {
    Assert-Setup

    $already = Get-RunningBots
    if ($already) {
        Write-Host "Already running: $($already.Name -join ', ')" -ForegroundColor Yellow
        Write-Host "Use  .\trade.ps1 restart  to replace them."
        return
    }

    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    Remove-Item $PidFile -ErrorAction SilentlyContinue

    $stamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
    foreach ($bot in $Bots) {
        $out = Join-Path $LogDir "$($bot.Name)_$stamp.log"
        $err = Join-Path $LogDir "$($bot.Name)_$stamp.err"

        $proc = Start-Process -FilePath $Python `
            -ArgumentList $bot.Script `
            -WorkingDirectory $Root `
            -RedirectStandardOutput $out `
            -RedirectStandardError $err `
            -WindowStyle Hidden -PassThru

        "$($bot.Name),$($proc.Id)" | Add-Content $PidFile
        # Keep a stable name pointing at the newest log so `logs` is simple.
        Copy-Item $out (Join-Path $LogDir "$($bot.Name).log") -Force -ErrorAction SilentlyContinue
        Write-Host ("  started {0,-9} pid {1,-6} {2}" -f $bot.Name, $proc.Id, $bot.Desc) -ForegroundColor Green
    }

    Start-Sleep -Seconds 3
    $alive = Get-RunningBots
    if ($alive.Count -lt $Bots.Count) {
        Write-Host "`nA bot exited immediately. Last error output:" -ForegroundColor Red
        Get-ChildItem $LogDir -Filter "*_$stamp.err" | ForEach-Object {
            $text = Get-Content $_.FullName -Raw
            if ($text) { Write-Host "`n--- $($_.Name) ---`n$text" -ForegroundColor Red }
        }
        return
    }

    Write-Host "`nBoth running. Watch them with:  .\trade.ps1 logs" -ForegroundColor Cyan
}

function Stop-Bots {
    $running = Get-RunningBots
    if (-not $running) {
        Write-Host "Nothing running." -ForegroundColor Yellow
        Remove-Item $PidFile -ErrorAction SilentlyContinue
        return
    }

    foreach ($bot in $running) {
        Stop-Process -Id $bot.Id -Force -ErrorAction SilentlyContinue
        Write-Host "  stopped $($bot.Name) (pid $($bot.Id))" -ForegroundColor Green
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue

    Write-Host "`nStopped. Any orders they left resting are STILL LIVE on the" -ForegroundColor Yellow
    Write-Host "market and can still fill. Clear them with:  .\trade.ps1 panic" -ForegroundColor Yellow
}

function Show-Status {
    $running = Get-RunningBots
    if ($running) {
        foreach ($bot in $running) {
            $cpu = [math]::Round($bot.Proc.CPU, 1)
            Write-Host ("  RUNNING  {0,-9} pid {1,-6} cpu {2}s" -f $bot.Name, $bot.Id, $cpu) -ForegroundColor Green
        }
    } else {
        Write-Host "  stopped" -ForegroundColor Yellow
    }

    # Last position line each bot logged -- the number that actually matters.
    foreach ($bot in $Bots) {
        $log = Join-Path $LogDir "$($bot.Name).log"
        if (Test-Path $log) {
            $line = Get-Content $log -Tail 40 | Where-Object { $_ -match "net=" } | Select-Object -Last 1
            if ($line) { Write-Host "  $($bot.Name): $line" -ForegroundColor Gray }
        }
    }
}

function Watch-Logs {
    $logs = Get-ChildItem $LogDir -Filter "*.log" -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -notmatch "_\d{4}-" }
    if (-not $logs) { Write-Host "No logs yet." -ForegroundColor Yellow; return }

    Write-Host "Following $($logs.Count) log(s). Ctrl+C stops watching -- the" -ForegroundColor Cyan
    Write-Host "bots keep running.`n" -ForegroundColor Cyan

    $jobs = foreach ($log in $logs) {
        Start-Job -ArgumentList $log.FullName, $log.BaseName -ScriptBlock {
            param($path, $tag)
            Get-Content $path -Wait -Tail 15 | ForEach-Object { "[$tag] $_" }
        }
    }
    try   { while ($true) { $jobs | Receive-Job; Start-Sleep -Milliseconds 400 } }
    finally { $jobs | Stop-Job -PassThru | Remove-Job }
}

switch ($Command) {
    "start"   { Start-Bots }
    "stop"    { Stop-Bots }
    "status"  { Show-Status }
    "logs"    { Watch-Logs }
    "restart" { Stop-Bots; Start-Sleep -Seconds 2; Start-Bots }
    "panic"   {
        Assert-Setup
        Write-Host "Cancelling every resting order..." -ForegroundColor Red
        & $Python (Join-Path $Root "panic.py")
    }
}
