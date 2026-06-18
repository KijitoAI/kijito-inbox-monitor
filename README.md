# kijito-monitor

A standalone, **zero-dependency** (Python stdlib only) **local liveness watcher for your Kijito inbox**. It polls
the inbox and emits one event per new message into whatever agent harness is running — as NDJSON on stdout, or by
running a command per event. It keeps a *running* agent's inbox live by waking it **between** tool calls.

It is the **client-side** complement to Kijito's server-side unread banner: the banner is the zero-setup floor
(delivered on the agent's next tool call); this watcher is the proactive layer that wakes a running agent *without*
a call. It is NOT a server and NOT a notification service.

> **Status:** v1, verified. All DONE-WHEN criteria in [`../docs/DESIGN.md`](../docs/DESIGN.md) §12 pass on real
> runs (2 consecutive green). Single file: `kijito_monitor.py`. POSIX (Linux/macOS); Windows runs interval-only.

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
nohup ./arm-hive-monitor.sh \
  >"$HOME/.cache/kijito-monitor/events.ndjson" \
  2>"$HOME/.cache/kijito-monitor/monitor.err" &
```

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

### launchd autostart

The repository includes `com.kijito.monitor.plist` for the local macOS user agent. Cutover should be explicit:
stop any existing detached producer first, then install and load the agent.

```sh
mkdir -p "$HOME/.cache/kijito-monitor" "$HOME/Library/LaunchAgents"
cp com.kijito.monitor.plist "$HOME/Library/LaunchAgents/com.kijito.monitor.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.kijito.monitor.plist"
launchctl kickstart -k "gui/$(id -u)/com.kijito.monitor"
```

The plist writes NDJSON events to `~/.cache/kijito-monitor/events.ndjson`, stderr to
`~/.cache/kijito-monitor/monitor.err`, and keeps the monitor alive with launchd restart semantics. The companion
`com.kijito.monitor.newsyslog.conf` is the native log-rotation template for `/etc/newsyslog.d/`:

```sh
sudo cp com.kijito.monitor.newsyslog.conf /etc/newsyslog.d/com.kijito.monitor.conf
```

## Agent Signposting

At the start of an agent session, the operator only needs to arm one monitor for the local hive:

```sh
cd /Users/jason/Code/Kijito.ai/kijito_monitor/monitor
./arm-hive-monitor.sh
```

The wrapper uses `~/.cache/kijito-monitor/hive.json` as the state-file base. That base path becomes one file per
persona, for example:

```text
~/.cache/kijito-monitor/hive.codex.json
~/.cache/kijito-monitor/hive.river.json
~/.cache/kijito-monitor/hive.ladybug.json
~/.cache/kijito-monitor/hive.argus.json
```

Every realtime event includes `persona`, so a harness, log tail, or notification bridge can route it back to the
right agent context. To watch the live event stream directly:

```sh
./arm-hive-monitor.sh --heartbeat 300
```

To send events into another command instead of stdout:

```sh
./arm-hive-monitor.sh --emit exec-per-event --exec 'printf "%s %s\n" "$KIJITOMON_PERSONA" "$KIJITOMON_FROM"'
```

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
| `--content-chars N` / `--no-content` | Truncate (default 220) or omit message content. |
| `--seed-at ID` | Seed the cursor at a last-handled id (overrides a state-file cursor). |
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
