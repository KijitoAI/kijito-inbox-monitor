import json
import os
import tempfile
import unittest
import urllib.error

import kijito_inbox_monitor as km


class Args:
    def __init__(self, persona=None, personas=None, all_personas=False):
        self.persona = persona
        self.personas = personas
        self.all_personas = all_personas


class FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


class FakeOpener:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def open(self, req, timeout=None):
        self.calls.append((req.full_url, timeout))
        if self.exc:
            raise self.exc
        return self.response


def read_file(path):
    """Read a file and CLOSE it. `open(p).read()` leaves the handle to the GC, which is what the suite's
    ResourceWarnings were made of - and this repo treats a leaked fd as a real defect (see StateFile)."""
    with open(path) as f:
        return f.read()


_AUTO = object()


def server_page(items, omitted=0, exact=True, next_before_id=_AUTO):
    """Build a page BODY the way the real server builds one, then parse it with the PRODUCTION validator.

    Two rules the live API holds that a hand-built Poll silently does not:
      · a withholding is DECLARED - `truncated` (no quantity) and/or `size_dropped` (a count); and
      · `next_before_id` is set EXACTLY when rows were withheld, and it is the OLDEST row returned
        (Kijito web_api.py builds both from one expression, commented "present exactly when mail was
        withheld"). Confirmed against the live API across 14 pages, including the exactly-at-limit edge.

    A fixture that ignores either rule describes a page NO SERVER CAN SEND, and a guard tested only
    against such a page is a guard tested against nothing - which is how one of these tests came to
    exercise the oldest-row check while claiming to cover the anti-spin one. Going through
    fetch_from_payload() also means these tests run the SAME validation production runs, instead of a
    second copy of the rules that can drift away from it.

    Pass `next_before_id=` explicitly to build a DELIBERATELY malformed page (that is the point of the
    contract tests); leave it alone and you get the well-formed pairing.
    """
    payload = {"result": list(items)}
    if omitted:
        if exact:
            payload["size_dropped"] = omitted
        else:
            payload["truncated"] = True                     # a count-limit truncation states no quantity
            if omitted > 1:
                payload["size_dropped"] = omitted - 1
    if next_before_id is _AUTO:
        payload["next_before_id"] = min(m["id"] for m in items) if (omitted and items) else None
    else:
        payload["next_before_id"] = next_before_id
    return km.fetch_from_payload(payload)


class MultiPersonaHelpersTest(unittest.TestCase):
    def test_state_path_derives_persona_file_from_base(self):
        self.assertEqual(km._state_path_for_persona("/tmp/hive.json", "codex"),
                         "/tmp/hive.codex.json")

    def test_state_path_is_idempotent_when_base_already_names_persona(self):
        self.assertEqual(km._state_path_for_persona("/tmp/codex.json", "codex"),
                         "/tmp/codex.json")
        self.assertEqual(km._state_path_for_persona("/tmp/hive.codex.json", "codex"),
                         "/tmp/hive.codex.json")

    def test_state_path_sanitizes_persona_for_filename(self):
        self.assertEqual(km._state_path_for_persona("/tmp/hive.json", "team/person"),
                         "/tmp/hive.team_person.json")

    def test_state_path_casefolds_so_variants_cannot_collide_on_a_case_insensitive_fs(self):
        # On APFS/NTFS 'hive.Claude-chat.json' and 'hive.claude-chat.json' are the SAME file, so the
        # producer used to block on its own flock adopting the variant. One name -> one path.
        self.assertEqual(km._state_path_for_persona("/tmp/hive.json", "Claude-chat"),
                         km._state_path_for_persona("/tmp/hive.json", "claude-chat"))
        self.assertEqual(km._state_path_for_persona("/tmp/hive.json", "Claude-chat"),
                         "/tmp/hive.claude-chat.json")

    def test_state_path_idempotency_survives_a_case_variant_base(self):
        self.assertEqual(km._state_path_for_persona("/tmp/hive.Claude-chat.json", "Claude-chat"),
                         "/tmp/hive.Claude-chat.json")

    def test_requested_personas_dedupes_and_strips(self):
        args = Args(persona=[" codex ", ""], personas=["argus, river", "codex"])
        self.assertEqual(km.requested_personas(args, None, {}), ["codex", "argus", "river"])

    def test_requested_personas_defaults_to_directory_when_none_provided(self):
        opener = FakeOpener(FakeResponse(200, {"result": [{"persona": "codex"}, {"persona": "argus"}]}))
        args = Args()
        self.assertEqual(km.requested_personas(args, opener, {}), ["codex", "argus"])

    def test_watches_all_personas_only_for_default_or_explicit_all(self):
        self.assertTrue(km.watches_all_personas(Args()))
        self.assertTrue(km.watches_all_personas(Args(persona=["codex"], all_personas=True)))
        self.assertFalse(km.watches_all_personas(Args(persona=["codex"])))
        self.assertFalse(km.watches_all_personas(Args(personas=["codex,argus"])))

    def test_new_personas_preserves_discovered_order_and_never_drops(self):
        self.assertEqual(km.new_personas(["codex", "argus"], ["argus", "river", "codex", "ladybug"]),
                         ["river", "ladybug"])

    def test_new_personas_treats_a_case_variant_as_already_watched(self):
        self.assertEqual(km.new_personas(["claude-chat"], ["Claude-chat", "river"]), ["river"])

    def test_new_personas_collapses_case_variants_within_one_batch(self):
        self.assertEqual(km.new_personas([], ["Claude-chat", "claude-chat"]), ["Claude-chat"])

    def test_fetch_unread_counts_maps_persona_counts_and_absence_is_implicit_zero(self):
        opener = FakeOpener(FakeResponse(200, {
            "result": [
                {"persona": "argus", "unread": 9, "unread_urgent": 9},
                {"persona": "sterling", "unread": "bad"},
            ]
        }))
        available, counts = km.fetch_unread_counts(opener, km.NOTIFY_PENDING_URL, {})
        self.assertTrue(available)
        self.assertEqual(counts, {"argus": 9, "sterling": 0})
        self.assertEqual(counts.get("codex", 0), 0)

    def test_fetch_unread_counts_unavailable_on_http_or_bad_shape(self):
        available, counts = km.fetch_unread_counts(
            FakeOpener(FakeResponse(500, {"result": []})),
            km.NOTIFY_PENDING_URL,
            {},
        )
        self.assertFalse(available)
        self.assertEqual(counts, {})

        available, counts = km.fetch_unread_counts(
            FakeOpener(FakeResponse(200, {"result": {}})),
            km.NOTIFY_PENDING_URL,
            {},
        )
        self.assertFalse(available)
        self.assertEqual(counts, {})

    def test_fetch_unread_counts_unavailable_on_network_exception(self):
        available, counts = km.fetch_unread_counts(
            FakeOpener(exc=urllib.error.URLError("down")),
            km.NOTIFY_PENDING_URL,
            {},
        )
        self.assertFalse(available)
        self.assertEqual(counts, {})


class RotatingFileSinkTest(unittest.TestCase):
    def _sink(self, max_bytes, keep):
        path = os.path.join(tempfile.mkdtemp(), "events.ndjson")
        sink = km.RotatingFileSink(path, max_bytes=max_bytes, keep=keep)
        self.addCleanup(sink.close)
        return sink, path

    def test_rotates_at_threshold_and_keeps_writing_live_file(self):
        sink, path = self._sink(max_bytes=60, keep=3)
        for i in range(40):
            sink.write('{"n": %d}\n' % i)
        # the live file (followed by a tail -F consumer by NAME) still exists and holds the LATEST writes
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            live = f.read()
        self.assertIn('"n": 39', live)
        # at least one archive was produced
        self.assertTrue(os.path.exists(path + ".1"))

    def test_keep_caps_archive_count(self):
        sink, path = self._sink(max_bytes=10, keep=2)
        for i in range(60):
            sink.write('{"n": %d}\n' % i)
        d = os.path.dirname(path)
        archives = [p for p in os.listdir(d) if p.startswith("events.ndjson.")]
        self.assertLessEqual(len(archives), 2)

    def test_max_bytes_zero_disables_rotation(self):
        sink, path = self._sink(max_bytes=0, keep=3)
        for i in range(200):
            sink.write('{"n": %d}\n' % i)
        self.assertFalse(os.path.exists(path + ".1"))

    def test_no_data_loss_across_a_rotation_within_keep_budget(self):
        sink, path = self._sink(max_bytes=80, keep=10)
        n = 30
        for i in range(n):
            sink.write('{"n": %d}\n' % i)
        # reassemble live + archives (newest→oldest): every line must be present exactly once
        d = os.path.dirname(path)
        lines = []
        for name in sorted((p for p in os.listdir(d) if p.startswith("events.ndjson")),
                           key=lambda p: int(p.split(".")[-1]) if p[-1].isdigit() else -1, reverse=True):
            with open(os.path.join(d, name)) as f:
                lines.extend(f.read().splitlines())
        seen = sorted(json.loads(x)["n"] for x in lines if x.strip())
        self.assertEqual(seen, list(range(n)))

    def test_emitter_sink_writes_file_not_stdout(self):
        sink, path = self._sink(max_bytes=0, keep=3)
        em = km.Emitter("stdout-jsonl", None, 220, False, sink=sink)
        em.lifecycle("armed", cursor=7, persona="argus")
        with open(path) as f:
            data = f.read()
        self.assertIn('"event": "armed"', data)
        self.assertIn('"persona": "argus"', data)

    def test_suppress_author_drops_own_new_events_only(self):
        sink, path = self._sink(max_bytes=0, keep=3)
        em = km.Emitter("stdout-jsonl", None, 220, False, sink=sink, suppress_authors=["argus"])
        em.new({"id": 1, "from": "argus", "content": "mine", "_persona": "river"})    # self-echo → dropped
        em.new({"id": 2, "from": "river", "content": "theirs", "_persona": "argus"})  # real mail → kept
        em.lifecycle("alert", persona="argus", reason="x")                            # liveness → kept
        with open(path) as f:
            data = f.read()
        self.assertNotIn('"id": 1', data)
        self.assertIn('"id": 2', data)
        self.assertIn('"event": "alert"', data)

    def test_events_file_template_routes_per_persona(self):
        d = tempfile.mkdtemp()
        tmpl = os.path.join(d, "events.{persona}.ndjson")
        em = km.Emitter("stdout-jsonl", None, 220, False, sink_template=tmpl, max_bytes=0, keep=3)
        self.addCleanup(em.close)
        em.new({"id": 1, "from": "river", "_persona": "argus"})
        em.new({"id": 2, "from": "codex", "_persona": "ladybug"})
        em.lifecycle("armed", cursor=5, persona="argus")   # lifecycle carries persona too → same file
        with open(os.path.join(d, "events.argus.ndjson")) as f:
            argus = f.read()
        with open(os.path.join(d, "events.ladybug.ndjson")) as f:
            lady = f.read()
        self.assertIn('"id": 1', argus)
        self.assertIn('"event": "armed"', argus)
        self.assertNotIn('"id": 2', argus)        # ladybug's mail does NOT leak into argus's file
        self.assertIn('"id": 2', lady)
        self.assertNotIn('"id": 1', lady)


class ValidationGuardTest(unittest.TestCase):
    def _args(self, argv):
        return km.build_parser().parse_args(argv)

    def test_poll_seconds_must_be_positive(self):
        with self.assertRaises(km.FatalConfig):
            km.validate_args(self._args(["--persona", "argus", "--poll-seconds", "0"]))

    def test_seed_at_rejected_in_multipersona(self):
        with self.assertRaises(km.FatalConfig):
            km.validate_args(self._args(["--all-personas", "--seed-at", "5"]))

    def test_seed_at_allowed_for_single_persona(self):
        km.validate_args(self._args(["--persona", "argus", "--seed-at", "5"]))  # must not raise

    def test_keep_logs_min_one(self):
        with self.assertRaises(km.FatalConfig):
            km.validate_args(self._args(["--persona", "argus", "--keep-logs", "0"]))

    def test_events_file_and_template_mutually_exclusive(self):
        with self.assertRaises(km.FatalConfig):
            km.validate_args(self._args(["--events-file", "/a", "--events-file-template", "/b.{persona}.ndjson"]))

    def test_events_file_template_requires_placeholder(self):
        with self.assertRaises(km.FatalConfig):
            km.validate_args(self._args(["--events-file-template", "/no/placeholder.ndjson"]))


class AuthAndUrlTest(unittest.TestCase):
    class _HArgs:
        def __init__(self, token_file=None, auth_header=None):
            self.token_file = token_file
            self.auth_header = auth_header

    def setUp(self):
        self._saved = os.environ.pop("KIJITOMON_TOKEN", None)

    def tearDown(self):
        if self._saved is not None:
            os.environ["KIJITOMON_TOKEN"] = self._saved

    def test_missing_token_is_fatal(self):
        with self.assertRaises(km.FatalConfig):
            km.build_headers(self._HArgs())

    def test_env_token_yields_bearer_and_user_agent(self):
        os.environ["KIJITOMON_TOKEN"] = "secret123"
        h = km.build_headers(self._HArgs())
        self.assertEqual(h["Authorization"], "Bearer secret123")
        self.assertEqual(h["User-Agent"], km.USER_AGENT)
        self.assertIn("kijito-inbox-monitor/", km.USER_AGENT)

    def test_token_file_wins_over_env_and_custom_header(self):
        os.environ["KIJITOMON_TOKEN"] = "envtok"
        fd, path = tempfile.mkstemp()
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, "w") as f:
            f.write("filetok\n")
        h = km.build_headers(self._HArgs(token_file=path, auth_header="X-Kijito-Token"))
        self.assertEqual(h["X-Kijito-Token"], "filetok")
        self.assertNotIn("Authorization", h)
        self.assertEqual(h["User-Agent"], km.USER_AGENT)

    def test_persona_url_targets_remote_inbox_as_peek(self):
        url = km.persona_url("argus")
        self.assertTrue(url.startswith("https://api.kijito.ai/api/inbox?"))
        self.assertIn("persona=argus", url)
        self.assertIn("mark_read=false", url)

    def test_no_localhost_anywhere_in_module(self):
        for bad in ("127.0.0.1", "localhost", ":7474"):
            self.assertNotIn(bad, km.KIJITO_BASE + km.INBOX_URL + km.PERSONAS_URL + km.NOTIFY_PENDING_URL)


class LongPollTest(unittest.TestCase):
    def test_parses_counts_and_cursor_and_request_shape(self):
        op = FakeOpener(FakeResponse(200, {"result": [{"persona": "argus", "unread": 2}], "cursor": "c1"}))
        available, counts, cursor = km.fetch_unread_counts_longpoll(op, {}, 50, None)
        self.assertTrue(available)
        self.assertEqual(counts, {"argus": 2})
        self.assertEqual(cursor, "c1")
        url, timeout = op.calls[0]
        self.assertIn("wait=50", url)
        self.assertNotIn("cursor=", url)            # no cursor echoed on the first call
        self.assertEqual(timeout, 50 + km.LONGPOLL_SLACK)  # client timeout sits above the server hold

    def test_echoes_cursor_on_subsequent_call(self):
        op = FakeOpener(FakeResponse(200, {"result": [], "cursor": "c2"}))
        km.fetch_unread_counts_longpoll(op, {}, 30, "prev")
        url, _ = op.calls[0]
        self.assertIn("cursor=prev", url)

    def test_missing_cursor_means_server_not_longpolling(self):
        # forward/back-compat: a server that ignores ?wait returns no cursor → caller interval-polls
        op = FakeOpener(FakeResponse(200, {"result": [{"persona": "argus", "unread": 1}]}))
        available, counts, cursor = km.fetch_unread_counts_longpoll(op, {}, 50, None)
        self.assertTrue(available)
        self.assertEqual(counts, {"argus": 1})
        self.assertIsNone(cursor)

    def test_connection_error_keeps_old_cursor_for_lossless_resume(self):
        op = FakeOpener(exc=urllib.error.URLError("dropped"))
        available, counts, cursor = km.fetch_unread_counts_longpoll(op, {}, 50, "keepme")
        self.assertFalse(available)
        self.assertEqual(counts, {})
        self.assertEqual(cursor, "keepme")

    def test_non_2xx_keeps_old_cursor(self):
        op = FakeOpener(FakeResponse(503, {"result": []}))
        available, _, cursor = km.fetch_unread_counts_longpoll(op, {}, 50, "x")
        self.assertFalse(available)
        self.assertEqual(cursor, "x")

    def test_parse_unread_rows_rejects_bad_shape(self):
        self.assertIsNone(km._parse_unread_rows({"result": {}}))
        self.assertEqual(km._parse_unread_rows({"result": [{"persona": "a", "unread": 4}]}), {"a": 4})
        self.assertEqual(km._parse_unread_rows({"result": [{"persona": "a", "unread": "bad"}]}), {"a": 0})


class DiscoverFromCountsTest(unittest.TestCase):
    class FakeTarget:
        def __init__(self, persona):
            self.persona = persona

        def lifecycle(self, *a, **k):
            pass

    def setUp(self):
        self._orig = km.build_persona_target

    def tearDown(self):
        km.build_persona_target = self._orig

    def _patch_builder(self, made):
        km.build_persona_target = (
            lambda persona, obo, headers, args, emitter: made.append(persona) or self.FakeTarget(persona))

    def test_adds_unwatched_mail_bearing_personas_in_all_mode(self):
        made = []
        self._patch_builder(made)
        targets = [self.FakeTarget("argus")]
        added = km.discover_from_counts(Args(), {"argus": 0, "river": 3, "ladybug": 1}, targets, {}, {}, None)
        self.assertEqual(set(added), {"river", "ladybug"})
        self.assertEqual({t.persona for t in targets}, {"argus", "river", "ladybug"})

    def test_noop_for_explicit_persona_subset(self):
        made = []
        self._patch_builder(made)
        targets = [self.FakeTarget("argus")]
        added = km.discover_from_counts(Args(persona=["argus"]), {"river": 3}, targets, {}, {}, None)
        self.assertEqual(added, [])
        self.assertEqual(made, [])
        self.assertEqual({t.persona for t in targets}, {"argus"})

    def test_case_variant_in_counts_is_not_readopted(self):
        # THE REGRESSION TEST. The inbox namespace held both 'claude-chat' and 'Claude-chat'; adopting
        # the variant tried to lock a state file the producer itself already held (same inode on a
        # case-insensitive FS) and warned on EVERY tick - 20,079 lines in one 3-day run.
        made = []
        self._patch_builder(made)
        targets = [self.FakeTarget("claude-chat")]
        added = km.discover_from_counts(Args(), {"claude-chat": 9, "Claude-chat": 1}, targets, {}, {}, None)
        self.assertEqual(added, [])
        self.assertEqual(made, [])
        self.assertEqual({t.persona for t in targets}, {"claude-chat"})

    def test_a_genuinely_new_uppercase_persona_is_still_adopted_with_its_case_preserved(self):
        # Case-insensitive MATCHING must not become case-mangling: the name still goes to the API as sent.
        made = []
        self._patch_builder(made)
        targets = [self.FakeTarget("argus")]
        added = km.discover_from_counts(Args(), {"Vellum": 2}, targets, {}, {}, None)
        self.assertEqual(added, ["Vellum"])
        self.assertEqual(made, ["Vellum"])


class DeclaredOmissionsTest(unittest.TestCase):
    """The server declares an incomplete window; the parser must not throw the declaration away."""

    def test_size_dropped_is_read(self):
        self.assertEqual(km._declared_omissions({"result": [], "size_dropped": 15}), (15, True))

    def test_boolean_truncation_without_a_count_still_counts_as_incomplete(self):
        # "incomplete but unquantified" must never round down to "nothing missing".
        self.assertEqual(km._declared_omissions({"result": [], "truncated": True}), (1, False))
        self.assertEqual(km._declared_omissions({"result": [], "size_truncated": True}), (1, False))

    def test_a_lone_oversized_message_is_NOT_an_omission(self):
        # size_truncated with size_dropped=0 means one message's BODY was clipped, not that a row was
        # withheld. Counting it invents a gap and alarms about mail that was never missing.
        self.assertEqual(km._declared_omissions(
            {"result": [{"id": 1}], "size_truncated": True, "size_dropped": 0}), (0, True))

    def test_count_truncation_with_zero_size_drops_is_still_an_omission(self):
        # Measured live: limit=3 returns truncated=True, size_dropped=0, and rows ARE missing.
        # These are different mechanisms and must not cancel each other out.
        self.assertEqual(km._declared_omissions(
            {"result": [], "truncated": True, "size_truncated": False, "size_dropped": 0}), (1, False))

    def test_count_and_size_truncation_accumulate(self):
        self.assertEqual(km._declared_omissions({"result": [], "truncated": True, "size_dropped": 4}), (5, False))

    def test_a_complete_window_declares_nothing(self):
        self.assertEqual(km._declared_omissions({"result": [], "truncated": False,
                                                 "size_truncated": False, "size_dropped": 0}), (0, True))

    def test_fetch_carries_the_declaration_onto_the_poll(self):
        body = json.dumps({"result": [{"id": 5}], "size_truncated": True, "size_dropped": 3}).encode()
        poll = km.fetch(FakeOpener(FakeResponse(200, json.loads(body))), "http://x/api/inbox", {})
        self.assertTrue(poll.ok)
        self.assertEqual(poll.omitted, 3)


