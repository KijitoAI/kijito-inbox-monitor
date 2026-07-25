#!/usr/bin/env python3
"""Kijito Inbox Monitor - client-side liveness watcher for your Kijito inbox.

A standalone, zero-dependency (Python stdlib only) process that polls your Kijito inbox at api.kijito.ai and emits
one event per new message into whatever harness is running - NDJSON on stdout and/or by exec-ing a command per
event. It keeps a *running* agent's inbox live by waking it BETWEEN tool calls (the LLM-UX inbox-liveness fix). It
is NOT a server.

Authentication is required: set $KIJITOMON_TOKEN (or --token-file) to your Kijito API token. POSIX target
(Linux/macOS); on Windows it runs interval-only (no SIGUSR1 seam, no flock). See docs/DESIGN.md for the design.
"""
import argparse
import datetime
import errno
import http.client
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

__version__ = "0.3.0"
SOURCE = "kijito-inbox"
# A named User-Agent is REQUIRED: api.kijito.ai is fronted by a WAF that 403s the default Python-urllib UA.
USER_AGENT = "kijito-inbox-monitor/%s" % __version__
KIJITO_BASE = "https://api.kijito.ai"
INBOX_URL = KIJITO_BASE + "/api/inbox"
PERSONAS_URL = KIJITO_BASE + "/api/personas"
NOTIFY_PENDING_URL = KIJITO_BASE + "/api/notify/pending"
EXEC_TIMEOUT = 10
HTTP_TIMEOUT = 5  # per-request timeout default (normal fetches)
LONGPOLL_SLACK = 10  # client socket timeout = server hold (--wait) + this, so a half-open hold is always detected
LONGPOLL_BACKOFF_CAP = 30  # cap (s) on exponential backoff between failed long-poll attempts
PIN_TRACKING_CAP = 5000    # max delivered ids remembered above a pinned watermark (bounds the state file)
WALK_BACK_MAX_PAGES = 50   # page budget for an authoritative backward walk over an omitted span
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
    # Use LISTS (not tuples) so the identity is JSON-round-trip stable - a persisted identity reloads
    # as lists, and the freshly-computed one must compare EQUAL (tuples would reload as lists → spurious
    # mismatch → restart-resume silently re-baselines, defeating the state-file).
    q = sorted([k, v] for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if k != "mark_read")
    return [scheme, host, port, path, q]


