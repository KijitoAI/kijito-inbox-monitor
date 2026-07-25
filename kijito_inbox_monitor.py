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
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

try:
    import fcntl  # POSIX only
except ImportError:  # pragma: no cover - Windows
    fcntl = None

__version__ = "0.4.0"
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
BROKEN_SINK_RETRY_S = 30   # cooldown before re-trying a persona sink we refused; the refusal's RELEASE
                           # condition, so removing a hostile path recovers without a restart (re-audit 10, H2)
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
                 omitted_exact=True, next_before_id=None, continuation_ok=True, consistent=True,
                 unread_not_shown=None):
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
        # False when the window's OWN TWO HALVES disagree - see fetch_from_payload(). A window that
        # contradicts itself cannot be believed in either direction, so it can neither close a span nor
        # be walked through; it PINS. Both directions of the contradiction are covered, not just the
        # one that happens to have been seen in the wild.
        self.consistent = consistent
        # Unread mail the server holds that this response did NOT hand us. None = the server did not say
        # (older API), which is NOT the same as 0 - see _hidden_unread() for why that distinction is the
        # whole safety property of this field.
        self.unread_not_shown = unread_not_shown


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
    # THE OMISSION DECLARATION AND THE CONTINUATION ARE TWO HALVES OF ONE STATEMENT, and the server emits
    # them from a SINGLE expression - `next_before_id = oldest_row if (has_more or size_dropped) else None`
    # (Kijito web_api.py, commented "present exactly when mail was withheld"). So a window withheld rows IF
    # AND ONLY IF it hands back a continuation, and either half contradicting the other is a contract
    # violation, not a quirk to interpret:
    #   withheld AND terminal      -> "I hid rows" + "there is nothing older"  (Loom re-audit 6, HIGH 3)
    #   withheld NOTHING AND more  -> "I hid nothing" + "there is more"        (Loom re-audit 7, HIGH 4)
    # The second is not merely the theoretical twin of the first; it follows from how the window is BUILT.
    # A page returns every older row that FIT, so if it withheld nothing there is nothing older left for a
    # continuation to point at. Believing the "I hid nothing" half advances the cursor over whatever the
    # other half says is still there, which is the silent-loss direction.
    # VERIFIED against the live API across 14 pages, including the case that could have made this rule
    # pin production forever: a page returning EXACTLY `limit` rows with more behind it declares
    # truncated=True (limit=4 -> next_before_id=1032), while one that exactly exhausts the mailbox
    # declares nothing and terminates (limit=5 -> next_before_id=null). The server never leaves a
    # complete window pointing onward, so this check cannot fire on healthy traffic.
    consistent = True
    if nb_ok:
        if n and nb is None:
            consistent = False
        elif not n and nb is not None:
            consistent = False
    uns = data.get("unread_not_shown")
    # A non-int (absent, null, a string, a float) means the server made NO statement. Coercing that to 0
    # would manufacture a "nothing is hidden" assertion out of silence - the exact inversion this field
    # exists to avoid. Negative is nonsense from a count, so it is also treated as no statement.
    return Poll(True, items=items, status=status, omitted=n, omitted_exact=exact,
                next_before_id=nb, continuation_ok=nb_ok, consistent=consistent,
                unread_not_shown=uns if isinstance(uns, int) and not isinstance(uns, bool) and uns >= 0
                else None)


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


# Urgent unread per persona, from the SAME row the unread count comes from - no extra request. Kept
# separately from `counts` so the fast-path arithmetic is untouched. A sender marking a message urgent is
# the closest thing the hive has to a declared expectation of attention, which makes it the one signal
# that can distinguish "idle by design" from "nobody is coming" without asking the silent party.
_URGENT_UNREAD = {}


def _parse_unread_rows(data):
    """Parse a /api/notify/pending body into {persona: unread}, or None if the shape is invalid.
    A persona with zero unread is ABSENT from the list → callers treat absent as 0.

    Also records `unread_urgent` into _URGENT_UNREAD as a side table. The endpoint hands it over on every
    tick and it was previously discarded; a signal you already receive and throw away is the cheapest kind
    of blindness.
    """
    rows = data.get("result") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return None
    counts = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("persona"), str):
            u = row.get("unread")
            counts[row["persona"]] = u if isinstance(u, int) else 0
            ug = row.get("unread_urgent")
            # Absent (an older server) means NO STATEMENT, not zero - the same tri-state discipline as
            # §5.2. Recording a 0 we were never told would assert "nothing is escalated" on no evidence.
            if isinstance(ug, int) and not isinstance(ug, bool) and ug >= 0:
                _URGENT_UNREAD[row["persona"]] = ug
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


def _safe_text(s):
    """Return `s` in a form that can ALWAYS be written to a UTF-8 file and put in a child's environment.

    A CONTENT BYTE MUST NEVER BECOME A DELIVERY FAILURE. Two shapes arrive from real message bodies and
    neither is our bug to have opinions about:
      · a NUL, which cannot appear in an environment value at all (subprocess raises ValueError);
      · a lone surrogate, which cannot be encoded to UTF-8 (json.dumps(ensure_ascii=False) passes it
        straight through, and the file write then raises UnicodeEncodeError).
    Both used to matter less because emit failures were swallowed. With the round-7 delivery gate they
    would matter enormously: the exec path would report a permanent non-delivery and WEDGE THE WATERMARK
    on that one message forever, and the file path would raise straight out of poll_once and crash the
    producer - which under a KeepAlive supervisor is a crash LOOP, since the same message is refetched
    every time. Trading a silent skip for a permanent stall is the failure this project keeps re-learning
    (see the corruption pin), so the fix is to make the event REPRESENTABLE rather than to fail on it.
    """
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        s = s.encode("utf-8", "replace").decode("utf-8")
    return s.replace("\x00", "") if "\x00" in s else s


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
        self._pending = False      # bytes written that are not known to be on stable storage yet
        self._sync_failed = False  # an fsync we can never retry (the fd was rotated away) failed
        self._dir_pending = False  # a directory ENTRY changed (create/rotate) and is not durable yet
        self._broken = None        # non-None => the sink is unusable; write() reports a FAILED delivery
        self._open()

    def _open(self):
        # Never abandon a live handle: any path that reopens without closing first leaks an fd per
        # rotation, which in a long-lived producer is unbounded. Defensive rather than reactive - the
        # callers currently all close first, and this makes that non-load-bearing.
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        dirn = os.path.dirname(os.path.abspath(self.path)) or "."
        _makedirs_private(dirn)
        existed = os.path.lexists(self.path)   # lexists: a dangling SYMLINK counts as present, and must
        self._fh = _open_private(self.path, "a", encoding="utf-8")
        # EVERY persisted artifact, not just the one we opened (Loom re-audit 9, H2): rotated archives
        # written by an older version keep their 0644 forever otherwise, because they are never reopened.
        # THE RANGE COMES FROM THE DIRECTORY, NOT FROM `keep` (Loom re-audit 10, M4). A bound derived from
        # CURRENT retention cannot reach an artifact left by a LARGER FORMER retention - shrinking keep
        # from 10 to 5 stranded .7 at 0644 permanently - and an increment-until-absent scan would stop at
        # the first hole a hand-deleted archive leaves. Listing is the only bound config cannot outlive.
        # ★ THE VERDICT IS DELIBERATELY IGNORED HERE (loom's class, half A - "who consumes this?"). It is
        # consumed by _repair_mode itself, which warns per file. Escalating would be wrong in both
        # directions: these are ARCHIVES, not the live sink, so refusing to open the events file because a
        # months-old archive is unreadable converts a stale-permission leak into a total delivery outage.
        for archive in self._archive_paths():
            _repair_mode(archive)
        if not existed:
            # A NEW FILE NEEDS ITS DIRECTORY ENTRY SYNCED, NOT JUST ITS BYTES (Loom re-audit 8, HIGH 2).
            # fsync on the fd makes the CONTENT durable; the NAME lives in the directory. Deferred to
            # sync() so it lands before the cursor that acknowledges these events is persisted.
            self._dir_pending = True

    def _archive_paths(self):
        """Every rotated archive of this sink that EXISTS RIGHT NOW, found by listing the directory.

        Deliberately not `range(1, keep + 2)`: that bound is CURRENT config, and the artifacts most likely
        to be left at a permissive mode are exactly the ones a FORMER, larger retention wrote (Loom
        re-audit 10, M4). Matches `<basename>.<digits>` only, so the `.lock` sidecar is never touched.
        """
        d = os.path.dirname(os.path.abspath(self.path)) or "."
        base = os.path.basename(self.path)
        try:
            names = os.listdir(d)
        except OSError:
            return []          # unreadable directory: nothing to enumerate, and _open still has to proceed
        return sorted(os.path.join(d, n) for n in names
                      if n.startswith(base + ".") and n[len(base) + 1:].isdigit())

    def _reopen_or_break(self):
        """Reopen after a rotation. A failure here must NOT escape the poll loop (Loom re-audit 9,
        MEDIUM): an exception out of write() unwinds through poll_once and, under a KeepAlive supervisor,
        is a crash loop. It becomes a broken sink instead, which write() reports as a failed delivery, so
        the cursor holds and the mail is re-delivered when the sink recovers."""
        try:
            self._open()
            self._broken = None
        except OSError as e:
            self._broken = str(e)
            self._fh = None
            sys.stderr.write("kijito-inbox-monitor: WARNING events sink %s is unusable (%s); holding the "
                             "cursor until it recovers\n" % (self.path, e))

    def write(self, line):
        """Append one event line. Returns True IFF the line reached the file.

        A FAILED write must never be reported as a delivery. The cursor that acknowledges an event is
        persisted from the same poll, so swallowing an OSError here would advance the watermark over a
        message nobody received - the exact silent loss this tool exists to prevent, arriving through
        the emit path instead of the fetch path.
        """
        if self._broken is not None or self._fh is None:
            self._reopen_or_break()                     # try to recover, silently on success
            if self._broken is not None or self._fh is None:
                return False
        try:
            self._fh.write(line)
            self._fh.flush()
        except (OSError, UnicodeError, ValueError) as e:
            # UnicodeError/ValueError are belt-and-braces behind _safe_text(): an event that still cannot
            # be encoded must be a FAILED DELIVERY (loud, retried, visible on stderr) and never an
            # exception escaping poll_once, which under a KeepAlive supervisor is a silent crash loop.
            sys.stderr.write("kijito-inbox-monitor: WARNING event write FAILED, holding the cursor: %s\n" % e)
            return False
        self._pending = True
        self._maybe_rotate()
        return True

    def sync(self):
        """Force written events onto stable storage. Returns True IFF they are durable.

        THE DURABILITY BARRIER (Loom re-audit 7, MEDIUM). flush() only moves bytes from Python's buffer
        into the kernel's; a power loss between the flush and the writeback loses them. The state file
        IS fsynced, so without this the CURSOR can outlive the EVENT it acknowledges - the watcher comes
        back believing it delivered mail that no consumer ever saw, and never fetches it again. Ordering,
        not just syncing, is what matters: event durable BEFORE cursor durable.
        """
        failed_earlier, self._sync_failed = self._sync_failed, False
        ok = True
        if self._dir_pending:
            # THE NAME AS WELL AS THE BYTES. Syncing only the fd leaves a cursor that can outlive the
            # PATHNAME of the events it acknowledges - and the state file may live in a DIFFERENT
            # directory (--state-file and --events-file-template are independent), so syncing the state
            # directory proves nothing about this one.
            if _fsync_dir(os.path.dirname(os.path.abspath(self.path)) or "."):
                self._dir_pending = False
            else:
                sys.stderr.write("kijito-inbox-monitor: WARNING could not fsync the events directory; "
                                 "holding the cursor\n")
                ok = False
        if self._pending:
            try:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._pending = False
            except OSError as e:
                sys.stderr.write("kijito-inbox-monitor: WARNING event fsync FAILED, holding the cursor: %s\n" % e)
                ok = False
        return ok and not failed_earlier

    def _maybe_rotate(self):
        if self.max_bytes <= 0:
            return
        try:
            size = os.fstat(self._fh.fileno()).st_size
        except OSError:
            return
        if size < self.max_bytes:
            return
        # Sync BEFORE the rename: after os.replace this fd names the archive, so a later sync() cannot
        # make these bytes durable. If it fails, remember it - the next sync() must report non-durable
        # once (holding the cursor for one poll) rather than silently losing the signal.
        if not self.sync():
            self._sync_failed = True
        try:
            try:
                self._fh.close()
            except OSError as e:   # a close() failure is a real error, not a reason to unwind the poll
                sys.stderr.write("kijito-inbox-monitor: WARNING closing %s during rotation failed: %s\n"
                                 % (self.path, e))
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
            # Every rename above rewrote directory ENTRIES; none of them is durable until the directory
            # itself is synced (Loom re-audit 8, HIGH 2).
            self._dir_pending = True
            self._reopen_or_break()  # reopen by NAME - a tail -F consumer follows us onto the fresh file

    def close(self):
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError as e:
                sys.stderr.write("kijito-inbox-monitor: WARNING closing %s failed: %s\n" % (self.path, e))
            finally:
                self._fh = None


