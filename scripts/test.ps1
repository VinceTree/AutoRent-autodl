$ErrorActionPreference = 'Stop'
$projectRoot = [System.IO.Directory]::GetParent($PSScriptRoot).FullName
Set-Location -LiteralPath $projectRoot
python -m unittest discover -s tests -v
exit $LASTEXITCODE
