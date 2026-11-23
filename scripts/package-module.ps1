param(
  [string]$BinaryPath = "target/aarch64-linux-android/release/duckd",
  [string]$DistDir = "dist",
  [string]$X86BinaryPath = ""
)
$ErrorActionPreference = "Stop"
$packager = Join-Path $PSScriptRoot "package-module.py"
$repoRoot = Split-Path -Parent $PSScriptRoot
$versionLine = Get-Content (Join-Path $repoRoot "module/module.prop") | Where-Object { $_ -match '^version=' } | Select-Object -First 1
$version = $versionLine.Split('=', 2)[1].Trim()
$pythonArgs = @($packager, "--binary", $BinaryPath, "--output", "$DistDir/duck-toolbox-$version.zip")
if ($X86BinaryPath) { $pythonArgs += @("--binary-x86-64", $X86BinaryPath) }
python @pythonArgs
if ($LASTEXITCODE -ne 0) { throw "Module validation or packaging failed." }
