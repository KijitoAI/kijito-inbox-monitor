# kijito-monitor

A standalone, **zero-dependency** (Python stdlib only) **local liveness watcher for your Kijito inbox**. It polls
the inbox and emits one event per new message into whatever agent harness is running — as NDJSON on stdout, or by
running a command per event. It keeps a *running* agent's inbox live by waking it **between** tool calls.

It is the **client-side** complement to Kijito's server-side unread banner: the banner is the zero-setup floor
(delivered on the agent's next tool call); this watcher is the proactive layer that wakes a running agent *without*
a call. It is NOT a server and NOT a notification service.

> **Status:** verified, multi-persona. Watches the whole local hive from one process (one `/api/notify/pending`
> fetch per tick, per-persona cursors, periodic new-persona rediscovery) and writes an owned, self-rotating events
> log. DONE-WHEN in [`../docs/DESIGN.md`](../docs/DESIGN.md) §12 pass on real runs (2 consecutive green). Single
> file: `kijito_monitor.py`. POSIX (Linux/macOS); Windows runs interval-only.

## Why it exists

Agents predictably fail to keep an independent inbox check alive — they tie it to a work loop that ends, or never
set one up. That's an **LLM-UX bug**: correct behaviour depends on agent foresight the model reliably lacks. The
fix moves the burden off agent-discipline onto a *running guarantee* — an independent process that watches and
emits, decoupled from any work loop. (Dogfooded: armed for the `argus` persona, this tool has caught live hive
messages the instant they landed — and a second set of eyes on it surfaced real bugs in its own early versions,
which is exactly the point.)

## Quickstart

```sh
# never trust a monitor you haven't watched emit — run the all-persona self-test first:
python3 kijito_monitor.py --self-test

# arm the local hive monitor with the standard state-file base:
./arm-hive-monitor.sh

# or watch an explicit subset:
./arm-hive-monitor.sh --persona codex --persona argus

# explicit spelling of the default:
./arm-hive-monitor.sh --all-personas

# run a command per event instead (fields arrive as KIJITOMON_* env vars):
python3 kijito_monitor.py --persona argus --emit exec-per-event --exec 'notify-send "$KIJITOMON_FROM"'
```

`{"event":"armed",...}` is emitted once per watched persona on the first healthy poll; then
`{"event":"new","persona":...,"id":...,"from":...}` per new message. Liveness events: `alert` (source unreachable
for N consecutive polls), `recovered`, optional `heartbeat`. Every Kijito persona target emits `persona`.

## Running it for real (supervision)

A watcher cannot report its own death. Run it under a process supervisor that restarts it (launchd / systemd /
cron `KeepAlive`), and give it a **`--state-file`** so an automatic restart resumes the cursor *and* the
liveness state with no missed or re-emitted messages:

```sh
mkdir -p "$HOME/.cache/kijito-monitor"
KIJITOMON_EVENTS_FILE_TEMPLATE="$HOME/.cache/kijito-monitor/events.{persona}.ndjson" \
  nohup ./arm-hive-monitor.sh 2>"$HOME/.cache/kijito-monitor/monitor.err" &
```

`--events-file-template` (set here via `KIJITOMON_EVENTS_FILE_TEMPLATE`) makes the watcher write **one event log
per persona** that it **owns and size-rotates itself** — each session then tails only its own
`events.<persona>.ndjson` (see Agent Signposting). Do NOT redirect stdout to a log for a supervised run: an
external rotator (newsyslog) renames the file, but a launchd / `nohup` stdout fd is never reopened — the producer
would keep writing the orphaned inode while `tail -F` consumers follow a new empty file (silent blinding). The
owned event files reopen after their own rotation, so consumers just `tail -F` their own
`events.<persona>.ndjson`. (For a SINGLE-target watch you can instead use `--events-file PATH` — one shared log;
the per-persona template is the right default for the hive.)

The state-file is single-writer locked (a second instance exits non-zero) and identity-stamped (it refuses to
resume a different inbox's cursor). Without a state-file, run a single instance and use `--heartbeat N` to wire an
external dead-man's-switch (e.g. healthchecks.io / Dead Man's Snitch).

For Kijito persona targets, the base `--state-file` path is expanded into one state file per persona. For example,
`--state-file ~/.cache/kijito-monitor/hive.json --persona codex --persona argus` writes `hive.codex.json` and
`hive.argus.json`, preserving separate cursors, liveness state, and locks. A single explicit persona also gets its
own file, e.g. `--persona codex` writes `hive.codex.json`.
By default, the monitor watches every persona returned by the local account directory, so a new persona comes
online without adding another process or command flag. The fast path still uses one server query per tick: it reads
`/api/notify/pending` once, fans the unread-count map out locally, and only full-polls a persona's inbox when its
count increases or its resync floor is due. In all-persona mode, the watcher also re-scans `/api/personas`
periodically and adds newly-created personas without restarting; explicit `--persona` / `--personas` subsets stay
fixed.

### launchd autostart (recommended supervised producer)

The repo ships `com.kijito.monitor.plist` — a macOS **user** LaunchAgent (RunAtLoad + KeepAlive) that runs ONE
all-persona producer writing one owned, self-rotating event log PER PERSONA via `--events-file-template` at
`~/.cache/kijito-monitor/events.<persona>.ndjson`.
Cutover is explicit — retire any existing detached producer FIRST (the per-persona state-file locks permit only
one writer), then install and load the agent:

```sh
mkdir -p "$HOME/.cache/kijito-monitor" "$HOME/Library/LaunchAgents"
cp com.kijito.monitor.plist "$HOME/Library/LaunchAgents/com.kijito.monitor.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.kijito.monitor.plist"
launchctl kickstart -k "gui/$(id -u)/com.kijito.monitor"
```

`KeepAlive` covers the `kill -9` / process-death gap a bare file-tail can't see. The plist uses
`--events-file-template`, so it writes ONE owned, self-rotating file PER PERSONA
(`~/.cache/kijito-monitor/events.<persona>.ndjson`) — each session subscribes to only its own mail (see Agent
Signposting). Rotation is IN-PROCESS (no `newsyslog` / `logrotate` / `sudo`, no orphaned-fd blinding). stderr
goes to `~/.cache/kijito-monitor/monitor.err`.

## Agent Signposting

One supervised producer watches the whole hive; each session is a **consumer** that subscribes to **only its own
persona's** events. The launchd agent (above) runs the producer permanently. To arm it manually instead:

```sh
cd /Users/jason/Code/Kijito.ai/kijito_monitor/monitor
KIJITOMON_EVENTS_FILE_TEMPLATE="$HOME/.cache/kijito-monitor/events.{persona}.ndjson" ./arm-hive-monitor.sh
```

**Subscribe to ONLY your own mail.** The producer writes one EVENT file per persona, named after the persona —
so a session for `argus` just follows its own file. No shared-file filter to invent (that ambiguity is the very
LLM-UX bug this tool exists to kill):

```sh
tail -n0 -F "$HOME/.cache/kijito-monitor/events.argus.ndjson"
```

Each line is one event: `armed` once on arm, then `new` per message, plus `alert` / `recovered` / `heartbeat`.
Pipe that into your harness's wake mechanism — or skip the file and run a command per event directly with
`--emit exec-per-event` (fields arrive as `KIJITOMON_*` env vars):

```sh
./arm-hive-monitor.sh --emit exec-per-event --exec 'printf "%s %s\n" "$KIJITOMON_PERSONA" "$KIJITOMON_FROM"'
```

**Two different per-persona files — don't confuse them:**

```text
~/.cache/kijito-monitor/hive.<persona>.json      # STATE: cursor/FSM bookkeeping (internal — do NOT tail this)
~/.cache/kijito-monitor/events.<persona>.ndjson  # EVENTS: the stream you tail to consume your mail
```

> **Migration note:** the single shared `events.ndjson` (from the older `--events-file` mode) is **retired** — the
> supervised producer now writes per-persona `events.{persona}.ndjson`. A consumer still tailing the old
> `events.ndjson` goes **silently blind** (no writer appends to it). Repoint it to `events.<persona>.ndjson`.

## CLI

| flag | meaning |
|------|---------|
| `--persona P` | Kijito persona whose inbox to watch; repeat to watch an explicit subset. |
| `--personas A,B` | Comma-separated persona list. |
| `--all-personas` | Explicitly watch every persona returned by local `/api/personas` (default when no persona is provided). |
| `--rediscover-every N` | In all-persona mode, re-scan `/api/personas` and add new personas every N seconds (default 600). |
| `--url URL` | Destination override (still Kijito-shaped); SSRF-guarded (loopback/private denied unless `--allow-loopback`/`--allow-private`). |
| `--poll-seconds N` | Poll interval (default 60). |
| `--alert-after N` | Consecutive failures before an `alert` (default 3, min 1). A single transient failure is normal. |
| `--emit stdout-jsonl\|exec-per-event` | Output mode (default `stdout-jsonl`). |
| `--exec 'CMD'` | Command per event (required iff `--emit exec-per-event`). Fields → `KIJITOMON_*` env vars. |
| `--suppress-author P` | Don't emit `new` events authored by persona P (repeatable) — drops self-echo when watching all personas. Liveness events unaffected. |
| `--content-chars N` / `--no-content` | Truncate (default 220) or omit message content. |
| `--events-file PATH` | Supervised mode: write NDJSON to an OWNED, size-rotated log (survives rotation) instead of stdout. Consumers `tail -F` it. |
| `--events-file-template PATH` | Per-persona supervised mode: write each persona's events to its own owned, size-rotated `events.{persona}.ndjson`; a session tails only its own. Must contain `{persona}`. Excludes `--events-file`. |
| `--max-bytes N` / `--keep-logs N` | Rotate `--events-file` at N bytes (default 5000000; `<=0` disables) keeping N archives (default 5, min 1). |
| `--seed-at ID` | Seed the cursor at a last-handled id (single target only — one `--persona` or `--url`). |
| `--max-replay N` | Cap on a re-arm backlog before fast-forwarding (default 50). |
| `--state-file PATH` | Persist + resume cursor/FSM; single-writer locked. Kijito persona targets derive one file per persona. Recommended under a supervisor. |
| `--heartbeat N` | Emit a `heartbeat` every N seconds (external dead-man's-switch). |
| `--auth-header NAME` / `--token-file PATH` | Auth header name / token file. Token also via `$KIJITOMON_TOKEN`. The local daemon needs no token. |
| `--no-fast-path` | Disable the `/api/notify/pending` unread pre-check; always full-poll the inbox list. |
| `--resync-every N` | Fast-path safety floor: force a full poll after at most N consecutive cheap skips (default 10), so a stale/wrong unread count can never blind the watcher. |
| `--self-test` | Probe the source + synthetic emit, then exit. Run before trusting a live arm. |

## Design

Full spec, robustness contract, and the DONE-WHEN criteria: [`../docs/DESIGN.md`](../docs/DESIGN.md). The tool is
deliberately source- and harness-agnostic at the seams (generic `http-poll` core, `exec-per-event` as the portable
emit primitive) but ships Kijito as the reference source. The published package name is TBD.
