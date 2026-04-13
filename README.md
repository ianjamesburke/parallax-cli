# Parallax CLI

AI video agent pipeline. Analyzes footage, makes edit decisions, and renders output using an LLM agent network. Includes a web interface (`parallax chat`) for directing the pipeline through a browser.

## Requirements

- Python 3.11+
- `ffmpeg` — `brew install ffmpeg`
- [`just`](https://github.com/casey/just) — `brew install just`

## Install

```sh
git clone https://github.com/ianjamesburke/parallax-cli
cd parallax-cli
just install
```

`just install` does two things:
1. Symlinks `bin/parallax` into `~/.local/bin/`
2. Creates `web/.venv` and installs all web server dependencies

Make sure `~/.local/bin` is on your `PATH`:

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
```

## API Keys

Add these to your shell rc (`~/.zshrc` or `~/.zsh_secrets`):

```sh
export ANTHROPIC_API_KEY="sk-ant-..."      # required — chat agent
export AI_VIDEO_GEMINI_KEY="AIza..."       # required — image generation
export ELEVENLABS_API_KEY="sk_..."         # required — voiceover
```

Get keys: [Anthropic](https://console.anthropic.com/) · [Google AI Studio](https://aistudio.google.com/app/apikey) · [ElevenLabs](https://elevenlabs.io/)

## Launch the Web Interface

```sh
cd ~/my-project
parallax chat
```

Opens a browser at `http://127.0.0.1:<port>/`. Talk to the Head of Production agent to generate images, build a manifest, and render videos.

To share with others, set a password and expose via [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/):

```sh
PARALLAX_WEB_PASSWORD=yourpassword parallax chat
cloudflared tunnel run --url http://127.0.0.1:<port> parallax
```

## CLI Commands

```sh
parallax run "cut a 15s teaser from these clips"   # run pipeline in cwd
parallax chat                                       # launch web interface
parallax status                                    # show manifest stats for cwd
parallax project new my-shoot                      # scaffold a new project
parallax projects                                  # list known projects
```

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | **required** | Anthropic API credentials |
| `AI_VIDEO_GEMINI_KEY` | **required** | Gemini image generation |
| `ELEVENLABS_API_KEY` | **required** | ElevenLabs voiceover |
| `PARALLAX_WEB_PASSWORD` | — | Enables basic auth + per-user workspaces |
| `PARALLAX_WEB_MODEL` | `claude-sonnet-4-6` | Model override |
| `PARALLAX_WEB_PORT` | auto | Force a specific port |
| `PARALLAX_WEB_NO_BROWSER` | `0` | Set to `1` to skip auto-open |
| `PARALLAX_PROJECT_DIR` | cwd | Override the project root |

## Test Mode

```sh
TEST_MODE=true parallax run "test prompt"
```

Uses placeholders, makes zero external API calls.

## Tests

```sh
just test        # full suite
just test-cli    # CLI smoke tests only
```
