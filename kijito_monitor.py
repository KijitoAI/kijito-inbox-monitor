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


def fetch_personas(opener, headers):
    """Fetch the local account persona directory for default/explicit all-persona mode."""
    req = urllib.request.Request("http://127.0.0.1:7474/api/personas", headers=headers, method="GET")
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            if not (200 <= resp.status < 300):
                raise FatalConfig("/api/personas returned http %d" % resp.status)
            data = json.loads(resp.read())
    except FatalConfig:
        raise
    except Exception as e:
        raise FatalConfig("cannot fetch /api/personas for --all-personas: %s" % e)
    rows = data.get("result") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise FatalConfig("/api/personas shape-invalid: result is not a list")
    personas = []
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("persona"), str) and row["persona"]:
            personas.append(row["persona"])
    if not personas:
        raise FatalConfig("/api/personas returned no personas")
    return personas


def fetch_unread_counts(opener, count_url, headers):
    """§9 fast-path pre-check: GET /api/notify/pending once and fan out locally.

    Returns (available, {persona: unread_count}). available=False if the endpoint is absent / non-2xx / bad shape →
    callers fall back to the full inbox-list poll. Response: {"result":[{persona,unread,unread_urgent}]};
    a persona with zero unread is ABSENT from the list → callers treat absent as 0.
    """
    req = urllib.request.Request(count_url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            if not (200 <= resp.status < 300):
                return (False, {})
            data = json.loads(resp.read())
    except Exception:
        return (False, {})
    rows = data.get("result") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return (False, {})
    counts = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("persona"), str):
            u = row.get("unread")
            counts[row["persona"]] = u if isinstance(u, int) else 0
    return (True, counts)


