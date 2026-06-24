# Kijito Inbox Monitor: Design & Implementation Spec

**Updated:** 2026-06-20 (rev 6, v2 multi-persona + supervised producer shipped and deployed;
see §14). **Status:** shipped and live (v2 under launchd).

**Goal:** give Kijito a solid, usable local liveness watcher for its built-in inbox. The concrete
Kijito-inbox monitor is the win. Agnosticism is a means (generalize only where it makes the tool more
useful), not the end. Learn from prior art, and don't gold-plate.

§1 through §13 are the v1 single-persona core, still accurate and load-bearing (one process watches one
inbox; the cursor/dedup/FSM/self-test/state/SSRF/seam contracts apply per-persona unchanged). §14 records
the v2 deltas: the deployed build watches the whole local hive from one supervised process and writes one
owned, self-rotating event log per persona. Read §14 alongside §1, §11, and §12 for current reality.

---

## 1. What it is

A standalone, single zero-dependency Python-stdlib script (urllib, json, signal, select, fcntl, subprocess;
no pip installs) that polls the Kijito inbox and emits one event per new message into whatever harness is
running, as NDJSON on stdout and/or exec-a-command-per-event. It is the client-side liveness watcher: it
keeps a running agent's inbox live by waking it between tool calls. It is not a server, and not a
notification service. POSIX target (Linux/macOS); Windows runs interval-only (no SIGUSR1 seam, no flock,
per §10/§7.3).

## 2. The problem

The "inbox-liveness" LLM-UX bug: agents predictably fail to keep an independent inbox check alive. They tie
it to a work loop that ends, or never set one up. The fix is to move the burden off agent-discipline and
onto a running guarantee: an independent process that watches and emits, decoupled from any work loop.
(Dogfooded; adversarial review surfaced real bugs in its own early versions, which are folded into this rev.)

## 3. The composition contract (locked with the server side)

This is the client half of Kijito's server-side inbox-liveness system. Two complementary layers:

| Layer | What | Where | Guarantee |
|-------|------|-------|-----------|
| **Banner** (server) | unread banner in every Kijito tool response | server-side, every client | zero-setup floor; delivery-on-next-call |
| **Watcher** (this) | independent process polls and emits per-new-item | client-side, harnesses that run a process | proactive; wake-without-a-call |

- One shared signal source (the `control_plane` urgent counter) so liveness never diverges; the watcher
  consumes a server count over HTTP (§9) and never reimplements liveness.
- v1 is a pure local poller plus the opaque-wake seam (§10), so a hosted bridge can later push wake-then-pull.
- Marketplace: the goal is to surface it as "the local liveness watcher for your Kijito inbox."

## 4. Architecture

`SOURCE adapter (http-poll) → GENERIC CORE (cursor/dedup/alert-FSM/self-test/state/wake-seam) → EMIT (stdout-jsonl | exec-per-event)`.
v1 ships one adapter (`http-poll`, the Kijito reference). Future adapters are explicitly deferred.

## 5. The `http-poll` adapter: Kijito inbox contract (code-verified, audited 2026-06-17)

