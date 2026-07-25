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
  "        if self.sink_template is not None:\n            s = self._sinks_by_persona.get(persona or \"_all\")\n            if s is not None:\n                ok = s.sync() and ok",
  "        for s in self._sinks_by_persona.values():\n            ok = s.sync() and ok"),
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
  "        if st.st_mode & 0o077:\n            os.fchmod(fd, PRIVATE_FILE_MODE)",
  "        if False:\n            os.fchmod(fd, PRIVATE_FILE_MODE)"),
 ("L8-HIGH1: the lock sidecar goes back to the umask",
  "        self._lockf = _open_private(self.path + \".lock\", \"a+\")",
  "        self._lockf = open(self.path + \".lock\", \"a+\")"),
 ("L8-HIGH2: a newly created event file does not sync its directory entry",
  "        if not existed:",
  "        if False:"),
 ("L8-HIGH2: a rotation does not sync the rewritten directory entries",
  "            self._dir_pending = True\n            self._open()",
  "            self._open()"),
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
    d=tempfile.mkdtemp(); shutil.copy(TESTS, os.path.join(d,"test_kijito_monitor.py"))
    open(os.path.join(d,"kijito_inbox_monitor.py"),"w").write(src)
    p=subprocess.run([sys.executable,"-m","unittest","test_kijito_monitor"],cwd=d,capture_output=True,text=True)
    return p.returncode, p.stderr
base=open(SRC).read()
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
    m=re.search(r"FAILED \((.*)\)",err); det=m.group(1) if m else "error"
    total=re.search(r"Ran (\d+) tests",err)
    nerr=re.search(r"errors=(\d+)",det); nfail=re.search(r"failures=(\d+)",det)
    if rc==0: print("SURVIVED ",label); surv.append(label)
    elif nfail is None and nerr and total and int(nerr.group(1))>int(total.group(1))*0.5:
        print("!! CRASHED not failed:",label,det); surv.append(label)
    else: print("caught   ",label,"  (%s)"%det)
print()
print("%d SURVIVED"%len(surv) if surv else "all %d mutations caught"%len(M))
for x in surv: print("  -",x)
sys.exit(1 if surv else 0)