class BoundedWindowGapTest(unittest.TestCase):
    """A bounded window must never let the cursor silently cross mail the server admits it dropped."""

    def _target(self, cursor, armed=True):
        t = km.WatchTarget.__new__(km.WatchTarget)
        t.cursor, t.armed, t.persona, t.url = cursor, armed, "argus", "http://x/api/inbox?persona=argus"
        t.opener, t.headers = None, {}
        t.emitted_above, t.gap_alerted = set(), None
        t.pin_evidence_intact, t.pin_forced = True, False
        t.state_corrupt = False
        t.pin_release_at = None
        t.delivery_blocked = False
        t.state_not_durable = False
        return t

    def test_window_reaching_back_past_the_cursor_is_safe(self):
        # Steady state: long-poll keeps the backlog tiny, so the window covers the cursor. Dropped
        # messages are all BELOW it and were already emitted. Must NOT fire - or it alarms forever.
        t = self._target(cursor=1135)
        poll = km.Poll(True, items=[{"id": 1101}, {"id": 1135}], omitted=15)
        self.assertIsNone(t._uncovered_gap(poll, poll.items))

    def test_window_starting_above_the_cursor_with_omissions_is_the_loss_case(self):
        t = self._target(cursor=1100)
        poll = km.Poll(True, items=[{"id": 1200}, {"id": 1260}], omitted=10)
        self.assertEqual(t._uncovered_gap(poll, poll.items), (1100, 1200, 10, True))

    def test_no_declared_omissions_means_no_gap_however_high_the_window_starts(self):
        # ids are account-global, so a window starting above the cursor is NORMAL when the server
        # says it dropped nothing - other personas' mail occupies the intervening ids.
        t = self._target(cursor=1100)
        poll = km.Poll(True, items=[{"id": 1200}], omitted=0)
        self.assertIsNone(t._uncovered_gap(poll, poll.items))

    def test_unarmed_target_never_reports_a_gap(self):
        t = self._target(cursor=1100, armed=False)
        poll = km.Poll(True, items=[{"id": 1200}], omitted=5)
        self.assertIsNone(t._uncovered_gap(poll, poll.items))


class WalkBackTest(unittest.TestCase):
    """The authoritative backward read. Coverage is proven by EXHAUSTION, never by counting rows."""

    def _target(self, cursor=100):
        t = km.WatchTarget.__new__(km.WatchTarget)
        t.cursor, t.armed, t.persona, t.url = cursor, True, "argus", "http://x/api/inbox?persona=argus"
        t.opener, t.headers = None, {}
        t.emitted_above, t.gap_alerted = set(), None
        t.pin_evidence_intact, t.pin_forced = True, False
        t.state_corrupt = False
        t.pin_release_at = None
        t.delivery_blocked = False
        t.state_not_durable = False
        return t

    def _pages(self, mapping, calls=None):
        """mapping: before_id -> (items, next_before_id). Pages are built SERVER-SHAPED (see server_page):
        a page that hands back a continuation also declares that it withheld rows, because that is the
        only pairing the API can produce."""
        def f(opener, url, headers):
            bid = int(url.split("before_id=")[1].split("&")[0])
            if calls is not None:
                calls.append(bid)
            items, nb = mapping.get(bid, ([], None))
            return server_page(items, omitted=(1 if nb is not None else 0), exact=False, next_before_id=nb)
        return f

    def test_walk_chains_on_next_before_id_until_it_reaches_the_watermark(self):
        calls = []
        t = self._target(cursor=100)
        orig, km.fetch = km.fetch, self._pages({
            200: ([{"id": 180}, {"id": 190}], 180),
            180: ([{"id": 120}, {"id": 150}], 120),
            120: ([{"id": 90}, {"id": 100}], 90),      # reaches the watermark
        }, calls)
        try:
            rows, covered = t._walk_back(200, 100)
        finally:
            km.fetch = orig
        self.assertTrue(covered)
        self.assertEqual(calls, [200, 180, 120])
        self.assertEqual(sorted(m["id"] for m in rows), [90, 100, 120, 150, 180, 190])

    def test_walk_is_covered_when_no_older_mail_exists(self):
        t = self._target(cursor=0)
        orig, km.fetch = km.fetch, self._pages({200: ([{"id": 150}], None)})
        try:
            rows, covered = t._walk_back(200, 0)
        finally:
            km.fetch = orig
        self.assertTrue(covered)                    # next_before_id null = end of the chain

    def test_a_failed_page_is_NOT_coverage(self):
        t = self._target(cursor=100)
        orig, km.fetch = km.fetch, lambda o, u, h: km.Poll(False, reason="http 502")
        try:
            rows, covered = t._walk_back(200, 100)
        finally:
            km.fetch = orig
        self.assertFalse(covered)
        self.assertEqual(rows, [])

    def test_walk_refuses_to_spin_on_a_non_advancing_cursor(self):
        # A server that echoes the same next_before_id would otherwise loop until the page budget.
        # ⚠️ THE FIXTURE HAS TO REACH THIS GUARD, and the previous one did not: it paired oldest=199 with
        # a continuation of 200, so the OLDEST-ROW check rejected it first and this test passed without
        # ever running the anti-spin branch it is named for. The continuation must EQUAL the oldest row
        # (so the earlier checks pass) and still fail to advance - i.e. a page that returns the very row
        # you asked to page back from. Verified to discriminate by mutation: deleting the anti-spin
        # guard now hangs this walk out to the page budget instead of stopping at one call.
        calls = []
        t = self._target(cursor=0)
        orig, km.fetch = km.fetch, self._pages({200: ([{"id": 200}], 200)}, calls)
        try:
            rows, covered = t._walk_back(200, 0)
        finally:
            km.fetch = orig
        self.assertFalse(covered)
        self.assertEqual(len(calls), 1)

    def test_walk_is_bounded_by_a_page_budget_and_a_short_walk_is_not_coverage(self):
        calls = []
        t = self._target(cursor=0)
        # every page hands back a strictly lower cursor, so only the budget stops it
        def f(opener, url, headers):
            bid = int(url.split("before_id=")[1].split("&")[0])
            calls.append(bid)
            return server_page([{"id": bid - 1}], omitted=1, exact=False)
        orig, km.fetch = km.fetch, f
        try:
            rows, covered = t._walk_back(100000, 0)
        finally:
            km.fetch = orig
        self.assertFalse(covered)                   # budget exhausted proves nothing
        self.assertEqual(len(calls), km.WALK_BACK_MAX_PAGES)

    def test_walk_omits_nothing_and_sends_before_id_explicitly(self):
        seen = []
        t = self._target(cursor=100)
        orig, km.fetch = km.fetch, self._pages({200: ([{"id": 100}], None)}, seen)
        try:
            t._walk_back(200, 100)
        finally:
            km.fetch = orig
        self.assertEqual(seen, [200])               # starts AT the window floor


class BoundedWindowEndToEndTest(unittest.TestCase):
    """poll_once end-to-end over the authoritative backward walk."""

    class RecordingEmitter:
        """A stand-in for Emitter that ACKNOWLEDGES by default and can be told to refuse.

        `new()` returns True/False exactly as the real emitter does, because the watcher now treats the
        return value as the delivery acknowledgement (Loom re-audit 7, HIGH 1). A double that returned
        None would read as "not delivered" and pin - which is the SAFE direction for a protocol slip,
        and is why the watcher tests `is True` rather than truthiness.
        """
        def __init__(self, fail_ids=(), sync_ok=True):
            self.new_ids, self.events = [], []
            self.fail_ids = set(fail_ids)   # ids whose delivery FAILS (e.g. an --exec exiting non-zero)
            self.sync_ok = sync_ok          # False = the durability barrier cannot be met
            self.syncs = 0
            self.synced_personas = []

        def new(self, m):
            if m["id"] in self.fail_ids:
                return False
            self.new_ids.append(m["id"])
            return True

        def lifecycle(self, event, **f):
            self.events.append((event, f))
            return True

        def sync(self, persona=None):
            self.syncs += 1
            self.synced_personas.append(persona)
            return self.sync_ok

    class FullArgs:
        persona = personas = None
        all_personas = False
        alert_after = 3
        poll_seconds = 60
        heartbeat = 0
        max_replay = 50
        no_fast_path = True
        resync_every = 10
        self_test = False

    def _target(self, cursor, emitter):
        t = km.WatchTarget.__new__(km.WatchTarget)
        t.persona, t.url, t.headers = "argus", "http://x/api/inbox?persona=argus", {}
        t.opener, t.emitter, t.args = None, emitter, self.FullArgs()
        t.cursor, t.armed, t.fsm_state, t.failures = cursor, True, "UP", 0
        t.state_file = t.last_unread = None
        t.fast_path = False
        t.skips = t.first_poll = 0
        t.last_heartbeat = km._monotonic()
        t.count_url, t.unread_persona = km.NOTIFY_PENDING_URL, "argus"
        t.emitted_above, t.gap_alerted = set(), None
        t.pin_evidence_intact, t.pin_forced = True, False
        t.state_corrupt = False
        t.pin_release_at = None
        t.delivery_blocked = False
        t.state_not_durable = False
        return t

    def _fetch(self, main_items, omitted, walk=None, exact=True, walk_fail=False):
        """main window + a backward-walk map {before_id: (items, next_before_id)}.

        Every page is built SERVER-SHAPED via server_page(), so the whole end-to-end suite runs against
        bodies the API can actually produce and through the production validator - not hand-built Polls
        that skip it.
        """
        def f(opener, url, headers):
            if "before_id=" in url:
                if walk_fail:
                    return km.Poll(False, reason="http 502")
                bid = int(url.split("before_id=")[1].split("&")[0])
                items, nb = (walk or {}).get(bid, ([], None))
                return server_page(items, omitted=(1 if nb is not None else 0), exact=False,
                                   next_before_id=nb)
            return server_page(main_items, omitted=omitted, exact=exact)
        return f

    def _run(self, t, fetch_fn, times=1):
        orig, km.fetch = km.fetch, fetch_fn
        try:
            for _ in range(times):
                t.poll_once()
        finally:
            km.fetch = orig

    def test_a_completed_walk_recovers_hidden_mail_advances_and_stays_quiet(self):
        em = self.RecordingEmitter()
        t = self._target(cursor=1100, emitter=em)
        # window [1200,1260] hides 1105 and 1150; the walk reaches back past the watermark.
        self._run(t, self._fetch([{"id": 1200}, {"id": 1260}], 2,
                                 walk={1200: ([{"id": 1105}, {"id": 1150}, {"id": 1100}], None)}))
        self.assertEqual(em.new_ids, [1105, 1150, 1200, 1260])
        self.assertEqual([f for e, f in em.events if e == "alert"], [])
        self.assertEqual(t.cursor, 1260)
        self.assertEqual(t.emitted_above, set())

    def test_LOOM4_an_inexact_count_IS_closable_once_the_span_can_be_walked(self):
        # This is what the backward cursor bought: coverage by exhaustion, so an unquantified
        # truncation is no longer permanently unclosable.
        em = self.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)
        self._run(t, self._fetch([{"id": 200}], 1, exact=False,
                                 walk={200: ([{"id": 150}, {"id": 100}], None)}))
        self.assertEqual(em.new_ids, [150, 200])
        self.assertEqual(t.cursor, 200)
        self.assertEqual([f for e, f in em.events if e == "alert"], [])

    def test_LOOM_REPRO_a_walk_that_cannot_complete_pins_the_cursor(self):
        # Loom repro, now against the authoritative path: if the walk cannot be performed at all, the
        # span is unproven and the watermark must hold.
        em = self.RecordingEmitter()
        t = self._target(cursor=1100, emitter=em)
        self._run(t, self._fetch([{"id": 1200}], 4, walk_fail=True))
        self.assertEqual(em.new_ids, [1200])
        self.assertEqual(t.cursor, 1100)
        self.assertEqual(t.emitted_above, {1200})
        alert = [f for e, f in em.events if e == "alert"][0]
        self.assertTrue(alert["pinned"])

    def test_a_walk_stopped_by_the_page_budget_pins(self):
        em = self.RecordingEmitter()
        t = self._target(cursor=0, emitter=em)
        def f(opener, url, headers):
            if "before_id=" in url:
                bid = int(url.split("before_id=")[1].split("&")[0])
                return server_page([{"id": bid - 1}], omitted=1, exact=False)
            return server_page([{"id": 100000}], omitted=1)
        self._run(t, f)
        self.assertEqual(t.cursor, 0)               # never reached the watermark
        self.assertTrue([f for e, f in em.events if e == "alert"])

    def test_LOOM3_concurrent_arrivals_are_delivered_but_are_not_coverage(self):
        # Mail arriving mid-walk is emitted, but coverage still comes only from the walk terminating.
        em = self.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)
        self._run(t, self._fetch([{"id": 200}], 1, walk_fail=True))
        self.assertEqual(t.cursor, 100)
        self.assertEqual(em.new_ids, [200])

    def test_a_pinned_watermark_does_not_re_emit_on_the_next_poll(self):
        em = self.RecordingEmitter()
        t = self._target(cursor=1100, emitter=em)
        self._run(t, self._fetch([{"id": 1200}], 4, walk_fail=True), times=3)
        self.assertEqual(em.new_ids, [1200])        # emitted ONCE across three polls
        self.assertEqual(t.cursor, 1100)

    def test_a_pinned_gap_alerts_once_not_once_per_poll(self):
        em = self.RecordingEmitter()
        t = self._target(cursor=1100, emitter=em)
        self._run(t, self._fetch([{"id": 1200}], 4, walk_fail=True), times=5)
        self.assertEqual(len([e for e, f in em.events if e == "alert"]), 1)

    def test_gap_alert_is_keyed_on_the_stable_watermark_not_the_drifting_floor(self):
        em = self.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)
        floors = iter([200, 250, 300])
        def f(opener, url, headers):
            if "before_id=" in url:
                return km.Poll(False, reason="http 502")
            return server_page([{"id": next(floors)}], omitted=2)
        self._run(t, f, times=3)
        self.assertEqual(len([f for e, f in em.events if e == "alert"]), 1)
        self.assertEqual(t.gap_alerted, 100)

    def test_watermark_resumes_advancing_once_windows_are_complete_again(self):
        em = self.RecordingEmitter()
        t = self._target(cursor=1100, emitter=em)
        self._run(t, self._fetch([{"id": 1200}], 4, walk_fail=True))
        self.assertEqual(t.cursor, 1100)
        self._run(t, self._fetch([{"id": 1200}, {"id": 1300}], 0))
        self.assertEqual(t.cursor, 1300)
        self.assertEqual(t.emitted_above, set())
        self.assertEqual(em.new_ids, [1200, 1300])

    def test_steady_state_truncation_is_silent_and_changes_nothing(self):
        em = self.RecordingEmitter()
        t = self._target(cursor=1135, emitter=em)
        self._run(t, self._fetch([{"id": 1101}, {"id": 1140}], 15))
        self.assertEqual(em.new_ids, [1140])
        self.assertEqual([e for e, f in em.events if e == "alert"], [])
        self.assertEqual(t.cursor, 1140)

    def test_LOOM3_restart_does_not_re_emit_what_a_pin_already_delivered(self):
        em = self.RecordingEmitter()
        t = self._target(cursor=1100, emitter=em)
        t.armed = False
        t.emitted_above = {1200}
        self._run(t, self._fetch([{"id": 1200}], 0))
        self.assertEqual(em.new_ids, [])

    def test_LOOM3_restart_replay_cap_does_not_erase_a_pin(self):
        em = self.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)
        t.armed = False
        t.emitted_above = {200, 201}
        t.args.max_replay = 1
        self._run(t, self._fetch(
            [{"id": 200}, {"id": 201}, {"id": 202}, {"id": 203}, {"id": 204}], 5, walk_fail=True))
        self.assertEqual(t.cursor, 100)
        self.assertNotIn("replay_capped", [e for e, f in em.events])

    def test_replay_cap_still_applies_when_nothing_is_pinned(self):
        em = self.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)
        t.armed = False
        t.args.max_replay = 1
        self._run(t, self._fetch([{"id": 200}, {"id": 201}, {"id": 202}], 0))
        self.assertIn("replay_capped", [e for e, f in em.events])
        self.assertEqual(t.cursor, 202)

    def test_pin_tracking_is_bounded_and_evidence_loss_is_a_DURABLE_alert(self):
        em = self.RecordingEmitter()
        t = self._target(cursor=0, emitter=em)
        t.emitted_above = set(range(1, km.PIN_TRACKING_CAP + 50))
        self._run(t, self._fetch([{"id": km.PIN_TRACKING_CAP + 100}], 1, walk_fail=True))
        self.assertLessEqual(len(t.emitted_above), km.PIN_TRACKING_CAP)
        loss = [f for e, f in em.events if e == "alert" and f.get("evidence_lost")]
        self.assertEqual(len(loss), 1)
        self.assertGreater(loss[0]["forgot"], 0)
        self.assertFalse(t.pin_evidence_intact)

    def test_an_authoritative_walk_restores_lost_evidence(self):
        # Overflow makes the span unclosable by counting - but a completed walk re-establishes ground
        # truth directly, so it may close even then.
        em = self.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)
        t.pin_evidence_intact = False
        self._run(t, self._fetch([{"id": 200}], 1, walk={200: ([{"id": 150}, {"id": 100}], None)}))
        self.assertEqual(t.cursor, 200)
        self.assertTrue(t.pin_evidence_intact)

    def test_LOOM4_a_restored_pin_clears_when_a_complete_window_reaches_back(self):
        em = self.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)
        t.emitted_above = {200}
        self._run(t, self._fetch([{"id": 100}, {"id": 200}], 0))
        self.assertEqual(em.new_ids, [])
        self.assertEqual(t.cursor, 200)
        self.assertEqual(t.emitted_above, set())
        self.assertIsNone(t.gap_alerted)


