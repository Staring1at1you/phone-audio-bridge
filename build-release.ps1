param(
    [string]$Version = '0.7.1',
    [string]$AdbSource = '',
    [switch]$SkipAndroidBuild
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$taskPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$taskVenvConfig = Join-Path $PSScriptRoot '.venv/pyvenv.cfg'
if (-not (Test-Path -LiteralPath $taskPython) -or -not (Test-Path -LiteralPath $taskVenvConfig)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
}
& $taskPython -m pip install --disable-pip-version-check -q -r pc/requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }

& $taskPython -m unittest discover -s pc -p 'test_*.py' -q
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }

if (-not $SkipAndroidBuild) {
    & (Join-Path $PSScriptRoot 'build-android.ps1')
}
$taskApk = Join-Path $PSScriptRoot 'android/app/build/outputs/apk/debug/app-debug.apk'
if (-not (Test-Path -LiteralPath $taskApk)) { throw 'Android APK is missing.' }

if (-not $AdbSource) {
    $taskAdbCommand = Get-Command adb -ErrorAction SilentlyContinue
    if (-not $taskAdbCommand) { throw 'ADB not found. Supply -AdbSource path/to/platform-tools.' }
    $AdbSource = Split-Path -LiteralPath $taskAdbCommand.Source
}
$taskAdbDirectory = (Resolve-Path -LiteralPath $AdbSource).Path
$taskAdbFiles = @('adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll', 'libwinpthread-1.dll')
foreach ($taskFile in $taskAdbFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $taskAdbDirectory $taskFile))) {
        throw "Required platform-tools file is missing: $taskFile"
    }
}

& $taskPython -m PyInstaller --clean --noconfirm PhoneAudioBridge.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

$taskStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$taskArtifactRoot = Join-Path $PSScriptRoot 'artifacts'
$taskPackageName = "PhoneAudioBridge-$Version-windows-x64-$taskStamp"
$taskPackage = Join-Path $taskArtifactRoot $taskPackageName
New-Item -ItemType Directory -Path $taskPackage -Force | Out-Null
Copy-Item -LiteralPath 'dist/PhoneAudioBridge.exe' -Destination $taskPackage
foreach ($taskFile in $taskAdbFiles) {
    Copy-Item -LiteralPath (Join-Path $taskAdbDirectory $taskFile) -Destination $taskPackage
}
Copy-Item -LiteralPath $taskApk -Destination (Join-Path $taskPackage 'PhoneAudioBridge-Android.apk')
Copy-Item -LiteralPath 'README.md' -Destination $taskPackage
Copy-Item -LiteralPath 'QUICK_START.txt' -Destination $taskPackage

$taskZip = Join-Path $taskArtifactRoot "$taskPackageName.zip"
Compress-Archive -LiteralPath $taskPackage -DestinationPath $taskZip -CompressionLevel Optimal
Write-Output $taskZip
