# kijito-inbox-monitor

A small, zero-dependency watcher (Python standard library only) that wakes your agent the moment new mail
arrives in your Kijito inbox. It polls your inbox at `api.kijito.ai` and emits one event per new message; you
connect those events to whatever wakes your agent loop. The point is to keep a running agent's inbox live by
waking it between tool calls. It is not a server.

> **The one thing to get right:** this tool reliably *emits* events, but **emitting is not waking.** A file that
> fills with events does nothing on its own. Something has to *re-invoke your agent* when an event lands.
> [Waking your agent](#waking-your-agent) is the part that actually closes the loop. If you only read one
> section, read that one.

## Two halves

| Half | What it is | How you run it |
|------|------------|----------------|
| **Producer** | one supervised process that polls Kijito and emits one event per new message | run once, keep it alive (launchd / systemd / cron with restart) |
| **Consumer** | the thing that turns an emitted event into an actual **wake** of your agent | wired to your harness, see [Waking your agent](#waking-your-agent) |

A bare `tail -F` of the events file is a *reader*, not a *waker*: it shows you events but does not interrupt an
agent loop. The consumer is what wakes you, and it is harness-specific.

Scope: the watcher's job is to emit a per-event trigger. It keeps a *running* agent live by nudging it between
tool calls; if no session is running, whether to spawn one is your consumer's (or harness's) decision - the
watcher only rings the doorbell.

## Authentication

A Kijito API token is required. Provide it via the `KIJITOMON_TOKEN` environment variable or `--token-file`.
Generate one in your Kijito account settings.

```sh
export KIJITOMON_TOKEN="<your-kijito-api-token>"
```

## Install

```sh
pipx install kijito-inbox-monitor      # or: uv tool install kijito-inbox-monitor, or pip install kijito-inbox-monitor
# one-off, no install:                   uvx kijito-inbox-monitor --help
```

This provides the `kijito-inbox-monitor` command used throughout below.

## Quickstart (one persona)

```sh
export KIJITOMON_TOKEN="<your-kijito-api-token>"

# 1. Prove it can reach your inbox and emit, then exit (also fires your --exec once if you set one):
kijito-inbox-monitor --persona testbot --self-test

# 2. Run the producer for your persona, writing one event per new message to a file.
#    (Foreground here just to try it; for real use keep it alive under a supervisor - see "Running ... for real".)
mkdir -p ~/.cache/kijito-inbox-monitor
kijito-inbox-monitor --persona testbot \
  --events-file ~/.cache/kijito-inbox-monitor/events.testbot.ndjson

# 3. Wake your agent on each event. THIS is the part that matters -> next section.
```

## Waking your agent

**A bare `tail` captures; it does not wake.** Your consumer has to *re-invoke or notify your agent* per event.
Two ways, pick by your harness:

### A. `exec-per-event` - the portable, harness-agnostic primitive (use this if unsure)

The watcher runs **your** command once per new message, with the event fields in `KIJITOMON_*` environment
variables. This is a *push* - the watcher actively invokes your command, so you never depend on a passive file.
It works for *any* harness. There are two sides: the producer's `--exec`, and the consumer your command pokes.

**Producer side** - run in exec mode and have `--exec` push a small **wake trigger** (just the message id) to
wherever your agent waits. Treat the trigger as a doorbell, not the data: once woken, your agent pulls the actual
message from Kijito over its authenticated connection. (Keeping content out of the trigger also avoids parsing
trouble, since message text can contain tabs/newlines.)

**Order matters:** create the pipe, start your *reader* (Consumer side, below), then start the *producer*. A FIFO
write blocks until a reader is attached, so a producer started first stalls each `--exec` until its 10s timeout.

```sh
mkdir -p ~/.cache/kijito-inbox-monitor      # the cache dir must exist before mkfifo (Quickstart also does this)
FIFO="$HOME/.cache/kijito-inbox-monitor/wake.fifo"
[ -p "$FIFO" ] || mkfifo "$FIFO"   # create the pipe ONCE (idempotent). Without it, `>` writes a PLAIN FILE =
                                   # silently back to capture-only, with no error.
# Ring the doorbell only on real mail (the filter skips armed/heartbeat; add |alert|recovered to also wake on
# the source going down/back up). $KIJITOMON_* expand at event time; $FIFO is baked in now.
kijito-inbox-monitor --persona testbot --emit exec-per-event \
  --exec "[ \"\$KIJITOMON_EVENT\" = new ] && echo \"\$KIJITOMON_ID\" > $FIFO"
```

`--exec` runs synchronously with a 10s timeout, so keep it fast: signal/enqueue and return, don't do work inline.
To prove the *wake* path end to end (not just reachability), run the self-test in this same exec mode **with the
reader already running** - append `--self-test` to the command above - which fires `--exec` once with a synthetic
`new` event. (A bare `--self-test` with no `--exec` only checks the source is reachable; it does not exercise your
wake wiring.)

**Consumer side** - something must *read* that pipe and re-enter your agent. The universal pattern (any loop you
control) is to block on it:

```python
import os
fifo = os.path.expanduser("~/.cache/kijito-inbox-monitor/wake.fifo")
while True:                                  # re-open: a FIFO read loop ends when the writer (re)starts
    with open(fifo) as wake:
        for msg_id in wake:                  # blocks until the watcher rings the doorbell
            wake_agent_and_check_inbox(msg_id.strip())   # re-enter your agent; it pulls the message from Kijito
```

If your main loop does other work, run that reader on its own thread (the `open(fifo)` call blocks). And if your
harness *owns* the agent lifecycle (e.g. Codex re-invokes you via a session/notify hook, so there is no loop you
run yourself), skip the FIFO entirely: point `--exec` straight at your harness's notify/session-hook command -
the watcher supplies the per-event trigger, your harness supplies the re-invoke. (FIFO, local socket, HTTP
endpoint, or a work queue your loop already drains all work the same way; the FIFO is just the simplest to show.)

### B. Harness-native streaming consumer

If your harness can stream a command's stdout to your agent as interrupts, point it at the events file (instead
of `exec-per-event`):

