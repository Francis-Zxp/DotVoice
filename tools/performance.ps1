$ErrorActionPreference = 'Stop'
$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$exe = Join-Path $project 'dist\0.3.0\DotVoice\DotVoice.exe'
$process = Start-Process -FilePath $exe -ArgumentList '--smoke-ui','--smoke-seconds','22' -WorkingDirectory (Split-Path $exe) -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 7
function Get-AppProcesses {
    $all = @(Get-CimInstance Win32_Process)
    $ids = @($process.Id)
    for ($i=0; $i -lt 5; $i++) {
        $ids = @($ids + @($all | Where-Object { $_.ParentProcessId -in $ids } | Select-Object -ExpandProperty ProcessId) | Select-Object -Unique)
    }
    @(Get-Process -Id $ids -ErrorAction SilentlyContinue)
}
$before = @(Get-AppProcesses)
$cpuBefore = ($before | Measure-Object CPU -Sum).Sum
$timer = [Diagnostics.Stopwatch]::StartNew()
Start-Sleep -Seconds 10
$after = @(Get-AppProcesses)
$elapsed = $timer.Elapsed.TotalSeconds
$cpuAfter = ($after | Measure-Object CPU -Sum).Sum
$report = [ordered]@{
    mode = 'Paused; UI visible; microphone never opened; includes descendant WebView2 processes'
    seconds = [Math]::Round($elapsed,2)
    processCount = $after.Count
    cpuPercentOfOneCore = [Math]::Round(100 * ($cpuAfter-$cpuBefore)/$elapsed,2)
    totalWorkingSetMiB = [Math]::Round(($after | Measure-Object WorkingSet64 -Sum).Sum/1MB,1)
    note = 'Short idle sample only; working-set sum can count shared pages more than once. Not pen battery consumption.'
}
$report | ConvertTo-Json | Set-Content -Encoding utf8 (Join-Path $project 'design\idle-performance.json')
$report | ConvertTo-Json
$process.WaitForExit(15000) | Out-Null
