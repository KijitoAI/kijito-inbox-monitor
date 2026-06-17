#!/usr/bin/env python3
"""KijitoMonitor — client-side liveness watcher for the Kijito inbox.

A standalone, zero-dependency (Python stdlib only) process that polls the Kijito inbox and emits one event per new
message into whatever harness is running — NDJSON on stdout and/or by exec-ing a command per event. It keeps a
*running* agent's inbox live by waking it BETWEEN tool calls (the LLM-UX inbox-liveness fix). It is NOT a server.

This is the v1 reference implementation of docs/DESIGN.md (rev 5). POSIX target (Linux/macOS); on Windows it runs
interval-only (no SIGUSR1 seam, no flock). Build/behaviour invariants — see DESIGN.md sections cited inline.
"""
import argparse
import datetime
import errno
import http.client
import ipaddress
import json
import os
import select
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

try:
    import fcntl  # POSIX only
except ImportError:  # pragma: no cover - Windows
    fcntl = None

SOURCE = "kijito-inbox"
DEFAULT_KIJITO_URL = "http://127.0.0.1:7474/api/inbox"
EXEC_TIMEOUT = 10
HTTP_TIMEOUT = 5  # §8 per-request timeout default
IS_POSIX = os.name == "posix"


# --------------------------------------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------------------------------------
class FatalConfig(Exception):
    """A fatal startup/config error → exit non-zero (NOT a per-poll liveness failure)."""


# --------------------------------------------------------------------------------------------------------------------
# §7.3 Canonical identity (computed BEFORE DNS resolution; trivial URL variations must not flip it)
# --------------------------------------------------------------------------------------------------------------------
def canonical_identity(url):
    p = urllib.parse.urlsplit(url)
    scheme = (p.scheme or "http").lower()
    host = (p.hostname or "").lower()
    port = p.port or (443 if scheme == "https" else 80)
    path = (p.path or "/").rstrip("/") or "/"
    # sort query params; the constant mark_read is excluded so its presence can't flip identity.
    # Use LISTS (not tuples) so the identity is JSON-round-trip stable — a persisted identity reloads
    # as lists, and the freshly-computed one must compare EQUAL (tuples would reload as lists → spurious
    # mismatch → restart-resume silently re-baselines, defeating the state-file).
    q = sorted([k, v] for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if k != "mark_read")
    return [scheme, host, port, path, q]


# --------------------------------------------------------------------------------------------------------------------
# §8 SSRF guard — applies to a user-supplied --url only; the Kijito loopback reference is exempt.
# --------------------------------------------------------------------------------------------------------------------
def _ip_is_internal(ip):
    a = ipaddress.ip_address(ip)
    return (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved
            or a.is_multicast or a.is_unspecified)


