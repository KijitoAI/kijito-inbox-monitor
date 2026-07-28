# Changelog

All notable changes to kijito-inbox-monitor are documented in this file.
The format is based on Keep a Changelog, and this project follows Semantic Versioning.

## [Unreleased]

## [0.4.0] - 2026-07-28

⚠️ **WHAT THIS RELEASE DOES AND DOES NOT ESTABLISH.** Two audit rounds swept a defect CLASS rather than
patching instances: *a safety check whose result nobody consumes, a safety state nothing clears, and - added
in the second round - a state committed as if an action succeeded before it did.* **It establishes that the
KNOWN INSTANCES ARE FIXED. It does NOT establish that no further shapes exist.** That distinction is not
boilerplate: widening the class immediately surfaced two more sites AND a blind spot in the detector itself
(bool-return detection was not transitive, so one function was invisible and another was caught only by the
accident of an unrelated `return True`). A class derived from N instances cannot be validated by
rediscovering those N.
⚠️ **PROVENANCE OF THE REVIEW.** The final verdict came from the author of the acceptance criteria, who was
also a party to the technical dispute it adjudicated. She disclosed both conflicts and held the
do-not-ship outcome as genuinely reachable - and returned it once before this release. The independent
reviewer originally assigned never read the request.

### Fixed (re-audit 11 - the alarm path)
- **An alarm could be recorded as raised when it was never delivered.** Every alarm committed its
  "already alarmed" state BEFORE emitting and discarded the emit's answer, and three of the four had no
  second channel. An undelivered alarm was never re-raised - not when the channel recovered, and not after
  a restart, because `gap_alerted` is persisted. **Mail was never at risk** (the cursor holds correctly
  throughout); it was the ALARMS that vanished, which matters because the headline promise is that a walk
  which cannot complete pins *loudly* rather than in silence. `WatchTarget.lifecycle` now returns delivery,
  and alarms go through `_alarm()`, which falls back to **stderr** - never a retry down the channel that
  just failed. A pure announcement latch (`gap_alerted`) commits only on delivery; behavioural state
  (`fsm_state`, `pin_evidence_intact`) commits regardless, because refusing to record evidence loss would
  trade a lost alarm for a lost invariant. Lifecycle events remain unacknowledged and ungated (DESIGN.md
  §170 unchanged); the guaranteed/informational split is now documented as §14.9.
- **The recovery edge failed the same way, facing the other way** - both `recovered` sites committed
  `fsm_state = "UP"` and discarded the emit, leaving a consumer that saw the DOWN alert holding an alarm it
  could never clear.
- **The dead-man's switch had no test at all.** Deleting the liveness DOWN alert outright left the entire
  suite green - the one event the README sells as the headline feature was undefended. It now has tests and
  its own mutation.
- **Two case-variant personas could refuse to start the whole producer.** `requested_personas()` deduped
  exactly while `new_personas()` casefolds, so `--persona Loom --persona loom` resolved to one state path;
  the second `flock` raised out of an uncaught list comprehension and killed startup for *every* persona,
  blaming "another watcher" for a collision with itself.

### Fixed (documentation)
- `RELEASING.md` claimed the producer "does not run this package (yet)" and "executes the WORKING TREE
  directly", four lines after the preceding section said the opposite. It had been stale since the producer
  was pinned to a read-only artifact, and the stale half was the dangerous one: a reader who trusted it
  would edit the tree, restart, and **deploy nothing while believing they deployed**.


### Security
- **The first fix for the permissions bug was itself a worse bug** (Loom re-audit 9, HIGH 1). The repair
  introduced in the previous entry followed symlinks, validated neither owner nor file type, and - because
  it was deliberately best-effort so that "a file we do not own cannot kill the watcher" - wrote the mail
  anyway when the `chmod` failed. Measured: a pre-existing 0666 file stayed 0666 **and received mail**; a
  symlink's target was chmod'ed and appended to; a *dangling* symlink caused its target to be created in
  another directory. A passive disclosure had been turned into an active write primitive. Opens now use
  `O_NOFOLLOW` and `O_NONBLOCK` (a FIFO at the path would otherwise block the writer forever - a hang,
  which is worse than a crash because nothing reports it) and validate on the already-open descriptor that
  the file is regular, owned by this user, and exactly 0600. Anything else is refused, and a refusal is a
  **failed delivery**: the cursor holds and the mail is retried rather than written somewhere unsafe.