class CorruptPinStateTest(unittest.TestCase):
    """Loom re-audit 4, HIGH 2. Corrupt pin state must fail CLOSED, never silently unpin."""

    def _write(self, payload):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "hive.json")
        with open(p, "w") as f:
            json.dump(payload, f)
        return p

    def test_malformed_emitted_above_keeps_the_pin_and_marks_evidence_unusable(self):
        # Loading it as an empty set silently UNPINS: the watcher then thinks nothing is outstanding and
        # the replay cap can jump the cursor over the very span the pin was protecting.
        p = self._write({"identity": "idx", "cursor": 100, "state": "UP",
                         "consecutive_failures": 0, "gap_alerted": 100,
                         "emitted_above": ["not", "ints"]})
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            loaded = km.StateFile(p, "idx").load()
        finally:
            km.sys.stderr = err
        cursor, emitted, intact = loaded["cursor"], loaded["emitted_above"], loaded["pin_evidence_intact"]
        self.assertEqual(cursor, 100)
        self.assertEqual(emitted, set())
        self.assertFalse(intact)                    # <- the pin is HELD, evidence marked unusable
        self.assertIn("malformed", buf.getvalue())

    def test_a_recorded_gap_alert_without_tracking_is_treated_as_inconsistent(self):
        p = self._write({"identity": "idx", "cursor": 100, "state": "UP",
                         "consecutive_failures": 0, "gap_alerted": 100})
        loaded = km.StateFile(p, "idx").load()
        emitted, alerted, intact = (loaded["emitted_above"], loaded["gap_alerted"],
                                    loaded["pin_evidence_intact"])
        self.assertEqual(emitted, set())
        self.assertEqual(alerted, 100)
        self.assertFalse(intact)                    # something was pinned when this was written

    def test_a_clean_file_with_no_pin_loads_intact(self):
        p = self._write({"identity": "idx", "cursor": 100, "state": "UP", "consecutive_failures": 0})
        loaded = km.StateFile(p, "idx").load()
        emitted, alerted, intact = (loaded["emitted_above"], loaded["gap_alerted"],
                                    loaded["pin_evidence_intact"])
        self.assertEqual((emitted, alerted, intact), (set(), None, True))

    def test_a_forced_pin_survives_arming_so_replay_cap_cannot_cross_it(self):
        # Loom's repro: corrupt emitted_above + gap_alerted 100 + max_replay 1 => cursor jumped to 201.
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = km.WatchTarget.__new__(km.WatchTarget)
        t.persona, t.url, t.headers, t.opener = "argus", "http://x/api/inbox?persona=argus", {}, None
        t.emitter, t.args = em, BoundedWindowEndToEndTest.FullArgs()
        t.args.max_replay = 1
        t.cursor, t.armed, t.fsm_state, t.failures = 100, False, "UP", 0
        t.state_file = t.last_unread = None
        t.fast_path = False
        t.skips = t.first_poll = 0
        t.last_heartbeat = km._monotonic()
        t.count_url, t.unread_persona = km.NOTIFY_PENDING_URL, "argus"
        t.emitted_above, t.gap_alerted = set(), 100
        t.pin_evidence_intact, t.pin_forced = False, True    # what a corrupt load produces
        t.state_corrupt = False
        t.pin_release_at = None
        t.delivery_blocked = False
        t.state_not_durable = False

        # The walk must NOT succeed here, or it would legitimately close the span and release the pin -
        # which is a DIFFERENT property, tested separately below. Isolating them keeps this test about
        # the one thing its name claims: arming must not let the replay cap cross a forced pin.
        def f(opener, url, headers):
            if "before_id=" in url:
                return km.Poll(False, reason="http 502")
            return server_page([{"id": 200}, {"id": 201}], omitted=3)
        orig, km.fetch = km.fetch, f
        try:
            t.poll_once()
        finally:
            km.fetch = orig
        self.assertEqual(t.cursor, 100)             # *** did NOT jump to 201 ***
        self.assertEqual([e for e, _ in em.events if e == "replay_capped"], [])
        self.assertTrue(t.pin_forced)               # an unresolved span keeps the pin forced

    def test_a_completed_authoritative_walk_RELEASES_a_forced_pin(self):
        # Loom re-audit 5, MEDIUM. A forced pin is held because TRACKING was lost; a completed walk is the
        # authoritative evidence that replaces it. Never clearing it froze the watermark forever, and since
        # a non-pinned poll records no emitted ids, every later window re-emitted the same mail.
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = BoundedWindowEndToEndTest()._target(cursor=100, emitter=em)
        t.pin_evidence_intact, t.pin_forced = False, True

        def f(opener, url, headers):
            if "before_id=" in url:
                # walks back past the watermark: authoritative exhaustion
                return server_page([{"id": 150}, {"id": 100}])   # walked back past the watermark
            return server_page([{"id": 200}], omitted=1)
        orig, km.fetch = km.fetch, f
        try:
            t.poll_once()
        finally:
            km.fetch = orig
        self.assertFalse(t.pin_forced)              # released by authoritative evidence
        self.assertTrue(t.pin_evidence_intact)
        self.assertEqual(t.cursor, 200)             # and the watermark can move again

    def test_a_forced_pin_with_no_gap_is_released_by_a_COMPLETE_window(self):
        # The other authoritative proof, and without it a forced pin that never meets another gap could
        # never clear at all - the watermark would stay frozen for the life of the process.
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = BoundedWindowEndToEndTest()._target(cursor=100, emitter=em)
        t.pin_evidence_intact, t.pin_forced = False, True
        # A COMPLETE window terminates: withheld nothing, so there is nothing older to point at.
        orig, km.fetch = km.fetch, lambda o, u, h: server_page(
            [{"id": 90}, {"id": 100}, {"id": 120}], omitted=0)
        try:
            t.poll_once()
        finally:
            km.fetch = orig
        self.assertFalse(t.pin_forced)
        self.assertEqual(t.cursor, 120)

    def test_a_forced_pin_is_NOT_released_by_an_incomplete_window(self):
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = BoundedWindowEndToEndTest()._target(cursor=100, emitter=em)
        t.pin_evidence_intact, t.pin_forced = False, True
        orig, km.fetch = km.fetch, lambda o, u, h: server_page(
            [{"id": 90}, {"id": 120}], omitted=2)
        try:
            t.poll_once()
        finally:
            km.fetch = orig
        self.assertTrue(t.pin_forced)               # the server still admits it withheld rows
        self.assertEqual(t.cursor, 100)


class CaseAsymmetryInvariantTest(unittest.TestCase):
    """THE TWO CASE RULES POINT OPPOSITE WAYS ON PURPOSE. Do not 'harmonise' them.

    Reading the source, the path layer casefolds and the identity layer does not, which looks like an
    inconsistency and invites a tidy-up. It is not. They answer different questions about different
    systems:

      PATH  (_state_safe_persona)  MUST casefold  - the local filesystem is case-INSENSITIVE (APFS,
            NTFS), so 'Claude-chat' and 'claude-chat' are the SAME FILE. Not casefolding made the
            watcher block on a flock it already held, so the variant got no event stream and its mail
            woke nobody.
      IDENTITY (stranded_inboxes) MUST NOT casefold - the SERVER's inbox namespace is case-SENSITIVE.
            The 'Claude-chat' inbox held a genuinely different message set from 'claude-chat'.
            Casefolding here silently merges two distinct inboxes and hides stranded mail - it was
            shipped that way for an hour and stopped detecting the very incident it existed for.

    A comment cannot defend this; the next person to make the code look consistent would delete it. This
    test is the defence. If it fails, someone has made the two layers agree - and making them agree IS
    the bug.
    """

    def setUp(self):
        self._orig = dict(km._PERSONA_MEMORY_COUNTS)
        km._PERSONA_MEMORY_COUNTS.clear()

    def tearDown(self):
        km._PERSONA_MEMORY_COUNTS.clear()
        km._PERSONA_MEMORY_COUNTS.update(self._orig)

    def test_path_layer_COLLAPSES_case_variants(self):
        self.assertEqual(km._state_path_for_persona("/tmp/hive.json", "Claude-chat"),
                         km._state_path_for_persona("/tmp/hive.json", "claude-chat"))

    def test_identity_layer_KEEPS_case_variants_distinct(self):
        # 'claude-chat' is a known directory persona owning memories; 'Claude-chat' is a separate inbox
        # holding mail. The variant MUST be flagged, or the 14-day stranding goes undetected again.
        km._PERSONA_MEMORY_COUNTS.update({"claude-chat": 35})
        self.assertEqual(
            km.stranded_inboxes(["claude-chat"], {"claude-chat": 9, "Claude-chat": 1}),
            ["Claude-chat"])

    def test_the_two_layers_disagree_and_that_is_the_invariant(self):
        # Stated as one assertion so the intent survives even if the tests above are edited apart.
        same_path = (km._state_path_for_persona("/tmp/hive.json", "Claude-chat")
                     == km._state_path_for_persona("/tmp/hive.json", "claude-chat"))
        km._PERSONA_MEMORY_COUNTS.update({"claude-chat": 35})
        distinct_identity = km.stranded_inboxes(["claude-chat"], {"Claude-chat": 1}) == ["Claude-chat"]
        self.assertTrue(same_path and distinct_identity,
                        "path layer must collapse case variants AND identity layer must keep them "
                        "distinct; if you just made these agree, you have reintroduced a real defect")


class OwnershipSignalTest(unittest.TestCase):
    """Directory membership stopped being sufficient once the directory listed every recipient."""

    def setUp(self):
        self._orig = dict(km._PERSONA_MEMORY_COUNTS)
        km._PERSONA_MEMORY_COUNTS.clear()

    def tearDown(self):
        km._PERSONA_MEMORY_COUNTS.clear()
        km._PERSONA_MEMORY_COUNTS.update(self._orig)

    def test_memory_count_is_read_from_the_top_level_field(self):
        self.assertEqual(km._row_memory_count({"persona": "x", "memory_count": 148}), 148)
        self.assertEqual(km._row_memory_count({"persona": "x", "memory_count": 0}), 0)

    def test_absent_memory_count_is_None_not_zero(self):
        # None means "server didn't say" and must never be read as "owns nothing".
        self.assertIsNone(km._row_memory_count({"persona": "x"}))
        self.assertIsNone(km._row_memory_count({"persona": "x", "memory_count": "many"}))

    def test_project_counts_are_NOT_summed_as_a_fallback(self):
        # Live: maestro sums to 0 across projects but owns 61 memories, because global-scoped memories
        # carry no project. Summing that field would flag half the fleet as unowned.
        row = {"persona": "maestro", "projects": [], "memory_count": 61}
        self.assertEqual(km._row_memory_count(row), 61)

    def test_a_registered_recipient_owning_no_memories_is_stranded(self):
        # The post-unification case: it IS in the directory, so absence can never catch it.
        km._PERSONA_MEMORY_COUNTS.update({"all": 0, "argus": 94})
        self.assertEqual(km.stranded_inboxes(["all", "argus"], {"all": 2, "argus": 1}), ["all"])

    def test_a_persona_that_owns_memories_is_never_stranded(self):
        km._PERSONA_MEMORY_COUNTS.update({"vellum": 129})
        self.assertEqual(km.stranded_inboxes(["vellum"], {"vellum": 6}), [])

    def test_unknown_memory_count_degrades_to_directory_membership_only(self):
        # Older server with no memory_count: the second signal stays quiet rather than guessing.
        km._PERSONA_MEMORY_COUNTS.update({"someone": None})
        self.assertEqual(km.stranded_inboxes(["someone"], {"someone": 3}), [])

    def test_absence_from_the_directory_still_wins_on_its_own(self):
        km._PERSONA_MEMORY_COUNTS.update({"argus": 94})
        self.assertEqual(km.stranded_inboxes(["argus"], {"argus": 1, "ghost": 1}), ["ghost"])

    def test_ownership_case_is_diagnosed_distinctly_from_a_case_variant(self):
        km._PERSONA_MEMORY_COUNTS.update({"all": 0})
        d = km._stranded_detail("all", ["all", "argus"], {"all": 2})
        self.assertIn("owns no memories", d)
        self.assertNotIn("case-variant", d)


class StrandedInboxTest(unittest.TestCase):
    class FakeTarget:
        def __init__(self, persona):
            self.persona = persona

    class FakeEmitter:
        def __init__(self):
            self.events = []

        def lifecycle(self, event, **fields):
            self.events.append(dict(event=event, **fields))

    def setUp(self):
        km._REPORTED_STRANDED.clear()
        # _PERSONA_MEMORY_COUNTS is module-global; without save/restore a test that seeds it leaks into
        # every later test and produces order-dependent failures.
        self._mem = dict(km._PERSONA_MEMORY_COUNTS)
        km._PERSONA_MEMORY_COUNTS.clear()

    def tearDown(self):
        km._REPORTED_STRANDED.clear()
        km._PERSONA_MEMORY_COUNTS.clear()
        km._PERSONA_MEMORY_COUNTS.update(self._mem)

    def _report(self, directory, counts, watchers=("argus", "river")):
        em = self.FakeEmitter()
        targets = [self.FakeTarget(p) for p in watchers]
        buf, orig = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            fresh = km.report_stranded_inboxes(directory, counts, targets, em)
        finally:
            km.sys.stderr = orig
        return fresh, em.events, buf.getvalue()

    def test_flags_an_inbox_with_mail_that_is_not_a_known_persona(self):
        # 'all' looks like a broadcast but has no broadcast semantics; mail to it reached nobody for 4 days.
        self.assertEqual(km.stranded_inboxes(["argus", "river"], {"argus": 2, "all": 2}), ["all"])

    def test_case_variant_IS_flagged_because_the_server_namespace_is_case_sensitive(self):
        # The incident: 'Claude-chat' held a DIFFERENT message set from 'claude-chat' - a real, distinct,
        # unwatched inbox. Casefolding this comparison would hide exactly what the check is for. (The
        # state-file mapping casefolds for the opposite reason; see _state_safe_persona.)
        self.assertEqual(km.stranded_inboxes(["claude-chat"], {"Claude-chat": 1}), ["Claude-chat"])

    def test_a_watched_persona_is_never_flagged(self):
        self.assertEqual(km.stranded_inboxes(["claude-chat", "argus"], {"claude-chat": 9, "argus": 1}), [])

    def test_case_variant_is_diagnosed_as_such_naming_its_twin(self):
        detail = km._stranded_detail("Claude-chat", ["claude-chat", "argus"], {"Claude-chat": 1})
        self.assertIn("case-variant of known persona 'claude-chat'", detail)

    def test_an_unknown_name_is_not_mislabelled_a_case_variant(self):
        detail = km._stranded_detail("all", ["claude-chat", "argus"], {"all": 2})
        self.assertNotIn("case-variant", detail)
        self.assertIn("2 unread", detail)

    def test_inbox_with_zero_unread_is_not_flagged(self):
        self.assertEqual(km.stranded_inboxes(["argus"], {"ghost": 0}), [])

    def test_unknown_directory_never_alarms(self):
        # A failed /api/personas must not make every persona look stranded.
        fresh, events, err = self._report([], {"argus": 1, "all": 2})
        self.assertEqual(fresh, [])
        self.assertEqual(events, [])
        self.assertEqual(err, "")

    def test_alert_goes_to_every_watcher_and_never_to_the_stranded_inbox(self):
        fresh, events, err = self._report(["argus", "river"], {"all": 2})
        self.assertEqual(fresh, ["all"])
        self.assertEqual({e["persona"] for e in events}, {"argus", "river"})
        self.assertNotIn("all", {e["persona"] for e in events})  # nothing tails it; an alarm there is no alarm
        self.assertIn("stranded mail", err)

    def test_alert_does_not_leak_into_a_phantom_that_became_a_watch_target(self):
        # REGRESSION, caught only against live data: a stranded inbox HAS mail, so discover_from_counts
        # gives it a watch target and a stream. Routing the alarm to every target therefore wrote it into
        # the very stream nobody consumes. Watchers must be filtered to real DIRECTORY personas.
        fresh, events, _ = self._report(["argus", "river"], {"all": 2},
                                        watchers=("argus", "river", "all"))
        self.assertEqual(fresh, ["all"])
        self.assertEqual({e["persona"] for e in events}, {"argus", "river"})

    def test_no_directory_backed_watcher_means_no_event_but_still_a_stderr_alarm(self):
        fresh, events, err = self._report(["argus"], {"all": 2}, watchers=("all",))
        self.assertEqual(fresh, ["all"])
        self.assertEqual(events, [])          # nowhere safe to deliver it
        self.assertIn("stranded mail", err)   # operator channel still gets it

    def test_alert_uses_the_alert_event_name_so_existing_consumers_surface_it(self):
        _, events, _ = self._report(["argus"], {"all": 2}, watchers=("argus",))
        self.assertEqual(events[0]["event"], "alert")
        self.assertIn("stranded-mail", events[0]["reason"])
        self.assertEqual(events[0]["stranded_inboxes"], ["all"])

    def test_LOOM3_suppression_releases_so_a_LATER_re_stranding_is_announced(self):
        # Loom re-audit 3, MEDIUM. Suppressing for the process lifetime made "reported once" mean
        # "reported once ever", silently contradicting the documented self-clearing behaviour: an inbox
        # rescued and then stranded AGAIN would never be announced until a restart.
        first, ev1, _ = self._report(["argus"], {"all": 2}, watchers=("argus",))
        self.assertEqual(first, ["all"])
        # ... the mail is consumed, so it is no longer stranded and the alarm goes quiet ...
        gone, ev2, _ = self._report(["argus"], {}, watchers=("argus",))
        self.assertEqual(gone, [])
        # ... and now it is stranded a SECOND time. This must be announced again.
        again, ev3, err3 = self._report(["argus"], {"all": 1}, watchers=("argus",))
        self.assertEqual(again, ["all"])
        self.assertTrue(ev3)
        self.assertIn("stranded mail", err3)

    def test_LOOM4_suppression_is_keyed_EXACTLY_so_case_variants_do_not_gag_each_other(self):
        # Loom re-audit 4, MEDIUM 5. The suppression set casefolded, so 'Claude-chat' and 'claude-chat'
        # shared one key - and they are DIFFERENT inboxes on a case-sensitive server. One staying
        # stranded held the other's alarm down. This is the same case-asymmetry defect in a third place.
        km._PERSONA_MEMORY_COUNTS.update({"claude-chat": 0, "Claude-chat": 0, "argus": 94})
        directory = ["claude-chat", "Claude-chat", "argus"]
        first, _, _ = self._report(directory, {"claude-chat": 1, "Claude-chat": 1}, watchers=("argus",))
        self.assertEqual(sorted(first), ["Claude-chat", "claude-chat"])
        # lowercase is rescued; the CAPITAL variant is still stranded and stays suppressed...
        gone, _, _ = self._report(directory, {"Claude-chat": 1}, watchers=("argus",))
        self.assertEqual(gone, [])
        # ...and now lowercase is stranded AGAIN. It must re-alert, even though its case-twin never cleared.
        again, ev, _ = self._report(directory, {"claude-chat": 2, "Claude-chat": 1}, watchers=("argus",))
        self.assertEqual(again, ["claude-chat"])
        self.assertTrue(ev)

    def test_suppression_holds_while_the_condition_persists(self):
        # Releasing must not become "alert every poll" - the condition still holding is not news.
        self._report(["argus"], {"all": 2}, watchers=("argus",))
        second, ev2, err2 = self._report(["argus"], {"all": 2}, watchers=("argus",))
        self.assertEqual(second, [])
        self.assertEqual(ev2, [])
        self.assertEqual(err2, "")

    def test_reported_once_per_process_not_once_per_tick(self):
        first, ev1, _ = self._report(["argus"], {"all": 2}, watchers=("argus",))
        second, ev2, err2 = self._report(["argus"], {"all": 2}, watchers=("argus",))
        self.assertEqual(first, ["all"])
        self.assertEqual(second, [])          # the 20k-line spam lesson, applied to the new alarm
        self.assertEqual(ev2, [])
        self.assertEqual(err2, "")

    def test_a_backlog_is_summarised_into_one_event_per_watcher_not_a_wake_storm(self):
        fresh, events, _ = self._report(["argus", "river"], {"all": 2, "ghost": 1, "typo": 3},
                                        watchers=("argus", "river"))
        self.assertEqual(set(fresh), {"all", "ghost", "typo"})
        self.assertEqual(len(events), 2)      # 2 watchers, not 3 inboxes x 2 watchers
        self.assertEqual(sorted(events[0]["stranded_inboxes"]), ["all", "ghost", "typo"])


class ExecEnvTest(unittest.TestCase):
    """--exec is the portable primitive, so every field the NDJSON carries must reach a shell consumer."""

    def _env_for(self, event):
        captured = {}
        orig = km.subprocess.run
        def _run(*a, **k):
            captured.update(k.get("env") or {})
            # A CompletedProcess, because the real subprocess.run returns one and emit() now READS its
            # returncode as the delivery acknowledgement. A stub returning None would make every exec
            # look like a failed delivery.
            return km.subprocess.CompletedProcess(args=a[0] if a else "", returncode=0)
        km.subprocess.run = _run
        try:
            km.Emitter("exec-per-event", "true", 220, False).emit(event)
        finally:
            km.subprocess.run = orig
        return captured

    def test_stranded_list_reaches_exec_comma_separated_not_as_a_python_repr(self):
        env = self._env_for({"event": "alert", "source": "kijito-inbox", "ts": "t", "persona": "argus",
                             "reason": "stranded-mail: ...", "stranded_inboxes": ["Claude-chat", "all"]})
        self.assertEqual(env["KIJITOMON_STRANDED"], "Claude-chat,all")
        self.assertNotIn("[", env["KIJITOMON_STRANDED"])   # "['Claude-chat', 'all']" is unusable in $VAR

    def test_absent_fields_are_simply_omitted_not_defaulted_or_fatal(self):
        env = self._env_for({"event": "alert", "source": "kijito-inbox", "ts": "t", "persona": "argus",
                             "reason": "stranded-mail: ...", "stranded_inboxes": ["all"]})
        self.assertNotIn("KIJITOMON_FAILURES", env)   # a stranded alert is not a reachability failure
        self.assertNotIn("KIJITOMON_ID", env)         # and carries no message id

    def test_scalar_fields_are_unaffected_by_the_list_handling(self):
        env = self._env_for({"event": "new", "source": "kijito-inbox", "ts": "t",
                             "id": 41, "from": "river", "persona": "argus"})
        self.assertEqual(env["KIJITOMON_ID"], "41")
        self.assertEqual(env["KIJITOMON_FROM"], "river")


class WarnOncePerPersonaTest(unittest.TestCase):
    def setUp(self):
        self._orig = set(km._WARNED_PERSONAS)
        km._WARNED_PERSONAS.clear()

    def tearDown(self):
        km._WARNED_PERSONAS.clear()
        km._WARNED_PERSONAS.update(self._orig)

    def _capture(self, fn):
        import io
        buf, orig = io.StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            fn()
        finally:
            km.sys.stderr = orig
        return buf.getvalue()

    def test_repeat_warnings_for_one_persona_are_suppressed_after_the_first(self):
        out = self._capture(lambda: [km._warn_persona_once("ghost", "cannot add persona 'ghost': locked")
                                     for _ in range(50)])
        self.assertEqual(out.count("cannot add persona"), 1)
        self.assertIn("further warnings", out)

    def test_suppression_is_case_insensitive_and_per_persona(self):
        def emit():
            km._warn_persona_once("Ghost", "boom")
            km._warn_persona_once("ghost", "boom")   # same persona, different case -> suppressed
            km._warn_persona_once("other", "boom")   # distinct persona -> still warns
        out = self._capture(emit)
        self.assertEqual(out.count("boom"), 2)


class WaitValidationTest(unittest.TestCase):
    def _args(self, argv):
        return km.build_parser().parse_args(argv)

    def test_negative_wait_rejected(self):
        with self.assertRaises(km.FatalConfig):
            km.validate_args(self._args(["--persona", "argus", "--wait", "-1"]))

    def test_wait_zero_allowed(self):
        km.validate_args(self._args(["--persona", "argus", "--wait", "0"]))  # disables long-poll, must not raise

    def test_default_wait_is_longpoll_on(self):
        self.assertEqual(self._args(["--persona", "argus"]).wait, 50)