# --------------------------------------------------------------------------------------------------------------------
# Connection hardening - resolve-once + pin the IP (no TOCTOU re-resolve), and never follow redirects.
# The destination is the fixed Kijito API host, so there is no user-supplied URL to guard; pinning + no-redirect
# remain as defense-in-depth against DNS games and redirect surprises.
# --------------------------------------------------------------------------------------------------------------------
def resolve_and_pin(host, port):
    """Resolve host and return the first IP to pin the connection to (no re-resolve at connect time = no TOCTOU)."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise FatalConfig("cannot resolve host %r: %s" % (host, e))
    return infos[0][4][0]


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
    """Redirects are never followed - a redirect is treated as an unhealthy poll."""
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
# §5 http-poll adapter - peek + shape-validate + classify healthy/failure
# --------------------------------------------------------------------------------------------------------------------
def _is_int(v):
    """A REAL integer. `bool` is a subclass of int in Python, so True would otherwise satisfy every
    isinstance(x, int) check in this file and then behave as 1 - a malformed row id, a malformed
    size_dropped and a malformed persisted cursor all slipped through that way (Loom re-audit 6)."""
    return isinstance(v, int) and not isinstance(v, bool)


_MISSING = object()   # "the server did not send this field at all", distinct from an explicit null


class Poll:
    """Result of one fetch. ok=True → HEALTHY (items is the validated list). ok=False → liveness FAILURE.

    `omitted` carries the server's OWN declaration that this window is incomplete. The inbox endpoint
    returns the NEWEST messages that fit a count limit AND an aggregate content budget, and reports what
    it left out via truncated / size_truncated / size_dropped. Discarding those fields is how a bounded
    window turns into permanent mail loss: items the server omitted are never emitted, and the cursor
    then advances past them. The truncation is not silent in the DATA - only in the handling of it.
    """
    def __init__(self, ok, items=None, reason=None, status=None, redirected=False, omitted=0,
                 omitted_exact=True, next_before_id=None, continuation_ok=True):
        self.ok = ok
        self.items = items
        self.reason = reason
        self.status = status
        self.redirected = redirected
        self.omitted = omitted             # >0 iff the server said this window is incomplete
        self.omitted_exact = omitted_exact  # False => `omitted` is only a LOWER BOUND, never closable by count
        self.next_before_id = next_before_id  # backward cursor; None when nothing older was withheld
        # False when the server's continuation was ABSENT or MALFORMED - i.e. it never answered. Distinct
        # from next_before_id=None, which is the server AFFIRMING there is nothing older. A walk may treat
        # only the affirmation as terminal; silence is a contract violation and must pin.
        self.continuation_ok = continuation_ok


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
    if not isinstance(data, dict):
        return Poll(False, reason="shape-invalid: body is not an object", status=status)
    # ONE implementation of the body contract, shared with the tests. Two copies of a rule this subtle is
    # two chances to get it wrong, and the tests would then be exercising the copy production does not use.
    return fetch_from_payload(data, status=status)


def fetch_from_payload(data, status=200):
    """Build a Poll from an already-decoded body. The validation path fetch() uses, exposed so tests can
    exercise the CONTRACT (absent vs null vs malformed continuation) rather than construct Polls by hand -
    a hand-built Poll bypasses exactly the checks under test."""
    items = data.get("result")
    if not isinstance(items, list):
        return Poll(False, reason="shape-invalid: result is not a list", status=status)
    seen_ids = set()
    for m in items:
        if not isinstance(m, dict) or not _is_int(m.get("id")):
            # `bool` is a subclass of int, so an id of True would otherwise pass and then compare as 1.
            return Poll(False, reason="shape-invalid: row missing integer id", status=status)
        if m["id"] in seen_ids:
            # A page cannot legitimately carry the same id twice, and the cursor logic dedupes only
            # against what it has ALREADY delivered - so a repeat inside one window is emitted twice.
            return Poll(False, reason="shape-invalid: duplicate id %s in one page" % m["id"], status=status)
        seen_ids.add(m["id"])
    n, exact = _declared_omissions(data)
    nb_raw = data.get("next_before_id", _MISSING)
    if nb_raw is None:
        nb, nb_ok = None, True
    elif isinstance(nb_raw, int) and not isinstance(nb_raw, bool) and nb_raw >= 0:
        nb, nb_ok = nb_raw, True
    else:
        nb, nb_ok = None, False
    return Poll(True, items=items, status=status, omitted=n, omitted_exact=exact,
                next_before_id=nb, continuation_ok=nb_ok)


def _declared_omissions(data):
    """How many messages the server says it left out of this window (0 if it says none).

    Returns (count, exact). `exact` is False when the server signalled a truncation WITHOUT saying how
    many rows it withheld - then `count` is only a LOWER BOUND, and no amount of recovered mail can prove
    the span empty, because there is no number to reach. A gap with an inexact count must stay pinned
    until an authoritative backward read can walk it; counting rows against a lower bound would let one
    recovered message "close" an unbounded hole.

    An alarm that invents losses is as corrosive as one that hides them, so this must not round in
    either direction. THREE DISTINCT SIGNALS, and conflating them is wrong BOTH ways:
      truncated=True                    -> rows withheld by the COUNT limit, quantity NOT stated -> inexact.
      size_dropped=N                    -> exactly N rows withheld by the content budget -> exact.
      size_truncated=True, size_dropped=0 -> a lone oversized message had its BODY clipped. No row was
                                           withheld, so this contributes NOTHING. Verified live: a
                                           limit=3 request returns truncated=True with size_dropped=0
                                           and rows genuinely missing, while an oversized single message
                                           reports size_truncated with nothing dropped.
    """
    n, exact = 0, True
    trunc = data.get("truncated", _MISSING)
    if trunc is True:
        n, exact = n + 1, False        # count-limit truncation never states a quantity
    elif trunc is not _MISSING and trunc is not False:
        # A truncation flag that is neither true nor false is UNINTERPRETABLE, and reading it as "no
        # omission" is the one direction that loses mail. Treat it as an unquantified withholding.
        n, exact = max(n, 1), False
    dropped = data.get("size_dropped")
    if _is_int(dropped):
        n += max(dropped, 0)
    else:
        st = data.get("size_truncated", _MISSING)
        if st is True:
            n, exact = max(n, 1), False    # size truncation with no number at all
        elif st is not _MISSING and st is not False:
            n, exact = max(n, 1), False    # same rule: an uninterpretable flag is not a denial
    return (n, exact)


# Memory count per persona, refreshed on every directory fetch. Used by the stranded-mail check to ask
# "does anyone actually OWN this inbox", which survives a directory that lists every registered recipient.
# None (not 0) means the server did not report a count, so the check must not infer anything from it.
_PERSONA_MEMORY_COUNTS = {}


def _row_memory_count(row):
    """Memories owned by this persona, or None if the server did not say.

    Prefers the top-level `memory_count`. Deliberately does NOT fall back to summing `projects[].count`:
    project counts exclude GLOBAL-scoped memories, so a persona whose memories are all global sums to
    zero and looks unowned. Measured live: maestro sums to 0 across projects but owns 61 memories; the
    same gap exists for codex, ladybug, leadgen, omniview, quill, sterling and vellum. Summing the wrong
    field would have made the alarm cry wolf about half the fleet.
    """
    n = row.get("memory_count")
    return n if isinstance(n, int) and n >= 0 else None


def fetch_personas(opener, headers):
    """Fetch the account persona directory for default/explicit all-persona mode."""
    req = urllib.request.Request(PERSONAS_URL, headers=headers, method="GET")
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
            _PERSONA_MEMORY_COUNTS[row["persona"]] = _row_memory_count(row)
    if not personas:
        raise FatalConfig("/api/personas returned no personas")
    return personas


def _parse_unread_rows(data):
    """Parse a /api/notify/pending body into {persona: unread}, or None if the shape is invalid.
    A persona with zero unread is ABSENT from the list → callers treat absent as 0."""
    rows = data.get("result") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    counts = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("persona"), str):
            u = row.get("unread")
            counts[row["persona"]] = u if isinstance(u, int) else 0
    return counts


def fetch_unread_counts(opener, count_url, headers):
    """§9 fast-path pre-check: GET /api/notify/pending once and fan the counts out in-process.

    Returns (available, {persona: unread_count}). available=False if the endpoint is absent / non-2xx / bad shape →
    callers fall back to the full inbox-list poll. Response: {"result":[{persona,unread,unread_urgent}]}.
    """
    req = urllib.request.Request(count_url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=HTTP_TIMEOUT) as resp:
            if not (200 <= resp.status < 300):
                return (False, {})
            data = json.loads(resp.read())
    except Exception:
        return (False, {})
    counts = _parse_unread_rows(data)
    if counts is None:
        return (False, {})
    return (True, counts)


def fetch_unread_counts_longpoll(opener, headers, wait, cursor):
    """Long-poll variant of the fast-path. GET /api/notify/pending?wait=<sec>[&cursor=<opaque>].

    The server holds the request up to `wait` seconds, returning the instant the account's mail-state advances
    beyond `cursor` (else on timeout). Returns (available, {persona: unread}, cursor):
    - `cursor` is the server's OPAQUE token to echo on the next call - NEVER parse it.
    - available=False on any connection error / non-2xx / bad shape → the caller falls back to the full inbox poll
      and RECONNECTS WITH THE SAME cursor (lossless resume across a wifi/NAT/Cloudflare/server-restart drop).
    - cursor is None when the server did NOT long-poll (no `cursor` field): the endpoint predates long-poll, so the
      caller interval-polls. This makes the client safe to ship BEFORE the server supports it - it interval-polls
      today and auto-upgrades to instant the moment a cursor starts coming back, no redeploy.
    The client socket timeout is wait+LONGPOLL_SLACK so a half-open held connection is detected, never hung.
    """
    q = {"wait": str(wait)}
    if cursor is not None:
        q["cursor"] = cursor
    url = NOTIFY_PENDING_URL + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=wait + LONGPOLL_SLACK) as resp:
            if not (200 <= resp.status < 300):
                return (False, {}, cursor)
            data = json.loads(resp.read())
    except Exception:
        return (False, {}, cursor)  # keep the old cursor → next attempt resumes losslessly
    counts = _parse_unread_rows(data)
    if counts is None:
        return (False, {}, cursor)
    new_cursor = data.get("cursor")
    if not isinstance(new_cursor, str) or not new_cursor:
        new_cursor = None  # server didn't long-poll → caller interval-polls (forward/back-compat)
    return (True, counts, new_cursor)


# --------------------------------------------------------------------------------------------------------------------
# §6 Emit
# --------------------------------------------------------------------------------------------------------------------
def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class RotatingFileSink:
    """Owns the events-log fd and rotates it by size IN-PROCESS, so the writer reopens after its OWN rename.

    Why this exists: a launchd StandardOutPath fd is NEVER reopened by launchd when an external rotator
    (newsyslog) renames the file - the producer would keep appending to the orphaned inode while a `tail -F`
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
            sys.stderr.write("kijito-inbox-monitor: WARNING log rotation failed (non-fatal): %s\n" % e)
        finally:
            self._open()  # always reopen by NAME - a tail -F consumer follows us onto the fresh file

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
        # own mail by `tail -F events.<persona>.ndjson` - no shared-file grep to invent (the inbox-liveness LLM-UX problem).
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
                "stranded_inboxes": "KIJITOMON_STRANDED",
            }
            for k, envname in keymap.items():
                if k in event and event[k] is not None:
                    v = event[k]
                    # A list is comma-joined, not str()'d: a Python repr ("['a', 'b']") is unusable from a
                    # shell consumer, and exec-per-event is the portable primitive people reach for first.
                    env[envname] = ",".join(str(x) for x in v) if isinstance(v, list) else str(v)
            try:
                subprocess.run(self.exec_cmd, shell=True, env=env, timeout=EXEC_TIMEOUT, check=False)
            except subprocess.TimeoutExpired:
                sys.stderr.write("kijito-inbox-monitor: exec timed out (non-fatal): %s\n" % self.exec_cmd)
            except Exception as e:  # non-fatal - watch continues, cursor already advanced
                sys.stderr.write("kijito-inbox-monitor: exec failed (non-fatal): %s\n" % e)

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
# A state file that EXISTS but cannot be trusted. Distinct from None (genuinely absent) because the two
# demand opposite behaviour: absent means baseline, corrupt means fail closed and re-emit.
CORRUPT_STATE = object()


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
        """Return the resumed state on a VALID identity-matching file; None if genuinely ABSENT.

        Raises FatalConfig on a present-but-unreadable path, and returns the CORRUPT sentinel on a file
        that EXISTS but cannot be trusted.

        ABSENT AND CORRUPT ARE NOT THE SAME ANSWER (Loom re-audit 5, HIGH 2). Both used to return None, so
        a garbled state file was indistinguishable from a first launch - and a first launch BASELINES to
        the newest visible id, silently skipping every message between the lost cursor and now. That is a
        permanent, invisible loss produced by the one event most likely to accompany a crash. A file that
        is present but unparseable is EVIDENCE THAT A CURSOR EXISTED, so it must fail closed and re-emit
        rather than fail open and skip. Duplicates are recoverable; skips are not.
        """
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r") as f:
                raw = f.read()
        except OSError as e:
            raise FatalConfig("state-file unreadable: %s" % e)
        if not raw.strip():
            # PRESENT BUT EMPTY IS NOT ABSENT (Loom re-audit 6, HIGH 4). A zero-byte file is still
            # evidence that a cursor existed here; treating it as a first launch baselines over
            # everything since. Same fail-open shape as an unparseable file, same answer.
            sys.stderr.write("kijito-inbox-monitor: WARNING state-file is present but EMPTY; refusing to "
                             "baseline over it: %s\n" % self.path)
            return CORRUPT_STATE
        try:
            d = json.loads(raw)
            cursor = d["cursor"]
            state = d["state"]
            failures = d["consecutive_failures"]
            ident = d["identity"]
        except (ValueError, KeyError, TypeError):
            sys.stderr.write("kijito-inbox-monitor: WARNING state-file is present but unparseable; refusing to "
                             "baseline over it (that would silently skip everything since the lost cursor): "
                             "%s\n" % self.path)
            return CORRUPT_STATE
        if not ((cursor is None or _is_int(cursor)) and state in ("UP", "DOWN")
                and _is_int(failures)):
            sys.stderr.write("kijito-inbox-monitor: WARNING state-file has a valid envelope but invalid "
                             "fields; refusing to baseline over it: %s\n" % self.path)
            return CORRUPT_STATE
        if ident != self.identity:
            sys.stderr.write("kijito-inbox-monitor: WARNING state-file identity mismatch (%r != %r) - NOT resuming its "
                             "cursor; re-baselining to avoid a silently-blind watcher.\n" % (ident, self.identity))
            return None
        # Ids already emitted ABOVE a pinned watermark. Absent in files written by older versions, which is
        # exactly the forward-compat case: an empty set just means "nothing pinned", the pre-pinning behaviour.
        alerted = d.get("gap_alerted")
        alerted = alerted if isinstance(alerted, int) else None
        raw = d.get("emitted_above")
        if raw is None:
            emitted, intact = set(), True          # no pin was in force; the ordinary case
        elif isinstance(raw, list) and all(isinstance(i, int) for i in raw):
            emitted, intact = set(raw), True
        else:
            # CORRUPT PIN STATE MUST FAIL CLOSED. Loading it as an empty set silently UNPINS: the watcher
            # would then think nothing was outstanding, let the replay cap jump the cursor over the very
            # span the pin was protecting, and lose it. We cannot know which ids were delivered, so we
            # keep the pin (empty tracking) and mark the evidence unusable - the gap can then only be
            # closed by an authoritative read, never by counting.
            sys.stderr.write("kijito-inbox-monitor: WARNING state-file 'emitted_above' is malformed; "
                             "keeping the watermark PINNED with no delivery tracking rather than "
                             "silently unpinning: %s\n" % self.path)
            emitted, intact = set(), False
        # A recorded gap alert with no pin tracking is itself inconsistent: something was pinned when the
        # file was written. Treat it the same way - hold the pin rather than assume it resolved.
        if alerted is not None and not emitted and intact and raw is None:
            intact = False
        # THE PIN'S OWN STATE IS PERSISTED (Loom re-audit 6, HIGH 1). It used to be inferred from
        # `emitted_above`, which is empty in exactly the case that matters - a corrupt-state pin, where
        # nothing has been tracked yet. So a restart lost the pin, the replay cap was free again, and the
        # very span the pin was protecting got crossed on the first poll. A pin that does not survive a
        # restart is not a pin; the crash is when you need it.
        d_intact = d.get("pin_evidence_intact")
        if d_intact is False:
            intact = False
        return {"cursor": cursor, "state": state, "failures": failures, "emitted_above": emitted,
                "gap_alerted": alerted, "pin_evidence_intact": intact,
                "pin_forced": d.get("pin_forced") is True,
                "state_corrupt": d.get("state_corrupt") is True}

    def save(self, cursor, state, failures, emitted_above=None, gap_alerted=None,
             pin_forced=False, pin_evidence_intact=True, state_corrupt=False):
        if not IS_POSIX:
            return  # best-effort; skip on Windows
        d = {"identity": self.identity, "cursor": cursor, "state": state, "consecutive_failures": failures}
        # Persisted so a RESTART cannot re-emit what we already delivered above a pinned watermark.
        # Without this, failing closed would trade silent loss for a duplicate storm on every restart.
        if emitted_above:
            d["emitted_above"] = sorted(emitted_above)
        # Persisted too, so a restart does not re-announce a gap it already announced.
        if gap_alerted is not None:
            d["gap_alerted"] = gap_alerted
        # The pin's own state, persisted rather than inferred. `emitted_above` is EMPTY for a
        # corrupt-state pin, so inferring from it silently dropped exactly the pin that matters.
        if pin_forced:
            d["pin_forced"] = True
        if not pin_evidence_intact:
            d["pin_evidence_intact"] = False
        if state_corrupt:
            d["state_corrupt"] = True
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
            sys.stderr.write("kijito-inbox-monitor: WARNING state-file write failed (non-fatal): %s\n" % e)
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
    """Resolve the required Kijito API token. --token-file wins over $KIJITOMON_TOKEN; missing token is fatal.

    Every request carries a named User-Agent - the Kijito API WAF rejects the default Python-urllib UA with 403.
    """
    headers = {"User-Agent": USER_AGENT}
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
        raise FatalConfig("no Kijito API token - set $KIJITOMON_TOKEN or pass --token-file (get a token from "
                          "your Kijito account)")
    if args.auth_header:
        headers[args.auth_header] = token
    else:
        headers["Authorization"] = "Bearer %s" % token
    return headers


