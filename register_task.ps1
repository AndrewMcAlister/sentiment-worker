<#
.SYNOPSIS
    Register, refresh or remove the five-minute sentiment curation task.

.DESCRIPTION
    The task runs `sentiment_worker.py --scheduled` every five minutes under
    pythonw.exe (no console window), as the current user, with
    MultipleInstancesPolicy = IgnoreNew so a long pass is never stacked on top
    of itself.

    The worker itself decides whether there is anything to do: it exits silently
    when McTrader's API is down or when no file is marked.

.PARAMETER Remove
    Unregister the task instead of creating it.

.PARAMETER Show
    Print the task's state, last run time and last result, then exit.

.PARAMETER Enable
    Register the task switched on even when files are already marked.

    By default the task is left **disabled** if the sentiment directory already
    holds marked files, because switching it on would immediately start curating
    them — trimming articles and renaming files.  An empty queue registers
    enabled, since there is nothing to act on until a file is marked.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File D:\sentiment-worker\register_task.ps1
    powershell -ExecutionPolicy Bypass -File D:\sentiment-worker\register_task.ps1 -Enable
    powershell -ExecutionPolicy Bypass -File D:\sentiment-worker\register_task.ps1 -Show
    powershell -ExecutionPolicy Bypass -File D:\sentiment-worker\register_task.ps1 -Remove
#>
[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$Show,
    [switch]$Enable
)

$ErrorActionPreference = 'Stop'

$TaskName = 'SentimentCurationWorker'
$Python   = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\pythonw.exe'
$Worker   = 'D:\sentiment-worker\sentiment_worker.py'
$WorkDir  = 'D:\sentiment-worker'
$SentimentDir = 'D:\temp\McTrader\Sentiment'

if ($Show) {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { Write-Output "Task $TaskName is not registered."; exit 1 }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    [pscustomobject]@{
        Name          = $task.TaskName
        State         = $task.State
        LastRunTime   = $info.LastRunTime
        LastResult    = $info.LastTaskResult
        NextRunTime   = $info.NextRunTime
        Action        = $task.Actions[0].Execute + ' ' + $task.Actions[0].Arguments
        WorkingDir    = $task.Actions[0].WorkingDirectory
        MultipleInst  = $task.Settings.MultipleInstances
        Interval      = $task.Triggers[0].Repetition.Interval
    } | Format-List
    exit 0
}

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed scheduled task '$TaskName'."
    } else {
        Write-Output "Scheduled task '$TaskName' was not registered."
    }
    exit 0
}

foreach ($path in @($Python, $Worker)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "missing: $path" }
}

# The XML form is used because a <Repetition> with no <Duration> means
# "indefinitely", which is what a five-minute task wants; the PowerShell cmdlet
# spells the same thing with a TimeSpan that some builds reject.
$start = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ss')

# Fail-safe default: a queue that is already marked means switching this on
# would curate live files immediately, so it is registered disabled unless the
# operator says otherwise.
$marked = 0
if (Test-Path -LiteralPath $SentimentDir) {
    $marked = @(Get-ChildItem -LiteralPath $SentimentDir -Filter '*.pending.json' -File).Count
}
$taskEnabled = if ($Enable -or $marked -eq 0) { 'true' } else { 'false' }

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Curation of marked McTrader sentiment files (SYMBOL.pending.json -&gt; SYMBOL.json) with the local model at 127.0.0.1:8080. Runs only while McTrader's API answers /openapi.json; exits silently otherwise.</Description>
    <URI>\$TaskName</URI>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>$start</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>$taskEnabled</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT3H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$Python</Command>
      <Arguments>"$Worker" --scheduled</Arguments>
      <WorkingDirectory>$WorkDir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

Register-ScheduledTask -TaskName $TaskName -Xml $xml -Force | Out-Null
$info = Get-ScheduledTaskInfo -TaskName $TaskName
$state = (Get-ScheduledTask -TaskName $TaskName).State
Write-Output "Registered '$TaskName' (state: $state)."
Write-Output "Next run: $($info.NextRunTime)"
Write-Output "Inspect : powershell -ExecutionPolicy Bypass -File $PSCommandPath -Show"
if ($taskEnabled -eq 'false') {
    Write-Output ""
    Write-Output "NOT RUNNING: $marked file(s) are already marked pending in $SentimentDir,"
    Write-Output "so enabling the task would start curating them now (articles trimmed,"
    Write-Output "files renamed to *.json). Nothing has been touched."
    Write-Output "A byte-identical copy of the queue is in $WorkDir\state\backup-before-first-run."
    Write-Output "Enable with:  Enable-ScheduledTask -TaskName $TaskName"
    Write-Output "Or re-run this script with -Enable."
}
