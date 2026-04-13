# Parallax CLI

AI video agent pipeline. Analyzes footage, makes edit decisions, and renders output using an LLM agent network.

## Install

```sh
git clone https://github.com/ianjamesburke/parallax-cli
cd parallax-cli
make install-cli
```

This symlinks `bin/parallax` into `~/.local/bin/`. Make sure `~/.local/bin` is on your `PATH`:

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
```

Parallax is self-contained. The video-production scripts it needs ship inside `packs/video/scripts/` — no external skill install required.

## Requirements

- Python 3.11+
- `ffmpeg` on PATH
- `ANTHROPIC_API_KEY` or run inside a Claude Code session
- `AI_VIDEO_GEMINI_KEY` — for image generation ([get one](https://aistudio.google.com/app/apikey))

```sh
export ANTHROPIC_API_KEY="sk-ant-..."
export AI_VIDEO_GEMINI_KEY="..."
```

## Commands

```sh
parallax run "cut a 15s teaser from these clips"   # run pipeline in cwd
parallax status                                    # show manifest stats for cwd
parallax project new my-shoot                      # scaffold a new project
parallax projects                                  # list known projects
```

`parallax run` discovers video files in `./`, `./assets/`, or `./input/` and writes manifest + outputs under `cwd/.parallax/<concept_id>/`.

## Plexi Viewer

Install the [Parallax viewer app](https://github.com/ianjamesburke/parallax-app) from the Plexi App Store to watch your pipeline run live.

## Test Mode

```sh
TEST_MODE=true parallax run "test prompt"
```

Uses placeholders, makes zero external API calls.
