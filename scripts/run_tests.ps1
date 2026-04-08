param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PytestArgs
)

$ProjectDir = Split-Path -Parent $PSScriptRoot
$WindowsPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
$LinuxPython = Join-Path $ProjectDir ".venv/bin/python"

if (Test-Path $WindowsPython) {
    $Python = $WindowsPython
} elseif (Test-Path $LinuxPython) {
    $Python = $LinuxPython
} else {
    throw "No project virtualenv interpreter found under $ProjectDir\.venv"
}

Set-Location $ProjectDir
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:PYTHONPATH = "src"

if (-not $PytestArgs -or $PytestArgs.Count -eq 0) {
    $PytestArgs = @("tests/", "-v")
}

& $Python -m pytest @PytestArgs
exit $LASTEXITCODE
