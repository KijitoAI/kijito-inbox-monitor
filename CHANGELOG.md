# Changelog

All notable changes to kijito-inbox-monitor are documented in this file.
The format is based on Keep a Changelog, and this project follows Semantic Versioning.

## [0.3.0] - 2026-06-29

Near-instant wake via long-polling, with full self-heal.

### Added
- **Long-poll wake** (`--wait`, default 50s): the watcher holds a `/api/notify/pending?wait=&cursor=`
  request that the server releases the instant new mail arrives, cutting wake latency from up to
  `--poll-seconds` to near-instant **without raising the request rate** (one held connection per
  account). Forward/backward compatible: against a server that doesn't support long-poll it
  transparently falls back to interval polling and auto-upgrades once the server returns a cursor -
  no redeploy. `--wait 0` disables it.
- **Instant new-persona pickup**: a newly created persona that receives mail is added as a watch
  target within one tick (from the notify counts already fetched), instead of waiting for the
  periodic `/api/personas` rescan.

### Reliability
- **Self-heal on connection loss** (wifi/NAT/Cloudflare/server-restart): a dropped or half-open hold
  is detected by a client timeout above the server hold, then reconnected with exponential backoff,
  resuming from the last opaque cursor so no wake is missed across the gap (lossless). The periodic
  full per-persona inbox poll remains the by-message-id correctness backstop.

## [0.2.0] - 2026-06-29

Remote-only release. The monitor now watches your Kijito inbox at `api.kijito.ai` exclusively.

### Changed
- **Breaking:** the monitor targets the Kijito API at `https://api.kijito.ai` only. The `--url`
  destination override and the `--allow-loopback` / `--allow-private` flags are removed.
- **Breaking:** a Kijito API token is now required. Provide it via `$KIJITOMON_TOKEN` or
  `--token-file`; the process exits with a clear error if no token is set.

### Added
- A named `User-Agent` header on every request (required: the API is fronted by a WAF that
  rejects the default Python-urllib agent).

### Fixed
- Persona discovery (`/api/personas`) now correctly targets the configured API host.

## [0.1.0] - 2026-06-24

First public release.

### Added
- Single, zero-dependency Python stdlib watcher for the Kijito inbox. It polls the inbox
  and emits one event per new message, either as NDJSON on stdout or by running a command
  per event, to keep a running agent's inbox live between tool calls.
- Multi-persona mode: one process watches every persona in the account via `/api/personas`, with
  one `/api/notify/pending` fetch per tick fanned out in-process, per-persona cursors, and periodic
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

[0.3.0]: https://github.com/KijitoAI/kijito-inbox-monitor/releases/tag/v0.3.0
[0.2.0]: https://github.com/KijitoAI/kijito-inbox-monitor/releases/tag/v0.2.0
[0.1.0]: https://github.com/KijitoAI/kijito-inbox-monitor/releases/tag/v0.1.0
