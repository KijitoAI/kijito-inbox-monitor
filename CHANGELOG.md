# Changelog

All notable changes to kijito-inbox-monitor are documented in this file.
The format is based on Keep a Changelog, and this project follows Semantic Versioning.

## [Unreleased]

### Added
- **Stranded-mail alarm.** The watcher reports mail sitting in an inbox that nothing consumes. Such mail
  is undeliverable and nothing else reports it: the sender gets a success and a message id, the recipient
  gets no signal, and there is no bounce. Two real cases prompted it - a case-variant of a live persona,
  whose reply sat unread for 14 days, and a group-looking name (`all`) with no broadcast semantics behind
  it, which swallowed a fleet-wide announcement for 4 days.

  An inbox is flagged when it holds mail and **either** the persona directory does not list it **or** it
  owns zero memories - nothing has ever written as that persona, so nobody is working under it. The second
  test matters because a directory built as a union of registered *recipients* lists every typo the moment
  someone sends to it, which would leave the first test unable to fire. Ownership reads the top-level
  `memory_count`, deliberately not a sum of `projects[].count`: project counts exclude global-scoped
  memories, so a persona whose memories are all global sums to zero and looks unowned - measured against a
  live account, that mistake would have flagged eight of nine active personas. Where a server reports no
  memory counts the signal stays quiet rather than guessing.

  Both signals come from endpoints already fetched, so the check costs no extra request. Reported once per
  inbox per process, to stderr and as one summarising event per watcher; a case-variant is diagnosed as
  such, naming its twin. Disable with `--no-stranded-alerts`.

  Two routing rules are load-bearing and easy to get wrong: the alarm is an `alert` rather than a new event
  name, so consumers already filtering `new|alert|recovered` surface it without being rearmed; and it is
  routed only to watchers backed by a real directory persona, because a stranded inbox has mail and
  therefore acquires a watch target and stream of its own - alerting every target would write the alarm
  into the very stream nobody reads. Producing an event is not delivering it.
- `$KIJITOMON_STRANDED` exposes the affected inboxes to `exec-per-event` consumers, comma-separated.

### Fixed
- **A bounded inbox window could permanently skip mail** (reported by Loom). The inbox endpoint returns
  the **newest** messages that fit a count limit *and* an aggregate content budget, and declares what it
  left out via `truncated` / `size_truncated` / `size_dropped`. The watcher parsed only `result`, discarded
  those fields, and advanced its cursor to the highest id it had seen - so any message the server omitted
  while it sat *above* the cursor was never emitted and was stepped over permanently. The truncation was
  never silent in the data, only in the handling of it.

  The cursor is now a **confirmed-contiguous watermark**. When the window reaches back past it, every
  omitted message is older than anything still owed and nothing changes - the steady state, since
  long-polling keeps the backlog small. When the window starts *above* the watermark while the server
  admits it withheld rows, the watcher reconciles with an `unread_only` re-fetch and then advances **only**
  on positive evidence the span is accounted for: that re-fetch must itself have been complete, and must
  yield at least as many previously-unseen rows as the server said it withheld. Anything less **pins** the
  watermark and raises an `alert` naming the cursor, the window floor and the shortfall.

  Pinning is the point: advancing past an unresolved span makes the next poll see the window reaching back
  past the cursor, declare itself safe, and bury the omission forever. The pin is persisted, so a restart
  neither re-emits what was already delivered nor forgets the gap, and visible mail is still delivered
  while pinned - failing closed costs no liveness. Recovery via `unread_only` is a heuristic, not an
  authoritative backward page; it is the best available until the endpoint offers one, and it is never
  treated as proof.

  Two accounting rules keep the alarm honest. Only rows genuinely absent from the visible window count as
  recovered - the watcher peeks without marking read, so an immediate retry commonly echoes the same
  messages, and counting those would report a recovery that never happened. And a lone oversized message
  (`size_truncated` with `size_dropped: 0`) had its body clipped rather than being withheld, so it is not
  an omission; count-limit truncation, size-budget drops and body clipping are accounted separately.

  No mail was lost in practice before this: polling cadence kept every observed window reaching back past
  the cursor. That was luck, not correctness - roughly eight typical messages in one gap exhausts the budget.

  Four further defects in that machinery, all found by Loom's third re-audit and all real:
  **new arrivals cannot prove an old omission was recovered** - a message landing between the two fetches
  used to satisfy the shortfall while the hidden span stayed hidden, so only rows falling strictly INSIDE
  the uncovered span now count; **a restart must respect a restored pin** - the arming path selected every
  id above the cursor without consulting what a previous run had already delivered, re-emitting it, and
  the replay cap moved the cursor to the newest id before any gap check, erasing the pin entirely;
  **pin tracking is bounded** so a gap that can never close cannot grow the state file without end; and
  **the gap alert is keyed on the pinned watermark, not the window floor**, because the floor drifts
  upward with every new message and re-announced the same unresolved span. The alert key is persisted too.
- **Case-variant personas no longer self-deadlock the watcher (silent wake gap).** A persona name was
  mapped to its state file verbatim, but macOS (APFS) and Windows are case-**insensitive**, so
  `Claude-chat` and `claude-chat` name the *same* file. Discovering a case-variant of an already-watched
  persona made the watcher try to lock a state file it already held, so the variant was never adopted and
  got **no event stream at all** - mail addressed to it woke nobody, and the failed adoption logged on
  every tick (one observed 3-day run: 20,079 of 20,129 stderr lines from that single warning).
  Persona matching is now case-insensitive throughout, and the persona's original case is preserved for the
  API - case-insensitive match, case-preserving display. Note the deliberate asymmetry with the
  stranded-mail check, which compares names **exactly**, because the server's inbox namespace *is*
  case-sensitive and casefolding there would hide the very defect it detects.
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
