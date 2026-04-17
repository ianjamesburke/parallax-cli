# Parallax CLI

**Parallax is an agent-operable video production CLI and web UI** for AI-assisted short-form video. A Head-of-Production agent talks to you in plain English, dispatches specialized tools (stills, voiceover, Ken Burns, captions, AI video), and drives a manifest-first pipeline that renders deterministic 9:16 / 16:9 / 1:1 output.

Version: **0.0.1** — early, honest, ships.

## What it can do today

- **Generate images** via Google Gemini Imagen (default) or fal.ai Flux (`parallax fal image low/medium/high` → Flux schnell / dev / pro).
- **Generate video from text or an image** via fal.ai — three tiers:
  - **low:** LTX-2.3 (~$0.02/clip) — text-to-video + image-to-video
  - **medium:** Wan-t2v (~$0.20/clip)
  - **high:** Kling 1.6 (~$0.056/sec) — supports start + end frame anchoring
- **Voice** via ElevenLabs. `parallax voice list` enumerates voices; `parallax voice clone --sample audio.wav` clones from a sample. Voice id is persisted into the manifest on every render.
- **Voiceover + word-timed captions** via ElevenLabs + WhisperX alignment.
- **Captions + headlines** rendered as transparent PNGs via PIL (no fragile ffmpeg `drawtext`). Three bundled styles: `outline_white_on_black`, `outline_black_on_white`, `block_background`. Inter SemiBold, Anton, DM Sans ship in `assets/fonts/` (all OFL).
- **Compose** from a `manifest.yaml`: scenes, captions, headline, VO, resolution — everything the agent configures via the manifest takes effect in the output.
- **Mixed-aspect assembly:** horizontal clips get pillarboxed into vertical output (or crop-fill with `fit: cover`). Vertical sources stay full-frame.
- **Upload your own audio** (mp3/wav/m4a/aac/ogg/flac) via the web UI — reference it as `voiceover.audio_file` in the manifest and compose muxes it in.
- **Async video thumbnails** in the web UI (cached per workspace).
- **Web chat UI** (`parallax web`) with inline image previews, video posters, mid-flight interrupt, and per-session chat + media bin.
- **Agent-operable** — every subcommand supports `--json` (NDJSON events) and respects `TEST_MODE` for offline runs.

## What it does NOT do yet

- No real-time preview scrubbing in the UI.
- No end-to-end test on the HoP agent's tool orchestration — the individual tools are verified, the agent's routing is not.
- No audio-native video (Veo 3 tier) — scoped as a future `high-audio` tier.
- No multi-user collaborative editing.

## Install

Requires Python 3.11+, [uv](https://astral.sh/uv), and `ffmpeg`.

```sh
# from the repo root
uv tool install --editable .
```

Then:

```sh
parallax --help
```

## API keys

Put these in `~/.zsh_secrets` (or equivalent):

```sh
export ANTHROPIC_API_KEY="sk-ant-..."   # HoP agent
export AI_VIDEO_GEMINI_KEY="AIza..."    # Gemini Imagen (image gen default)
export FAL_KEY="..."                    # fal.ai (video gen + alt image tiers)
export ELEVENLABS_API_KEY="sk_..."      # voiceover + voice cloning
```

Anything you don't set disables the corresponding tool — the rest still works.

## Config

Optional `.parallax/config.toml` at your project root overrides tier → model mappings:

```toml
[fal.video.t2v]
low = "fal-ai/ltx-2.3/text-to-video"
medium = "fal-ai/wan-t2v"
high = "fal-ai/kling-video/v1.6/standard/text-to-video"

[fal.video.i2v]
low = "fal-ai/ltx-2.3/image-to-video"
medium = "fal-ai/wan-i2v"
high = "fal-ai/kling-video/v1.6/standard/image-to-video"

[fal.image]
low = "fal-ai/flux/schnell"
medium = "fal-ai/flux/dev"
high = "fal-ai/flux-pro/v1.1"
```

Precedence: `--model` flag > `PARALLAX_FAL_*` env > `config.toml` > built-in defaults. `parallax config show` prints the effective config with source attribution.

## Quickstart

```sh
# 1. Generate a vertical image (Flux schnell, ~$0.003)
parallax fal image low --prompt "neon sign reading 'shipping'" --aspect 9:16 --output hero.jpg

# 2. Animate it into a 6s clip (LTX-2.3 image-to-video, ~$0.02)
parallax fal video low --image hero.jpg --prompt "the sign flickers on" --aspect 9:16 --output clip.mp4

# 3. Or drive the whole thing from a browser
parallax web
```

## CLI surface

```
parallax web                                         # launch the Head-of-Production web UI
parallax fal image <low|medium|high>                 # generate image (Flux)
parallax fal video <low|medium|high> [--image PATH]  # generate video (LTX/Wan/Kling)
parallax fal models                                  # show tier → model map + source
parallax voice list                                  # list ElevenLabs voices
parallax voice clone --name X --sample audio.wav     # clone a voice
parallax voiceover --text "..." --voice <id>         # generate VO + word timings
parallax ingest                                      # index video files in input/
parallax compose                                     # render from manifest.yaml
parallax config show                                 # effective config dump
```

All subcommands accept `--json` for NDJSON output. Run with `TEST_MODE=1` for offline pipeline verification.

## Manifest

Every render is driven by a `manifest.yaml`. The invariant: `parallax compose` always reproduces the output from the manifest alone. Example:

```yaml
config:
  resolution: 1080x1920
scenes:
  - type: video
    source: input/clip.mp4
    start_s: 30
    end_s: 45
    fit: cover      # or contain (default)
voiceover:
  audio_file: audio/voiceover.mp3
  voice_id: SAz9YHcvj6GT2YYXdXww
captions:
  enabled: true
  style: outline_white_on_black
headline:
  text: WEEK IN REVIEW
  style: block_background
```

Unknown fields are rejected loudly. Common aliases (`trim_start` → `start_s`) are normalized with a warning.

## Test mode

```sh
TEST_MODE=true parallax compose
```

Mocks LLM + image/video/voiceover calls. No API spend, no keys required.

## License

See LICENSE.
