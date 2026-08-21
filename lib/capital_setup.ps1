# Capital (Qun Yi) API component setup -- ONE call does Step 3 + the verify-tool staging
# of references/capital-broker.md. Idempotent: re-running skips what is already done.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\blave-agent\workspace\lib\capital_setup.ps1
#   ... [-Version 2.13.59] [-Force]
#
# Runs fine from the agent's own (SYSTEM) shell -- nothing here touches the certificate.
# What it does, in order, each step logged and summarised as JSON at the end:
#   1. VC++ 2010 runtime (mfc100/msvcr100) via choco if missing -- CTSecuritiesATL.dll links it
#   2. Python comtypes + pywin32 if missing
#   3. CapitalAPI_<Version>.zip -> download, extract, copy the x64 component set to C:\skcom\x64,
#      regsvr32 SKCOM.dll, prove registration by CreateObject(SKCenterLib) from Python
#   4. SKCOMVerifyDJ zip -> download, extract, copy the WHOLE x64 folder (exe + DLLs) to
#      %PUBLIC%\Desktop\SKCOMVerifyDJ\ -- the exe alone dies with DllNotFoundException
#      (measured 2026-08-21); SKCOM.dll is copied in from C:\skcom\x64 if the zip lacks it
# Exit 0 = every step ok, 1 = something failed (see "errors" in the JSON).
#
# Why a script: the agent doing these ~20 tool calls by hand took 7-12 min per onboarding
# (LLM think-gaps between every call on a burst-starved Windows box, 2026-08-20/21); one
# call is ~2-3 min, dominated by the two downloads.
# THIS FILE MUST STAY ASCII (no Chinese path literals -- the zip's folder names are found
# by filter, never spelled out).

param(
    [string]$Version = '2.13.59',
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is ~10x slower with the bar on

$ws = if ($env:BLAVE_AGENT_WORKSPACE) { $env:BLAVE_AGENT_WORKSPACE } else { 'C:\blave-agent\workspace' }
$tmp = Join-Path $ws 'tmp\capital'
$skcomDir = 'C:\skcom\x64'
$toolDir = Join-Path $env:PUBLIC 'Desktop\SKCOMVerifyDJ'
$base = 'https://www.capital.com.tw/Service2/download/api_zip'
$componentZip = Join-Path $tmp "CapitalAPI_$Version.zip"
$verifyZip = Join-Path $tmp 'CapitalAPI_v5.0_SKCOMVerifyDJ.zip'
$versionMarker = Join-Path $skcomDir 'BLAVE_VERSION.txt'

$result = [ordered]@{ ok = $false; version = $Version; steps = [ordered]@{}; errors = @() }

function Log($m) { Write-Host "[capital_setup] $m" }

function Step([string]$name, [scriptblock]$body) {
    try {
        $r = & $body
        $script:result.steps[$name] = "$r"
        Log "$name : $r"
    } catch {
        $msg = $_.Exception.Message
        $script:result.steps[$name] = "FAILED: $msg"
        $script:result.errors += "$name : $msg"
        Log "$name FAILED: $msg"
    }
}

function Resolve-Python {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    if (Test-Path 'C:\Python314\python.exe') { return 'C:\Python314\python.exe' }
    throw 'python.exe not found (PATH or C:\Python314)'
}

function Download([string]$url, [string]$dest) {
    if ((Test-Path $dest) -and -not $Force) { return 'cached' }
    New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -TimeoutSec 600
    $len = (Get-Item $dest).Length
    if ($len -lt 1MB) { Remove-Item $dest -Force; throw "download too small ($len bytes) -- URL pattern changed?" }
    return "downloaded $([math]::Round($len / 1MB, 1)) MB"
}

# Find the x64 component folder inside an extracted zip without spelling the Chinese
# folder name: it is the directory that holds <marker> and whose path has a \x64\ segment.
function Find-X64Dir([string]$root, [string]$marker) {
    $hit = Get-ChildItem -Path $root -Recurse -Filter $marker -File -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match '\\x64\\' } | Select-Object -First 1
    if (-not $hit) { throw "$marker not found under an x64 folder in $root" }
    return $hit.DirectoryName
}