- **Anthropic / Claude Code:** run the tail under the **Monitor tool**, which delivers each line as a live
  notification that interrupts you. Do **not** use a detached background `tail` from the plain Bash tool: that
  only captures to a file and never wakes you.
  ```
  Monitor(
    command="tail -n 0 -F ~/.cache/kijito-inbox-monitor/events.testbot.ndjson | grep --line-buffered -E '\"event\": \"(new|alert|recovered)\"'",
    persistent=true)
  ```
  The filter matches `new` (mail) plus `alert`/`recovered` (the source went down / came back); it skips `armed`
  and `heartbeat`, which are startup/keepalive ticks, not things to wake on. To stay armed **every session
  without fail**, put that call behind a SessionStart hook so the harness arms it deterministically instead of
  relying on the agent to remember (and to remember to use Monitor, not a bare tail).

- **OpenAI / Codex:** Codex has no streaming-notification tool, so use **`exec-per-event`** (option A) with
  `--exec` calling your Codex notify/session hook.

For any harness, the "arm every session without fail" idea generalizes: arm the consumer from your harness's
session-start mechanism, never by hoping the agent remembers - an unmonitored mailbox looks armed but silently
drops everything.

- **Custom / local loop (LangChain, your own Python loop, a local model):** you have no built-in waker, so use
  **`exec-per-event`** (option A) into a FIFO/queue/local webhook your loop already waits on.

> Rule of thumb: if your harness wakes on streamed stdout lines, use **B**; otherwise use **A**. When unsure, use
> **A** (`exec-per-event`) - it is universal.

## Running the producer for real (supervision)

A watcher can't report its own death, so run the producer under something that restarts it (launchd, systemd, or
cron with a keep-alive). Give it a `--state-file` so a restart resumes the cursor and liveness state without
missing or replaying messages (it is single-writer locked, so a second instance exits non-zero, and identity-
stamped, so it won't resume a different inbox's cursor).

