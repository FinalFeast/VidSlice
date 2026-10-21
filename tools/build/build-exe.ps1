# Build a single-file Windows executable with PyInstaller.
#
# ASCII-only on purpose: cmd.exe / PowerShell read scripts in the OEM codepage,
# so non-ASCII characters in here would corrupt parsing. The project path itself
# may contain non-ASCII characters - it is derived at runtime via $PSScriptRoot,
# never written literally.
#
# Prerequisites:
#   - a Python with PyInstaller (pip install pyinstaller), NOT the trimmed runtime
#     in tools/python - that one has no pip and no SSL:
#       C:\pybuild\python.exe -m pip install pyinstaller
#   - tools/ffmpeg populated (see build-ffmpeg-min.sh / build-ffprobe-min.sh)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tools\build\build-exe.ps1
#   powershell -ExecutionPolicy Bypass -File tools\build\build-exe.ps1 -BuildPython C:\other\python.exe

param(
    [string]$BuildPython = 'C:\pybuild\python.exe',
    [string]$Name = 'VideoSplitter'
)

$ErrorActionPreference = 'Stop'

# tools/build -> tools -> project root
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ffmpegDir = Join-Path $root 'tools\ffmpeg'
$appEntry = Join-Path $root 'app\main.py'
$iconFile = Join-Path $root 'assets\icon.ico'
$work = Join-Path $env:TEMP 'vsplit-pyi'

foreach ($f in 'ffmpeg.exe', 'ffprobe.exe', 'libgcc_s_seh-1.dll', 'libwinpthread-1.dll') {
    $p = Join-Path $ffmpegDir $f
    if (-not (Test-Path $p)) { throw "missing $p - build ffmpeg first" }
}
if (-not (Test-Path $iconFile)) { throw "missing $iconFile - see tools\build\make-icon.py" }
if (-not (Test-Path $BuildPython)) { throw "build python not found: $BuildPython" }

$args = @(
    '--onefile', '--windowed', '--noconfirm', '--clean',
    '--name', $Name,
    '--icon', $iconFile,
    '--paths', (Join-Path $root 'app'),
    # ffmpeg/ffprobe are launched as subprocesses, so they must exist as real files
    '--add-binary', "$ffmpegDir\ffmpeg.exe;ffmpeg",
    '--add-binary', "$ffmpegDir\ffprobe.exe;ffmpeg",
    '--add-binary', "$ffmpegDir\libgcc_s_seh-1.dll;ffmpeg",
    '--add-binary', "$ffmpegDir\libwinpthread-1.dll;ffmpeg",
    # the window icon is loaded at runtime via find_resource()
    '--add-data', "$iconFile;assets",
    '--distpath', (Join-Path $work 'dist'),
    '--workpath', (Join-Path $work 'build'),
    '--specpath', $work,
    $appEntry
)

& $BuildPython -m PyInstaller @args
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with $LASTEXITCODE" }

$built = Join-Path $work "dist\$Name.exe"
if (-not (Test-Path $built)) { throw "expected output missing: $built" }

$distDir = Join-Path $root 'dist'
New-Item -ItemType Directory -Force -Path $distDir | Out-Null
$target = Join-Path $distDir "$Name.exe"
Copy-Item $built $target -Force

$size = [math]::Round((Get-Item $target).Length / 1MB, 2)
Write-Output "built: $target ($size MB)"
