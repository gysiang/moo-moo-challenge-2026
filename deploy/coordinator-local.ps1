param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Install", "Run", "Status")]
    [string]$Action,
    [Parameter(Mandatory = $true)]
    [string]$Database,
    [Parameter(Mandatory = $true)]
    [string]$Python,
    [Parameter(Mandatory = $true)]
    [string]$EnvFile
)

$ErrorActionPreference = "Stop"
$taskName = "Moomoo-Coordinator-DryRun"
$root = Split-Path -Parent $PSScriptRoot
$Database = (Resolve-Path -LiteralPath $Database).ProviderPath
$Python = (Resolve-Path -LiteralPath $Python).ProviderPath
$EnvFile = (Resolve-Path -LiteralPath $EnvFile).ProviderPath
$logFile = Join-Path (Split-Path -Parent $Database) "worker.log"
Set-Location -LiteralPath $root

if ($Action -eq "Run") {
    & $Python -u -m coordinator --db $Database run --env-file $EnvFile --interval 120 2>&1 |
        ForEach-Object { Add-Content -LiteralPath $logFile -Value $_ -Encoding UTF8 }
    exit $LASTEXITCODE
}

$statusJson = & $Python -m coordinator --db $Database status
if ($LASTEXITCODE -ne 0) {
    throw "Existing rehearsal ledger could not be read; no task was changed."
}
$status = $statusJson | ConvertFrom-Json
if ($status.mode -ne "DRY_RUN" -or $status.trading_enabled -ne $false -or $status.guard_halted -ne $true) {
    throw "Coordinator safety interlocks are not intact; no task was changed."
}

if ($Action -eq "Status") {
    $statusJson
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) {
        $info = $task | Get-ScheduledTaskInfo
        [pscustomobject]@{
            task_name = $task.TaskName
            task_state = [string]$task.State
            last_result = $info.LastTaskResult
            last_run = $info.LastRunTime.ToString("o")
            log_file = $logFile
        } | ConvertTo-Json -Compress
    }
    exit 0
}

$now = [DateTimeOffset]::UtcNow
$starts = [DateTimeOffset]::Parse($status.starts_at)
$expires = [DateTimeOffset]::Parse($status.expires_at)
if ($status.revoked_at -or $now -lt $starts -or $now -ge $expires) {
    throw "Rehearsal is inactive or revoked; no task was changed."
}
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw "Coordinator task already exists; inspect it instead of overwriting it."
}

$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "{0}" -Action Run -Database "{1}" -Python "{2}" -EnvFile "{3}"' -f $PSCommandPath, $Database, $Python, $EnvFile
$taskAction = New-ScheduledTaskAction -Execute (Join-Path $PSHOME "powershell.exe") -Argument $arguments -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.EndBoundary = $expires.UtcDateTime.ToString("yyyy-MM-ddTHH:mm:ssZ")
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ($expires - $now) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$task = New-ScheduledTask -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings -Description "Local 24-hour dry-run coordinator. No orders, approvals, Guard messages or automatic renewal."
Register-ScheduledTask -TaskName $taskName -InputObject $task | Out-Null
Start-ScheduledTask -TaskName $taskName
[pscustomobject]@{
    task_name = $taskName
    expires_at = $status.expires_at
    trading_enabled = $false
    guard_halted = $true
    log_file = $logFile
} | ConvertTo-Json -Compress