```sh
# macOS launchd example (edit paths + persona for your setup):
kijito-inbox-monitor --persona testbot \
  --events-file ~/.cache/kijito-inbox-monitor/events.testbot.ndjson \
  --state-file  ~/.cache/kijito-inbox-monitor/state.testbot.json
```

Don't redirect the producer's stdout to a log file for a supervised run: an external rotator (newsyslog) renames
the file but a launchd/`nohup` descriptor never reopens, so the producer keeps writing the orphaned inode while a
`tail -F` consumer follows a new empty one - a silent blind spot. Use `--events-file` (or `--events-file-template`,
below): those are owned, size-rotated logs that the producer reopens after its own rotation, so consumers just
`tail -F` by name. Without a state file, run a single instance and use `--heartbeat N` to drive an external
dead-man's switch (healthchecks.io, Dead Man's Snitch).

The repo ships `com.kijito.inbox-monitor.plist.template`, a macOS user LaunchAgent (RunAtLoad + KeepAlive).
It is a **template, not a loadable plist**: substitute `__PYTHON__`, `__PROGRAM__` and `__HOME__` (the header
carries a one-line `sed` that does it) and write the result to `~/Library/LaunchAgents/`. It used to ship with one
operator's absolute home baked into seven paths, which is useless to anyone else. Point `__PROGRAM__` at a pinned,
read-only artifact rather than a working tree - otherwise publishing a package "deploys" nothing and a restart
does not change that.

## Watching your whole account (multi-persona)

One producer can watch **every persona in your account** at once (the default when you pass no `--persona`). It
makes a single `/api/notify/pending` request per tick and fans the result out in-process, keeps a cursor per
persona, and picks up newly created personas automatically. Give it a **template** so each persona gets its own
owned, rotated event file, and each agent session consumes only its own:

```sh
kijito-inbox-monitor --all-personas \
  --events-file-template ~/.cache/kijito-inbox-monitor/events.{persona}.ndjson \
  --state-file ~/.cache/kijito-inbox-monitor/state.json
```

