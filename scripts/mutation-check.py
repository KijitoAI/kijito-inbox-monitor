#!/usr/bin/env python3
"""Behavioural mutation check: break each guarantee on purpose, confirm the suite NOTICES.

WHY THIS EXISTS. A green suite is not evidence. It says the tests pass, not that they can FAIL when the
behaviour they name is removed. Every fix in this repo is verified by deliberately reintroducing the bug
and requiring the suite to go red; anything that survives marks a test that is decorative.

WHAT IT REFUSES TO COUNT AS A CATCH, each learned from a mutant that lied here:
  · a mutant whose pattern is no longer in the source  -> VACUOUS (the code moved; the test proved nothing)
  · a mutant that does not COMPILE                     -> proves the suite noticed a SyntaxError, not a defect
  · a mutant where nearly every test ERRORS            -> the program crashed rather than misbehaved
Only a compiling mutant that produces FAILURES counts.

USAGE
    python3 scripts/mutation-check.py          # exits non-zero if any mutation survives

EXTENDING IT. MUTATIONS is a list of (label, pattern, replacement) or (label, [(pat, rep), ...]) for
multi-part edits. Each entry must remove exactly ONE guarantee. When a mutation survives, ask three
questions before adding a test: is the property untested, is the code under test the code that actually
RUNS (a duplicated implementation will hide this), and could an OLDER guard be catching the scenario first?
All three have happened in this repo.
"""

