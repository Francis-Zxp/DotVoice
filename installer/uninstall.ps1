param([switch]$Silent)
$ErrorActionPreference = 'Stop'
$target = [IO.Path]::GetFullPath($PSScriptRoot)
$expected = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'Programs\DotVoice'))
if ($target -ne $expected -or !(Test-Path -LiteralPath (Join-Path $target 'installed.json'))) { throw '拒绝移除未知安装目录。' }
# An elevated DotVoice (兼容管理员权限窗口) hides its path from this non-elevated script: count it too.
if (Get-Process -Name DotVoice, dotvoice-core -ErrorAction SilentlyContinue | Where-Object { -not $_.Path -or $_.Path.StartsWith($target + '\', [StringComparison]::OrdinalIgnoreCase) }) { throw '请先右键托盘图标退出点声。' }
if (!$Silent) {
    Add-Type -AssemblyName System.Windows.Forms
    if ([Windows.Forms.MessageBox]::Show('卸载点声？使用记录将保留在本地，方便以后重新安装。', '卸载点声', 'OKCancel', 'Question') -ne 'OK') { exit 0 }
}
# 兼容管理员权限窗口: the elevated logon task "DotVoice (管理员)" goes first when it starts this
# copy. Deleting it may take one administrator confirmation; without it nothing is removed.
$taskName = 'DotVoice (管理员)'
$schtasks = Join-Path ([Environment]::SystemDirectory) 'schtasks.exe'
# Windows PowerShell 5.1 turns redirected stderr of a native command into a terminating error
# under 'Stop' (e.g. "task not found"): run schtasks under 'Continue' and judge its exit code.
$prev = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
$taskXml = (& $schtasks /Query /TN $taskName /XML 2>$null) -join "`n"; $queryExit = $LASTEXITCODE
$ErrorActionPreference = $prev
if ($queryExit -eq 0 -and $taskXml) {
    $command = "$(([xml]$taskXml).Task.Actions.Exec.Command)".Trim().Trim('"')
    if ($command -and [IO.Path]::GetFullPath($command).StartsWith($expected + '\', [StringComparison]::OrdinalIgnoreCase)) {
        $ErrorActionPreference = 'Continue'; & $schtasks /Delete /TN $taskName /F *> $null; $deleteExit = $LASTEXITCODE; $ErrorActionPreference = $prev
        if ($deleteExit -ne 0) {
            try {
                $elevated = Start-Process -FilePath $schtasks -ArgumentList @('/Delete', '/TN', ('"' + $taskName + '"'), '/F') -Verb RunAs -WindowStyle Hidden -Wait -PassThru
                if ($elevated.ExitCode -ne 0) { throw "schtasks $($elevated.ExitCode)" }
            } catch { throw '没有删除管理员启动任务“DotVoice (管理员)”（需要管理员确认），卸载未进行。' }
        }
    }
}
# Delete only registered files. Any additional user files remain untouched.
$files = Get-Content -LiteralPath (Join-Path $target 'installed-files.json') -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($name in @($files.path) + @('installed.json','installed-files.json')) {
    $file = [IO.Path]::GetFullPath((Join-Path $target $name))
    if (!$file.StartsWith($expected + '\', [StringComparison]::OrdinalIgnoreCase)) { throw '卸载清单路径无效。' }
    if (Test-Path -LiteralPath $file -PathType Leaf) { Remove-Item -LiteralPath $file -Force }
}
# Then a self-test's results (DotVoice.exe --smoke run from this folder keeps them in runtime\smoke;
# user data lives in %LOCALAPPDATA%\DotVoice) and the folders left empty (deepest first), so the next
# install does not back up an empty tree. Folders holding other files stay; a leftover folder is
# harmless, so this never fails the uninstall.
try {
    $smoke = Join-Path $target 'runtime\smoke'
    # .NET removes a link itself, never what it points to (5.1's Remove-Item -Recurse follows junctions).
    if ((Test-Path -LiteralPath $smoke -PathType Container) -and !((Get-Item -LiteralPath (Split-Path -Parent $smoke) -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { [IO.Directory]::Delete($smoke, $true) }
    Get-ChildItem -LiteralPath $target -Directory -Recurse | Sort-Object { $_.FullName.Length } -Descending |
        Where-Object { -not (Get-ChildItem -LiteralPath $_.FullName -Force) } | Remove-Item -ErrorAction SilentlyContinue
    if (-not (Get-ChildItem -LiteralPath $target -Force)) { Remove-Item -LiteralPath $target -ErrorAction SilentlyContinue }
} catch { }
foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
    $shortcut = Join-Path $folder '点声 DotVoice.lnk'
    if (Test-Path -LiteralPath $shortcut) {
        $shell = New-Object -ComObject WScript.Shell
        if ($shell.CreateShortcut($shortcut).TargetPath -eq (Join-Path $expected 'DotVoice.exe')) { Remove-Item -LiteralPath $shortcut }
    }
}
Remove-Item -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\DotVoice' -ErrorAction SilentlyContinue
Write-Output '点声已卸载。用户记录已保留。'