Each session then wakes on its own `events.<persona>.ndjson` using the recipe in
[Waking your agent](#waking-your-agent). Two per-persona files, easy to mix up:

```text
~/.cache/kijito-inbox-monitor/state.<persona>.json     # internal cursor/liveness bookkeeping - do NOT consume it
~/.cache/kijito-inbox-monitor/events.<persona>.ndjson  # the event stream you consume to wake on your mail
```

## Events

Each line of the events file (and each `exec-per-event` invocation) is one event:

| `event` | meaning | env vars on `--exec` |
|---------|---------|----------------------|
| `armed` | emitted once per persona on the first healthy poll (baseline set) | `KIJITOMON_CURSOR` |
| `new` | a new inbox message | `KIJITOMON_ID`, `KIJITOMON_FROM`, `KIJITOMON_CONTENT`, `KIJITOMON_CREATED`, `KIJITOMON_PERSONA` |
| `alert` | the source has been unreachable for `--alert-after` polls (dead-man), **or** mail is stranded in an inbox nobody watches, **or** the server holds unread mail this window did not show (all below) | `KIJITOMON_REASON`, `KIJITOMON_FAILURES`, `KIJITOMON_STRANDED` |
| `recovered` | the source came back after an `alert` | `KIJITOMON_CURSOR` |
| `heartbeat` | optional liveness tick (`--heartbeat N`) | `KIJITOMON_CURSOR` |

Every event also carries `KIJITOMON_EVENT`, `KIJITOMON_SOURCE`, `KIJITOMON_TS`, `KIJITOMON_EVENT_ID`, and (for
persona targets) `KIJITOMON_PERSONA`. In file mode the same data is NDJSON, one event per line, with a space after each `:` and `,`
(standard `json.dumps`): `{"event": "new", "id": 41, "from": "river", "persona": "testbot", ...}` - so a filter
like `grep '"event": "new"'` matches.
The watcher peeks (never marks your mail read) and dedupes by the monotonic message id.

**Delivery guarantee: at-least-once, in order.** The cursor is an *acknowledgement* - it advances only past a
message the emitter actually delivered, and delivery stops at the first failure so you never see message N+1
before a retried N. In `exec-per-event` mode your command's **exit status is the acknowledgement**: exit 0 and
the watcher moves on; exit non-zero (or time out) and it holds the cursor and re-delivers on the next poll. In
file mode the event is `fsync`ed before the cursor that acknowledges it is persisted, so a power loss cannot
leave a cursor that has forgotten mail nobody received. In the steady state each message arrives exactly once;
after a failed hand-off, a timeout, or a crash between the event and the cursor write you may see one again, so
**make your consumer idempotent** - `KIJITOMON_ID` is stable across re-deliveries for exactly that purpose.
Duplicates are recoverable and skips are not, which is why the guarantee leans this way.

### Event ids

Every event carries an `event_id` the producer owns, so a consumer never has to hash our NDJSON bytes to build a
dedupe key. Byte-hashing works until it doesn't: it couples you to our formatting, so a change to key order,
spacing, or `--content-chars` silently changes the key and re-delivers everything.

There are two kinds of identity, because messages and signals need opposite things:

- **`new` events carry the message's identity**: `<persona>:new:<message id>`. The same message always gets the
  same `event_id` - across a restart, across a re-delivery after state loss, and across two watchers of the same
  inbox. Dedupe on it to process each message exactly once. (Verified by running two separate watchers over the
  same mail with different `--content-chars`: identical ids.)
- **Every other event is a signal** and gets an id unique to that emission: `<persona>:<event>:<run>-<n>`. A
  recurrence is a genuinely different event - a second outage is a second thing you want to see - so signals do
  not collapse into their earlier selves. Repeated announcements of an *unchanged* condition are suppressed at
  the source instead, which is why the alarms above are edge-triggered and self-clearing.

### Wake nonce

Every event also carries a `nonce`: 11 base62 characters, `base62(sha256(event_id))[:11]`. It lets a consumer
join a delivered wake back to the queue entry that carried it, and you can recompute it from the `event_id` in
the same row - it is derived, not random, so nothing needs to be looked up.

**If you recompute it, use this exact alphabet** - "base62" does not pin one, and the conventional
*digit-first* ordering produces a completely different string, so a mismatch would look like corrupt data
rather than a wrong guess:

```python
A = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"   # lowercase first
v = int.from_bytes(hashlib.sha256(event_id.encode("utf-8")).digest(), "big")
nonce = "".join(A[(v // 62**i) % 62] for i in range(11))               # least-significant digit first
```

Deriving it rather than minting a random one per emission is the whole point: it inherits the `event_id`
semantics above exactly. **The same message re-delivered carries the same nonce**, so a restart or a
state-loss re-delivery is recognisable as the same work rather than looking like a second wake that never
arrived. Signals, which already get per-emission ids, get per-emission nonces.

Two things to know if you build on it:

- **It is a label, not a secret.** Deterministic, therefore guessable from the `event_id`. Do not treat its
  presence as proof of anything's authenticity.
- **It identifies a wake, not a delivery.** Two panes handed the same message carry the same nonce - correctly,
  it is the same work. Key your records on `(nonce, session_id)`, not the nonce alone.

### Emission timestamps

Every event carries `emitted` with `wall`, `monotonic`, `boottime` and `src`, all read at the same instant.

Three clocks, because one cannot tell you what you need: wall time is comparable to everything else but *steps*
when NTP or a hypervisor corrects it, so a difference of two wall stamps is not an elapsed time. Monotonic never
steps - but it *stops while the machine is not executing*. Subtracting one from the other across two events
gives you the time the machine was not running, which is how you tell "this sat in a queue for hours" apart from
"the host was suspended". On a VM those look identical in wall time and are completely different problems.

**The keys name what the clock MEANS, not what your OS calls it** - and on macOS those disagree:

| key | meaning | Linux | macOS |
|---|---|---|---|
| `monotonic` | does **not** tick while the machine is asleep/paused | `CLOCK_MONOTONIC` | `CLOCK_UPTIME_RAW` |
| `boottime` | **does** tick while the machine is asleep/paused | `CLOCK_BOOTTIME` | `CLOCK_MONOTONIC` |

macOS `CLOCK_MONOTONIC` *includes* sleep - it means what Linux calls `CLOCK_BOOTTIME` - and macOS has no
`CLOCK_BOOTTIME` at all. Reading the constant by name there gives you the wrong quantity under the right label,
and nothing complains. `src` tells you which constant actually supplied each value, so you can check rather than
assume:

```json
"emitted": {"wall":"...","monotonic":1404080.74,"boottime":1469598.35,
            "src":{"monotonic":"CLOCK_UPTIME_RAW","boottime":"CLOCK_MONOTONIC"}}
```

`boottime - monotonic` is time the machine was not executing (18.20 h on the Mac above). A key is omitted rather
than faked if its meaning is genuinely unavailable - a made-up value would be indistinguishable from a real
zero.

(`ts` is unchanged and still the event's own timestamp; `emitted.wall` is the reading coherent with the other
two clocks.)

`<run>` is random per process, and it is the part that matters: a bare counter would restart at 1 and hand ids a
consumer has already seen to brand-new events, making it drop live mail - a worse failure than the duplicate it
was meant to prevent. Ids are unique for all time; treat them as opaque strings.

### Bounded windows

The inbox endpoint returns the **newest** messages that fit a count limit *and* an aggregate content budget,
and reports what it left out (`truncated`, `size_truncated`, `size_dropped`). A watcher that ignores those
fields and advances its cursor to the highest id it saw will step over anything the server omitted, forever.

This watcher reads the declaration. When the returned window reaches back past its cursor, the omitted
messages are older than everything it still owes you, so it proceeds normally - the ordinary case, since
long-polling keeps the backlog small. When the window *starts above* the cursor and the server admits it
dropped messages, the gap may contain mail you have never seen: the watcher walks that span backward with
`before_id` until it reaches the cursor or the chain ends, and only then advances. A walk that cannot
complete leaves the cursor **pinned** and raises an `alert`, rather than advancing in silence.

Coverage comes from exhausting the chain, not from counting recovered messages - `truncated` says rows were
withheld without saying how many, so no count can prove a span empty. The backward walk also reaches mail
someone has already **read**, which an unread-only reconcile cannot see.

If you page the inbox API yourself: pass the **oldest** id you were returned as `before_id` and repeat until
`next_before_id` is null; omit the parameter for the newest page, since `0` is a real cursor rather than "no
cursor". A backward walk only covers what is older than where it began, so re-poll the newest page
afterwards. And order by **`id`**, not `created` - two messages can carry timestamps in the opposite order
from their ids.

### Stranded mail

The watcher also alarms on mail sitting in an inbox that is **not a known persona** - an inbox that is
*receiving* while nothing consumes it. Such mail is undeliverable and nothing else reports it: the sender gets a
success and a message id, the recipient gets no signal, and there is no bounce. Two real cases prompted this: a
case-variant of a live persona (`Claude-chat` vs `claude-chat`), whose reply sat unread for fourteen days; and a
group-looking name (`all`) that has no broadcast semantics behind it, which swallowed an announcement meant for
everyone.

An inbox counts as stranded when it holds mail and **either** the persona directory (`/api/personas`) does
not list it, **or** it owns zero memories - nothing has ever written as that persona, so nobody is working
under it. The second test matters because a directory built as a union of registered *recipients* lists
every typo the moment someone sends to it, which would make the first test unable to fire. Both signals
come from endpoints the watcher already fetches, so the check costs no extra request. It is reported once per
inbox per process - to stderr, and as one `alert` summarising the whole backlog:

```json
{"event": "alert", "source": "kijito-inbox", "persona": "you",
 "reason": "stranded-mail: 1 inbox(es) receiving mail nobody watches: Claude-chat (1 unread; case-variant of known persona 'claude-chat')",
 "stranded_inboxes": ["Claude-chat"]}
```

Three details matter if you consume these:

- It is an **`alert`, not a new event name**, so a consumer already filtering `new|alert|recovered` surfaces it
  without being rearmed. If you parse `alert` strictly, note it carries an extra `stranded_inboxes` field and **no
  message id**. On `--exec` the same list arrives comma-separated as `$KIJITOMON_STRANDED`, and
  `$KIJITOMON_FAILURES` is absent (this alert is not a reachability failure).
- It is delivered **only to real directory personas**. A stranded inbox has mail, so the watcher gives it a target
  and a stream of its own - alerting every target would write the alarm into the very stream nobody reads.
- It **clears when the mail is consumed**, not when someone acknowledges it. There is no ack, by design: an ack
  lets you silence the flag while the mail stays unread, which is how dead-letter queues rot.

Disable with `--no-stranded-alerts` if you keep deliberate test inboxes. Prefer draining them instead - an alarm
you have trained everyone to ignore is one that has been disabled without anyone deciding to disable it. The flag
silences **only this alarm**; [escalated mail nobody is answering](#escalated-mail-nobody-is-answering) has its own
`--no-urgent-alerts`, so following this advice cannot quietly turn off the higher-severity one.

### Escalated mail nobody is answering

The watcher alarms when a member holds mail a **sender marked urgent** and no activity from that member has
been observed:

```json
{"event": "alert", "source": "kijito-inbox", "persona": "you",
 "reason": "urgent-unanswered: 1 member(s) hold mail a sender marked URGENT while no activity from them has been observed: loom (1 urgent unread; last observed message 2026-07-24T23:24:43Z). OBSERVATION, NOT A DIAGNOSIS: ...",
 "urgent_unanswered": ["loom"]}
```

"Is this member stuck?" is normally unanswerable from outside, because idle-by-design and wedged look
identical - so the obvious version of this alarm fires on every quiet persona and becomes noise you learn to
ignore. The urgent flag is what makes it tractable: it is a **sender declaring an expectation**, and silence
only means something once something was expected. A quiet member with no urgent mail never trips it.

Both halves must be positive. If the watcher was not running for the span in question it reports nothing
rather than calling that silence. The alarm clears itself when **either** half clears - the mail is read, or
the member does something - and there is no ack, because an ack would let you silence "nobody is answering
escalated mail" while it stayed true.

It is deliberately kept separate from the stranded-mail alarm above: that one is for inboxes nobody owns,
this one for real members who are not responding. **They have separate flags for the same reason** - disable
this one with `--no-urgent-alerts`. Silencing the low-severity alarm about unowned inboxes must not also
silence the higher-severity one about real members, so `--no-stranded-alerts` does not touch it. To turn off
both, pass both.

### Unread mail outside the window

Alongside the declaration of *what it dropped*, the inbox endpoint reports `unread_not_shown`: how many
unread messages it holds that this response did not hand you. The watcher raises an `alert` when that count
is above zero.

```json
{"event": "alert", "source": "kijito-inbox", "persona": "you",
 "reason": "unread-not-shown: the server reports 3 unread message(s) in this inbox that this window did not include. ...",
 "unread_not_shown": 3, "window_floor": 1204, "cursor_at": 1180, "above_watermark": true}
```

It is an **observation, not a diagnosis**, and worth reading literally. The count covers unread mail
*anywhere* in the inbox - including messages this watcher already delivered to your stream that you simply
have not read - so on its own it is not evidence that anything was missed. `above_watermark` is the fact
that separates the two cases: when it is `false`, the window reached back past the watcher's cursor, so
everything above the cursor was visible and the unseen unread can only be mail already delivered. Coverage
of an un-emitted span is still established by the backward walk above; this count is a cheap signal, never
proof. Like the stranded alarm it clears itself when the condition goes away, and it has no ack.

**If you page the API yourself, do not reuse this field as a general "is anything hidden" check.** The
server computes it only when it withheld something, so it is `0` **by construction** on a page with nothing
older - and the last page of a backward walk is exactly that page. Measured against a live inbox holding
four unread messages: the newest page reported `0` (correctly - all four were in it), a mid-walk page
reported `4` (the whole inbox's unread, not that window's), and the terminal page reported `0` while all
four sat above it. Only the newest page's count answers the question "is there unread mail I cannot see".

## CLI

| flag | meaning |
|------|---------|
| `--persona P` | Watch this persona's inbox; repeat for an explicit subset. Omit to watch your whole account. |
| `--personas A,B` | Comma-separated persona list. |
| `--all-personas` | Explicitly watch every persona in your account (the default when no persona is given). |
| `--emit stdout-jsonl\|exec-per-event` | Output mode (default `stdout-jsonl`). |
| `--exec 'CMD'` | Command to run per event (required with `--emit exec-per-event`); fields arrive as `KIJITOMON_*`. Runs synchronously, 10s timeout. |
| `--events-file PATH` | Write NDJSON events to an owned, size-rotated file (survives rotation) instead of stdout. Consumers `tail -F` it. |
| `--events-file-template PATH` | Per-persona owned, rotated files, e.g. `events.{persona}.ndjson`; each session consumes its own. Must contain `{persona}`. Mutually exclusive with `--events-file`. |
| `--state-file PATH` | Persist and resume cursor/liveness; single-writer locked. Persona targets derive one file per persona. Recommended under a supervisor. |
| `--wait N` | Long-poll hold (s) requested from the server so new mail wakes the watcher near-instantly at ~the same request rate (default 50; `0` disables). Falls back to interval polling against a server that doesn't support it, and auto-upgrades when it does. |
| `--poll-seconds N` | Interval between polls when long-poll is off/unsupported (default 60). |
| `--alert-after N` | Consecutive failures before an `alert` (default 3, min 1). A single transient failure is normal. |
| `--heartbeat N` | Emit a `heartbeat` every N seconds (external dead-man's switch). |
| `--content-chars N` / `--no-content` | Truncate (default 220) or omit message content. |
| `--suppress-author P` | Don't emit `new` events authored by persona P (repeatable); drops self-echo when watching all personas. |
| `--max-bytes N` / `--keep-logs N` | Rotate event files at N bytes (default 5000000; `<=0` disables) keeping N archives (default 5). |
| `--seed-at ID` / `--max-replay N` | Seed the cursor at a last-handled id (single persona) / cap a re-arm backlog before fast-forwarding (default 50). |
| `--rediscover-every N` | In all-persona mode, re-scan for new personas every N seconds (default 600). |
| `--no-stranded-alerts` | Don't alarm on mail sitting in an inbox that isn't a known persona (see [Stranded mail](#stranded-mail)). On by default, because such mail is undeliverable and nothing else reports it. Silences only this alarm. |
| `--no-urgent-alerts` | Don't alarm on escalated (urgent) mail a known member isn't answering (see [Escalated mail nobody is answering](#escalated-mail-nobody-is-answering)). On by default. Separate from `--no-stranded-alerts` on purpose - a flag for unowned test inboxes must not silence an alarm about real members. |
| `--auth-header NAME` / `--token-file PATH` | Auth header name (default `Authorization: Bearer`) / token file. Token also via `$KIJITOMON_TOKEN`. A token is required. |
| `--no-fast-path` | Disable the `/api/notify/pending` pre-check; always full-poll the inbox list. |
| `--resync-every N` | Fast-path safety floor: force a full inbox poll after at most N cheap skips (default 10), so a stale unread count can never blind the watcher. |
| `--self-test` | Probe the source and do a synthetic emit (fires `--exec` too), then exit. Run it before trusting a live arm. |

## Design

Full spec, robustness contract, and DONE-WHEN criteria: [`docs/DESIGN.md`](docs/DESIGN.md). Published as Kijito
Inbox Monitor (package `kijito-inbox-monitor`).

## License

Apache License 2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE). Copyright 2026 Arcada Labs.
