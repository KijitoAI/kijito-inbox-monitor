# Changelog

All notable changes to kijito-inbox-monitor are documented in this file.
The format is based on Keep a Changelog, and this project follows Semantic Versioning.

## [Unreleased]

⚠️ **WHAT THE REVIEW OF THIS SET DOES AND DOES NOT COVER.** The seven alarm/liveness changes were reviewed
and approved by an independent engine-literate reviewer who re-ran the suite and the mutation harness rather
than accepting them. Two limits were disclosed rather than discovered: the reviewer did **not** run the
producer against a live account, so "verified live" claims here rest on the author's word; and the docs/gate
commit was out of that review's scope.

### Added
- **A wake `nonce` on every event** - 11 base62 chars, `base62(sha256(event_id))[:11]`. It lets a consumer join
  a delivered wake back to the queue entry that carried it, and it is recomputable from the `event_id` in the
  same row. **Derived rather than random on purpose:** this producer already defines identity (a `new` event
  keeps the same id across a restart, a state-loss re-delivery, and two watchers of one inbox), and a
  per-emission random nonce would have contradicted that - calling one re-delivered message two different wakes,
  so a consumer finds no queue entry for the second, scores it lost, and alarms **on the recovery path the
  producer exists to survive**. Deriving inherits the existing semantics for both identity families and changes
  neither. Note it is a label and not a secret (deterministic, hence guessable from the `event_id`), and that it
  identifies a *wake* rather than a *delivery* - two panes handed the same message share a nonce, so consumer
  records must key on `(nonce, session_id)`.
- **Emission timestamps (`emitted`)** - `wall`, `monotonic`, `boottime` and `src`, all read at one instant.
  Differencing wall against monotonic across two events measures the time the machine **was not executing**,
  which is what separates "this sat in a queue for hours" from "the host was suspended" - indistinguishable in
  wall time and completely different problems. Measured on a Parallels guest: 72.79 h of hypervisor freeze
  looked like ordinary elapsed wall time, while `BOOTTIME - MONOTONIC` read `0.00 s` throughout, because a
  hypervisor pause stops the guest's clocks together and the guest is not running to notice.
  **The keys name SEMANTICS, not OS constants, because on macOS the two are inverted:** `monotonic` means "does
  not tick while the machine is not executing" (Linux `CLOCK_MONOTONIC`, Darwin `CLOCK_UPTIME_RAW`) and
  `boottime` means "does tick" (Linux `CLOCK_BOOTTIME`, Darwin `CLOCK_MONOTONIC`). macOS `CLOCK_MONOTONIC`
  *includes* sleep and macOS has no `CLOCK_BOOTTIME`, so reading constants by name there yields the wrong
  quantity under the right label with nothing complaining - measured on a real Mac at 408.19 h vs 389.99 h,
  an 18.20 h gap that is the accumulated sleep. `src` names the constant behind each value so consumers can
  audit the mapping from the row instead of assuming it. A key is **omitted rather than faked** where its
  *semantic* is unavailable: a fabricated value is indistinguishable from a real zero-freeze reading, which is
  the failure the field exists to prevent. `ts` is unchanged; `emitted.wall` is the coherent wall reading.
- **An alarm for escalated mail nobody is answering.** Fires only when a member holds mail a sender marked
  URGENT *and* no activity from that member has been observed - both halves positive. Silence alone is never
  the trigger, because idle-by-design and wedged are indistinguishable from outside; the urgent flag is a
  sender declaring an expectation, which is what makes the silence mean something. Clears when either half
  clears, with no ack.
- **An alarm for unread mail the inbox window did not show** (`unread_not_shown` above zero) - the case where
  the endpoint tells you it is holding mail this response did not hand you.
- **An attributable liveness signal** (`--activity-file`) and a one-shot evaluation of it
  (`--check-activity`), so an external supervisor can assert the producer is not merely running but working.
- **A producer-owned event id on every event**, so a consumer can deduplicate across restarts and rotations
  without inferring identity from content.
- **`--no-urgent-alerts`** - see Changed.