- **Endpoint:** `GET /api/inbox?persona=<P>&mark_read=false`
- **Response:** `{"result": [ {"id":<int>,"from":"<persona>","content":"<plaintext>","created":"<iso-str>","read":<bool>}, ... ]}`
  (keys verbatim, in that order, per messaging.py:85-89). v1 hard-bakes this Kijito response shape (there is no
  generic parse config; that's deferred, see §scope). `--url` overrides only the destination, not the parse contract.
- **`mark_read` defaults to `true`** (web_api.py:504; SET m.read=true at messaging.py:90-96). The URL must carry
  `&mark_read=false`. A watcher must peek, never consume: every fetch site (the poll loop and `--self-test`) uses
  the `mark_read=false` URL. (Triple-confirmed; the original seed was fixed for this.)
- **`id` is a SERIAL PK, so it is strictly monotonic** (schema.py:168), with gaps allowed. The cursor keys on
  max-id, never on read/unread state.
- **No pagination:** the response is a full list (seen at 148K/539 msgs). Emit only the diff (id > cursor); never
  dump the body.
- **Auth:** the local daemon needs no token (loopback trust; empirically the watcher polls tokenless). If
  `$KIJITOMON_TOKEN`/`--token-file` is set, inject it as `Authorization: Bearer <token>`, or with `--auth-header NAME`
  as `NAME: <token>` verbatim. The header name (`--auth-header`) and the token-value source (`--token-file` wins over
  env) are independent axes. An unreadable `--token-file` is a fatal config error.
- **A poll is healthy iff** HTTP 2xx, and the body parses, and the envelope is shape-valid (`result` is a list; every
  row is an object with an integer `id`). Anything else (non-2xx, connection-refused, DNS failure, connection-reset,
  timeout, parse-fail, shape-violation) is a liveness failure (UNKNOWN), never "no mail." (A 200 with a
  truncated-but-parseable body that fails the shape check is a failure.)
- **Empty `{"result":[]}`** is healthy, with no new items.
- **Hive-off / 404 timing matters:** detected at startup or `--self-test`, it is a fatal config error (exit
  non-zero). Appearing mid-run, it is a per-poll liveness failure (the daemon may have restarted or the hive toggled
  transiently); it feeds the §7.1 FSM and does not kill the process (a transient server blip must not destroy the
  dead-man's-switch).
- **Config:** `url` (destination override), `poll_seconds` (default 60). The reference config hard-bakes the URL
  including `mark_read=false`.

## 6. Emit modes (portability)

"NDJSON-on-stdout is universal" is false on ingestion: Claude Code ingests per-event (hooks: JSON-on-stdin,
exit-code, `additionalContext`; plus FileChanged); LangGraph/OpenAI-Agents/Cursor are in-process (no stdin/stdout
event ingestion). So `exec-per-event` is the more portable primitive; `stdout-jsonl` is the ergonomic default.

### 6.1 Event schema (stdout-jsonl)

One object per line; every event carries `event`, `source`, `ts` (emit-time UTC ISO).
```
{"event":"new",         "source":"kijito-inbox","ts":"<iso>","id":246,"from":"river","content":"<≤N or omitted>","created":"<iso>"}
{"event":"armed",       "source":"kijito-inbox","ts":"<iso>","cursor":250}
{"event":"alert",       "source":"kijito-inbox","ts":"<iso>","reason":"unreachable","consecutive_failures":3,"seconds":180}
{"event":"recovered",   "source":"kijito-inbox","ts":"<iso>","cursor":250}
{"event":"heartbeat",   "source":"kijito-inbox","ts":"<iso>","cursor":250}     # only if --heartbeat; cursor may be null
{"event":"seed_ahead",  "source":"kijito-inbox","ts":"<iso>","seeded":600,"current_max":539}      # seed > reality (§7.0)
{"event":"replay_capped","source":"kijito-inbox","ts":"<iso>","capped_to":539,"dropped":389}      # backlog > --max-replay (§7.0)
```
- `new` carries `id`, `from`, `content`, `created`. `content` is a silent hard cut to `--content-chars` (default 220),
  with no marker; or it is omitted with `--no-content`. `seconds` in `alert` is nominal (`consecutive_failures × poll_seconds`).
- **Within-poll emit order (deterministic, total):** `alert`/`recovered` (FSM edge), then `replay_capped`/`seed_ahead`,
  then `armed`, then `new` (ascending id), then `heartbeat`. So `armed`/`recovered` set `cursor` before any `new`/`heartbeat`
  in the same cycle, which means `recovered.cursor` is non-null whenever a baseline has occurred (a `recovered` on a poll
  that also baselines carries the just-set cursor).

### 6.2 `exec-per-event` (`--emit exec-per-event --exec 'CMD'`, `--exec` required iff this mode)

Every event invokes `CMD`; inapplicable env vars are unset:

| env var | new | armed | alert | recovered | heartbeat | seed_ahead | replay_capped |
|---|---|---|---|---|---|---|---|
| `KIJITOMON_EVENT`,`_SOURCE`,`_TS` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `KIJITOMON_ID`,`_FROM`,`_CONTENT`,`_CREATED` | ✓ | – | – | – | – | – | – |
| `KIJITOMON_CURSOR` | – | ✓ | – | ✓ | ✓ | – | – |
| `KIJITOMON_REASON`,`_FAILURES` | – | – | ✓ | – | – | – | – |
| `KIJITOMON_SEEDED`,`_CURRENT_MAX` | – | – | – | – | – | ✓ | – |
| `KIJITOMON_CAPPED_TO`,`_DROPPED` | – | – | – | – | – | – | ✓ |

The spawned command has a 10s timeout; a non-zero exit or timeout is logged to stderr and is non-fatal (and never
holds the cursor back, per §7.0).

## 7. Robustness contract

### 7.0 Cursor / dedup algorithm
- A "re-arm" is a cursor-initialization at startup (resume / seed / baseline). It is not a §7.1 FSM `recovered`
  (recovery resumes normal diffing with the cursor already tracking; the replay cap below never re-applies on
  recovery).
- **Cursor and FSM resolve independently at startup** (they are two separate resolutions, not one ladder):
  - **Cursor:** (1) explicit `--seed-at <id>` sets `cursor = id` (operator intent wins, overriding any state-file
    cursor); else (2) a valid identity-matching `--state-file` (§7.3) with an integer cursor sets `cursor =
    resumed value`; else (3) `cursor = UNSET`.
  - **FSM** (`state`, `consecutive_failures`): a valid identity-matching `--state-file` always supplies it,
    independent of `--seed-at` (so `--seed-at` plus a DOWN state-file resumes DOWN, preserving dead-man's-switch
    continuity); an absent/mismatched/invalid state-file means the FSM starts `UP`/`0`.
- **`armed` fires on the first healthy poll** (never before a fetch). There is exactly one `armed` per (re)arm,
  carrying the post-decision `cursor`. A failed first poll does not baseline (it's a §7.1 failure; `armed` waits).
- **Each healthy poll:** select items with `id > cursor`; sort ascending; emit one `new` per item; then advance
  `cursor` to the max id in the selected diff, unconditionally. The cursor advances on selection, not emit success.
  Emit is best-effort/at-most-once: an exec failure neither holds back nor re-emits. `cursor` is monotonic.
- **First-healthy-poll branches** (mutually exclusive; each emits exactly one `armed`):
  - **UNSET baseline** (cursor was UNSET, including a state-file resume whose cursor was `null`): `cursor = max(id)`
    (or 0 if empty); emit `armed`; no `new`, no cap (nothing to replay). This branch is exempt from the cap.
  - **Non-null re-arm** (cursor came from `--seed-at` or a non-null state-file resume): let `n = count(id > cursor)`,
    `current_max = max(ids) if result else 0`.
    - if `cursor > current_max` (seed/resume ahead of reality): emit `armed{cursor}` plus `seed_ahead{seeded=cursor,
      current_max}` (real ids ≤ cursor are intentionally skipped); no `new`.
    - elif `n > --max-replay`: fast-forward `cursor = current_max`; emit `armed{cursor}` plus `replay_capped{capped_to=
      current_max, dropped=n}`; no `new`.
    - else (`n ≤ --max-replay`): emit `armed{cursor}`, then replay all `n` as `new` (so exactly `--max-replay`
      replay at the boundary; `>` is the cut).

Seven lessons (mandatory): use a standalone file, not inline-shell; use a single long-lived process with per-line
flush; dedup/cursor by max-id plus peek; emit the diff, not the body; a parse/shape/HTTP failure is UNKNOWN, not
"no mail" (§5); re-arm via `--state-file` or `--seed-at-last-handled`; avoid auto-stop-on-volume (handled by dedup,
peek, edge-alerts, and the replay cap).

### 7.1 Liveness alert FSM (dead-man's-switch)
States are **UP** (default) and **DOWN**; `consecutive_failures` counts from 0. A "failure" is any non-healthy poll (§5).
- **Healthy poll:** set `consecutive_failures = 0`; if DOWN, go UP and emit one `recovered`.
- **Failure:** `consecutive_failures += 1`; the UP-to-DOWN edge is crossed when `consecutive_failures` first
  reaches `--alert-after` while state is UP. Then set DOWN and emit one `alert`. (The `state==UP` guard is what makes
  it edge-once; a resumed `state==DOWN` never re-crosses the edge, so there is no duplicate `alert`.)
- `alert`/`recovered` are per-edge: a run may alert, recover, then alert again. A sub-threshold blip emits neither.
- `--alert-after` has a minimum of 1 (0 is rejected) and a default of 3 (a single transient failure is normal,
  bouncing in ~1-2s). SIGUSR1-triggered polls participate identically.

### 7.2 `--self-test`: runs once and exits (no poll loop)
Requires `--persona` or `--url`. (a) One real peek-mode (`mark_read=false`) fetch, checked healthy per §5 (so
hive-off/404/unreachable correctly fails self-test); (b) a synthetic `new` through the real emit path (stdout: line
written and flushed is ok; exec: spawn `--exec` with a fake `new`, child exit 0 within timeout is ok). Exit 0 iff
both healthy and emit-ok; else non-zero. The fetch result is printed regardless. The probe is peek-mode, so it is
read-state-neutral (DONE-WHEN #5 holds after self-test).

### 7.3 State persistence (`--state-file PATH`, optional, recommended under a supervisor)
- **Content (JSON):** `{"identity":<canonical-id>, "cursor":<int|null>, "state":"UP|DOWN",
  "consecutive_failures":<int>}`.
- **Canonical identity (`<canonical-id>`)** is computed before DNS resolution so trivial URL variations don't flip
  it. From the effective inbox URL (the baked `--persona` reference form and an equivalent `--url` form must
  canonicalize equal), it is the tuple `(scheme.lower(), host.lower(), effective_port, path, sorted(query_params except
  the constant mark_read))`. Normalize by stripping a trailing `/` on path, filling the scheme's default port, sorting
  query params, and lowercasing host. (So `?persona=river&mark_read=false` ≡ `?mark_read=false&persona=river`, and
  `:7474` ≡ default-for-scheme, and the `--persona river` reference ≡ the equivalent explicit `--url`.) `persona` is
  encoded via the `persona=` query param already in the URL; do not add a separate persona field that the `--url` form
  lacks.
- **Single-writer lock:** on startup, acquire an exclusive `fcntl.flock` on the state-file (LOCK_EX|LOCK_NB); if it's
  held, exit non-zero ("state-file in use"), which prevents two watchers tearing the cursor backwards. Hold the lock fd
  open for the whole process lifetime. flock is advisory and auto-released by the OS on process exit
  (normal/SIGTERM/SIGKILL/crash), so there is no stale lockfile to clean (unlike a pidfile).
- **Write:** after each poll, write atomically: `mkstemp` in the same dir, then write, `fsync`, `os.replace`;
  best-effort remove of stale temps.
- **Resume validity:** valid iff it parses as the schema (integer-or-null `cursor`, `state ∈ {UP,DOWN}`, integer
  `consecutive_failures`) and `identity` equals the current canonical-id. On a valid match, resume per §7.0 (cursor
  unless `--seed-at` overrides; FSM always). On an identity mismatch, do not resume the cursor (it would yield a
  silently-blind watcher): log a loud warning, re-baseline the cursor as UNSET, and start the FSM fresh (UP/0). A
  parse/schema-invalid or empty file is treated as absent (fall through). A present-but-unreadable path is a fatal
  config error.
- **Without `--state-file`:** no lock and no persistence, so the FSM is per-process (a restart resets it), and two
  no-state watchers for the same inbox would both emit `new` (duplicate delivery; they don't corrupt read-state since
  both peek, but the harness is woken twice). Run a single instance, or use `--state-file` under a supervisor (which
  both locks and persists). The external dead-man's-switch (`--heartbeat` to healthchecks.io / Dead Man's Snitch) is
  then the cross-restart liveness guarantee, and DONE-WHEN #3's "no re-emit while down" scopes to a single process.

## 8. Security (SSRF + creds, lift, don't reinvent)

- **SSRF guard on a user-supplied `--url` only**; the baked Kijito loopback reference is exempt. Two independent
  rules: **(destination class)** default-deny private/loopback/link-local for a generic `--url`; opt in with
  `--allow-loopback`/`--allow-private`. Lift `_resolve_and_pin` (notify_channels.py:57-99, resolve all IPs, reject
  internal) plus `_PinnedHTTPSConnection` (:123-146, connect to pinned ip, no re-resolve so no TOCTOU). **(redirects)**
  Redirects are never followed, independent of the allow-flags (refused regardless), per `_safe_post` (def :166,
  ~:166-201, no-redirect opener plus timeout). Per-request timeout default is 5s. A user-`--url` SSRF violation
  (blocked dest or redirect) is a fatal startup error (exit non-zero), distinct from a per-poll liveness failure.
  Stdlib: no-redirect via `HTTPRedirectHandler.redirect_request → None`; IP-pin via custom `HTTPConnection` through
  `do_open`; `urlopen(timeout=)`.
- **Creds via env/file, never argv** (`$KIJITOMON_TOKEN` / `--token-file`; §5 for header and precedence).
- **Zero-knowledge:** local content is decrypted locally (no tension); any future hosted bridge carries an opaque
  wake only (`--no-content` supports opaque mode).

## 9. Signal strategy: the all-unread fast-path (implemented, server PR#66)

- **Baseline:** the inbox-list poll (§5) is always the floor and the source of truth. The max-id cursor decides
  what to emit, so the fast-path can never cause a missed or duplicate emit.
- **Fast-path (cheap O(1) pre-check):** `GET /api/notify/pending` (SLASH path; the hyphen `/api/notify-pending`
  404s), read-only, never marks read. Response `{"result":[{"persona","unread","unread_urgent"},...]}`; `unread` is
  all read=false for that persona (a persona with 0 unread is absent, treat as 0). The watcher probes it once on
  arm; if available it consumes `unread` for its persona and does the full inbox-list fetch only when `unread`
  increases, saving the full-list diff on quiet polls. It auto-falls-back to baseline if the endpoint is absent or
  non-2xx (a non-Kijito source, or a daemon without the field, simply runs baseline).
- **Safety floor (`--resync-every`, default 10):** the watcher never skips more than N consecutive polls; it
  forces a full inbox poll regardless. So a stale / wrong / unsupported count (e.g. a daemon running pre-PR#66 code
  that returns 0 forever) can at worst add latency, never blind the watcher. `unread` is only the wake trigger.
- `--no-fast-path` forces baseline (always full-poll). Note: a self-sent message does not bump your own `unread`
  (the daemon doesn't treat your own outgoing mail as unread-for-you), so the fast-path wakes you on incoming mail,
  which is the intended liveness behaviour.
- Build-time note (2026-06-17): the `unread` field is live on prod/the box, but the local :7474 daemon was running
  stale pre-PR#66 code (the source has it, web_api.py:601, but the running process is pending a restart). Verified:
  the fast-path mechanics on a mock, and the resync floor catching real mail on the stale local daemon.

## 10. Opaque-wake seam (build the hook, not the bridge)

An internal "poll now" trigger besides the interval, wired to SIGUSR1 (POSIX only). Mandatory race-free mechanics:
- **Install a no-op Python handler** `signal.signal(SIGUSR1, lambda *_: None)`. This is required, or the default
  disposition terminates the process and `set_wakeup_fd` writes nothing.
- **Self-pipe via a non-blocking `socketpair`** (more portable than `os.pipe` for `set_wakeup_fd`): set both ends
  non-blocking, `signal.set_wakeup_fd(w)`; the main loop blocks in `select.select([r],[],[],timeout)`. A signal at
  any instant either interrupts the in-progress `select` or leaves a byte that makes the next `select` return
  immediately, so no wakeup is lost.
- **Read-and-clear by draining the pipe** (`os.read(r, 4096)`) at the start of each poll (before fetch). Any
  SIGUSR1 after that drain, even during the same poll's fetch/emit, leaves a byte guaranteeing a subsequent poll.
  This gives "at most one extra poll per quiescent signal" with no signal lost once a poll has begun.
- **One polling site on the main loop; the handler does no work**, so re-entrancy is structurally impossible.
- v1 opens no remote listener. A later hosted bridge turns an opaque wake into a SIGUSR1/FIFO poke, then pull over
  the authenticated channel (the client-side consumer in Kijito's notify-then-pull matrix). Windows: interval-only.

## 11. CLI / config surface (v1)

```
kijito-inbox-monitor \
  [--persona P] \                  # required UNLESS --url given
  [--url URL] \                    # destination override (still Kijito-shaped); SSRF-guarded
  [--allow-loopback] [--allow-private] \              # generic --url only; default deny
  [--poll-seconds 60] [--alert-after 3] \             # --alert-after min 1
  [--emit stdout-jsonl|exec-per-event] [--exec 'CMD'] \ # --exec required iff emit=exec-per-event
  [--content-chars 220 | --no-content] \
  [--seed-at LAST_HANDLED_ID] [--max-replay 50] \
  [--state-file PATH] [--heartbeat SECONDS] \
  [--auth-header NAME] [--token-file PATH] \          # also $KIJITOMON_TOKEN
  [--self-test]
```
**Arg matrix:** `--url` absent means `--persona` required, the Kijito reference URL used. `--url` present overrides,
and `--persona` is ignored. It's an error (exit non-zero) if both are absent. An explicit `--seed-at` overrides a
state-file cursor.
**`--heartbeat SECONDS`:** emitted on the poll cycle (healthy or failed; it proves the watcher is alive) once at
least SECONDS have elapsed since process start / last heartbeat; carries `cursor` (null before baseline); resolution
is `--poll-seconds`.

## 12. v1 scope & DONE-WHEN (binary)

**In v1:** the generic core plus `http-poll` (Kijito reference, hard-baked shape) plus `stdout-jsonl` and
`exec-per-event` plus the full §7 contract (cursor/FSM/self-test/state-file) plus §8 SSRF/creds plus the §10 SIGUSR1
self-pipe seam plus the §9 baseline poll and the all-unread fast-path (`/api/notify/pending`) with the
`--resync-every` no-blindness safety floor.
**Deferred (explicit, not dropped):** the generic parse-config (`list_path`/`id_field`/arbitrary `fields`) for
non-Kijito REST shapes, the adapter zoo (file/IMAP/Slack/GitHub), native A2A/MCP, pip packaging, notification fan-out,
the hosted wake bridge, and the final published name (§13). (The server all-unread count and its consumption are now
done, see §9.)

**DONE-WHEN (each independently verifiable):**
1. `--self-test` exits 0 (one real peek-mode shape-valid fetch healthy and synthetic emit ok); exits non-zero
   against a hive-off/unreachable daemon. Reachability is printed.
2. (stdout-jsonl mode) Armed against the live inbox; after observing the `armed` event (cursor=C), send a test hive
   message M, and the watcher emits exactly one `new` with `id=M.id` (M.id > C); no `new` is emitted for any message
   with `id ≤ C`. (Framed as the cursor boundary, not wall-clock "pre-existing", so it's deterministic against a live
   multi-writer inbox.)
3. `--alert-after 3` with `--state-file`: simulate source-down by pointing `--url` at an unused localhost port
   (`--allow-loopback`), the canonical/safe test (do not stop the shared daemon), giving one `alert`; restore, giving
   one `recovered`; no re-emit while down.
4. **Restart-safe (cursor + dedup):** Stop the watcher at cursor=C (state-file written). Send message M (id>C) while
   stopped. Relaunch with the same `--state-file` (or `--seed-at C`). Pass means M emitted exactly once and no message
   ≤C re-emitted.
5. **Peek-stable:** after a poll and after `--self-test`, an unread message's `read` field is unchanged. Verify by a
   direct `GET /api/inbox?persona=P&mark_read=false` before and after (the target row's `read` stays the same).
6. **SSRF:** a generic `--url` at a private/loopback destination is refused without `--allow-loopback`/
   `--allow-private` (fatal, exit non-zero); a redirecting `--url` is refused regardless of those flags; the Kijito
   default still works; the per-request timeout is enforced.
7. **Replay cap:** with `cursor` set below a backlog of more than `--max-replay` items, the first poll emits
   `replay_capped` plus `armed` and zero `new`; with a backlog ≤ `--max-replay`, all replay as `new`.
8. **Shape/empty:** empty `{"result":[]}` is healthy no-new; a non-2xx / non-JSON / shape-invalid body is a liveness
   failure (counts toward alert), never a false "no mail."
9. **State-file safety:** a state-file whose `identity` mismatches the current `(persona,url)` does not resume its
   cursor (it re-baselines with a warning); a second watcher on the same state-file exits non-zero (flock).
10. Lives in `monitor/` as a single zero-dep stdlib file, committed and pushed (private GitHub
    `KijitoAI/kijito-inbox-monitor`, 2026-06-20; stays private until the public-flip gate), with a README
    documenting the supervision requirement plus `--state-file` (§7.3) and the CLI (§11). (v2: still one file; see §14
    for the multi-persona DONE-WHEN that supersede the single-persona framing of #2/#4 above. They hold per-persona.)

## 13. Naming: decided (2026-06-20; renamed 2026-06-24)

**Name: Kijito Inbox Monitor** (package `kijito-inbox-monitor`; GitHub `KijitoAI/kijito-inbox-monitor`,
matching the `Kijito`/`KijitoWeb` siblings). **Argus** is retained as the builder persona and internal codename, not
the product name. The name describes the product (marketplace tagline: "the local liveness watcher for your Kijito
inbox"), and it is collision-safe against the crowded "Argus" monitoring/observability namespace.

> **Rename note (2026-06-24):** the original 2026-06-20 call was `Kijito Monitor` / `kijito-monitor`, justified
> partly by "zero churn" since the deployed surface already encoded it. Before any external user existed, the choice
> was made to do it right and rename to the more descriptive **Kijito Inbox Monitor**, accepting the one-time internal
> churn (launchd label `com.kijito.inbox-monitor`, cache dir `~/.cache/kijito-inbox-monitor`, script
> `kijito_inbox_monitor.py`, repo) as a coordinated migration rather than ship an under-described public name.
> `KIJITOMON_*` env vars are unchanged.

For the record: the names `mailwatch`/`mail-watcher`/`agent-watch`/`nudge` were taken or avoided; the Kijito-ward
shortlist was `kijito-watch`/`kijito-inbox-watch`. Confirm `kijito-inbox-monitor` on PyPI/npm before any public
package publish (verified free 2026-06-24).

---

## 14. v2: multi-persona hive watch + supervised producer (shipped + deployed, 2026-06-19/20)

The deployed build watches the whole local hive from one process and is supervised by launchd. The §1 through §13
single-persona contracts are unchanged and apply per persona; this section records what was added on top. (Origin:
the multi-persona fold-in, folded into the canonical `monitor/` tree; per-persona event streams; the current arming
recipe.)

### 14.1 Multi-persona watch (one process, N inboxes)
- **Default (no `--persona`/`--personas`/`--url`):** watch every persona returned by `GET /api/personas`. A new
  persona comes online with no new process or flag. `--all-personas` is the explicit spelling.
- **Explicit subsets:** `--persona P` (repeatable) / `--personas A,B`. `--url` remains the single-target override.
- **Per-persona isolation:** each watched persona has its own cursor, alert FSM, state-file, and flock, derived from
  the `--state-file` base path as `hive.<persona>.json` (so `--state-file ~/.cache/kijito-inbox-monitor/hive.json`
  yields `hive.argus.json`, `hive.river.json`, and so on). All §7.0/§7.1/§7.3 semantics hold independently per persona.
- **Periodic rediscovery (`--rediscover-every`, default 600s):** in all-persona mode, re-scan `/api/personas` and add
  newly-created personas without a restart. It is add-only; it never drops a persona mid-run. Explicit
  `--persona`/`--personas` subsets stay fixed (no rediscovery).

### 14.2 One signal fetch per tick, fanned out locally
The §9 fast-path generalizes cleanly to the hive: one `GET /api/notify/pending` per tick returns the per-persona
`{persona, unread, unread_urgent}` map; the watcher fans it out locally to each persona's wake decision, and does not
issue one request per watched persona. A persona's full inbox-list poll (§5) still fires only on arm, on its `unread`
increase, on its `--resync-every` floor, or on fast-path fallback. The `--resync-every` no-blindness floor (§9)
applies per persona.

### 14.3 Owned, self-rotating EVENT sinks (the consume-your-own fix)
Two emit-to-file modes for supervised runs (both write NDJSON the watcher owns and size-rotates in-process, with no
`newsyslog`/`logrotate`/`sudo`, so there is no orphaned-fd silent-blinding; consumers `tail -F`):
- **`--events-file PATH`**: one shared log. Correct for a single-target supervised watch.
- **`--events-file-template PATH`**: one log per persona, e.g. `events.{persona}.ndjson` (one `RotatingFileSink` per
  persona, created lazily, all closed on shutdown). The `{persona}` placeholder is required, and it is mutually
  exclusive with `--events-file`. This is what the deployed hive producer runs.
- **Rotation:** `--max-bytes` (default 5_000_000; `<=0` disables) keeping `--keep-logs` archives (default 5, min 1).
- **`--suppress-author P`** (repeatable): drop `new` events authored by P, which kills the self-echo an all-persona
  watcher gets for mail it sent (a dogfood finding). Liveness events (`alert`/`recovered`/`heartbeat`) are unaffected;
  the cursor still advances (no re-emit).

**Why per-persona event files (LLM-UX):** off a single shared log, a session can only get its own mail by inventing
an undocumented consumer-side `grep "persona": "X"` filter, which is not discoverable and which each agent improvises
differently. One file per persona makes "subscribe to only my own mail" a self-evident `tail -F
events.<persona>.ndjson`: zero filtering, discoverable by filename.

**Disambiguation (load-bearing):** `hive.<persona>.json` is internal state (cursor/FSM bookkeeping; do not tail);
`events.<persona>.ndjson` is the event stream a session tails to consume its mail.

**Migration trap:** the older single shared `events.ndjson` is retired. A consumer still tailing it goes silently
blind (no writer appends). Repoint to `events.<persona>.ndjson`. (This was hit live during cutover; silence is not
success.)

### 14.4 Deployment: single supervised producer, many tailing consumers
- **Producer:** one launchd user LaunchAgent `com.kijito.inbox-monitor` (`~/Library/LaunchAgents/`, RunAtLoad +
  KeepAlive) runs the all-persona producer with `--events-file-template`. KeepAlive covers the `kill -9` /
  process-death gap a bare file-tail can't see (kill-9-proven). stderr goes to `~/.cache/kijito-inbox-monitor/monitor.err`.
- **Consumers:** each agent session is a consumer that tails only its own `events.<persona>.ndjson` into its harness's
  wake mechanism. A session does not start its own watcher; a second producer would collide on the per-persona
  state-file flock.
- **Cutover discipline:** retire any existing detached producer first (the per-persona flocks permit one writer), then
  `launchctl bootstrap` and `kickstart` the agent. Self-rotating event files mean consumers reattach across rotations
  via `tail -F` (follow-by-name) with no gap.

### 14.5 v2 DONE-WHEN (supersede the single-persona framing of §12 #2/#4; they hold per-persona)
- **m1.** Bare arm (no flags) watches every `/api/personas` persona from one process; each gets its own
  `hive.<persona>.json` (separate cursor/FSM/lock), with no shared `hive.json` and no replay flood on restart.
- **m2.** Exactly one `/api/notify/pending` request per tick regardless of persona count (fanned out locally).
- **m3.** `--events-file-template` writes one `events.<persona>.ndjson` per persona; a session tailing its own file
  receives only its own `new` events; rotation reopens in-process (the consumer reattaches via `tail -F`).
- **m4.** `--all-personas` plus `--suppress-author P` drops `new` events authored by P; liveness events still flow.
- **m5.** Supervised under `com.kijito.inbox-monitor` (RunAtLoad + KeepAlive): a `kill -9` of the producer is recovered
  automatically; exactly one producer runs; per-persona cursors resume (no replay flood).

### 14.6 Still open (not blocking; tracked elsewhere)
- **Name decided** (Kijito Inbox Monitor, §13) and pushed private (`KijitoAI/kijito-inbox-monitor`, 2026-06-20).
  Remaining: the public flip when ready (confirm PyPI/npm `kijito-inbox-monitor` first); and the README links to
  `../docs/DESIGN.md`, which is repo-external (this spec lives in the workspace, not the repo), so vendor this spec
  into the repo before the public flip so the link resolves on GitHub.
- **Marketplace** surfacing, at launch-time.
- **Codex-side consumer bridge:** Codex sessions aren't yet woken by their event file; the Claude harness Monitor
  tool is the native consumer (done).
