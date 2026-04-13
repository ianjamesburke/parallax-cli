#!/usr/bin/env bash
#
# bootstrap-ecosystem.sh — single-command install for the Parallax ecosystem
# (Plexi desktop app + Parallax CLI + Parallax Plexi app) on a fresh Mac.
#
# Safe to re-run: every stage checks state before mutating anything.
# Never echoes secrets to stdout. Never force-removes outside narrow scopes.

set -euo pipefail

# ---------- helpers ----------

log()  { printf '[bootstrap] %s\n' "$*"; }
warn() { printf '[bootstrap] WARN: %s\n' "$*" >&2; }
die()  { printf '[bootstrap] ERROR: %s\n' "$*" >&2; exit 1; }

confirm() {
    # confirm "prompt text" -> returns 0 on yes, 1 on no. Default no.
    local reply
    read -r -p "$1 [y/N] " reply || reply=""
    [[ "$reply" =~ ^[Yy]$ ]]
}

# Guarded append: append a line to a file only if it isn't already there.
guarded_append() {
    local line="$1" file="$2"
    [[ -f "$file" ]] || : > "$file"
    if ! grep -Fqx "$line" "$file"; then
        printf '%s\n' "$line" >> "$file"
        log "appended to $file: $line"
    fi
}

# Upsert an `export KEY="value"` line in a file. macOS BSD sed needs `-i ''`.
upsert_env_var() {
    local key="$1" value="$2" file="$3"
    [[ -f "$file" ]] || : > "$file"
    local new_line
    new_line="export ${key}=\"${value}\""
    if grep -Eq "^[[:space:]]*export[[:space:]]+${key}=" "$file"; then
        # Replace existing line in-place. Use a sed delimiter unlikely to clash.
        # We use a Python one-liner instead of sed because secret values can
        # contain arbitrary characters that would break sed escaping.
        python3 - "$key" "$value" "$file" <<'PY'
import sys, re, pathlib
key, value, path = sys.argv[1], sys.argv[2], sys.argv[3]
p = pathlib.Path(path)
lines = p.read_text().splitlines()
pat = re.compile(rf'^\s*export\s+{re.escape(key)}=')
out = []
replaced = False
for ln in lines:
    if pat.match(ln) and not replaced:
        out.append(f'export {key}="{value}"')
        replaced = True
    else:
        out.append(ln)
p.write_text("\n".join(out) + "\n")
PY
        log "updated ${key} in ${file}"
    else
        printf '%s\n' "$new_line" >> "$file"
        log "wrote ${key} to ${file}"
    fi
}

brew_install_if_missing() {
    local pkg="$1"
    if brew list --formula "$pkg" >/dev/null 2>&1 || brew list --cask "$pkg" >/dev/null 2>&1; then
        log "brew: $pkg already installed"
        return 0
    fi
    log "brew install $pkg"
    if ! brew install "$pkg"; then
        die "brew install $pkg failed. Run 'brew doctor' to diagnose, then re-run this script."
    fi
}

# ---------- constants ----------

readonly GITHUB_DIR="$HOME/Documents/GitHub"
readonly PLEXI_DIR="$GITHUB_DIR/PLEXI"
readonly PARALLAX_CLI_DIR="$GITHUB_DIR/parallax CLI"
readonly PARALLAX_APP_DIR="$GITHUB_DIR/parallax-app"

readonly PLEXI_REMOTE="https://github.com/ianjamesburke/PLEXI.git"
readonly PARALLAX_CLI_REMOTE="https://github.com/ianjamesburke/parallax.git"
readonly PARALLAX_APP_REMOTE="https://github.com/ianjamesburke/parallax-app.git"

readonly PLEXI_APPS_DIR="$HOME/.plexi/apps"
readonly PLEXI_PARALLAX_APP_DEST="$PLEXI_APPS_DIR/parallax"

readonly ZSHENV="$HOME/.zshenv"

# ---------- Stage 1: preflight + prerequisites ----------

