# Capital (Qun Yi) read-only probe -- ONE call does Steps 5-6 of references/capital-broker.md
# the only way they can work: under a PASSWORD logon as Administrator.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\blave-agent\workspace\lib\capital_probe.ps1
#   ... [-TimeoutSec 120]
#
# Your own shell is nt authority\system, and key-auth SSH cannot unlock DPAPI either -- from
# both, SKCOM login returns 602 and `certutil -user -store My` shows the WRONG (empty) store.
# This script wraps the schtasks vehicle (see Step 2 CONFIRMED notes) around
# `lib/capital_worker.py --once`, which logs in, reads accounts + one equity/position tick,
# summarises the Administrator cert store, and writes state/capital_probe.json. That JSON is
# printed here. Never hand-write a login script and never diagnose the cert store from your
# own shell -- both misfired in 2026-08-20/21 onboardings (uid 29026).
#
# Exit 0 = probe ok (login + accounts + tick), 2 = probe ran but failed (read "stage"/"error"),
# 3 = the task never produced a result within TimeoutSec, 1 = could not even schedule.
# Safe to run while the blave-agent-capital worker service is up (separate process/session).
# THIS FILE MUST STAY ASCII.

param([int]$TimeoutSec = 120)

# Continue, not Stop: this is procedural with explicit $LASTEXITCODE checks and Fail() exits.
# Under Stop, a native command writing to stderr (e.g. schtasks /delete on a non-existent task)
# raises a terminating error and aborts the script -- exactly the wrong behavior for best-effort
# cleanup. The best-effort schtasks calls below silence themselves via cmd redirection.
$ErrorActionPreference = 'Continue'
$ws = if ($env:BLAVE_AGENT_WORKSPACE) { $env:BLAVE_AGENT_WORKSPACE } else { 'C:\blave-agent\workspace' }
$out = Join-Path $ws 'state\capital_probe.json'
$task = 'BlaveCapitalProbe'

function Fail([int]$code, [string]$stage, [string]$msg) {
    Write-Output (@{ ok = $false; stage = $stage; error = $msg } | ConvertTo-Json)
    exit $code
}

$pwFile = @('C:\blave-agent\credentials\rdp_password.txt', 'C:\openclaw\credentials\rdp_password.txt') |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $pwFile) { Fail 1 'setup' 'rdp_password.txt not found (C:\blave-agent\credentials or C:\openclaw\credentials)' }
$pw = (Get-Content $pwFile -Raw).Trim()
if (-not $pw) { Fail 1 'setup' "$pwFile is empty" }

$pyCmd = Get-Command python -ErrorAction SilentlyContinue
$py = if ($pyCmd) { $pyCmd.Source } elseif (Test-Path 'C:\Python314\python.exe') { 'C:\Python314\python.exe' } else { $null }
if (-not $py) { Fail 1 'setup' 'python.exe not found (PATH or C:\Python314)' }
$worker = Join-Path $ws 'lib\capital_worker.py'
if (-not (Test-Path $worker)) { Fail 1 'setup' "$worker missing" }
# schtasks /tr cannot carry nested quotes reliably; keep both paths space-free (they are on
# every Blave Agent image) and refuse rather than silently mis-launch.
if (($py -match ' ') -or ($worker -match ' ')) { Fail 1 'setup' "python or workspace path contains a space: $py | $worker" }

# Freshness gate: run_once stamps read_at (int unix seconds, UTC) in every result. Record the
# epoch just before launch and only accept a file whose read_at >= it -- so a stale probe.json
# that couldn't be deleted (locked) is ignored instead of read as this run's result. Compute
# the epoch culture-invariantly (Get-Date -UFormat %s parsing is locale-fragile). Subtract 2s
# of slack for any clock granularity between here and the worker's time.time().
$startEpoch = [int]([DateTimeOffset](Get-Date)).ToUnixTimeSeconds() - 2
Remove-Item $out -Force -ErrorAction SilentlyContinue
cmd /c "schtasks /delete /tn $task /f >nul 2>nul"
schtasks /create /tn $task /tr "$py $worker --once" /sc once /st 00:00 /ru Administrator /rp $pw /rl HIGHEST /f 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Fail 1 'setup' "schtasks /create failed (exit $LASTEXITCODE) -- wrong Administrator password? (never reset it, see Step 2)" }
schtasks /run /tn $task 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { cmd /c "schtasks /delete /tn $task /f >nul 2>nul"; Fail 1 'setup' "schtasks /run failed (exit $LASTEXITCODE)" }

$deadline = (Get-Date).AddSeconds($TimeoutSec)
$obj = $null
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 2
    if (Test-Path $out) {
        try { $candidate = (Get-Content $out -Raw | ConvertFrom-Json) } catch { $candidate = $null }
        if ($candidate -and ($candidate.read_at -ne $null) -and ([int]$candidate.read_at -ge $startEpoch)) {
            $obj = $candidate; break
        }
    }
}
cmd /c "schtasks /delete /tn $task /f >nul 2>nul"
if (-not $obj) { Fail 3 'probe' "no fresh result after ${TimeoutSec}s -- worker hung (message pump / venue down?)" }

Write-Output ($obj | ConvertTo-Json -Compress)
if ($obj.ok) { exit 0 } else { exit 2 }
