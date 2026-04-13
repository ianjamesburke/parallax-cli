# parallax-web

Localhost chat + gallery for the Parallax video pipeline. Opens a browser,
talks to Claude via the Anthropic SDK, lets the model read/inspect your
project files, and dispatches renders through the `parallax` CLI.

See the [root README](../README.md) for full install and setup instructions.

## Quick start

```sh
# From the repo root:
just install

# Then from any project directory:
cd ~/my-project
parallax chat
```

## Running directly (without the CLI wrapper)

```sh
cd ~/my-project
PYTHONPATH="path/to/parallax-cli/web" python3 -m parallax_web
```

## Event log

Sessions, messages, tool calls, and dispatch events are recorded to
`~/.parallax/events.jsonl`. Each line is a JSON object with a `kind` field.

```sh
tail -f ~/.parallax/events.jsonl | python3 -m json.tool
```
