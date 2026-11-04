#!/usr/bin/env bash
# ==============================================================================
# HEX AI Workbench — One-Line Universal Installer
# Compatible with macOS (Apple Silicon / Intel) and Linux (x86_64 / arm64)
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Stewy8506/Harness/main/install.sh | bash
# Or from a cloned repository:
#   ./install.sh
# ==============================================================================

set -euo pipefail

HEX_DIR="${HEX_DIR:-$HOME/.hex}"
VENV_DIR="$HEX_DIR/env"
BIN_DIR="${HEX_BIN_DIR:-$HOME/.local/bin}"
REPO_URL="https://github.com/Stewy8506/Harness.git"
PACKAGE_SPEC="${HEX_PACKAGE_SPEC:-git+https://github.com/Stewy8506/Harness.git}"

# ANSI color codes
BOLD='\033[1m'
CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

print_banner() {
    cat << "EOF"
  _   _ _______  __
 | | | | ____\ \/ /   HEX AI WORKBENCH
 | |_| |  _|  \  /    Autonomous AI Workbench & Interactive TUI
 |  _  | |___ /  \    CoW Snapshots • Call Journal • Graphify AST
 |_| |_|_____/_/\_\   https://github.com/Stewy8506/Harness
EOF
}

info() {
    printf "${CYAN}▶${NC} %s\n" "$1"
}

success() {
    printf "${GREEN}✓${NC} %s\n" "$1"
}

warn() {
    printf "${YELLOW}!${NC} %s\n" "$1"
}

error() {
    printf "${RED}✗${NC} %s\n" "$1" >&2
}

# 1. Detect Operating System and Architecture
OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS" in
    Darwin)
        PLATFORM="macOS"
        ;;
    Linux)
        PLATFORM="Linux"
        ;;
    *)
        error "Unsupported operating system: $OS. HEX supports macOS and Linux."
        exit 1
        ;;
esac

print_banner
echo ""
info "Detected platform: $PLATFORM ($ARCH)"

# 2. Locate compatible Python (>= 3.10)
PYTHON_BIN=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
            PYTHON_BIN="$(command -v "$candidate")"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    error "Python 3.10+ is required but was not found on your system."
    if [ "$OS" = "Darwin" ]; then
        echo "  ↳ On macOS: brew install python3"
    else
        echo "  ↳ On Ubuntu/Debian: sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
        echo "  ↳ On Fedora/RHEL:   sudo dnf install -y python3 python3-pip"
    fi
    exit 1
fi

PY_VER="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
success "Using Python: $PYTHON_BIN (v$PY_VER)"

# 3. Create isolated virtual environment at ~/.hex/env
info "Setting up isolated virtual environment in $VENV_DIR..."
mkdir -p "$HEX_DIR"
if [ ! -d "$VENV_DIR" ] || [ ! -f "$VENV_DIR/bin/python" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
success "Virtual environment initialized."

# Ensure pip & setuptools are up to date if internet is reachable
info "Checking packaging tools in environment..."
"$VENV_DIR/bin/python" -m pip install --retries 1 --timeout 5 --quiet --upgrade pip setuptools wheel 2>/dev/null || true

# 4. Install hex-ai package
# Check if running from inside local git repository root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd || pwd)"
if [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -d "$SCRIPT_DIR/src/harness_core" ]; then
    info "Installing HEX from local repository checkout: $SCRIPT_DIR..."
    LATEST_WHL="$(ls -t "$SCRIPT_DIR/dist/"hex_ai-*.whl 2>/dev/null | head -n 1 || true)"
    if [ -n "$LATEST_WHL" ] && [ -f "$LATEST_WHL" ]; then
        "$VENV_DIR/bin/python" -m pip install --quiet "$LATEST_WHL"
    else
        "$VENV_DIR/bin/python" -m pip install --retries 1 --timeout 5 --quiet "$SCRIPT_DIR[tui]" 2>/dev/null || \
        "$VENV_DIR/bin/python" -m pip install --retries 1 --timeout 5 --quiet "$SCRIPT_DIR"
    fi
    
    # Copy vendor directory into ~/.hex/vendor if present
    if [ -d "$SCRIPT_DIR/vendor" ]; then
        info "Syncing local vendor engines into $HEX_DIR/vendor..."
        mkdir -p "$HEX_DIR/vendor"
        cp -R "$SCRIPT_DIR/vendor/"* "$HEX_DIR/vendor/" 2>/dev/null || true
    fi
else
    info "Installing HEX from $PACKAGE_SPEC..."
    "$VENV_DIR/bin/python" -m pip install --quiet "$PACKAGE_SPEC" || {
        warn "Could not install via git package. Checking for wheel distribution..."
        "$VENV_DIR/bin/python" -m pip install "$PACKAGE_SPEC"
    }
fi

if [ ! -f "$VENV_DIR/bin/hex" ]; then
    error "Installation failed: executable $VENV_DIR/bin/hex not found."
    exit 1
fi
success "Package hex-ai installed successfully."

# 5. Create symlinks in user bin directory (~/.local/bin)
mkdir -p "$BIN_DIR"
ln -sf "$VENV_DIR/bin/hex" "$BIN_DIR/hex"
ln -sf "$VENV_DIR/bin/hex" "$BIN_DIR/harness"
success "Linked executable: $BIN_DIR/hex -> $VENV_DIR/bin/hex"

# 6. Verify PATH contains BIN_DIR
PATH_NEEDS_UPDATE=0
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) PATH_NEEDS_UPDATE=1 ;;
esac

# 7. Initialize default configuration if uninitialized
if [ ! -f "$HEX_DIR/config.yaml" ]; then
    info "Running initial onboarding to generate hardware configuration..."
    "$BIN_DIR/hex" onboard --defaults --config-dir "$HEX_DIR" >/dev/null 2>&1 || true
    success "Configuration initialized at $HEX_DIR/config.yaml"
fi

echo ""
echo -e "${BOLD}${GREEN}======================================================================${NC}"
echo -e "${BOLD}${GREEN}   HEX AI WORKBENCH INSTALLED SUCCESSFULLY!                           ${NC}"
echo -e "${BOLD}${GREEN}======================================================================${NC}"
echo ""

if [ "$PATH_NEEDS_UPDATE" -eq 1 ]; then
    warn "$BIN_DIR is not currently in your system PATH."
    echo "  ↳ Add it to your shell configuration by running:"
    echo ""
    if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = "zsh" ]; then
        echo -e "    ${CYAN}echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc${NC}"
    else
        echo -e "    ${CYAN}echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc && source ~/.bashrc${NC}"
    fi
    echo ""
fi

echo "Quick Start Commands:"
echo -e "  • Start Interactive TUI:     ${CYAN}hex chat${NC}   (or ${CYAN}hex tui${NC})"
echo -e "  • Run System Diagnostics:    ${CYAN}hex doctor${NC}"
echo -e "  • Search Past Tasks:         ${CYAN}hex search \"query\"${NC}"
echo -e "  • Inspect Antigravity Quota: ${CYAN}hex quota${NC}"
echo -e "  • Autonomous Task Execution: ${CYAN}hex run \"Add feature X\"${NC}"
echo ""
echo "Documentation & Updates: https://github.com/Stewy8506/Harness"
echo ""