### Changed
- **`--no-stranded-alerts` no longer silences the urgent-unanswered alarm.** It gated both, while saying so
  in only one of the three places an operator reads - and its own documented advice ("set this if you keep
  deliberate test inboxes") therefore turned off a higher-severity alarm about real members as a side effect.
  The two alarms now have separate flags; pass both to disable both. The coupling is pinned by tests through
  the real run loop and by its own mutation, because nothing tested the mapping either way before.
  (Found in review of the urgent-unanswered alarm, which is unreleased - so no released behaviour changes.)
- **Account-level alarms are routed by evidence of a consumer**, instead of to every directory persona -
  long-dead test personas were receiving alerts into streams nobody reads.

### Fixed
- **One persona's hostile or un-tightenable `.lock` sidecar killed the whole producer.** `InsecureFile` is an
  `OSError` by design, but every containment arm caught `FatalConfig` only, so the throw went past all of
  them on the startup path and both late-add paths. Containment is now per-persona at four sites, and fails
  **closed**: if every persona fails to initialise, the producer raises rather than staying up watching
  nothing. The lesson worth keeping is that the earlier fix checked that the catch *existed* and never that
  its type *covered the throw*.
- **`RELEASING.md` prescribed a path the package does not contain** - a clone could not run the gate the
  document mandates. Restated as the property plus a self-contained check, with a third prepublish gate
  (`path-escapes`) so the class cannot return.

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
- **Alarm routing now requires evidence of a consumer.** Account-level alarms went to every directory
  persona, which meant long-dead test personas kept receiving alerts into streams nobody reads. Eligibility
  is now positive - observed authorship, or memories the directory says they own - and it mirrors the
  stranded-mail ownership test on purpose, since two predicates for one question drift apart and then
  disagree. Authorship alone suffices so first contact is not broken, and an unreported memory count leaves
  a persona eligible because no data is not evidence of absence.

  It fails open: if the predicate would leave nobody, every directory watcher is used instead. An alarm
  delivered to a dead stream costs a line; one delivered to nobody is the silent failure the tool exists to
  prevent. Measured live, recipients fell from 25 to 18.

- **Urgent-unanswered alarm.** The watcher reports members holding mail a sender marked **urgent** while no
  activity from them has been observed.

  "Is this member stuck?" is normally unanswerable from outside: idle-by-design and wedged look identical,
  so the obvious alarm fires on every quiet persona and rots into noise. The urgent flag breaks the tie
  because it is a *sender* declaring an expectation - not the recipient declaring liveness - and silence
  only means something once something was expected. A quiet member with no urgent mail never trips it, so
  the alarm fires exactly where somebody escalated and nothing happened.

  Both halves of the predicate must be positive: `unread_urgent > 0` and an explicit "no activity in a span
  we covered". A NOT-OBSERVABLE answer means the watcher was not running then, and reporting that as silence
  would be a fabrication. It costs no request - `unread_urgent` arrives on the same row as the unread count
  the fast path already fetches every tick, and was previously parsed and discarded.

  Kept disjoint from the stranded-mail alarm on purpose - that one is for inboxes nobody owns, this one for
  real members who are not responding - because two alarms covering one inbox drift apart and then disagree.
  Same honesty rules as the rest: an `alert` rather than a new event name, one summarising event per
  watcher, the observation and never the diagnosis, self-clearing when either half clears, and no ack.

- **`--activity-file PATH`: publish who has been observed AUTHORING mail.** Refreshed each tick, it lets a
  harness answer "has X been active since my message?" from data the watcher already collects.

  Authorship was chosen over the two signals that look better and are both forgeable by accident. Inbox
  read-state can be produced for any persona by any agent calling the inbox with the default
  `mark_read=true`, and it fails the other way too, since a member consuming its own event stream reads its
  mail without touching read-state. And a GET on the presence endpoint carrying a persona parameter beats
  that persona into the active roster, so merely probing someone makes them look alive. Only B produces B's
  outbound, and nobody else can manufacture or erase it.

  It costs no request: all-personas mode already fetches every inbox each tick with `mark_read=false`, and
  every row already carries its author. That matters, because the alternative - a client polling every
  inbox on a timer to reconstruct this - puts a loop that reads everyone's mail on a schedule, where one
  missing `mark_read=false` destroys read-state fleet-wide.

  Both coverage limits are published, because a claim of silence is only as good as the watching.
  `observed_since` bounds the process; `observation_floor_id` is the MAXIMUM of the per-inbox window floors,
  deliberately not the minimum - a persona's mail lands in whichever inbox they wrote to, so a claim that
  they authored nothing is only as strong as the worst-covered inbox. Queries below the floor answer NOT
  OBSERVABLE rather than "silent". The rendered observation carries the wait count and last-evidence stamp
  and is asserted by test to state no cause, since deadlocked, unreachable and still-working are
  indistinguishable from this data and need opposite responses.

  `--check-activity PERSONA --since-id N` evaluates a published report in one shot, with no token, network
  or watch loop, so a shell heartbeat can call it. It exits 0 on evidence of activity, 1 on a silence in a
  span the report actually covered (printing the observation), and 2 on NOT OBSERVABLE or an unreadable
  report. 1 and 2 are distinct deliberately: collapsing them turns "I was not watching" into "they were
  silent". The watcher and the one-shot share one implementation of the tri-state.

- **Every event now carries a producer-owned `event_id`**, so a consumer can dedupe without hashing our NDJSON
  bytes. Byte-hashing works until it doesn't: it couples the consumer to our serialisation, so a change to key
  order, spacing or `--content-chars` silently changes the dedupe key and re-delivers old events. Prompted by a
  real consumer deduping ID-less events on `event+ts` - unique only while two events never land inside one clock
  tick, and the timestamp is stamped at emit time.

  Two identities, because messages and signals need opposite guarantees. `new` events carry the message's
  identity (`<persona>:new:<message id>`), so the same message always yields the same id - across a restart, a
  re-delivery after state loss, and two watchers of one inbox; dedupe on it for exactly-once processing. Every
  other event is a signal and gets an id unique to that emission (`<persona>:<event>:<run>-<n>`), because a
  recurrence is a genuinely different event and a second outage is a second thing worth seeing. Repeated
  announcements of an unchanged condition are suppressed at the source instead, where suppression belongs.

  `<run>` is random per process. A bare in-process counter is specifically ruled out: it restarts at 1 and hands
  ids a consumer has already seen to brand-new events, so a correct consumer drops live mail - a worse failure
  than the duplicate the id was introduced to prevent. Ids are stamped at the single emit chokepoint, so a future
  event kind cannot forget one, and are exported to `--exec` consumers as `$KIJITOMON_EVENT_ID`.

- **Unread-mail-outside-the-window alarm.** The inbox endpoint reports `unread_not_shown` - unread messages
  it holds that this response did not return. Above zero, the watcher raises an `alert` carrying the count,
  the window floor, the cursor, and `above_watermark`. It is deliberately a cheap signal rather than a
  coverage mechanism: the count has no cursor of its own, so it can say THAT mail is out of view but never
  WHICH rows, and coverage still comes from the backward walk that terminates.

  The event states an observation, not a diagnosis. The count includes unread mail anywhere in the inbox -
  among it messages this watcher already delivered that the agent simply has not read - so it is not on its
  own evidence that anything was missed. `above_watermark` is the fact that separates the two cases, and is
  reported rather than resolved.

  A zero is not self-justifying, and this is the trap the implementation is built around: the server computes
  the field only when it withheld something, so it is `0` **by construction** on a page with nothing older.
  The negative answer therefore requires positive evidence - the zero was genuinely computed, or the window
  is structurally complete - and a field the server never sent is a third state that asserts nothing either
  way. For the same reason the check runs on the newest-page poll only. Measured against a live inbox holding
  four unread messages: the newest page reported `0` (correctly), a mid-walk page reported `4` (the whole
  inbox's unread, not that window's), and the terminal page of the walk reported `0` while all four sat above
  it - so feeding walk pages to the check would both invent alarms and clear real ones.

  Routed like the stranded-mail alarm (an `alert` rather than a new event name, no ack, self-clearing) but
  failing the opposite way when the persona directory is unknown: that alarm withholds because it would
  otherwise flag every persona, while this one concerns the target's own inbox, where firing needlessly costs
  a line in a stream nobody reads and withholding costs the silent wake gap this tool exists to prevent.

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
  name, so consumers already filtering `alert` surface it without being rearmed - a fresh name would have gone
  unwatched on every armed consumer, because a running `grep` never re-reads its argv; and it is
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