def resolve_and_pin(host, port, allow_loopback, allow_private):
    """Resolve ALL IPs for host; reject if ANY is internal (unless explicitly allowed). Return the pinned IP.

    Rejecting if ANY resolved address is internal kills decimal/hex literals and DNS-rebind. Returns the first IP
    to pin the connection to (no re-resolve at connect time = no TOCTOU)."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise FatalConfig("cannot resolve host %r: %s" % (host, e))
    ips = []
    for fam, _, _, _, sockaddr in infos:
        ips.append(sockaddr[0])
    for ip in ips:
        a = ipaddress.ip_address(ip)
        if _ip_is_internal(ip):
            if a.is_loopback and allow_loopback:
                continue
            if (a.is_private or a.is_link_local) and allow_private:
                continue
            raise FatalConfig("SSRF guard: host %r resolves to internal address %s (use --allow-loopback/"
                              "--allow-private to override)" % (host, ip))
    return ips[0]


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, pinned_ip=None, timeout=HTTP_TIMEOUT, **kw):
        super().__init__(host, timeout=timeout, **kw)
        self._pinned_ip = pinned_ip

    def connect(self):
        ip = self._pinned_ip or self.host
        self.sock = socket.create_connection((ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, pinned_ip=None, timeout=HTTP_TIMEOUT, **kw):
        super().__init__(host, timeout=timeout, **kw)
        self._pinned_ip = pinned_ip

    def connect(self):
        ip = self._pinned_ip or self.host
        sock = socket.create_connection((ip, self.port), self.timeout)
        ctx = self._context or ssl.create_default_context()
        # connect to the pinned IP but verify the cert against the real hostname (SNI preserved)
        self.sock = ctx.wrap_socket(sock, server_hostname=self.host)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are never followed (independent of the allow-flags) — §8."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_opener(pinned_ip):
    class _PinnedHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(lambda h, **kw: _PinnedHTTPConnection(h, pinned_ip=pinned_ip, **kw), req)

    class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(lambda h, **kw: _PinnedHTTPSConnection(h, pinned_ip=pinned_ip, **kw), req)

    return urllib.request.build_opener(_NoRedirect, _PinnedHTTPHandler, _PinnedHTTPSHandler)


# --------------------------------------------------------------------------------------------------------------------
# §5 http-poll adapter — peek + shape-validate + classify healthy/failure
# --------------------------------------------------------------------------------------------------------------------
class Poll:
    """Result of one fetch. ok=True → HEALTHY (items is the validated list). ok=False → liveness FAILURE."""
    def __init__(self, ok, items=None, reason=None, status=None, redirected=False):
        self.ok = ok
        self.items = items
        self.reason = reason
        self.status = status
        self.redirected = redirected


def fetch(opener, url, headers):
    """One peek fetch. Returns a Poll. A poll is HEALTHY iff 2xx AND parses AND shape-valid (§5)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            status = resp.status
            body = resp.read()
    except urllib.error.HTTPError as e:
        # _NoRedirect makes 3xx raise here as well as 4xx/5xx
        if 300 <= e.code < 400:
            return Poll(False, reason="redirect", status=e.code, redirected=True)
        return Poll(False, reason="http %d" % e.code, status=e.code)
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        return Poll(False, reason="unreachable: %s" % e)
    if not (200 <= status < 300):
        return Poll(False, reason="http %d" % status, status=status)
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as e:
        return Poll(False, reason="parse-fail: %s" % e, status=status)
    if not isinstance(data, dict) or not isinstance(data.get("result"), list):
        return Poll(False, reason="shape-invalid: result is not a list", status=status)
    items = data["result"]
    for m in items:
        if not isinstance(m, dict) or not isinstance(m.get("id"), int):
            return Poll(False, reason="shape-invalid: row missing integer id", status=status)
    return Poll(True, items=items, status=status)