def persona_url(persona):
    return "%s?persona=%s&mark_read=false" % (INBOX_URL, urllib.parse.quote(persona))


def make_opener_for(url):
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    port = p.port or (443 if p.scheme == "https" else 80)
    pinned = resolve_and_pin(host, port)
    return build_opener(pinned)


def _state_path_for_persona(base_path, persona):
    if not base_path or not persona:
        return base_path
    root, ext = os.path.splitext(base_path)
    safe = _state_safe_persona(persona)
    base = os.path.basename(root).casefold()
    if base == safe or base.endswith("." + safe):
        return base_path
    return root + "." + safe + (ext or ".json")


def _state_safe_persona(persona):
    """Map a persona to a filename component - CASEFOLDED, deliberately.

    macOS (APFS) and Windows are case-INSENSITIVE, so 'Claude-chat' and 'claude-chat' name the SAME
    file. Deriving the path from the raw name made the producer block on its OWN flock every tick for
    a case-variant persona, and left that persona with no event stream at all - a SILENT wake gap,
    which is the exact failure this tool exists to prevent. Matching case-insensitively here is the
    filesystem half of the fix; the persona's ORIGINAL case is preserved for the API (persona_url),
    i.e. case-insensitive match, case-preserving display.
    """
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in persona.casefold())


