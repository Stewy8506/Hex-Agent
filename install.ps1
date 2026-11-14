# ==============================================================================
# HEX AI Workbench — Windows PowerShell Universal Installer
# Compatible with Windows 10 / 11 and Windows Server (x64 / arm64)
#
# Usage from PowerShell (Admin or User):
#   powershell -ExecutionPolicy ByPass -File .\install.ps1
# Or from cloned repository:
#   .\install.ps1
# ==============================================================================

$ErrorActionPreference = "Stop"

# Paths & Directories
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not $ScriptDir) { $ScriptDir = (Get-Location).Path }

$HexDir = if ($env:HEX_DIR) { $env:HEX_DIR } else { Join-Path $env:USERPROFILE ".hex" }
$VenvDir = Join-Path $ScriptDir ".venv"
$BinDir = if ($env:HEX_BIN_DIR) { $env:HEX_BIN_DIR } else { Join-Path $env:USERPROFILE ".local\bin" }

function Print-Banner {
    Write-Host "  _   _ _______  __" -ForegroundColor Cyan
    Write-Host " | | | | ____\ \/ /   HEX AI WORKBENCH" -ForegroundColor Cyan
    Write-Host " | |_| |  _|  \  /    Autonomous AI Workbench & Interactive TUI" -ForegroundColor Cyan
    Write-Host " |  _  | |___ /  \    CoW Snapshots • Call Journal • Graphify AST" -ForegroundColor Cyan
    Write-Host " |_| |_|_____/_/\_\   https://github.com/Stewy8506/Harness" -ForegroundColor Cyan
}

function Write-Info($msg) {
    Write-Host "▶ $msg" -ForegroundColor Cyan
}

function Write-Success($msg) {
    Write-Host "✓ $msg" -ForegroundColor Green
}

function Write-WarnMsg($msg) {
    Write-Host "! $msg" -ForegroundColor Yellow
}

function Write-ErrMsg($msg) {
    Write-Host "✗ $msg" -ForegroundColor Red
}

Print-Banner
Write-Host ""

# 1. Detect Operating System & Architecture
$Arch = if ([System.Environment]::Is64BitOperatingSystem) { "x64" } else { "x86" }
Write-Info "Detected platform: Windows ($Arch)"

# 2. Check Git availability
Write-Info "Checking Git installation..."
$GitCmd = Get-Command git -ErrorAction SilentlyContinue
if (-not $GitCmd) {
    Write-ErrMsg "Git is required but was not found in your PATH."
    Write-Host "  ↳ Please install Git for Windows: https://git-scm.com/download/win"
    Write-Host "  ↳ Or install via winget: winget install Git.Git"
    exit 1
}
$GitVer = (git --version)
Write-Success "Git available: $GitVer"

# 3. Locate compatible Python (>= 3.10)
Write-Info "Searching for Python 3.10+ runtime..."
$PythonCmd = $null
$PythonArgs = @()

$candidates = @(
    @{ Name = "py"; Args = @("-3") },
    @{ Name = "python"; Args = @() },
    @{ Name = "python3"; Args = @() }
)

foreach ($c in $candidates) {
    $found = Get-Command $c.Name -ErrorAction SilentlyContinue
    if ($found) {
        $checkArgs = $c.Args + @("-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)")
        try {
            & $c.Name $checkArgs 2>$null
            if ($LASTEXITCODE -eq 0) {
                $PythonCmd = $c.Name
                $PythonArgs = $c.Args
                break
            }
        } catch {
            # continue search
        }
    }
}

if (-not $PythonCmd) {
    Write-ErrMsg "Python 3.10+ is required but was not found on your system."
    Write-Host "  ↳ Download from: https://www.python.org/downloads/"
    Write-Host "  ↳ Or install via winget: winget install Python.Python.3.11"
    exit 1
}

$pyVerArgs = $PythonArgs + @("-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
$PyVer = (& $PythonCmd $pyVerArgs)
Write-Success "Using Python: $PythonCmd (v$PyVer)"

# 4. Check for Rust / Cargo (Optional native TUI cockpit build)
Write-Info "Checking Rust / Cargo toolchain..."
$CargoCmd = Get-Command cargo -ErrorAction SilentlyContinue
if ($CargoCmd) {
    $CargoVer = (cargo --version)
    Write-Success "Cargo detected: $CargoVer"
    $HexTuiCrate = Join-Path $ScriptDir "crates\hex-tui\Cargo.toml"
    if (Test-Path $HexTuiCrate) {
        Write-Info "Building native Ratatui cockpit (crates/hex-tui)..."
        try {
            & cargo build --release --manifest-path $HexTuiCrate
            if ($LASTEXITCODE -eq 0) {
                Write-Success "Native hex-tui binary compiled successfully in crates/hex-tui/target/release"
            } else {
                Write-WarnMsg "Cargo build exited with non-zero code. Falling back to Python console mode."
            }
        } catch {
            Write-WarnMsg "Cargo build encountered an error. Falling back to Python console mode."
        }
    }
} else {
    Write-Info "Cargo/Rust not detected. Skipping native hex-tui build (Python console fallback will be used)."
}

# 5. Create virtual environment in .venv
Write-Info "Setting up virtual environment in $VenvDir..."
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    $venvArgs = $PythonArgs + @("-m", "venv", $VenvDir)
    & $PythonCmd $venvArgs
    if ($LASTEXITCODE -ne 0) {
        Write-ErrMsg "Failed to create virtual environment in $VenvDir"
        exit 1
    }
}
Write-Success "Virtual environment initialized."