_BROKEN_SINK = object()   # a sink that exists in config but cannot be written to SAFELY


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
        # key -> monotonic deadline before which we will not retry. NOT a set (Loom re-audit 10, H2):
        # membership alone has no release condition, so removing a hostile symlink never recovered without
        # a restart. WHAT CLEARS THIS: the deadline expiring and the reopen SUCCEEDING (see _sink_for).
        self._broken_sinks = {}
        # §6.3 event-id namespace. A BARE counter would restart at 1 on every process start and hand
        # old ids to new events - a consumer that had already seen them would drop live mail, which is
        # worse than the duplicate it was meant to prevent. Namespacing the counter with a per-run token
        # makes ids unique across restarts by construction; 8 random bytes keep that true even for a
        # supervisor restarting the producer thousands of times.
        self._run = "%016x" % int.from_bytes(os.urandom(8), "big")
        self._seq = 0

    def _sink_for(self, persona):
        """Route an event to its persona's sink (template mode), the single shared sink, or stdout (None).

        A sink we cannot create SAFELY returns the BROKEN sentinel, never None: None means "no sink
        configured, write to stdout", and falling through to stdout because a path looked like a symlink
        would print the mail we just refused to file. One persona's bad path must also not take the
        others down, so it is contained here rather than raised.
        """
        if self.sink_template is None:
            return self.sink
        key = persona or "_all"  # events with no persona (e.g. a bare --url target) land in one _all file
        s = self._sinks_by_persona.get(key)
        if s is None:
            # A REFUSAL IS A COOLDOWN, NOT A VERDICT (Loom re-audit 10, H2). Caching the refusal with no
            # release meant a persona whose path was briefly hostile stayed undeliverable for the life of
            # the process: the operator removed the symlink, the fault was gone, and mail kept being held
            # with nothing left to fix. A permanent fail-closed is the same bug as a fail-open, facing the
            # other way (invariant 2: every pin must be dischargeable).
            retry_at = self._broken_sinks.get(key)
            if retry_at is not None and _monotonic() < retry_at:
                return _BROKEN_SINK
            path = self.sink_template.replace("{persona}", _state_safe_persona(key))
            try:
                s = RotatingFileSink(path, self._max_bytes, self._keep)
            except OSError as e:
                self._broken_sinks[key] = _monotonic() + BROKEN_SINK_RETRY_S
                _warn_persona_once(key, "cannot open an events sink for %r safely (%s); its mail will be "
                                        "held, not written elsewhere" % (key, e))
                return _BROKEN_SINK
            if self._broken_sinks.pop(key, None) is not None:
                # Re-arm the suppressed warning so a LATER break is reported instead of silently
                # inheriting this one's suppression, and say so - a recovery nobody can see is the same
                # invisibility this tool exists to remove.
                _clear_persona_warning(key)
                sys.stderr.write("kijito-inbox-monitor: events sink for persona %r recovered; its held "
                                 "mail will be delivered\n" % key)
            self._sinks_by_persona[key] = s
        return s

    def close(self):
        if self.sink is not None:
            self.sink.close()
        for s in self._sinks_by_persona.values():
            s.close()

    def sync(self, persona=None):
        """Make THIS persona's written events durable. Returns True IFF they are on stable storage.

        Called by the watcher BEFORE it persists a cursor that acknowledges those events. exec-per-event
        has no sink of ours to sync (the consumer owns its own durability, and its exit status is the
        acknowledgement), and a stdout stream is a pipe we do not own - both answer True.

        SCOPED TO ONE PERSONA on purpose. The Emitter is shared by every watch target, so syncing all
        sinks would let ONE persona's failing sink retract every OTHER persona's deliveries - a full
        directory's worth of duplicate storms caused by a stream nobody was reading. In template mode
        each persona owns its sink; in single-file mode there is one shared sink and syncing it is
        correct for whichever target asks.
        """
        ok = True
        if self.sink is not None:
            ok = self.sink.sync() and ok
        if self.sink_template is not None:
            if (persona or "_all") in self._broken_sinks:
                return False       # nothing was written, so nothing is durable; hold the cursor
            s = self._sinks_by_persona.get(persona or "_all")
            if s is not None:
                ok = s.sync() and ok
        return ok

    def _clip(self, content):
        if self.no_content:
            return None
        s = "" if content is None else str(content)
        return s[: self.content_chars]

    def _event_id(self, event):
        """A producer-owned identity for this event (§6.3). Never derived from the serialised bytes.

        TWO KINDS OF IDENTITY, because `new` and the signals need opposite things:

        · `new` carries the MESSAGE's identity - persona plus the server's message id. The same message
          therefore always gets the same event id: across a restart, across a re-delivery after state
          loss, and across two watchers of the same inbox. That is what makes exactly-once processing
          possible on the consumer side, and it is the case that matters, because a duplicated message
          is duplicated WORK while a duplicated signal is only noise.

        · everything else is a SIGNAL, and gets an id unique to this emission. A recurrence is a
          genuinely different event - a second outage is a second thing you want to see - so signals
          must NOT collapse into their earlier selves. Repeated announcements of an UNCHANGED condition
          are suppressed at the source instead (the alarms are edge-triggered and self-clearing), which
          is where that belongs.

        Deliberately not a hash of the emitted line: byte-hashing couples the consumer to our
        formatting, so a change to key order, spacing or content clipping silently changes the dedupe
        key and re-delivers old events.
        """
        persona = event.get("persona") or "_"
        if event.get("event") == "new" and isinstance(event.get("id"), int):
            return "%s:new:%d" % (persona, event["id"])
        self._seq += 1
        return "%s:%s:%s-%d" % (persona, event.get("event") or "_", self._run, self._seq)

    def emit(self, event):
        """Deliver one event. Returns True IFF delivery was ACKNOWLEDGED.

        DELIVERY IS ACKNOWLEDGED, NOT ASSUMED (Loom re-audit 7, HIGH 1). The return value is what lets
        the watcher hold its cursor below a message it could not hand over. Before this, emit() swallowed
        every failure and the cursor advanced regardless, so a consumer whose --exec exited non-zero -
        the wake hook that is the entire point of exec mode - never saw that message again, and the
        watcher reported success. Anything other than True here means "not acknowledged": the message
        will be re-delivered rather than dropped, because a duplicate is recoverable and a skip is not.

        `event` is a dict already containing event/source/ts and type-specific fields.
        """
        # Stamped HERE, the single chokepoint every event passes through, rather than in the
        # convenience constructors: a future event kind added elsewhere cannot forget to carry one.
        event["event_id"] = self._event_id(event)
        if self.mode == "stdout-jsonl":
            # Sanitised at the SERIALISED line, so one call covers every field an event can carry -
            # content, `from`, an alarm `reason` built from server data - rather than each of them.
            line = _safe_text(json.dumps(event, ensure_ascii=False)) + "\n"
            sink = self._sink_for(event.get("persona"))
            if sink is _BROKEN_SINK:
                return False           # a failed delivery: hold the cursor, never divert the mail
            if sink is not None:
                return sink.write(line)
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except OSError as e:
                sys.stderr.write("kijito-inbox-monitor: WARNING stdout write FAILED, holding the cursor: %s\n" % e)
                return False
            return True
        else:  # exec-per-event
            env = dict(os.environ)
            env["KIJITOMON_EVENT"] = str(event.get("event", ""))
            env["KIJITOMON_SOURCE"] = str(event.get("source", ""))
            env["KIJITOMON_TS"] = str(event.get("ts", ""))
            env["KIJITOMON_EVENT_ID"] = str(event.get("event_id", ""))
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
                    env[envname] = _safe_text(",".join(str(x) for x in v) if isinstance(v, list) else str(v))
            try:
                r = subprocess.run(self.exec_cmd, shell=True, env=env, timeout=EXEC_TIMEOUT, check=False)
            except subprocess.TimeoutExpired:
                # A timeout is NOT a delivery. The command may well have run - so the consumer must be
                # idempotent - but we have no acknowledgement, and inventing one is how mail disappears.
                sys.stderr.write("kijito-inbox-monitor: exec TIMED OUT, holding the cursor: %s\n" % self.exec_cmd)
                return False
            except Exception as e:
                sys.stderr.write("kijito-inbox-monitor: exec FAILED to run, holding the cursor: %s\n" % e)
                return False
            if r.returncode != 0:
                sys.stderr.write("kijito-inbox-monitor: exec exited %d, holding the cursor (the event will be "
                                 "re-delivered): %s\n" % (r.returncode, self.exec_cmd))
                return False
            return True

    # convenience constructors (carry the canonical fields; ts stamped at emit time)
    def new(self, m):
        """Emit one `new` event. Returns True IFF the message is ACKNOWLEDGED (see emit())."""
        if self.suppress_authors and m.get("from") in self.suppress_authors:
            # --suppress-author: don't wake on an event WE authored (self-echo noise). This is a
            # deliberate POLICY drop, so it counts as acknowledged - the cursor must still advance, or
            # suppressing an author would pin the watermark forever on that author's next message.
            return True
        ev = {"event": "new", "source": SOURCE, "ts": _now_iso(), "id": m.get("id"),
              "from": m.get("from"), "created": m.get("created")}
        if m.get("_persona"):
            ev["persona"] = m.get("_persona")
        c = self._clip(m.get("content"))
        if c is not None:
            ev["content"] = c
        return self.emit(ev)

    def lifecycle(self, event, **fields):
        ev = {"event": event, "source": SOURCE, "ts": _now_iso()}
        ev.update(fields)
        return self.emit(ev)