- **Only the file being opened was repaired** (Loom re-audit 9, HIGH 2). Pre-existing rotated archives and
  an existing state file kept their old modes, and a 0700 file was left alone because the check tested
  `mode & 0o077` rather than requiring exactly 0600. All persisted artifacts are now repaired, and
  directories are 0700 at **every** level (`os.makedirs(mode=)` applies the mode to the leaf only, so
  nested paths left their parents 0755). A directory writable by other users is reported.
- **The event stream was world-readable and it carries message bodies** (Loom re-audit 8, HIGH 1). Event
  files and the state-file lock sidecar were created with a plain `open()`, which takes the process umask -
  022 by default - so every `events.<persona>.ndjson` was mode 0644 and readable by any other local user,
  with message content in it unless `--no-content` was set. The auth token (0600) and the state file (0600
  via `mkstemp`) were already correct, which is what made the gap easy to miss: the one file nobody had
  thought about is the one holding the plaintext. Both are now created 0600, directories this tool creates
  are 0700, and an **existing** file that is more permissive is tightened on open and the change reported -
  because the creation mode does nothing for files that already leaked. Rotated archives inherit 0600 from
  the live file. If you have been running an earlier version, check the modes on your events files.

### Changed
- **Delivery is now ACKNOWLEDGED rather than assumed, and the delivery guarantee is stated honestly as
  at-least-once, in order** (Loom re-audit 7, HIGH 1). The cursor *is* the acknowledgement: once it moves
  past an id that message is never fetched again. It previously advanced on selection, so an `--exec` that
  exited non-zero, timed out, or failed to spawn had its result discarded and the message was silently
  dropped - on the one path whose entire purpose is waking an agent. It now advances only over messages the
  emitter reports as delivered (`exec` exit 0, or a successful write), stopping at the first failure so a
  consumer never sees message N+1 ahead of a retried N. **Make your consumer idempotent**; `KIJITOMON_ID` is
  stable across re-deliveries. This also resolves a contradiction that already existed between the README
  ("exactly once across restarts") and DESIGN.md ("best-effort/at-most-once"); the docs now agree.

### Fixed
- **A window that withheld nothing while pointing at older mail was believed** (Loom re-audit 7, HIGH 4).
  The server sets `next_before_id` *exactly* when rows were withheld, so "I hid nothing" and "there is more"
  cannot both be true - and the gap check never looked at the continuation at all, so it took the first half
  at its word and advanced over whatever the second half pointed at. Both directions of the contradiction now
  pin. Verified against the live API across 14 pages, including the exactly-at-limit edge that could have made
  the rule fire on healthy traffic (it does not).
- **A malformed pin field in the state file failed open** (Loom re-audit 7, HIGH 2). `pin_forced` was read
  as `value is True`, so a JSON `1` normalised to false and silently *unpinned* the watermark, letting the
  replay cap cross the very span the pin was protecting; `pin_evidence_intact: 0` had the mirror bug, and
  booleans were accepted as message ids. Every persisted field is now read strictly, and anything
  unrecognised is treated as a corrupt state file rather than a permissive default.
- **A persona respelled in a different case destroyed its own cursor** (Loom re-audit 7, HIGH 3). The state
  *path* casefolds while the stored *identity* keeps the directory's spelling, so a file written as
  `persona=Loom` was reloaded by a run that discovered `loom`, judged a mismatch, and re-baselined - skipping
  every message since. A case-only difference now migrates the file and keeps the cursor. Deliberately
  narrow: only the query *value* is compared case-insensitively.
- **The corruption-recovery pin could never clear** (Loom re-audit 7, HIGH 5). It parks the watermark one
  below the window it re-emits, which made the ordinary release test unsatisfiable by that same window - so
  the pin held forever, the cursor froze, and because delivered ids were recorded only while a *gap* was
  pinned, the identical window was re-emitted on every poll and across every restart. The pin now carries a
  persisted release floor, and every delivered id the watermark does not cover is remembered whatever left it
  uncovered. (Repairing a fail-open into a permanent fail-closed is not a repair.)
