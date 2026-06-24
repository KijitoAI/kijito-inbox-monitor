import json
import os
import tempfile
import unittest
import urllib.error

import kijito_inbox_monitor as km


class Args:
    def __init__(self, persona=None, personas=None, all_personas=False, url=None):
        self.persona = persona
        self.personas = personas
        self.all_personas = all_personas
        self.url = url


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
        available, counts = km.fetch_unread_counts(opener, "http://127.0.0.1:7474/api/notify/pending", {})
        self.assertTrue(available)
        self.assertEqual(counts, {"argus": 9, "sterling": 0})
        self.assertEqual(counts.get("codex", 0), 0)

    def test_fetch_unread_counts_unavailable_on_http_or_bad_shape(self):
        available, counts = km.fetch_unread_counts(
            FakeOpener(FakeResponse(500, {"result": []})),
            "http://127.0.0.1:7474/api/notify/pending",
            {},
        )
        self.assertFalse(available)
        self.assertEqual(counts, {})

        available, counts = km.fetch_unread_counts(
            FakeOpener(FakeResponse(200, {"result": {}})),
            "http://127.0.0.1:7474/api/notify/pending",
            {},
        )
        self.assertFalse(available)
        self.assertEqual(counts, {})

    def test_fetch_unread_counts_unavailable_on_network_exception(self):
        available, counts = km.fetch_unread_counts(
            FakeOpener(exc=urllib.error.URLError("down")),
            "http://127.0.0.1:7474/api/notify/pending",
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
            km.validate_args(self._args(["--url", "http://x", "--poll-seconds", "0"]))

    def test_seed_at_rejected_in_multipersona(self):
        with self.assertRaises(km.FatalConfig):
            km.validate_args(self._args(["--all-personas", "--seed-at", "5"]))

    def test_seed_at_allowed_for_single_persona(self):
        km.validate_args(self._args(["--persona", "argus", "--seed-at", "5"]))  # must not raise

    def test_keep_logs_min_one(self):
        with self.assertRaises(km.FatalConfig):
            km.validate_args(self._args(["--url", "http://x", "--keep-logs", "0"]))

    def test_url_conflicts_with_persona(self):
        with self.assertRaises(km.FatalConfig):
            km.validate_args(self._args(["--url", "http://x", "--persona", "argus"]))

    def test_events_file_and_template_mutually_exclusive(self):
        with self.assertRaises(km.FatalConfig):
            km.validate_args(self._args(["--events-file", "/a", "--events-file-template", "/b.{persona}.ndjson"]))

    def test_events_file_template_requires_placeholder(self):
        with self.assertRaises(km.FatalConfig):
            km.validate_args(self._args(["--events-file-template", "/no/placeholder.ndjson"]))


if __name__ == "__main__":
    unittest.main()
