# Parallax CLI
# Run `just --list` to see available recipes.

# Default recipe — show the list
default:
    @just --list

# Launch parallax chat with LAN exposure. Optional env_file path (default: ./.env).
# Usage: just start              # loads ./.env
#        just start ~/shared/.env   # loads a custom path
start env_file=".env":
    #!/usr/bin/env bash
    set -e
    if [ -f "{{env_file}}" ]; then
        echo "loading {{env_file}}"
        set -a
        source "{{env_file}}"
        set +a
    else
        echo "no env file at {{env_file}} — continuing with current environment"
    fi
    export PARALLAX_WEB_HOST=0.0.0.0
    echo "binding to 0.0.0.0 (LAN-exposed)"
    parallax chat

# Install everything: sync deps + install CLI wrapper
install: sync install-cli

# Sync all dependencies via uv (creates/updates .venv)
sync:
    #!/usr/bin/env bash
    set -e
    if ! command -v uv &>/dev/null; then
        echo "uv not found — installing..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
    fi
    uv sync
    echo "deps synced into .venv ($(uv run python --version))"

# Install the parallax CLI wrapper into ~/.local/bin
# Removes any old symlink first, then writes a real bash wrapper so .venv python is always used.
install-cli:
    #!/usr/bin/env bash
    set -e
    REPO="$(pwd)"
    DEST="$HOME/.local/bin/parallax"
    mkdir -p "$HOME/.local/bin"
    # Remove old symlink or file — critical if a symlink exists, writing through it overwrites the target
    rm -f "$DEST"
    printf '#!/usr/bin/env bash\nexec "%s/.venv/bin/python" "%s/bin/parallax" "$@"\n' "$REPO" "$REPO" > "$DEST"
    chmod +x "$DEST"
    echo "installed $DEST → $REPO/bin/parallax (via .venv)"
    echo "ensure $HOME/.local/bin is on your PATH"

# Run the CLI smoke tests
test-cli:
    TEST_MODE=true uv run python test/test_cli.py

# Run the full test suite
test:
    TEST_MODE=true uv run python test/test_manifest_first.py
    TEST_MODE=true uv run python test/test_manifest_validator.py
    TEST_MODE=true uv run python test/test_cli.py