_WARNED_PERSONAS = set()


def _warn_persona_once(persona, text):
    """Emit a per-persona warning at most ONCE per process.

    Persona discovery runs every tick, so a condition that cannot resolve itself (a state file held by
    another watcher, an unusable path) otherwise grows stderr without bound: one observed 3-day run had
    20,079 of 20,129 stderr lines from a single repeated warning, which buries every other diagnostic.
    """
    key = persona.casefold()
    if key in _WARNED_PERSONAS:
        return
    _WARNED_PERSONAS.add(key)
    sys.stderr.write("kijito-inbox-monitor: WARNING %s (further warnings for %r suppressed)\n"
                     % (text, persona))


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
    return args.all_personas or not (args.persona or args.personas)


def new_personas(existing, discovered):
    # Case-INSENSITIVE: a case-variant of a persona we already watch is the SAME inbox and (on a
    # case-insensitive filesystem) the same state file - adopting it again self-deadlocks. Also
    # collapses variants within `discovered`, keeping the first spelling seen.
    seen = {p.casefold() for p in existing}
    out = []
    for p in discovered:
        key = p.casefold()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


class WatchTarget:
    def __init__(self, persona, url, opener, headers, args, emitter):
        self.persona = persona
        self.url = url
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
        # FAIL-CLOSED state. `cursor` is a CONFIRMED-CONTIGUOUS watermark: everything at or below it is
        # known delivered. When the server admits it hid messages above the cursor, the watermark PINS
        # rather than stepping over them, and ids emitted above the pin are remembered here so liveness
        # (delivering what we can see) does not cost us duplicates. Both are persisted.
        self.emitted_above = set()
        self.gap_alerted = None   # the pinned watermark we have already alerted on, so pinning does not spam
        # False once we can no longer reason about the pinned span - tracking overflowed, or the persisted
        # pin state was corrupt. A gap can then only be closed by an authoritative read, never by counting.
        self.pin_evidence_intact = True
        self.pin_forced = False   # hold a pin whose tracking we lost, so nothing can jump the watermark
        self.state_corrupt = False  # a state file was PRESENT but unusable: arm fail-closed, and say so

        self.count_url = NOTIFY_PENDING_URL
        cp = urllib.parse.urlsplit(url)
        self.unread_persona = dict(urllib.parse.parse_qsl(cp.query)).get("persona") or persona

        state_path = _state_path_for_persona(args.state_file, persona)
        if state_path:
            self.state_file = StateFile(state_path, self.identity)
            if not args.self_test:
                self.state_file.lock()
                loaded = self.state_file.load()
                if loaded is CORRUPT_STATE:
                    # A file that EXISTS but cannot be parsed is EVIDENCE A CURSOR EXISTED. Baselining
                    # here would step over every message between that lost cursor and now, invisibly.
                    # So fail closed: keep no cursor, force the pin so the watermark cannot jump, and
                    # mark the evidence unusable. The first poll then emits everything visible (the
                    # replay cap is bypassed while pinned) and the gap is announced rather than buried.
                    self.state_corrupt = True
                    self.pin_forced = True
                    self.pin_evidence_intact = False
                elif loaded is not None:
                    self.cursor = loaded["cursor"]
                    self.fsm_state, self.failures = loaded["state"], loaded["failures"]
                    self.emitted_above = loaded["emitted_above"]
                    self.gap_alerted = loaded["gap_alerted"]
                    self.pin_evidence_intact = loaded["pin_evidence_intact"]
                    self.state_corrupt = loaded["state_corrupt"]
                    # A persisted forced pin is authoritative; the inference from missing tracking is only
                    # a fallback for files written before the flag existed.
                    self.pin_forced = loaded["pin_forced"] or not loaded["pin_evidence_intact"]
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

    def _uncovered_gap(self, poll, items):
        """(cursor, window_floor, omitted) iff omitted mail may sit ABOVE the cursor, else None.

        THE DISCRIMINATOR, and it is the whole reason this is not a permanent alarm:
          window_floor <= cursor  -> the window reaches back PAST what we already emitted, so every
                                     omitted message is BELOW the cursor and was already delivered. Safe.
          window_floor >  cursor  -> the window starts above the cursor while the server says it dropped
                                     things, so the uncovered span (cursor, window_floor) may hold mail
                                     we have never emitted. Unsafe.
        In steady state the long-poll keeps the backlog to a message or two, so the window always reaches
        back and this returns None - no behaviour change. It fires after an outage or a burst, which is
        exactly when a bounded window starts hiding things.
        """
        if not poll.omitted or self.cursor is None or not self.armed or not items:
            return None
        floor = min(m["id"] for m in items)
        if floor <= self.cursor:
            return None
        return (self.cursor, floor, poll.omitted, poll.omitted_exact)

    def _walk_back(self, from_id, stop_at):
        """Page BACKWARD over (stop_at, from_id) and return (rows, covered).

        This is the AUTHORITATIVE way to read an omitted span, and it replaces the unread_only
        heuristic entirely. Two properties the heuristic never had:
          · it reaches messages someone has already READ - the exact rows unread_only structurally
            cannot see, and the ones most likely to be hidden in an old span;
          · it TERMINATES, so the span can be declared covered by exhaustion rather than by counting
            recovered rows against a number the server may never have stated.
        That is what makes an INEXACT omission count closable at all.

        Contract (river, api main @249e2b3): pass the OLDEST id you were returned as `before_id` and
        repeat until the page is empty or `next_before_id` is null. OMIT the parameter for the newest
        page - 0 is a REAL cursor, not "no cursor". A malformed cursor is a hard 400, so a bug here
        fails loudly instead of silently re-serving the newest page.

        `covered` is True only if the walk reached stop_at or ran out of older messages. A walk cut
        short by the page budget returns False, and the caller must keep the watermark pinned: a
        partial walk proves nothing, and claiming otherwise is the very failure this replaced.

        THE CHAIN IS VALIDATED STRICTLY, NOT ASSUMED (Loom re-audit 5, HIGH 1). Coverage-by-exhaustion
        is only as good as the chain being a real chain, so every link is checked before it is trusted:
          · the continuation must BE AN ANSWER. A missing or malformed `next_before_id` is not an
            end-of-chain, it is silence, and reading silence as "nothing older" hands back coverage the
            server never asserted.
          · the continuation must EQUAL THE OLDEST ROW WE WERE HANDED. The contract is "pass the oldest
            id you were returned"; a server whose continuation points BELOW that is skipping the rows in
            between, and following it walks straight over them while reporting success.
        Neither check can be satisfied by accident, and both fail to PIN, which is the safe direction.
        """
        sep = "&" if "?" in self.url else "?"
        rows, cursor, pages = [], from_id, 0
        while pages < WALK_BACK_MAX_PAGES:
            pages += 1
            poll = fetch(self.opener, "%s%sbefore_id=%d" % (self.url, sep, cursor), self.headers)
            if not poll.ok:
                return (rows, False)          # transient failure: no claim either way
            if not poll.continuation_ok:
                # Absent or malformed continuation: the server did not answer. NOT exhaustion.
                return (rows, False)
            batch = poll.items or []
            rows.extend(batch)
            if poll.omitted and poll.next_before_id is None:
                # SELF-CONTRADICTORY PAGE (Loom re-audit 6, HIGH 3): it declares that rows were withheld
                # AND that nothing older exists. Both cannot be true, and believing the terminal half
                # closes a span over the very rows the other half just admitted to hiding.
                return (rows, False)
            if not batch:
                if poll.next_before_id is not None:
                    # EMPTY PAGE CLAIMING THERE IS MORE (Loom re-audit 6, HIGH 2). It returned nothing
                    # while pointing further back, so the range it covered is unobserved - and because
                    # the oldest-row check has no row to check, following the pointer walks straight
                    # over that range and still reports the span covered.
                    return (rows, False)
                return (rows, True)           # empty AND affirmed terminal: the chain genuinely ends
            oldest = min(m["id"] for m in batch)
            if oldest <= stop_at:
                return (rows, True)           # walked back past the watermark: span fully seen
            if poll.next_before_id is not None and poll.next_before_id != oldest:
                # The chain skips rows between `oldest` and the continuation. Following it would
                # walk over them and still report the span covered.
                return (rows, False)
            if poll.next_before_id is None:
                return (rows, True)           # server AFFIRMS there is nothing older
            if poll.next_before_id >= cursor:
                return (rows, False)          # cursor not advancing; refuse to spin
            cursor = poll.next_before_id
        return (rows, False)                  # budget exhausted before reaching the watermark

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

            if poll.status == 404 and (self.first_poll or args.self_test):
                raise FatalConfig("inbox endpoint 404 (hive disabled?) - fatal at startup")
            if poll.status == 401 and (self.first_poll or args.self_test):
                raise FatalConfig("inbox endpoint 401 (bad or missing token) - fatal at startup")

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
                    if self.cursor is None and self.state_corrupt:
                        # Fail CLOSED: arm BELOW everything visible and EMIT the whole window, rather than
                        # baselining to the newest id and skipping the lost span in silence. The replay cap
                        # is deliberately not applied - it exists to stop a huge first-run backlog, and
                        # here every visible message is one we may already owe someone.
                        self.cursor = min((m["id"] for m in items), default=0) - 1
                        new_items = sorted(items, key=lambda m: m["id"])
                        diag = ("state_corrupt", {"armed_at": self.cursor,
                                                  "reason": "state file present but unusable; re-emitting the "
                                                            "visible window instead of baselining over it"})
                    elif self.cursor is None:
                        self.cursor = max((m["id"] for m in items), default=0)
                    else:
                        current_max = max((m["id"] for m in items), default=0)
                        # A RESTORED PIN SURVIVES ARMING. `emitted_above` is only ever non-empty when a
                        # previous run pinned the watermark below an unresolved gap, so both branches below
                        # must respect it: the replay cap would otherwise jump the cursor straight over the
                        # gap on the first poll after a restart, silently erasing it, and the replay count
                        # would double-count mail we already delivered.
                        # `pin_forced` covers the case where the pin is real but its tracking was lost,
                        # so an empty emitted_above must NOT read as "nothing was pinned".
                        pinned_on_load = bool(self.emitted_above) or self.pin_forced
                        n = sum(1 for m in items if m["id"] > self.cursor)
                        if self.cursor > current_max:
                            diag = ("seed_ahead", {"seeded": self.cursor, "current_max": current_max})
                        elif n > args.max_replay and not pinned_on_load:
                            diag = ("replay_capped", {"capped_to": current_max, "dropped": n})
                            self.cursor = current_max
                            self.emitted_above = set()
                        else:
                            new_items = sorted((m for m in items
                                                if m["id"] > self.cursor
                                                and m["id"] not in self.emitted_above),
                                               key=lambda m: m["id"])
                    self.armed = True
                else:
                    # `emitted_above` is normally empty. It is non-empty only while the watermark is PINNED
                    # below an unresolved gap, and it is what lets us keep delivering visible mail without
                    # re-delivering it on every subsequent poll.
                    new_items = sorted((m for m in items
                                        if m["id"] > self.cursor and m["id"] not in self.emitted_above),
                                       key=lambda m: m["id"])

                if recovered:
                    self.lifecycle("recovered", cursor=self.cursor)
                if diag:
                    self.lifecycle(diag[0], **diag[1])
                if do_arm:
                    self.lifecycle("armed", cursor=self.cursor)
                # §5.1 A BOUNDED WINDOW MUST NOT SILENTLY SWALLOW MAIL.
                # The server returns the NEWEST messages that fit, and declares what it left out. If it
                # omitted anything AND the window does not reach back to our cursor, un-emitted mail can
                # be sitting in the uncovered gap - and advancing the cursor past it loses it forever.
                gap = self._uncovered_gap(poll, items)
                pinned = False
                if gap is not None:
                    cursor_at, window_floor, omitted, omitted_exact = gap
                    visible = {m["id"] for m in items}
                    # Count ONLY rows the visible window did not already contain and that sit above the
                    # watermark. Counting every returned row lets a retry that echoes the same suffix be
                    # reported as a recovery that never happened - a false success, worse than a loud failure.
                    # Walk the span BACKWARD from the window floor down to the watermark. Coverage is
                    # proven by exhausting the chain, not by counting rows against a number - which is
                    # why this closes an INEXACT omission count that no amount of counting could.
                    walked, covered = self._walk_back(window_floor, cursor_at)
                    unseen = [m for m in walked
                              if m["id"] > (self.cursor or 0) and m["id"] not in visible
                              and m["id"] not in self.emitted_above]
                    gap_recovered = [m for m in unseen if cursor_at < m["id"] < window_floor]
                    known = {m["id"] for m in new_items}
                    for m in unseen:
                        if m["id"] not in known:
                            new_items.append(m)
                            known.add(m["id"])
                    new_items.sort(key=lambda m: m["id"])

                    # FAIL CLOSED unless there is POSITIVE evidence the span is accounted for. Recovery here
                    # is a heuristic (unread_only), not an authoritative backward page, so silence from it
                    # proves nothing. Treat the gap as closed only when the reconciling window was itself
                    # COMPLETE (it declared no omissions of its own) and it yielded at least as many
                    # previously-unseen rows as the server said it withheld. Anything less pins the
                    # watermark: stepping over would make the next poll see floor<=cursor, declare itself
                    # safe, and bury the omission permanently.
                    # CLOSURE BY EXHAUSTION, not by arithmetic. A completed backward walk has SEEN the
                    # whole span, so the omission count - exact or not - stops mattering. A walk cut
                    # short proves nothing and keeps the watermark pinned.
                    # `pin_evidence_intact` still gates: once tracking has overflowed we cannot tell a
                    # recovered row from one we delivered and forgot, so we do not trust our own view of
                    # what is new until the walk itself re-establishes it.
                    closed = covered and (self.pin_evidence_intact or bool(walked))
                    if closed and not self.pin_evidence_intact:
                        # An authoritative read re-establishes ground truth, so the span is knowable again.
                        self.pin_evidence_intact = True
                    if closed:
                        # AND RELEASE THE FORCED PIN (Loom re-audit 5, MEDIUM). A forced pin was held
                        # because tracking was lost; a COMPLETED walk is the authoritative evidence that
                        # replaces it. Leaving it set froze the watermark permanently, and because a
                        # non-pinned poll never records emitted ids, every later window re-emitted the
                        # same mail forever - the duplicate storm the pin exists to prevent.
                        self.pin_forced = False
                    pinned = not closed
                    # Alert identity is the PINNED WATERMARK, not the window floor. The floor drifts upward
                    # as new mail arrives, so keying on it re-fires for what is the same unresolved span;
                    # the watermark is stable for exactly as long as the gap is unresolved. Persisted, so a
                    # restart does not re-announce it either.
                    if pinned and self.gap_alerted != cursor_at:
                        self.gap_alerted = cursor_at
                        self.lifecycle("alert",
                                       reason=("bounded-window: server omitted %d message(s) and the window "
                                               "started at id %s above cursor %s; a backward walk recovered %d "
                                               "from inside the span but did not reach the watermark, so "
                                               "it stays PINNED at %s"
                                               % (omitted, window_floor, cursor_at, len(gap_recovered),
                                                  cursor_at)),
                                       omitted=omitted, window_floor=window_floor, cursor_at=cursor_at,
                                       reconciled=len(gap_recovered), pinned=True)

                for m in new_items:
                    m = dict(m)
                    m["_persona"] = self.persona
                    self.emitter.new(m)
                if pinned:
                    # Watermark HELD. Remember what we delivered above it so the next poll (and any
                    # restart, since this is persisted) neither re-emits it nor forgets the gap.
                    self.emitted_above.update(m["id"] for m in new_items)
                    if len(self.emitted_above) > PIN_TRACKING_CAP:
                        # A pin that cannot clear would otherwise grow this set - and the state file -
                        # without bound. Keep the NEWEST ids (the ones a future window can still show us,
                        # and therefore the ones that could be re-emitted) and drop the oldest.
                        keep = sorted(self.emitted_above)[-PIN_TRACKING_CAP:]
                        dropped = len(self.emitted_above) - len(keep)
                        self.emitted_above = set(keep)
                        # ONCE WE HAVE FORGOTTEN A DELIVERED ID, WE CAN NO LONGER REASON ABOUT THIS SPAN.
                        # A forgotten id reappearing in a reconcile looks "previously unseen", so it would
                        # both re-emit AND be counted as recovery - manufacturing evidence out of our own
                        # amnesia. From here the gap can only be closed by an authoritative read.
                        if self.pin_evidence_intact:
                            self.pin_evidence_intact = False
                            # A durable event, not just stderr: this is a correctness degradation somebody
                            # has to act on, and stderr is not something a consumer watches.
                            self.lifecycle("alert",
                                           reason=("bounded-window: pin at cursor %s outlived its tracking "
                                                   "budget and forgot %d delivered id(s). Some mail may be "
                                                   "re-emitted, and this span can no longer be closed by "
                                                   "reconciliation - it needs an authoritative backward read"
                                                   % (self.cursor, dropped)),
                                           cursor_at=self.cursor, forgot=dropped,
                                           pinned=True, evidence_lost=True)
                else:
                    # A COMPLETE window that reaches back past the watermark proves everything above it is
                    # visible, so a leftover pin can be released even when there is nothing NEW to emit.
                    # Gating this on `new_items` left a restored pin stuck forever whenever the window
                    # contained only ids we had already delivered - the exact state a restart lands in.
                    reach = min((m["id"] for m in items), default=None)
                    complete = poll.omitted == 0 and reach is not None and reach <= (self.cursor or 0)
                    if self.pin_forced and complete:
                        # The other authoritative proof: nothing was withheld AND the window reaches back
                        # past the watermark, so there is no span left to be uncertain about. Without this
                        # a forced pin that never sees a gap again could never clear, and the watermark
                        # would stay frozen for the life of the process.
                        self.pin_forced = False
                    if self.pin_forced:
                        high = None           # still forced: the watermark holds
                    else:
                        high = max([m["id"] for m in new_items]
                                   + ([max(m["id"] for m in items)] if complete else []), default=None)
                    if high is not None and high > (self.cursor or 0):
                        self.cursor = high
                        # Watermark moved, so anything at or below it is confirmed and needs no tracking.
                        self.emitted_above = {i for i in self.emitted_above if i > self.cursor}
                        if not self.emitted_above:
                            self.gap_alerted = None
                            self.pin_evidence_intact = True

            else:
                self.failures += 1
                if self.failures == args.alert_after and self.fsm_state == "UP":
                    self.fsm_state = "DOWN"
                    self.lifecycle("alert", reason=poll.reason or "unreachable",
                                   consecutive_failures=self.failures,
                                   seconds=self.failures * args.poll_seconds)

        if self.state_file is not None:
            self.state_file.save(self.cursor, self.fsm_state, self.failures,
                                 self.emitted_above, self.gap_alerted,
                                 pin_forced=self.pin_forced,
                                 pin_evidence_intact=self.pin_evidence_intact,
                                 state_corrupt=self.state_corrupt)

        # §9 enable the fast-path once - on the first healthy poll where the count endpoint is available.
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
    url = persona_url(persona)
    origin = urllib.parse.urlsplit(url).netloc
    opener = opener_by_origin.get(origin)
    if opener is None:
        opener = make_opener_for(url)
        opener_by_origin[origin] = opener
    return WatchTarget(persona, url, opener, headers, args, emitter)