# --------------------------------------------------------------------------------------------------------------------
# §6 Emit
# --------------------------------------------------------------------------------------------------------------------
def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class RotatingFileSink:
    """Owns the events-log fd and rotates it by size IN-PROCESS, so the writer reopens after its OWN rename.

    Why this exists: a launchd StandardOutPath fd is NEVER reopened by launchd when an external rotator
    (newsyslog) renames the file — the producer would keep appending to the orphaned inode while a `tail -F`
    consumer follows the new empty file → SILENT blinding (the exact failure class this tool fights). Owning
    the fd here and reopening after our OWN rename closes that hole with no external dependency and no sudo;
    consumers just tail -F by name. max_bytes <= 0 disables rotation (unbounded)."""
    def __init__(self, path, max_bytes, keep):
        self.path = path
        self.max_bytes = max_bytes
        self.keep = max(1, keep)
        self._fh = None
        self._open()

    def _open(self):
        dirn = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(dirn, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8")

    def write(self, line):
        self._fh.write(line)
        self._fh.flush()
        self._maybe_rotate()

    def _maybe_rotate(self):
        if self.max_bytes <= 0:
            return
        try:
            size = os.fstat(self._fh.fileno()).st_size
        except OSError:
            return
        if size < self.max_bytes:
            return
        try:
            self._fh.close()
            oldest = "%s.%d" % (self.path, self.keep)
            if os.path.exists(oldest):
                os.remove(oldest)
            for i in range(self.keep - 1, 0, -1):
                src = "%s.%d" % (self.path, i)
                if os.path.exists(src):
                    os.replace(src, "%s.%d" % (self.path, i + 1))
            if os.path.exists(self.path):
                os.replace(self.path, "%s.1" % self.path)
        except OSError as e:
            sys.stderr.write("kijito-monitor: WARNING log rotation failed (non-fatal): %s\n" % e)
        finally:
            self._open()  # always reopen by NAME — a tail -F consumer follows us onto the fresh file

    def close(self):
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None


class Emitter:
    def __init__(self, mode, exec_cmd, content_chars, no_content, sink=None, suppress_authors=None,
                 sink_template=None, max_bytes=0, keep=5):
        self.mode = mode
        self.exec_cmd = exec_cmd
        self.content_chars = content_chars
        self.no_content = no_content
        self.sink = sink  # single shared RotatingFileSink (--events-file), else None (→ stdout)
        self.suppress_authors = set(suppress_authors or [])  # drop self-echo 'new' events from these authors
        # --events-file-template: one OWNED RotatingFileSink PER PERSONA, so a session subscribes to ONLY its
        # own mail by `tail -F events.<persona>.ndjson` — no shared-file grep to invent (the [1988] LLM-UX class).
        self.sink_template = sink_template
        self._max_bytes = max_bytes
        self._keep = keep
        self._sinks_by_persona = {}

    def _sink_for(self, persona):
        """Route an event to its persona's sink (template mode), the single shared sink, or stdout (None)."""
        if self.sink_template is None:
            return self.sink
        key = persona or "_all"  # events with no persona (e.g. a bare --url target) land in one _all file
        s = self._sinks_by_persona.get(key)
        if s is None:
            path = self.sink_template.replace("{persona}", _state_safe_persona(key))
            s = RotatingFileSink(path, self._max_bytes, self._keep)
            self._sinks_by_persona[key] = s
        return s

    def close(self):
        if self.sink is not None:
            self.sink.close()
        for s in self._sinks_by_persona.values():
            s.close()

    def _clip(self, content):
        if self.no_content:
            return None
        s = "" if content is None else str(content)
        return s[: self.content_chars]

    def emit(self, event):
        """event: dict already containing event/source/ts and type-specific fields."""
        if self.mode == "stdout-jsonl":
            line = json.dumps(event, ensure_ascii=False) + "\n"
            sink = self._sink_for(event.get("persona"))
            if sink is not None:
                sink.write(line)
            else:
                sys.stdout.write(line)
                sys.stdout.flush()
        else:  # exec-per-event
            env = dict(os.environ)
            env["KIJITOMON_EVENT"] = str(event.get("event", ""))
            env["KIJITOMON_SOURCE"] = str(event.get("source", ""))
            env["KIJITOMON_TS"] = str(event.get("ts", ""))
            keymap = {
                "id": "KIJITOMON_ID", "from": "KIJITOMON_FROM", "content": "KIJITOMON_CONTENT",
                "created": "KIJITOMON_CREATED", "cursor": "KIJITOMON_CURSOR",
                "persona": "KIJITOMON_PERSONA",
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
        if self.suppress_authors and m.get("from") in self.suppress_authors:
            return  # --suppress-author: don't wake on an event WE authored (self-echo noise). Cursor still advances.
        ev = {"event": "new", "source": SOURCE, "ts": _now_iso(), "id": m.get("id"),
              "from": m.get("from"), "created": m.get("created")}
        if m.get("_persona"):
            ev["persona"] = m.get("_persona")
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
        dirn = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(dirn, exist_ok=True)
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
        os.makedirs(dirn, exist_ok=True)
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
    raise FatalConfig("internal error: effective_url() needs --url; use persona_url() for Kijito personas")


def persona_url(persona):
    return "%s?persona=%s&mark_read=false" % (DEFAULT_KIJITO_URL, urllib.parse.quote(persona)), True


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


def _state_path_for_persona(base_path, persona):
    if not base_path or not persona:
        return base_path
    root, ext = os.path.splitext(base_path)
    safe = _state_safe_persona(persona)
    base = os.path.basename(root)
    if base == safe or base.endswith("." + safe):
        return base_path
    return root + "." + safe + (ext or ".json")


def _state_safe_persona(persona):
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in persona)


def requested_personas(args, opener, headers):
    personas = []
    for p in (p.strip() for p in args.persona or []):
        if p and p not in personas:
            personas.append(p)
    for group in args.personas or []:
        for p in (part.strip() for part in group.split(",")):
            if p and p not in personas:
                personas.append(p)
    if args.all_personas or not personas:
        for p in fetch_personas(opener, headers):
            if p not in personas:
                personas.append(p)
    return personas


def watches_all_personas(args):
    return not args.url and (args.all_personas or not (args.persona or args.personas))


def new_personas(existing, discovered):
    seen = set(existing)
    return [p for p in discovered if p not in seen]


class WatchTarget:
    def __init__(self, persona, url, is_reference, opener, headers, args, emitter):
        self.persona = persona
        self.url = url
        self.is_reference = is_reference
        self.is_user_url = not is_reference
        self.opener = opener
        self.headers = headers
        self.args = args
        self.emitter = emitter
        self.identity = canonical_identity(url)
        self.state_file = None
        self.cursor = None
        self.fsm_state = "UP"
        self.failures = 0
        self.armed = False
        self.fast_path = False
        self.last_unread = None
        self.skips = 0
        self.first_poll = True
        self.last_heartbeat = _monotonic()

        cp = urllib.parse.urlsplit(url)
        self.count_url = urllib.parse.urlunsplit((cp.scheme, cp.netloc, "/api/notify/pending", "", ""))
        self.unread_persona = dict(urllib.parse.parse_qsl(cp.query)).get("persona") or persona

        state_path = _state_path_for_persona(args.state_file, persona)
        if state_path:
            self.state_file = StateFile(state_path, self.identity)
            if not args.self_test:
                self.state_file.lock()
                loaded = self.state_file.load()
                if loaded is not None:
                    r_cursor, r_state, r_failures = loaded
                    self.cursor = r_cursor
                    self.fsm_state, self.failures = r_state, r_failures
        if args.seed_at is not None:
            self.cursor = args.seed_at

    def self_test(self):
        poll = fetch(self.opener, self.url, self.headers)
        reach_ok = poll.ok
        label = self.persona or self.url
        sys.stderr.write("self-test[%s]: source %s (%s)\n" % (
            label, "REACHABLE+healthy" if reach_ok else "UNHEALTHY", poll.reason or "ok"
        ))
        emit_ok = True
        try:
            self.emitter.new({"id": 0, "from": "self-test", "content": "synthetic emit OK",
                              "created": _now_iso(), "_persona": self.persona})
        except Exception as e:
            emit_ok = False
            sys.stderr.write("self-test[%s]: emit FAILED: %s\n" % (label, e))
        sys.stderr.write("self-test[%s]: emit=%s reachable=%s\n" % (
            label, "OK" if emit_ok else "FAIL", reach_ok
        ))
        return reach_ok and emit_ok

    def lifecycle(self, event, **fields):
        if self.persona:
            fields["persona"] = self.persona
        self.emitter.lifecycle(event, **fields)

    def poll_once(self, counts_available=False, unread_counts=None):
        args = self.args
        unread_counts = unread_counts or {}

        skip_full = False
        if self.armed and self.fast_path and not args.no_fast_path and self.unread_persona:
            if counts_available:
                unread = unread_counts.get(self.unread_persona, 0)
                increased = unread > self.last_unread if self.last_unread is not None else True
                self.last_unread = unread
                if not increased and self.skips < args.resync_every:
                    skip_full = True
                    self.skips += 1
            # unavailable (transient) → fall through to the full inbox-list poll (the baseline)

        if skip_full:
            # count endpoint reachable + no unread increase = a HEALTHY poll with no new items
            if self.fsm_state == "DOWN":
                self.fsm_state = "UP"
                self.lifecycle("recovered", cursor=self.cursor)
            self.failures = 0
        else:
            self.skips = 0
            poll = fetch(self.opener, self.url, self.headers)

            if poll.redirected and self.is_user_url and self.first_poll:
                raise FatalConfig("SSRF guard: --url returned a redirect (refused)")
            if poll.status == 404 and (self.first_poll or args.self_test):
                raise FatalConfig("inbox endpoint 404 (hive disabled?) — fatal at startup")

            if poll.ok:
                recovered = False
                if self.fsm_state == "DOWN":
                    self.fsm_state = "UP"
                    recovered = True
                self.failures = 0

                items = poll.items
                diag = None
                new_items = []
                do_arm = not self.armed

                if do_arm:
                    if self.cursor is None:
                        self.cursor = max((m["id"] for m in items), default=0)
                    else:
                        current_max = max((m["id"] for m in items), default=0)
                        n = sum(1 for m in items if m["id"] > self.cursor)
                        if self.cursor > current_max:
                            diag = ("seed_ahead", {"seeded": self.cursor, "current_max": current_max})
                        elif n > args.max_replay:
                            diag = ("replay_capped", {"capped_to": current_max, "dropped": n})
                            self.cursor = current_max
                        else:
                            new_items = sorted((m for m in items if m["id"] > self.cursor), key=lambda m: m["id"])
                    self.armed = True
                else:
                    new_items = sorted((m for m in items if m["id"] > self.cursor), key=lambda m: m["id"])

                if recovered:
                    self.lifecycle("recovered", cursor=self.cursor)
                if diag:
                    self.lifecycle(diag[0], **diag[1])
                if do_arm:
                    self.lifecycle("armed", cursor=self.cursor)
                for m in new_items:
                    m = dict(m)
                    m["_persona"] = self.persona
                    self.emitter.new(m)
                if new_items:
                    self.cursor = max(self.cursor if self.cursor is not None else 0,
                                      max(m["id"] for m in new_items))

            else:
                self.failures += 1
                if self.failures == args.alert_after and self.fsm_state == "UP":
                    self.fsm_state = "DOWN"
                    self.lifecycle("alert", reason=poll.reason or "unreachable",
                                   consecutive_failures=self.failures,
                                   seconds=self.failures * args.poll_seconds)

        if self.state_file is not None:
            self.state_file.save(self.cursor, self.fsm_state, self.failures)

        # §9 enable the fast-path once — on the first healthy poll where the count endpoint is available.
        # (Single enable point; the max-id cursor stays the source of truth for WHAT to emit, unread is only
        # the wake TRIGGER, so a late/again enable is harmless.)
        if self.armed and not self.fast_path and not args.no_fast_path and self.unread_persona and counts_available:
            self.fast_path = True
            self.last_unread = unread_counts.get(self.unread_persona, 0)

        if args.heartbeat and (_monotonic() - self.last_heartbeat) >= args.heartbeat:
            self.lifecycle("heartbeat", cursor=self.cursor)
            self.last_heartbeat = _monotonic()

        self.first_poll = False


def build_persona_target(persona, opener_by_origin, headers, args, emitter):
    url, is_reference = persona_url(persona)
    origin = urllib.parse.urlsplit(url).netloc
    opener = opener_by_origin.get(origin)
    if opener is None:
        opener = make_opener_for(url, is_reference, args)
        opener_by_origin[origin] = opener
    return WatchTarget(persona, url, is_reference, opener, headers, args, emitter)


def discover_persona_targets(args, headers, emitter, targets, opener_by_origin, directory_opener):
    current = [t.persona for t in targets if t.persona]
    discovered = fetch_personas(directory_opener, headers)
    added = []
    for persona in new_personas(current, discovered):
        try:
            target = build_persona_target(persona, opener_by_origin, headers, args, emitter)
        except FatalConfig as e:
            sys.stderr.write("kijito-monitor: WARNING cannot add persona %r: %s\n" % (persona, e))
            continue
        targets.append(target)
        added.append(persona)
        target.lifecycle("persona_added")
    return added


def run(args):
    headers = build_headers(args)
    sink = None
    sink_template = None
    if not args.self_test and args.emit == "stdout-jsonl":
        if args.events_file_template:
            sink_template = args.events_file_template  # one sink per persona (lazily created on first event)
        elif args.events_file:
            sink = RotatingFileSink(args.events_file, args.max_bytes, args.keep_logs)
    emitter = Emitter(args.emit, args.exec, args.content_chars, args.no_content, sink=sink,
                      suppress_authors=args.suppress_author, sink_template=sink_template,
                      max_bytes=args.max_bytes, keep=args.keep_logs)
    directory_opener = None
    opener_by_origin = {}

    if args.url:
        url, is_reference = effective_url(args)
        opener = make_opener_for(url, is_reference, args)
        targets = [WatchTarget(None, url, is_reference, opener, headers, args, emitter)]
    else:
        directory_url = "http://127.0.0.1:7474/api/personas"
        directory_opener = make_opener_for(directory_url, True, args)
        personas = requested_personas(args, directory_opener, headers)
        if not personas:
            raise FatalConfig("at least one persona is required")
        targets = [build_persona_target(p, opener_by_origin, headers, args, emitter) for p in personas]

    # ---- self-test (§7.2): run once, exit -------------------------------------------------------------------------
    if args.self_test:
        ok = True
        for target in targets:
            ok = target.self_test() and ok
        return 0 if ok else 1

    seam = WakeSeam()
    seam.install()
    rediscover_at = _monotonic() + args.rediscover_every

    while not seam.stop:
        seam.drain()  # read-and-clear at START of poll (§10)
        if watches_all_personas(args) and directory_opener is not None and _monotonic() >= rediscover_at:
            try:
                discover_persona_targets(args, headers, emitter, targets, opener_by_origin, directory_opener)
            except FatalConfig as e:
                sys.stderr.write("kijito-monitor: WARNING persona rediscovery failed: %s\n" % e)
            rediscover_at = _monotonic() + args.rediscover_every
        counts_available = False
        unread_counts = {}
        count_target = next((t for t in targets if t.unread_persona), None)
        if count_target is not None and not args.no_fast_path:
            counts_available, unread_counts = fetch_unread_counts(
                count_target.opener, count_target.count_url, headers
            )
        for target in targets:
            target.poll_once(counts_available, unread_counts)
        if seam.stop:
            break
        seam.wait(args.poll_seconds)

    emitter.close()
    return 0


# --------------------------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(prog="kijito-monitor",
                                description="Client-side liveness watcher for the Kijito inbox (see DESIGN.md).")
    p.add_argument("--persona", action="append",
                   help="Kijito persona whose inbox to watch. Repeat for multi-persona mode.")
    p.add_argument("--personas", action="append",
                   help="Comma-separated personas to watch, e.g. codex,river,ladybug.")
    p.add_argument("--all-personas", action="store_true",
                   help="Watch every persona returned by /api/personas on the local Kijito daemon (default).")
    p.add_argument("--rediscover-every", type=int, default=600,
                   help="In all-persona mode, re-scan /api/personas every N seconds and add newly-created personas "
                        "(default 600, min 1). Explicit persona subsets are not expanded.")
    p.add_argument("--url", help="Destination override (still Kijito-shaped); SSRF-guarded.")
    p.add_argument("--allow-loopback", action="store_true", help="Permit a loopback --url destination.")
    p.add_argument("--allow-private", action="store_true", help="Permit a private/link-local --url destination.")
    p.add_argument("--poll-seconds", type=int, default=60)
    p.add_argument("--alert-after", type=int, default=3, help="Consecutive failures before an alert (min 1).")
    p.add_argument("--emit", choices=("stdout-jsonl", "exec-per-event"), default="stdout-jsonl")
    p.add_argument("--exec", help="Command to run per event (required iff --emit exec-per-event).")
    p.add_argument("--suppress-author", action="append",
                   help="Do not emit 'new' events authored by this persona (repeatable) — drops the self-echo you "
                        "get when watching all personas AND sending mail. Liveness events are unaffected.")
    p.add_argument("--content-chars", type=int, default=220)
    p.add_argument("--no-content", action="store_true", help="Omit message content entirely (opaque mode).")
    p.add_argument("--events-file",
                   help="Write NDJSON events to this file (an OWNED, size-rotated fd) instead of stdout — the "
                        "supervised-producer mode that survives log rotation. Consumers tail -F it. "
                        "Only applies to --emit stdout-jsonl.")
    p.add_argument("--events-file-template",
                   help="Per-persona supervised mode: write EACH persona's events to its OWN owned, size-rotated "
                        "file, e.g. ~/.cache/kijito-monitor/events.{persona}.ndjson — a session then subscribes "
                        "to only its own mail with `tail -F events.<persona>.ndjson`, no filtering. Must contain "
                        "'{persona}'. Mutually exclusive with --events-file.")
    p.add_argument("--max-bytes", type=int, default=5_000_000,
                   help="Rotate the events file(s) once one reaches N bytes (default 5000000; <=0 disables).")
    p.add_argument("--keep-logs", type=int, default=5,
                   help="How many rotated --events-file archives to keep (default 5, min 1).")
    p.add_argument("--seed-at", type=int, help="Cursor seed = last-handled id (overrides a state-file cursor).")
    p.add_argument("--max-replay", type=int, default=50, help="Cap on a re-arm backlog before fast-forwarding.")
    p.add_argument("--state-file",
                   help="Persist+resume cursor/FSM; single-writer locked. Kijito persona targets derive one "
                        "file per persona from this base path. Recommended w/ a supervisor.")
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
    if args.rediscover_every < 1:
        raise FatalConfig("--rediscover-every must be >= 1")
    if args.emit == "exec-per-event" and not args.exec:
        raise FatalConfig("--exec is required when --emit exec-per-event")
    if args.emit != "exec-per-event" and args.exec:
        sys.stderr.write("kijito-monitor: WARNING --exec ignored (emit mode is %s)\n" % args.emit)
    if args.url and (args.persona or args.personas or args.all_personas):
        raise FatalConfig("--url cannot be combined with --persona/--personas/--all-personas")
    if args.poll_seconds < 1:
        raise FatalConfig("--poll-seconds must be >= 1")  # 0 → a select(timeout=0) busy-loop hammering the source
    if args.heartbeat is not None and args.heartbeat < 1:
        raise FatalConfig("--heartbeat must be >= 1")
    if args.content_chars < 0:
        raise FatalConfig("--content-chars must be >= 0")
    if args.max_replay < 0:
        raise FatalConfig("--max-replay must be >= 0")
    if args.keep_logs < 1:
        raise FatalConfig("--keep-logs must be >= 1")
    if args.events_file and args.events_file_template:
        raise FatalConfig("--events-file and --events-file-template are mutually exclusive")
    if args.events_file_template and "{persona}" not in args.events_file_template:
        raise FatalConfig("--events-file-template must contain the '{persona}' placeholder")
    if (args.events_file or args.events_file_template) and args.emit != "stdout-jsonl":
        sys.stderr.write("kijito-monitor: WARNING --events-file/-template ignored (emit mode is %s)\n" % args.emit)
    if args.seed_at is not None:
        single = bool(args.url) or (len(args.persona or []) == 1 and not args.personas and not args.all_personas)
        if not single:
            raise FatalConfig("--seed-at requires a single target (one --persona or --url), "
                              "not multi-persona/all-personas — each persona has its own cursor")


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