import re, shutil, subprocess, sys, tempfile, os
MUTANT_TIMEOUT = 90   # a mutant that hangs must not stall the gate; it is reported, never counted
SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "kijito_inbox_monitor.py")
TESTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "test_kijito_monitor.py")
M=[
 ("HIGH1a: silence read as end-of-chain again",
  "            if not poll.continuation_ok:\n                # Absent or malformed continuation: the server did not answer. NOT exhaustion.\n                return (rows, False)",
  "            pass"),
 ("HIGH1a: malformed continuation coerced to a valid None",
  "        nb, nb_ok = None, False",
  "        nb, nb_ok = None, True"),
 ("HIGH1a: absent field treated as an affirmed null",
  "    nb_raw = data.get(\"next_before_id\", _MISSING)",
  "    nb_raw = data.get(\"next_before_id\")"),
 ("HIGH1b: continuation no longer checked against oldest row",
  "            if poll.next_before_id is not None and poll.next_before_id != oldest:",
  "            if False:"),
 ("HIGH1b: check weakened to allow a lower continuation",
  "            if poll.next_before_id is not None and poll.next_before_id != oldest:\n                # The chain skips",
  "            if poll.next_before_id is not None and poll.next_before_id > oldest:\n                # The chain skips"),
 ("HIGH2: corrupt state collapses back into absent",
  "            return CORRUPT_STATE\n        if not ((cursor is None or _is_int(cursor))",
  "            return None\n        if not ((cursor is None or _is_int(cursor))"),
 ("HIGH2: corrupt state baselines to the newest id",
  "                        self.cursor = min((m[\"id\"] for m in items), default=0) - 1\n                        new_items = sorted(items, key=lambda m: m[\"id\"])",
  "                        self.cursor = max((m[\"id\"] for m in items), default=0)"),
 ("MEDIUM: completed walk no longer releases the forced pin",
  "                    release_earned = closed",
  "                    release_earned = False"),
 ("MEDIUM: forced pin released with NO authoritative evidence",
  "                    if self.pin_forced and complete and blocked_at is None:",
  "                    if self.pin_forced and blocked_at is None:"),
 ("MEDIUM: forced pin ignored entirely when advancing",
  "                    if self.pin_forced:\n                        high = None           # still forced: the watermark holds",
  "                    if False:\n                        high = None"),
 ("L6-HIGH1: pin_forced not persisted",
  "        if pin_forced:\n            d[\"pin_forced\"] = True",
  "        pass"),
 ("L6-HIGH1: persisted pin ignored on load",
  "                    self.pin_forced = loaded[\"pin_forced\"] or not loaded[\"pin_evidence_intact\"]",
  "                    self.pin_forced = not loaded[\"pin_evidence_intact\"]"),
 ("L6-HIGH2: empty page claiming more is trusted",
  "                if poll.next_before_id is not None:\n                    # EMPTY PAGE CLAIMING THERE IS MORE",
  "                if False:\n                    # EMPTY PAGE CLAIMING THERE IS MORE"),
 # L6-HIGH3 moved into the shared consistency rule in round 7; mutate it THERE or the entry goes
 # vacuous and silently stops defending anything.
 ("L6-HIGH3: contradictory withholding+terminal trusted",
  "        if n and nb is None:\n            consistent = False",
  "        if False:\n            consistent = False"),
 ("L7-HIGH1: exec exit status discarded again",
  "            if r.returncode != 0:",
  "            if False:"),
 ("L7-HIGH1: cursor advances over what was not delivered",
  "                        high = max(sorted(delivered)",
  "                        high = max([m[\"id\"] for m in new_items]"),
 ("L7-HIGH1: the delivery gate is removed from the advance",
  "                    if blocked_at is not None and high is not None:",
  "                    if False:"),
 ("L7-HIGH1: delivery no longer stops at the first failure (order lost)",
  "                    if blocked_at is not None:\n                        break",
  "                    if False:\n                        break"),
 ("L7-HIGH1: a suppressed author read as a FAILED delivery (would pin forever)",
  "            return True\n        ev = {\"event\": \"new\"",
  "            return False\n        ev = {\"event\": \"new\""),
 ("L7-HIGH1: a failed sink write reported as a delivery",
  "            sys.stderr.write(\"kijito-inbox-monitor: WARNING event write FAILED, holding the cursor: %s\\n\" % e)\n            return False",
  "            return True"),
 ("L7-MEDIUM: the durability barrier is skipped",
  "                if delivered and not self.emitter.sync(self.persona):",
  "                if False:"),
 ("L7-MEDIUM: the barrier syncs every persona's sink, not this one's",
  "            s = self._sinks_by_persona.get(persona or \"_all\")\n            if s is not None:\n                ok = s.sync() and ok",
  "            for s in self._sinks_by_persona.values():\n                ok = s.sync() and ok"),
 # ★ loom re-audit 8: deleting a CALL proves the call is present; it does NOT prove the ANSWER is
 # read. Both forms are kept deliberately - the weak one catches removal, the strong one catches
 # "called it and ignored what it said", which is the defect that actually shipped.
 ("L8-HIGH3: the state directory is not fsynced at all (call removed)",
  "            if not _fsync_dir(dirn):",
  "            if False and not _fsync_dir(dirn):"),
 ("L8-HIGH3: ★ the directory fsync is CALLED and its failure IGNORED (result mutation)",
  "            if not _fsync_dir(dirn):\n                sys.stderr.write(\"kijito-inbox-monitor: WARNING state-file directory fsync FAILED",
  "            if _fsync_dir(dirn) is None:\n                sys.stderr.write(\"kijito-inbox-monitor: WARNING state-file directory fsync FAILED"),
 ("L8-HIGH3: save reports success regardless of durability",
  "                return False\n            return True\n        except OSError as e:",
  "                return True\n            return True\n        except OSError as e:"),
 ("L8-HIGH1: the event stream is opened with the umask again",
  "        self._fh = _open_private(self.path, \"a\", encoding=\"utf-8\")",
  "        self._fh = open(self.path, \"a\", encoding=\"utf-8\")"),
 ("L8-HIGH1: an already-leaked file is left world-readable",
  "    if cur != PRIVATE_FILE_MODE:",
  "    if False:"),
 ("L8-HIGH1: the lock sidecar goes back to the umask",
  "        self._lockf = _open_private(self.path + \".lock\", \"a+\")\n        try:\n            fcntl.flock(",
  "        self._lockf = open(self.path + \".lock\", \"a+\")\n        try:\n            fcntl.flock("),
 ("L8-HIGH2: a newly created event file does not sync its directory entry",
  "        if not existed:",
  "        if False:"),
 ("L8-HIGH2: a rotation does not sync the rewritten directory entries",
  "            self._dir_pending = True\n            self._reopen_or_break()",
  "            self._reopen_or_break()"),
 ("L8-HIGH2: sync() ignores the pending directory entry",
  "        if self._dir_pending:",
  "        if False:"),
 ("L7-MEDIUM: the sink is never fsynced",
  "                os.fsync(self._fh.fileno())\n                self._pending = False",
  "                self._pending = False"),
 ("L7-HIGH2: pin_forced read leniently again (a JSON 1 unpins)",
  "        pin_forced = _flag(\"pin_forced\")",
  "        pin_forced = d.get(\"pin_forced\") is True"),
 ("L7-HIGH2: a malformed pin field no longer fails closed",
  "        if not strict_ok:",
  "        if False:"),
 ("L7-HIGH2: gap_alerted accepts a bool again",
  "        elif _is_int(alerted):",
  "        elif isinstance(alerted, int):"),
 ("L7-HIGH3: a case-only identity is treated as a different source again",
  "            if identity_migratable(ident, self.identity):",
  "            if False:"),
 ("L7-HIGH3: identity migration widened to ignore host/path too",
  "    if stored[:4] != current[:4]:\n        return False",
  "    if False:\n        return False"),
 ("L7-HIGH4: the inverse contradiction is trusted again",
  "        elif not n and nb is not None:\n            consistent = False",
  "        elif False:\n            consistent = False"),
 ("L7-HIGH4b: the gap check ignores an UNANSWERED continuation",
  "        if not poll.continuation_ok:\n            # SILENCE IS NOT AN ANSWER HERE EITHER.",
  "        if False:\n            # SILENCE IS NOT AN ANSWER HERE EITHER."),
 ("L7-HIGH4b: an unanswered window can still RELEASE a pin",
  "                    complete = (poll.omitted == 0 and poll.consistent and poll.continuation_ok",
  "                    complete = (poll.omitted == 0 and poll.consistent and True"),
 ("L7-HIGH4: the gap check ignores consistency again",
  "        if not poll.consistent:\n            # A SELF-CONTRADICTORY WINDOW",
  "        if False:\n            # A SELF-CONTRADICTORY WINDOW"),
 ("L7-HIGH4: a contradictory window can release a pin again",
  "                    complete = (poll.omitted == 0 and poll.consistent",
  "                    complete = (poll.omitted == 0 and True"),
 ("L7-HIGH5: the corruption pin loses its release floor",
  "        if self.pin_release_at is not None:\n            floor = max(floor, self.pin_release_at)",
  "        pass"),
 ("L7-HIGH5: the release floor is never recorded",
  "                if self.pin_forced and self.state_corrupt and self.pin_release_at is None and items:",
  "                if False:"),
 ("L7-HIGH5: the release floor is not persisted (dies on restart)",
  "        if pin_release_at is not None:\n            d[\"pin_release_at\"] = pin_release_at",
  "        pass"),
 ("L7-HIGH5: delivered ids are tracked ONLY while pinned again",
  "                uncovered = {i for i in delivered if i > (self.cursor or 0)}",
  "                uncovered = {i for i in delivered if i > (self.cursor or 0)} if pinned else set()"),
 # Found by adversarially re-reading round 7 before submitting it: a pin discharged on a poll that
 # could not deliver throws away the release floor, and the restart cannot rebuild it.
 ("L7-HIGH5: a pin is discharged by a poll that FAILED to deliver (walk proof)",
  "                if release_earned and blocked_at is None:",
  "                if release_earned:"),
 ("L7-HIGH5: a pin is discharged by a poll that FAILED to deliver (complete-window proof)",
  "                    if self.pin_forced and complete and blocked_at is None:",
  "                    if self.pin_forced and complete:"),
 # Found by running the repro LIVE and reading what was persisted - no fixture asserted it.
 ("L7-HIGH5: a released pin leaves its state behind",
  "        self.pin_forced = False\n        self.pin_release_at = None\n        self.state_corrupt = False",
  "        self.pin_forced = False"),
 ("L7-poison: content bytes can wedge the watermark again (sanitiser removed)",
  "    try:\n        s.encode(\"utf-8\")\n    except UnicodeEncodeError:\n        s = s.encode(\"utf-8\", \"replace\").decode(\"utf-8\")\n    return s.replace(\"\\x00\", \"\") if \"\\x00\" in s else s",
  "    return s"),
 ("L7-poison: the sink crashes instead of reporting a failed delivery",
  "        except (OSError, UnicodeError, ValueError) as e:",
  "        except OSError as e:"),
 ("L9-H1: O_NOFOLLOW dropped (symlink followed again)",
  "             | getattr(os, \"O_NOFOLLOW\", 0) | getattr(os, \"O_NONBLOCK\", 0))",
  "             | getattr(os, \"O_NONBLOCK\", 0))"),
 ("L9-H1: O_NONBLOCK dropped (a FIFO would hang the watcher)",
  "             | getattr(os, \"O_NOFOLLOW\", 0) | getattr(os, \"O_NONBLOCK\", 0))",
  "             | getattr(os, \"O_NOFOLLOW\", 0))"),
 ("L9-H1: the re-read after chmod is dropped (repair assumed, not verified)",
  "        again = os.fstat(fd).st_mode & 0o777",
  "        again = PRIVATE_FILE_MODE"),
 ("L9-H1: the owner check is dropped",
  "    if st.st_uid != os.geteuid():",
  "    if False:"),
 ("L9-H1: the regular-file check is dropped",
  "    if not stat.S_ISREG(st.st_mode):",
  "    if False:"),
 ("L9-H1: ★ an untightenable file is WARNED ABOUT and written anyway (the round-8 behaviour)",
  "        if again != PRIVATE_FILE_MODE:\n            raise InsecureFile",
  "        if False:\n            raise InsecureFile"),
 ("L9-H2: pre-existing rotated archives are not repaired",
  "        for archive in self._archive_paths():\n            _repair_mode(archive)",
  "        for archive in []:\n            _repair_mode(archive)"),
 # L10-M4: the CALL survives, only its RANGE narrows back to current retention. Deleting a call proves
 # the call is present; it does not prove the range it walks is the right one.
 ("L10-M4: archive repair is bounded by CURRENT retention again (a shrunk `keep` strands the rest)",
  "        return sorted(os.path.join(d, n) for n in names\n"
  "                      if n.startswith(base + \".\") and n[len(base) + 1:].isdigit())",
  "        return sorted(os.path.join(d, \"%s.%d\" % (base, i)) for i in range(1, self.keep + 2)\n"
  "                      if \"%s.%d\" % (base, i) in names)"),
 # L10-H1: two mutations on ONE fix. The first deletes the CALL, the second keeps the call and discards
 # only its ANSWER - which is the exact defect loom found, and the one a call-deletion cannot detect.
 ("L10-H1: the state file itself is not repaired (call deleted)",
  "        if not _repair_mode(self.path):  # the state file itself",
  "        if False:  # the state file itself"),
 ("L10-H1: ★ _repair_mode's verdict is DISCARDED again at the call site (result-mutation)",
  "        if not _repair_mode(self.path):  # the state file itself",
  "        _repair_mode(self.path)\n        if False:  # the state file itself"),
 ("L10-H1: the state-file READ path follows symlinks again (O_NOFOLLOW dropped)",
  "            fd = os.open(self.path,\n"
  "                         os.O_RDONLY | getattr(os, \"O_NOFOLLOW\", 0) | getattr(os, \"O_NONBLOCK\", 0))",
  "            fd = os.open(self.path, os.O_RDONLY | getattr(os, \"O_NONBLOCK\", 0))"),
 ("L10-H1: an unsafe state file is trusted instead of failing closed",
  "        if self.unsafe:",
  "        if False:"),
 # L10-H2: the release condition. A permanent fail-closed is the same bug as a fail-open, facing the
 # other way - so the mutation restores the PERMANENT refusal and the suite must notice the recovery
 # never happens.
 ("L10-H2: ★ a refused persona sink is cached PERMANENTLY again (no release condition)",
  "            retry_at = self._broken_sinks.get(key)\n            if retry_at is not None and _monotonic() < retry_at:",
  "            retry_at = self._broken_sinks.get(key)\n            if retry_at is not None:"),
 ("L10-H2: a recovered sink never clears its warning suppression",
  "                _clear_persona_warning(key)",
  "                pass"),
 # L10-M3: the self-test. Call-mutation AND result-mutation, because the defect was purely in the result.
 ("L10-M3: ★ self_test discards the emitter's answer again and reports emit=OK (result-mutation)",
  "            emit_ok = bool(self.emitter.new({\"id\": 0, \"from\": \"self-test\", \"content\": \"synthetic emit OK\",\n"
  "                                             \"created\": _now_iso(), \"_persona\": self.persona}))",
  "            emit_ok = True\n"
  "            self.emitter.new({\"id\": 0, \"from\": \"self-test\", \"content\": \"synthetic emit OK\",\n"
  "                              \"created\": _now_iso(), \"_persona\": self.persona})"),
 ("L9-M3: makedirs goes back to leaf-only",
  "    for d in reversed(missing):\n        try:\n            os.mkdir(d, PRIVATE_DIR_MODE)",
  "    for d in reversed(missing[:1]):\n        try:\n            os.mkdir(d, PRIVATE_DIR_MODE)"),
 ("L9-M4: ★ save()'s durability answer is discarded again (result ignored at the CALL SITE)",
  "            if durable is False:\n                self._state_not_durable()",
  "            if False:\n                self._state_not_durable()"),
 ("L9-M5: a broken sink falls through to stdout instead of failing delivery",
  "            if sink is _BROKEN_SINK:\n                return False",
  "            if sink is _BROKEN_SINK:\n                return True"),
 ("L9-M5: a failed reopen escapes the poll loop again",
  "        if self._broken is not None or self._fh is None:\n            self._reopen_or_break()",
  "        if False:\n            self._reopen_or_break()"),
 ("L7-item7: the lock fd is never released",
  "            try:\n                self._lockf.close()\n            finally:\n                self._lockf = None",
  "            pass"),
 ("L6-HIGH4: zero-byte state reads as absent again",
  "            return CORRUPT_STATE\n        try:\n            d = json.loads(raw)",
  "            return None\n        try:\n            d = json.loads(raw)"),
 ("L6-MEDIUM: bool accepted as an int again",
  "    return isinstance(v, int) and not isinstance(v, bool)",
  "    return isinstance(v, int)"),
 ("L6-MEDIUM: duplicate ids allowed",
  "        if m[\"id\"] in seen_ids:",
  "        if False:"),
 ("L6-MEDIUM: uninterpretable truncation reads as no-omission",
  "    elif trunc is not _MISSING and trunc is not False:",
  "    elif False:"),
 # ---- re-audit 11 (river): acknowledge-before-deliver in the ALARM path -------------------------
 # ★ A1 is the one that mattered: this exact mutation SURVIVED all 242 tests before this round,
 # because the dead-man's switch - the event README sells as the headline feature - had no test.
 ("L11-A1: ★ the liveness DOWN alert is deleted outright (it survived 242 tests before)",
  '                    down_reason = poll.reason or "unreachable"\n'
  '                    self._alarm("alert", "source is DOWN: %s" % down_reason,\n'
  '                                reason=down_reason,\n'
  '                                consecutive_failures=self.failures,\n'
  '                                seconds=int(round(span)),\n'
  '                                floor_seconds=int(floor))',
  '                    pass'),
 ("L11-F1: the DOWN alert loses its stderr fallback (undelivered == silent again)",
  '            sys.stderr.write("kijito-inbox-monitor: %s EVENT UNDELIVERED (persona %r): %s\\n"\n'
  '                             % (event.upper(), self.persona, log_text))',
  '            pass'),
 ("L11-F1: ★ the gap alarm latches BEFORE delivery again (an undelivered alarm never re-raises)",
  '                        if self._alarm("alert", gap_reason,',
  '                        self.gap_alerted = cursor_at\n'
  '                        if self._alarm("alert", gap_reason,'),
 ("L11-F1: WatchTarget.lifecycle drops the emitter's answer again",
  "        return self.emitter.lifecycle(event, **fields)",
  "        self.emitter.lifecycle(event, **fields)"),
 ("L11-F1: the FAST-PATH recovered edge stops reporting an undelivered event",
  '                self.fsm_state = "UP"\n                self._alarm("recovered", "source recovered", cursor=self.cursor)',
  '                self.fsm_state = "UP"\n                self.lifecycle("recovered", cursor=self.cursor)'),
 ("L11-F1: the MAIN-PATH recovered edge stops reporting an undelivered event",
  '                    self._alarm("recovered", "source recovered", cursor=self.cursor)',
  '                    self.lifecycle("recovered", cursor=self.cursor)'),
 ("L11-F3: the startup persona list dedupes EXACTLY again (case-variants self-deadlock)",
  "        key = p.casefold()\n        if p and key not in seen:",
  "        key = p\n        if p and key not in seen:"),

 # ---- the containment residual: InsecureFile is an OSError, NOT a FatalConfig ---------------------
 # Each arm gets its OWN entry, deliberately: river's binding condition on this fix, because a
 # containment guard with no mutation is EXACTLY how this defect stayed invisible for two rounds. The
 # narrowing below is the original bug, restored verbatim - a guard that names the wrong exception type
 # is indistinguishable at a glance from one that works, so only a mutation can tell them apart.
 ("R0-arm1: DIRECTORY rediscovery catches FatalConfig only, so a hostile lock kills the producer",
  "except (FatalConfig, OSError) as e:\n            # \u2605 THE CATCH",
  "except FatalConfig as e:\n            # \u2605 THE CATCH"),
 ("R0-arm2: COUNTS discovery catches FatalConfig only, so a hostile lock kills the producer",
  "except (FatalConfig, OSError) as e:\n                # Same widening",
  "except FatalConfig as e:\n                # Same widening"),
 ("R0-arm3: the STARTUP path catches FatalConfig only, so one hostile persona stops all the others",
  'except (FatalConfig, OSError) as e:\n            _warn_persona_once(p, "cannot watch persona',
  'except FatalConfig as e:\n            _warn_persona_once(p, "cannot watch persona'),
 # A duplicate FatalConfig arm compiles and is simply unreachable, so this deletes the backstop
 # without introducing a SyntaxError the harness would refuse to count.
 ("R0-arm4: the top-level OSError backstop is gone, so an escaping one is a traceback again",
  "    except OSError as e:\n        # THE BACKSTOP",
  "    except FatalConfig as e:\n        # THE BACKSTOP"),
 ("R0: containment degrades into a watcher that is UP and watching nothing (fail-closed removed)",
  "    if not targets:\n        # FAIL CLOSED.",
  "    if False:\n        # FAIL CLOSED."),
 # ladybug's review finding on c6e1699: the urgent alarm was gated by --no-stranded-alerts, so the
 # stranded flag's own advice ("set this if you keep deliberate test inboxes") silenced a
 # higher-severity alarm about real members. Re-coupling them is the regression worth pinning.
 ("A1: the urgent-unanswered alarm is re-coupled to --no-stranded-alerts (one flag silences both again)",
  "        if counts_available and not args.no_urgent_alerts:",
  "        if counts_available and not args.no_stranded_alerts:"),
 # Row M375 (bug #19): a ~20 s server restart woke consumers twice (alert + recovered) because the
 # dead-man edge was a failure COUNT reached in ~7 s of long-poll retries. The floor and the retry hint
 # are the fix; each mutant below removes exactly one piece of it.
 ("M375a: the default alert floor is zero again (count-only edge: a 20 s restart alerts)",
  "    return (args.alert_after - 1) * args.poll_seconds",
  "    return 0"),
 ("M375b: the fast-path recovery announces `recovered` after a run that never alerted",
  "            # count endpoint reachable + no unread increase = a HEALTHY poll with no new items\n            if self.fsm_state == \"DOWN\":",
  "            # count endpoint reachable + no unread increase = a HEALTHY poll with no new items\n            if self.fsm_state == \"DOWN\" or self.failures:"),
 ("M375c: a 502's Retry-After is read and thrown away",
  "        _RETRY_HINT[\"seconds\"] = _retry_after_seconds(e.headers, body)",
  "        pass"),
 ("M375d: the backoff ignores the server's retry hint",
  "        backoff = max(backoff, hint)",
  "        pass"),
 ("M375e: the retry hint is not clamped (a hostile Retry-After parks the producer)",
  "    return max(0, min(secs, RETRY_AFTER_CAP))",
  "    return max(0, secs)"),
 # crucible (2026-09-27): a read-down that hides an arrival (N -> 1) was skipped until the resync floor.
 ("F1: the fast path fetches only on an INCREASE again",
  "                changed = ((unread, newest) != (self.last_unread, self.last_newest)",
  "                changed = ((unread > self.last_unread or newest != self.last_newest)"),
 # praetor, real Windows 11 (2026-09-27): save() skipped the write off POSIX, so every restart baselined
 # and mail sent while the producer was down was never announced.
 ("W1: the state file is silently not written on Windows again",
  "        d = {\"identity\": self.identity, \"cursor\": cursor, \"state\": state, \"consecutive_failures\": failures}",
  "        if not IS_POSIX:\n            return True\n        d = {\"identity\": self.identity, \"cursor\": cursor, \"state\": state, \"consecutive_failures\": failures}"),
 # praetor (2026-09-27): on Windows lock() was a bare return, so two producers on one state file both ran.
 ("W2: Windows takes no single-writer lock again",
  "            if msvcrt is None:\n                return\n            _makedirs_private(",
  "            if True:\n                return\n            _makedirs_private("),
 # river M379 (Kijito 14ec0b27): the server states each persona's newest unread id; N -> N reads+arrivals.
 ("N1: the fast path ignores the server's newest unread id again (N -> N stays blind)",
  "                changed = ((unread, newest) != (self.last_unread, self.last_newest)",
  "                changed = ((unread, newest) != (self.last_unread, newest)"),
 ("N2: a stale newest unread id survives a newer response",
  "    _NEWEST_UNREAD.clear()\n    _NEWEST_UNREAD.update(newest)",
  "    _NEWEST_UNREAD.update(newest)"),
 ("N3: a boolean is taken for a newest unread id",
  "                if nid is None or (isinstance(nid, int) and not isinstance(nid, bool) and nid >= 0):",
  "                if nid is None or (isinstance(nid, int) and nid >= 0):"),
 # river M486: the API base is configurable (--api-base > $KIJITO_BASE > default), plain http only to this
 # machine because every request carries the bearer token. Each mutant removes one piece of that.
 ("M486a: plain http is accepted for a remote host (the token crosses the network in the clear)",
  "    if scheme == \"http\" and host not in _LOOPBACK_HOSTS:",
  "    if False:"),
 ("M486b: $KIJITO_BASE is ignored",
  "    if env_value.strip():",
  "    if False:"),
 ("M486c: the environment beats an explicit --api-base",
  "    if flag_value is not None:\n        return normalize_api_base(flag_value, \"--api-base\"), \"--api-base\"",
  "    if flag_value is not None and not environ.get(API_BASE_ENV):\n        return normalize_api_base(flag_value, \"--api-base\"), \"--api-base\""),
 ("M486d: run() never installs the configured base (the producer still polls the hosted API)",
  "    set_api_base(base)\n",
  ""),
 ("M486e: only the first resolved address is pinned (localhost -> ::1 refuses a 127.0.0.1 server)",
  "    return tuple(dict.fromkeys(info[4][0] for info in infos))",
  "    return infos[0][4][0]"),
 ("M486f: a trailing slash is kept (every URL becomes //api/...)",
  "parts.path.rstrip(\"/\"))",
  "parts.path)"),
 ("M486g: a base with a user, query or supervisor-special character is accepted",
  "    if bad:\n        raise FatalConfig(\"%s %r contains",
  "    if False:\n        raise FatalConfig(\"%s %r contains"),
 ("M486h: the count endpoint is left on the hosted API when the base changes",
  "    NOTIFY_PENDING_URL = base + \"/api/notify/pending\"",
  "    pass"),
 # M486 review folds (sec-monitor17): one mutant per fold.
 ("M486-F1: environment proxies are honoured again (token to <API address>:<proxy port>)",
  "    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect, _PinnedHTTPHandler,\n                                       _PinnedHTTPSHandler)",
  "    return urllib.request.build_opener(_NoRedirect, _PinnedHTTPHandler, _PinnedHTTPSHandler)"),
 ("M486-F2: plain http trusts the name, not the resolved addresses",
  "    if p.scheme == \"http\":\n        loopback = ",
  "    if False:\n        loopback = "),
 ("M486-F3a: `armed` no longer says which API is watched",
  "                    self.lifecycle(\"armed\", cursor=self.cursor, api_base=KIJITO_BASE)",
  "                    self.lifecycle(\"armed\", cursor=self.cursor)"),
 ("M486-F3b: `heartbeat` no longer says which API is watched",
  "            self.lifecycle(\"heartbeat\", cursor=self.cursor, api_base=KIJITO_BASE)",
  "            self.lifecycle(\"heartbeat\", cursor=self.cursor)"),
 ("M486-F4: --print-api-base falls through to the watcher (its spawns must carry no token)",
  "        sys.stdout.write(base + \"\\n\")\n        return 0\n    if args.check_activity:",
  "        sys.stdout.write(base + \"\\n\")\n    if args.check_activity:"),
 ("M486-N1: a netloc that does not rebuild as host[:port] is accepted",
  "    if parts.netloc.lower() != rebuilt or (\":\" in host and not _is_ip(host)):",
  "    if False:"),
 ("M375f: a stale retry hint survives into the next, unrelated failure",
  "    req = urllib.request.Request(url, headers=headers, method=\"GET\")\n    _RETRY_HINT[\"seconds\"] = None\n",
  "    req = urllib.request.Request(url, headers=headers, method=\"GET\")\n"),
 # row M488: --redeem-key collects a key minted with delivery="pickup". Each mutant removes ONE defence.
 ("M488-B1: the allowed-base check is skipped (any --api-base receives the code)",
  "        why = _redeem_base_refusal(base, self.home)\n",
  "        why = None\n"),
 ("M488-B2: loopback is exempt from the human-written api_base file",
  "    if base == DEFAULT_API_BASE:\n        return None\n    if not IS_POSIX:\n        return (\"a non-default",
  "    if base == DEFAULT_API_BASE or urllib.parse.urlsplit(base).hostname in _LOOPBACK_HOSTS:\n        return None\n    if not IS_POSIX:\n        return (\"a non-default"),
 ("M488-B3: the api_base file is read without the owner/mode/symlink checks",
  "        raw = _read_private(path, 4096)\n",
  "        with open(path, \"rb\") as fh:\n            raw = fh.read(4096)\n"),
 ("M488-B4: Windows accepts a non-default base it cannot check",
  "    if not IS_POSIX:\n        return (\"a non-default server",
  "    if False:\n        return (\"a non-default server"),
 ("M488-B5: the code's host is never compared with the base",
  "        if host != want:\n",
  "        if False:\n"),
 ("M488-S1: the existing key itself is sent instead of its hash",
  "            h = hashlib.sha256(raw).hexdigest()\n",
  "            h = raw.decode()\n"),
 ("M488-S2: key-shaped environment values are not scanned",
  "            if _KEY_WHOLE.fullmatch(value):\n",
  "            if False:\n"),
 ("M488-S3: MCP client configs are not scanned",
  "            for raw in _scan_mcp_config(path):\n",
  "            for raw in []:\n"),
 ("M488-S4: per-persona watcher key files are not scanned",
  "    \".config/kijito-inbox-monitor/token.*\",\n",
  ""),
 ("M488-S5: the Kijito-written legacy key files are not scanned",
  "    \".claude/.kijito_api_token.*\",\n",
  ""),
 ("M488-S6: an insecure key file is silently skipped instead of stopping the run",
  "                except OSError as e:\n                    raise RedeemExit(REDEEM_EXIT_USAGE, \"REDEEM_REFUSED\", \"key file %s.",
  "                except OSError as e:\n                    continue\n                    raise RedeemExit(REDEEM_EXIT_USAGE, \"REDEEM_REFUSED\", \"key file %s."),
 ("M488-S7: the 32-hash cap is dropped",
  "        sent = found[:MAX_PRESENTED_KEY_HASHES]\n",
  "        sent = found\n"),
 ("M488-S8: an empty key file is hashed (sha256 of nothing goes on the wire)",
  "                if raw:\n                    add(raw, path)",
  "                if True:\n                    add(raw, path)"),
 ("M488-S9: a linked MCP config is not refused by its lstat (Windows has no O_NOFOLLOW)",
  "    if _is_link(st):\n        return skip(\"it is a symbolic link\")",
  "    if False:\n        return skip(\"it is a symbolic link\")"),
 ("M488-S10: an oversized MCP config is scanned",
  [("        if fst.st_size > MCP_CONFIG_MAX_BYTES:\n", "        if False:\n"),
   ("    if size > MCP_CONFIG_MAX_BYTES:\n        return skip", "    if False:\n        return skip")]),
 ("M488-R1: reading a key file TIGHTENS its mode (the check-only reader swapped for the repairing one)",
  "        _check_private_fd(fd, path)\n        chunks, size = [], 0",
  "        _assert_private_fd(fd, path)\n        chunks, size = [], 0"),
 ("M488-R2: a key file of any mode is read",
  "    if pst.st_mode & 0o777 != PRIVATE_FILE_MODE:\n        raise InsecureFile(\"%s has mode",
  "    if False:\n        raise InsecureFile(\"%s has mode"),
 ("M488-R6: a key file that is not a regular file (a FIFO, a directory) is read",
  "    if not stat.S_ISREG(pst.st_mode):\n",
  "    if False:\n"),
 ("M488-R7: another user's key file is read",
  "    if pst.st_uid != os.geteuid():\n",
  "    if False:\n"),
 ("M488-R3: the lstat pre-check before reading a private file is dropped",
  "    st = _lstat(path)\n    if _is_link(st):\n        raise InsecureFile(\"%s is a symbolic link",
  "    st = _lstat(path)\n    if False:\n        raise InsecureFile(\"%s is a symbolic link"),
 ("M488-R4: Windows reparse tags are ignored (symlinks and junctions followed)",
  "    return getattr(st, \"st_reparse_tag\", 0) in (IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT)",
  "    return False"),
 ("M488-R5: a Windows junction (mount point) is not treated as a link",
  "    return getattr(st, \"st_reparse_tag\", 0) in (IO_REPARSE_TAG_SYMLINK, IO_REPARSE_TAG_MOUNT_POINT)",
  "    return getattr(st, \"st_reparse_tag\", 0) in (IO_REPARSE_TAG_SYMLINK,)"),
 ("M488-T1: a target directory other users can write is accepted",
  "            if (st.st_mode & 0o022) and not (st.st_mode & stat.S_ISVTX):\n                raise RedeemExit",
  "            if False:\n                raise RedeemExit"),
 ("M488-T2: an occupied target is replaced without --replace",
  "            if not a.replace:\n",
  "            if False:\n"),
 ("M488-T3: --replace-prefix is not checked before sending",
  "            if not content or not content.startswith(a.replace_prefix.encode(\"ascii\")):\n",
  "            if False:\n"),
 ("M488-T4: --replace-prefix is not re-checked at install (a swapped key is clobbered)",
  "                if not current.startswith(self.args.replace_prefix.encode(\"ascii\")):\n",
  "                if False:\n"),
 ("M488-T5: an absent target is installed by a clobbering rename instead of a link",
  "            os.link(self.tmp, target)\n",
  "            os.replace(self.tmp, target)\n"),
 ("M488-T6: an empty target filled during the redeem is clobbered",
  "                if mode == \"empty\" and current:\n",
  "                if False:\n"),
 ("M488-T7: no exclusive-create fallback where hard links are unsupported",
  "            if e.errno not in _LINK_FALLBACK_ERRNOS:\n",
  "            if True:\n"),
 ("M488-T8: --token-file may name any path (a key routed into the workspace)",
  "        if self._same_path(p, watcher) or self._same_path(p, rest):\n            return p",
  "        if True:\n            return p"),
 ("M488-T9: a per-persona file name that --safe-persona would rewrite is accepted",
  "            if persona and _state_safe_persona(persona) == persona:\n",
  "            if persona:\n"),
 ("M488-T10: a rest (wider) key may target the watcher file",
  "        if a.kind == \"rest\" and self._same_path(target, watcher):\n",
  "        if False:\n"),
 ("M488-H1: a $HOME that disagrees with the passwd entry is ignored",
  "        if \"HOME\" in environ and not (environ[\"HOME\"] and _same_dir(environ[\"HOME\"], home)):\n",
  "        if False:\n"),
 ("M488-H2: Windows does not compare the home with the profile folder",
  "    if os.path.normcase(os.path.realpath(home)).casefold() != os.path.normcase(os.path.realpath(profile)).casefold():\n",
  "    if False:\n"),
 ("M488-N1: an nginx 502 (maybe after a commit) is read as retry-safe",
  "\"edge_internal_error\", \"edge_unavailable\", \"edge_gateway_timeout\"})",
  "\"edge_internal_error\", \"edge_unavailable\", \"edge_gateway_timeout\", \"edge_bad_gateway\"})"),
 ("M488-N2: retry-safe answers are read as ambiguous (the agent revokes a key it could still collect)",
  "        if err_code in _PICKUP_RETRY_CODES:\n",
  "        if False:\n"),
 ("M488-N3: pickup_unsealable is not a definite refusal",
  "    \"pickup_unsealable\": \"PICKUP_FAILED\",\n",
  ""),
 ("M488-N4: a 400 is not a usage error",
  "        if status == 400:\n",
  "        if False:\n"),
 ("M488-N5: no explicit connect, so a refused connection reads as ambiguous",
  "                conn.connect()\n",
  "                pass\n"),
 ("M488-N6: the run never records that the request was sent (an interrupt after it reads as retry-safe)",
  "            self.phase = \"sent\"\n",
  ""),
 ("M488-N7: an interrupt exits 0 like the watcher",
  "        outcome, why = r.abort(\"interrupted\"), \"interrupted\"",
  "        return 0"),
 ("M488-N8: the temp file is left behind",
  "        if self.tmp and not self.parked:\n",
  "        if False:\n"),
 ("M488-N9: verify ignores the account the probe reports",
  "        if meta[\"account\"] and data.get(\"account\") != meta[\"account\"]:\n",
  "        if False:\n"),
 ("M488-N10: verify ignores a probe that was not honoured",
  "        if not isinstance(data, dict) or data.get(\"probe\") is not True:\n",
  "        if not isinstance(data, dict):\n"),
 ("M488-N11: --redeem-key falls through to the watcher's argument checks and exit arms",
  "    if args.redeem_key:\n        return redeem_key(args)\n",
  ""),
 ("M488-N12: the redeem request carries an Authorization header",
  "\"User-Agent\": USER_AGENT, \"Content-Type\": \"application/json\", \"Accept\": \"application/json\"})",
  "\"User-Agent\": USER_AGENT, \"Content-Type\": \"application/json\", \"Accept\": \"application/json\", \"Authorization\": \"Bearer x\"})"),
]
def run(src):
    # RELEASE WHAT WE ACQUIRE (Loom re-audit 10, L6). The temp tree was never removed and the source file
    # was written through an unbound handle, so a full gate run leaked one directory per mutant plus a
    # descriptor each. In a tool whose whole purpose is to catch resource and lifecycle defects, leaking
    # both is not merely untidy - it is the harness exhibiting the class it is meant to detect.
    d=tempfile.mkdtemp()
    try:
        # COPY THE WHOLE TREE, not two files. The suite also reads the README and the supervisor templates
        # (the state-file-name and opaque-output agreement tests), so a two-file copy made the BASELINE red
        # and the gate refused to run at all - a harness that cannot run proves nothing, green or red.
        shutil.copytree(os.path.dirname(SRC), os.path.join(d, "t"),
                        ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules", ".venv"))
        d=os.path.join(d, "t")
        with open(os.path.join(d,"kijito_inbox_monitor.py"),"w") as fh:
            fh.write(src)
        # PIN THE WARNING FILTER. Inherited PYTHONWARNINGS=error turns a mutant's leaked fd into an ERROR,
        # which silently converts "survived" into "caught" - a gate whose verdict depends on the caller's
        # environment is not a gate (Loom re-audit 9, MEDIUM: 55+ ResourceWarnings under warnings=error).
        env=dict(os.environ); env["PYTHONWARNINGS"]="default"
        try:
            p=subprocess.run([sys.executable,"-m","unittest","test_kijito_monitor"],cwd=d,capture_output=True,
                             text=True,env=env,timeout=MUTANT_TIMEOUT)
        except subprocess.TimeoutExpired:
            # A mutant that HANGS is not caught - nothing asserted anything, the suite simply stopped. It also
            # used to stall the gate itself indefinitely, which is how a quality tool becomes one nobody runs.
            return None, "HUNG"
        return p.returncode, p.stderr
    finally:
        shutil.rmtree(os.path.dirname(d) if d.endswith(os.sep + "t") else d, ignore_errors=True)
with open(SRC) as fh: base=fh.read()
rc,_=run(base)
if rc!=0: print("BASELINE NOT GREEN"); sys.exit(1)
print("baseline: GREEN\n")
surv=[]
for entry in M:
    # (label, pattern, replacement), or (label, [(pattern, replacement), ...]) for ONE guarantee that lives in
    # more than one place (the docstring always promised this form; row M488 is its first user).
    label = entry[0]
    edits = entry[1] if len(entry) == 2 else [(entry[1], entry[2])]
    bad = None
    for pat, _ in edits:
        if pat not in base:
            bad = "!! PATTERN NOT FOUND (vacuous):"; break
        if base.count(pat)>1:
            # AN AMBIGUOUS PATTERN MUTATES WHICHEVER COPY COMES FIRST, NOT THE ONE ITS LABEL NAMES. The Windows
            # lock (W2) added a second, identical sidecar open, so "the lock sidecar goes back to the umask"
            # silently moved onto the Windows branch, which no POSIX test executes, and read as a survivor.
            # The reverse is worse: a copy that some other test happens to catch reads as a kill. Anchor it.
            bad = "!! PATTERN AMBIGUOUS (%d matches):" % base.count(pat); break
    if bad:
        print(bad,label); surv.append(label); continue
    mut=base
    for pat, rep in edits:
        mut=mut.replace(pat,rep,1)
    try: compile(mut,"m","exec")
    except SyntaxError as e:
        print("!! DOES NOT COMPILE:",label,e); surv.append(label); continue
    rc,err=run(mut)
    if rc is None:
        print("!! HUNG, not a catch:",label); surv.append(label); continue
    m=re.search(r"FAILED \((.*)\)",err); det=m.group(1) if m else None
    total=re.search(r"Ran (\d+) tests",err)
    nerr=re.search(r"errors=(\d+)",det or ""); nfail=re.search(r"failures=(\d+)",det or "")
    if rc==0: print("SURVIVED ",label); surv.append(label)
    elif total is None or det is None:
        # A RUN THAT PRODUCED NO TEST VERDICT IS NOT A CATCH (Loom re-audit 10, M5). `det` used to default
        # to the string "error" when no summary matched, and the chain's terminal `else` was the OPTIMISTIC
        # arm - so a mutant that killed the interpreter outright (a signal, os._exit, an import-time crash)
        # matched no failure pattern and was recorded as CAUGHT. Nothing asserted anything; the process
        # merely died, which proves the mutation is DETECTABLE but not that any TEST detects it.
        # `total` was computed here and never read - the same dropped-answer half of the class, inside the
        # gate itself. It is now the liveness precondition: no "Ran N tests" means the suite never ran, and
        # a check that could not run must never be scored as a check that passed.
        print("!! NO TEST VERDICT (crash/signal/exit %s), not a catch:"%rc,label); surv.append(label)
    elif nfail is None and nerr:
        # STRICTLY: no FAILURES means no test NOTICED the behaviour - it only noticed the program
        # breaking. The old form allowed this whenever errors were under half the suite, which is how
        # two error-only mutants were being counted as caught while the docstring said otherwise
        # (Loom re-audit 9, MEDIUM). A threshold here is a way of being slightly wrong on purpose.
        print("!! ERRORS ONLY, not a catch:",label,det); surv.append(label)
    else: print("caught   ",label,"  (%s)"%det)
print()
print("%d SURVIVED"%len(surv) if surv else "all %d mutations caught"%len(M))
for x in surv: print("  -",x)
sys.exit(1 if surv else 0)
