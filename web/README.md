# parallax-web

Localhost chat + gallery for the Parallax video pipeline. Opens a browser,
talks to Claude via the Anthropic SDK, lets the model read/inspect your
project files, and dispatches renders through the `parallax` CLI.

## Install

```bash
cd "path/to/parallax CLI/web"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Export your API key (stash in your shell rc for convenience):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

## Run

From any project directory you want to work in:

```bash
parallax chat
```

Or directly, if you'd rather skip the wrapper:

```bash
cd ~/my-video-project
PYTHONPATH="path/to/parallax CLI/web" python3 -m parallax_web
```

A browser tab opens at `http://127.0.0.1:<port>/`.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | **required** | Anthropic API credentials |
| `PARALLAX_WEB_MODEL` | `claude-sonnet-4-6` | Model id |
| `PARALLAX_WEB_PORT` | auto | Force a specific port |
| `PARALLAX_WEB_NO_BROWSER` | `0` | Set to `1` to skip auto-open |
| `PARALLAX_PROJECT_DIR` | cwd | Override the project root |
| `PARALLAX_WEB_VERBOSE` | `0` | Log every HTTP request |

## Telemetry

Every session, message, tool call, and dispatch event is recorded to
`~/.parallax/usage.db`. Inspect with:

```bash
sqlite3 ~/.parallax/usage.db ".schema"
sqlite3 ~/.parallax/usage.db "select * from sessions order by started_at desc limit 5;"
```
