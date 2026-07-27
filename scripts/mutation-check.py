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
  "        self._lockf = _open_private(self.path + \".lock\", \"a+\")",
  "        self._lockf = open(self.path + \".lock\", \"a+\")"),
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
 # L10-M4: the CALL survives, only its RANGE narrows back to current retention. [[22206]] - deleting a
 # call proves the call is present; it does not prove the range it walks is the right one.
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
]
def run(src):
    # RELEASE WHAT WE ACQUIRE (Loom re-audit 10, L6). The temp tree was never removed and the source file
    # was written through an unbound handle, so a full gate run leaked one directory per mutant plus a
    # descriptor each. In a tool whose whole purpose is to catch resource and lifecycle defects, leaking
    # both is not merely untidy - it is the harness exhibiting the class it is meant to detect.
    d=tempfile.mkdtemp()
    try:
        shutil.copy(TESTS, os.path.join(d,"test_kijito_monitor.py"))
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
        shutil.rmtree(d, ignore_errors=True)
with open(SRC) as fh: base=fh.read()
rc,_=run(base)
if rc!=0: print("BASELINE NOT GREEN"); sys.exit(1)
print("baseline: GREEN\n")
surv=[]
for label,pat,rep in M:
    if pat not in base:
        print("!! PATTERN NOT FOUND (vacuous):",label); surv.append(label); continue
    mut=base.replace(pat,rep,1)
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