def cheap_unread(opener, count_url, headers, persona):
    """§9 fast-path pre-check: GET /api/notify/pending (read-only, never marks read; River PR#66).
    Returns (available, unread_count). available=False if the endpoint is absent / non-2xx / bad shape →
    the caller falls back to the full inbox-list poll. Response: {"result":[{persona,unread,unread_urgent}]};
    a persona with zero unread is ABSENT from the list → treat absent as 0."""
    req = urllib.request.Request(count_url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            if not (200 <= resp.status < 300):
                return (False, 0)
            data = json.loads(resp.read())
    except Exception:
        return (False, 0)
    rows = data.get("result") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return (False, 0)
    for row in rows:
        if isinstance(row, dict) and row.get("persona") == persona:
            u = row.get("unread")
            return (True, u if isinstance(u, int) else 0)
    return (True, 0)  # persona absent from the list → zero unread, endpoint IS available


# --------------------------------------------------------------------------------------------------------------------
# §6 Emit
# --------------------------------------------------------------------------------------------------------------------
def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class Emitter:
    def __init__(self, mode, exec_cmd, content_chars, no_content):
        self.mode = mode
        self.exec_cmd = exec_cmd
        self.content_chars = content_chars
        self.no_content = no_content

    def _clip(self, content):
        if self.no_content:
            return None
        s = "" if content is None else str(content)
        return s[: self.content_chars]

    def emit(self, event):
        """event: dict already containing event/source/ts and type-specific fields."""
        if self.mode == "stdout-jsonl":
            sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        else:  # exec-per-event
            env = dict(os.environ)
            env["KIJITOMON_EVENT"] = str(event.get("event", ""))
            env["KIJITOMON_SOURCE"] = str(event.get("source", ""))
            env["KIJITOMON_TS"] = str(event.get("ts", ""))
            keymap = {
                "id": "KIJITOMON_ID", "from": "KIJITOMON_FROM", "content": "KIJITOMON_CONTENT",
                "created": "KIJITOMON_CREATED", "cursor": "KIJITOMON_CURSOR",
                "reason": "KIJITOMON_REASON", "consecutive_failures": "KIJITOMON_FAILURES",
                "seeded": "KIJITOMON_SEEDED", "current_max": "KIJITOMON_CURRENT_MAX",
                "capped_to": "KIJITOMON_CAPPED_TO", "dropped": "KIJITOMON_DROPPED",
            }
            for k, envname in keymap.items():
                if k in event and event[k] is not None:
                    env[envname] = str(event[k])
            try:
                subprocess.run(self.exec_cmd, shell=True, env=env, timeout=EXEC_TIMEOUT, check=False)
            except subprocess.TimeoutExpired:
                sys.stderr.write("kijito-monitor: exec timed out (non-fatal): %s\n" % self.exec_cmd)
            except Exception as e:  # non-fatal — watch continues, cursor already advanced
                sys.stderr.write("kijito-monitor: exec failed (non-fatal): %s\n" % e)

    # convenience constructors (carry the canonical fields; ts stamped at emit time)
    def new(self, m):
        ev = {"event": "new", "source": SOURCE, "ts": _now_iso(), "id": m.get("id"),
              "from": m.get("from"), "created": m.get("created")}
        c = self._clip(m.get("content"))
        if c is not None:
            ev["content"] = c
        self.emit(ev)

    def lifecycle(self, event, **fields):
        ev = {"event": event, "source": SOURCE, "ts": _now_iso()}
        ev.update(fields)
        self.emit(ev)


# --------------------------------------------------------------------------------------------------------------------
# §7.3 State file (canonical identity + flock + atomic write + resume)
# --------------------------------------------------------------------------------------------------------------------
class StateFile:
    def __init__(self, path, identity):
        self.path = path
        self.identity = identity
        self._lockf = None

    def lock(self):
        if not IS_POSIX or fcntl is None:
            return  # Windows: no lock (documented; run a single instance)
        # Lock a DEDICATED .lock SIDECAR, never the state-file itself: save() replaces the state-file's inode
        # (mkstemp + os.replace) on every poll, which would orphan a flock held on it and let a second watcher
        # lock the new inode freely. The sidecar is never replaced, so the flock persists for the process
        # lifetime. flock is advisory + auto-released by the OS on exit (no stale lockfile to clean).
        self._lockf = open(self.path + ".lock", "a+")
        try:
            fcntl.flock(self._lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise FatalConfig("state-file in use (another watcher holds the lock): %s" % self.path)

    def load(self):
        """Return (cursor, state, failures) on a VALID identity-matching file; None if absent/invalid (fall through).
        Raises FatalConfig on a present-but-unreadable path."""
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r") as f:
                raw = f.read()
        except OSError as e:
            raise FatalConfig("state-file unreadable: %s" % e)
        if not raw.strip():
            return None
        try:
            d = json.loads(raw)
            cursor = d["cursor"]
            state = d["state"]
            failures = d["consecutive_failures"]
            ident = d["identity"]
        except (ValueError, KeyError, TypeError):
            return None  # schema-invalid → treat as absent
        if not ((cursor is None or isinstance(cursor, int)) and state in ("UP", "DOWN")
                and isinstance(failures, int)):
            return None
        if ident != self.identity:
            sys.stderr.write("kijito-monitor: WARNING state-file identity mismatch (%r != %r) — NOT resuming its "
                             "cursor; re-baselining to avoid a silently-blind watcher.\n" % (ident, self.identity))
            return None
        return (cursor, state, failures)

    def save(self, cursor, state, failures):
        if not IS_POSIX:
            return  # best-effort; skip on Windows
        d = {"identity": self.identity, "cursor": cursor, "state": state, "consecutive_failures": failures}
        dirn = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp = tempfile.mkstemp(dir=dirn, prefix=".kijmon-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(d, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except OSError as e:
            sys.stderr.write("kijito-monitor: WARNING state-file write failed (non-fatal): %s\n" % e)
            try:
                os.unlink(tmp)
            except OSError:
                pass


# --------------------------------------------------------------------------------------------------------------------
# §10 SIGUSR1 self-pipe (POSIX) + clean shutdown
# --------------------------------------------------------------------------------------------------------------------
class WakeSeam:
    def __init__(self):
        self.r = self.w = None
        self.stop = False

    def install(self):
        if not IS_POSIX:
            return
        self.r, self.w = socket.socketpair()
        self.r.setblocking(False)
        self.w.setblocking(False)
        signal.set_wakeup_fd(self.w.fileno())
        # a real (no-op) handler must be installed or the default disposition terminates the process
        signal.signal(signal.SIGUSR1, lambda *_: None)
        # clean shutdown: flip stop flag and let select wake (set_wakeup_fd writes the byte)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_stop)

    def _on_stop(self, *_):
        self.stop = True

    def drain(self):
        if self.r is None:
            return
        try:
            while True:
                if not self.r.recv(4096):
                    break
        except (BlockingIOError, OSError):
            pass

    def wait(self, timeout):
        """Block up to timeout, returning early if a signal byte arrives. Drain happens at the next poll start."""
        if self.r is None:
            # Windows / no seam: plain sleep, but stay interruptible-ish via short slices
            end = _monotonic() + timeout
            while _monotonic() < end and not self.stop:
                time_sleep(min(0.5, end - _monotonic()))
            return
        try:
            select.select([self.r], [], [], timeout)
        except (InterruptedError, OSError):
            pass


def _monotonic():
    import time as _t
    return _t.monotonic()


def time_sleep(s):
    import time as _t
    _t.sleep(max(0.0, s))


# --------------------------------------------------------------------------------------------------------------------
# Core watcher
# --------------------------------------------------------------------------------------------------------------------
def build_headers(args):
    token = None
    if args.token_file:  # --token-file wins over env
        try:
            with open(args.token_file) as f:
                token = f.read().strip()
        except OSError as e:
            raise FatalConfig("--token-file unreadable: %s" % e)
    elif os.environ.get("KIJITOMON_TOKEN"):
        token = os.environ["KIJITOMON_TOKEN"].strip()
    if not token:
        return {}
    if args.auth_header:
        return {args.auth_header: token}
    return {"Authorization": "Bearer %s" % token}


def effective_url(args):
    """Return (url, is_reference). The reference is the Kijito loopback inbox for --persona."""
    if args.url:
        # ensure mark_read=false is present (peek) on a user --url too
        return _ensure_peek(args.url), False
    if not args.persona:
        raise FatalConfig("either --persona or --url is required")
    base = DEFAULT_KIJITO_URL
    return "%s?persona=%s&mark_read=false" % (base, urllib.parse.quote(args.persona)), True


def _ensure_peek(url):
    p = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(p.query, keep_blank_values=True))
    q["mark_read"] = "false"
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), p.fragment))