def discover_persona_targets(args, headers, emitter, targets, opener_by_origin, directory_opener):
    current = [t.persona for t in targets if t.persona]
    discovered = fetch_personas(directory_opener, headers)
    added = []
    # `discovered` is returned as well as used: it is the DIRECTORY namespace, which the stranded-mail
    # check diffs the inbox namespace against. Fetched here already, so the check costs no extra request.
    for persona in new_personas(current, discovered):
        try:
            target = build_persona_target(persona, opener_by_origin, headers, args, emitter)
        except FatalConfig as e:
            _warn_persona_once(persona, "cannot add persona %r: %s" % (persona, e))
            continue
        targets.append(target)
        added.append(persona)
        target.lifecycle("persona_added")
    return added, discovered


def discover_from_counts(args, counts, targets, opener_by_origin, headers, emitter):
    """Add a watch target for any persona that appears in the notify counts (i.e. has mail) but isn't watched yet.

    This is how a NEW persona is picked up within one tick of receiving mail - for free from the long-poll / fast-path
    counts we already fetch - instead of waiting for the periodic /api/personas rescan. Only auto-adds in all-personas
    mode; an explicit --persona/--personas subset stays fixed."""
    if not watches_all_personas(args):
        return []
    # Case-INSENSITIVE membership: see new_personas(). The counts come from the INBOX namespace, which
    # can legitimately hold a name the persona DIRECTORY does not (that divergence is what stranded mail
    # in the first place), so this is the path where case-variants actually show up.
    current = {t.persona.casefold() for t in targets if t.persona}
    added = []
    for persona in counts:
        if persona and persona.casefold() not in current:
            try:
                target = build_persona_target(persona, opener_by_origin, headers, args, emitter)
            except FatalConfig as e:
                _warn_persona_once(persona, "cannot add persona %r from counts: %s" % (persona, e))
                continue
            targets.append(target)
            added.append(persona)
            current.add(persona.casefold())
            target.lifecycle("persona_added")
    return added


