# Parallax CLI — run `just --list` to see available recipes.

ROOT := justfile_directory()
BETA := ROOT / "worktrees/beta"

default:
    @just --list

# Install from main. Run after pulling changes to main.
install:
    @just _install "{{ROOT}}" "main"

# Install from worktrees/beta. Use during active development on the beta branch.
install-beta:
    @just _install "{{BETA}}" "beta"

# Shared install logic — not intended to be called directly.
_install repo label:
    #!/usr/bin/env bash
    set -e
    if ! command -v uv &>/dev/null; then
        echo "uv not found — installing..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        export PATH="$HOME/.local/bin:$PATH"
    fi
    cd "{{repo}}"
    uv sync
    DEST="$HOME/.local/bin/parallax"
    mkdir -p "$HOME/.local/bin"
    rm -f "$DEST"
    printf '#!/usr/bin/env bash\nexec "%s/.venv/bin/python" "%s/bin/parallax" "$@"\n' "{{repo}}" "{{repo}}" > "$DEST"
    chmod +x "$DEST"
    echo "installed: $DEST → {{repo}} ({{label}})"

# Launch parallax chat with LAN exposure. Optional env_file path (default: ./.env).
start env_file=".env":
    #!/usr/bin/env bash
    set -e
    if [ -f "{{env_file}}" ]; then
        set -a; source "{{env_file}}"; set +a
    fi
    PARALLAX_WEB_HOST=0.0.0.0 parallax chat

# Run tests (delegates to beta Justfile where the tests live).
test:
    just --justfile "{{BETA}}/Justfile" --working-directory "{{BETA}}" test

test-cli:
    just --justfile "{{BETA}}/Justfile" --working-directory "{{BETA}}" test-cli
