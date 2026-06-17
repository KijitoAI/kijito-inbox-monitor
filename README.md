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
# never trust a monitor you haven't watched emit — run the self-test first:
python3 kijito_monitor.py --self-test --persona argus

# watch the argus inbox; one NDJSON event per new message on stdout:
python3 kijito_monitor.py --persona argus

# run a command per event instead (fields arrive as KIJITOMON_* env vars):
python3 kijito_monitor.py --persona argus --emit exec-per-event --exec 'notify-send "$KIJITOMON_FROM"'
```

`{"event":"armed",...}` is emitted once on the first healthy poll; then `{"event":"new","id":...,"from":...}` per
new message. Liveness events: `alert` (source unreachable for N consecutive polls), `recovered`, optional
`heartbeat`.

## Running it for real (supervision)

A watcher cannot report its own death. Run it under a process supervisor that restarts it (launchd / systemd /
cron `KeepAlive`), and give it a **`--state-file`** so an automatic restart resumes the cursor *and* the
liveness state with no missed or re-emitted messages:

```sh
python3 kijito_monitor.py --persona argus --state-file ~/.cache/kijito-monitor/argus.json
```

The state-file is single-writer locked (a second instance exits non-zero) and identity-stamped (it refuses to
resume a different inbox's cursor). Without a state-file, run a single instance and use `--heartbeat N` to wire an
external dead-man's-switch (e.g. healthchecks.io / Dead Man's Snitch).

## CLI

| flag | meaning |
|------|---------|
| `--persona P` | Kijito persona whose inbox to watch (required unless `--url`). |
| `--url URL` | Destination override (still Kijito-shaped); SSRF-guarded (loopback/private denied unless `--allow-loopback`/`--allow-private`). |
| `--poll-seconds N` | Poll interval (default 60). |
| `--alert-after N` | Consecutive failures before an `alert` (default 3, min 1). A single transient failure is normal. |
| `--emit stdout-jsonl\|exec-per-event` | Output mode (default `stdout-jsonl`). |
| `--exec 'CMD'` | Command per event (required iff `--emit exec-per-event`). Fields → `KIJITOMON_*` env vars. |
| `--content-chars N` / `--no-content` | Truncate (default 220) or omit message content. |
| `--seed-at ID` | Seed the cursor at a last-handled id (overrides a state-file cursor). |
| `--max-replay N` | Cap on a re-arm backlog before fast-forwarding (default 50). |
| `--state-file PATH` | Persist + resume cursor/FSM; single-writer locked. Recommended under a supervisor. |
| `--heartbeat N` | Emit a `heartbeat` every N seconds (external dead-man's-switch). |
| `--auth-header NAME` / `--token-file PATH` | Auth header name / token file. Token also via `$KIJITOMON_TOKEN`. The local daemon needs no token. |
| `--self-test` | Probe the source + synthetic emit, then exit. Run before trusting a live arm. |

## Design

Full spec, robustness contract, and the DONE-WHEN criteria: [`../docs/DESIGN.md`](../docs/DESIGN.md). The tool is
deliberately source- and harness-agnostic at the seams (generic `http-poll` core, `exec-per-event` as the portable
emit primitive) but ships Kijito as the reference source. The published package name is TBD.