stage_preflight() {
    log "Stage 1: preflight + prerequisites"

    if [[ "$(uname)" != "Darwin" ]]; then
        die "This bootstrap is macOS-only. Detected: $(uname)"
    fi

    # Xcode Command Line Tools
    if ! xcode-select -p >/dev/null 2>&1; then
        log "Xcode Command Line Tools not found. Launching installer..."
        xcode-select --install || true
        die "Xcode CLT install window launched. Complete the install, then re-run this script."
    fi
    log "xcode CLT: $(xcode-select -p)"

    # Homebrew
    if ! command -v brew >/dev/null 2>&1; then
        log "Homebrew not found. Installing..."
        if ! /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"; then
            die "Homebrew install failed. See https://brew.sh and re-run."
        fi
        # Put brew on PATH for this shell (Apple Silicon + Intel)
        if [[ -x /opt/homebrew/bin/brew ]]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [[ -x /usr/local/bin/brew ]]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
    fi
    command -v brew >/dev/null 2>&1 || die "brew missing after install attempt."
    log "brew: $(brew --version | head -n1)"

    # Core packages
    brew_install_if_missing git
    brew_install_if_missing just
    brew_install_if_missing python@3.11
    brew_install_if_missing ffmpeg
    # ffmpeg-full lives in a tap
    if ! brew list --formula ffmpeg-full >/dev/null 2>&1; then
        log "tapping homebrew-ffmpeg/ffmpeg for ffmpeg-full"
        brew tap homebrew-ffmpeg/ffmpeg || warn "tap homebrew-ffmpeg/ffmpeg failed (may already exist)"
        brew_install_if_missing homebrew-ffmpeg/ffmpeg/ffmpeg-full
    else
        log "brew: ffmpeg-full already installed"
    fi

    # Rust via rustup
    if ! command -v rustup >/dev/null 2>&1; then
        brew_install_if_missing rustup-init
        # rustup-init ships as a separate binary; run it non-interactively.
        if command -v rustup-init >/dev/null 2>&1; then
            log "running rustup-init -y"
            rustup-init -y --default-toolchain stable --no-modify-path || die "rustup-init failed"
            export PATH="$HOME/.cargo/bin:$PATH"
        fi
    fi
    if command -v rustup >/dev/null 2>&1; then
        rustup default stable >/dev/null 2>&1 || warn "rustup default stable reported an error (already set?)"
        log "rust: $(rustc --version 2>/dev/null || echo 'not yet on PATH — open a new shell')"
    else
        warn "rustup not on PATH after install. You may need to open a new shell before running Stage 3."
    fi

    log "prereqs OK"
}

# ---------- Stage 2: clone / update repos ----------

clone_or_update() {
    local dir="$1" remote="$2" label="$3"
    if [[ -d "$dir/.git" ]]; then
        log "$label: updating (git pull --ff-only)"
        if ! git -C "$dir" pull --ff-only; then
            warn "$label: fast-forward pull failed. Resolve manually in $dir; continuing with existing checkout."
        fi
    elif [[ -d "$dir" ]]; then
        die "$label: $dir exists but is not a git repo. Move it aside and re-run."
    else
        log "$label: cloning $remote -> $dir"
        mkdir -p "$(dirname "$dir")"
        if ! git clone "$remote" "$dir"; then
            die "$label: git clone failed. Check network / credentials and re-run."
        fi
    fi
}

stage_repos() {
    log "Stage 2: clone or update repos"
    mkdir -p "$GITHUB_DIR"
    clone_or_update "$PLEXI_DIR"         "$PLEXI_REMOTE"         "PLEXI"
    clone_or_update "$PARALLAX_CLI_DIR"  "$PARALLAX_CLI_REMOTE"  "parallax CLI"
    clone_or_update "$PARALLAX_APP_DIR"  "$PARALLAX_APP_REMOTE"  "parallax-app"
    log "repos ready"
}

# ---------- Stage 3: build + install Plexi ----------

stage_plexi() {
    log "Stage 3: build + install Plexi"
    pushd "$PLEXI_DIR" >/dev/null

    # Canonical path per PLEXI/README.md "Build from source" is install.sh,
    # which runs `cargo bundle --release` and copies to /Applications.
    if [[ -x ./install.sh ]]; then
        log "running PLEXI/install.sh"
        ./install.sh
    elif command -v just >/dev/null 2>&1 && grep -qE '^install:' justfile 2>/dev/null; then
        log "running just install (fallback)"
        just install
    else
        popd >/dev/null
        die "PLEXI: no install.sh or just install recipe found."
    fi

    popd >/dev/null

    if [[ -d "/Applications/Plexi.app" ]]; then
        log "verified /Applications/Plexi.app"
    else
        warn "Plexi.app not found in /Applications after install. Check logs above."
    fi

    log "plexi installed"
}

# ---------- Stage 4: install Parallax CLI ----------

