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
        return t

    def _pages(self, mapping, calls=None):
        """mapping: before_id -> (items, next_before_id)"""
        def f(opener, url, headers):
            bid = int(url.split("before_id=")[1].split("&")[0])
            if calls is not None:
                calls.append(bid)
            items, nb = mapping.get(bid, ([], None))
            return km.Poll(True, items=items, next_before_id=nb)
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
        calls = []
        t = self._target(cursor=0)
        orig, km.fetch = km.fetch, self._pages({200: ([{"id": 199}], 200)}, calls)
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
            return km.Poll(True, items=[{"id": bid - 1}], next_before_id=bid - 1)
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
        def __init__(self):
            self.new_ids, self.events = [], []

        def new(self, m):
            self.new_ids.append(m["id"])

        def lifecycle(self, event, **f):
            self.events.append((event, f))

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
        return t

    def _fetch(self, main_items, omitted, walk=None, exact=True, walk_fail=False):
        """main window + a backward-walk map {before_id: (items, next_before_id)}."""
        def f(opener, url, headers):
            if "before_id=" in url:
                if walk_fail:
                    return km.Poll(False, reason="http 502")
                bid = int(url.split("before_id=")[1].split("&")[0])
                items, nb = (walk or {}).get(bid, ([], None))
                return km.Poll(True, items=items, next_before_id=nb)
            return km.Poll(True, items=main_items, omitted=omitted, omitted_exact=exact)
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
                return km.Poll(True, items=[{"id": bid - 1}], next_before_id=bid - 1)
            return km.Poll(True, items=[{"id": 100000}], omitted=1)
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
            return km.Poll(True, items=[{"id": next(floors)}], omitted=2)
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

        # The walk must NOT succeed here, or it would legitimately close the span and release the pin -
        # which is a DIFFERENT property, tested separately below. Isolating them keeps this test about
        # the one thing its name claims: arming must not let the replay cap cross a forced pin.
        def f(opener, url, headers):
            if "before_id=" in url:
                return km.Poll(False, reason="http 502")
            return km.Poll(True, items=[{"id": 200}, {"id": 201}], omitted=3)
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
                return km.Poll(True, items=[{"id": 150}, {"id": 100}], next_before_id=None)
            return km.Poll(True, items=[{"id": 200}], omitted=1, next_before_id=200)
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
        orig, km.fetch = km.fetch, lambda o, u, h: km.Poll(
            True, items=[{"id": 90}, {"id": 100}, {"id": 120}], omitted=0, next_before_id=90)
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
        orig, km.fetch = km.fetch, lambda o, u, h: km.Poll(
            True, items=[{"id": 90}, {"id": 120}], omitted=2, next_before_id=90)
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
        km.subprocess.run = lambda *a, **k: captured.update(k.get("env") or {})
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
            return km.Poll(True, items=[{"id": 200}], omitted=1, next_before_id=200)
        self._run(t, f)
        self.assertEqual(t.cursor, 100)                       # *** did NOT step over 125 ***
        self.assertTrue([x for e, x in em.events if e == "alert"])

    def test_LOOM5_a_MALFORMED_continuation_must_not_read_as_end_of_chain(self):
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)

        def f(opener, url, headers):
            if "before_id=" in url:
                return km.fetch_from_payload({"result": [{"id": 150}], "next_before_id": "150"})
            return km.Poll(True, items=[{"id": 200}], omitted=1, next_before_id=200)
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
            return km.Poll(True, items=[{"id": 200}], omitted=1, next_before_id=200)
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
        pages = {200: {"result": [{"id": 150}], "next_before_id": 120},   # skips 130
                 120: {"result": [{"id": 100}], "next_before_id": None}}

        def f(opener, url, headers):
            if "before_id=" in url:
                bid = int(url.split("before_id=")[1].split("&")[0])
                return km.fetch_from_payload(pages[bid])
            return km.Poll(True, items=[{"id": 200}], omitted=1, next_before_id=200)
        self._run(t, f)
        self.assertEqual(t.cursor, 100)
        self.assertTrue([x for e, x in em.events if e == "alert"])

    def test_a_continuation_equal_to_the_oldest_row_is_accepted(self):
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)
        pages = {200: {"result": [{"id": 150}], "next_before_id": 150},
                 150: {"result": [{"id": 100}], "next_before_id": None}}

        def f(opener, url, headers):
            if "before_id=" in url:
                bid = int(url.split("before_id=")[1].split("&")[0])
                return km.fetch_from_payload(pages[bid])
            return km.Poll(True, items=[{"id": 200}], omitted=1, next_before_id=200)
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
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)
        pages = {200: {"result": [], "next_before_id": 150},
                 150: {"result": [{"id": 100}], "next_before_id": None}}

        def f(opener, url, headers):
            if "before_id=" in url:
                return km.fetch_from_payload(pages[int(url.split("before_id=")[1].split("&")[0])])
            return km.Poll(True, items=[{"id": 200}], omitted=1, next_before_id=200)
        self._run(t, f)
        self.assertEqual(t.cursor, 100)                 # *** did not step over 175 ***
        self.assertTrue([x for e, x in em.events if e == "alert"])

    def test_an_empty_page_that_AFFIRMS_the_end_still_closes(self):
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)

        def f(opener, url, headers):
            if "before_id=" in url:
                return km.fetch_from_payload({"result": [], "next_before_id": None})
            return km.Poll(True, items=[{"id": 200}], omitted=1, next_before_id=200)
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
            return km.Poll(True, items=[{"id": 200}], omitted=1, next_before_id=200)
        self._run(t, f)
        self.assertEqual(t.cursor, 100)
        self.assertTrue([x for e, x in em.events if e == "alert"])

    def test_LOOM6_empty_plus_truncated_plus_terminal_is_also_contradictory(self):
        em = BoundedWindowEndToEndTest.RecordingEmitter()
        t = self._target(cursor=100, emitter=em)

        def f(opener, url, headers):
            if "before_id=" in url:
                return km.fetch_from_payload({"result": [], "truncated": True, "next_before_id": None})
            return km.Poll(True, items=[{"id": 200}], omitted=1, next_before_id=200)
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
        self.addCleanup(lambda: t.state_file and t.state_file.close()
                        if hasattr(t.state_file, "close") else None)
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
        self.assertTrue(t.pin_forced)              # from the FLAG, not inferred from lost evidence
        self.assertTrue(t.pin_evidence_intact)


if __name__ == "__main__":
    unittest.main()
