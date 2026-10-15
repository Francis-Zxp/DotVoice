# Offline identity tests: parse the installer but never execute its top-level code.
$ErrorActionPreference = 'Stop'
$file = Join-Path (Split-Path -Parent $PSScriptRoot) 'integrations\tidepool\install-control.ps1'
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($file, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
foreach ($name in @('Get-Arguments', 'Get-Role')) {
    $function = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    if (-not $function) { throw "Missing function: $name" }
    . ([scriptblock]::Create($function.Extent.Text))
}
$root = 'C:\Users\Test User\AppData\Local\Tidepool'
$receiverPath = Join-Path $root 'receiver\tidepool_receiver.py'
$guardPath = Join-Path $root 'receiver\run.ps1'
$launcherPath = Join-Path $root 'receiver\run_hidden.vbs'
$pythonPath = Join-Path $root 'venv\Scripts\python.exe'
$powershellPath = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
$wscriptPath = 'C:\Windows\System32\wscript.exe'
$allowedPython = @($pythonPath, 'D:\Python\python.exe')
function Assert-Role($Exe, $Command, $Expected) {
    $actual = Get-Role ([pscustomobject]@{ ExecutablePath = $Exe; CommandLine = $Command; ProcessId = 999 })
    if ($actual -ne $Expected) { throw "Expected '$Expected', got '$actual'." }
}
Assert-Role $pythonPath ('"{0}" -u "{1}" --port 17876' -f $pythonPath, $receiverPath) 'receiver'
Assert-Role 'D:\Python\python.exe' ('"{0}" -u "{1}" --port 17876' -f $pythonPath, $receiverPath) 'receiver'
Assert-Role $powershellPath ('"{0}" -NoLogo -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{1}"' -f $powershellPath, $guardPath) 'guard'
Assert-Role $wscriptPath ('"{0}" "{1}"' -f $wscriptPath, $launcherPath) 'launcher'
Assert-Role $pythonPath ('"{0}" -u "{1}.other"' -f $pythonPath, $receiverPath) $null
Assert-Role $powershellPath ('"{0}" -Command "Write-Output Tidepool"' -f $powershellPath) $null
$rejected = $false
try { Assert-Role 'C:\other\python.exe' ('"{0}" -u "{1}"' -f $pythonPath, $receiverPath) $null }
catch { $rejected = $true }
if (-not $rejected) { throw 'Unrecognized interpreter was not rejected.' }
Write-Output 'Installer parses; 7 offline process-identity cases passed. No processes or installed files were changed.'
