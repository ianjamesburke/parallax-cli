.PHONY: install-cli test test-cli

BIN_SRC := $(abspath bin/parallax)
BIN_DST := $(HOME)/.local/bin/parallax

install-cli:
	@mkdir -p $(HOME)/.local/bin
	@ln -sfn $(BIN_SRC) $(BIN_DST)
	@echo "linked $(BIN_DST) -> $(BIN_SRC)"
	@echo "ensure $(HOME)/.local/bin is on your PATH"

test-cli:
	TEST_MODE=true python3.11 test/test_cli.py

test:
	TEST_MODE=true python3.11 test/test_manifest_first.py
	TEST_MODE=true python3.11 test/test_manifest_validator.py
	TEST_MODE=true python3.11 test/test_cli.py
