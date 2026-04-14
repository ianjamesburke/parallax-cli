# Parallax CLI
# Run `just --list` to see available recipes.

# Default recipe — show the list
default:
    @just --list

# Launch parallax chat, loading .env from this directory if present
start:
    #!/usr/bin/env bash
    set -e
    if [ -f .env ]; then
        echo "loading .env"
        set -a
        source .env
        set +a
    fi
    parallax chat

# Install everything: CLI symlink + web venv
install: install-cli install-web

# Install the parallax CLI by symlinking bin/parallax into ~/.local/bin
install-cli:
    mkdir -p "$HOME/.local/bin"
    ln -sfn "$(pwd)/bin/parallax" "$HOME/.local/bin/parallax"
    @echo "linked $HOME/.local/bin/parallax -> $(pwd)/bin/parallax"
    @echo "ensure $HOME/.local/bin is on your PATH"

# Set up the web server venv and install its dependencies
install-web:
    python3 -m venv web/.venv
    web/.venv/bin/pip install --quiet --upgrade pip
    web/.venv/bin/pip install -r web/requirements.txt
    @echo "web venv ready at web/.venv"

# Run the CLI smoke tests
test-cli:
    TEST_MODE=true python3 test/test_cli.py

# Run the full test suite
test:
    TEST_MODE=true python3 test/test_manifest_first.py
    TEST_MODE=true python3 test/test_manifest_validator.py
    TEST_MODE=true python3 test/test_cli.py
