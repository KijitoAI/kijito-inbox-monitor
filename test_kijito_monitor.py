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