class Loom5ContractValidationTest(unittest.TestCase):
    """Loom re-audit 5. The chain must be VALIDATED, not assumed - written from loom's own repros."""

    def _target(self, cursor, emitter):
        return BoundedWindowEndToEndTest()._target(cursor, emitter)

    def _run(self, t, f):
        orig, km.fetch = km.fetch, f
        try:
            t.poll_once()
        finally:
            km.fetch = orig

    # ---- HIGH 1a: silence is not exhaustion ------------------------------------------------------
    def test_LOOM5_a_continuation_that_is_ABSENT_must_not_read_as_end_of_chain(self):
        # loom's repro: cursor 100, first visible 200, the continuation page [150] OMITS
        # next_before_id while 125 is hidden. Reading that omission as exhaustion advanced the cursor
        # to 200 with no alert, stepping over 125 permanently.
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)

        def f(opener, url, headers):
            if "before_id=" in url:
                return km.fetch_from_payload({"result": [{"id": 150}]})    # NO next_before_id key
            return server_page([{"id": 200}], omitted=1, exact=False)
        self._run(t, f)
        self.assertEqual(t.cursor, 100)                       # *** did NOT step over 125 ***
        self.assertTrue([x for e, x in em.events if e == "alert"])

    def test_LOOM5_a_MALFORMED_continuation_must_not_read_as_end_of_chain(self):
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)

        def f(opener, url, headers):
            if "before_id=" in url:
                return km.fetch_from_payload({"result": [{"id": 150}], "next_before_id": "150"})
            return server_page([{"id": 200}], omitted=1, exact=False)
        self._run(t, f)
        self.assertEqual(t.cursor, 100)
        self.assertTrue([x for e, x in em.events if e == "alert"])

    def test_an_EXPLICIT_null_continuation_is_still_a_real_terminal(self):
        # The counterpart that must keep working: an affirmed "nothing older" closes the span normally,
        # or the fix would pin forever and break the feature it is protecting.
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)

        def f(opener, url, headers):
            if "before_id=" in url:
                return km.fetch_from_payload({"result": [{"id": 150}], "next_before_id": None})
            return server_page([{"id": 200}], omitted=1, exact=False)
        self._run(t, f)
        self.assertEqual(t.cursor, 200)
        self.assertEqual([x for e, x in em.events if e == "alert"], [])

    # ---- HIGH 1b: the continuation must match the oldest row handed back -------------------------
    def test_LOOM5_a_continuation_below_the_oldest_returned_row_skips_and_must_pin(self):
        # loom's repro: page [150] with next_before_id 120 silently skips 130.
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)

        # The chain is otherwise WELL-FORMED and would terminate cleanly, so the only thing that can
        # catch the skip is the oldest-row check. Without it the walk reaches 100, declares the span
        # covered, and the cursor steps over 130 - which is the defect.
        # Each page DECLARES the withholding that its continuation implies, so the only thing wrong with
        # this chain is the skip - otherwise it would pin on the consistency check instead and this test
        # would pass without ever reaching the oldest-row check it exists for.
        pages = {200: lambda: server_page([{"id": 150}], omitted=1, exact=False,
                                          next_before_id=120),                      # skips 130
                 120: lambda: server_page([{"id": 100}])}

        def f(opener, url, headers):
            if "before_id=" in url:
                bid = int(url.split("before_id=")[1].split("&")[0])
                return pages[bid]()
            return server_page([{"id": 200}], omitted=1, exact=False)
        self._run(t, f)
        self.assertEqual(t.cursor, 100)
        self.assertTrue([x for e, x in em.events if e == "alert"])

    def test_a_continuation_equal_to_the_oldest_row_is_accepted(self):
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)
        pages = {200: lambda: server_page([{"id": 150}], omitted=1, exact=False),    # continuation == 150
                 150: lambda: server_page([{"id": 100}])}                            # withheld nothing

        def f(opener, url, headers):
            if "before_id=" in url:
                bid = int(url.split("before_id=")[1].split("&")[0])
                return pages[bid]()
            return server_page([{"id": 200}], omitted=1, exact=False)
        self._run(t, f)
        self.assertEqual(t.cursor, 200)
        self.assertEqual([x for e, x in em.events if e == "alert"], [])

    # ---- the parse layer ------------------------------------------------------------------------
    def test_fetch_separates_affirmed_null_from_silence(self):
        ok = km.fetch_from_payload({"result": [], "next_before_id": None})
        self.assertTrue(ok.continuation_ok)
        self.assertIsNone(ok.next_before_id)
        cur = km.fetch_from_payload({"result": [], "next_before_id": 0})
        self.assertTrue(cur.continuation_ok)
        self.assertEqual(cur.next_before_id, 0)               # 0 is a REAL cursor
        for bad in ({"result": []},                            # absent
                    {"result": [], "next_before_id": "12"},
                    {"result": [], "next_before_id": 1.5},
                    {"result": [], "next_before_id": True},
                    {"result": [], "next_before_id": -1}):
            p = km.fetch_from_payload(bad)
            self.assertFalse(p.continuation_ok, "%r must not count as an answer" % (bad,))


class Loom5CorruptStateTest(unittest.TestCase):
    """Loom re-audit 5, HIGH 2. A state file that EXISTS but is unusable must fail CLOSED."""

    def _write(self, text):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "hive.json")
        with open(p, "w") as f:
            f.write(text)
        return p

    def _load(self, text):
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            return km.StateFile(self._write(text), "idx").load(), buf.getvalue()
        finally:
            km.sys.stderr = err

    def test_unparseable_json_is_CORRUPT_not_absent(self):
        loaded, warned = self._load("{not json")
        self.assertIs(loaded, km.CORRUPT_STATE)
        self.assertIn("unparseable", warned)

    def test_valid_json_with_invalid_fields_is_CORRUPT_not_absent(self):
        loaded, _ = self._load(json.dumps({"identity": "idx", "cursor": "nope",
                                           "state": "UP", "consecutive_failures": 0}))
        self.assertIs(loaded, km.CORRUPT_STATE)

    def test_a_genuinely_absent_file_is_still_absent(self):
        self.assertIsNone(km.StateFile(os.path.join(tempfile.mkdtemp(), "nope.json"), "idx").load())

    def test_LOOM6_a_zero_byte_file_is_CORRUPT_not_absent(self):
        # I asserted the opposite last round and loom was right: a file that EXISTS is evidence a cursor
        # existed here, whatever its contents. Treating zero bytes as a first launch baselines over
        # everything since - the identical fail-open the unparseable path was just fixed for.
        for blank in ("", "   ", "\n\n"):
            loaded, warned = self._load(blank)
            self.assertIs(loaded, km.CORRUPT_STATE, "blank %r must not read as absent" % blank)
            self.assertIn("EMPTY", warned)

    def test_LOOM5_corrupt_state_re_emits_the_window_instead_of_baselining_over_it(self):
        # loom's repro: corrupt prior cursor 100 + visible 150,200 => baselined to 200 and skipped BOTH.
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = BoundedWindowEndToEndTest()._target(cursor=None, emitter=em)
        t.armed = False
        t.state_corrupt, t.pin_forced, t.pin_evidence_intact = True, True, False
        t.args.max_replay = 1                       # the cap must NOT be able to swallow them either
        orig, km.fetch = km.fetch, lambda o, u, h: km.Poll(
            True, items=[{"id": 150}, {"id": 200}], omitted=0, next_before_id=150)
        try:
            t.poll_once()
        finally:
            km.fetch = orig
        self.assertEqual(em.new_ids, [150, 200])    # *** both emitted, neither skipped ***
        self.assertTrue([f for e, f in em.events if e == "state_corrupt"])


class Loom6ContractValidationTest(unittest.TestCase):
    """Loom re-audit 6 - written from its repros. Every page is validated, including the empty and the
    self-contradictory ones, and the corruption pin now survives a restart."""

    def _target(self, cursor, emitter):
        return BoundedWindowEndToEndTest()._target(cursor, emitter)

    def _run(self, t, f):
        orig, km.fetch = km.fetch, f
        try:
            t.poll_once()
        finally:
            km.fetch = orig

    def test_LOOM6_an_EMPTY_page_claiming_more_must_pin(self):
        # loom's repro: main [200] omitted; page before 200 is EMPTY with next=150; page 150 returns
        # [100] terminal => span declared covered while 175 was never observed. The oldest-row check
        # cannot fire on an empty page, so emptiness itself has to be the signal.
        # ⚠️ THE EMPTY PAGE DECLARES A WITHHOLDING ON PURPOSE. Without it the page is ALSO
        # self-contradictory (withheld nothing + there is more), the round-7 consistency check rejects
        # it first, and this test goes green with the guard it is named for deleted - which is exactly
        # what the mutation harness caught when the round-7 check landed. A newer guard catching the
        # mutant first is the same defect as an older one doing it.
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)
        pages = {200: {"result": [], "truncated": True, "next_before_id": 150},
                 150: {"result": [{"id": 100}], "next_before_id": None}}

        def f(opener, url, headers):
            if "before_id=" in url:
                return km.fetch_from_payload(pages[int(url.split("before_id=")[1].split("&")[0])])
            return server_page([{"id": 200}], omitted=1, exact=False)
        self._run(t, f)
        self.assertEqual(t.cursor, 100)                 # *** did not step over 175 ***
        self.assertTrue([x for e, x in em.events if e == "alert"])

    def test_an_empty_page_that_AFFIRMS_the_end_still_closes(self):
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)

        def f(opener, url, headers):
            if "before_id=" in url:
                return km.fetch_from_payload({"result": [], "next_before_id": None})
            return server_page([{"id": 200}], omitted=1, exact=False)
        self._run(t, f)
        self.assertEqual(t.cursor, 200)

    def test_LOOM6_a_page_that_declares_withholding_AND_the_end_is_contradictory(self):
        # loom: page [150], truncated=true, next=null advances despite the declared withholding.
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)

        def f(opener, url, headers):
            if "before_id=" in url:
                return km.fetch_from_payload({"result": [{"id": 150}], "truncated": True,
                                              "next_before_id": None})
            return server_page([{"id": 200}], omitted=1, exact=False)
        self._run(t, f)
        self.assertEqual(t.cursor, 100)
        self.assertTrue([x for e, x in em.events if e == "alert"])

    def test_LOOM6_empty_plus_truncated_plus_terminal_is_also_contradictory(self):
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)

        def f(opener, url, headers):
            if "before_id=" in url:
                return km.fetch_from_payload({"result": [], "truncated": True, "next_before_id": None})
            return server_page([{"id": 200}], omitted=1, exact=False)
        self._run(t, f)
        self.assertEqual(t.cursor, 100)

    # ---- MEDIUM: bools, duplicates, uninterpretable flags ----------------------------------------
    def test_a_boolean_is_not_a_row_id(self):
        self.assertFalse(km.fetch_from_payload({"result": [{"id": True}]}).ok)

    def test_a_boolean_is_not_a_size_dropped(self):
        self.assertEqual(km._declared_omissions({"result": [], "size_dropped": True}), (0, True))

    def test_duplicate_ids_in_one_page_are_rejected(self):
        # The cursor dedupes against what it has ALREADY delivered, so a repeat inside one window is
        # emitted twice.
        p = km.fetch_from_payload({"result": [{"id": 5}, {"id": 5}], "next_before_id": None})
        self.assertFalse(p.ok)
        self.assertIn("duplicate", p.reason)

    def test_an_uninterpretable_truncation_flag_is_not_a_denial(self):
        for junk in ("yes", 1, [], {}):
            n, exact = km._declared_omissions({"result": [], "truncated": junk})
            self.assertGreaterEqual(n, 1, "truncated=%r must not read as 'nothing withheld'" % (junk,))
            self.assertFalse(exact)
        self.assertEqual(km._declared_omissions({"result": [], "truncated": False}), (0, True))


class Loom6PinPersistenceTest(unittest.TestCase):
    """Loom re-audit 6, HIGH 1. A pin that does not survive a restart is not a pin."""

    def _path(self):
        return os.path.join(tempfile.mkdtemp(), "hive.json")

    def test_the_corruption_pin_round_trips(self):
        p = self._path()
        km.StateFile(p, "idx").save(149, "UP", 0, pin_forced=True, pin_evidence_intact=False,
                                    state_corrupt=True)
        loaded = km.StateFile(p, "idx").load()
        self.assertTrue(loaded["pin_forced"])
        self.assertFalse(loaded["pin_evidence_intact"])
        self.assertTrue(loaded["state_corrupt"])
        self.assertEqual(loaded["cursor"], 149)

    def test_an_ordinary_save_persists_no_pin(self):
        p = self._path()
        km.StateFile(p, "idx").save(200, "UP", 0)
        loaded = km.StateFile(p, "idx").load()
        self.assertFalse(loaded["pin_forced"])
        self.assertTrue(loaded["pin_evidence_intact"])
        self.assertFalse(loaded["state_corrupt"])
        with open(p) as f:
            self.assertNotIn("pin_forced", json.load(f))     # absent, not written as false

    def test_LOOM6_a_restart_cannot_let_the_replay_cap_cross_a_corruption_pin(self):
        # loom's repro: corrupt prior, visible 150/200 => cursor 149 forced; RESTART with a bounded
        # window 200/201/202 and max_replay 1 => cursor jumped to 202, crossing the hidden span.
        p = self._path()
        km.StateFile(p, "idx").save(149, "UP", 0, pin_forced=True, pin_evidence_intact=False,
                                    state_corrupt=True)
        loaded = km.StateFile(p, "idx").load()

        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = BoundedWindowEndToEndTest()._target(cursor=loaded["cursor"], emitter=em)
        t.armed = False
        t.pin_forced = loaded["pin_forced"] or not loaded["pin_evidence_intact"]
        t.pin_evidence_intact = loaded["pin_evidence_intact"]
        t.state_corrupt = loaded["state_corrupt"]
        t.args.max_replay = 1
        orig, km.fetch = km.fetch, lambda o, u, h: km.Poll(
            True, items=[{"id": 200}, {"id": 201}, {"id": 202}], omitted=2, next_before_id=200)
        try:
            t.poll_once()
        finally:
            km.fetch = orig
        self.assertEqual(t.cursor, 149)             # *** the pin survived the restart ***
        self.assertEqual([e for e, _ in em.events if e == "replay_capped"], [])


    def test_LOOM6_the_pin_is_restored_by_WatchTarget_init_itself(self):
        # The previous test simulated what __init__ does; this one EXERCISES it, which is the difference
        # between testing the property and testing my restatement of it. The mutation harness caught
        # that gap: breaking the real load path changed nothing the suite could see.
        base = self._path()
        url = "http://x/api/inbox?persona=argus"
        # The watcher derives ONE FILE PER PERSONA from the base path, so the fixture has to write where
        # __init__ will actually look. Writing to the base path made this test pass vacuously at first.
        derived = km._state_path_for_persona(base, "argus")
        km.StateFile(derived, km.canonical_identity(url)).save(
            149, "UP", 0, pin_forced=True, pin_evidence_intact=False, state_corrupt=True)

        class A(BoundedWindowEndToEndTest.FullArgs):
            state_file = base
            seed_at = None
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = km.WatchTarget("argus", url, None, {}, A(), em)
        # This used to be a hasattr(..., "close") guard that matched nothing, so it released nothing -
        # a cleanup that silently did no work, which is why the lock fd leaked anyway. StateFile now has
        # a real unlock().
        self.addCleanup(t.state_file.unlock)
        self.assertTrue(t.pin_forced)              # restored by __init__, not by the test
        self.assertFalse(t.pin_evidence_intact)
        self.assertTrue(t.state_corrupt)
        self.assertEqual(t.cursor, 149)

    def test_LOOM6_the_persisted_pin_is_authoritative_ON_ITS_OWN(self):
        # ISOLATION MATTERS HERE. With both flags set, "pin_forced" and "evidence lost" produce the same
        # answer, so a load path that ignores the persisted flag entirely still looks correct. This case
        # persists ONLY pin_forced, where the inference from missing evidence would say False.
        base = self._path()
        url = "http://x/api/inbox?persona=argus"
        km.StateFile(km._state_path_for_persona(base, "argus"), km.canonical_identity(url)).save(
            149, "UP", 0, pin_forced=True, pin_evidence_intact=True, state_corrupt=False)

        class A(BoundedWindowEndToEndTest.FullArgs):
            state_file = base
            seed_at = None
        t = km.WatchTarget("argus", url, None, {}, A(), BoundedWindowEndToEndTest.RecordingEmitter())
        self.addCleanup(t.state_file.unlock)       # the lock fd is ours to release (item 7 hygiene)
        self.assertTrue(t.pin_forced)              # from the FLAG, not inferred from lost evidence
        self.assertTrue(t.pin_evidence_intact)


class Loom7DeliveryAcknowledgementTest(unittest.TestCase):
    """Loom re-audit 7, HIGH 1. THE CURSOR IS AN ACKNOWLEDGEMENT, so only a DELIVERED message may move it.

    Loom's repro: an --exec that exits 9 had its result discarded (`check=False`, return value dropped),
    the cursor advanced to 200 and persisted, and the message was never fetched again. The wake hook that
    is the entire point of exec mode failed and the watcher recorded success - while the README promised
    each message exactly once across restarts.
    """
    E2E = BoundedWindowEndToEndTest

    def test_LOOM7_a_failed_delivery_does_NOT_advance_the_cursor(self):
        em = self.E2E.RecordingEmitter(fail_ids={200})
        t = self.E2E()._target(cursor=100, emitter=em)
        self.E2E()._run(t, self.E2E()._fetch([{"id": 200}], 0))
        self.assertEqual(em.new_ids, [])            # nothing was handed over
        self.assertEqual(t.cursor, 100)             # *** loom's repro: this used to become 200 ***
        self.assertEqual(t.emitted_above, set())    # and nothing is recorded as delivered

    def test_LOOM7_a_failed_delivery_is_RETRIED_rather_than_lost(self):
        # The other half of the repro, and the half that matters to a consumer: "never retried".
        em = self.E2E.RecordingEmitter(fail_ids={200})
        t = self.E2E()._target(cursor=100, emitter=em)
        fetch = self.E2E()._fetch([{"id": 200}], 0)
        self.E2E()._run(t, fetch, times=3)
        self.assertEqual(em.new_ids, [])
        em.fail_ids.clear()                          # the consumer recovers
        self.E2E()._run(t, fetch)
        self.assertEqual(em.new_ids, [200])          # delivered on the retry, not skipped
        self.assertEqual(t.cursor, 200)

    def test_a_SUCCESSFUL_delivery_still_advances_the_cursor(self):
        # The control. Without it every assertion above is satisfied by a watcher that never advances.
        em = self.E2E.RecordingEmitter()
        t = self.E2E()._target(cursor=100, emitter=em)
        self.E2E()._run(t, self.E2E()._fetch([{"id": 200}], 0))
        self.assertEqual(em.new_ids, [200])
        self.assertEqual(t.cursor, 200)

    def test_delivery_STOPS_at_the_first_failure_so_order_is_preserved(self):
        # at-least-once IN ORDER: a consumer must not see 30 before a retried 20.
        em = self.E2E.RecordingEmitter(fail_ids={20})
        t = self.E2E()._target(cursor=0, emitter=em)
        self.E2E()._run(t, self.E2E()._fetch([{"id": 10}, {"id": 20}, {"id": 30}], 0))
        self.assertEqual(em.new_ids, [10])           # 30 was NOT delivered ahead of the failed 20
        self.assertEqual(t.cursor, 10)               # acknowledged exactly as far as delivery got

    def test_LOOM7_a_COMPLETE_window_cannot_acknowledge_past_an_undelivered_message(self):
        # The subtle leak: `complete` proves the SERVER withheld nothing, which says nothing about
        # whether WE handed over what it showed us. Without the gate the window's max id advances the
        # cursor straight over the message whose delivery just failed.
        em = self.E2E.RecordingEmitter(fail_ids={200})
        t = self.E2E()._target(cursor=100, emitter=em)
        fetch = self.E2E()._fetch([{"id": 90}, {"id": 100}, {"id": 200}], 0)
        self.E2E()._run(t, fetch)
        self.assertEqual(t.cursor, 199)              # up to just below the undelivered id, never past it
        em.fail_ids.clear()
        self.E2E()._run(t, fetch)
        self.assertEqual(em.new_ids, [200])

    def test_a_delivery_failure_is_reported_ONCE_and_self_clears(self):
        em = self.E2E.RecordingEmitter(fail_ids={200})
        t = self.E2E()._target(cursor=100, emitter=em)
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            self.E2E()._run(t, self.E2E()._fetch([{"id": 200}], 0), times=4)
            self.assertEqual(buf.getvalue().count("delivery of message"), 1)   # not once per poll
            em.fail_ids.clear()
            self.E2E()._run(t, self.E2E()._fetch([{"id": 200}], 0))
            self.assertIn("recovered", buf.getvalue())                          # keyed on the condition
        finally:
            km.sys.stderr = err

    # ---- the emitter's own contract --------------------------------------------------------------
    def test_exec_exit_zero_IS_an_acknowledgement_and_nonzero_is_NOT(self):
        e = km.Emitter("exec-per-event", "true", 220, False)
        self.assertIs(e.new({"id": 1, "from": "river"}), True)
        e = km.Emitter("exec-per-event", "exit 9", 220, False)       # loom's exact exit code
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            self.assertIs(e.new({"id": 1, "from": "river"}), False)
        finally:
            km.sys.stderr = err
        self.assertIn("exited 9", buf.getvalue())

    def test_an_exec_TIMEOUT_is_not_a_delivery(self):
        # The command may well have run - so consumers must be idempotent - but we hold no
        # acknowledgement, and inventing one is how mail disappears.
        e = km.Emitter("exec-per-event", "sleep 5", 220, False)
        orig, km.EXEC_TIMEOUT = km.EXEC_TIMEOUT, 0.05
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            self.assertIs(e.new({"id": 1, "from": "river"}), False)
        finally:
            km.EXEC_TIMEOUT, km.sys.stderr = orig, err

    def test_a_SUPPRESSED_author_is_ACKNOWLEDGED_not_treated_as_a_failure(self):
        # ⚠️ REGRESSION GUARD. --suppress-author is a deliberate policy drop; if the gate read it as an
        # undelivered message the watermark would pin on that author's next message and never move
        # again - turning a noise filter into a permanent stall.
        e = km.Emitter("stdout-jsonl", None, 220, False, suppress_authors=["argus"])
        self.assertIs(e.new({"id": 1, "from": "argus"}), True)

    def test_a_failed_sink_write_is_NOT_a_delivery(self):
        d = tempfile.mkdtemp()
        sink = km.RotatingFileSink(os.path.join(d, "events.ndjson"), 0, 5)
        self.addCleanup(sink.close)
        self.assertTrue(sink.write("{}\n"))                 # control: a healthy write IS a delivery
        real_fh = sink._fh
        self.addCleanup(real_fh.close)

        class Broken:
            def write(self, *_):
                raise OSError("ENOSPC")
            def flush(self):
                pass
            def close(self):
                pass
        sink._fh = Broken()
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            self.assertFalse(sink.write("{}\n"))
        finally:
            km.sys.stderr = err
        self.assertIn("holding the cursor", buf.getvalue())


