# Changelog

All notable changes to kijito-inbox-monitor are documented in this file.
The format is based on Keep a Changelog, and this project follows Semantic Versioning.

## [Unreleased]

### Changed
- **The stranded-mail check now asks whether anyone OWNS an inbox, not just whether the directory lists
  it.** Directory membership was a sound proxy for "nobody consumes this" only while the directory was
  derived from authorship. A server may instead build it as a union that includes every registered
  *recipient* - and a recipient is registered the moment anyone sends to that name, typo included - which
  makes every future phantom "known" instantly and the absence signal unable to fire at all.

  So an inbox is now also flagged when it holds mail while owning **zero memories**: nothing has ever
  written as that persona, so nobody is working under it. That tracks the actual invariant - whether a
  consumer exists - instead of a proxy for it. The original absence signal is kept, so a name missing from
  the directory still fires on its own, and where a server reports no memory counts the new signal stays
  quiet rather than guessing.

  Ownership is read from the top-level `memory_count`, deliberately **not** by summing `projects[].count`:
  project counts exclude global-scoped memories, so a persona whose memories are all global sums to zero
  and looks unowned. Measured against a live account, that mistake would have flagged eight of nine
  active personas.

### Fixed
- **A bounded inbox window could permanently skip mail** (reported by Loom against 0.3.0). The inbox
  endpoint returns the **newest** messages that fit a count limit *and* an aggregate content budget, and
  declares what it left out via `truncated` / `size_truncated` / `size_dropped`. The watcher parsed only
  `result`, discarded those fields, and then advanced its cursor to the highest id it had seen - so any
  message the server omitted while it sat *above* the cursor was never emitted and was stepped over
  permanently. The truncation was never silent in the data, only in the handling of it.

  The watcher now reads the declaration and applies a cheap test: if the window reaches back past the
  cursor, every omitted message is below it and was already delivered, so nothing changes (this is the
  steady state - long-polling keeps the backlog to a message or two). If the window *starts above* the
  cursor while the server admits it dropped things, the uncovered span may hold undelivered mail, and the
  watcher reconciles with an `unread_only` re-fetch - a far smaller window, and precisely the set where a
  missed wake still matters - before advancing. Anything it still cannot account for raises an `alert`
  naming the cursor, the window floor, and the shortfall, rather than advancing quietly.

  No mail was lost in practice before this fix: polling cadence kept every observed window reaching back
  past the cursor. That was luck, not correctness - roughly eight typical messages in one gap exhausts the
  budget, which any outage plus a burst can produce.

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