# --------------------------------------------------------------------------------------------------------------------
# §7.3 State file (canonical identity + flock + atomic write + resume)
# --------------------------------------------------------------------------------------------------------------------
# A state file that EXISTS but cannot be trusted. Distinct from None (genuinely absent) because the two
# demand opposite behaviour: absent means baseline, corrupt means fail closed and re-emit.
CORRUPT_STATE = object()


PRIVATE_FILE_MODE = 0o600   # event streams and lock sidecars carry/guard message content
PRIVATE_DIR_MODE = 0o700


class InsecureFile(OSError):
    """A path we were about to write MAIL into is not something we are willing to write mail into."""


def _assert_private_fd(fd, path):
    """Fail CLOSED unless this fd is a REGULAR file, owned by US, at exactly 0600 (Loom re-audit 9, H1/H2).

    My round-8 repair was best-effort - it warned on failure and wrote anyway - on the reasoning that "a
    file we do not own must not crash the watcher". That reasoning is exactly backwards for a file we are
    about to append PRIVATE MAIL to: loom's repro left a pre-existing 0666 file at 0666 and delivered mail
    into it. Refusing to write is the only safe answer, and the caller turns that into a FAILED DELIVERY,
    so the cursor holds and nothing is lost.
    """
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise InsecureFile("%s is not a regular file" % path)
    if st.st_uid != os.geteuid():
        raise InsecureFile("%s is owned by uid %d, not by us (uid %d)" % (path, st.st_uid, os.geteuid()))
    cur = st.st_mode & 0o777
    if cur != PRIVATE_FILE_MODE:
        # EXACTLY 0600, not merely "no group/other bits" - a 0700 event file kept its execute bit under
        # the old `st_mode & 0o077` test, which is a mode nothing here should ever have (re-audit 9, H2).
        try:
            os.fchmod(fd, PRIVATE_FILE_MODE)
        except OSError as e:
            # Typed, so a caller reading the log can tell "we refused this path" from "the disk broke".
            raise InsecureFile("%s is %o and cannot be tightened to 0600: %s" % (path, cur, e))
        again = os.fstat(fd).st_mode & 0o777
        if again != PRIVATE_FILE_MODE:
            raise InsecureFile("%s is %o and could not be tightened to 0600 (now %o)" % (path, cur, again))
        sys.stderr.write("kijito-inbox-monitor: tightened %s from %o to 0600 (it carries message content "
                         "and was reachable by other local users)\n" % (path, cur))