def make_opener_for(url, is_reference, args):
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    port = p.port or (443 if p.scheme == "https" else 80)
    # reference loopback is exempt → allow loopback for it
    allow_loopback = args.allow_loopback or is_reference
    pinned = resolve_and_pin(host, port, allow_loopback, args.allow_private)
    return build_opener(pinned)


def run(args):
    url, is_reference = effective_url(args)
    identity = canonical_identity(url)
    headers = build_headers(args)
    opener = make_opener_for(url, is_reference, args)  # raises FatalConfig on SSRF/destination violation
    is_user_url = not is_reference
    emitter = Emitter(args.emit, args.exec, args.content_chars, args.no_content)

    # ---- self-test (§7.2): run once, exit -------------------------------------------------------------------------
    if args.self_test:
        poll = fetch(opener, url, headers)
        reach_ok = poll.ok
        sys.stderr.write("self-test: source %s (%s)\n" % ("REACHABLE+healthy" if reach_ok else "UNHEALTHY",
                                                          poll.reason or "ok"))
        emit_ok = True
        try:
            emitter.new({"id": 0, "from": "self-test", "content": "synthetic emit OK", "created": _now_iso()})
        except Exception as e:
            emit_ok = False
            sys.stderr.write("self-test: emit FAILED: %s\n" % e)
        sys.stderr.write("self-test: emit=%s reachable=%s\n" % ("OK" if emit_ok else "FAIL", reach_ok))
        return 0 if (reach_ok and emit_ok) else 1

    # ---- state file: lock + resume --------------------------------------------------------------------------------
    state_file = None
    cursor = None          # None == UNSET
    fsm_state = "UP"
    failures = 0
    armed = False
    if args.state_file:
        state_file = StateFile(args.state_file, identity)
        state_file.lock()
        loaded = state_file.load()
        if loaded is not None:
            r_cursor, r_state, r_failures = loaded
            cursor = r_cursor            # may be overridden by --seed-at below
            fsm_state, failures = r_state, r_failures  # FSM always resumes on a valid match (§7.0)
    # --seed-at overrides the cursor only (FSM still resumes from a valid state-file)
    if args.seed_at is not None:
        cursor = args.seed_at

    seam = WakeSeam()
    seam.install()

    # §9 all-unread fast-path (River PR#66): a cheap O(1) /api/notify/pending pre-check. Enabled ON ARM iff the
    # endpoint is available; the inbox-list poll stays the BASELINE. SAFE by construction — the max-id cursor is
    # the source of truth for WHAT to emit; `unread` only decides WHETHER to do the full fetch (a stale/absent
    # hint can at worst cost an extra fetch, never a missed/duplicate message). `unread` only rises on genuinely
    # new mail (Kijito has no un-read op).
    cp = urllib.parse.urlsplit(url)
    count_url = urllib.parse.urlunsplit((cp.scheme, cp.netloc, "/api/notify/pending", "", ""))
    my_persona = dict(urllib.parse.parse_qsl(cp.query)).get("persona") or args.persona
    fast_path = False
    last_unread = None
    skips = 0  # consecutive fast-path skips; bounded by --resync-every (safety floor against a stale count)

    first_poll = True
    proc_start = _monotonic()
    last_heartbeat = proc_start

    while not seam.stop:
        seam.drain()  # read-and-clear at START of poll (§10)

        # fast-path pre-check: skip the full fetch when unread has NOT increased — but NEVER skip more than
        # --resync-every consecutive polls. The floor is a SAFETY net: a stale/wrong/unsupported count (e.g. a
        # daemon running pre-all-unread code that returns 0 forever) can then at worst add latency, never blind
        # the watcher. On a correct endpoint, mail is caught immediately on the unread increase.
        skip_full = False
        if armed and fast_path and not args.no_fast_path:
            avail, unread = cheap_unread(opener, count_url, headers, my_persona)
            if avail:
                increased = unread > last_unread if last_unread is not None else True
                last_unread = unread
                if not increased and skips < args.resync_every:
                    skip_full = True
                    skips += 1
            # unavailable (transient) → fall through to the full inbox-list poll (the baseline)

        if skip_full:
            # count endpoint reachable + no unread increase = a HEALTHY poll with no new items
            if fsm_state == "DOWN":
                fsm_state = "UP"
                emitter.lifecycle("recovered", cursor=cursor)
            failures = 0
        else:
            skips = 0  # a full fetch resets the skip floor
            poll = fetch(opener, url, headers)

            # mid-run vs startup classification of redirect / hive-off-404 (§5/§8)
            if poll.redirected and is_user_url and first_poll:
                raise FatalConfig("SSRF guard: --url returned a redirect (refused)")
            if poll.status == 404 and (first_poll or args.self_test):
                raise FatalConfig("inbox endpoint 404 (hive disabled?) — fatal at startup")

            if poll.ok:
                # ---- FSM healthy edge -----------------------------------------------------------------------------
                recovered = False
                if fsm_state == "DOWN":
                    fsm_state = "UP"
                    recovered = True
                failures = 0

                items = poll.items
                diag = None          # ('seed_ahead'|'replay_capped', fields)
                new_items = []
                do_arm = not armed

                if do_arm:
                    if cursor is None:
                        # UNSET baseline: emit nothing, exempt from cap
                        cursor = max((m["id"] for m in items), default=0)
                    else:
                        # non-null re-arm (seed-at or non-null state resume)
                        current_max = max((m["id"] for m in items), default=0)
                        n = sum(1 for m in items if m["id"] > cursor)
                        if cursor > current_max:
                            diag = ("seed_ahead", {"seeded": cursor, "current_max": current_max})
                        elif n > args.max_replay:
                            diag = ("replay_capped", {"capped_to": current_max, "dropped": n})
                            cursor = current_max
                        else:
                            new_items = sorted((m for m in items if m["id"] > cursor), key=lambda m: m["id"])
                    armed = True
                else:
                    new_items = sorted((m for m in items if m["id"] > cursor), key=lambda m: m["id"])

                # ---- emit in canonical within-poll order (§6.1) -----------------------------------------------
                if recovered:
                    emitter.lifecycle("recovered", cursor=cursor)
                if diag:
                    emitter.lifecycle(diag[0], **diag[1])
                if do_arm:
                    emitter.lifecycle("armed", cursor=cursor)
                for m in new_items:
                    emitter.new(m)
                if new_items:
                    cursor = max(cursor if cursor is not None else 0, max(m["id"] for m in new_items))

                # enable the fast-path once, right after arming (probe the count endpoint — §9)
                if do_arm and not args.no_fast_path:
                    avail, unread = cheap_unread(opener, count_url, headers, my_persona)
                    if avail:
                        fast_path = True
                        last_unread = unread
            else:
                # ---- FSM failure ----------------------------------------------------------------------------------
                failures += 1
                if failures == args.alert_after and fsm_state == "UP":
                    fsm_state = "DOWN"
                    emitter.lifecycle("alert", reason=poll.reason or "unreachable",
                                      consecutive_failures=failures, seconds=failures * args.poll_seconds)

        if state_file is not None:
            state_file.save(cursor, fsm_state, failures)

        # ---- heartbeat (proves the WATCHER is alive; HEALTHY or failed) ------------------------------------------
        if args.heartbeat and (_monotonic() - last_heartbeat) >= args.heartbeat:
            emitter.lifecycle("heartbeat", cursor=cursor)
            last_heartbeat = _monotonic()

        first_poll = False
        if seam.stop:
            break
        seam.wait(args.poll_seconds)

    return 0