_REPORTED_STRANDED = set()


def stranded_inboxes(directory, counts):
    """Inboxes holding unread mail that the persona DIRECTORY does not know about.

    Two namespaces exist and are populated by different paths: the DIRECTORY (who exists) and the INBOX
    (who can receive). When they diverge, mail lands in an inbox that nobody owns and nothing watches -
    it is never delivered, and nothing reports it, so the sender sees success and the recipient sees
    nothing. Both cases observed in the wild had this shape: a case-variant of a live persona, and a
    group-looking name ('all') with no broadcast semantics behind it. One held a substantive reply for
    14 days before anyone noticed.

    Compared EXACTLY, deliberately NOT casefolded. The SERVER's inbox namespace is case-SENSITIVE -
    verified: the 'Claude-chat' inbox held a different message set from 'claude-chat' - so a case-variant
    is a real, DISTINCT inbox holding real mail, and casefolding here would hide the very incident this
    check exists to catch.

    Note the deliberate asymmetry with _state_safe_persona(), which DOES casefold: the local filesystem
    is case-INSENSITIVE and cannot hold two state files for the two names, so the watcher can never adopt
    the variant. The rules are complementary rather than contradictory - the variant is unwatchable
    locally AND unwatched remotely, which is exactly why it has to be alarmed on instead of adopted.

    TWO SIGNALS, because directory membership alone stopped being sufficient. A server may build its
    directory as a UNION that includes every registered RECIPIENT - and a recipient is registered the
    moment anyone sends to that name, typo included. On such a server every future phantom is "in the
    directory" instantly and absence can never fire again. So an inbox also counts as stranded when it
    holds mail while owning ZERO memories: nothing has ever written as that persona, so nobody is
    working under it. That tracks the real invariant - whether a CONSUMER exists - rather than a proxy
    for it. Where the server reports no memory counts at all the second signal simply stays quiet, so
    this degrades to the original behaviour instead of guessing.
    """
    known = {p for p in directory if p}
    out = []
    for p in sorted(counts):
        if not p or not counts.get(p):
            continue
        if p not in known:
            out.append(p)
            continue
        owned = _PERSONA_MEMORY_COUNTS.get(p)
        if owned == 0:
            out.append(p)
    return out


