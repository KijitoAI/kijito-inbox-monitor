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
  "                    if closed:\n                        # AND RELEASE THE FORCED PIN",
  "                    if False:\n                        # AND RELEASE THE FORCED PIN"),
 ("MEDIUM: forced pin released with NO authoritative evidence",
  "                    if self.pin_forced and complete:",
  "                    if self.pin_forced:"),
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
 ("L6-HIGH3: contradictory withholding+terminal trusted",
  "            if poll.omitted and poll.next_before_id is None:",
  "            if False:"),
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
