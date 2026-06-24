# KijitoInboxMonitor — Design & Implementation Spec

**Author:** argus · **Updated:** 2026-06-20 (rev 6 — v2 multi-persona + supervised producer SHIPPED + DEPLOYED;
see §15) · **Status:** SHIPPED + LIVE (v2 under launchd).
**Goal (Jason, re-centered [2014]):** give Kijito a **solid, usable local liveness watcher for its built-in
inbox**. The concrete Kijito-inbox monitor is the win — agnosticism is a *means* (generalize only where it makes us
more useful), never the end. Learn from prior art; don't gold-plate.

§1–§14 are the **v1 single-persona core** — still accurate and load-bearing (one process watches one inbox; the
cursor/dedup/FSM/self-test/state/SSRF/seam contracts apply per-persona unchanged). **§15 records the v2 deltas**:
the deployed build watches the WHOLE local hive from ONE supervised process and writes one owned, self-rotating
EVENT log per persona. Read §15 alongside §1, §11, and §12 for current reality.

---

## 1. What it is

A standalone, **single zero-dependency Python-stdlib script** (urllib, json, signal, select, fcntl, subprocess;
no pip installs) that **polls the Kijito inbox and emits one event per new message** into whatever harness is
running — NDJSON on stdout and/or exec-a-command-per-event. It is the **client-side liveness watcher**: keeps a
*running* agent's inbox live by waking it *between* tool calls. NOT a server, NOT a notification service. **POSIX
target** (Linux/macOS); Windows runs interval-only (no SIGUSR1 seam, no flock — §10/§7.3).

## 2. The problem

The "inbox-liveness" **LLM-UX bug** ([1988]): agents predictably fail to keep an independent inbox check alive —
they tie it to a work loop that ends, or never set one up. Fix = move the burden **off agent-discipline onto a
running guarantee**: an independent process that watches + emits, decoupled from any work loop. (Dogfooded for
argus; adversarial review surfaced real bugs in its own early versions — folded into this rev.)

## 3. The composition contract (LOCKED with River — [2018], [2023])

Client half of Kijito's server-side inbox-liveness system. Two complementary layers:

| Layer | What | Where | Guarantee |
|-------|------|-------|-----------|
| **Banner** (server) | unread banner in every Kijito tool response | server-side, every client | zero-setup FLOOR; *delivery-on-next-CALL* |
| **Watcher** (this) | independent process polls + emits per-new-item | client-side, harnesses that run a process | proactive; *wake-WITHOUT-a-call* |

- One shared signal source (the `control_plane` urgent counter) so liveness never diverges; the watcher consumes a
  server count over HTTP (§9), never reimplements liveness.
- **v1 = pure local poller** + the **opaque-wake seam** (§10) so a hosted bridge can *later* push wake→pull.
- **Marketplace:** River wants it as a surface — "the local liveness watcher for your Kijito inbox."

## 4. Architecture

`SOURCE adapter (http-poll) → GENERIC CORE (cursor/dedup/alert-FSM/self-test/state/wake-seam) → EMIT (stdout-jsonl | exec-per-event)`.
v1 ships ONE adapter (`http-poll`, Kijito reference). Future adapters explicitly deferred.

## 5. The `http-poll` adapter — Kijito inbox contract (CODE-VERIFIED [2016], audited 2026-06-17)

