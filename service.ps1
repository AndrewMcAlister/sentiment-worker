# The sentiment worker as a Windows service: asks for Administrator rights when the action needs them.
#
#   service.ps1 -Action Install [-Interval 300] [-Python PATH] [-NoStart]
#   service.ps1 -Action Start | Stop | Restart | Status | Uninstall [-Purge]
#
# The work is done by service.py beside this file (ctypes, no pywin32); this wrapper only
# finds an interpreter and elevates.  Elevation re-runs this same file with the same
# arguments, which keeps the argument list in one place.
param(
    [string]$Action = 'Status',
    [double]$Interval = 300,
    [string]$Python,
    [switch]$NoStart,
    [switch]$Purge,
    [string]$Name = 'SentimentWorker'
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$serviceScript = Join-Path $root 'service.py'
if (-not (Test-Path -LiteralPath $serviceScript)) {
    Write-Host "service.py is missing beside this file ($root)." -ForegroundColor Red
    exit 1
}

function Find-Python {
    if ($Python) { return $Python }
    if ($env:SENTIMENT_PYTHON) { return $env:SENTIMENT_PYTHON }
    $taskPython = 'C:\Users\AndrewBot\AppData\Local\Programs\Python\Python312\python.exe'
    if (Test-Path -LiteralPath $taskPython) { return $taskPython }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr = New-Object Security.Principal.WindowsPrincipal($id)
    return $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

$needsAdmin = @('Install', 'Uninstall', 'Start', 'Stop', 'Restart') -contains $Action
if ($needsAdmin -and -not (Test-Admin)) {
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"",
                 '-Action', $Action, '-Name', $Name, '-Interval', $Interval)
    if ($NoStart) { $argList += '-NoStart' }
    if ($Purge) { $argList += '-Purge' }
    if ($Python) { $argList += @('-Python', "`"$Python`"") }
    Write-Host "Asking for Administrator rights..." -ForegroundColor Cyan
    try {
        $proc = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList `
            -Verb RunAs -Wait -PassThru -WindowStyle Normal
        exit $proc.ExitCode
    } catch {
        Write-Host "Elevation was not given, so nothing was changed." -ForegroundColor Yellow
        exit 1
    }
}

$py = Find-Python
if (-not $py) {
    Write-Host "No Python was found. Set SENTIMENT_PYTHON to the python.exe that runs the worker." -ForegroundColor Red
    exit 1
}

$pyArgs = @($serviceScript, $Action.ToLowerInvariant(), '--name', $Name)
if ($Action -eq 'Install') {
    $pyArgs += @('--interval', [string]$Interval)
    if ($Python) { $pyArgs += @('--python', $Python) }
    if ($NoStart) { $pyArgs += '--no-start' }
}
if ($Action -eq 'Uninstall' -and $Purge) { $pyArgs += '--purge' }

& $py @pyArgs
exit $LASTEXITCODE
