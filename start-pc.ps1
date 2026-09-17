$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
$taskVenvConfig = Join-Path $PSScriptRoot '.venv/pyvenv.cfg'
if (-not (Test-Path -LiteralPath $taskPython) -or -not (Test-Path -LiteralPath $taskVenvConfig)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
}
& $taskPython -m pip install --disable-pip-version-check -q -r pc/requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $taskPython pc/check_environment.py --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Repairing damaged audio dependencies...'
    & $taskPython -m pip install --disable-pip-version-check --force-reinstall -r pc/requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Dependency repair failed.' }
    & $taskPython pc/check_environment.py --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Audio dependency validation failed.' }
}
& $taskPython pc/gui.py
if ($LASTEXITCODE -ne 0) { throw 'Desktop application exited with an error.' }