- **Endpoint:** `GET /api/inbox?persona=<P>&mark_read=false`
- **Response:** `{"result": [ {"id":<int>,"from":"<persona>","content":"<plaintext>","created":"<iso-str>","read":<bool>}, ... ]}`
  (keys verbatim, that order — messaging.py:85-89). **v1 HARD-BAKES this Kijito response shape** (no generic parse
  config — that's deferred, §scope). `--url` overrides only the **destination**, not the parse contract.
- **`mark_read` DEFAULTS `true`** (web_api.py:504; SET m.read=true at messaging.py:90-96). The URL **MUST** carry
  `&mark_read=false`. **A watcher MUST PEEK, never consume** — EVERY fetch site (poll loop AND `--self-test`) uses
  the `mark_read=false` URL. ([2020]/[2023], triple-confirmed; original seed fixed by ladybug.)
- **`id` = SERIAL PK → strictly monotonic** (schema.py:168), **gaps allowed**. Cursor keys on **MAX-id**, never on
  read/unread state ([1867]).
- **NO pagination** — full list (seen at 148K/539 msgs). Emit only the DIFF (id > cursor); never dump the body.
- **AUTH:** local daemon needs **NO token** (loopback trust — empirically the argus watcher polls tokenless). If
  `$KIJITOMON_TOKEN`/`--token-file` is set, inject as `Authorization: Bearer <token>`, or with `--auth-header NAME`
  as `NAME: <token>` verbatim. Header-NAME (`--auth-header`) and token-VALUE source (`--token-file` wins over env)
  are **independent axes**. Unreadable `--token-file` = fatal config error.
- **A poll is HEALTHY iff** HTTP 2xx AND body parses AND envelope is shape-valid (`result` is a list; every row is
  an object with an integer `id`). **Anything else** — non-2xx, connection-refused, DNS failure, connection-reset,
  timeout, parse-fail, shape-violation — is a **liveness FAILURE (UNKNOWN), never "no mail."** (A 200 with a
  truncated-but-parseable body that fails the shape check is a FAILURE.)
- **Empty `{"result":[]}`** = healthy, no new items.
- **Hive-off / 404 timing matters:** detected **at startup / `--self-test`** → fatal config error (exit
  non-zero). Appearing **mid-run** → a per-poll **liveness FAILURE** (the daemon may have restarted or hive toggled
  transiently) — it feeds the §7.1 FSM, it does NOT kill the process (a transient server blip must not destroy the
  dead-man's-switch).
- **Config:** `url` (destination override), `poll_seconds` (default 60). Reference config hard-bakes the URL incl.
  `mark_read=false`.

## 6. Emit modes (portability — [2011])

"NDJSON-on-stdout is universal" is FALSE on ingestion: Claude Code ingests per-event (hooks: JSON-on-stdin,
exit-code, `additionalContext`; + FileChanged); LangGraph/OpenAI-Agents/Cursor are in-process (no stdin/stdout
event ingestion). So **`exec-per-event` is the MORE portable primitive**; `stdout-jsonl` is the ergonomic default.

### 6.1 Event schema (stdout-jsonl) — one object/line; EVERY event carries `event`, `source`, `ts` (emit-time UTC ISO).
```
{"event":"new",         "source":"kijito-inbox","ts":"<iso>","id":246,"from":"river","content":"<≤N or omitted>","created":"<iso>"}
{"event":"armed",       "source":"kijito-inbox","ts":"<iso>","cursor":250}
{"event":"alert",       "source":"kijito-inbox","ts":"<iso>","reason":"unreachable","consecutive_failures":3,"seconds":180}
{"event":"recovered",   "source":"kijito-inbox","ts":"<iso>","cursor":250}
{"event":"heartbeat",   "source":"kijito-inbox","ts":"<iso>","cursor":250}     # only if --heartbeat; cursor may be null
{"event":"seed_ahead",  "source":"kijito-inbox","ts":"<iso>","seeded":600,"current_max":539}      # seed > reality (§7.0)
{"event":"replay_capped","source":"kijito-inbox","ts":"<iso>","capped_to":539,"dropped":389}      # backlog > --max-replay (§7.0)
```
- `new` carries `id`, `from`, `content`, `created`. `content` = silent hard cut to `--content-chars` (default 220),
  no marker; or omitted with `--no-content`. `seconds` in `alert` is **nominal** (`consecutive_failures × poll_seconds`).
- **Within-poll emit ORDER (deterministic, total):** `alert`/`recovered` (FSM edge) → `replay_capped`/`seed_ahead`
  → `armed` → `new` (ascending id) → `heartbeat`. So `armed`/`recovered` set `cursor` before any `new`/`heartbeat`
  in the same cycle → `recovered.cursor` is non-null whenever a baseline has occurred (a `recovered` on a poll that
  also baselines carries the just-set cursor).

### 6.2 `exec-per-event` (`--emit exec-per-event --exec 'CMD'`, `--exec` required iff this mode)
EVERY event invokes `CMD`; inapplicable env vars are unset:

| env var | new | armed | alert | recovered | heartbeat | seed_ahead | replay_capped |
|---|---|---|---|---|---|---|---|
| `KIJITOMON_EVENT`,`_SOURCE`,`_TS` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `KIJITOMON_ID`,`_FROM`,`_CONTENT`,`_CREATED` | ✓ | – | – | – | – | – | – |
| `KIJITOMON_CURSOR` | – | ✓ | – | ✓ | ✓ | – | – |
| `KIJITOMON_REASON`,`_FAILURES` | – | – | ✓ | – | – | – | – |
| `KIJITOMON_SEEDED`,`_CURRENT_MAX` | – | – | – | – | – | ✓ | – |
| `KIJITOMON_CAPPED_TO`,`_DROPPED` | – | – | – | – | – | – | ✓ |

Spawned command **timeout 10s**; non-zero exit / timeout is logged to stderr and **non-fatal** (and NEVER holds the
cursor back — §7.0).

## 7. Robustness contract ([2019], [1866], [1994])

### 7.0 Cursor / dedup algorithm
- **"Re-arm"** = a cursor-INITIALIZATION at startup (resume / seed / baseline). It is NOT a §7.1 FSM `recovered`
  (recovery resumes normal diffing with the cursor already tracking — the replay cap below NEVER re-applies on
  recovery).
- **CURSOR vs FSM resolve INDEPENDENTLY at startup** (they are two separate resolutions, not one ladder):
  - **CURSOR:** (1) explicit `--seed-at <id>` → `cursor = id` (operator intent wins, overriding any state-file
    cursor); else (2) a **valid identity-matching** `--state-file` (§7.3) with an integer cursor → `cursor =
    resumed value`; else (3) `cursor = UNSET`.
  - **FSM** (`state`, `consecutive_failures`): a **valid identity-matching** `--state-file` ALWAYS supplies it —
    **independent of `--seed-at`** (so `--seed-at` + a DOWN state-file resumes DOWN, preserving dead-man's-switch
    continuity); absent/mismatched/invalid state-file → FSM starts `UP`/`0`.
- **`armed` fires on the FIRST HEALTHY poll** (never before a fetch). **Exactly one `armed` per (re)arm**, carrying
  the post-decision `cursor`. A failed first poll does NOT baseline (it's a §7.1 failure; `armed` waits).
- **Each HEALTHY poll:** SELECT items with `id > cursor`; sort ascending; emit one `new` per item; then **advance
  `cursor` to the max id in the SELECTED diff, unconditionally** — cursor advances on SELECTION, not emit success.
  Emit is best-effort/at-most-once: an exec failure neither holds back nor re-emits. `cursor` is monotonic.
- **First-healthy-poll branches** (mutually exclusive; each emits exactly one `armed`):
  - **UNSET baseline** (cursor was UNSET — incl. a state-file resume whose cursor was `null`): `cursor = max(id)`
    (or 0 if empty); emit `armed`; **no** `new`, **no** cap (nothing to replay). *Exempt from the cap.*
  - **Non-null re-arm** (cursor came from `--seed-at` or a non-null state-file resume): let `n = count(id > cursor)`,
    `current_max = max(ids) if result else 0`.
    - if `cursor > current_max` (seed/resume ahead of reality): emit `armed{cursor}` + `seed_ahead{seeded=cursor,
      current_max}` (real ids ≤ cursor are intentionally skipped); no `new`.
    - elif `n > --max-replay`: fast-forward `cursor = current_max`; emit `armed{cursor}` + `replay_capped{capped_to=
      current_max, dropped=n}`; no `new`.
    - else (`n ≤ --max-replay`): emit `armed{cursor}` then replay all `n` as `new` (so exactly `--max-replay`
      replay at the boundary; `>` is the cut).

7 lessons (mandatory): standalone FILE not inline-shell [1994]; single long-lived process + per-line flush;
dedup/cursor by MAX-id + peek; emit DIFF not body; parse/shape/HTTP failure = UNKNOWN not "no mail" (§5); re-arm via
`--state-file` or `--seed-at-last-handled`; avoid auto-stop-on-volume [1866] (dedup + peek + edge-alerts + the
replay cap).

### 7.1 Liveness alert FSM (dead-man's-switch — [2026])
States **UP** (default) / **DOWN**; `consecutive_failures` from 0. A "failure" = any non-healthy poll (§5).
- **HEALTHY poll:** `consecutive_failures = 0`; if DOWN → UP, emit ONE `recovered`.
- **Failure:** `consecutive_failures += 1`; the **UP→DOWN edge** is crossed when `consecutive_failures` first
  *reaches* `--alert-after` while state is UP → set DOWN, emit ONE `alert`. (The `state==UP` guard is what makes it
  edge-once; a resumed `state==DOWN` never re-crosses the edge, so no duplicate `alert`.)
- `alert`/`recovered` are **per-edge** — a run may alert→recover→alert again. A sub-threshold blip emits neither.
- `--alert-after` minimum **1** (reject 0), default **3** (a single transient failure is NORMAL — bounce ~1-2s).
  SIGUSR1-triggered polls participate identically.

### 7.2 `--self-test` ([1994]) — runs ONCE and exits (no poll loop)
Requires `--persona` or `--url`. (a) ONE real **peek-mode** (`mark_read=false`) fetch, checked HEALTHY per §5 (so
hive-off/404/unreachable correctly FAILS self-test); (b) a synthetic `new` through the real emit path (stdout: line
written+flushed = ok; exec: spawn `--exec` with a fake `new`, child exit 0 within timeout = ok). **Exit 0 iff BOTH
healthy AND emit-ok;** else non-zero. Fetch result printed regardless. The probe is peek-mode → read-state-neutral
(DONE-WHEN #5 holds after self-test).

### 7.3 State persistence (`--state-file PATH`, optional, recommended under a supervisor)
- **Content (JSON):** `{"identity":<canonical-id>, "cursor":<int|null>, "state":"UP|DOWN",
  "consecutive_failures":<int>}`.
- **Canonical identity (`<canonical-id>`)** — computed BEFORE DNS resolution so trivial URL variations don't flip
  it: from the EFFECTIVE inbox URL (the baked `--persona` reference form and an equivalent `--url` form must
  canonicalize EQUAL), the tuple `(scheme.lower(), host.lower(), effective_port, path, sorted(query_params EXCEPT
  the constant mark_read))`. Normalize: strip a trailing `/` on path, fill the scheme's default port, sort query
  params, lowercase host. (So `?persona=river&mark_read=false` ≡ `?mark_read=false&persona=river`, and `:7474` ≡
  default-for-scheme, and the `--persona river` reference ≡ the equivalent explicit `--url`.) `persona` is encoded
  via the `persona=` query param already in the URL — do NOT add a separate persona field that the `--url` form
  lacks.
- **Single-writer LOCK:** on startup acquire an exclusive `fcntl.flock` on the state-file (LOCK_EX|LOCK_NB); if
  held, **exit non-zero** ("state-file in use") — prevents two watchers tearing the cursor backwards. **Hold the
  lock fd open for the whole process lifetime.** flock is advisory and **auto-released by the OS on process exit**
  (normal/SIGTERM/SIGKILL/crash) → no stale lockfile to clean (unlike a pidfile).
- **Write:** after each poll, atomic = `mkstemp` in the SAME dir → write → `fsync` → `os.replace`; best-effort
  remove of stale temps.
- **Resume validity:** VALID iff it parses as the schema (integer-or-null `cursor`, `state ∈ {UP,DOWN}`, integer
  `consecutive_failures`) AND `identity` **equals the current canonical-id**. On a VALID match → resume per §7.0
  (cursor unless `--seed-at` overrides; FSM always). **Identity MISMATCH** → do NOT resume the cursor (would yield a
  silently-blind watcher): log a loud warning + re-baseline cursor as UNSET, and start the FSM fresh (UP/0). A
  **parse/schema-invalid or empty** file → treated as ABSENT (fall through). A **present-but-unreadable** path → 
  fatal config error.
- **Without `--state-file`:** no lock and no persistence → FSM is per-process (a restart resets it), and **two
  no-state watchers for the same inbox would BOTH emit `new`** (duplicate delivery — they don't corrupt read-state
  since both peek, but the harness is woken twice). Run a SINGLE instance, or use `--state-file` under a supervisor
  (which both locks and persists). The **external** dead-man's-switch (`--heartbeat` → healthchecks.io/Dead Man's
  Snitch) is then the cross-restart liveness guarantee, and DONE-WHEN #3's "no re-emit while down" scopes to a
  single process.

## 8. Security (SSRF + creds — lift, don't reinvent [2019])

- **SSRF guard on a user-supplied `--url` ONLY**; the baked Kijito loopback reference is exempt. Two independent
  rules: **(destination class)** default-deny private/loopback/link-local for a generic `--url`; opt in with
  `--allow-loopback`/`--allow-private`. Lift `_resolve_and_pin` (notify_channels.py:57-99 — resolve ALL IPs, reject
  internal) + `_PinnedHTTPSConnection` (:123-146 — connect to pinned ip, no re-resolve = no TOCTOU). **(redirects)**
  **never followed, independent of the allow-flags** (refused regardless), per `_safe_post` (def :166, ~:166-201 —
  no-redirect opener + timeout). Per-request **timeout default 5s**. A user-`--url` SSRF violation (blocked dest or
  redirect) is a **fatal startup error → exit non-zero**, distinct from a per-poll liveness failure.
  Stdlib: no-redirect via `HTTPRedirectHandler.redirect_request → None`; IP-pin via custom `HTTPConnection` through
  `do_open`; `urlopen(timeout=)`.
- **Creds via env/file, NEVER argv** (`$KIJITOMON_TOKEN` / `--token-file`; §5 for header + precedence).
- **Zero-knowledge:** local content is decrypted locally (no tension); any future hosted bridge carries an **opaque
  wake only** (`--no-content` supports opaque mode).

## 9. Signal strategy — the all-unread fast-path (IMPLEMENTED — [2023], River PR#66)

- **Baseline:** the inbox-list poll (§5) is always the floor and the source of truth — the max-id cursor decides
  WHAT to emit, so the fast-path can never cause a missed or duplicate emit.
- **Fast-path (cheap O(1) pre-check):** `GET /api/notify/pending` (SLASH path; the hyphen `/api/notify-pending`
  404s) — READ-ONLY, never marks read. Response `{"result":[{"persona","unread","unread_urgent"},...]}`; `unread` =
  ALL read=false for that persona (a persona with 0 unread is ABSENT → treat as 0). The watcher probes it ONCE on
  arm; if available it consumes `unread` for its persona and does the full inbox-list fetch ONLY when `unread`
  INCREASES — saving the full-list diff on quiet polls. Auto-falls-back to baseline if the endpoint is absent /
  non-2xx (a non-Kijito source, or a daemon without the field, simply runs baseline).
- **SAFETY FLOOR (`--resync-every`, default 10):** the watcher NEVER skips more than N consecutive polls — it
  forces a full inbox poll regardless. So a stale / wrong / unsupported count (e.g. a daemon running pre-PR#66 code
  that returns 0 forever) can at worst add latency, NEVER blind the watcher. `unread` is only the wake TRIGGER.
- `--no-fast-path` forces baseline (always full-poll). NOTE: a self-sent message does NOT bump your own `unread`
  (the daemon doesn't treat your own outgoing mail as unread-for-you) — so fast-path wakes you on INCOMING mail,
  which is the intended liveness behaviour.
- BUILD-TIME NOTE (2026-06-17): the `unread` field is live on prod/the box but the LOCAL :7474 daemon was running
  stale pre-PR#66 code (source has it — web_api.py:601 — but the running process is pending a restart [2015]).
  Verified: the fast-path mechanics on a mock, and the resync floor catching real mail on the stale local daemon.

## 10. Opaque-wake seam (build the hook, not the bridge — [2018])

Internal "poll now" trigger besides the interval, wired to **SIGUSR1** (POSIX only). Mandatory race-free mechanics:
- **Install a NO-OP Python handler** `signal.signal(SIGUSR1, lambda *_: None)` — REQUIRED, or the default
  disposition terminates the process and `set_wakeup_fd` writes nothing.
- **Self-pipe via a non-blocking `socketpair`** (more portable than `os.pipe` for `set_wakeup_fd`): set both ends
  non-blocking, `signal.set_wakeup_fd(w)`; the main loop blocks in `select.select([r],[],[],timeout)`. A signal at
  any instant either interrupts the in-progress `select` or leaves a byte that makes the **next** `select` return
  immediately — no lost wakeup.
- **Read-and-clear = DRAIN the pipe** (`os.read(r, 4096)`) at the **START** of each poll (before fetch). Any
  SIGUSR1 after that drain — even during the same poll's fetch/emit — leaves a byte guaranteeing a subsequent poll.
  Gives "at most one extra poll per quiescent signal" with **no signal lost** once a poll has begun.
- **One polling site on the main loop; the handler does no work** → re-entrancy is structurally impossible.
- v1 opens NO remote listener. A later hosted bridge turns an opaque wake into a SIGUSR1/FIFO poke → pull over the
  authenticated channel (client-side consumer in Kijito's [1684] notify-then-pull matrix). Windows: interval-only.

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
**Arg matrix:** `--url` absent → `--persona` required, Kijito reference URL used. `--url` present → overrides,
`--persona` ignored. Error (exit non-zero) if both absent. An explicit `--seed-at` overrides a state-file cursor.
**`--heartbeat SECONDS`:** emitted on the poll cycle (HEALTHY or failed — it proves the *watcher* is alive) once ≥
SECONDS elapsed since process start / last heartbeat; carries `cursor` (null before baseline); resolution =
`--poll-seconds`.

## 12. v1 scope & DONE-WHEN (binary)

**In v1:** generic core + `http-poll` (Kijito reference, hard-baked shape) + `stdout-jsonl` & `exec-per-event` +
full §7 contract (cursor/FSM/self-test/state-file) + §8 SSRF/creds + §10 SIGUSR1 self-pipe seam + §9 baseline poll
+ the all-unread fast-path (`/api/notify/pending`) with the `--resync-every` no-blindness safety floor.
**Deferred (explicit, not dropped):** the generic parse-config (`list_path`/`id_field`/arbitrary `fields`) for
non-Kijito REST shapes, adapter zoo (file/IMAP/Slack/GitHub), native A2A/MCP, pip packaging, notification fan-out,
the hosted wake bridge, final published name (§13). (The server all-unread count + its consumption are now DONE — §9.)

**DONE-WHEN (each independently verifiable):**
1. `--self-test` exits 0 (one real peek-mode shape-valid fetch healthy AND synthetic emit ok); exits non-zero
   against a hive-off/unreachable daemon. Reachability printed.
2. (stdout-jsonl mode) Armed against the **live** inbox; **after observing the `armed` event** (cursor=C), send a
   test hive message M → the watcher emits exactly one `new` with `id=M.id` (M.id > C); no `new` is emitted for any
   message with `id ≤ C`. (Framed as the cursor boundary, not wall-clock "pre-existing", so it's deterministic
   against a live multi-writer inbox.)
3. `--alert-after 3` with `--state-file`: simulate source-down by pointing `--url` at an **unused localhost port**
   (`--allow-loopback`) — the canonical/safe test (do NOT stop the shared daemon) — → ONE `alert`; restore → ONE
   `recovered`; no re-emit while down.
4. **Restart-safe (cursor + dedup):** Stop the watcher at cursor=C (state-file written). Send message M (id>C)
   while stopped. Relaunch with the same `--state-file` (or `--seed-at C`). PASS = M emitted exactly once AND no
   message ≤C re-emitted.
5. **Peek-stable:** after a poll AND after `--self-test`, an unread message's `read` field is UNCHANGED — verify by
   a direct `GET /api/inbox?persona=P&mark_read=false` before and after (the target row's `read` stays the same).
6. **SSRF:** a generic `--url` at a private/loopback destination is refused without `--allow-loopback`/
   `--allow-private` (fatal, exit non-zero); a redirecting `--url` is refused regardless of those flags; the Kijito
   default still works; per-request timeout enforced.
7. **Replay cap:** with `cursor` set below a backlog of > `--max-replay` items, the first poll emits `replay_capped`
   + `armed` and ZERO `new`; with a backlog ≤ `--max-replay`, all replay as `new`.
8. **Shape/empty:** empty `{"result":[]}` = healthy no-new; a non-2xx / non-JSON / shape-invalid body = a liveness
   failure (counts toward alert), never a false "no mail."
9. **State-file safety:** a state-file whose `identity` mismatches the current `(persona,url)` does NOT resume its
   cursor (re-baselines with a warning); a second watcher on the same state-file exits non-zero (flock).
10. Lives in `monitor/` as a single zero-dep stdlib file, **committed + pushed** (private GitHub
    `ArcadaLabs-Jason/KijitoInboxMonitor`, 2026-06-20; stays private until the public-flip gate), with a README
    documenting the supervision requirement + `--state-file` (§7.3) and the CLI (§11). *(v2: still one file; see §15
    for the multi-persona DONE-WHEN that supersede the single-persona framing of #2/#4 above — they hold
    per-persona.)*

## 13. Naming — DECIDED (Jason, 2026-06-20; RENAMED 2026-06-24)

**Name: Kijito Inbox Monitor** (package `kijito-inbox-monitor`; GitHub `ArcadaLabs-Jason/KijitoInboxMonitor`,
matching the `Kijito`/`KijitoWeb` siblings). **Argus** is retained as the builder PERSONA + internal codename, NOT
the product name. The name IS the description (marketplace tagline: "the local liveness watcher for your Kijito
inbox"), and is collision-safe vs the crowded "Argus" monitoring/observability namespace.

> **Rename note (Jason, 2026-06-24):** the original 2026-06-20 call was `Kijito Monitor` / `kijito-monitor`,
> justified partly by "zero churn" since the deployed surface already encoded it. Before any external user existed,
> Jason chose to "do it right" and rename to the more descriptive **Kijito Inbox Monitor** — accepting the one-time
> internal churn (launchd label `com.kijito.inbox-monitor`, cache dir `~/.cache/kijito-inbox-monitor`, script
> `kijito_inbox_monitor.py`, repo) as a coordinated migration rather than ship an under-described public name.
> `KIJITOMON_*` env vars are unchanged.

For the record: TAKEN/avoided `mailwatch`/`mail-watcher`/`agent-watch`/`nudge`; the Kijito-ward shortlist was
`kijito-watch`/`kijito-inbox-watch` [2028]/[2014]/[2023]. **Confirm `kijito-inbox-monitor` on PyPI/npm before any
PUBLIC package publish** (verified free 2026-06-24).

## 14. References (Kijito memory ids)
Goal [2024]. Re-center [2014]. Composition [2018]/[2023]. Inbox contract [2016]. Robustness [2019]/[1866]/[1994].
mark_read footgun [2020]. Liveness [2026]. Naming [2028]. Harness/portability [2011]. Landscape [2012]/[2013].
LLM-UX bug [1988]. Server inbox-liveness [1989]. Notify-then-pull [1684]. v2 multi-persona [2184]/[2208]/[2218].

---

## 15. v2 — multi-persona hive watch + supervised producer (SHIPPED + DEPLOYED, 2026-06-19/20)

The deployed build watches the **whole local hive from ONE process** and is supervised by launchd. The §1–§14
single-persona contracts are unchanged and apply **per persona**; this section records what was added on top.
(Origin: codex's multi-persona fold-in [2184], folded into the canonical `monitor/` tree; per-persona event
streams [2208]; current arming recipe [2218].)

### 15.1 Multi-persona watch (one process, N inboxes)
- **Default (no `--persona`/`--personas`/`--url`):** watch **every** persona returned by `GET /api/personas`. A
  new persona comes online with no new process or flag. `--all-personas` is the explicit spelling.
- **Explicit subsets:** `--persona P` (repeatable) / `--personas A,B`. `--url` remains the single-target override.
- **Per-persona isolation:** each watched persona has its **own cursor, alert FSM, state-file, and flock**, derived
  from the `--state-file` base path → `hive.<persona>.json` (so `--state-file ~/.cache/kijito-inbox-monitor/hive.json`
  yields `hive.argus.json`, `hive.river.json`, …). All §7.0/§7.1/§7.3 semantics hold independently per persona.
- **Periodic rediscovery (`--rediscover-every`, default 600s):** in all-persona mode, re-scan `/api/personas` and
  **add** newly-created personas without a restart. **Add-only** — it never drops a persona mid-run. Explicit
  `--persona`/`--personas` subsets stay fixed (no rediscovery).

### 15.2 One signal fetch per tick, fanned out locally
The §9 fast-path generalizes cleanly to the hive: **one** `GET /api/notify/pending` per tick returns the
per-persona `{persona, unread, unread_urgent}` map; the watcher **fans it out locally** to each persona's
wake decision — it does NOT issue one request per watched persona. A persona's full inbox-list poll (§5) still
fires only on arm, on its `unread` increase, on its `--resync-every` floor, or on fast-path fallback. The
`--resync-every` no-blindness floor (§9) applies per persona.

### 15.3 Owned, self-rotating EVENT sinks (the [1988] consume-your-own fix)
Two emit-to-file modes for supervised runs (both write NDJSON the watcher **owns and size-rotates in-process** — no
`newsyslog`/`logrotate`/`sudo`, so no orphaned-fd silent-blinding; consumers `tail -F`):
- **`--events-file PATH`** — ONE shared log. Correct for a **single-target** supervised watch.
- **`--events-file-template PATH`** — one log **per persona**, e.g. `events.{persona}.ndjson` (one
  `RotatingFileSink` per persona, created lazily, all closed on shutdown). **`{persona}` placeholder required;
  mutually exclusive with `--events-file`.** This is what the deployed hive producer runs.
- **Rotation:** `--max-bytes` (default 5_000_000; `<=0` disables) keeping `--keep-logs` archives (default 5, min 1).
- **`--suppress-author P`** (repeatable): drop `new` events authored by P — kills the self-echo an all-persona
  watcher gets for mail IT sent (ladybug dogfood finding). Liveness events (`alert`/`recovered`/`heartbeat`)
  unaffected; the cursor still advances (no re-emit).

**Why per-persona event files (LLM-UX, [1988]/[2208]):** off a single shared log, a session can only get its own
mail by inventing an undocumented consumer-side `grep "persona": "X"` filter — not discoverable, each agent
improvises differently. One file per persona makes "subscribe to only my own mail" a self-evident `tail -F
events.<persona>.ndjson` — zero filtering, discoverable by filename.

**Disambiguation (load-bearing):** `hive.<persona>.json` = internal **STATE** (cursor/FSM bookkeeping — do NOT
tail); `events.<persona>.ndjson` = the **EVENT** stream a session tails to consume its mail.

**Migration trap:** the older single shared `events.ndjson` is **retired**. A consumer still tailing it goes
**silently blind** (no writer appends). Repoint to `events.<persona>.ndjson`. (Hit live during cutover — silence
is not success.)

### 15.4 Deployment — single supervised producer, many tailing consumers
- **Producer:** ONE launchd **user** LaunchAgent `com.kijito.inbox-monitor` (`~/Library/LaunchAgents/`, RunAtLoad +
  KeepAlive) runs the all-persona producer with `--events-file-template`. KeepAlive covers the `kill -9` /
  process-death gap a bare file-tail can't see (kill-9-proven). stderr → `~/.cache/kijito-inbox-monitor/monitor.err`.
- **Consumers:** each agent **session** is a consumer that tails ONLY its own `events.<persona>.ndjson` into its
  harness's wake mechanism. A session does **NOT** start its own watcher — a second producer would collide on the
  per-persona state-file flock.
- **Cutover discipline:** retire any existing detached producer FIRST (the per-persona flocks permit one writer),
  then `launchctl bootstrap` + `kickstart` the agent. Self-rotating event files mean consumers reattach across
  rotations via `tail -F` (follow-by-name) with no gap.

### 15.5 v2 DONE-WHEN (supersede the single-persona framing of §12 #2/#4 — they hold per-persona)
- **m1.** Bare arm (no flags) watches every `/api/personas` persona from ONE process; each gets its own
  `hive.<persona>.json` (separate cursor/FSM/lock) — no shared `hive.json`, no replay flood on restart.
- **m2.** Exactly ONE `/api/notify/pending` request per tick regardless of persona count (fanned out locally).
- **m3.** `--events-file-template` writes one `events.<persona>.ndjson` per persona; a session tailing its own
  file receives only its own `new` events; rotation reopens in-process (consumer reattaches via `tail -F`).
- **m4.** `--all-personas` + `--suppress-author P` drops `new` events authored by P; liveness events still flow.
- **m5.** Supervised under `com.kijito.inbox-monitor` (RunAtLoad + KeepAlive): a `kill -9` of the producer is recovered
  automatically; exactly one producer runs; per-persona cursors resume (no replay flood).

### 15.6 Still open (not blocking; tracked elsewhere)
- **Name DECIDED** (Kijito Inbox Monitor, §13) + **pushed PRIVATE** (`ArcadaLabs-Jason/KijitoInboxMonitor`, 2026-06-20).
  Remaining: the PUBLIC flip when ready (confirm PyPI/npm `kijito-inbox-monitor` first); and the README links to
  `../docs/DESIGN.md`, which is repo-EXTERNAL (this spec lives in the workspace, not the repo) → **vendor this
  spec into the repo before the public flip** so the link resolves on GitHub.
- **Marketplace** surfacing — launch-time (River's lane).
- **Codex-side consumer bridge** — Codex sessions aren't yet woken by their event file (codex's lane [2165]); the
  Claude harness Monitor tool is the native consumer (done).
