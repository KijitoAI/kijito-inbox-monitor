# (tool name TBD) — a tool-agnostic agent inbox/event monitor

> **Status: pre-implementation (design phase).** This repo is scaffolded but intentionally empty of
> code — implementation waits on the harden + QA-gate of the design (see `../CLAUDE.md` gates).

A standalone, lightweight **local watcher** that polls or subscribes to a message/inbox/event
**source** and emits one event per new item into whatever agent harness/runtime is running —
broadly compatible with all the agent tools people use. The client-side liveness complement to
server-side push/wake systems.

**Design + plan:** `../docs/DESIGN.md`. **Reference seed:** `../reference/ladybug_inbox_watch.py`.

**Planned v1 (per DESIGN §6):** "generic core + Kijito reference adapter" — a single
zero-dependency stdlib script: a config-driven `http-poll` source adapter + `stdout-jsonl` and
`exec-per-event` emit modes + the robustness contract (cursor dedup, alert-on-N-failures,
`--self-test`) + an SSRF guard on user-supplied source URLs.

**Name + license:** TBD in the harden phase (DESIGN §6 has candidates). The tool is agnostic — the
published name should not be Kijito-branded.
