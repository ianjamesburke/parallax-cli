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

# ---- uv ----------------------------------------------------------------------
step "Checking uv"
if ! command -v uv &>/dev/null; then
    echo "  installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
ok "uv $(uv --version)"

# ---- just -------------------------------------------------------------------
step "Checking just"
if ! command -v just &>/dev/null; then
    echo "  installing just via brew..."
    if ! command -v brew &>/dev/null; then
        echo "  Homebrew not found — installing..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        if [ -f /opt/homebrew/bin/brew ]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        fi
    fi
    brew install just
fi
ok "just $(just --version)"

# ---- ffmpeg -----------------------------------------------------------------
step "Checking ffmpeg"
if ! command -v ffmpeg &>/dev/null; then
    echo "  installing ffmpeg via brew..."
    if ! command -v brew &>/dev/null; then
        echo "  Homebrew not found — installing..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        if [ -f /opt/homebrew/bin/brew ]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        fi
    fi
    brew install ffmpeg
fi
ok "ffmpeg $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}')"

# ---- Clone or update repo ---------------------------------------------------
step "Setting up repository"
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
step "Installing (uv sync + CLI wrapper)"
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
echo " Then: cd <your-project> && parallax status"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