# Run a Python snippet from a TEMP FILE, never `-c`. A `-c` one-liner with embedded double
# quotes (r"C:\..." / print("ok")) gets its quoting mangled through SSH -> powershell ->
# python.exe and python sees a SyntaxError. Returns $true iff python exits 0.
function Invoke-Py([string]$py, [string[]]$lines, [string]$name) {
    $f = Join-Path $tmp "_$name.py"
    Set-Content -Path $f -Value $lines -Encoding ASCII
    & $py $f 2>&1 | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Test-SkcomRegistered([string]$py) {
    return (Invoke-Py $py @(
        'import comtypes.client as c'
        'c.GetModule(r"C:\skcom\x64\SKCOM.dll")'
        'import comtypes.gen.SKCOMLib as sk'
        'c.CreateObject(sk.SKCenterLib, interface=sk.ISKCenterLib)'
    ) 'skcom_check')
}

New-Item -ItemType Directory -Force -Path $tmp | Out-Null

# --- 1. VC++ 2010 runtime ---------------------------------------------------------------
Step 'vcredist2010' {
    $have = (Test-Path 'C:\Windows\System32\mfc100.dll') -and (Test-Path 'C:\Windows\System32\msvcr100.dll')
    if ($have) { return 'present' }
    if (-not (Get-Command choco -ErrorAction SilentlyContinue)) { throw 'mfc100.dll missing and choco not installed -- install Chocolatey first' }
    & choco install vcredist2010 -y --no-progress 2>&1 | Out-Null
    $have = (Test-Path 'C:\Windows\System32\mfc100.dll') -and (Test-Path 'C:\Windows\System32\msvcr100.dll')
    if (-not $have) { throw 'choco install vcredist2010 finished but mfc100.dll/msvcr100.dll still missing' }
    return 'installed'
}

# --- 2. Python deps ---------------------------------------------------------------------
$py = $null
Step 'python_deps' {
    $script:py = Resolve-Python
    # Test exactly what capital_worker.py imports (comtypes.client + pythoncom), NOT win32api --
    # pywin32's win32api can fail to import from a bare interpreter even when pythoncom works.
    $import = @('import comtypes.client, pythoncom')
    if (Invoke-Py $script:py $import 'dep_check') { return "present ($script:py)" }
    & $script:py -m pip install --quiet comtypes pywin32 2>&1 | Out-Null
    if (-not (Invoke-Py $script:py $import 'dep_check')) { throw "comtypes/pythoncom still not importable after pip install" }
    return "installed ($script:py)"
}

# --- 3. Component: download, extract, copy, register, verify ----------------------------
Step 'component' {
    if (-not $script:py) { throw 'skipped: python not resolved' }
    # Idempotency keys on ACTUAL registration (CreateObject works), not on our marker file --
    # a machine the agent set up by hand won't have the marker, and the live worker service
    # holds the SKCOM DLLs open, so an unconditional Copy-Item would fail with a sharing
    # violation. If it's already registered, touch the marker and skip the file ops entirely.
    if (-not $Force -and (Test-SkcomRegistered $script:py)) {
        Set-Content -Path $versionMarker -Value $Version -ErrorAction SilentlyContinue
        return "already registered (CreateObject ok) -- skipped download/copy"
    }
    $dl = Download "$base/CapitalAPI_$Version.zip" $componentZip
    $extract = Join-Path $tmp "CapitalAPI_$Version"
    Expand-Archive -Path $componentZip -DestinationPath $extract -Force
    $src = Find-X64Dir $extract 'SKCOM.dll'
    New-Item -ItemType Directory -Force -Path $skcomDir | Out-Null
    Copy-Item -Path (Join-Path $src '*') -Destination $skcomDir -Recurse -Force
    $reg = Start-Process -FilePath 'regsvr32.exe' -ArgumentList '/s', (Join-Path $skcomDir 'SKCOM.dll') -Wait -PassThru
    if ($reg.ExitCode -ne 0) { throw "regsvr32 SKCOM.dll exit code $($reg.ExitCode) (3 = LoadLibrary failed -- VC++ 2010 missing?)" }
    if (-not (Test-SkcomRegistered $script:py)) { throw 'regsvr32 returned 0 but CreateObject(SKCenterLib) fails from Python -- bitness mismatch?' }
    Set-Content -Path $versionMarker -Value $Version
    return "$dl, registered + CreateObject ok ($Version)"
}

# --- 4. Verify tool staged WITH its DLLs -------------------------------------------------
Step 'verify_tool' {
    if ((Test-Path (Join-Path $toolDir 'SKCOMVerifyDJ.exe')) -and (Test-Path (Join-Path $toolDir 'SKCOM.dll')) -and -not $Force) {
        return "already staged at $toolDir"
    }
    $dl = Download "$base/CapitalAPI_v5.0_SKCOMVerifyDJ.zip" $verifyZip
    $extract = Join-Path $tmp 'SKCOMVerifyDJ'
    Expand-Archive -Path $verifyZip -DestinationPath $extract -Force
    $src = Find-X64Dir $extract 'SKCOMVerifyDJ.exe'
    New-Item -ItemType Directory -Force -Path $toolDir | Out-Null
    Copy-Item -Path (Join-Path $src '*') -Destination $toolDir -Recurse -Force
    if (-not (Test-Path (Join-Path $toolDir 'SKCOM.dll'))) {
        # The tool P/Invokes SKCOM.dll from its own folder; borrow the registered set.
        Copy-Item -Path (Join-Path $skcomDir '*') -Destination $toolDir -Recurse -Force
    }
    return "$dl, staged at $toolDir (exe + DLLs)"
}

$result.ok = ($result.errors.Count -eq 0)
$result.python = $py
$result.skcom_dir = $skcomDir
$result.verify_tool = Join-Path $toolDir 'SKCOMVerifyDJ.exe'
Write-Output ($result | ConvertTo-Json -Depth 4)
if ($result.ok) { exit 0 } else { exit 1 }
