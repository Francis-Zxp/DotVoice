# Default mode is read-only. Apply/Rollback touch this user's Tidepool receiver only.
[CmdletBinding(DefaultParameterSetName = 'Check')]
param(
    [Parameter(ParameterSetName = 'Check')] [switch] $Check,
    [Parameter(Mandatory, ParameterSetName = 'Apply')] [switch] $Apply,
    [Parameter(Mandatory, ParameterSetName = 'Rollback')] [string] $Rollback
)
Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Tidepool'))
$receiverDir = Join-Path $root 'receiver'
$receiverPath = Join-Path $receiverDir 'tidepool_receiver.py'
$guardPath = Join-Path $receiverDir 'run.ps1'
$launcherPath = Join-Path $receiverDir 'run_hidden.vbs'
$backupRoot = Join-Path $root 'backups'
$payload = Join-Path $PSScriptRoot 'receiver'
$managedFiles = @('tidepool_receiver.py', 'tidepool_control.py')
$taskName = 'Tidepool Receiver (user logon)'
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$powershellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$wscriptPath = Join-Path $env:SystemRoot 'System32\wscript.exe'

function Assert-LocalPath([string] $Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full -ne $root -and -not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Refusing a target outside the current-user Tidepool installation.'
    }
    $cursor = $full
    while ($cursor -and $cursor.Length -ge $root.Length) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing a linked installation/backup path: $cursor"
            }
        }
        if ($cursor -eq $root) { break }
        $cursor = Split-Path -Parent $cursor
    }
    return $full
}

function Get-Arguments([string] $CommandLine) {
    # This installer accepts the simple, quoted argv produced by run.ps1/VBS.
    # Complex -Command/encoded/custom launches are deliberately not controlled.
    if ($CommandLine -notmatch '^\s*(?:"[^"]*"|[^\s"]+)(?:\s+(?:"[^"]*"|[^\s"]+))*\s*$') { return @() }
    return @([regex]::Matches($CommandLine, '"[^"]*"|[^\s"]+') | ForEach-Object { $_.Value.Trim('"') })
}

function Get-Role($Process) {
    $argv = @(Get-Arguments ([string]$Process.CommandLine))
    if ($argv.Count -lt 2) { return $null }
    $exe = [string]$Process.ExecutablePath
    if ($exe -eq $powershellPath -and $argv.Count -eq 9 -and $argv[0] -eq $powershellPath -and
        (($argv[1..7] -join ' ') -eq '-NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File') -and
        $argv[8] -eq $guardPath) { return 'guard' }
    if ($exe -eq $wscriptPath -and $argv.Count -eq 2 -and $argv[0] -eq $wscriptPath -and
        $argv[1] -eq $launcherPath) { return 'launcher' }
    if ($allowedPython -contains $exe -and $argv.Count -ge 3 -and $argv[0] -eq $pythonPath -and
        $argv[1] -eq '-u' -and $argv[2] -eq $receiverPath) { return 'receiver' }
    if ($argv -contains $receiverPath -or $argv -contains $guardPath -or $argv -contains $launcherPath) {
        throw "An unrecognized process uses this receiver path (PID $($Process.ProcessId)); no process will be guessed."
    }
    return $null
}

function Get-ReceiverProcesses {
    $found = @()
    foreach ($item in @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='powershell.exe' OR Name='pwsh.exe' OR Name='wscript.exe'")) {
        if ($item.ProcessId -eq $PID) { continue }
        $role = Get-Role $item
        if (-not $role) { continue }
        $owner = Invoke-CimMethod -InputObject $item -MethodName GetOwnerSid
        if ($owner.ReturnValue -ne 0 -or $owner.Sid -ne $currentSid) {
            throw "Cannot verify current-user ownership of receiver PID $($item.ProcessId)."
        }
        $found += [pscustomobject]@{ Role = $role; Process = $item }
    }
    return $found
}

function Stop-ExactProcess($Record) {
    $old = $Record.Process
    $now = Get-CimInstance Win32_Process -Filter "ProcessId=$($old.ProcessId)"
    if (-not $now) { return }
    if ($now.CreationDate -ne $old.CreationDate -or $now.ExecutablePath -ne $old.ExecutablePath -or
        $now.CommandLine -ne $old.CommandLine -or (Get-Role $now) -ne $Record.Role) {
        throw 'Process identity changed; refusing to stop a reused PID.'
    }
    $owner = Invoke-CimMethod -InputObject $now -MethodName GetOwnerSid
    if ($owner.ReturnValue -ne 0 -or $owner.Sid -ne $currentSid) { throw 'Process owner changed.' }
    Stop-Process -Id $now.ProcessId -Force -ErrorAction SilentlyContinue
}

