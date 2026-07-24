# Changelog

All notable changes to kijito-inbox-monitor are documented in this file.
The format is based on Keep a Changelog, and this project follows Semantic Versioning.

## [Unreleased]

### Added
- **Stranded-mail alarm.** The watcher now reports mail sitting in an inbox that is not a known persona,
  i.e. an inbox that is *receiving* while nothing consumes it. Such mail is undeliverable and nothing
  else reports it: the sender gets a success and a message id, the recipient gets no signal, and there is
  no bounce. Two real cases prompted this - a case-variant of a live persona (a substantive reply sat
  unread for 14 days), and a group-looking name (`all`) that has no broadcast semantics behind it, which
  swallowed a fleet-wide announcement for 4 days.
  Detected by diffing the persona **directory** (`/api/personas`) against the **inbox** namespace
  (`/api/notify/pending`), both of which are already fetched, so the check costs no extra request.
  Reported once per inbox per process, to stderr and as an `alert` event summarising the whole backlog
  into one event per watcher. A case-variant is diagnosed as such, naming its twin. Disable with
  `--no-stranded-alerts` if you keep deliberate test inboxes.

  Two routing rules are load-bearing and easy to get wrong: the alarm is an `alert` rather than a new
  event name, so consumers already filtering `new|alert|recovered` surface it without being rearmed; and
  it is routed only to watchers backed by a real directory persona, because a stranded inbox has mail and
  therefore acquires a watch target and stream of its own - alerting every target would write the alarm
  into the very stream nobody reads. Producing an event is not delivering it.

### Fixed
- **Case-variant personas no longer self-deadlock the watcher (silent wake gap).** A persona name was
  mapped to its state file verbatim, but macOS (APFS) and Windows are case-**insensitive**, so
  `Claude-chat` and `claude-chat` name the *same* file. Discovering a case-variant of an
  already-watched persona made the watcher try to lock a state file it already held itself, so the
  variant was never adopted and got **no event stream at all** - mail addressed to it woke nobody, and
  the failed adoption logged a warning on every tick (one observed 3-day run: 20,079 of 20,129 stderr
  lines from that single warning, burying every other diagnostic).
  Persona matching is now case-insensitive throughout - state-file paths are casefolded, and both
  `/api/personas` and notify-counts discovery treat a case-variant as already watched. The persona's
  original case is preserved for the API, i.e. case-insensitive match, case-preserving display.
- **Per-persona warnings are emitted once per process** instead of once per tick, so a condition that
  cannot resolve itself can no longer grow stderr without bound.

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
