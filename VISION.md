# Parallax

Parallax is the definitive CLI for AI-assisted video production. The goal is absolute: handle every stage of making a video — from raw footage to published content — through a single manifest and a clean command namespace. Not a GUI tool, not a cloud platform, not a wrapper around someone else's editor. A first-class CLI that a human or an agent can drive from brief to publish with no lock-in to any single AI provider.

**Parallax is also a skill.** The CLI is designed to be fully agent-operable — structured output, machine-readable help text, `--json` NDJSON on every command, predictable exit codes, and `TEST_MODE` for offline pipeline verification. `SKILL.md` at the repo root is the agent's operating manual and stays synchronized with the current command surface. The external skill at `~/.agents/skills/parallax/` wraps that contract for Claude Code invocation.

## The Horizon

Every stage of video production — ingest, rough cut, fine cut, scripting, voiceover, music, stills, animation, review, and publish — should be expressible as a `parallax` command. Frame-level comments, general revision notes, YouTube account management, metadata, scheduling: all of it. The tool should eventually be capable of receiving a brief and producing a finished, published video with minimal human intervention — or with deep human involvement at every stage, because each stage is independently approvable and inspectable.

That's the horizon. Everything below is how we get there.

## Architecture Principles

**Manifest-first.** Every render is driven by `manifest.yaml`. No globbing, no implicit state. The manifest is the project. It can be read by humans, edited by agents, and committed to git.

**Each stage is independently approvable.** Ingest, stills, scripting, voiceover, composition, and publish are discrete pipeline stages. A human or agent can inspect and approve each output before the next stage runs. This keeps AI costs predictable and editorial control intact.

**CLI is the primary interface.** The web UI is a wrapper around the CLI, not the other way around. Commands produce structured output and clean exit codes so agents can drive the tool the same way humans do.

**BYOK or managed billing.** Users bring their own API keys or authenticate with a Parallax account for managed billing. The tool is explicit about which providers are being called and what they cost — `--estimate` flags give cost and time previews before any API call commits.

## Command Namespace

```
parallax ingest        — footage intake: transcription, optional visual analysis
parallax generate      — explicit generative calls (stills, video, voice)
parallax script        — writing agents for script and VO copy
parallax edit          — natural language manifest editing via editor agent
parallax compose       — deterministic manifest → final video render (pure ffmpeg)
parallax trim          — clip trimming primitive
parallax manifest      — manifest CRUD
parallax status        — project health and pre-flight check
parallax web           — local web UI
parallax project       — project scaffolding
parallax setup         — dep check, API key config, ingest preferences
parallax publish       — YouTube and social platform publishing (V3)
parallax animate       — SVG/HTML character generation and animation (V3)
```

Each command is a stable contract. Subcommand help text is machine-readable. Nothing is implicit.

## V2 Focus

V2 is the foundation build. V1 shipped a capable but rough pipeline — AI stills, Ken Burns renders, ElevenLabs voiceover, manifest-driven composition — with shortcuts taken to get it out the door.

V2 replaces the V1 internals with a clean namespace architecture that can carry everything above. The specific deliverables: `ingest` with transcript-by-default and `--estimate` preview, explicit `generate still/video/voice` commands, `script write/rewrite`, `edit` for natural language manifest changes, `compose` as a pure ffmpeg render with no AI calls, and `status` for pre-flight health checks. The web UI continues to exist but stays subordinate to the CLI.

V2 does not touch publishing or animation. Those namespaces are reserved and stubbed. The goal is a clean, well-seamed foundation — not a complete product.

## V3 and Beyond

V3 ships `publish` fully: YouTube upload, metadata, scheduling, account management. It also opens the `animate` namespace: SVG and HTML-based character generation, rigged animations, expressive motion — a genuinely new primitive for video production that doesn't exist anywhere else at the CLI level.

After that, the horizon is open. The architecture should make each new capability feel like adding a command, not rebuilding the tool.
