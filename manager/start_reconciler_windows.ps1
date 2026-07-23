# Watchdog wrapper — restarts reconciler.py on crash and sends Telegram alert.
# Windows equivalent of manager/start_reconciler.sh.
#
# Usage (manual, mirrors Linux's tmux-based start):
#   powershell -ExecutionPolicy Bypass -File manager\start_reconciler_windows.ps1
#
# Intended supervision (NSSM, survives reboot — see references/manager.md):
#   nssm install blaveclaw-reconciler powershell.exe "-ExecutionPolicy Bypass -File %BLAVECLAW_HOME%\workspace\manager\start_reconciler_windows.ps1"
#   nssm set blaveclaw-reconciler AppDirectory %BLAVECLAW_HOME%\workspace
#   nssm set blaveclaw-reconciler Start SERVICE_AUTO_START
#   nssm start blaveclaw-reconciler
# (%BLAVECLAW_HOME% defaults to C:\openclaw if unset — see references/deployment.md)

$Workspace = Split-Path -Parent $PSScriptRoot
Set-Location $Workspace

function Notify($Msg) {
    $py = @'
import sys, os
sys.path.insert(0, '.')
from lib.notify import make_sender
text = sys.argv[1]
send = make_sender()
send(text)
'@
    $py | python - $Msg
}

function Get-Msg($Key, $Default, $Arg) {
    $py = @'
import sys
sys.path.insert(0, '.')
from lib.portfolio import load_portfolio_config
cfg  = load_portfolio_config()
msgs = cfg.get('messages', {})
key, default, arg = sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ''
tpl  = msgs.get(key, default)
print(tpl.format(code=arg) if arg else tpl)
'@
    if ($Arg) {
        return ($py | python - $Key $Default $Arg)
    } else {
        return ($py | python - $Key $Default)
    }
}

Notify (Get-Msg 'watchdog_started' '✅ Auto-trading started')

while ($true) {
    python manager\reconciler.py
    $ExitCode = $LASTEXITCODE
    $Msg = Get-Msg 'watchdog_restart' '⚠️ System restarted (code {code}), resuming in 10s' $ExitCode
    Write-Output $Msg
    Notify $Msg
    Start-Sleep -Seconds 10
}