# --------------------------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="kijito-monitor",
                                description="Client-side liveness watcher for the Kijito inbox (see DESIGN.md).")
    p.add_argument("--persona", help="Kijito persona whose inbox to watch (required unless --url given).")
    p.add_argument("--url", help="Destination override (still Kijito-shaped); SSRF-guarded.")
    p.add_argument("--allow-loopback", action="store_true", help="Permit a loopback --url destination.")
    p.add_argument("--allow-private", action="store_true", help="Permit a private/link-local --url destination.")
    p.add_argument("--poll-seconds", type=int, default=60)
    p.add_argument("--alert-after", type=int, default=3, help="Consecutive failures before an alert (min 1).")
    p.add_argument("--emit", choices=("stdout-jsonl", "exec-per-event"), default="stdout-jsonl")
    p.add_argument("--exec", help="Command to run per event (required iff --emit exec-per-event).")
    p.add_argument("--content-chars", type=int, default=220)
    p.add_argument("--no-content", action="store_true", help="Omit message content entirely (opaque mode).")
    p.add_argument("--seed-at", type=int, help="Cursor seed = last-handled id (overrides a state-file cursor).")
    p.add_argument("--max-replay", type=int, default=50, help="Cap on a re-arm backlog before fast-forwarding.")
    p.add_argument("--state-file", help="Persist+resume cursor/FSM; single-writer locked. Recommended w/ a supervisor.")
    p.add_argument("--heartbeat", type=int, help="Emit a heartbeat event every N seconds (external dead-man's-switch).")
    p.add_argument("--auth-header", help="Header NAME for the token (default Authorization: Bearer).")
    p.add_argument("--token-file", help="File holding the auth token (wins over $KIJITOMON_TOKEN).")
    p.add_argument("--no-fast-path", action="store_true",
                   help="Disable the /api/notify/pending unread pre-check; always full-poll the inbox list.")
    p.add_argument("--resync-every", type=int, default=10,
                   help="Fast-path safety floor: force a full inbox poll after at most N consecutive cheap "
                        "skips, so a stale/wrong unread count can never blind the watcher (default 10, min 1).")
    p.add_argument("--self-test", action="store_true", help="Probe + synthetic emit, then exit (run before trusting).")
    return p


def validate_args(args):
    if args.alert_after < 1:
        raise FatalConfig("--alert-after must be >= 1")
    if args.resync_every < 1:
        raise FatalConfig("--resync-every must be >= 1")
    if args.emit == "exec-per-event" and not args.exec:
        raise FatalConfig("--exec is required when --emit exec-per-event")
    if args.emit != "exec-per-event" and args.exec:
        sys.stderr.write("kijito-monitor: WARNING --exec ignored (emit mode is %s)\n" % args.emit)
    if not args.url and not args.persona:
        raise FatalConfig("either --persona or --url is required")


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        validate_args(args)
        return run(args)
    except FatalConfig as e:
        sys.stderr.write("kijito-monitor: FATAL %s\n" % e)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
