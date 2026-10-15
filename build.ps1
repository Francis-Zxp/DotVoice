$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$env:PYTHONPATH = ''
$version = '0.11.6'
$output = Join-Path $PSScriptRoot "dist\$version"
$distRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'dist'))
$output = [IO.Path]::GetFullPath($output)
if ([IO.Path]::GetDirectoryName($output) -ne $distRoot) { throw 'Build output must be a direct child of dist' }
$app = Join-Path $output 'DotVoice'
Add-Type -AssemblyName Microsoft.VisualBasic

# A portable copy (no installed.json beside DotVoice.exe: a dist folder run directly, or the
# unzipped release) keeps the user's data in DotVoice\runtime: prefs, the encrypted API key,
# notes, usage, devices and diagnostics. Only runtime\smoke is --smoke output.
function Test-PortableData([string]$Folder) {
    $runtime = Join-Path $Folder 'DotVoice\runtime'
    if (-not (Test-Path -LiteralPath $runtime)) { return $false }
    try {
        return [bool]@(Get-ChildItem -LiteralPath $runtime -Force | Where-Object { $_.Name -ne 'smoke' }).Count
    } catch { return $true }  # unreadable: treat it as data
}

function Test-RunningFrom([string]$Folder) {
    [bool](Get-Process -Name DotVoice, dotvoice-core -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($Folder + '\', [StringComparison]::OrdinalIgnoreCase) })
}

# The Recycle Bin (recoverable). Windows may still delete outright what the bin cannot take, so
# only build output is ever sent here, never a portable copy's data.
function Remove-ToRecycleBin([string]$Folder) {
    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteDirectory($Folder, 'OnlyErrorDialogs', 'SendToRecycleBin')
}

# Start from nothing but the portable data: DotVoice\runtime is moved aside (a rename within the
# project: nothing is copied or deleted), the rest of dist\<ver> goes to the Recycle Bin, and the
# data goes back into the fresh folder, even when recycling fails. The manifest and the release
# zips leave runtime out, so it never reaches an install or a download.
function Clear-BuildOutput([string]$Output, [string]$Backups) {
    if (-not (Test-Path -LiteralPath $Output)) { return }
    if (Test-RunningFrom $Output) { throw "DotVoice is running from $Output; close it before building" }
    $runtime = Join-Path $Output 'DotVoice\runtime'
    $aside = $null
    if (Test-PortableData $Output) {
        New-Item -ItemType Directory -Path $Backups -Force | Out-Null
        $aside = Join-Path $Backups ('runtime-before-build-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
        [IO.Directory]::Move($runtime, $aside)
    }
    try {
        Remove-ToRecycleBin $Output
    } finally {
        if ($aside) {
            try {
                New-Item -ItemType Directory -Path (Split-Path -Parent $runtime) -Force | Out-Null
                [IO.Directory]::Move($aside, $runtime)
            } catch {
                throw "The portable data of $runtime is in $aside; move it back by hand. $_"
            }
            Write-Output "Kept the portable data in $runtime"
        }
    }
}

# Earlier versions' output goes to the Recycle Bin, never the folder just built. Folders not
# named like a version stay, and so do junctions, a folder DotVoice is running from and one whose
# DotVoice\runtime holds a portable copy's data.
function Remove-OldBuilds([string]$DistRoot, [string]$Current) {
    # Enumeration expands Windows 8.3 aliases; compare the same canonical paths.
    $DistRoot = (Get-Item -LiteralPath $DistRoot -Force).FullName
    $Current = (Get-Item -LiteralPath $Current -Force).FullName
    foreach ($old in @(Get-ChildItem -LiteralPath $DistRoot -Directory -Force)) {
        $folder = [IO.Path]::GetFullPath($old.FullName)
        if ($folder -eq $Current -or [IO.Path]::GetDirectoryName($folder) -ne $DistRoot) { continue }
        if ($old.Name -notmatch '^\d+(\.\d+)+(-[0-9A-Za-z.-]+)?$') { continue }
        if ($old.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        if (Test-RunningFrom $folder) {
            Write-Warning "Kept $folder (DotVoice is running from it)"
            continue
        }
        if (Test-PortableData $folder) {
            Write-Warning "Kept $folder (its DotVoice\runtime holds a portable copy's settings, key or notes; move them, then remove the folder)"
            continue
        }
        try {
            Remove-ToRecycleBin $folder
            Write-Output "Moved to the Recycle Bin: $folder"
        } catch {
            Write-Warning "Could not move $folder to the Recycle Bin: $_"
        }
    }
}

Clear-BuildOutput $output (Join-Path $PSScriptRoot 'backups')

# 1. Python core: console subsystem so piped stdio works; the shell starts it
#    with CREATE_NO_WINDOW, so no console ever appears.
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --console --name dotvoice-core `
    --distpath (Join-Path $output 'core-build') --workpath build\core --specpath build\core `
    --additional-hooks-dir tools\hooks --hidden-import hid --copy-metadata hidapi `
    --exclude-module sherpa_onnx --exclude-module pypinyin --exclude-module numpy `
    --exclude-module webview --exclude-module clr --exclude-module pythonnet --exclude-module tkinter `
    --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide2 --exclude-module PySide6 `
    core_main.py
if ($LASTEXITCODE -ne 0) { throw 'Core build failed' }
# 2. Tauri shell (frontend is built by beforeBuildCommand).
Push-Location shell
try {
    pnpm install --frozen-lockfile
    if ($LASTEXITCODE -ne 0) { throw 'pnpm install failed' }
    pnpm tauri build --no-bundle
    if ($LASTEXITCODE -ne 0) { throw 'Shell build failed' }
} finally { Pop-Location }

# 3. Assemble the portable folder used by Install.cmd.
New-Item -ItemType Directory -Path $app -Force | Out-Null
Copy-Item -LiteralPath 'shell\src-tauri\target\release\DotVoice.exe' -Destination $app
Move-Item -LiteralPath (Join-Path $output 'core-build\dotvoice-core') -Destination (Join-Path $app 'core')
$coreBuild = [IO.Path]::GetFullPath((Join-Path $output 'core-build'))
if ([IO.Path]::GetDirectoryName($coreBuild) -ne $output) { throw 'Core build path is invalid' }
Remove-Item -LiteralPath $coreBuild -Recurse -Force
Copy-Item config.json,README.md,VALIDATION.md,ACCEPTANCE.md,RELEASE_0.11.6.md,LICENSE,THIRD_PARTY_PV60.md -Destination $app
Copy-Item -LiteralPath licenses -Destination $app -Recurse -Force
$integration = Join-Path $app 'integrations\tidepool'
New-Item -ItemType Directory -Path (Join-Path $integration 'receiver') -Force | Out-Null
Copy-Item -LiteralPath 'integrations\tidepool\install-control.ps1','integrations\tidepool\README.md' -Destination $integration
Copy-Item -LiteralPath 'integrations\tidepool\receiver\tidepool_receiver.py','integrations\tidepool\receiver\tidepool_control.py' -Destination (Join-Path $integration 'receiver')
Copy-Item -LiteralPath 'installer\uninstall.ps1' -Destination $app
& .\.venv\Scripts\python.exe tools\install_manifest.py $app
if ($LASTEXITCODE -ne 0) { throw 'Manifest failed' }
Write-Output "Built $app"

# 4. Only now, after a successful build: earlier versions' output (see Remove-OldBuilds).
Remove-OldBuilds $distRoot $output