function Stop-Receiver {
    # Stop only the exact guardian first, then re-enumerate children it could spawn.
    foreach ($record in @(Get-ReceiverProcesses | Where-Object { $_.Role -ne 'receiver' })) { Stop-ExactProcess $record }
    foreach ($record in @(Get-ReceiverProcesses | Where-Object { $_.Role -eq 'receiver' })) { Stop-ExactProcess $record }
    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        $remaining = @(Get-ReceiverProcesses)
        if ($remaining.Count -eq 0) { return }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Receiver shutdown was not confirmed; files have not been replaced.'
}

function New-Backup([bool] $WasRunning) {
    $folder = Assert-LocalPath (Join-Path $backupRoot ('dotvoice-control-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)))
    New-Item -ItemType Directory -Path $folder -Force | Out-Null
    $files = @()
    foreach ($name in $managedFiles + @('run.ps1', 'run_hidden.vbs')) {
        $source = Assert-LocalPath (Join-Path $receiverDir $name)
        $exists = Test-Path -LiteralPath $source -PathType Leaf
        $hash = $null
        if ($exists) {
            Copy-Item -LiteralPath $source -Destination (Join-Path $folder $name)
            $hash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
        }
        $files += [pscustomobject]@{ name = $name; existed = $exists; sha256 = $hash }
    }
    if ($task) {
        Export-ScheduledTask -TaskName $taskName -TaskPath '\' | Set-Content -LiteralPath (Join-Path $folder 'task.xml') -Encoding UTF8
    }
    # Snapshot only: neither Apply nor Rollback writes the installed config.
    Copy-Item -LiteralPath (Join-Path $root 'config.json') -Destination (Join-Path $folder 'config.json')
    $shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'Tidepool Receiver.lnk'
    if (Test-Path -LiteralPath $shortcut) { Copy-Item -LiteralPath $shortcut -Destination (Join-Path $folder 'Tidepool Receiver.lnk') }
    [ordered]@{ schema = 1; root = $root; was_running = $WasRunning; files = $files;
        task_present = [bool]$task; task_enabled = $taskEnabled } | ConvertTo-Json -Depth 4 |
        Set-Content -LiteralPath (Join-Path $folder 'manifest.json') -Encoding UTF8
    return $folder
}

function Read-Backup([string] $Folder) {
    $Folder = Assert-LocalPath $Folder
    if (-not $Folder.StartsWith($backupRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Rollback must use a backup inside Tidepool\backups.' }
    $manifest = Get-Content -LiteralPath (Join-Path $Folder 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema -ne 1 -or $manifest.root -ne $root) { throw 'Backup belongs to a different installation.' }
    foreach ($name in $managedFiles) {
        $entry = @($manifest.files | Where-Object { $_.name -eq $name })
        if ($entry.Count -ne 1) { throw "Invalid backup entry: $name" }
        if ($entry[0].existed) {
            $file = Assert-LocalPath (Join-Path $Folder $name)
            if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $entry[0].sha256) { throw "Backup hash mismatch: $name" }
        }
    }
    return $manifest
}

function Restore-Receiver([string] $Folder) {
    $manifest = Read-Backup $Folder
    foreach ($name in $managedFiles) {
        $entry = $manifest.files | Where-Object { $_.name -eq $name }
        $target = Assert-LocalPath (Join-Path $receiverDir $name)
        if ($entry.existed) { Copy-Item -LiteralPath (Join-Path $Folder $name) -Destination $target -Force }
        elseif (Test-Path -LiteralPath $target -PathType Leaf) { Remove-Item -LiteralPath $target -Force }
    }
}

function Test-ControlReady {
    $endpointPath = Join-Path $root 'control\endpoint.json'
    $deadline = [DateTime]::UtcNow.AddSeconds(25)
    do {
        try {
            $endpoint = Get-Content -LiteralPath $endpointPath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($endpoint.schema -ne 1 -or $endpoint.url -notmatch '^http://127\.0\.0\.1:\d+$' -or
                $endpoint.token -notmatch '^[A-Za-z0-9_-]{32,128}$') { throw 'Unexpected endpoint.' }
            $ids = @(Get-ReceiverProcesses | Where-Object { $_.Role -eq 'receiver' } | ForEach-Object { $_.Process.ProcessId })
            if ($ids -notcontains $endpoint.pid) { throw 'Endpoint belongs to another process.' }
            $status = Invoke-RestMethod -Uri ($endpoint.url + '/v1/status') -Headers @{ Authorization = 'Bearer ' + $endpoint.token } -TimeoutSec 2 -MaximumRedirection 0
            if ($status.fresh -eq $true -and $status.control_supported -eq $true -and $status.receiver_pid -eq $endpoint.pid) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

foreach ($path in @($root, $receiverDir, $receiverPath, $guardPath, $launcherPath)) { Assert-LocalPath $path | Out-Null }
foreach ($path in @($receiverPath, $guardPath, $launcherPath, (Join-Path $root 'config.json'))) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Installed Tidepool file not found: $path" }
}
$config = Get-Content -LiteralPath (Join-Path $root 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$pythonPath = [IO.Path]::GetFullPath([string]$config.python)
if ($pythonPath -ne (Join-Path $root 'venv\Scripts\python.exe')) { throw 'Only the existing current-user Tidepool venv layout is supported.' }
$allowedPython = @($pythonPath)
$venvConfig = Get-Content -LiteralPath (Join-Path $root 'venv\pyvenv.cfg') -Raw
$baseMatch = [regex]::Match($venvConfig, '(?m)^executable\s*=\s*(.+?)\s*$')
if ($baseMatch.Success) { $allowedPython += $baseMatch.Groups[1].Value.Trim() }
$task = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
$taskEnabled = $false
if ($task) {
    $actions = @($task.Actions)
    if ($actions.Count -ne 1 -or $actions[0].Execute -ne $wscriptPath -or
        $actions[0].Arguments -ne ('"' + $launcherPath + '"') -or $actions[0].WorkingDirectory -ne $receiverDir) {
        throw 'The named scheduled task has a different action; it will not be modified.'
    }
    $taskEnabled = [bool]$task.Settings.Enabled
}
$processes = @(Get-ReceiverProcesses)
$ready = @($managedFiles | Where-Object { -not (Test-Path -LiteralPath (Join-Path $payload $_) -PathType Leaf) }).Count -eq 0
if ($PSCmdlet.ParameterSetName -eq 'Check') {
    [pscustomobject]@{ mode = 'read-only'; installation = $root; payload_ready = $ready;
        task_present = [bool]$task; task_enabled = $taskEnabled;
        processes = @($processes | ForEach-Object { @{ pid = $_.Process.ProcessId; role = $_.Role; executable = $_.Process.ExecutablePath } });
        would_replace = $managedFiles; config_changed = $false; control_health_checked = $false } | ConvertTo-Json -Depth 4
    return
}
if ($Apply -and -not $ready) { throw 'Candidate receiver files are missing. No installed file was changed.' }
if ($Rollback) { $rollbackManifest = Read-Backup $Rollback }
if ($Apply) {
    & $pythonPath -B -c "import ast, pathlib, sys; [ast.parse(pathlib.Path(p).read_text(encoding='utf-8-sig'), filename=p) for p in sys.argv[1:]]" @($managedFiles | ForEach-Object { Join-Path $payload $_ })
    if ($LASTEXITCODE -ne 0) { throw 'Candidate Python syntax check failed. No installed file was changed.' }
}
$wasRunning = $processes.Count -gt 0
$backup = New-Backup $wasRunning
Write-Host "Backup: $backup"
$restart = $wasRunning -or ($Rollback -and $rollbackManifest.was_running)
try {
    if ($taskEnabled) { Disable-ScheduledTask -TaskName $taskName -TaskPath '\' | Out-Null }
    Stop-Receiver
    try {
        if ($Rollback) { Restore-Receiver $Rollback }
        else {
            foreach ($name in $managedFiles) {
                $target = Assert-LocalPath (Join-Path $receiverDir $name)
                Copy-Item -LiteralPath (Join-Path $payload $name) -Destination $target -Force
                if ((Get-FileHash -LiteralPath $target).Hash -ne (Get-FileHash -LiteralPath (Join-Path $payload $name)).Hash) { throw 'Installed file hash mismatch.' }
            }
        }
    } catch {
        Restore-Receiver $backup
        if ($restart) { Start-Process -FilePath $wscriptPath -ArgumentList ('"' + $launcherPath + '"') -WorkingDirectory $receiverDir -WindowStyle Hidden | Out-Null }
        throw
    }
    if ($restart) {
        Start-Process -FilePath $wscriptPath -ArgumentList ('"' + $launcherPath + '"') -WorkingDirectory $receiverDir -WindowStyle Hidden | Out-Null
        if ($Apply -and -not (Test-ControlReady)) { throw "Receiver updated, but control health was not confirmed. Review logs or use -Rollback `"$backup`"." }
    }
    Write-Host 'Complete. Existing config, PIN, firmware, Wi-Fi and autostart action were preserved.'
    if (-not $restart) { Write-Host 'The receiver was not running, so it remains stopped. Use its existing launcher when ready.' }
} finally {
    if ($taskEnabled) { Enable-ScheduledTask -TaskName $taskName -TaskPath '\' | Out-Null }
}