def _open_private(path, mode="a", encoding=None):
    """Open a file that must never be readable by anyone else, refusing anything suspicious.

    THE EVENT STREAM CARRIES MESSAGE BODIES. A plain open() takes the process umask (022 by default), so
    every events.<persona>.ndjson was created 0644 - world-readable private hive mail, verified live
    (re-audit 8, H1). But the FIRST repair was itself unsafe (re-audit 9, H1), and worse than the leak it
    fixed: it FOLLOWED SYMLINKS, so it chmod'ed and appended mail to whatever a link pointed at, and a
    DANGLING link created its target in another directory entirely. A passive disclosure had been turned
    into an active write primitive.
      · O_NOFOLLOW - the final component must not be a symlink. (No TOCTOU window: the check is the open.)
      · owner + regular-file, checked on the FD we already hold, never by a second path lookup.
      · fail CLOSED - callers convert InsecureFile into a failed delivery, never a crash and never a write.
    """
    # O_NONBLOCK matters as much as O_NOFOLLOW here: opening a FIFO for writing BLOCKS until a reader
    # appears, so a FIFO planted at the events path would HANG the watcher forever - silently, with no
    # crash to notice and no events to miss noticing. (Found when the regular-file test hung the suite.)
    # On a regular file O_NONBLOCK is a no-op, so it costs nothing on the path we actually take.
    flags = (os.O_WRONLY | os.O_CREAT | os.O_APPEND
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        fd = os.open(path, flags, PRIVATE_FILE_MODE)
    except OSError as e:
        # ELOOP here means the path IS a symlink - report it as what it is, not as a generic open failure.
        raise InsecureFile("refusing to open %s: %s" % (path, e))
    try:
        _assert_private_fd(fd, path)
    except OSError:
        os.close(fd)
        raise
    return os.fdopen(fd, mode, encoding=encoding) if encoding else os.fdopen(fd, mode)


def _repair_mode(path):
    """Tighten an EXISTING artifact to 0600 in place. Returns True if it is now safe.

    Repairing only the file we happen to open leaves every OTHER persisted artifact exactly as it was -
    loom found pre-existing rotated archives still at 0644 after the round-8 fix (re-audit 9, H2). Opened
    O_NOFOLLOW and validated on the fd, for the same reason as _open_private.
    """
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return True                      # nothing there is nothing to leak
    except OSError as e:
        sys.stderr.write("kijito-inbox-monitor: WARNING refusing to repair %s: %s\n" % (path, e))
        return False
    try:
        _assert_private_fd(fd, path)
        return True
    except OSError as e:
        sys.stderr.write("kijito-inbox-monitor: WARNING %s may be readable by other local users: %s\n"
                         % (path, e))
        return False
    finally:
        os.close(fd)


def _makedirs_private(path):
    """Create EVERY missing level 0700, and warn about an existing level anyone else can write.

    os.makedirs(mode=...) applies the mode to the LEAF only; intermediate directories get the umask
    default, so a nested path left its parents 0755 (Loom re-audit 9, MEDIUM). An EXISTING directory is
    still not re-permissioned - silently changing a path the operator already owns is not ours to do - but
    a group/world-WRITABLE one is reported, because that is the condition under which someone else can
    swap a file for a symlink underneath us. (_open_private then refuses it, which is the real defence;
    this is the warning that tells you why events stopped.)
    """
    path = os.path.abspath(path)
    missing = []
    cur = path
    while not os.path.isdir(cur):
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for d in reversed(missing):
        try:
            os.mkdir(d, PRIVATE_DIR_MODE)
        except FileExistsError:
            pass
    # Check the WHOLE ancestor chain, not only the levels we created - loom's point was that an EXISTING
    # directory is never validated, and an existing one is exactly where a hostile path would already be.
    # A sticky directory (/tmp, mode 1777) is excluded: the sticky bit is precisely what makes a shared
    # writable directory safe, and warning about it would train the reader to ignore this line.
    seen = path
    while True:
        try:
            st = os.stat(seen)
        except OSError:
            break
        if (st.st_mode & 0o022) and not (st.st_mode & stat.S_ISVTX):
            sys.stderr.write("kijito-inbox-monitor: WARNING directory %s is writable by other local users "
                             "(mode %o); files there can be swapped for symlinks underneath us\n"
                             % (seen, st.st_mode & 0o777))
        parent = os.path.dirname(seen)
        if parent == seen:
            break
        seen = parent


def _fsync_dir(path):
    """fsync a DIRECTORY so a rename inside it is durable. Returns True on success.

    os.replace is atomic for a concurrent READER, but atomicity is not durability: after a power loss
    the new file's contents can be on disk while the directory entry still names the old inode - i.e. a
    silently OLDER cursor. Syncing the file alone (which is all we did) does not cover the rename.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def identity_migratable(stored, current):
    """True iff a persisted identity differs from the current one ONLY BY THE CASE OF A QUERY VALUE.

    THE CASE-ONLY MIGRATION (Loom re-audit 7, HIGH 3). The state PATH casefolds the persona
    (_state_safe_persona - the local filesystem is case-insensitive, so it must), while the IDENTITY
    embeds the persona with its original case, straight from the directory. So one file, written when
    the directory spelled the persona `Loom`, is reloaded by a run that discovered `loom` - the identity
    compares UNEQUAL, load() reports ABSENT, and absent BASELINES to the newest visible id, skipping
    everything since the lost cursor. A cursor destroyed by a spelling change is exactly the silent skip
    the state file exists to prevent.

    ★ THIS IS A THIRD LAYER, NOT A HARMONISATION OF THE OTHER TWO (see CaseAsymmetryInvariantTest, and
    do not "simplify" it into them). The SERVER's inbox namespace stays case-SENSITIVE - `Loom` and
    `loom` remain distinct inboxes and a variant holding mail is still alarmed on as stranded. What this
    says is narrower and follows from the path layer: because the path already collapses the variants,
    ONE state file can only ever describe ONE of them, so a casefold-equal identity in THAT file is the
    same watched source spelled differently - a migration to accept and rewrite, not a different source
    to baseline over.

    Deliberately strict about WHAT may differ: scheme, host, port and path must match EXACTLY, and so
    must every query KEY. Only the query VALUE is compared case-insensitively. Nothing here invents
    case-insensitivity for a URL path or for a host we did not already lowercase.
    """
    if not (isinstance(stored, list) and len(stored) == 5 and isinstance(current, list) and len(current) == 5):
        return False
    if stored[:4] != current[:4]:
        return False
    sq, cq = stored[4], current[4]
    if not (isinstance(sq, list) and isinstance(cq, list)) or len(sq) != len(cq):
        return False
    for s, c in zip(sq, cq):
        if not (isinstance(s, (list, tuple)) and len(s) == 2):
            return False
        if str(s[0]) != str(c[0]) or str(s[1]).casefold() != str(c[1]).casefold():
            return False
    return True


class StateFile:
    def __init__(self, path, identity):
        self.path = path
        self.identity = identity
        self._lockf = None
        # Set by lock() when the state file could not be PROVEN private, and consumed by load(), which
        # then fails closed. WHAT CLEARS THIS: nothing within the process - the condition is a property of
        # the path on disk, re-evaluated from scratch on the next start. Recorded explicitly because "no
        # release condition" is only acceptable when it is the ANSWER, not when it is an oversight.
        self.unsafe = False

    def lock(self):
        if not IS_POSIX or fcntl is None:
            return  # Windows: no lock (documented; run a single instance)
        dirn = os.path.dirname(os.path.abspath(self.path)) or "."
        _makedirs_private(dirn)
        # Lock a DEDICATED .lock SIDECAR, never the state-file itself: save() replaces the state-file's inode
        # (mkstemp + os.replace) on every poll, which would orphan a flock held on it and let a second watcher
        # lock the new inode freely. The sidecar is never replaced, so the flock persists for the process
        # lifetime. flock is advisory + auto-released by the OS on exit (no stale lockfile to clean).
        if not _repair_mode(self.path):  # the state file itself, if an older version left it permissive
            # CONSUME THE VERDICT (Loom re-audit 10, H1). This was a bare statement, so a state file we
            # could not prove private was then trusted anyway. It is the highest-value file here to
            # subvert: whoever controls the CURSOR controls which mail counts as already delivered, and a
            # cursor moved FORWARD is silent, permanent mail loss - the single failure this tool exists to
            # prevent. Deliberately NOT fatal: one persona's hostile path must not take the whole producer
            # down (the same reasoning as _sink_for). load() fails closed on it instead, which routes into
            # the existing, tested "present but untrustworthy" path rather than inventing a new one.
            self.unsafe = True
        self._lockf = _open_private(self.path + ".lock", "a+")
        try:
            fcntl.flock(self._lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Close the sidecar we just opened before failing. Leaving it open leaks an fd per refused
            # lock - and persona rediscovery runs every tick, so a persona whose file is held by another
            # watcher would leak one fd per attempt for the life of the process.
            self.unlock()
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
        if self.unsafe:
            # The verdict lock() computed and used to discard. Present-but-not-provably-ours is exactly
            # the "evidence a cursor existed, but not one we can trust" case: fail closed, re-emit.
            sys.stderr.write("kijito-inbox-monitor: WARNING state-file %s could not be proven private; "
                             "refusing to trust its cursor and failing closed\n" % self.path)
            return CORRUPT_STATE
        # LET THE O_NOFOLLOW OPEN BE THE EXISTENCE TEST (Loom re-audit 10, H1). The WRITE path was given
        # O_NOFOLLOW in re-audit 9 and the READ path was left behind, so a symlink planted at the state
        # path was followed and its target read as our own state. os.path.exists() follows symlinks too,
        # so it was answering for the TARGET rather than the link - and being a second path lookup it
        # opened a TOCTOU window between the check and the open. One syscall now settles both questions.
        try:
            fd = os.open(self.path,
                         os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        except FileNotFoundError:
            return None                  # genuinely ABSENT - the one case that may baseline
        except OSError as e:
            # PRESENT, but not something we are willing to read: a symlink (ELOOP), a FIFO, a directory.
            # Still EVIDENCE THAT A CURSOR EXISTED, so it takes the same fail-closed answer as an
            # unparseable file. Reading it would be worse than not resuming; baselining would be worst.
            sys.stderr.write("kijito-inbox-monitor: WARNING state-file %s exists but is not a file we will "
                             "read (%s); failing closed and re-emitting rather than baselining\n"
                             % (self.path, e))
            return CORRUPT_STATE
        try:
            f = os.fdopen(fd, "r")
        except OSError as e:
            os.close(fd)                 # never leak the descriptor we just took (re-audit 10, L6's class)
            raise FatalConfig("state-file unreadable: %s" % e)
        with f:
            try:
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
            if identity_migratable(ident, self.identity):
                # CASE-ONLY MIGRATION, not a different source - see identity_migratable(). Resume the
                # cursor; the next save() rewrites the file with the current spelling, so this converges
                # after one poll instead of re-warning forever.
                sys.stderr.write("kijito-inbox-monitor: state-file identity differs only by case (%r -> %r); "
                                 "MIGRATING it rather than re-baselining (re-baselining would skip every "
                                 "message since the stored cursor): %s\n" % (ident, self.identity, self.path))
            else:
                sys.stderr.write("kijito-inbox-monitor: WARNING state-file identity mismatch (%r != %r) - NOT "
                                 "resuming its cursor; re-baselining to avoid a silently-blind watcher.\n"
                                 % (ident, self.identity))
                return None
        # EVERY PERSISTED FIELD IS READ STRICTLY, AND ANYTHING UNRECOGNISED FAILS CLOSED (Loom re-audit 7,
        # HIGH 2). The pin's own flags were read with `d.get(k) is True`, so a JSON `1` - the shape a
        # hand-edit, a jq one-liner or another language's serialiser produces - normalised to False and
        # SILENTLY UNPINNED the watermark, letting the replay cap cross the very span the pin was
        # protecting. `pin_evidence_intact` had the mirror bug (`is False`, so `0` read as intact), and
        # ints-that-are-bools were accepted as row ids and as gap_alerted. A malformed field is EVIDENCE
        # THE FILE CANNOT BE TRUSTED, so the honest answer is CORRUPT_STATE - which arms below the visible
        # window and re-emits it - never a quietly permissive default.
        strict_ok = True

        def _flag(key):
            """Strict tri-state read of a persisted boolean: (value, ok)."""
            nonlocal strict_ok
            v = d.get(key, _MISSING)
            if v is _MISSING or v is None:
                return False
            if v is True or v is False:
                return v
            sys.stderr.write("kijito-inbox-monitor: WARNING state-file field %r is not a boolean (%r); refusing "
                             "to interpret it: %s\n" % (key, v, self.path))
            strict_ok = False
            return False

        # Ids already emitted ABOVE a pinned watermark. Absent in files written by older versions, which is
        # exactly the forward-compat case: an empty set just means "nothing pinned", the pre-pinning behaviour.
        alerted = d.get("gap_alerted", _MISSING)
        if alerted is _MISSING or alerted is None:
            alerted = None
        elif _is_int(alerted):
            pass
        else:
            sys.stderr.write("kijito-inbox-monitor: WARNING state-file 'gap_alerted' is not an integer (%r); "
                             "refusing to interpret it: %s\n" % (alerted, self.path))
            alerted, strict_ok = None, False
        release_at = d.get("pin_release_at", _MISSING)
        if release_at is _MISSING or release_at is None:
            release_at = None
        elif _is_int(release_at):
            pass
        else:
            sys.stderr.write("kijito-inbox-monitor: WARNING state-file 'pin_release_at' is not an integer (%r); "
                             "refusing to interpret it: %s\n" % (release_at, self.path))
            release_at, strict_ok = None, False
        raw = d.get("emitted_above")
        if raw is None:
            emitted, intact = set(), True          # no pin was in force; the ordinary case
        elif isinstance(raw, list) and all(_is_int(i) for i in raw):
            # _is_int, not isinstance(i, int): `true` in this list would otherwise become the id 1 and
            # suppress a real message 1 for the life of the pin.
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
        # Read strictly: `0` used to slip past `is False` and leave the evidence marked INTACT, which is
        # the fail-OPEN direction on the one field that says "stop trusting your own view of this span".
        if d.get("pin_evidence_intact", _MISSING) is not _MISSING and not _flag("pin_evidence_intact"):
            intact = False
        pin_forced = _flag("pin_forced")
        state_corrupt = _flag("state_corrupt")
        if not strict_ok:
            sys.stderr.write("kijito-inbox-monitor: WARNING state-file has malformed pin fields; treating the "
                             "whole file as CORRUPT (fail closed) rather than resuming a state we cannot "
                             "read: %s\n" % self.path)
            return CORRUPT_STATE
        # Whether the unread-not-shown alarm is currently ANNOUNCED. Absent (older file) reads as False:
        # a re-announce after an upgrade costs one event and is honest about the current condition,
        # whereas defaulting to True would silence a live condition for the rest of the run.
        hidden = d.get("unread_hidden") is True
        return {"cursor": cursor, "state": state, "failures": failures, "emitted_above": emitted,
                "gap_alerted": alerted, "pin_evidence_intact": intact,
                "pin_forced": pin_forced, "pin_release_at": release_at,
                "state_corrupt": state_corrupt, "unread_hidden": hidden}

    def unlock(self):
        """Release the single-writer flock and close the sidecar fd.

        The OS drops an flock when the process exits, so this is hygiene rather than correctness - but an
        fd held for a target that is torn down is a genuine leak in a long-lived process, and it is what
        surfaced as the suite's two ResourceWarnings (Loom re-audit 7, item 7).
        """
        if self._lockf is not None:
            try:
                self._lockf.close()
            finally:
                self._lockf = None

    def save(self, cursor, state, failures, emitted_above=None, gap_alerted=None,
             pin_forced=False, pin_evidence_intact=True, state_corrupt=False, pin_release_at=None,
             unread_hidden=False):
        """Persist the cursor. Returns True IFF the write is DURABLE (Loom re-audit 8, HIGH 3).

        The directory fsync used to be called and its answer thrown away, so a failure returned success
        with no diagnostic: the cursor was written and its durability merely assumed. The failure
        direction is re-delivery rather than loss - a reverted state file replays mail - but a watcher
        that cannot tell you it failed to persist will keep not telling you, and a disk failing this way
        is exactly the condition nobody notices.
        """
        if not IS_POSIX:
            return True  # best-effort; skip on Windows
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
        # The floor that RELEASES a corruption pin. Persisted with the pin itself: a pin whose release
        # condition does not survive a restart is a pin that can never clear.
        if pin_release_at is not None:
            d["pin_release_at"] = pin_release_at
        # Same reason, for the unread-not-shown alarm: KeepAlive restarts a crashing producer, and an
        # un-persisted suppression would turn a crash loop into a wake storm on a condition nobody can
        # act on any faster for being told twice.
        if unread_hidden:
            d["unread_hidden"] = True
        dirn = os.path.dirname(os.path.abspath(self.path)) or "."
        _makedirs_private(dirn)
        fd, tmp = tempfile.mkstemp(dir=dirn, prefix=".kijmon-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(d, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            # The rename needs its own sync - see _fsync_dir(). Without it the cursor's DURABILITY story
            # stops one level short of the thing that makes it visible. ITS ANSWER IS RETURNED, not
            # discarded: a check whose result nobody reads is not a check.
            if not _fsync_dir(dirn):
                sys.stderr.write("kijito-inbox-monitor: WARNING state-file directory fsync FAILED for %s; "
                                 "the cursor is written but its durability is UNPROVEN (a crash may replay "
                                 "mail from an older cursor)\n" % dirn)
                return False
            return True
        except OSError as e:
            sys.stderr.write("kijito-inbox-monitor: WARNING state-file write failed (non-fatal): %s\n" % e)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False


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


def _clear_persona_warning(persona):
    """WHAT CLEARS THIS: the condition the warning described actually recovering.

    Without a release, suppress-once is itself an instance of loom's class - a state set and never
    cleared - and it fails in the dangerous direction: a persona whose sink broke, recovered, then broke
    AGAIN would be silently suppressed forever, so the second outage arrives with no diagnostic at all.
    Called from the recovery path, never on a timer: the warning is suppressed exactly as long as the
    condition it reported is still true.
    """
    _WARNED_PERSONAS.discard(persona.casefold())


def requested_personas(args, opener, headers):
    # ⚠️ CASE-INSENSITIVE DEDUPE, THE SAME RULE AS new_personas() (re-audit 11, F3). This used to be an
    # EXACT `p not in personas`, so `--persona Loom --persona loom` survived as two entries - but
    # _state_safe_persona() CASEFOLDS the state path, so both resolve to ONE state file. The second
    # flock then raises FatalConfig("state-file in use") out of the UNCAUGHT list comprehension in
    # run(), and the producer refuses to start FOR EVERY PERSONA. That breaks the containment rule this
    # file states twice ("one persona's hostile path must not take the whole producer down"), and the
    # error blamed "another watcher" when the collision was with itself.
    # Keeps the FIRST spelling seen, exactly like new_personas(), so nothing about the normal path moves.
    personas = []
    seen = set()

    def add(p):
        key = p.casefold()
        if p and key not in seen:
            seen.add(key)
            personas.append(p)

    for p in (p.strip() for p in args.persona or []):
        add(p)
    for group in args.personas or []:
        for p in (part.strip() for part in group.split(",")):
            add(p)
    if args.all_personas or not personas:
        for p in fetch_personas(opener, headers):
            add(p)
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
        # The floor at which a corruption pin may RELEASE. A corrupt-state arm parks the watermark one
        # BELOW the window it re-emits, so the ordinary release test (a complete window reaching back to
        # at-or-below the watermark) is unsatisfiable by construction - the pin carries the floor that
        # discharges it instead. Persisted, because a release condition that dies on restart is not one.
        self.pin_release_at = None
        self.delivery_blocked = False  # an emit failed; the cursor is held below it until it succeeds
        self.state_not_durable = False  # the last cursor write could not be proven durable
        # Is the unread-not-shown alarm currently ANNOUNCED? Keyed on the CONDITION, so it self-clears
        # (§5.2). One flag per target, so the key is per-inbox and EXACT - a case-variant persona is a
        # different WatchTarget with a different flag, and cannot hold this one's alarm down.
        self.unread_hidden = False
        # Does the persona DIRECTORY know this inbox? Optimistic by default: see poll_once() for why this
        # alarm fails OPEN where the stranded-mail alarm fails closed.
        self.directory_backed = True

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
                    self.pin_release_at = loaded["pin_release_at"]
                    # A persisted forced pin is authoritative; the inference from missing tracking is only
                    # a fallback for files written before the flag existed.
                    self.pin_forced = loaded["pin_forced"] or not loaded["pin_evidence_intact"]
                    self.unread_hidden = loaded["unread_hidden"]
        if args.seed_at is not None:
            self.cursor = args.seed_at

    def self_test(self):
        poll = fetch(self.opener, self.url, self.headers)
        reach_ok = poll.ok
        label = self.persona or self.url
        sys.stderr.write("self-test[%s]: source %s (%s)\n" % (
            label, "REACHABLE+healthy" if reach_ok else "UNHEALTHY", poll.reason or "ok"
        ))
        # CONSUME THE EMITTER'S ANSWER (Loom re-audit 10, M3). This was `emit_ok = True` with the call as a
        # bare statement, flipping only on an EXCEPTION - but emit() reports a failed delivery by RETURNING
        # False, which is its documented, non-exceptional path (a refused sink, a failed write, a non-zero
        # --exec). So a self-test against a sink that had just refused the write printed emit=OK. The one
        # surface whose entire job is to tell an operator "this works before you trust it" was itself an
        # instance of the class it exists to detect - which is why a sweep starts with the diagnostics.
        try:
            emit_ok = bool(self.emitter.new({"id": 0, "from": "self-test", "content": "synthetic emit OK",
                                             "created": _now_iso(), "_persona": self.persona}))
        except Exception as e:
            emit_ok = False
            sys.stderr.write("self-test[%s]: emit FAILED: %s\n" % (label, e))
        sys.stderr.write("self-test[%s]: emit=%s reachable=%s\n" % (
            label, "OK" if emit_ok else "FAIL", reach_ok
        ))
        return reach_ok and emit_ok

    def lifecycle(self, event, **fields):
        """Emit a lifecycle event. RETURNS whether it was delivered (Loom/river re-audit 11, F1).

        This used to drop `Emitter.lifecycle`'s bool on the floor, which made every caller structurally
        unable to know whether the thing it had just recorded as announced was in fact announced.
        """
        if self.persona:
            fields["persona"] = self.persona
        return self.emitter.lifecycle(event, **fields)

    def _alarm(self, event, log_text, **fields):
        """Emit an edge event and GUARANTEE it reaches a human, returning whether it was DELIVERED.

        `log_text` is the stderr wording ONLY; it is deliberately NOT injected into the event, so this
        adds no field to any event's schema (§6.1). Callers that want `reason` on the wire pass it in
        **fields like any other field.

        THE DEFECT THIS CLOSES (re-audit 11, F1): every alarm committed its "already alarmed" state
        BEFORE emitting and discarded the emit's answer, so an alarm that was never delivered was never
        re-raised - not after the channel recovered, and not after a restart, because `gap_alerted` is
        PERSISTED. Mail was never at risk (the cursor holds correctly throughout); it was the ALARMS
        that vanished, which is worse than it sounds because the tool's headline promise is that it
        pins LOUDLY rather than in silence.

        The fallback is stderr, NOT a retry down the event channel - that channel is the thing that
        just failed, and reporting a fault through the faulty channel is how the fault stays invisible
        (DESIGN.md §176, the same reasoning as _delivery_failed). Deliberately does NOT gate the cursor
        on a lifecycle event: DESIGN.md §170 says lifecycle events are not acknowledged and not gated,
        and that stays true. "Do not gate the watermark on it" and "do not record that you alarmed when
        you did not" are different propositions, and only the first was ever documented.
        """
        delivered = self.lifecycle(event, **fields)
        if not delivered:
            sys.stderr.write("kijito-inbox-monitor: %s EVENT UNDELIVERED (persona %r): %s\n"
                             % (event.upper(), self.persona, log_text))
        return delivered

    def _pin_release_floor(self):
        """The reach a COMPLETE window must achieve to discharge a pin.

        Normally the watermark itself: a window reaching back to at-or-below the cursor visibly spans
        everything we have not confirmed. A CORRUPTION pin is the exception - it parks the watermark one
        below the window it re-emits, so that window's own floor is always cursor+1 and the ordinary test
        can never be met by it (Loom re-audit 7, HIGH 5: the pin never cleared, the cursor never moved,
        and the window was re-delivered on every poll for the life of the process AND across restarts).
        Taking the MAX keeps the ordinary rule exactly as strict as it was - a recorded floor can only
        ever be the span we re-emitted, never something below the watermark.
        """
        floor = self.cursor or 0
        if self.pin_release_at is not None:
            floor = max(floor, self.pin_release_at)
        return floor

    def _release_pin(self):
        """Discharge a forced pin AND everything that was holding it up.

        ONE implementation, because there are TWO authoritative proofs (a completed backward walk, and a
        complete window reaching the release floor) and they must leave identical state. They did not:
        the walk path cleared only `pin_forced`, so a released corruption pin went on persisting
        `state_corrupt: true` and its `pin_release_at` for the life of the file - a state file still
        describing a corruption that had been fully recovered. Found by running the repro against the
        LIVE api.kijito.ai and reading what was actually written back, which no fixture asserted.
        """
        self.pin_forced = False
        self.pin_release_at = None
        self.state_corrupt = False

    def _state_not_durable(self):
        if self.state_not_durable:
            return
        self.state_not_durable = True
        sys.stderr.write("kijito-inbox-monitor: WARNING the cursor for persona %r was written but its "
                         "durability is UNPROVEN; a crash may replay mail from an older cursor (further "
                         "reports suppressed until it persists cleanly)\n" % self.persona)

    def _state_durable_again(self):
        if not self.state_not_durable:
            return
        self.state_not_durable = False
        sys.stderr.write("kijito-inbox-monitor: cursor persistence for persona %r recovered\n" % self.persona)

    def _delivery_failed(self, mid):
        """Report a failed hand-off ONCE, and say what the watcher is doing about it.

        Deliberately stderr and NOT an `alert` event: the event channel is the thing that just failed, so
        an alarm about it would be routed through the broken pipe (exec mode re-runs the same failing
        command; sink mode writes to the file that just refused a write). Reporting a fault down the
        faulty channel is how the fault stays invisible. Keyed on the condition and self-clearing, like
        every other alarm here.
        """
        if self.delivery_blocked:
            return
        self.delivery_blocked = True
        sys.stderr.write("kijito-inbox-monitor: WARNING delivery of message %s to persona %r FAILED; HOLDING the "
                         "cursor below it so it is re-delivered rather than skipped (further reports "
                         "suppressed until delivery recovers)\n" % (mid, self.persona))

    def _delivery_recovered(self):
        if not self.delivery_blocked:
            return
        self.delivery_blocked = False
        sys.stderr.write("kijito-inbox-monitor: delivery to persona %r recovered; the cursor is advancing "
                         "again\n" % self.persona)

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
        declared, exact = poll.omitted, poll.omitted_exact
        if not poll.continuation_ok:
            # SILENCE IS NOT AN ANSWER HERE EITHER. A window whose `next_before_id` is ABSENT or
            # MALFORMED has told us nothing about whether it withheld rows, so its "I omitted nothing"
            # cannot be taken as an assertion - the two fields are one statement and half of it is
            # unreadable. The WALK has refused to read that silence as exhaustion since Loom re-audit 5
            # (HIGH 1); the gap check never got the same rule, so a server that garbled the field while
            # declaring no omission advanced the watermark over anything it was hiding, silently and
            # with no alert. Same defect, one layer over. (Found by re-reading round 7 adversarially.)
            declared, exact = max(declared, 1), False
        if not poll.consistent:
            # A SELF-CONTRADICTORY WINDOW IS AN OMISSION WE CANNOT COUNT (Loom re-audit 7, HIGH 4). This
            # check used to read `poll.omitted` alone and never looked at the continuation at all, so a
            # window declaring "I withheld nothing" while handing back a cursor for older mail was taken
            # at its word - and the watermark stepped over whatever the continuation was pointing at.
            # There is no number to reach here, so it enters as an UNQUANTIFIED withholding: closable
            # only by a backward walk that exhausts the span, never by arithmetic.
            declared, exact = max(declared, 1), False
        if not declared or self.cursor is None or not self.armed or not items:
            return None
        floor = min(m["id"] for m in items)
        if floor <= self.cursor:
            return None
        return (self.cursor, floor, declared, exact)

    def _hidden_unread(self, poll):
        """Does the server hold unread mail this window did not show us? True / False / None (NO CLAIM).

        §5.2 A CHEAP ALARM SIGNAL - deliberately NOT a coverage mechanism. `unread_not_shown` is a COUNT
        with no cursor of its own, so it can say THAT something is out of view but never WHICH rows;
        coverage stays with the backward walk, which terminates (§5.1). This answers only the alarm
        question "is there unread mail I cannot see", where a superset is the right answer because you
        want to know regardless of WHY the mail is absent.

        THE TRAP, AND IT INVERTS THE OBVIOUS READING OF A ZERO. The server computes this field ONLY
        when it withheld something; otherwise it is 0 BY CONSTRUCTION. So `== 0` does NOT assert "no
        unread mail exists". VERIFIED LIVE against api.kijito.ai on a real inbox holding 4 unread:
            newest page      next_before_id=1179  unread_not_shown=0   <- computed, and truly nothing hidden
            walk page        next_before_id=1145  unread_not_shown=4   <- the whole inbox's unread, not this window's
            terminal page    next_before_id=null  unread_not_shown=0   <- 0 WITH 4 UNREAD SITTING ABOVE IT
        Reading that last 0 as "clear" is the false-negative this method exists to refuse. A FALSE
        assertion is therefore only avoidable by requiring POSITIVE evidence for the negative answer,
        never by trusting the number - which is why the two False branches below are justified by
        DIFFERENT facts and are not the redundancy they look like.

        EVALUATE ONLY ON THE NEWEST-PAGE POLL. On a backward-walk page `next_before_id is None` means
        merely "nothing OLDER than this page", not "nothing outside this window" - the terminal-page row
        above is exactly that case. poll_once() calls this with the un-cursored poll only, so the walk
        pages structurally cannot reach it.
        """
        n = poll.unread_not_shown
        if n is None:
            return None          # server made no statement (older API) -> assert nothing in either direction
        if n > 0:
            return True          # unread mail exists that this response did not include
        if poll.next_before_id is not None:
            return False         # the 0 was genuinely COMPUTED against a withheld remainder
        if poll.omitted == 0:
            return False         # complete window: nothing older exists and nothing was withheld
        # Contradictory: rows were declared omitted, yet no cursor leads to them. The 0 is unexplained,
        # so make no claim rather than report a clear we cannot justify.
        return None

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
            # VALIDATE THE PAGE BEFORE TAKING ANY COVERAGE FROM IT. Every check below rejects a page
            # whose own account of itself does not hold together; a page that fails one of them cannot
            # be trusted to have handed back the rows it appears to contain, so it may not close a span
            # even when it seems to reach the watermark.
            if not poll.consistent:
                # SELF-CONTRADICTORY PAGE, IN EITHER DIRECTION. Withheld-rows + "nothing older" (Loom
                # re-audit 6, HIGH 3) and withheld-nothing + "there is more" (Loom re-audit 7, HIGH 4)
                # are the same defect facing opposite ways: the two halves of the page's declaration
                # disagree, so believing EITHER half steps over what the other one just asserted.
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
            if poll.next_before_id is not None and poll.next_before_id != oldest:
                # The chain skips rows between `oldest` and the continuation. Following it would
                # walk over them and still report the span covered.
                # Checked BEFORE the reach-back return below: a page that reaches the watermark while
                # skipping rows is still a page whose row set we cannot vouch for, and taking coverage
                # from it would be trusting the one page we just caught misdescribing itself.
                return (rows, False)
            if oldest <= stop_at:
                return (rows, True)           # walked back past the watermark: span fully seen
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
                # THE RECOVERY EDGE IS THE SAME DEFECT FACING THE OTHER WAY (argus, re-audit 11 - a
                # site the review did not name). Committing "UP" and discarding the emit means a
                # consumer that saw the DOWN alert never learns the source came back: it is left
                # holding an alarm it can NEVER clear, because this edge is crossed exactly once.
                # The transition must commit (it is the FSM); the announcement gets stderr.
                self.fsm_state = "UP"
                self._alarm("recovered", "source recovered", cursor=self.cursor)
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
                # §5.4 Record who AUTHORED what, from the window we already have. Done before any cursor
                # or dedup logic: authorship is evidence about the SENDER and is worth collecting whether
                # or not the message is new to US - a message we have already delivered still proves its
                # author was alive when they sent it.
                note_authorship(items)
                note_observation_floor(self.persona, items)
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
                        # THE PIN NOW CARRIES ITS OWN RELEASE FLOOR (Loom re-audit 7, HIGH 5) - see
                        # _pin_release_floor(). Set here, and again below if this first window was empty.
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

                # THE CORRUPTION PIN'S RELEASE FLOOR, in ONE place so the arming poll and a later one
                # cannot disagree (Loom re-audit 7, HIGH 5). A corrupt arm parks the watermark at
                # min(visible)-1 so it can re-emit the whole window; that makes the ordinary release test
                # - a complete window reaching back to at-or-below the watermark - unsatisfiable by
                # construction, because the reach IS min(visible) and min(visible) > min(visible)-1. So
                # the pin recorded the floor it must reach back to instead. Also set on a LATER poll when
                # the arming window was EMPTY: there was no floor to record then, and leaving it unset
                # would freeze the watermark exactly as before, one poll further on.
                if self.pin_forced and self.state_corrupt and self.pin_release_at is None and items:
                    self.pin_release_at = min(m["id"] for m in items)
                if recovered:
                    # Same recovery edge as the fast path above, and the commit is 60+ lines earlier
                    # (`fsm_state = "UP"; recovered = True`), which is why a block-local detector could
                    # not pair them - it was found by reading, prompted by its twin.
                    self._alarm("recovered", "source recovered", cursor=self.cursor)
                if diag:
                    self.lifecycle(diag[0], **diag[1])
                if do_arm:
                    self.lifecycle("armed", cursor=self.cursor)
                # §5.1 A BOUNDED WINDOW MUST NOT SILENTLY SWALLOW MAIL.
                # The server returns the NEWEST messages that fit, and declares what it left out. If it
                # omitted anything AND the window does not reach back to our cursor, un-emitted mail can
                # be sitting in the uncovered gap - and advancing the cursor past it loses it forever.
                window_cursor = self.cursor   # the watermark AS THIS WINDOW SAW IT, before any advance
                gap = self._uncovered_gap(poll, items)
                pinned = False
                release_earned = False
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
                    # RELEASE THE FORCED PIN (Loom re-audit 5, MEDIUM) - but not here, and not yet. A
                    # forced pin was held because tracking was lost, and a COMPLETED walk is the
                    # authoritative evidence that replaces it; leaving it set froze the watermark
                    # permanently. The DECISION is made here, where the evidence is; the ACT is deferred
                    # until after delivery, because a pin must not be discharged on a poll that failed to
                    # hand over what it was holding (see the release site below).
                    release_earned = closed
                    pinned = not closed
                    # Alert identity is the PINNED WATERMARK, not the window floor. The floor drifts upward
                    # as new mail arrives, so keying on it re-fires for what is the same unresolved span;
                    # the watermark is stable for exactly as long as the gap is unresolved. Persisted, so a
                    # restart does not re-announce it either.
                    if pinned and self.gap_alerted != cursor_at:
                        # ★ LATCH ONLY ON DELIVERY (re-audit 11, F1). `gap_alerted` is a PURE
                        # ANNOUNCEMENT MARKER - it drives no behaviour, it only records "this span was
                        # announced" - and it is PERSISTED, so committing it before the emit meant an
                        # undelivered alarm was suppressed for the life of the span AND across restarts.
                        # The gap condition is re-derived every poll, so re-raising costs nothing and
                        # self-clears the moment it is genuinely delivered.
                        gap_reason = ("bounded-window: server omitted %d message(s) and the window "
                                      "started at id %s above cursor %s; a backward walk recovered %d "
                                      "from inside the span but did not reach the watermark, so "
                                      "it stays PINNED at %s"
                                      % (omitted, window_floor, cursor_at, len(gap_recovered), cursor_at))
                        if self._alarm("alert", gap_reason,
                                       reason=gap_reason,
                                       omitted=omitted, window_floor=window_floor, cursor_at=cursor_at,
                                       reconciled=len(gap_recovered), pinned=True):
                            self.gap_alerted = cursor_at

                # DELIVERY IS ACKNOWLEDGED, NOT ASSUMED (Loom re-audit 7, HIGH 1). The cursor IS the
                # acknowledgement - once it advances past an id, that message is never fetched again - so
                # it may only advance over messages the emitter actually DELIVERED. An --exec that exits
                # non-zero, times out, or cannot be spawned used to advance it anyway: the wake hook that
                # is the entire point of exec mode failed, the message was never retried, and the watcher
                # reported success. Delivery stops at the FIRST failure so a consumer never sees message
                # N+1 before a retried N; the guarantee is at-least-once IN ORDER, because a duplicate is
                # recoverable and a skip is not.
                delivered, blocked_at = set(), None
                for m in new_items:
                    if blocked_at is not None:
                        break
                    row = dict(m)
                    row["_persona"] = self.persona
                    if self.emitter.new(row) is True:
                        delivered.add(m["id"])
                    else:
                        blocked_at = m["id"]
                        self._delivery_failed(m["id"])
                # THE DURABILITY BARRIER (Loom re-audit 7, MEDIUM): the event must be on stable storage
                # BEFORE the cursor that acknowledges it. If the sink cannot be synced, NOTHING emitted
                # this poll counts as delivered - the acknowledgement is retracted wholesale rather than
                # left half-true.
                if delivered and not self.emitter.sync(self.persona):
                    blocked_at = min(delivered) if blocked_at is None else min(blocked_at, min(delivered))
                    delivered = set()
                if blocked_at is None:
                    self._delivery_recovered()

                # §5.2 UNREAD MAIL WE CANNOT SEE. Fires on the FALSE->TRUE edge and releases itself when
                # the condition clears, so it needs no ack: an ack would let someone silence "there is
                # mail you are not being shown" while it was still true.
                hidden = self._hidden_unread(poll)
                if hidden is True:
                    # Routed like the stranded-mail alarm, but failing the OPPOSITE way on purpose. That
                    # one withholds when the directory is unknown because alarming would flag EVERY
                    # persona; this one concerns the target's OWN inbox, so the worst case of firing is a
                    # line in a stream nobody reads, while the worst case of withholding is the silent
                    # wake gap this whole tool exists to prevent. Suppressed only for an inbox the
                    # directory positively does not know - and the flag is then left UNSET so the alarm
                    # can still announce itself if that inbox later becomes directory-backed.
                    if not self.unread_hidden and self.directory_backed:
                        floor = min((m["id"] for m in items), default=None)
                        unread_reason = (
                            "unread-not-shown: the server reports %d unread message(s) in this "
                            "inbox that this window did not include. OBSERVATION, NOT A "
                            "DIAGNOSIS: the count covers unread mail ANYWHERE in the inbox, "
                            "including messages already delivered to this stream that the agent "
                            "has not read, so it is not by itself evidence of missed mail. "
                            "Coverage of an un-emitted span is proven by the backward walk, "
                            "never by this count." % poll.unread_not_shown)
                        # ★ LATCH ONLY ON DELIVERY, and go through _alarm for the stderr fallback
                        # (re-audit 11, F1 - this feature predates that rule and was written against the
                        # old `lifecycle` + commit-first shape). `unread_hidden` is a PURE ANNOUNCEMENT
                        # LATCH: it drives no behaviour, it only records "this condition was announced",
                        # and it is PERSISTED - so committing it before the emit would suppress an
                        # UNDELIVERED alarm for the life of the condition AND across restarts. The
                        # condition is re-derived every poll, so re-raising costs nothing and self-clears
                        # the moment it is genuinely delivered. See [[22470]] for the discriminating
                        # question (announcement latch vs behavioural state).
                        if self._alarm("alert", unread_reason,
                                       reason=unread_reason,
                                       unread_not_shown=poll.unread_not_shown,
                                       window_floor=floor, cursor_at=window_cursor,
                                       # The discriminating FACT, left for the reader to interpret: when
                                       # the window reaches back past the watermark, everything above it
                                       # is visible, so the unseen unread can only be mail already
                                       # delivered.
                                       above_watermark=(None if floor is None or window_cursor is None
                                                        else floor > window_cursor)):
                            self.unread_hidden = True
                elif hidden is False:
                    self.unread_hidden = False   # condition cleared -> re-arm, so a recurrence is announced
                # hidden is None -> the server made no statement; hold the current state and claim nothing

                # ★ A PIN IS NOT DISCHARGED ON A POLL THAT COULD NOT DELIVER (found by adversarially
                # re-reading my own round-7 work, the way loom would). Both proofs answer "did the SERVER
                # withhold anything" - neither says a word about whether WE handed the window over. On a
                # corrupt-state arm the watermark sits at min(visible)-1, so releasing while delivery was
                # blocked threw away the release floor AND the state_corrupt flag while the cursor was
                # still parked below the whole mailbox. A restart then re-forced the pin from the
                # surviving pin_evidence_intact=False - now with NO floor - and `reach <= cursor` is
                # unsatisfiable when the cursor sits below the oldest message that exists. Measured: the
                # watermark froze at 99 forever and correctness fell back entirely onto emitted_above
                # growing without bound. Holding the pin one more poll costs nothing; the delivery gate
                # already holds the cursor, and the pin self-clears the moment delivery recovers.
                if release_earned and blocked_at is None:
                    self._release_pin()

                if not pinned:
                    # A COMPLETE window that reaches back past the watermark proves everything above it is
                    # visible, so a leftover pin can be released even when there is nothing NEW to emit.
                    # Gating this on `new_items` left a restored pin stuck forever whenever the window
                    # contained only ids we had already delivered - the exact state a restart lands in.
                    reach = min((m["id"] for m in items), default=None)
                    # `continuation_ok` belongs here for the same reason it belongs in the gap check:
                    # a window that did not answer "is there more?" cannot be the PROOF that there is not.
                    complete = (poll.omitted == 0 and poll.consistent and poll.continuation_ok
                                and reach is not None and reach <= self._pin_release_floor())
                    if self.pin_forced and complete and blocked_at is None:
                        # The other authoritative proof: nothing was withheld AND the window reaches back
                        # past the watermark, so there is no span left to be uncertain about. Without this
                        # a forced pin that never sees a gap again could never clear, and the watermark
                        # would stay frozen for the life of the process.
                        self._release_pin()
                    if self.pin_forced:
                        high = None           # still forced: the watermark holds
                    else:
                        high = max(sorted(delivered)
                                   + ([max(m["id"] for m in items)] if complete else []), default=None)
                    if blocked_at is not None and high is not None:
                        # THE GATE ITSELF: never acknowledge past a message we could not hand over, not
                        # even via a complete window. `complete` proves the SERVER hid nothing; it says
                        # nothing about whether WE delivered what it showed us.
                        high = min(high, blocked_at - 1)
                    if high is not None and high > (self.cursor or 0):
                        self.cursor = high
                        # Watermark moved, so anything at or below it is confirmed and needs no tracking.
                        self.emitted_above = {i for i in self.emitted_above if i > self.cursor}
                        if not self.emitted_above:
                            self.gap_alerted = None
                            self.pin_evidence_intact = True

                # EVERY DELIVERED ID THE WATERMARK DOES NOT COVER IS REMEMBERED, whatever left it
                # uncovered (Loom re-audit 7, HIGH 5). This used to live inside the `pinned` branch
                # alone, so the OTHER ways of not advancing - a forced pin with no gap in sight, a
                # delivery that failed further up the batch - delivered mail and then forgot they had.
                # The corruption pin hit exactly that: it could not advance and it recorded nothing, so
                # it re-emitted its entire window on every poll and every restart, forever.
                uncovered = {i for i in delivered if i > (self.cursor or 0)}
                if uncovered:
                    self.emitted_above.update(uncovered)
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
                            # ⚠️ THIS COMMIT IS NOT AN ANNOUNCEMENT LATCH - it is a CORRECTNESS state that
                            # governs how the span may ever be closed, so it MUST be committed whether or
                            # not the alarm is delivered. Only the ANNOUNCEMENT needs the second channel,
                            # which _alarm provides (re-audit 11, F1). Getting this backwards - refusing to
                            # record evidence loss because a sink was broken - would trade a lost alarm for
                            # a lost invariant.
                            self.pin_evidence_intact = False
                            # A durable event, not just stderr: this is a correctness degradation somebody
                            # has to act on, and stderr is not something a consumer watches. _alarm still
                            # falls back to stderr, so an undelivered one is not silent.
                            pin_reason = ("bounded-window: pin at cursor %s outlived its tracking "
                                          "budget and forgot %d delivered id(s). Some mail may be "
                                          "re-emitted, and this span can no longer be closed by "
                                          "reconciliation - it needs an authoritative backward read"
                                          % (self.cursor, dropped))
                            self._alarm("alert", pin_reason, reason=pin_reason,
                                        cursor_at=self.cursor, forgot=dropped,
                                        pinned=True, evidence_lost=True)

            else:
                self.failures += 1
                if self.failures == args.alert_after and self.fsm_state == "UP":
                    # THE DEAD-MAN'S SWITCH. The FSM transition MUST commit (it drives the whole
                    # liveness model, and the firing condition is an EQUALITY on `failures`, so a
                    # reverted transition would never re-fire - the edge is crossed exactly once).
                    # So the state commits and the ANNOUNCEMENT gets the guaranteed second channel
                    # (re-audit 11, F1/A1). Before this, a broken sink meant the source could go down
                    # and NOTHING was ever emitted or logged - the one event README sells as the
                    # dead-man's switch, silently absent.
                    self.fsm_state = "DOWN"
                    down_reason = poll.reason or "unreachable"
                    self._alarm("alert", "source is DOWN: %s" % down_reason,
                                reason=down_reason,
                                consecutive_failures=self.failures,
                                seconds=self.failures * args.poll_seconds)

        if self.state_file is not None:
            durable = self.state_file.save(self.cursor, self.fsm_state, self.failures,
                                 self.emitted_above, self.gap_alerted,
                                 pin_forced=self.pin_forced,
                                 pin_evidence_intact=self.pin_evidence_intact,
                                 state_corrupt=self.state_corrupt,
                                 pin_release_at=self.pin_release_at,
                                 unread_hidden=self.unread_hidden)
            # ★ CONSUME THE ANSWER (Loom re-audit 9, MEDIUM). Round 8 taught me to RETURN a durability
            # status; this is the same defect one layer out - I produced an answer and then discarded it
            # at the call site, which is the exact thing the previous round was about. A cursor whose
            # persistence is unproven means a crash may replay mail, and the harm is the SILENCE.
            if durable is False:
                self._state_not_durable()
            else:
                self._state_durable_again()

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


def refresh_directory_backing(args, directory, targets):
    """Mark each target with whether the persona DIRECTORY knows its inbox (§5.2 alarm routing).

    Refreshed EVERY tick rather than stamped when a target is built, because a brand-new persona's first
    mail arrives through discover_from_counts BEFORE the periodic /api/personas rescan sees it: a
    creation-time flag would brand a perfectly real persona as unbacked and then never revisit it, since
    rediscovery skips personas already watched.

    Compared EXACTLY, never casefolded - the same asymmetry as stranded_inboxes(). The server's inbox
    namespace is case-SENSITIVE, so a case-variant is a DIFFERENT inbox and must not inherit the real
    one's backing.

    Two no-ops, both deliberate: an explicit --persona/--personas subset was hand-picked by an operator
    who is by definition consuming those streams, and an EMPTY directory is missing data rather than
    evidence of absence. In both cases the existing (optimistic) value stands.
    """
    if not watches_all_personas(args) or not directory:
        return
    known = {p for p in directory if p}
    for target in targets:
        if target.persona:
            target.directory_backed = target.persona in known


# §5.4 Attributable authorship, observed for free. Every inbox window the watcher already fetches carries a
# `from` on every row, so watching all personas means seeing who AUTHORED what without a single extra request.
# {persona: (highest message id it authored, that message's created stamp)}.
_LAST_AUTHORED = {}


def note_authorship(items):
    """Record the newest message id seen from each AUTHOR (§5.4).

    Attributable in the way inbox-read and `/api/presence` are not: only B produces B's outbound, and no
    third party can manufacture or erase it by reading something. That is the whole reason this signal is
    worth collecting - a liveness check built on a bit any observer can flip is not a check.

    Keyed EXACTLY, never casefolded: the server's persona namespace is case-SENSITIVE, so `Loom` and `loom`
    are different identities and must not merge (the identity half of the case asymmetry, §5.3).

    Only the newest window is needed. Backward-walk rows are always OLDER than the window floor they were
    reached from, so they cannot raise a maximum; skipping them costs no evidence.
    """
    for m in items or []:
        who = m.get("from")
        mid = m.get("id")
        if not isinstance(who, str) or not who or not isinstance(mid, int):
            continue
        prev = _LAST_AUTHORED.get(who)
        # Compare by `id`, NEVER by `created`: timestamps are stamped pre-lock while ids are assigned under
        # it, so two concurrent senders can carry timestamps in the opposite order from their ids.
        if prev is None or mid > prev[0]:
            _LAST_AUTHORED[who] = (mid, m.get("created"))


# Set once, when the watch loop starts. Everything before it is INVISIBLE to this process: the table is built
# from windows observed since then, so a question about earlier activity must answer UNKNOWN rather than
# "none". Without this floor a fresh producer would report every persona as inactive for one tick.
_OBSERVED_SINCE = None

# Per-inbox coverage: {persona: the lowest id ever seen in THAT inbox's window}. A persona's outbound can
# land in ANY inbox, so a claim that they have authored nothing is only as good as the WORST-covered inbox.
_INBOX_FLOORS = {}


def note_observation_floor(persona, items):
    ids = [m["id"] for m in (items or []) if isinstance(m.get("id"), int)]
    if not ids:
        return
    low = min(ids)
    cur = _INBOX_FLOORS.get(persona)
    if cur is None or low < cur:
        _INBOX_FLOORS[persona] = low


def observation_floor_id():
    """The id below which "nobody authored anything" CANNOT be asserted. MAXIMUM, deliberately.

    The tempting version is the minimum - the oldest message we have laid eyes on anywhere - and it is
    WRONG in the dangerous direction. Each inbox window reaches back only as far as its own floor, so
    between the lowest and highest floor there are inboxes we have NOT seen into. A message authored in
    that span, addressed to a poorly-covered inbox, is invisible to us; reporting "no activity" there is
    a silence we did not observe.
    Caught on live data: the watcher had seen ids down to 1160 (in one inbox) while another inbox's window
    only reached 1179, so a question about id 1165 looked answerable and was not.
    Taking the maximum can only make us answer NOT-OBSERVABLE more often, which is the safe direction.
    """
    return max(_INBOX_FLOORS.values()) if _INBOX_FLOORS else None


def evaluate_activity(report, persona, floor_id):
    """Answer the question from a PUBLISHED report (§5.4). True / False / None (NOT OBSERVABLE).

    Takes the report rather than reading module state, so the identical logic serves the running watcher
    and a one-shot `--check-activity` in a separate process. A second implementation of a tri-state this
    subtle is a second chance to get it wrong.

    The tri-state is the whole point, and it is the same discipline as §5.2: absence of evidence is
    evidence of absence only if you were actually watching.
    """
    if not isinstance(report, dict) or not report.get("observed_since"):
        return None                       # nothing has been observed at all
    seen = (report.get("last_authored") or {}).get(persona)
    if isinstance(seen, dict) and isinstance(seen.get("id"), int) and seen["id"] > floor_id:
        return True                       # positive evidence, and positive evidence needs no floor
    floor = report.get("observation_floor_id")
    if not isinstance(floor, int) or floor_id < floor:
        return None                       # the question predates what this report can speak for
    return False


def current_activity_report():
    """The in-process view, in the same shape write_activity_file() publishes."""
    return {
        "observed_since": _OBSERVED_SINCE,
        "observation_floor_id": observation_floor_id(),
        "last_authored": {p: {"id": i, "created": c} for p, (i, c) in _LAST_AUTHORED.items()},
    }


def activity_since(persona, floor_id):
    """Has `persona` authored anything after `floor_id`? True / False / None (NOT OBSERVABLE)."""
    return evaluate_activity(current_activity_report(), persona, floor_id)


# Words that assert a CAUSE this data cannot distinguish. Deadlocked, unreachable and thinking-hard all look
# identical here and need opposite remedies - a deadlock wants a ping, an unreachable member wants a human to
# restart its bridge. Enforced by a test, not just documented, because a rule that lives only in prose does
# not run.
FORBIDDEN_DIAGNOSES = ("deadlock", "stuck", "wedged", "dead", "down", "offline", "crashed", "hung")


def activity_observation(persona, floor_id, waits, last_evidence=None, report=None):
    """The exact text a waiter emits about the member it is waiting on (§5.4). OBSERVATION ONLY.

    Carries the wait count and the last-evidence stamp alongside the finding, so a reader can judge
    magnitude without a second query - a bare "no activity" invites the reader to supply the diagnosis
    themselves, which is the failure this wording exists to prevent.
    """
    rep = report if report is not None else current_activity_report()
    row = (rep.get("last_authored") or {}).get(persona)
    seen = (row.get("id"), row.get("created")) if isinstance(row, dict) else None
    evidence = last_evidence if last_evidence is not None else (seen[1] if seen else None)
    tail = ("; %s's last observed message was %s" % (persona, evidence)) if evidence else (
        "; no message from %s has been observed at all" % persona)
    return ("no activity from %s since your message at id %s; you have waited %d heartbeat(s)%s "
            "(checked: authored mail. This states what was OBSERVED, not why: not-yet-read, unable to "
            "receive, and still working are indistinguishable from here and need different responses.)"
            % (persona, floor_id, waits, tail))


def check_activity(path, persona, floor_id, waits):
    """One-shot `--check-activity`: read a published report and answer for ONE persona (§5.4).

    Exit codes are the contract, because this is meant to be called from a shell heartbeat:
        0  evidence of activity        (nothing to report)
        1  no activity in a span we actually covered   -> the observation is printed
        2  NOT OBSERVABLE / unusable report            -> print why; assert nothing
    2 is deliberately distinct from 1. Collapsing them would turn "I was not watching" into "they were
    silent", which is the false assertion this whole signal is built to refuse.
    """
    try:
        with open(path) as f:
            report = json.load(f)
    except (OSError, ValueError) as e:
        sys.stderr.write("kijito-inbox-monitor: activity report unreadable (%s): %s\n" % (path, e))
        return 2
    verdict = evaluate_activity(report, persona, floor_id)
    if verdict is True:
        row = (report.get("last_authored") or {}).get(persona) or {}
        sys.stdout.write("active: %s authored id %s at %s\n"
                         % (persona, row.get("id"), row.get("created")))
        return 0
    if verdict is None:
        sys.stdout.write("not observable: this report cannot speak about id %s for %s "
                         "(observed since %s, floor id %s). No claim either way.\n"
                         % (floor_id, persona, report.get("observed_since"),
                            report.get("observation_floor_id")))
        return 2
    sys.stdout.write(activity_observation(persona, floor_id, waits, report=report) + "\n")
    return 1


def write_activity_file(path, now_iso=None):
    """Publish the authorship table so any harness can evaluate the predicate without inventing a scan.

    This exists to keep consumers OUT of the dangerous shape. Answering "has B sent anything" from a client
    otherwise means polling every persona's inbox on a timer, where one missing `mark_read=false` destroys
    read-state fleet-wide. The watcher already holds the answer, gathered safely.
    """
    d = {
        "observed_since": _OBSERVED_SINCE,
        "observation_floor_id": observation_floor_id(),
        "updated": now_iso or _now_iso(),
        # A question about anything at or below observation_floor_id is NOT ANSWERABLE from this file.
        "last_authored": {p: {"id": i, "created": c} for p, (i, c) in sorted(_LAST_AUTHORED.items())},
        # Personas with mail a SENDER escalated. Published alongside authorship because the pair is what
        # separates "idle by design" from "nobody is coming": urgency is an expectation someone declared,
        # and silence only means something once something was expected. A persona absent here was not
        # reported on, which is not the same as zero.
        "urgent_unread": {p: n for p, n in sorted(_URGENT_UNREAD.items()) if n},
    }
    dirn = os.path.dirname(os.path.abspath(path)) or "."
    try:
        os.makedirs(dirn, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dirn, prefix=".kijmon-act-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(d, f)
        os.replace(tmp, path)             # atomic: a reader never sees a half-written table
    except OSError as e:
        sys.stderr.write("kijito-inbox-monitor: WARNING activity-file write failed (non-fatal): %s\n" % e)


_REPORTED_URGENT_QUIET = set()


def urgent_unanswered(directory):
    """Directory personas holding SENDER-ESCALATED mail while showing no observed activity (§5.5).

    THE PREDICATE, and the reason this alarm is buildable at all:
        unread_urgent > 0   AND   activity_since(persona) is False
    An "is this agent stuck" alarm normally cannot exist, because an agent idle BY DESIGN and an agent that
    is wedged look identical from outside - so it fires on every dormant persona and rots into noise. What
    breaks the tie is a declared EXPECTATION, and `unread_urgent` is one: not the recipient declaring
    liveness, but a SENDER declaring that this needs attention now. Silence only means something once
    something was expected.

    Both halves must be POSITIVE. `activity_since` is a tri-state and only `False` counts - a NOT-OBSERVABLE
    answer means the watcher was not running for the span in question, and reporting that as silence is the
    fabrication this whole signal exists to refuse.

    Restricted to DIRECTORY personas on purpose, which keeps this disjoint from the stranded-mail alarm:
    that one is for inboxes nobody OWNS, this one is for real members who are not responding. Two alarms
    with two philosophies drift apart and then disagree about the same inbox.
    """
    out = []
    floor = observation_floor_id()
    if floor is None:
        return out            # nothing observed at all: assert nothing
    known = {p for p in (directory or ()) if p}
    for persona, n in sorted(_URGENT_UNREAD.items()):
        if n and persona in known and activity_since(persona, floor) is False:
            out.append((persona, n))
    return out


def report_urgent_unanswered(directory, targets, emitter):
    """Emit the §5.5 observation. Self-clears when EITHER half of the predicate clears; never an ack.

    An ack would let someone silence "nobody is answering escalated mail" while it stayed true, which is
    how a dead-letter surface rots. Releasing the suppression the moment the condition lifts means a
    recurrence is announced again without anyone having to remember to reset anything.
    """
    directory = directory or ()
    current = urgent_unanswered(directory)
    names = {p for p, _ in current}
    _REPORTED_URGENT_QUIET.intersection_update(names)   # release: either half clearing re-arms the alarm
    fresh = [(p, n) for p, n in current if p not in _REPORTED_URGENT_QUIET]
    if not fresh:
        return []
    detail = []
    for persona, n in fresh:
        _REPORTED_URGENT_QUIET.add(persona)
        seen = _LAST_AUTHORED.get(persona)
        detail.append("%s (%d urgent unread; %s)" % (
            persona, n,
            ("last observed message %s" % seen[1]) if seen else "no message from them observed at all"))
    known = {p for p in directory if p}
    # One summarising event per watcher, exactly as the stranded alarm does - discovering several at once
    # must not become a wake storm.
    for watcher in sorted({t.persona for t in targets if t.persona and t.persona in known}):
        emitter.lifecycle(
            "alert", persona=watcher,
            reason=("urgent-unanswered: %d member(s) hold mail a sender marked URGENT while no activity "
                    "from them has been observed: %s. OBSERVATION, NOT A DIAGNOSIS: not-yet-read, unable "
                    "to receive, and still working are indistinguishable from here and need different "
                    "responses. Checked: authored mail." % (len(fresh), ", ".join(detail))),
            urgent_unanswered=[p for p, _ in fresh])
    return [p for p, _ in fresh]


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
    global _OBSERVED_SINCE
    _OBSERVED_SINCE = _now_iso()   # §5.4 nothing before this instant is observable to this process
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
        refresh_directory_backing(args, directory_personas, targets)
        for target in targets:
            target.poll_once(counts_available, unread_counts)
        # AFTER the polls, so this tick's authorship is already recorded - evaluating before them would
        # judge a member silent using a view that predates the very message proving they are not.
        if counts_available and not args.no_stranded_alerts:
            report_urgent_unanswered(directory_personas, targets, emitter)
        if args.activity_file:
            write_activity_file(args.activity_file)
        if seam.stop:
            break
        if held:
            continue  # the server-held long-poll already supplied the inter-poll wait - loop straight back
        seam.wait(lp_backoff if lp_backoff else args.poll_seconds)

    emitter.close()
    for target in targets:
        if target.state_file is not None:
            target.state_file.unlock()
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
    p.add_argument("--activity-file",
                   help="Publish who AUTHORED mail most recently, as JSON, refreshed each tick. Lets a "
                        "harness answer 'has X been active since my message?' from data this watcher "
                        "already collects, instead of polling every inbox itself. Off by default.")
    p.add_argument("--check-activity", metavar="PERSONA",
                   help="One-shot: read --activity-file and report whether PERSONA has authored anything "
                        "since --since-id. Exits 0 active, 1 no activity in a covered span (prints the "
                        "observation), 2 NOT OBSERVABLE. Reads only; no token or network needed.")
    p.add_argument("--since-id", type=int,
                   help="With --check-activity: the message id you are awaiting a reply to.")
    p.add_argument("--waits", type=int, default=1,
                   help="With --check-activity: how many of your own heartbeats you have waited "
                        "(reported verbatim, so a reader can judge magnitude). Default 1.")
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
    # A pure read of an existing report: no token, no network, no state file, no watch loop. Placed
    # before validate_args so a heartbeat can call it without satisfying the watcher's own config.
    if args.check_activity:
        if not args.activity_file:
            sys.stderr.write("kijito-inbox-monitor: FATAL --check-activity requires --activity-file\n")
            return 2
        if args.since_id is None:
            sys.stderr.write("kijito-inbox-monitor: FATAL --check-activity requires --since-id "
                             "(the message id you are waiting on a reply to)\n")
            return 2
        return check_activity(args.activity_file, args.check_activity, args.since_id, args.waits)
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