stage_parallax_cli() {
    log "Stage 4: install Parallax CLI"
    pushd "$PARALLAX_CLI_DIR" >/dev/null

    if ! make install-cli; then
        popd >/dev/null
        die "make install-cli failed in $PARALLAX_CLI_DIR"
    fi

    popd >/dev/null

    local bin="$HOME/.local/bin/parallax"
    if [[ ! -e "$bin" ]]; then
        die "expected $bin after make install-cli, but it's missing."
    fi

    # Ensure ~/.local/bin is on PATH via ~/.zshenv (idempotent).
    local path_line='export PATH="$HOME/.local/bin:$PATH"'
    guarded_append "$path_line" "$ZSHENV"
    # Make it effective for the remainder of this script.
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) export PATH="$HOME/.local/bin:$PATH" ;;
    esac

    log "parallax CLI installed"
}

# ---------- Stage 5: install Parallax Plexi app ----------

stage_parallax_plexi_app() {
    log "Stage 5: install Parallax Plexi app"
    mkdir -p "$PLEXI_PARALLAX_APP_DEST"

    # rsync copies everything except .git, and --delete keeps the destination
    # a mirror of the source (safe because the destination is a dedicated
    # install directory under ~/.plexi/apps/parallax — nothing else lives there).
    if ! rsync -a --delete --exclude='.git' \
        "$PARALLAX_APP_DIR"/ "$PLEXI_PARALLAX_APP_DEST"/; then
        die "rsync of parallax-app to $PLEXI_PARALLAX_APP_DEST failed."
    fi

    if [[ ! -f "$PLEXI_PARALLAX_APP_DEST/manifest.toml" ]]; then
        die "expected $PLEXI_PARALLAX_APP_DEST/manifest.toml, not found."
    fi

    # PLEXI gotcha from CLAUDE.md: install-alpha doesn't chmod entry points.
    # We apply the same guard here — ensure the Python entry point is executable.
    chmod +x "$PLEXI_PARALLAX_APP_DEST"/*.py 2>/dev/null || true

    log "parallax plexi app installed"
}

# ---------- Stage 6: provision secrets ----------

prompt_secret() {
    # prompt_secret VAR_NAME -> echoes value on stdout (captured by caller).
    local name="$1" value=""
    # -s keeps the value off the terminal.
    read -r -s -p "$name: " value
    printf '\n' >&2
    printf '%s' "$value"
}

provision_key() {
    local name="$1"
    local existing=""
    if [[ -f "$ZSHENV" ]]; then
        existing="$(grep -E "^[[:space:]]*export[[:space:]]+${name}=" "$ZSHENV" || true)"
    fi

    if [[ -n "$existing" ]]; then
        if ! confirm "$name already set in $ZSHENV. Overwrite?"; then
            log "$name: keeping existing value"
            return 0
        fi
    fi

    local value
    value="$(prompt_secret "$name")"
    if [[ -z "$value" ]]; then
        warn "$name: empty input — skipping"
        return 0
    fi

    upsert_env_var "$name" "$value" "$ZSHENV"
    # Do NOT print the value. upsert_env_var logs only the key name.
}

stage_secrets() {
    log "Stage 6: provision API keys"
    log "(values are read silently — nothing will be echoed)"
    provision_key ANTHROPIC_API_KEY
    provision_key AI_VIDEO_GEMINI_KEY
    provision_key AI_VIDEO_ELEVENLABS_KEY

    # Re-source so Stage 7 sees the new values.
    # shellcheck disable=SC1090
    set +u
    source "$ZSHENV" || warn "failed to source $ZSHENV"
    set -u

    log "secrets provisioned"
}

# ---------- Stage 7: smoke test ----------

stage_smoke_test() {
    log "Stage 7: smoke test (parallax setup)"
    if parallax setup; then
        log "smoke test passed"
        return 0
    fi

    warn "parallax setup reported failures. Review the output above."
    if confirm "Continue anyway?"; then
        warn "continuing with failing smoke test"
        return 0
    fi
    die "Aborted at smoke test."
}

# ---------- Stage 8: done ----------

stage_done() {
    cat <<'DONE'

==================================================
Parallax ecosystem ready.
==================================================

Next steps:
  1. Open Plexi from /Applications (first launch: right-click -> Open,
     or run `xattr -cr /Applications/Plexi.app` if macOS blocks it).
  2. In a terminal, cd into a directory that has some source video.
  3. In Plexi's companion terminal pane, run:

       parallax run --yes "<your brief here>"

     Finals land in ./output/.

If you opened this shell before the bootstrap ran, open a new terminal
so ~/.zshenv (PATH + API keys) is picked up.
DONE
}

# ---------- main ----------

main() {
    stage_preflight
    stage_repos
    stage_plexi
    stage_parallax_cli
    stage_parallax_plexi_app
    stage_secrets
    stage_smoke_test
    stage_done
}

main "$@"
