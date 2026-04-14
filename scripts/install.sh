#!/usr/bin/env bash
# install.sh — bootstrap Parallax CLI on macOS
# Safe to run from the repo root or pipe directly from GitHub:
#   curl -fsSL https://raw.githubusercontent.com/ianjamesburke/parallax-cli/main/scripts/install.sh | bash
set -e

REPO_URL="https://github.com/ianjamesburke/parallax-cli"
INSTALL_DIR="$HOME/parallax-cli"

step() { echo ""; echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ $*"; exit 1; }

# ---- Homebrew ---------------------------------------------------------------
step "Checking Homebrew"
if ! command -v brew &>/dev/null; then
    echo "  installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for the rest of this script (Apple Silicon path)
    if [ -f /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    fi
fi
ok "brew $(brew --version | head -1)"

# ---- Python 3.11+ -----------------------------------------------------------
step "Checking Python 3.11+"
PYTHON=""
for cmd in python3.12 python3.11; do
    if command -v "$cmd" &>/dev/null; then
        PYTHON=$(command -v "$cmd")
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "  installing python@3.11 via brew..."
    brew install python@3.11
    PYTHON=$(brew --prefix python@3.11)/bin/python3.11
fi
PY_VERSION=$("$PYTHON" -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}')")
ok "Python $PY_VERSION at $PYTHON"

# ---- just -------------------------------------------------------------------
step "Checking just"
if ! command -v just &>/dev/null; then
    echo "  installing just via brew..."
    brew install just
fi
ok "just $(just --version)"

# ---- ffmpeg -----------------------------------------------------------------
step "Checking ffmpeg"
if ! command -v ffmpeg &>/dev/null; then
    echo "  installing ffmpeg via brew..."
    brew install ffmpeg
fi
ok "ffmpeg $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"

# ---- Clone or update repo ---------------------------------------------------
step "Setting up repository"
# If we're already running from inside the repo, skip clone
REPO_ROOT=""
if git -C "$(pwd)" rev-parse --show-toplevel &>/dev/null; then
    TOP=$(git -C "$(pwd)" rev-parse --show-toplevel)
    if [ -f "$TOP/Justfile" ] && grep -q "parallax" "$TOP/Justfile" 2>/dev/null; then
        REPO_ROOT="$TOP"
        ok "already in repo at $REPO_ROOT"
    fi
fi
if [ -z "$REPO_ROOT" ]; then
    if [ -d "$INSTALL_DIR/.git" ]; then
        echo "  pulling latest..."
        git -C "$INSTALL_DIR" pull --ff-only
        REPO_ROOT="$INSTALL_DIR"
    else
        echo "  cloning $REPO_URL..."
        git clone "$REPO_URL" "$INSTALL_DIR"
        REPO_ROOT="$INSTALL_DIR"
    fi
    ok "repo at $REPO_ROOT"
fi

# ---- Install ----------------------------------------------------------------
step "Running just install"
cd "$REPO_ROOT"
just install

# ---- PATH check -------------------------------------------------------------
step "Checking PATH"
LOCAL_BIN="$HOME/.local/bin"
if [[ ":$PATH:" != *":$LOCAL_BIN:"* ]]; then
    echo "  adding $LOCAL_BIN to PATH in ~/.zshrc"
    echo '' >> ~/.zshrc
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
    echo "  run: source ~/.zshrc  (or open a new terminal)"
fi
ok "\$HOME/.local/bin is on PATH"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo " Parallax installed. Next: add your API keys."
echo ""
echo "  export ANTHROPIC_API_KEY=sk-ant-..."
echo "  export AI_VIDEO_GEMINI_KEY=AIza..."
echo "  export ELEVENLABS_API_KEY=sk_..."
echo ""
echo " Then: cd <your-project> && parallax chat"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
