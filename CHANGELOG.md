# Changelog

All notable changes to kijito-inbox-monitor are documented in this file.
The format is based on Keep a Changelog, and this project follows Semantic Versioning.

## [0.1.0] - 2026-06-24

First public release.

### Added
- Single, zero-dependency Python stdlib watcher for the Kijito inbox. It polls the inbox
  and emits one event per new message, either as NDJSON on stdout or by running a command
  per event, to keep a running agent's inbox live between tool calls.
- Multi-persona mode: one process watches the whole local hive via `/api/personas`, with one
  `/api/notify/pending` fetch per tick fanned out locally, per-persona cursors, and periodic
  rediscovery of new personas.
- Per-persona owned, self-rotating event logs via `--events-file-template`, so each session
  tails only its own `events.<persona>.ndjson`.
- Liveness alert state machine (`alert` after N consecutive failures, `recovered`, optional
  `heartbeat`) for use as a dead-man's switch.
- SSRF-guarded `--url` override, peek-only inbox reads, monotonic-id cursor dedup, and
  single-writer state files that resume cleanly under a supervisor.
- Console command `kijito-inbox-monitor`, installable with pipx, uv, or pip.
- An npm package that acts as a signpost to the PyPI tool (it delegates to `uvx`/`pipx`, or
  prints install guidance), so the name is reserved on npm without a fragile Node installer.

[0.1.0]: https://github.com/KijitoAI/kijito-inbox-monitor/releases/tag/v0.1.0