class Loom7PoisonMessageTest(unittest.TestCase):
    """A CONTENT BYTE MUST NEVER BECOME A DELIVERY FAILURE.

    ★ FOUND BY RE-READING ROUND 7 ADVERSARIALLY, and it is the delivery gate's own shadow: before the
    gate, an event that could not be represented was swallowed; after it, the SAME event would report a
    permanent non-delivery and wedge the watermark on one message forever. Two real shapes do this - a
    NUL (illegal in an environment value) and a lone surrogate (not encodable to UTF-8). The file path
    was worse still: it raised UnicodeEncodeError straight out of poll_once, which under a KeepAlive
    supervisor is a CRASH LOOP, because the same message is refetched every restart.

    Trading a silent skip for a permanent stall is the exact failure this project keeps re-learning, so
    the event is made representable rather than failed on.
    """
    def test_a_NUL_in_content_does_not_block_exec_delivery(self):
        e = km.Emitter("exec-per-event", "true", 220, False)
        self.assertIs(e.new({"id": 1, "from": "river", "content": "hello\x00world"}), True)

    def test_a_LONE_SURROGATE_does_not_block_exec_delivery(self):
        e = km.Emitter("exec-per-event", "true", 220, False)
        self.assertIs(e.new({"id": 2, "from": "river", "content": "bad \ud800 char"}), True)

    def test_a_LONE_SURROGATE_does_not_CRASH_the_file_sink(self):
        d = tempfile.mkdtemp()
        sink = km.RotatingFileSink(os.path.join(d, "events.ndjson"), 0, 5)
        self.addCleanup(sink.close)
        e = km.Emitter("stdout-jsonl", None, 220, False, sink=sink)
        self.assertIs(e.new({"id": 3, "from": "river", "content": "bad \ud800 char"}), True)
        line = read_file(os.path.join(d, "events.ndjson"))
        self.assertIn('"id": 3', line)
        self.assertNotIn("\ud800", line)

    def test_the_unrepresentable_field_can_be_ANY_field_not_just_content(self):
        # sanitising the SERIALISED line covers `from`, an alarm `reason` built from server data, etc.
        d = tempfile.mkdtemp()
        sink = km.RotatingFileSink(os.path.join(d, "events.ndjson"), 0, 5)
        self.addCleanup(sink.close)
        e = km.Emitter("stdout-jsonl", None, 220, False, sink=sink)
        self.assertIs(e.lifecycle("alert", reason="stranded-mail: \ud800 inbox", persona="argus"), True)
        self.assertIn("stranded-mail", read_file(os.path.join(d, "events.ndjson")))

    def test_ordinary_unicode_is_left_ALONE(self):
        # The control. A sanitiser that mangles normal text would be its own defect - most mail is not ASCII.
        d = tempfile.mkdtemp()
        sink = km.RotatingFileSink(os.path.join(d, "events.ndjson"), 0, 5)
        self.addCleanup(sink.close)
        e = km.Emitter("stdout-jsonl", None, 220, False, sink=sink)
        e.new({"id": 4, "from": "river", "content": "naïve café 日本語 — ★ emoji 🎉"})
        self.assertIn("naïve café 日本語 — ★ emoji 🎉", read_file(os.path.join(d, "events.ndjson")))
        self.assertEqual(km._safe_text("naïve café 日本語 🎉"), "naïve café 日本語 🎉")

    def test_an_encoding_error_on_write_is_a_FAILED_DELIVERY_not_a_crash(self):
        # The sanitiser makes this branch unreachable through the normal path, which is exactly why it
        # needs its own test: the mutation harness showed that reverting the guard to OSError-only broke
        # nothing, i.e. the defence was untested. Whatever the cause, an encoding error must come back as
        # a non-delivery (loud, retried) rather than propagate out of poll_once, because an exception
        # escaping the poll loop under a KeepAlive supervisor is a silent crash loop.
        d = tempfile.mkdtemp()
        sink = km.RotatingFileSink(os.path.join(d, "events.ndjson"), 0, 5)
        self.addCleanup(sink.close)
        real_fh = sink._fh
        self.addCleanup(real_fh.close)

        class Strict:
            def write(self, s):
                raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")
            def flush(self):
                pass
            def close(self):
                pass
        sink._fh = Strict()
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            # self.fail on escape, so this is a FAILURE not an ERROR: the mutation gate refuses to count
            # an error-only mutant, and a test that "passes" by letting an exception fly teaches it nothing.
            try:
                result = sink.write("{}\n")
            except Exception as e:
                self.fail("write() raised %r instead of reporting a failed delivery" % (e,))
            self.assertIs(result, False)
        finally:
            km.sys.stderr = err
        self.assertIn("holding the cursor", buf.getvalue())

    def test_a_poison_message_does_not_WEDGE_the_watermark(self):
        # End to end: the whole point. This message can never be delivered by a naive emitter, and the
        # delivery gate would hold the cursor below it on every poll, forever.
        E2E = BoundedWindowEndToEndTest
        d = tempfile.mkdtemp()
        sink = km.RotatingFileSink(os.path.join(d, "events.ndjson"), 0, 5)
        self.addCleanup(sink.close)
        em = km.Emitter("stdout-jsonl", None, 220, False, sink=sink)
        t = E2E()._target(cursor=100, emitter=em)
        E2E()._run(t, lambda o, u, h: server_page(
            [{"id": 200, "from": "river", "content": "poison \ud800\x00 payload"}], omitted=0))
        self.assertEqual(t.cursor, 200, "an unrepresentable message must not freeze the watermark")