- **A cursor write whose durability was unproven was reported to nobody** (Loom re-audit 9, MEDIUM). The
  previous entry made `save()` *return* a durability status; the call site then discarded it - the same
  defect one layer out. The watcher now consumes that answer and reports an unproven cursor once, clearing
  when persistence recovers.
- **A sink that could not be opened safely crashed the poll loop or fell through to stdout** (Loom
  re-audit 9, MEDIUM). A failed reopen after rotation raised out of `write()`, which under a supervisor is
  a crash loop; and a refused per-persona sink returned `None`, which means "no sink configured, write to
  stdout" - printing the very mail that had just been declined. Both are now failed deliveries, contained
  to the affected persona, and a broken sink retries and recovers on its own.
- **The events file's DIRECTORY ENTRY was never made durable** (Loom re-audit 8, HIGH 2). `fsync` on the
  file descriptor makes the *bytes* durable; the *name* lives in the directory. On create and on rotation
  the directory was left unsynced, so the state directory could persist an advanced cursor while the event
  pathname or a rotated archive was lost - and `--state-file` and `--events-file-template` may be in
  *different* directories, so syncing one proves nothing about the other. The events directory is now
  synced before the cursor that acknowledges those events is persisted, and a failure holds the cursor.
- **A failed state-directory `fsync` was reported as success** (Loom re-audit 8, HIGH 3). `save()` called
  the sync and discarded its answer, so the cursor was written and its durability merely assumed, with no
  diagnostic. `save()` now returns whether the write is durable and says so loudly when it is not. (The
  failure direction is re-delivery rather than loss - a reverted state file replays mail - but a watcher
  that cannot tell you it failed to persist will keep not telling you.)
- **A cursor could outlive the event it acknowledged** (Loom re-audit 7, MEDIUM). The state file's temp was
  fsynced but the directory holding the rename was not, and the event sink was flushed but never fsynced.
  Events are now fsynced *before* the cursor that acknowledges them is persisted, and the state directory is
  fsynced after `os.replace`; a sink that cannot be synced retracts that poll's acknowledgements entirely.
- The single-writer lock file descriptor was never released - leaked on every refused lock, and the source of
  the suite's two `ResourceWarning`s. `StateFile.unlock()` now exists and is called on shutdown.

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
  admits it withheld rows, the watcher **walks the span backward** with `before_id`, paging until it
  reaches the watermark or the chain ends, and advances only then. A walk that fails, stalls, or exhausts
  its page budget proves nothing, so the watermark **pins** and an `alert` names the cursor, the window
  floor and the shortfall.

  Coverage is established by **exhausting the chain, not by counting rows**. That distinction is what
  makes an unquantified truncation resolvable at all: `truncated` states that rows were withheld without
  saying how many, so no arithmetic can ever prove the span empty. It also reaches messages someone has
  already **read** - precisely the rows most likely to be hiding in an old span, and the ones an
  unread-only reconcile structurally cannot see.

  Pinning is the point: advancing past an unresolved span makes the next poll see the window reaching back
  past the cursor, declare itself safe, and bury the omission forever. The pin is persisted, so a restart
  neither re-emits what was already delivered nor forgets the gap, and visible mail is still delivered
  while pinned - failing closed costs no liveness. Pin tracking is bounded; on overflow the watcher says
  plainly that it can no longer reason about the span rather than quietly dropping ids, and only an
  authoritative walk can restore that ground truth.

  Two accounting rules keep it honest. Mail arriving *between* the two reads is delivered but never counted
  as recovery - new arrivals prove nothing about old omissions. And a lone oversized message
  (`size_truncated` with `size_dropped: 0`) had its body clipped rather than being withheld, so it is not
  an omission; count-limit truncation, size-budget drops and body clipping are accounted separately.

  Corrupt pin state fails **closed**: a malformed record holds the pin with no tracking rather than
  silently unpinning, because loading it as "nothing outstanding" would let the replay cap jump the cursor
  over the very span the pin was protecting.

  No mail was lost in practice before this: polling cadence kept every observed window reaching back past
  the cursor. That was luck, not correctness - roughly eight typical messages in one gap exhausts the budget.
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
