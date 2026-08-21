$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$bin = Join-Path (Split-Path (Get-Command python).Source -Parent) "Library\bin"
pyinstaller --noconfirm --clean --onefile --name "RCT-Randomizer" --add-data "static;static" --add-binary "$bin\sqlite3.dll;." --add-binary "$bin\libcrypto-3-x64.dll;." --add-binary "$bin\libssl-3-x64.dll;." --add-binary "$bin\liblzma.dll;." --add-binary "$bin\libbz2.dll;." app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
Copy-Item README.md "dist\README.txt" -Force
Write-Host "Build complete: dist\RCT-Randomizer.exe"