def _stranded_detail(persona, directory, counts):
    """Describe one stranded inbox, naming its twin when it is a case-variant.

    'case-variant of known persona X' is a far more actionable diagnosis than 'unknown inbox': it tells
    the operator the mail was meant for a real person and how it went astray.
    """
    twin = next((d for d in sorted(directory)
                 if d and d != persona and d.casefold() == persona.casefold()), None)
    if twin is not None:
        return "%s (%s unread; case-variant of known persona %r)" % (persona, counts.get(persona), twin)
    if persona in set(directory) and _PERSONA_MEMORY_COUNTS.get(persona) == 0:
        return "%s (%s unread; registered as a recipient but owns no memories, so nobody works as it)" % (
            persona, counts.get(persona))
    return "%s (%s unread)" % (persona, counts.get(persona))


def report_stranded_inboxes(directory, counts, targets, emitter):
    """Alarm on undelivered mail: an inbox RECEIVING while nobody owns or watches it.

    Reported at most once per inbox per process, and summarised into ONE event per watcher rather than
    one per (watcher, inbox), so discovering a backlog cannot turn into a wake storm.

    Routed ONLY to watchers backed by a real DIRECTORY persona. This is not a formality: a stranded inbox
    holds mail, so discover_from_counts() gives it a watch target and an event stream of its own - and
    routing the alarm to every target would therefore write it straight into the unconsumed stream whose
    unconsumed-ness is the fault being reported. Producing an event there is not delivering it.

    The event is an `alert` (not a new event name) so consumers already filtering new|alert|recovered
    surface it without being rearmed; a fresh event name would itself have gone unwatched.
    """
    if not directory:
        return []   # unknown directory: alarming would flag EVERY persona. No data is not evidence of a fault.
    current = stranded_inboxes(directory, counts)
    # RELEASE the suppression for anything no longer stranded, so the alarm can fire AGAIN if that inbox
    # is later re-stranded. Suppressing for the process lifetime made "reported once" mean "reported once
    # ever", which silently contradicted the documented self-clearing behaviour: an inbox that was rescued
    # and then stranded a second time would never be announced.
    #
    # Keyed EXACTLY, not casefolded - the same asymmetry as stranded_inboxes() itself. The server's inbox
    # namespace is case-sensitive, so 'Claude-chat' and 'claude-chat' are DIFFERENT inboxes; sharing one
    # suppression key between them lets either one hold the other's alarm down.
    _REPORTED_STRANDED.intersection_update(current)
    fresh = [p for p in current if p not in _REPORTED_STRANDED]
    if not fresh:
        return []
    for persona in fresh:
        _REPORTED_STRANDED.add(persona)
        sys.stderr.write(
            "kijito-inbox-monitor: ALERT stranded mail - %s is not a known persona, so no agent consumes its "
            "mail (further reports for %r suppressed)\n" % (_stranded_detail(persona, directory, counts), persona))
    detail = ", ".join(_stranded_detail(p, directory, counts) for p in fresh)
    known = {p for p in directory if p}
    for watcher in sorted({t.persona for t in targets if t.persona and t.persona in known}):
        emitter.lifecycle("alert", persona=watcher,
                          reason="stranded-mail: %d inbox(es) receiving mail nobody watches: %s" % (len(fresh), detail),
                          stranded_inboxes=list(fresh))
    return fresh


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
    opener_by_origin = {}

    directory_opener = make_opener_for(PERSONAS_URL)
    personas = requested_personas(args, directory_opener, headers)
    if not personas:
        raise FatalConfig("at least one persona is required")
    targets = [build_persona_target(p, opener_by_origin, headers, args, emitter) for p in personas]
    # The DIRECTORY namespace, kept separate from `targets` on purpose: targets also accumulate personas
    # discovered from the inbox counts, so diffing against targets would silently absorb the very phantom
    # inboxes the stranded-mail check exists to find.
    directory_personas = list(personas) if watches_all_personas(args) else []

    # ---- self-test (§7.2): run once, exit -------------------------------------------------------------------------
    if args.self_test:
        ok = True
        for target in targets:
            ok = target.self_test() and ok
        return 0 if ok else 1

    seam = WakeSeam()
    seam.install()
    rediscover_at = _monotonic() + args.rediscover_every
    cursor = None    # opaque long-poll cursor (the server's max-message-id token) echoed on each call
    lp_backoff = 0   # exponential backoff (s) between FAILED long-poll attempts; 0 while healthy

    while not seam.stop:
        seam.drain()  # read-and-clear at START of poll (§10)
        if watches_all_personas(args) and directory_opener is not None and _monotonic() >= rediscover_at:
            try:
                _, discovered = discover_persona_targets(
                    args, headers, emitter, targets, opener_by_origin, directory_opener)
                if discovered:
                    directory_personas = discovered
            except FatalConfig as e:
                sys.stderr.write("kijito-inbox-monitor: WARNING persona rediscovery failed: %s\n" % e)
            rediscover_at = _monotonic() + args.rediscover_every

        counts_available = False
        unread_counts = {}
        held = False  # True iff this iteration was a real server-HELD long-poll (it already provided the wait)
        count_target = next((t for t in targets if t.unread_persona), None)
        if count_target is not None and not args.no_fast_path:
            if args.wait > 0:
                counts_available, unread_counts, new_cursor = fetch_unread_counts_longpoll(
                    count_target.opener, headers, args.wait, cursor)
                if counts_available:
                    lp_backoff = 0
                    if new_cursor is not None:
                        cursor = new_cursor   # real long-poll: advance the cursor; the hold WAS the wait
                        held = True
                    # new_cursor is None → server doesn't long-poll (yet) → interval-poll via the sleep below
                else:
                    # drop / blip / outage: back off, resume the SAME cursor next time (lossless), and this tick
                    # falls through to per-target full inbox polls (the by-message-id correctness backstop).
                    lp_backoff = min((lp_backoff * 2) or 1, LONGPOLL_BACKOFF_CAP)
            else:
                counts_available, unread_counts = fetch_unread_counts(
                    count_target.opener, count_target.count_url, headers)

        if counts_available:
            discover_from_counts(args, unread_counts, targets, opener_by_origin, headers, emitter)
            if not args.no_stranded_alerts:
                report_stranded_inboxes(directory_personas, unread_counts, targets, emitter)
        for target in targets:
            target.poll_once(counts_available, unread_counts)
        if seam.stop:
            break
        if held:
            continue  # the server-held long-poll already supplied the inter-poll wait - loop straight back
        seam.wait(lp_backoff if lp_backoff else args.poll_seconds)

    emitter.close()
    return 0


