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
        self.assertEqual(km._declared_omissions({"result": [], "size_dropped": 15}), 15)

    def test_boolean_truncation_without_a_count_still_counts_as_incomplete(self):
        # "incomplete but unquantified" must never round down to "nothing missing".
        self.assertEqual(km._declared_omissions({"result": [], "truncated": True}), 1)
        self.assertEqual(km._declared_omissions({"result": [], "size_truncated": True}), 1)

    def test_a_complete_window_declares_nothing(self):
        self.assertEqual(km._declared_omissions({"result": [], "truncated": False,
                                                 "size_truncated": False, "size_dropped": 0}), 0)

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
        return t

    def test_window_reaching_back_past_the_cursor_is_safe(self):
        # Steady state: long-poll keeps the backlog tiny, so the window covers the cursor. Dropped
        # messages are all BELOW it and were already emitted. Must NOT fire - or it alarms forever.
        t = self._target(cursor=1135)
        poll = km.Poll(True, items=[{"id": 1101}, {"id": 1135}], omitted=15)
        self.assertIsNone(t._uncovered_gap(poll, poll.items))

    def test_window_starting_above_the_cursor_with_omissions_is_the_loss_case(self):
        # After an outage/burst: server dropped 10 and the window starts at 1200 while we are at 1100,
        # so (1100, 1200) may hold mail we never emitted.
        t = self._target(cursor=1100)
        poll = km.Poll(True, items=[{"id": 1200}, {"id": 1260}], omitted=10)
        self.assertEqual(t._uncovered_gap(poll, poll.items), (1100, 1200, 10))

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

    def test_reconcile_queries_unread_only_and_returns_rows_in_id_order(self):
        t = self._target(cursor=1100)
        seen = {}

        def fake_fetch(opener, url, headers):
            seen["url"] = url
            return km.Poll(True, items=[{"id": 1150}, {"id": 1105}], omitted=0)

        orig, km.fetch = km.fetch, fake_fetch
        try:
            rows = t._reconcile_gap(Args())
        finally:
            km.fetch = orig
        self.assertIn("unread_only=true", seen["url"])
        self.assertEqual([m["id"] for m in rows], [1105, 1150])   # ascending, so emission order is stable

    def test_reconcile_failure_is_best_effort_not_a_liveness_failure(self):
        t = self._target(cursor=1100)
        orig, km.fetch = km.fetch, lambda *a, **k: km.Poll(False, reason="http 502")
        try:
            self.assertEqual(t._reconcile_gap(Args()), [])   # returns empty, does not raise
        finally:
            km.fetch = orig


class BoundedWindowEndToEndTest(unittest.TestCase):
    """poll_once end-to-end: mail hidden by a bounded window must still reach the consumer."""

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
        no_fast_path = True     # force the full inbox poll, which is the path under test
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
        return t

    def test_mail_hidden_by_a_truncated_window_is_recovered_and_the_gap_is_announced(self):
        em = self.RecordingEmitter()
        t = self._target(cursor=1100, emitter=em)

        # Server drops the 2 OLDEST and returns a window starting ABOVE our cursor. 1105 and 1150 are
        # real mail addressed to us that the window hid; unread-only reconciliation can still see them.
        def fake_fetch(opener, url, headers):
            if "unread_only=true" in url:
                return km.Poll(True, items=[{"id": 1105}, {"id": 1150}], omitted=0)
            return km.Poll(True, items=[{"id": 1200}, {"id": 1260}], omitted=2)

        orig, km.fetch = km.fetch, fake_fetch
        try:
            t.poll_once()
        finally:
            km.fetch = orig

        # 1. the hidden messages were emitted, not skipped
        self.assertEqual(em.new_ids, [1105, 1150, 1200, 1260])
        # 2. the gap was announced loudly rather than swallowed
        alerts = [f for (e, f) in em.events if e == "alert"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["omitted"], 2)
        self.assertEqual(alerts[0]["window_floor"], 1200)
        self.assertEqual(alerts[0]["cursor_at"], 1100)
        self.assertEqual(alerts[0]["reconciled"], 2)
        # 3. cursor advanced only after the hidden mail was accounted for
        self.assertEqual(t.cursor, 1260)

    def test_steady_state_truncation_is_silent_and_changes_nothing(self):
        # The common case: window reaches back past the cursor, so the drops are already-emitted mail.
        # This must NOT alert, or the alarm fires on every poll and gets ignored.
        em = self.RecordingEmitter()
        t = self._target(cursor=1135, emitter=em)
        orig, km.fetch = km.fetch, lambda o, u, h: km.Poll(
            True, items=[{"id": 1101}, {"id": 1140}], omitted=15)
        try:
            t.poll_once()
        finally:
            km.fetch = orig
        self.assertEqual(em.new_ids, [1140])
        self.assertEqual([e for (e, f) in em.events if e == "alert"], [])
        self.assertEqual(t.cursor, 1140)

    def test_unrecoverable_gap_still_alerts_with_the_shortfall(self):
        # Reconciliation itself truncates or fails: we cannot close the gap, so it MUST be announced.
        em = self.RecordingEmitter()
        t = self._target(cursor=1100, emitter=em)

        def fake_fetch(opener, url, headers):
            if "unread_only=true" in url:
                return km.Poll(False, reason="http 502")
            return km.Poll(True, items=[{"id": 1200}], omitted=4)

        orig, km.fetch = km.fetch, fake_fetch
        try:
            t.poll_once()
        finally:
            km.fetch = orig
        alerts = [f for (e, f) in em.events if e == "alert"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["reconciled"], 0)
        self.assertIn("may never have been delivered", alerts[0]["reason"])


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

    def tearDown(self):
        km._REPORTED_STRANDED.clear()

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


if __name__ == "__main__":
    unittest.main()
