param([switch]$Silent)
$ErrorActionPreference = 'Stop'
$source = Join-Path $PSScriptRoot 'DotVoice'
$programs = Join-Path $env:LOCALAPPDATA 'Programs'
$target = Join-Path $programs 'DotVoice'
$manifest = Join-Path $source 'installed-files.json'
$registryKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\DotVoice'
try {
    if (!(Test-Path -LiteralPath (Join-Path $source 'DotVoice.exe')) -or !(Test-Path -LiteralPath (Join-Path $source 'core\dotvoice-core.exe')) -or !(Test-Path -LiteralPath (Join-Path $source 'core\_internal'))) { throw '安装包不完整，请重新解压完整 ZIP。' }
    if (!(Test-Path -LiteralPath $manifest)) { throw '缺少安装文件清单。' }
    $files = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
    $targetRoot = [IO.Path]::GetFullPath($target) + '\'
    $copies = New-Object System.Collections.Generic.List[object]
    foreach ($entry in $files) {
        $file = [IO.Path]::GetFullPath((Join-Path $source $entry.path))
        $destination = [IO.Path]::GetFullPath((Join-Path $target $entry.path))
        if (!$file.StartsWith($source + '\', [StringComparison]::OrdinalIgnoreCase) -or !$destination.StartsWith($targetRoot, [StringComparison]::OrdinalIgnoreCase)) { throw '安装文件路径无效。' }
        if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $entry.sha256) { throw ('安装文件损坏：' + $entry.path) }
        $copies.Add(@($file, $destination))
    }
    # An elevated DotVoice (兼容管理员权限窗口) hides its path from this script: count it too.
    # The logon task "DotVoice (管理员)" keeps working across upgrades (same DotVoice.exe path).
    $live = Get-Process -Name DotVoice, dotvoice-core -ErrorAction SilentlyContinue | Where-Object { -not $_.Path -or $_.Path.StartsWith($target + '\', [StringComparison]::OrdinalIgnoreCase) }
    if ($live) { throw '请右键托盘图标退出点声，然后重新安装。' }
    if (!$Silent) {
        Add-Type -AssemblyName System.Windows.Forms
        $choice = [Windows.Forms.MessageBox]::Show("将点声安装到：`n$target`n`n会添加桌面和开始菜单快捷方式，并登记到 Windows 已安装的应用。", '安装点声 0.11.8', 'OKCancel', 'Information')
        if ($choice -ne 'OK') { exit 0 }
    }
    New-Item -ItemType Directory -Path $programs -Force | Out-Null
    $backup = $null
    if (Test-Path -LiteralPath $target) {
        $backup = Join-Path $programs ('DotVoice-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
        # Both paths are fixed direct children of the user's Programs directory.
        if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($target)) -ne [IO.Path]::GetFullPath($programs)) { throw '安装目标路径异常。' }
        Move-Item -LiteralPath $target -Destination $backup
    }
    try {
        # Only the hash-checked files of the manifest: anything else in the unpacked folder (such as
        # a self-test's runtime\) stays out of the installation, so the uninstaller knows every file.
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        foreach ($copy in $copies) {
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($copy[1]))
            Copy-Item -LiteralPath $copy[0] -Destination $copy[1]
        }
        Copy-Item -LiteralPath $manifest -Destination (Join-Path $target 'installed-files.json')
        '{"version":"0.11.8","scope":"current-user"}' | Set-Content -LiteralPath (Join-Path $target 'installed.json') -Encoding UTF8
        $shell = New-Object -ComObject WScript.Shell
        foreach ($folder in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
            $shortcut = $shell.CreateShortcut((Join-Path $folder '点声 DotVoice.lnk'))
            $shortcut.TargetPath = Join-Path $target 'DotVoice.exe'
            $shortcut.WorkingDirectory = $target
            $shortcut.IconLocation = (Join-Path $target 'DotVoice.exe') + ',0'
            $shortcut.Description = '点声 · 语音输入与设备'
            $shortcut.Save()
        }
        New-Item -Path $registryKey -Force | Out-Null
        $uninstall = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $target 'uninstall.ps1') + '"'
        $props = @{ DisplayName='点声 DotVoice'; DisplayVersion='0.11.8'; Publisher='DotVoice'; InstallLocation=$target; DisplayIcon=(Join-Path $target 'DotVoice.exe'); UninstallString=$uninstall; QuietUninstallString=($uninstall + ' -Silent'); InstallDate=(Get-Date -Format 'yyyyMMdd') }
        foreach ($key in $props.Keys) { New-ItemProperty -Path $registryKey -Name $key -Value $props[$key] -PropertyType String -Force | Out-Null }
        foreach ($key in @('NoModify','NoRepair')) { New-ItemProperty -Path $registryKey -Name $key -Value 1 -PropertyType DWord -Force | Out-Null }
    } catch {
        # Preserve any partial install for diagnosis before restoring the backup.
        if (Test-Path -LiteralPath $target) { Move-Item -LiteralPath $target -Destination (Join-Path $programs ('DotVoice-incomplete-' + [Guid]::NewGuid().ToString('N'))) }
        if ($backup) { Move-Item -LiteralPath $backup -Destination $target }
        throw
    }
    # The backup only served the rollback above (user data lives in %LOCALAPPDATA%\DotVoice).
    if ($backup) { Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue }
    Write-Output ('Installed: ' + $target)
    if (!$Silent) { [Windows.Forms.MessageBox]::Show('安装完成。请从桌面或开始菜单打开点声。关闭窗口后驻留托盘，右键托盘图标可退出。', '点声', 'OK', 'Information') | Out-Null }
} catch {
    Write-Error $_
    exit 1
}