class Loom7DurabilityOrderingTest(unittest.TestCase):
    """Loom re-audit 7, MEDIUM. The EVENT must be durable BEFORE the CURSOR that acknowledges it."""
    E2E = BoundedWindowEndToEndTest

    class FakeState:
        def __init__(self, log):
            self.log = log
        def save(self, *a, **k):
            self.log.append("save-cursor")

    def test_the_sink_is_SYNCED_BEFORE_the_cursor_is_persisted(self):
        # Ordering is the whole property: syncing both but in the wrong order still lets a power loss
        # leave a cursor that has forgotten mail nobody received.
        log = []
        em = self.E2E.RecordingEmitter()
        orig_sync = em.sync
        em.sync = lambda persona=None: (log.append("sync-event"), orig_sync(persona))[1]
        t = self.E2E()._target(cursor=100, emitter=em)
        t.state_file = self.FakeState(log)
        self.E2E()._run(t, self.E2E()._fetch([{"id": 200}], 0))
        self.assertEqual(log, ["sync-event", "save-cursor"])

    def test_a_FAILED_sync_retracts_the_whole_tick(self):
        # If the events cannot be proven durable, none of them count as delivered - the acknowledgement
        # is withdrawn wholesale rather than left half-true.
        em = self.E2E.RecordingEmitter(sync_ok=False)
        t = self.E2E()._target(cursor=100, emitter=em)
        self.E2E()._run(t, self.E2E()._fetch([{"id": 200}], 0))
        self.assertEqual(em.new_ids, [200])          # it was written...
        self.assertEqual(t.cursor, 100)              # ...but never acknowledged
        self.assertEqual(t.emitted_above, set())

    @staticmethod
    def _fsync_spy(calls):
        """Record fsyncs, separating FILE from DIRECTORY - a directory sync is a different guarantee
        (the NAME is durable) from a file sync (the BYTES are), and a test that lumps them together
        cannot tell which one it lost."""
        import stat as _stat
        real = km.os.fsync
        def spy(fd):
            try:
                calls.append("dir" if _stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            except OSError:
                calls.append("file")
            return real(fd)
        return spy, real

    def test_the_barrier_syncs_only_THIS_persona_s_sink(self):
        # The Emitter is shared by every watch target, so an unscoped sync would let ONE persona's
        # failing sink retract every OTHER persona's deliveries - a directory-wide duplicate storm
        # caused by a stream nobody reads.
        em = self.E2E.RecordingEmitter()
        t = self.E2E()._target(cursor=100, emitter=em)
        self.E2E()._run(t, self.E2E()._fetch([{"id": 200}], 0))
        self.assertEqual(em.synced_personas, ["argus"])

    def test_a_real_emitter_syncs_only_the_named_persona_s_file(self):
        d = tempfile.mkdtemp()
        em = km.Emitter("stdout-jsonl", None, 220, False,
                        sink_template=os.path.join(d, "events.{persona}.ndjson"))
        self.addCleanup(em.close)
        em.new({"id": 1, "from": "river", "_persona": "argus"})
        em.new({"id": 2, "from": "river", "_persona": "loom"})
        calls = []
        spy, real = self._fsync_spy(calls)
        km.os.fsync = spy
        try:
            self.assertTrue(em.sync("argus"))
            self.assertEqual(calls.count("file"), 1, "only argus's sink bytes should have been synced")
            self.assertTrue(em.sync("loom"))
            self.assertEqual(calls.count("file"), 2)
        finally:
            km.os.fsync = real

    def test_a_healthy_sync_is_the_control(self):
        em = self.E2E.RecordingEmitter(sync_ok=True)
        t = self.E2E()._target(cursor=100, emitter=em)
        self.E2E()._run(t, self.E2E()._fetch([{"id": 200}], 0))
        self.assertEqual(t.cursor, 200)
        self.assertGreaterEqual(em.syncs, 1)

    def test_the_state_save_fsyncs_the_DIRECTORY_not_just_the_file(self):
        # os.replace is atomic for a reader, but the RENAME is only durable once the directory entry is
        # synced - otherwise the contents can survive a power loss while the name still points at the
        # previous inode, i.e. a silently older cursor.
        import stat as _stat
        d = tempfile.mkdtemp()
        synced_dirs = []
        real = km.os.fsync

        def spy(fd):
            try:
                if _stat.S_ISDIR(os.fstat(fd).st_mode):
                    synced_dirs.append(fd)
            except OSError:
                pass
            return real(fd)
        km.os.fsync = spy
        try:
            km.StateFile(os.path.join(d, "hive.json"), "idx").save(200, "UP", 0)
        finally:
            km.os.fsync = real
        self.assertTrue(synced_dirs, "the directory holding the state file was never fsynced")

    def test_the_sink_syncs_only_when_there_is_something_to_sync(self):
        d = tempfile.mkdtemp()
        sink = km.RotatingFileSink(os.path.join(d, "events.ndjson"), 0, 5)
        self.addCleanup(sink.close)
        calls = []
        spy, real = self._fsync_spy(calls)
        km.os.fsync = spy
        try:
            self.assertTrue(sink.sync())            # the NEW file's directory entry is synced once...
            self.assertEqual(calls.count("file"), 0, "no bytes were written, so nothing to sync")
            self.assertEqual(calls.count("dir"), 1, "a newly CREATED file needs its directory entry durable")
            sink.write("{}\n")
            self.assertTrue(sink.sync())
            self.assertEqual(calls.count("file"), 1)
            self.assertTrue(sink.sync())            # already durable: neither is repeated
            self.assertEqual(calls.count("file"), 1)
            self.assertEqual(calls.count("dir"), 1)
        finally:
            km.os.fsync = real


class Loom8FilePermissionsTest(unittest.TestCase):
    """Loom re-audit 8, HIGH 1. THE EVENT STREAM CARRIES MESSAGE BODIES AND WAS WORLD-READABLE.

    A plain open() takes the process umask - 022 here and on most defaults - so every
    events.<persona>.ndjson was created 0644. Confirmed on the LIVE deployment before fixing: the argus
    stream was 0644 with message content in it, alongside a 0600 token and a 0600 state file. The one
    file nobody had thought about is the one with the plaintext.
    """
    def _mode(self, path):
        return os.stat(path).st_mode & 0o777

    def test_a_NEW_event_file_is_created_0600(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.ndjson")
        sink = km.RotatingFileSink(p, 0, 5)
        self.addCleanup(sink.close)
        sink.write('{"event":"new","content":"private mail"}\n')
        self.assertEqual(self._mode(p), 0o600, "the event stream must not be readable by other users")

    def test_an_EXISTING_world_readable_file_is_REPAIRED(self):
        # The mode argument to os.open applies only at CREATION, so a fix that stops there leaves every
        # already-leaked file exactly as permissive as it was. loom asked for exactly this: "verify
        # existing modes after restart".
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.ndjson")
        with open(p, "w") as f:
            f.write('{"event":"new","content":"leaked earlier"}\n')
        os.chmod(p, 0o644)
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            sink = km.RotatingFileSink(p, 0, 5)
            self.addCleanup(sink.close)
        finally:
            km.sys.stderr = err
        self.assertEqual(self._mode(p), 0o600)
        self.assertIn("tightened", buf.getvalue())

    def test_the_LOCK_sidecar_is_also_0600(self):
        d = tempfile.mkdtemp()
        sf = km.StateFile(os.path.join(d, "hive.json"), "idx")
        sf.lock()
        self.addCleanup(sf.unlock)
        self.assertEqual(self._mode(os.path.join(d, "hive.json.lock")), 0o600)

    def test_the_STATE_file_stays_0600(self):
        # It already was, via mkstemp - this is the regression guard, since the fix touched save()'s
        # neighbourhood and a state file is exactly as sensitive as the stream.
        d = tempfile.mkdtemp()
        p = os.path.join(d, "hive.json")
        km.StateFile(p, "idx").save(200, "UP", 0)
        self.assertEqual(self._mode(p), 0o600)

    def test_a_ROTATED_archive_does_not_leak_what_the_live_file_protects(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.ndjson")
        sink = km.RotatingFileSink(p, 120, 3)
        self.addCleanup(sink.close)
        for i in range(6):
            sink.write('{"event":"new","content":"private %d"}\n' % i)
        archives = [os.path.join(d, f) for f in os.listdir(d) if f.startswith("events.ndjson.")]
        self.assertTrue(archives, "precondition: rotation happened")
        for a in archives:
            self.assertEqual(self._mode(a), 0o600, "%s leaks what the live file protects" % a)

    def test_a_directory_WE_create_is_0700(self):
        d = tempfile.mkdtemp()
        nested = os.path.join(d, "made", "by", "us")
        sink = km.RotatingFileSink(os.path.join(nested, "events.ndjson"), 0, 5)
        self.addCleanup(sink.close)
        self.assertEqual(os.stat(nested).st_mode & 0o777, 0o700)


class Loom9SafeOpenTest(unittest.TestCase):
    """Loom re-audit 9, HIGH 1. THE SECURITY FIX WAS ITSELF A SECURITY DEFECT, and a worse one.

    Round 8 stopped the event stream being world-READABLE. The repair it used followed symlinks, checked
    neither owner nor file type, and - because I made it deliberately best-effort so "a file we do not own
    cannot kill the watcher" - wrote the mail anyway when the chmod failed. That reasoning is exactly
    backwards for a file we are about to append PRIVATE MAIL to: it converted a passive disclosure into an
    active write primitive. Refusing is the only safe answer, and the caller turns a refusal into a FAILED
    DELIVERY, so the cursor holds and nothing is lost.
    """
    def test_a_SYMLINK_in_place_of_the_events_file_is_REFUSED(self):
        d = tempfile.mkdtemp()
        target = os.path.join(d, "victim.txt")
        link = os.path.join(d, "events.ndjson")
        with open(target, "w") as f:
            f.write("pre-existing victim content\n")
        os.chmod(target, 0o644)
        os.symlink(target, link)
        with self.assertRaises(OSError):
            km.RotatingFileSink(link, 0, 5)
        self.assertEqual(os.stat(target).st_mode & 0o777, 0o644, "the target must not be chmod'ed")
        self.assertNotIn("event", read_file(target), "the target must not receive our mail")

    def test_a_DANGLING_symlink_does_not_create_its_target(self):
        # loom: "dangling symlink created target in unsynced other dir" - O_CREAT through a link creates
        # the TARGET, wherever that points, and it is not in a directory we sync or protect.
        d = tempfile.mkdtemp()
        victim = os.path.join(d, "created-elsewhere.txt")
        os.symlink(victim, os.path.join(d, "events.ndjson"))
        with self.assertRaises(OSError):
            km.RotatingFileSink(os.path.join(d, "events.ndjson"), 0, 5)
        self.assertFalse(os.path.exists(victim), "a dangling link must not be followed into a new file")

    def test_a_file_we_CANNOT_tighten_is_REFUSED_not_written(self):
        # loom's exact repro: "writable preexisting file stayed 0666 and received mail".
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.ndjson")
        open(p, "w").close()
        os.chmod(p, 0o666)
        real = km.os.fchmod
        km.os.fchmod = lambda fd, m: (_ for _ in ()).throw(OSError("EPERM"))
        try:
            with self.assertRaises(km.InsecureFile):
                km.RotatingFileSink(p, 0, 5)
        finally:
            km.os.fchmod = real
        self.assertNotIn("event", read_file(p), "mail must never land in a file we could not secure")

    def test_a_file_owned_by_SOMEONE_ELSE_is_refused(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.ndjson")
        real = km.os.fstat
        class FakeStat:
            st_mode = 0o100600      # regular file, right mode
            st_uid = os.geteuid() + 1000
        km.os.fstat = lambda fd: FakeStat()
        try:
            with self.assertRaises(km.InsecureFile):
                km.RotatingFileSink(p, 0, 5)
        finally:
            km.os.fstat = real

    def test_a_NON_REGULAR_file_WE_OWN_is_refused(self):
        # ⚠️ ISOLATION MATTERS. /dev/null looks like the obvious case, but it is root-owned, so the OWNER
        # check fires first and the regular-file branch is never reached - the mutation harness proved
        # that by surviving a test built on it. This fakes a FIFO we own, so S_ISREG is the only check
        # that can reject it. Without it the watcher would "deliver" every message into a device and
        # advance its cursor over real mail.
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.ndjson")
        real = km.os.fstat
        class FifoWeOwn:
            st_mode = 0o010600          # S_IFIFO | 0600
            st_uid = os.geteuid()
        km.os.fstat = lambda fd: FifoWeOwn()
        try:
            with self.assertRaises(km.InsecureFile):
                km.RotatingFileSink(p, 0, 5)
        finally:
            km.os.fstat = real

    def test_a_file_whose_chmod_SILENTLY_DOES_NOTHING_is_refused(self):
        # Distinct from "chmod raises": some filesystems accept the call and change nothing. The mode must
        # be RE-READ after tightening, or the repair is assumed rather than verified.
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.ndjson")
        open(p, "w").close()
        os.chmod(p, 0o644)
        real = km.os.fchmod
        km.os.fchmod = lambda fd, m: None          # accepts, does nothing
        try:
            with self.assertRaises(km.InsecureFile):
                km.RotatingFileSink(p, 0, 5)
        finally:
            km.os.fchmod = real
        self.assertNotIn("event", read_file(p))

    def test_a_NON_REGULAR_file_is_refused_WITHOUT_BLOCKING(self):
        # A FIFO is the sharp case: opening one O_WRONLY blocks until a reader appears, so without
        # O_NONBLOCK the watcher HANGS - worse than crashing, because nothing reports it and the events
        # simply stop. The alarm turns that hang into a FAILURE, which is what the mutation gate can
        # actually count; a test that hangs teaches the gate nothing and stalls it besides.
        import signal
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.ndjson")
        os.mkfifo(p)
        def _blocked(signum, frame):
            raise AssertionError("opening a FIFO BLOCKED - O_NONBLOCK is missing")
        old_handler = signal.signal(signal.SIGALRM, _blocked)
        signal.alarm(3)
        try:
            with self.assertRaises(OSError):
                km.RotatingFileSink(p, 0, 5)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

    def test_the_CONTROL_a_normal_file_still_opens_and_writes(self):
        # Without this, every assertion above is satisfied by a sink that refuses everything.
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.ndjson")
        sink = km.RotatingFileSink(p, 0, 5)
        self.addCleanup(sink.close)
        self.assertTrue(sink.write('{"event":"new"}\n'))
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)


class Loom9RepairEveryArtifactTest(unittest.TestCase):
    """Loom re-audit 9, HIGH 2 + MEDIUM. Repairing only the file you happen to open repairs almost nothing."""

    def test_a_preexisting_ROTATED_ARCHIVE_is_repaired(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.ndjson")
        with open(p + ".1", "w") as f:
            f.write("old mail\n")
        os.chmod(p + ".1", 0o644)
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            km.RotatingFileSink(p, 0, 5).close()
        finally:
            km.sys.stderr = err
        self.assertEqual(os.stat(p + ".1").st_mode & 0o777, 0o600,
                         "an archive written by an older version is never reopened, so it leaks forever")

    def test_the_STATE_FILE_is_repaired_when_the_lock_is_taken(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "hive.json")
        with open(p, "w") as f:
            f.write("{}")
        os.chmod(p, 0o644)
        sf = km.StateFile(p, "idx")
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            sf.lock()
        finally:
            km.sys.stderr = err
        self.addCleanup(sf.unlock)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_0700_is_normalised_to_EXACTLY_0600(self):
        # The round-8 test was `st_mode & 0o077`, which ignores a stray owner-EXECUTE bit; 0700 is a mode
        # nothing here should ever have.
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.ndjson")
        open(p, "w").close()
        os.chmod(p, 0o700)
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            km.RotatingFileSink(p, 0, 5).close()
        finally:
            km.sys.stderr = err
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_EVERY_directory_level_is_0700_not_just_the_leaf(self):
        # os.makedirs(mode=...) applies the mode to the LEAF only; intermediates take the umask.
        d = tempfile.mkdtemp()
        try:
            km._makedirs_private(os.path.join(d, "a", "b", "c"))
        except Exception as e:
            self.fail("_makedirs_private raised on a nested path: %r" % (e,))
        for rel in (("a",), ("a", "b"), ("a", "b", "c")):
            path = os.path.join(d, *rel)
            if not os.path.isdir(path):
                self.fail("%s was never created" % ("/".join(rel),))   # a FAILURE, not a stat ERROR
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o700, rel)

    def test_a_WORLD_WRITABLE_parent_is_reported(self):
        d = tempfile.mkdtemp()
        os.chmod(d, 0o777)
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            km._makedirs_private(os.path.join(d, "sub"))
        finally:
            km.sys.stderr = err
        self.assertIn("writable by other local users", buf.getvalue())


class Loom9FailedSinkIsAFailedDeliveryTest(unittest.TestCase):
    """Loom re-audit 9, MEDIUM. A sink we cannot use must fail DELIVERY - never crash, never divert."""

    def test_an_unusable_persona_sink_fails_delivery_and_does_NOT_fall_back_to_stdout(self):
        # _sink_for returning None means "no sink configured, write to stdout". A sink we REFUSED must
        # not take that path, or the mail we declined to file gets printed instead.
        d = tempfile.mkdtemp()
        os.symlink(os.path.join(d, "nowhere.txt"), os.path.join(d, "events.argus.ndjson"))
        em = km.Emitter("stdout-jsonl", None, 220, False,
                        sink_template=os.path.join(d, "events.{persona}.ndjson"))
        self.addCleanup(em.close)
        buf, err = __import__("io").StringIO(), km.sys.stderr
        out, km.sys.stderr, km.sys.stdout = km.sys.stdout, buf, __import__("io").StringIO()
        try:
            delivered = em.new({"id": 1, "from": "river", "content": "secret", "_persona": "argus"})
            printed = km.sys.stdout.getvalue()
        finally:
            km.sys.stderr, km.sys.stdout = err, out
        self.assertIs(delivered, False, "an unusable sink is a FAILED delivery")
        self.assertNotIn("secret", printed, "it must not be diverted to stdout")
        self.assertFalse(em.sync("argus"), "and nothing about it is durable")

    def test_ONE_broken_persona_does_not_break_the_others(self):
        d = tempfile.mkdtemp()
        os.symlink(os.path.join(d, "nowhere.txt"), os.path.join(d, "events.argus.ndjson"))
        em = km.Emitter("stdout-jsonl", None, 220, False,
                        sink_template=os.path.join(d, "events.{persona}.ndjson"))
        self.addCleanup(em.close)
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            self.assertIs(em.new({"id": 1, "from": "r", "_persona": "argus"}), False)
            self.assertIs(em.new({"id": 2, "from": "r", "_persona": "loom"}), True)
        finally:
            km.sys.stderr = err

    def test_a_failed_REOPEN_after_rotation_does_not_escape_the_poll_loop(self):
        # An exception out of write() unwinds through poll_once and, under KeepAlive, is a crash loop.
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.ndjson")
        sink = km.RotatingFileSink(p, 60, 2)
        self.addCleanup(sink.close)
        real = km._open_private
        km._open_private = lambda *a, **k: (_ for _ in ()).throw(km.InsecureFile("simulated"))
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            try:
                result = sink.write('{"pad":"%s"}\n' % ("x" * 90))   # triggers rotation -> reopen fails
            except Exception as e:
                self.fail("a failed reopen escaped write(): %r" % (e,))
            self.assertIn(result, (True, False))                  # it RETURNED rather than raising
            # ...and while the path is still unusable, delivery FAILS rather than silently succeeding
            try:
                second = sink.write("{}\n")
            except Exception as e:
                self.fail("a write to a broken sink escaped: %r" % (e,))
            self.assertIs(second, False, "a broken sink reports failed delivery")
        finally:
            km._open_private, km.sys.stderr = real, err
        self.assertIn("unusable", buf.getvalue())
        # and once the path works again it recovers on its own - fail-closed must not mean fail-forever
        self.assertTrue(sink.write("{}\n"))

    def test_a_broken_sink_RECOVERS_when_the_path_becomes_usable(self):
        # Fail-closed must not mean fail-forever - the same rule as every pin in this file.
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.ndjson")
        sink = km.RotatingFileSink(p, 0, 5)
        self.addCleanup(sink.close)
        sink._fh.close()                 # close it rather than orphaning the handle
        sink._broken, sink._fh = "simulated", None
        self.assertTrue(sink.write('{"event":"new"}\n'), "it must retry and recover")
        self.assertIsNone(sink._broken)


class Loom9CursorDurabilityIsConsumedTest(unittest.TestCase):
    """Loom re-audit 9, MEDIUM. Round 8 made save() RETURN durability; the caller threw it away.

    ★ That is the audit-8 finding one layer out - fixing "nobody reads the answer" by producing an answer
    nobody reads. The harness could not see it because the mutation deleted the CALL, and the call was
    still there; only mutating the CALL SITE's use of the RESULT catches this.
    """
    E2E = BoundedWindowEndToEndTest

    class FlakyState:
        def __init__(self, answers):
            self.answers, self.calls = list(answers), 0
        def save(self, *a, **k):
            self.calls += 1
            return self.answers[min(self.calls - 1, len(self.answers) - 1)]

    def test_an_UNPROVEN_cursor_write_is_reported_once_and_self_clears(self):
        em = self.E2E.RecordingEmitter()
        t = self.E2E()._target(cursor=100, emitter=em)
        t.state_file = self.FlakyState([False, False, True])
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            self.E2E()._run(t, self.E2E()._fetch([{"id": 200}], 0), times=2)
            self.assertEqual(buf.getvalue().count("durability is UNPROVEN"), 1, "reported once, not per poll")
            self.assertTrue(t.state_not_durable)
            self.E2E()._run(t, self.E2E()._fetch([{"id": 300}], 0))     # save() now succeeds
            self.assertFalse(t.state_not_durable)
            self.assertIn("recovered", buf.getvalue())
        finally:
            km.sys.stderr = err

    def test_a_DURABLE_write_says_nothing(self):
        em = self.E2E.RecordingEmitter()
        t = self.E2E()._target(cursor=100, emitter=em)
        t.state_file = self.FlakyState([True])
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            self.E2E()._run(t, self.E2E()._fetch([{"id": 200}], 0))
        finally:
            km.sys.stderr = err
        self.assertNotIn("UNPROVEN", buf.getvalue())
        self.assertFalse(t.state_not_durable)


class Loom8DurableDirectoryEntryTest(unittest.TestCase):
    """Loom re-audit 8, HIGH 2 and HIGH 3. The NAME must be durable, and a failed sync must be REPORTED."""

    def test_a_NEW_event_file_syncs_its_DIRECTORY_before_the_cursor_can_advance(self):
        # fsync on the fd makes the BYTES durable; the NAME lives in the directory. Without this the
        # state directory can persist an advanced cursor while the event pathname is lost - and the two
        # directories can legitimately DIFFER, since --state-file and --events-file-template are
        # independent, so syncing one proves nothing about the other.
        import stat as _stat
        d = tempfile.mkdtemp()
        kinds = []
        real = km.os.fsync
        def spy(fd):
            try:
                kinds.append("dir" if _stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            except OSError:
                kinds.append("file")
            return real(fd)
        km.os.fsync = spy
        try:
            sink = km.RotatingFileSink(os.path.join(d, "events.ndjson"), 0, 5)
            self.addCleanup(sink.close)
            sink.write("{}\n")
            self.assertTrue(sink.sync())
        finally:
            km.os.fsync = real
        self.assertIn("dir", kinds, "a newly created event file needs its DIRECTORY entry synced")
        self.assertIn("file", kinds)

    def test_a_ROTATION_syncs_the_directory_again(self):
        import stat as _stat
        d = tempfile.mkdtemp()
        sink = km.RotatingFileSink(os.path.join(d, "events.ndjson"), 80, 3)
        self.addCleanup(sink.close)
        sink.write('{"pad":"%s"}\n' % ("x" * 100))     # forces a rotation
        sink.sync()
        kinds = []
        real = km.os.fsync
        def spy(fd):
            try:
                kinds.append("dir" if _stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            except OSError:
                kinds.append("file")
            return real(fd)
        km.os.fsync = spy
        try:
            sink.write('{"pad":"%s"}\n' % ("y" * 100))  # rotates again: directory entries rewritten
            self.assertTrue(sink.sync())
        finally:
            km.os.fsync = real
        self.assertIn("dir", kinds, "every rename rewrites directory entries and none is durable until synced")

    def test_a_PARTIALLY_FAILED_rotation_still_syncs_the_entries_it_DID_rewrite(self):
        # ⚠️ THE MUTATION HARNESS FOUND THIS ONE. On a normal rotation the live file is renamed away, so
        # the reopen sees a NEW file and flags the directory itself - which makes rotation's own flag look
        # redundant, and a test using a healthy rotation passes with it deleted. The case where it is NOT
        # redundant is a rotation that fails PARTWAY: the archive renames already rewrote directory
        # entries, but the live file survives, so the reopen sees an existing file and flags nothing.
        # Without rotation's own flag those rewritten entries are never made durable.
        import stat as _stat
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.ndjson")
        with open(p + ".1", "w") as f:              # an archive exists, so rotation has renames to do
            f.write("old\n")
        sink = km.RotatingFileSink(p, 60, 3)
        self.addCleanup(sink.close)
        sink.write('{"pad":"%s"}\n' % ("x" * 80))
        self.assertTrue(sink.sync())                # clear the create-time flag

        real_replace = km.os.replace
        def fail_only_the_live_rename(src, dst):
            if os.path.abspath(src) == os.path.abspath(p):
                raise OSError("simulated: archives rotated, live rename failed")
            return real_replace(src, dst)
        kinds = []
        real_fsync = km.os.fsync
        def spy(fd):
            try:
                kinds.append("dir" if _stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            except OSError:
                kinds.append("file")
            return real_fsync(fd)
        km.os.replace, km.os.fsync = fail_only_the_live_rename, spy
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            sink.write('{"pad":"%s"}\n' % ("y" * 80))   # triggers the partial rotation
            sink.sync()
        finally:
            km.os.replace, km.os.fsync, km.sys.stderr = real_replace, real_fsync, err
        self.assertTrue(os.path.exists(p), "precondition: the live file survived, so the reopen sees it")
        self.assertIn("dir", kinds,
                      "entries were rewritten before the failure and must still be made durable")

    def test_a_FAILED_directory_sync_holds_the_cursor(self):
        d = tempfile.mkdtemp()
        sink = km.RotatingFileSink(os.path.join(d, "events.ndjson"), 0, 5)
        self.addCleanup(sink.close)
        import stat as _stat
        real = km.os.fsync
        def only_dirs_fail(fd):
            if _stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("simulated directory fsync failure")
            return real(fd)
        km.os.fsync = only_dirs_fail
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            sink.write("{}\n")
            self.assertFalse(sink.sync(), "an unsynced directory entry is not durable delivery")
        finally:
            km.os.fsync, km.sys.stderr = real, err
        self.assertIn("events directory", buf.getvalue())

    # ---- HIGH 3: the RESULT must be honoured, not merely the call present ----
    def test_save_returns_TRUE_when_the_write_is_durable(self):
        d = tempfile.mkdtemp()
        self.assertIs(km.StateFile(os.path.join(d, "hive.json"), "idx").save(200, "UP", 0), True)

    def test_save_returns_FALSE_and_SAYS_SO_when_the_directory_cannot_be_synced(self):
        # ★ loom's sharpest point this round: the existing mutation proved the CALL was present, never
        # that its ANSWER was read. `_fsync_dir` returned False and save() discarded it, reporting
        # success with the cursor written and its durability merely assumed.
        import stat as _stat
        d = tempfile.mkdtemp()
        real = km.os.fsync
        def only_dirs_fail(fd):
            if _stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("simulated directory fsync failure")
            return real(fd)
        km.os.fsync = only_dirs_fail
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            ok = km.StateFile(os.path.join(d, "hive.json"), "idx").save(200, "UP", 0)
        finally:
            km.os.fsync, km.sys.stderr = real, err
        self.assertIs(ok, False, "durability must not be reported as proven when it is not")
        self.assertIn("UNPROVEN", buf.getvalue(), "and it must SAY so - silence is the actual defect")

    def test_save_returns_FALSE_when_the_write_itself_fails(self):
        sf = km.StateFile(os.path.join(tempfile.mkdtemp(), "nested-missing", "hive.json"), "idx")
        real = km.os.replace
        km.os.replace = lambda *a: (_ for _ in ()).throw(OSError("simulated replace failure"))
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            self.assertIs(sf.save(200, "UP", 0), False)
        finally:
            km.os.replace, km.sys.stderr = real, err


class Loom7StrictPersistedSchemaTest(unittest.TestCase):
    """Loom re-audit 7, HIGH 2. A malformed persisted field FAILS CLOSED - it never defaults quietly.

    `d.get("pin_forced") is True` reads a JSON `1` as False, which SILENTLY UNPINS the watermark; the
    replay cap is then free to cross the very span the pin was protecting (loom: max_replay crosses
    100 -> 202). `pin_evidence_intact` had the mirror bug (`is False`, so `0` read as intact). Both are
    the shape where a hand-edit, a jq one-liner or another language's serialiser produces the value.
    """
    def _load(self, d):
        p = os.path.join(tempfile.mkdtemp(), "hive.json")
        with open(p, "w") as f:
            json.dump(d, f)
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            return km.StateFile(p, "idx").load(), buf.getvalue()
        finally:
            km.sys.stderr = err

    def _base(self, **extra):
        d = {"identity": "idx", "cursor": 100, "state": "UP", "consecutive_failures": 0}
        d.update(extra)
        return d

    def test_a_WELL_FORMED_pin_file_still_loads(self):
        # The control first: without it every assertion below passes against a load() that rejects
        # everything, which would be its own permanent fail-closed.
        loaded, _ = self._load(self._base(pin_forced=True, state_corrupt=True,
                                          pin_evidence_intact=False, gap_alerted=99))
        self.assertIsNot(loaded, km.CORRUPT_STATE)
        self.assertTrue(loaded["pin_forced"])
        self.assertTrue(loaded["state_corrupt"])
        self.assertFalse(loaded["pin_evidence_intact"])
        self.assertEqual(loaded["gap_alerted"], 99)

    def test_LOOM7_pin_forced_as_1_is_CORRUPT_not_silently_false(self):
        loaded, warned = self._load(self._base(pin_forced=1))
        self.assertIs(loaded, km.CORRUPT_STATE)
        self.assertIn("pin_forced", warned)

    def test_LOOM7_state_corrupt_as_1_is_CORRUPT_not_silently_false(self):
        loaded, _ = self._load(self._base(state_corrupt=1))
        self.assertIs(loaded, km.CORRUPT_STATE)

    def test_LOOM7_pin_evidence_intact_as_0_is_CORRUPT_not_silently_intact(self):
        loaded, _ = self._load(self._base(pin_evidence_intact=0))
        self.assertIs(loaded, km.CORRUPT_STATE)

    def test_a_non_boolean_pin_field_of_any_shape_is_CORRUPT(self):
        for bad in ("true", 1.0, [], {}, "yes"):
            loaded, _ = self._load(self._base(pin_forced=bad))
            self.assertIs(loaded, km.CORRUPT_STATE, "pin_forced=%r must not be interpreted" % (bad,))

    def test_gap_alerted_must_be_a_REAL_integer(self):
        for bad in (True, False, "99", 9.5):
            loaded, _ = self._load(self._base(gap_alerted=bad))
            self.assertIs(loaded, km.CORRUPT_STATE, "gap_alerted=%r must not be interpreted" % (bad,))

    def test_pin_release_at_must_be_a_REAL_integer(self):
        for bad in (True, "150", 1.5):
            loaded, _ = self._load(self._base(pin_release_at=bad))
            self.assertIs(loaded, km.CORRUPT_STATE)
        loaded, _ = self._load(self._base(pin_release_at=150))
        self.assertEqual(loaded["pin_release_at"], 150)

    def test_a_BOOLEAN_in_emitted_above_is_not_a_message_id(self):
        # `true` would otherwise become the id 1 and suppress a real message 1 for the life of the pin.
        loaded, warned = self._load(self._base(emitted_above=[True, 5]))
        self.assertEqual(loaded["emitted_above"], set())     # fails closed: pin held, tracking unusable
        self.assertFalse(loaded["pin_evidence_intact"])
        self.assertIn("malformed", warned)

    def test_LOOM7_a_malformed_pin_field_cannot_let_the_replay_cap_cross_the_pin(self):
        # THE CONSEQUENCE, end to end - loom's own repro shape (max_replay crossing 100 -> 202). A
        # corrupt file arms BELOW the visible window and re-emits it; it must not baseline over it.
        d = tempfile.mkdtemp()
        base = os.path.join(d, "hive.json")
        url = "http://x/api/inbox?persona=argus"
        with open(km._state_path_for_persona(base, "argus"), "w") as f:
            json.dump({"identity": km.canonical_identity(url), "cursor": 100, "state": "UP",
                       "consecutive_failures": 0, "pin_forced": 1}, f)      # the malformed flag

        class A(BoundedWindowEndToEndTest.FullArgs):
            state_file = base
            seed_at = None
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            t = km.WatchTarget("argus", url, None, {}, A(), BoundedWindowEndToEndTest.RecordingEmitter())
        finally:
            km.sys.stderr = err
        self.addCleanup(t.state_file.unlock)
        self.assertTrue(t.pin_forced)                # not quietly unpinned
        self.assertTrue(t.state_corrupt)
        self.assertIsNone(t.cursor)                  # and NOT resumed at 100 as if the file were sound


class Loom7CaseOnlyIdentityMigrationTest(unittest.TestCase):
    """Loom re-audit 7, HIGH 3. A persona spelled differently is not a different SOURCE.

    loom's repro: the state PATH casefolds, so `Loom` and `loom` share one file - but the IDENTITY
    inside it keeps the directory's spelling, so a run that discovers `loom` compares it against a
    stored `Loom`, calls it a mismatch, reports ABSENT, and BASELINES to the newest visible id. The
    cursor is destroyed by a spelling change and 150/200 is skipped in silence.
    """
    def _url(self, persona):
        return "http://x/api/inbox?persona=%s&mark_read=false" % persona

    def test_LOOM7_a_case_only_difference_MIGRATES_and_KEEPS_the_cursor(self):
        p = os.path.join(tempfile.mkdtemp(), "hive.argus.json")
        km.StateFile(p, km.canonical_identity(self._url("Loom"))).save(150, "UP", 0)
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            loaded = km.StateFile(p, km.canonical_identity(self._url("loom"))).load()
        finally:
            km.sys.stderr = err
        self.assertIsNotNone(loaded, "a case-only difference must not read as a MISSING state file")
        self.assertEqual(loaded["cursor"], 150)      # *** the cursor survives the spelling change ***
        self.assertIn("MIGRATING", buf.getvalue())

    def test_the_migrated_identity_is_REWRITTEN_so_it_converges(self):
        p = os.path.join(tempfile.mkdtemp(), "hive.argus.json")
        km.StateFile(p, km.canonical_identity(self._url("Loom"))).save(150, "UP", 0)
        sf = km.StateFile(p, km.canonical_identity(self._url("loom")))
        sf.save(151, "UP", 0)
        with open(p) as f:
            self.assertEqual(json.load(f)["identity"], km.canonical_identity(self._url("loom")))

    def test_a_GENUINELY_different_persona_still_re_baselines(self):
        # The control that keeps the migration honest: this must NOT become "any persona resumes".
        p = os.path.join(tempfile.mkdtemp(), "hive.argus.json")
        km.StateFile(p, km.canonical_identity(self._url("river"))).save(150, "UP", 0)
        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            self.assertIsNone(km.StateFile(p, km.canonical_identity(self._url("loom"))).load())
        finally:
            km.sys.stderr = err
        self.assertIn("identity mismatch", buf.getvalue())

    def test_only_the_QUERY_VALUE_is_case_migrated_never_the_host_or_path(self):
        cur = km.canonical_identity(self._url("loom"))
        self.assertTrue(km.identity_migratable(km.canonical_identity(self._url("LOOM")), cur))
        # a different path / port / host is a different source, whatever its case
        self.assertFalse(km.identity_migratable(
            km.canonical_identity("http://x/api/INBOX?persona=loom&mark_read=false"), cur))
        self.assertFalse(km.identity_migratable(
            km.canonical_identity("http://y/api/inbox?persona=loom&mark_read=false"), cur))
        self.assertFalse(km.identity_migratable(
            km.canonical_identity("http://x:81/api/inbox?persona=loom&mark_read=false"), cur))
        # and a garbled stored identity is not migratable, it is a mismatch
        for junk in (None, "idx", [], ["http", "x", 80, "/api/inbox"], ["http", "x", 80, "/api/inbox", "q"]):
            self.assertFalse(km.identity_migratable(junk, cur), "%r must not migrate" % (junk,))

    def test_a_different_number_of_query_params_is_not_migratable(self):
        cur = km.canonical_identity(self._url("loom"))
        self.assertFalse(km.identity_migratable(
            km.canonical_identity("http://x/api/inbox?persona=loom&extra=1&mark_read=false"), cur))


class Loom7InversePaginationContradictionTest(unittest.TestCase):
    """Loom re-audit 7, HIGH 4. BOTH directions of the pagination contradiction must pin.

    `next_before_id` is set by the server EXACTLY when rows were withheld, so "I withheld nothing" and
    "there is more" cannot both be true. _uncovered_gap() never looked at the continuation at all (zero
    references), so it believed the first half and advanced over whatever the second half was pointing
    at. Verified against the live API, including the exactly-at-limit edge that could have made this
    check fire on healthy traffic - it does not.
    """
    E2E = BoundedWindowEndToEndTest

    def test_the_parse_layer_flags_BOTH_directions(self):
        forward = km.fetch_from_payload({"result": [{"id": 200}], "truncated": True,
                                         "next_before_id": None})
        self.assertFalse(forward.consistent)         # withheld rows + "nothing older"
        inverse = km.fetch_from_payload({"result": [{"id": 200}], "next_before_id": 200})
        self.assertFalse(inverse.consistent)         # withheld nothing + "there is more"

    def test_the_WELL_FORMED_pairings_stay_consistent(self):
        # Controls. Without these the rule could be satisfied by marking every page inconsistent, which
        # would pin the watcher permanently on healthy traffic - a worse bug than the one being fixed.
        complete = km.fetch_from_payload({"result": [{"id": 200}], "next_before_id": None})
        self.assertTrue(complete.consistent)
        withheld = km.fetch_from_payload({"result": [{"id": 200}], "size_dropped": 3,
                                          "next_before_id": 200})
        self.assertTrue(withheld.consistent)
        empty = km.fetch_from_payload({"result": [], "next_before_id": None})
        self.assertTrue(empty.consistent)
        # a lone oversized message clips a BODY without withholding a ROW, so it is not an omission
        clipped = km.fetch_from_payload({"result": [{"id": 200}], "size_truncated": True,
                                         "size_dropped": 0, "next_before_id": None})
        self.assertTrue(clipped.consistent)

    def test_LOOM7_the_inverse_contradiction_is_an_UNCOVERED_GAP(self):
        t = BoundedWindowGapTest()._target(cursor=100)
        poll = km.fetch_from_payload({"result": [{"id": 200}], "next_before_id": 200})
        gap = t._uncovered_gap(poll, poll.items)
        self.assertIsNotNone(gap, "a window contradicting itself must not read as 'nothing omitted'")
        self.assertEqual(gap[0], 100)                # pinned at the watermark
        self.assertFalse(gap[3], "the omission has no stated quantity, so it is INEXACT")

    def test_LOOM7_the_inverse_contradiction_PINS_instead_of_advancing(self):
        em = self.E2E.RecordingEmitter()
        t = self.E2E()._target(cursor=100, emitter=em)

        def f(opener, url, headers):
            if "before_id=" in url:
                return km.Poll(False, reason="http 502")     # the walk cannot prove the span either
            return km.fetch_from_payload({"result": [{"id": 200}], "next_before_id": 200})
        self.E2E()._run(t, f)
        self.assertEqual(em.new_ids, [200])          # visible mail still flows...
        self.assertEqual(t.cursor, 100)              # ...but the watermark does NOT step over the span
        self.assertTrue([x for e, x in em.events if e == "alert"])

    def test_a_window_that_did_not_ANSWER_cannot_assert_it_withheld_nothing(self):
        # ★ FOUND BY RE-READING ROUND 7 ADVERSARIALLY, and it is the audit-5 HIGH 1 rule one layer over:
        # the WALK has refused to read an absent/malformed continuation as exhaustion since round 5, but
        # the GAP CHECK consulted only `omitted`. So a server that garbled `next_before_id` while
        # declaring no omission advanced the watermark over whatever it was hiding - silently, no alert.
        # The two fields are ONE statement; half of it being unreadable makes the other half unusable.
        for label, payload in (
            ("malformed", {"result": [{"id": 200}], "next_before_id": "abc"}),
            ("absent", {"result": [{"id": 200}]}),
            ("non-integral", {"result": [{"id": 200}], "next_before_id": 1.5}),
        ):
            poll = km.fetch_from_payload(payload)
            self.assertFalse(poll.continuation_ok, label)
            t = BoundedWindowGapTest()._target(cursor=100)
            gap = t._uncovered_gap(poll, poll.items)
            self.assertIsNotNone(gap, "a %s continuation must not read as 'nothing was omitted'" % label)
            self.assertFalse(gap[3], "there is no stated quantity, so the omission is INEXACT")
        # the control: a window that DID answer, and answered "nothing older", still advances freely
        ok = km.fetch_from_payload({"result": [{"id": 200}], "next_before_id": None})
        self.assertTrue(ok.continuation_ok)
        self.assertIsNone(BoundedWindowGapTest()._target(cursor=100)._uncovered_gap(ok, ok.items))

    def test_a_window_that_did_not_answer_cannot_RELEASE_a_pin_either(self):
        em = self.E2E.RecordingEmitter()
        t = self.E2E()._target(cursor=100, emitter=em)
        t.pin_evidence_intact, t.pin_forced = False, True
        orig, km.fetch = km.fetch, lambda o, u, h: km.fetch_from_payload(
            {"result": [{"id": 90}, {"id": 100}, {"id": 120}], "next_before_id": "abc"})
        try:
            t.poll_once()
        finally:
            km.fetch = orig
        self.assertTrue(t.pin_forced, "a window that did not answer cannot be the proof that nothing is hidden")

    def test_a_walk_page_with_the_inverse_contradiction_is_NOT_coverage(self):
        t = WalkBackTest()._target(cursor=100)

        def f(opener, url, headers):
            return km.fetch_from_payload({"result": [{"id": 150}], "next_before_id": 150})
        orig, km.fetch = km.fetch, f
        try:
            rows, covered = t._walk_back(200, 100)
        finally:
            km.fetch = orig
        self.assertFalse(covered)

    def test_a_contradictory_window_cannot_RELEASE_a_forced_pin_either(self):
        # The same page must not be accepted as the "complete window" proof on the other branch.
        em = self.E2E.RecordingEmitter()
        t = self.E2E()._target(cursor=100, emitter=em)
        t.pin_evidence_intact, t.pin_forced = False, True
        orig, km.fetch = km.fetch, lambda o, u, h: km.fetch_from_payload(
            {"result": [{"id": 90}, {"id": 100}, {"id": 120}], "next_before_id": 90})
        try:
            t.poll_once()
        finally:
            km.fetch = orig
        self.assertTrue(t.pin_forced)


class Loom7CorruptionPinReleaseTest(unittest.TestCase):
    """Loom re-audit 7, HIGH 5. A pin that can NEVER clear is the same bug facing the other way.

    A corrupt-state arm parks the watermark at min(visible)-1 so it can re-emit the whole window. The
    release test asked for a complete window reaching back to at-or-below the watermark - but that
    window's floor IS min(visible), which is always cursor+1. So the pin never cleared, the cursor never
    moved, and because a non-pinned poll recorded no delivered ids, the identical window was re-emitted
    on every poll and across every restart (loom: repeats 150/200 forever). This was a regression from
    my own round-6 fix: I repaired a fail-OPEN by building a permanent fail-CLOSED.
    """
    E2E = BoundedWindowEndToEndTest

    def _corrupt_target(self, emitter):
        t = self.E2E()._target(cursor=None, emitter=emitter)
        t.armed = False
        t.state_corrupt, t.pin_forced, t.pin_evidence_intact = True, True, False
        return t

    def test_LOOM7_the_corruption_pin_RELEASES_on_the_window_it_re_emitted(self):
        em = self.E2E.RecordingEmitter()
        t = self._corrupt_target(em)
        self.E2E()._run(t, self.E2E()._fetch([{"id": 150}, {"id": 200}], 0))
        self.assertEqual(em.new_ids, [150, 200])     # the window is re-emitted, not baselined over
        self.assertFalse(t.pin_forced, "the pin must be dischargeable by the window that armed it")
        self.assertEqual(t.cursor, 200)

    def test_LOOM7_the_corruption_pin_does_NOT_re_emit_the_window_forever(self):
        # loom's repro verbatim: "repeats 150/200 forever across polls AND restarts".
        em = self.E2E.RecordingEmitter()
        t = self._corrupt_target(em)
        self.E2E()._run(t, self.E2E()._fetch([{"id": 150}, {"id": 200}], 0), times=5)
        self.assertEqual(em.new_ids, [150, 200])     # ONCE across five polls, not five times

    def test_a_pin_that_cannot_be_discharged_still_tracks_what_it_delivered(self):
        # The second half of the defect: even while the watermark legitimately cannot move, delivered
        # ids must be remembered or every later poll re-delivers them.
        em = self.E2E.RecordingEmitter()
        t = self._corrupt_target(em)
        # an INCOMPLETE window cannot discharge the pin, and the walk cannot prove the span
        self.E2E()._run(t, self.E2E()._fetch([{"id": 150}, {"id": 200}], 2, walk_fail=True), times=4)
        self.assertEqual(em.new_ids, [150, 200])     # delivered once...
        self.assertTrue(t.pin_forced)                # ...while the pin correctly still holds
        self.assertEqual(t.emitted_above, {150, 200})

    def test_an_INCOMPLETE_window_does_NOT_discharge_the_corruption_pin(self):
        # The control: the release must require real evidence, not merely "a window happened".
        em = self.E2E.RecordingEmitter()
        t = self._corrupt_target(em)
        self.E2E()._run(t, self.E2E()._fetch([{"id": 150}, {"id": 200}], 2, walk_fail=True))
        self.assertTrue(t.pin_forced)
        self.assertEqual(t.cursor, 149)              # still parked below the window

    def test_the_release_floor_is_PERSISTED_so_a_RESTART_can_still_clear_it(self):
        # A release condition that dies on restart is not a release condition - which is exactly how the
        # round-6 pin came to be un-clearable in the first place.
        d = tempfile.mkdtemp()
        base = os.path.join(d, "hive.json")
        url = "http://x/api/inbox?persona=argus"
        km.StateFile(km._state_path_for_persona(base, "argus"), km.canonical_identity(url)).save(
            149, "UP", 0, pin_forced=True, pin_evidence_intact=False, state_corrupt=True,
            pin_release_at=150)

        class A(BoundedWindowEndToEndTest.FullArgs):
            state_file = base
            seed_at = None
        t = km.WatchTarget("argus", url, None, {}, A(), self.E2E.RecordingEmitter())
        self.addCleanup(t.state_file.unlock)
        self.assertEqual(t.pin_release_at, 150)
        self.assertTrue(t.pin_forced)
        self.E2E()._run(t, self.E2E()._fetch([{"id": 150}, {"id": 200}], 0))
        self.assertFalse(t.pin_forced)               # the restart can discharge it too
        self.assertEqual(t.cursor, 200)

    def test_the_release_floor_never_LOOSENS_an_ordinary_pin(self):
        # The floor is a max(), so an ordinary forced pin (no corruption, no recorded floor) still
        # demands a window reaching back past the watermark - unchanged from before.
        em = self.E2E.RecordingEmitter()
        t = self.E2E()._target(cursor=100, emitter=em)
        t.pin_evidence_intact, t.pin_forced = False, True
        self.E2E()._run(t, self.E2E()._fetch([{"id": 120}, {"id": 150}], 0))
        self.assertTrue(t.pin_forced, "a window that does not reach the watermark proves nothing")
        self.assertEqual(t.cursor, 100)

    def test_a_HELD_pin_with_NO_GAP_still_remembers_what_it_delivered(self):
        # ★ THE CASE THE MUTATION HARNESS FOUND, and it is WIDER than the corruption pin. Whenever the
        # watermark cannot advance while mail above it is still being delivered, the delivered ids must
        # be recorded - and this shape reaches it without any corrupt state at all: an ordinary forced
        # pin (tracking lost) plus a COMPLETE window that simply does not reach back past the watermark.
        # The pin correctly holds, the cursor correctly freezes, and with tracking gated on `pinned`
        # nothing is recorded, so the same two messages are re-delivered on every poll forever. The
        # corruption pin was one instance of that class; the fix belongs at the class.
        em = self.E2E.RecordingEmitter()
        t = self.E2E()._target(cursor=100, emitter=em)
        t.pin_evidence_intact, t.pin_forced = False, True
        self.E2E()._run(t, self.E2E()._fetch([{"id": 120}, {"id": 150}], 0), times=3)
        self.assertEqual(em.new_ids, [120, 150])     # ONCE across three polls, not three times
        self.assertEqual(t.emitted_above, {120, 150})
        self.assertEqual(t.cursor, 100)              # while the pin still legitimately holds
        self.assertTrue(t.pin_forced)

    def test_a_pin_is_NOT_discharged_by_a_poll_that_could_not_DELIVER(self):
        # ★ FOUND BY ADVERSARIALLY RE-READING MY OWN ROUND-7 WORK. Both release proofs answer "did the
        # SERVER withhold anything" - neither says anything about whether WE handed the window over.
        # Releasing on a poll whose delivery failed threw away the release floor AND state_corrupt while
        # the watermark was still parked below the entire mailbox.
        em = self.E2E.RecordingEmitter(fail_ids={150})
        t = self._corrupt_target(em)
        self.E2E()._run(t, self.E2E()._fetch([{"id": 150}, {"id": 200}], 0))
        self.assertTrue(t.pin_forced, "a pin must not be discharged while we still owe someone mail")
        self.assertEqual(t.pin_release_at, 150, "the release floor must survive a blocked delivery")
        self.assertTrue(t.state_corrupt)
        self.assertEqual(t.cursor, 149)
        # ...and it self-clears the moment delivery recovers, so this is not a new permanent pin
        em.fail_ids.clear()
        self.E2E()._run(t, self.E2E()._fetch([{"id": 150}, {"id": 200}], 0))
        self.assertFalse(t.pin_forced)
        self.assertEqual(t.cursor, 200)
        self.assertEqual(em.new_ids, [150, 200])

    def test_the_WALK_proof_is_also_gated_on_delivery_not_only_the_window_proof(self):
        # ⚠️ THE TWO PROOFS NEED SEPARATE TESTS. The test above reaches the release through the
        # COMPLETE-WINDOW path (no gap, so `release_earned` is never even set), which left the WALK
        # path's gate defending nothing - the mutation harness caught exactly that. This shape has a
        # real gap, a walk that COMPLETES (so the walk proof is earned), and a delivery that fails.
        em = self.E2E.RecordingEmitter(fail_ids={150})
        t = self._corrupt_target(em)
        self.E2E()._run(t, self.E2E()._fetch([{"id": 150}, {"id": 200}], 1,
                                             walk={150: ([{"id": 149}], None)}))
        self.assertTrue(t.pin_forced, "a COMPLETED WALK must not discharge a pin we could not deliver")
        self.assertEqual(t.pin_release_at, 150)
        em.fail_ids.clear()
        self.E2E()._run(t, self.E2E()._fetch([{"id": 150}, {"id": 200}], 1,
                                             walk={150: ([{"id": 149}], None)}))
        self.assertFalse(t.pin_forced, "and it must clear once delivery recovers")

    def test_LOOM7_a_blocked_delivery_does_not_leave_a_pin_that_can_NEVER_clear(self):
        # THE FULL CHAIN, end to end and across a RESTART, because the harm only became visible there:
        # release-while-blocked discarded the floor; the restart re-forced the pin from the surviving
        # pin_evidence_intact=False, now with NO floor; and `reach <= cursor` is unsatisfiable when the
        # cursor sits below the oldest message that exists. Measured before the fix: the watermark froze
        # below the whole mailbox permanently, leaving emitted_above as the only thing preventing
        # re-delivery - i.e. correctness resting on an unbounded set.
        d = tempfile.mkdtemp()
        base = os.path.join(d, "hive.json")
        url = "http://x/api/inbox?persona=argus"
        with open(km._state_path_for_persona(base, "argus"), "w") as f:
            f.write("{garbled")

        class A(BoundedWindowEndToEndTest.FullArgs):
            state_file = base
            seed_at = None
            max_replay = 5
        window = [{"id": 100 + i} for i in range(20)]

        def poll(target, times=1):
            orig, km.fetch = km.fetch, (lambda o, u, h: server_page(window, omitted=0))
            try:
                for _ in range(times):
                    target.poll_once()
            finally:
                km.fetch = orig

        buf, err = __import__("io").StringIO(), km.sys.stderr
        km.sys.stderr = buf
        try:
            em = self.E2E.RecordingEmitter(fail_ids={100})       # delivery broken on the arming poll
            t = km.WatchTarget("argus", url, None, {}, A(), em)
            poll(t)
            t.state_file.unlock()
            saved = json.load(open(km._state_path_for_persona(base, "argus")))
            self.assertEqual(saved.get("pin_release_at"), 100, "the floor must be persisted, not discarded")
            self.assertTrue(saved.get("pin_forced"))

            em2 = self.E2E.RecordingEmitter()                     # RESTART, delivery healthy again
            t2 = km.WatchTarget("argus", url, None, {}, A(), em2)
            self.addCleanup(t2.state_file.unlock)
            self.assertEqual(t2.pin_release_at, 100)
            poll(t2, 3)
        finally:
            km.sys.stderr = err
        self.assertFalse(t2.pin_forced, "the pin must clear once delivery recovers")
        self.assertEqual(t2.cursor, 119, "the watermark must reach the window max, not freeze below it")
        self.assertEqual(sorted(set(em2.new_ids)), [m["id"] for m in window])
        self.assertEqual(t2.emitted_above, set(), "nothing should still need per-id tracking")

    def test_BOTH_release_paths_clear_the_state_that_was_holding_the_pin(self):
        # ★ FOUND BY RUNNING THE REPRO AGAINST THE LIVE API AND READING WHAT WAS ACTUALLY WRITTEN BACK.
        # Every unit test here asserted `pin_forced` was False and stopped there, so nothing noticed that
        # the WALK path left `state_corrupt: true` and a stale `pin_release_at` persisted forever - a
        # state file still describing a corruption that had been fully recovered. Two proofs of the same
        # fact must leave identical state, which is why they now share one _release_pin().
        for label, fetch in (
            ("complete-window proof", self.E2E()._fetch([{"id": 150}, {"id": 200}], 0)),
            ("completed-walk proof", self.E2E()._fetch([{"id": 150}, {"id": 200}], 1,
                                                       walk={150: ([{"id": 149}], None)})),
        ):
            em = self.E2E.RecordingEmitter()
            t = self._corrupt_target(em)
            self.E2E()._run(t, fetch)
            self.assertFalse(t.pin_forced, label)
            self.assertIsNone(t.pin_release_at, "%s left a stale release floor" % label)
            self.assertFalse(t.state_corrupt, "%s left state_corrupt asserted after recovery" % label)

    def test_an_EMPTY_first_window_does_not_strand_the_pin_without_a_floor(self):
        # The edge the floor could have missed: nothing visible at arming time means no floor to record,
        # and leaving it unset would freeze the watermark exactly as before, one poll further on.
        em = self.E2E.RecordingEmitter()
        t = self._corrupt_target(em)
        self.E2E()._run(t, self.E2E()._fetch([], 0))
        self.assertIsNone(t.pin_release_at)
        self.E2E()._run(t, self.E2E()._fetch([{"id": 150}, {"id": 200}], 0))
        self.assertEqual(em.new_ids, [150, 200])
        self.assertFalse(t.pin_forced)
        self.assertEqual(t.cursor, 200)


class Loom7StateFileHygieneTest(unittest.TestCase):
    """Loom re-audit 7, item 7. The lock fd was never closed - two ResourceWarnings, and a real leak."""

    def test_unlock_RELEASES_the_single_writer_lock(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "hive.json")
        a = km.StateFile(p, "idx")
        a.lock()
        b = km.StateFile(p, "idx")
        with self.assertRaises(km.FatalConfig):
            b.lock()                                  # control: the lock really is exclusive
        a.unlock()
        try:
            b.lock()                                  # and unlock really releases it
        except km.FatalConfig:
            self.fail("unlock() did not release the lock")
        # ⚠️ `self.fail`, not a bare call that raises: a test that passes by ERRORING is weaker than one
        # that FAILS, and the mutation gate now refuses to count an error-only mutant as caught. This
        # test was one of the two loom found being credited on errors alone.
        self.addCleanup(b.unlock)

    def test_unlock_is_idempotent_and_safe_on_an_unlocked_file(self):
        sf = km.StateFile(os.path.join(tempfile.mkdtemp(), "hive.json"), "idx")
        sf.unlock()
        sf.lock()
        sf.unlock()
        sf.unlock()


class Loom11AlarmDeliveryTest(unittest.TestCase):
    """Re-audit 11, F1/F2 - AN ALARM MUST NOT RECORD ITSELF AS RAISED UNLESS IT WAS DELIVERED.

    Every alarm used to commit its "already alarmed" state BEFORE emitting and discard the emit's
    answer, so an alarm that was never delivered was never re-raised - not after the channel recovered
    and not after a restart (`gap_alerted` is persisted). Mail was never at risk; the ALARMS vanished.
    F2 is why it stayed invisible: the liveness alert had NO test at all, and deleting it outright left
    all 242 tests green.
    """

    class Recorder:
        """Emitter double whose lifecycle delivery can be switched off mid-test."""
        def __init__(self, deliver=True):
            self.deliver, self.events, self.new_ids = deliver, [], []

        def new(self, m):
            self.new_ids.append(m["id"])
            return True

        def lifecycle(self, event, **f):
            self.events.append((event, f))
            return self.deliver          # False = the sink refused it, exactly like a broken sink

        def sync(self, persona=None):
            return True

    def _target(self, emitter, cursor=100, fsm="UP"):
        t = km.WatchTarget.__new__(km.WatchTarget)
        t.persona, t.url, t.headers = "argus", "http://x/api/inbox?persona=argus", {}
        t.opener, t.emitter = None, emitter
        t.args = BoundedWindowEndToEndTest.FullArgs()
        t.cursor, t.armed, t.fsm_state, t.failures = cursor, True, fsm, 0
        t.state_file = t.last_unread = None
        t.fast_path = False
        t.skips = t.first_poll = 0
        t.last_heartbeat = km._monotonic()
        t.count_url, t.unread_persona = km.NOTIFY_PENDING_URL, "argus"
        t.emitted_above, t.gap_alerted = set(), None
        t.pin_evidence_intact, t.pin_forced = True, False
        t.state_corrupt = t.delivery_blocked = t.state_not_durable = False
        t.pin_release_at = None
        return t

    def _run(self, t, fetch_fn, times=1):
        orig, km.fetch = km.fetch, fetch_fn
        try:
            for _ in range(times):
                t.poll_once()
        finally:
            km.fetch = orig

    @staticmethod
    def _down_fetch(reason="http 502"):
        return lambda opener, url, headers: km.Poll(False, reason=reason)

    # ---- F2: the dead-man's switch had NO test. These are it. --------------------------------------
    def test_the_liveness_DOWN_alert_IS_EMITTED_after_alert_after_failures(self):
        # THE GAP A1 EXPOSED: deleting this alert entirely left all 242 tests green, because nothing
        # asserted the one event README sells as the dead-man's switch.
        em = self.Recorder()
        t = self._target(em)
        self._run(t, self._down_fetch(), times=t.args.alert_after)
        alerts = [f for e, f in em.events if e == "alert"]
        self.assertEqual(len(alerts), 1, "the source went down and no alert was emitted")
        self.assertEqual(alerts[0]["reason"], "http 502")
        self.assertEqual(alerts[0]["consecutive_failures"], t.args.alert_after)
        self.assertEqual(t.fsm_state, "DOWN")

    def test_the_DOWN_alert_does_not_fire_before_the_threshold(self):
        em = self.Recorder()
        t = self._target(em)
        self._run(t, self._down_fetch(), times=t.args.alert_after - 1)
        self.assertEqual([f for e, f in em.events if e == "alert"], [])
        self.assertEqual(t.fsm_state, "UP")

    def test_an_UNDELIVERED_DOWN_alert_still_reaches_stderr(self):
        # The FSM transition must commit (the firing condition is an EQUALITY on `failures`, so the
        # edge is crossed exactly once and a reverted transition would never re-fire). So the
        # announcement gets the guaranteed second channel instead.
        em = self.Recorder(deliver=False)
        t = self._target(em)
        buf = _capture_stderr(self)
        self._run(t, self._down_fetch(), times=t.args.alert_after)
        self.assertEqual(t.fsm_state, "DOWN")
        self.assertIn("UNDELIVERED", buf.getvalue())
        self.assertIn("http 502", buf.getvalue())

    # ---- my own finding: the RECOVERY edge fails the same way, facing the other way ----------------
    def test_the_RECOVERED_event_is_emitted_when_the_source_comes_back(self):
        em = self.Recorder()
        t = self._target(em, fsm="DOWN")
        self._run(t, BoundedWindowEndToEndTest()._fetch([{"id": 101}], 0))
        self.assertEqual([e for e, f in em.events if e == "recovered"], ["recovered"])
        self.assertEqual(t.fsm_state, "UP")

    def test_an_UNDELIVERED_RECOVERED_event_still_reaches_stderr(self):
        # Symmetric to the DOWN case and arguably worse: a consumer that saw the alert is left holding
        # an alarm it can NEVER clear, because this edge is also crossed exactly once.
        em = self.Recorder(deliver=False)
        t = self._target(em, fsm="DOWN")
        buf = _capture_stderr(self)
        self._run(t, BoundedWindowEndToEndTest()._fetch([{"id": 101}], 0))
        self.assertEqual(t.fsm_state, "UP")
        self.assertIn("UNDELIVERED", buf.getvalue())
        self.assertIn("recovered", buf.getvalue().lower())

    def test_the_FAST_PATH_recovery_edge_also_reports_an_undelivered_event(self):
        # THE TWIN. There are TWO recovery edges - the fast-path skip and the full-poll path - and
        # fixing only the one the first test happened to exercise would be the exact trap of "my fix
        # for the last finding was the next finding". A mutation on this site survived until this test.
        em = self.Recorder(deliver=False)
        t = self._target(em, fsm="DOWN")
        t.args.no_fast_path = False
        t.fast_path, t.last_unread = True, 5
        buf = _capture_stderr(self)
        t.poll_once(counts_available=True, unread_counts={"argus": 5})   # no increase -> skip_full
        self.assertEqual(t.fsm_state, "UP")
        self.assertIn("UNDELIVERED", buf.getvalue())
        self.assertEqual([e for e, f in em.events if e == "recovered"], ["recovered"])

    # ---- F1 core: a pure announcement latch must not commit on a failed delivery -------------------
    def test_an_UNDELIVERED_gap_alarm_does_NOT_latch_and_RE_RAISES_next_poll(self):
        em = self.Recorder(deliver=False)
        t = self._target(em, cursor=1100)
        fetch = BoundedWindowEndToEndTest()._fetch([{"id": 1200}], 4, walk_fail=True)
        buf = _capture_stderr(self)
        self._run(t, fetch)
        self.assertIn("UNDELIVERED", buf.getvalue(), "and it must still reach stderr")
        self.assertIsNone(t.gap_alerted, "an alarm nobody received must not be recorded as raised")
        first = len([e for e, f in em.events if e == "alert"])
        self.assertEqual(first, 1)
        em.deliver = True                      # the sink recovers; the gap is still open
        self._run(t, fetch)
        self.assertEqual(len([e for e, f in em.events if e == "alert"]), first + 1,
                         "the alarm must be RE-RAISED once the channel recovers")
        self.assertEqual(t.gap_alerted, 1100, "and only now may it latch")

    def test_a_DELIVERED_gap_alarm_latches_and_does_not_spam(self):
        # The positive control for the test above: without this, a latch that never commits would look
        # identical to a working one.
        em = self.Recorder()
        t = self._target(em, cursor=1100)
        fetch = BoundedWindowEndToEndTest()._fetch([{"id": 1200}], 4, walk_fail=True)
        self._run(t, fetch, times=3)
        self.assertEqual(t.gap_alerted, 1100)
        self.assertEqual(len([e for e, f in em.events if e == "alert"]), 1,
                         "a delivered alarm must be announced exactly once per unresolved span")

    # ---- F3: the startup path must dedupe case-variants like rediscovery does ----------------------
    def test_requested_personas_DEDUPES_CASE_VARIANTS(self):
        # Two spellings casefold to ONE state path, so the second flock raises FatalConfig out of the
        # uncaught list comprehension in run() and the producer refuses to start FOR EVERY persona.
        args = km.build_parser().parse_args(["--persona", "Loom", "--persona", "loom"])
        got = km.requested_personas(args, None, {})
        self.assertEqual(got, ["Loom"], "case-variants share one state file; keep the first spelling")
        self.assertEqual(len({km._state_safe_persona(p) for p in got}), len(got),
                         "no two watched personas may resolve to the same state path")


def _capture_stderr(test):
    """Swap stderr for a buffer for the duration of ONE test, and return it."""
    buf, err = __import__("io").StringIO(), km.sys.stderr
    km.sys.stderr = buf
    test.addCleanup(lambda: setattr(km.sys, "stderr", err))
    return buf


class Loom10ClassSweepTest(unittest.TestCase):
    """Loom re-audit 10 - the CLASS, swept rather than patched one finding at a time.

    "Safety repair checks are locally correct but their RESULT/LIFECYCLE is not propagated end-to-end."
    Two halves: WHO CONSUMES THIS (an answer nobody reads) and WHAT CLEARS THIS (a state nothing releases).
    Every test here asserts the half that a call-deletion mutation cannot reach - that the ANSWER is read
    and that the STATE is released - because a test proving the call is present proved exactly that and no
    more, which is how the same defect survived nine rounds.
    """

    # ---- half A: the answer is CONSUMED -------------------------------------------------------------
    def test_M3_self_test_reports_FAIL_when_the_emitter_REFUSES_the_write(self):
        # THE SELF-TEST MUST NOT LIE. emit() reports a failed delivery by RETURNING False - its normal,
        # documented path - so a self-test that only catches exceptions reports emit=OK against a sink
        # that just refused the mail. The surface whose whole job is "is this tool working?" was itself
        # an instance of the class.
        d = tempfile.mkdtemp()
        os.symlink(os.path.join(d, "nowhere.txt"), os.path.join(d, "events.argus.ndjson"))
        em = km.Emitter("stdout-jsonl", None, 220, False,
                        sink_template=os.path.join(d, "events.{persona}.ndjson"))
        self.addCleanup(em.close)
        # WatchTarget.__init__ builds openers and state files; self_test needs only these five attributes.
        wt = object.__new__(km.WatchTarget)
        wt.opener, wt.url, wt.headers, wt.persona, wt.emitter = None, "https://x/api/inbox", {}, "argus", em
        real_fetch = km.fetch
        km.fetch = lambda *a, **k: km.Poll(True, [])          # the SOURCE is healthy; only the SINK is not
        self.addCleanup(lambda: setattr(km, "fetch", real_fetch))
        buf = _capture_stderr(self)
        ok = wt.self_test()
        self.assertIs(ok, False, "a self-test whose emit was REFUSED must not report success")
        self.assertIn("emit=FAIL", buf.getvalue())
        self.assertNotIn("emit=OK", buf.getvalue())

    def test_H1_an_unrepairable_state_file_is_marked_unsafe_rather_than_used(self):
        # The verdict of _repair_mode() at the lock site was DISCARDED. A symlink at the state path is the
        # real, reachable form of "cannot be proven private": _repair_mode opens O_NOFOLLOW, gets ELOOP,
        # and answers False.
        d = tempfile.mkdtemp()
        p = os.path.join(d, "hive.json")
        os.symlink(os.path.join(d, "elsewhere.json"), p)
        sf = km.StateFile(p, "idx")
        _capture_stderr(self)
        sf.lock()
        self.addCleanup(sf.unlock)
        self.assertIs(sf.unsafe, True, "_repair_mode answered False and the answer must be CONSUMED")

    def test_H1_an_unsafe_state_file_fails_CLOSED_instead_of_being_trusted(self):
        # A file we cannot prove private must not supply a cursor - whoever controls the cursor controls
        # which mail counts as delivered, and a cursor moved forward is silent, permanent loss.
        # _repair_mode is stubbed because the portable ways to make a REAL file untightenable need another
        # uid; the condition itself is entirely reachable (a file owned by someone else at our state path).
        # The file on disk is deliberately VALID and loadable, so the assertion can only pass because the
        # unsafe verdict was consumed - never because the read failed anyway.
        d = tempfile.mkdtemp()
        p = os.path.join(d, "hive.json")
        km.StateFile(p, "idx").save(200, "UP", 0)
        self.assertIsInstance(km.StateFile(p, "idx").load(), dict, "precondition: this file IS loadable")
        real = km._repair_mode
        km._repair_mode = lambda path: False
        self.addCleanup(lambda: setattr(km, "_repair_mode", real))
        sf = km.StateFile(p, "idx")
        _capture_stderr(self)
        sf.lock()
        self.addCleanup(sf.unlock)
        self.assertIs(sf.load(), km.CORRUPT_STATE,
                      "an unprovable state file must fail closed and re-emit, never resume")

    def test_H1_the_state_file_READ_path_does_not_follow_a_SYMLINK(self):
        # The WRITE path got O_NOFOLLOW in re-audit 9; the READ path was left following links, so a link
        # planted at the state path was read as our own state. The target here is a genuinely valid,
        # identity-matching state file - so if the read followed the link it would RESUME from it, and
        # this test would be the only thing standing between that and a forged cursor.
        d = tempfile.mkdtemp()
        target = os.path.join(d, "attacker.json")
        km.StateFile(target, "idx").save(999, "UP", 0)
        self.assertIsInstance(km.StateFile(target, "idx").load(), dict, "precondition: target is valid")
        p = os.path.join(d, "hive.json")
        os.symlink(target, p)
        sf = km.StateFile(p, "idx")            # note: no lock(), so `unsafe` is False and cannot mask this
        self.assertIs(sf.unsafe, False)
        _capture_stderr(self)
        self.assertIs(sf.load(), km.CORRUPT_STATE,
                      "a symlink at the state path must be refused, not resumed from")

    def test_H1_a_genuinely_ABSENT_state_file_still_baselines(self):
        # The fail-closed path must not swallow the ordinary first-launch case: absent is still None.
        sf = km.StateFile(os.path.join(tempfile.mkdtemp(), "hive.json"), "idx")
        self.assertIsNone(sf.load(), "a first launch must still baseline, not re-emit")

    # ---- half B: the state is RELEASED --------------------------------------------------------------
    def test_H2_a_refused_persona_sink_RECOVERS_once_the_path_is_usable(self):
        # A refusal cached with no release meant the operator removed the hostile symlink and mail was
        # still held, with nothing left to fix, for the life of the process. A permanent fail-closed is
        # the same defect as a fail-open, facing the other way.
        d = tempfile.mkdtemp()
        link = os.path.join(d, "events.argus.ndjson")
        os.symlink(os.path.join(d, "nowhere.txt"), link)
        em = km.Emitter("stdout-jsonl", None, 220, False,
                        sink_template=os.path.join(d, "events.{persona}.ndjson"))
        self.addCleanup(em.close)
        clock = [1000.0]
        real = km._monotonic
        km._monotonic = lambda: clock[0]
        self.addCleanup(lambda: setattr(km, "_monotonic", real))
        buf = _capture_stderr(self)
        km.sys.stdout, out = __import__("io").StringIO(), km.sys.stdout
        self.addCleanup(lambda: setattr(km.sys, "stdout", out))
        self.assertIs(em.new({"id": 1, "from": "r", "_persona": "argus"}), False, "refused while hostile")
        os.unlink(link)                                   # the operator fixes the path
        clock[0] += km.BROKEN_SINK_RETRY_S + 1            # and the cooldown expires
        self.assertIs(em.new({"id": 2, "from": "r", "_persona": "argus"}), True,
                      "the refusal must RELEASE once the condition that caused it is gone")
        self.assertTrue(em.sync("argus"), "and its events are durable again")
        self.assertIn("recovered", buf.getvalue(), "a recovery nobody can see is still invisibility")

    def test_H2_a_recovered_sink_RE_ARMS_its_suppressed_warning(self):
        # warn-once is itself a state that is set and never cleared. Left unreleased, a persona that
        # broke, recovered, then broke AGAIN would be suppressed forever - the second outage arriving
        # with no diagnostic at all. Fixing one half of the class must not create a new instance of it.
        d = tempfile.mkdtemp()
        link = os.path.join(d, "events.argus.ndjson")
        os.symlink(os.path.join(d, "nowhere.txt"), link)
        em = km.Emitter("stdout-jsonl", None, 220, False,
                        sink_template=os.path.join(d, "events.{persona}.ndjson"))
        self.addCleanup(em.close)
        km._WARNED_PERSONAS.discard("argus")
        self.addCleanup(lambda: km._WARNED_PERSONAS.discard("argus"))
        clock = [1000.0]
        real = km._monotonic
        km._monotonic = lambda: clock[0]
        self.addCleanup(lambda: setattr(km, "_monotonic", real))
        _capture_stderr(self)
        km.sys.stdout, out = __import__("io").StringIO(), km.sys.stdout
        self.addCleanup(lambda: setattr(km.sys, "stdout", out))
        em.new({"id": 1, "from": "r", "_persona": "argus"})
        self.assertIn("argus", km._WARNED_PERSONAS, "precondition: the warning was suppressed")
        os.unlink(link)
        clock[0] += km.BROKEN_SINK_RETRY_S + 1
        em.new({"id": 2, "from": "r", "_persona": "argus"})
        self.assertNotIn("argus", km._WARNED_PERSONAS,
                         "a recovered persona must be able to warn again if it breaks a second time")

    # ---- the shapes loom's one-sentence class does NOT cover ----------------------------------------
    def test_M4_an_archive_left_by_a_LARGER_former_retention_is_still_repaired(self):
        # The repair loop's range came from CURRENT config, so shrinking keep from 10 to 5 stranded .7 at
        # 0644 permanently. A bound taken from config cannot reach what a former config wrote.
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.ndjson")
        for suffix in (".1", ".7"):
            with open(p + suffix, "w") as f:
                f.write("old mail\n")
            os.chmod(p + suffix, 0o644)
        _capture_stderr(self)
        km.RotatingFileSink(p, 0, 5).close()              # keep=5 - .7 is BEYOND the old range
        self.assertEqual(os.stat(p + ".1").st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(p + ".7").st_mode & 0o777, 0o600,
                         "an archive from a larger former retention leaks forever otherwise")

    def test_M4_the_lock_sidecar_is_not_mistaken_for_an_archive(self):
        # Listing the directory widens the range on purpose; it must not widen it onto the wrong files.
        d = tempfile.mkdtemp()
        p = os.path.join(d, "events.ndjson")
        sink = km.RotatingFileSink(p, 0, 5)
        self.addCleanup(sink.close)
        self.assertEqual([os.path.basename(x) for x in sink._archive_paths()], [])
        for name in ("events.ndjson.lock", "events.ndjson.bak", "events.ndjson.2"):
            with open(os.path.join(d, name), "w") as f:
                f.write("x")
        self.assertEqual([os.path.basename(x) for x in sink._archive_paths()], ["events.ndjson.2"])


if __name__ == "__main__":
    unittest.main()
