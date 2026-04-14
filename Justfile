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

# Install everything: CLI symlink + web venv
install: install-cli install-web

# Install the parallax CLI by symlinking bin/parallax into ~/.local/bin
install-cli:
    mkdir -p "$HOME/.local/bin"
    ln -sfn "$(pwd)/bin/parallax" "$HOME/.local/bin/parallax"
    @echo "linked $HOME/.local/bin/parallax -> $(pwd)/bin/parallax"
    @echo "ensure $HOME/.local/bin is on your PATH"

# Set up the web server venv and install its dependencies (requires Python 3.11+)
install-web:
    #!/usr/bin/env bash
    set -e
    PYTHON=""
    for candidate in python3.12 python3.11; do
        if command -v "$candidate" &>/dev/null; then
            PYTHON=$(command -v "$candidate")
            break
        fi
    done
    if [ -z "$PYTHON" ]; then
        if command -v python3 &>/dev/null; then
            PYTHON=$(command -v python3)
        fi
    fi
    if [ -z "$PYTHON" ]; then
        echo "error: Python 3.11+ not found — install with: brew install python@3.11"
        exit 1
    fi
    PYVER=$("$PYTHON" -c "import sys; print(sys.version_info >= (3,11))")
    if [ "$PYVER" != "True" ]; then
        echo "error: Python 3.11+ required, found $("$PYTHON" --version)"
        echo "install with: brew install python@3.11"
        exit 1
    fi
    "$PYTHON" -m venv web/.venv
    # Use the venv's python to run pip — avoids missing pip-binary issues
    web/.venv/bin/python3 -m pip install --quiet --upgrade pip
    web/.venv/bin/python3 -m pip install -r web/requirements.txt
    echo "web venv ready at web/.venv ($(web/.venv/bin/python3 --version))"

# Run the CLI smoke tests
test-cli:
    TEST_MODE=true python3 test/test_cli.py

# Run the full test suite
test:
    TEST_MODE=true python3 test/test_manifest_first.py
    TEST_MODE=true python3 test/test_manifest_validator.py
    TEST_MODE=true python3 test/test_cli.py