# Upgrade packaging tools in .venv
Write-Info "Checking packaging tools in environment..."
try {
    & $VenvPython -m pip install --retries 1 --timeout 5 --quiet --upgrade pip setuptools wheel 2>$null
} catch {
    # Non-fatal if offline
}

# 6. Install dependencies from pyproject.toml (editable mode)
Write-Info "Installing HEX from local repository checkout: $ScriptDir..."
$PyProject = Join-Path $ScriptDir "pyproject.toml"
if (Test-Path $PyProject) {
    $installSpec = "$ScriptDir[tui]"
    try {
        & $VenvPython -m pip install --quiet -e $installSpec 2>$null
    } catch {
        # Fallback to plain editable
        & $VenvPython -m pip install --quiet -e $ScriptDir
    }
} else {
    Write-Info "Installing HEX package via pip..."
    & $VenvPython -m pip install --quiet "hex-ai[tui]"
}

# 7. Create wrapper scripts in User PATH (~/.local/bin)
if (-not (Test-Path $BinDir)) {
    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
}

$HexBatSource = Join-Path $ScriptDir "hex.bat"
$HexBatDest = Join-Path $BinDir "hex.bat"
if (Test-Path $HexBatSource) {
    Copy-Item -Path $HexBatSource -Destination $HexBatDest -Force
} else {
    $batContent = @"
@echo off
setlocal
set "VENV_PY=$VenvPython"
if exist "%VENV_PY%" (
    "%VENV_PY%" -m harness_core.cli %*
    exit /b %ERRORLEVEL%
)
python -m harness_core.cli %*
exit /b %ERRORLEVEL%
"@
    Set-Content -Path $HexBatDest -Value $batContent -Encoding Ascii
}

# Also create hex.ps1 wrapper for native PowerShell execution
$HexPs1Dest = Join-Path $BinDir "hex.ps1"
$ps1Wrapper = @"
& "$VenvPython" -m harness_core.cli `$args
"@
Set-Content -Path $HexPs1Dest -Value $ps1Wrapper -Encoding Utf8

Write-Success "Installed CLI wrappers to $BinDir (hex.bat and hex.ps1)"

# 8. Verify PATH contains BinDir
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$MachinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
$AllPaths = "$UserPath;$MachinePath;$env:Path"
$PathNeedsUpdate = ($AllPaths -notlike "*$BinDir*")

# 9. Initialize default configuration if uninitialized
$ConfigFile = Join-Path $HexDir "config.yaml"
if (-not (Test-Path $ConfigFile)) {
    Write-Info "Running initial onboarding to generate hardware configuration..."
    try {
        & $VenvPython -m harness_core.cli onboard --defaults --config-dir $HexDir 2>$null | Out-Null
        Write-Success "Configuration initialized at $ConfigFile"
    } catch {
        Write-WarnMsg "Automatic onboarding step skipped. You can run 'hex onboard' anytime."
    }
}

Write-Host ""
Write-Host "======================================================================" -ForegroundColor Green
Write-Host "   HEX AI WORKBENCH INSTALLED SUCCESSFULLY!                           " -ForegroundColor Green
Write-Host "======================================================================" -ForegroundColor Green
Write-Host ""

if ($PathNeedsUpdate) {
    Write-WarnMsg "$BinDir is not currently in your system PATH."
    Write-Host "  ↳ Add it permanently to your User PATH by running in PowerShell:"
    Write-Host "    [Environment]::SetEnvironmentVariable('Path', `"`$([Environment]::GetEnvironmentVariable('Path', 'User'));$BinDir`", 'User')" -ForegroundColor Cyan
    Write-Host "    `$env:Path += `";$BinDir`"" -ForegroundColor Cyan
    Write-Host ""
}

Write-Host "Quick Start Commands:"
Write-Host "  • Start Interactive TUI:     hex   (or hex chat)" -ForegroundColor Cyan
Write-Host "  • Run System Diagnostics:    hex doctor" -ForegroundColor Cyan
Write-Host "  • Run Benchmark Suite:       hex eval --suite core" -ForegroundColor Cyan
Write-Host "  • Search Past Tasks:         hex search `"query`"" -ForegroundColor Cyan
Write-Host "  • Inspect Antigravity Quota: hex quota" -ForegroundColor Cyan
Write-Host "  • Autonomous Task Execution: hex run `"Add feature X`"" -ForegroundColor Cyan
Write-Host ""
Write-Host "Documentation & Updates: https://github.com/Stewy8506/Harness"
Write-Host ""
