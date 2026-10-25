$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python -m unittest discover -s tests -v
exit $LASTEXITCODE
