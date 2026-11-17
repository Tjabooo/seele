# Install or update seele on Windows: venv, config skeleton, and a scheduled task "SEELE" that
# starts the bot at boot (no login needed) and restarts it if it exits.
#   powershell -ExecutionPolicy Bypass -File install.ps1            install and (re)start
#   powershell -ExecutionPolicy Bypass -File install.ps1 -NoStart   install only (the task starts at the next boot)
# Needs an elevated shell for the task (an admin's SSH session is one). Log: ~\.local\state\seele\seele.log
param([switch]$NoStart)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$data = Join-Path $HOME ".local\share\seele"
$conf = Join-Path $HOME ".config\seele"
$state = Join-Path $HOME ".local\state\seele"
$venv = Join-Path $data "venv"
New-Item -ItemType Directory -Force $data, $conf, $state | Out-Null

$python = (Get-Command python -ErrorAction Stop).Source
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) { & $python -m venv $venv }
$vpy = Join-Path $venv "Scripts\python.exe"
& $vpy -m pip install -q --upgrade $here
& $vpy -m pip install -q --force-reinstall --no-deps $here  # same version number still updates

$cfg = Join-Path $conf "config.toml"
if (-not (Test-Path $cfg)) {
    Copy-Item (Join-Path $here "config.example.toml") $cfg
    icacls $cfg /inheritance:r /grant:r "$($env:USERNAME):F" | Out-Null
    Write-Host "Created $cfg : put your bot token in it."
}

# The task runs this loop: the bot comes back 10 s after any exit.
$runner = Join-Path $data "seele-run.cmd"
@"
@echo off
set SEELE_LOG_FILE=$state\seele.log
set PYTHONUTF8=1
:loop
"$vpy" -m seele run
ping -n 11 127.0.0.1 >nul
goto loop
"@ | Set-Content -Encoding ascii $runner

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$runner`"" -WorkingDirectory $HOME
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Stop-ScheduledTask -TaskName "SEELE" -ErrorAction SilentlyContinue
# Stopping the task kills only its cmd loop: the venv's python launcher and the bot under it
# survive and keep answering next to the new bot (every message handled twice). Kill them too.
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match '-m seele run' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Register-ScheduledTask -TaskName "SEELE" -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "seele: prompt Claude Code from Discord" -Force | Out-Null

if ($NoStart) {
    Write-Host "Installed; task SEELE starts at the next boot (Start-ScheduledTask SEELE to start it now)."
} elseif ((Select-String -Path $cfg -Pattern '^token = ".+"' -Quiet) -or $env:DISCORD_TOKEN) {
    Start-ScheduledTask -TaskName "SEELE"
    Write-Host "seele (re)started. Log: $state\seele.log"
} else {
    Write-Host "Add the token, then: Start-ScheduledTask SEELE"
}
