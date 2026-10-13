$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python tools/build_icon.py
if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed' }
python -m PyInstaller --noconfirm --clean AgentMeter.spec
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
Copy-Item -LiteralPath 'README.md' -Destination 'dist\README.md' -Force
Copy-Item -LiteralPath 'THIRD_PARTY_NOTICES.md' -Destination 'dist\THIRD_PARTY_NOTICES.md' -Force
Write-Output ('Built: ' + (Join-Path $PSScriptRoot 'dist\AgentMeter.exe'))
