# Parallax CLI
# Run `just --list` to see available recipes.

# Default recipe — show the list
default:
    @just --list

# Install the parallax CLI by symlinking bin/parallax into ~/.local/bin
install-cli:
    mkdir -p "$HOME/.local/bin"
    ln -sfn "$(pwd)/bin/parallax" "$HOME/.local/bin/parallax"
    @echo "linked $HOME/.local/bin/parallax -> $(pwd)/bin/parallax"
    @echo "ensure $HOME/.local/bin is on your PATH"

# Run the CLI smoke tests
test-cli:
    TEST_MODE=true python3 test/test_cli.py

# Run the full test suite
test:
    TEST_MODE=true python3 test/test_manifest_first.py
    TEST_MODE=true python3 test/test_manifest_validator.py
    TEST_MODE=true python3 test/test_cli.py