# --------------------------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="kijito-inbox-monitor",
        description="Watch your Kijito inbox and emit one event per new message. NOTE: emitting is not waking - a "
                    "bare `tail` of the events file captures but does NOT wake your agent. To actually wake on new "
                    "mail, use --emit exec-per-event with a command that pokes your agent loop, or run the tail "
                    "under your harness's streaming/notification consumer. See the README 'Waking your agent'.")
    p.add_argument("--persona", action="append",
                   help="Kijito persona whose inbox to watch. Repeat for multi-persona mode.")
    p.add_argument("--personas", action="append",
                   help="Comma-separated personas to watch, e.g. codex,river,ladybug.")
    p.add_argument("--all-personas", action="store_true",
                   help="Watch every persona in your Kijito account (default).")
    p.add_argument("--no-stranded-alerts", action="store_true",
                   help="do not alarm on mail sitting in an inbox that is not a known persona. Off by "
                        "default because such mail is UNDELIVERABLE and nothing else reports it; set this "
                        "only if you keep deliberate test inboxes and expect the alarm.")
    p.add_argument("--rediscover-every", type=int, default=600,
                   help="In all-persona mode, re-scan your account every N seconds and add newly-created personas "
                        "(default 600, min 1). Explicit persona subsets are not expanded.")
    p.add_argument("--poll-seconds", type=int, default=60,
                   help="Interval (s) between polls when long-poll is off/unsupported (default 60).")
    p.add_argument("--wait", type=int, default=50,
                   help="Long-poll hold (s) requested from /api/notify/pending so new mail wakes the watcher "
                        "near-instantly at ~the same request rate (default 50; the server clamps to its own max). "
                        "0 disables long-poll → plain interval polling at --poll-seconds. If the server doesn't "
                        "support long-poll, the client auto-falls back to interval polling (no redeploy needed). "
                        "Clean shutdown during a held poll can take up to --wait seconds (a supervisor's SIGKILL "
                        "mid-hold is safe - state is persisted every cycle).")
    p.add_argument("--alert-after", type=int, default=3, help="Consecutive failures before an alert (min 1).")
    p.add_argument("--emit", choices=("stdout-jsonl", "exec-per-event"), default="stdout-jsonl")
    p.add_argument("--exec", help="Command to run per event (required iff --emit exec-per-event).")
    p.add_argument("--suppress-author", action="append",
                   help="Do not emit 'new' events authored by this persona (repeatable) - drops the self-echo you "
                        "get when watching all personas AND sending mail. Liveness events are unaffected.")
    p.add_argument("--content-chars", type=int, default=220)
    p.add_argument("--no-content", action="store_true", help="Omit message content entirely (opaque mode).")
    p.add_argument("--events-file",
                   help="Write NDJSON events to this file (an OWNED, size-rotated fd) instead of stdout - the "
                        "supervised-producer mode that survives log rotation. Consumers tail -F it. "
                        "Only applies to --emit stdout-jsonl.")
    p.add_argument("--events-file-template",
                   help="Per-persona supervised mode: write EACH persona's events to its OWN owned, size-rotated "
                        "file, e.g. ~/.cache/kijito-inbox-monitor/events.{persona}.ndjson - a session then subscribes "
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
        sys.stderr.write("kijito-inbox-monitor: WARNING --exec ignored (emit mode is %s)\n" % args.emit)
    if args.poll_seconds < 1:
        raise FatalConfig("--poll-seconds must be >= 1")  # 0 → a select(timeout=0) busy-loop hammering the source
    if args.wait < 0:
        raise FatalConfig("--wait must be >= 0 (0 disables long-poll)")
    if args.wait > 0 and args.no_fast_path:
        sys.stderr.write("kijito-inbox-monitor: WARNING --wait ignored with --no-fast-path (long-poll is part of "
                         "the fast-path)\n")
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
        sys.stderr.write("kijito-inbox-monitor: WARNING --events-file/-template ignored (emit mode is %s)\n" % args.emit)
    if args.seed_at is not None:
        single = len(args.persona or []) == 1 and not args.personas and not args.all_personas
        if not single:
            raise FatalConfig("--seed-at requires a single --persona target, "
                              "not multi-persona/all-personas - each persona has its own cursor")


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        validate_args(args)
        return run(args)
    except FatalConfig as e:
        sys.stderr.write("kijito-inbox-monitor: FATAL %s\n" % e)
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
