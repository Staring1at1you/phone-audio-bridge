param(
    [string]$Sdk = 'D:/Android/Sdk',
    [string]$Gradle = '',
    [switch]$Install
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Sdk)) { throw 'Supply an Android SDK path using -Sdk.' }
if (-not $Gradle) {
    $taskGradleCommand = Get-Command gradle -ErrorAction SilentlyContinue
    if ($taskGradleCommand) {
        $Gradle = $taskGradleCommand.Source
    } else {
        $taskGradleCache = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.gradle/wrapper/dists/gradle-8.9-bin'
        $taskGradleFile = Get-ChildItem -LiteralPath $taskGradleCache -Filter gradle.bat -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($taskGradleFile) { $Gradle = $taskGradleFile.FullName }
    }
}
if (-not $Gradle) { throw 'Install Gradle 8.9 or supply -Gradle path/to/gradle.bat.' }
$env:ANDROID_HOME = (Resolve-Path -LiteralPath $Sdk).Path
Push-Location -LiteralPath (Join-Path $PSScriptRoot 'android')
try {
    & $Gradle --no-daemon assembleDebug
    if ($LASTEXITCODE -ne 0) { throw 'Android build failed.' }
    if ($Install) {
        adb -d install -r app/build/outputs/apk/debug/app-debug.apk
        if ($LASTEXITCODE -ne 0) { throw 'APK installation failed.' }
        adb -d shell am start -n dev.usbaudio/.MainActivity
        if ($LASTEXITCODE -ne 0) { throw 'App launch failed.' }
    }
} finally {
    Pop-Location
}
