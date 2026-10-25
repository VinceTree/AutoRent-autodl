param([switch]$Demo, [switch]$ReadOnly)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) {
    Write-Host 'Python 3.10+ is required. Install Python with tkinter support.'
    Read-Host 'Press Enter to close'
    exit 1
}
$launchArgs = @((Join-Path $PSScriptRoot 'autodl_quick.py'), 'gui')
if ($Demo) { $launchArgs += '--demo' }
if ($ReadOnly) { $launchArgs += '--read-only' }
& $pythonCommand.Source @launchArgs
if ($LASTEXITCODE -ne 0) { Read-Host 'Press Enter to close' }
