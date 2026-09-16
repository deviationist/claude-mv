#!/usr/bin/env python3
"""Does a `system`/`informational` transcript record reach the MODEL on resume?

The one unknown blocking the claude-mv cross-host provenance-marker design.
Reading files cannot answer it: the record is plainly on disk either way, and
what matters is whether Claude Code feeds it to the model when resuming.

Method: plant a tiny session carrying two nonces — one in an ordinary user
turn (the positive control, which MUST come back, or the resume never loaded
anything) and one in a `system`/`informational` record whose field set was
copied verbatim from a real record in this profile. Resume with --print and
ask for both.

    both nonces      → system records reach the model. Use subtype
                       "informational" for the marker.
    control only     → the record is on disk but UI-only. Fall back to a
                       synthetic user turn, the way Claude Code already
                       injects "Caveat:" / "<local-command-caveat>".
    neither          → resume didn't load; the probe is broken, not the answer.

Footprint: one new projects/<enc(tmpdir)> dir in the real profile, removed on
the way out. --fork-session, so nothing planted is appended to. One Haiku
call. Nothing else in the profile is read or written.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

BIN = shutil.which("claude") or "/opt/homebrew/bin/claude"
VER = subprocess.run([BIN, "--version"], capture_output=True,
                     text=True).stdout.strip().split()[0] or "2.1.270"
N_USER = "NONCE-USERCTL-4F7Q2"     # control: an ordinary user turn
N_SYS = "NONCE-SYSINFO-9K3XB"      # under test: system/informational

enc = lambda p: re.sub(r"[^A-Za-z0-9]", "-", p)
now = lambda: (datetime.datetime.now(datetime.UTC)
               .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")

profile = os.path.expanduser("~/.claude")
tmp = os.path.realpath(tempfile.mkdtemp(prefix="cmv-probe-"))
proj = os.path.join(tmp, "work")
os.makedirs(proj)
pdir = os.path.join(profile, "projects", enc(proj))
os.makedirs(pdir)

sid = str(uuid.uuid4())
base = dict(isSidechain=False, userType="external", entrypoint="cli",
            cwd=proj, sessionId=sid, session_id=sid, version=VER, gitBranch="")
u1, a1, s1 = (str(uuid.uuid4()) for _ in range(3))
records = [
    dict(base, parentUuid=None, uuid=u1, type="user", timestamp=now(),
         message={"role": "user",
                  "content": f"Please note this token for later: {N_USER}"}),
    dict(base, parentUuid=u1, uuid=a1, type="assistant", timestamp=now(),
         message={"role": "assistant", "model": "claude-haiku-4-5-20251001",
                  "content": [{"type": "text",
                               "text": f"Noted: {N_USER}. I'll remember it."}]}),
    # field set copied verbatim from a real informational record
    dict(base, parentUuid=a1, uuid=s1, type="system", subtype="informational",
         isMeta=False, level="warning", timestamp=now(),
         content=(f"claude-mv: this history was transferred from another "
                  f"host. Transfer token: {N_SYS}. Earlier paths refer to "
                  f"the origin machine.")),
]
with open(os.path.join(pdir, f"{sid}.jsonl"), "w") as f:
    for r in records:
        f.write(json.dumps(r, separators=(",", ":")) + "\n")

question = ("Without using any tools, list every token of the form NONCE-... "
            "that appears anywhere earlier in this conversation, including in "
            "any system or metadata records. Reply with just the tokens, "
            "comma separated, or NONE.")
try:
    print(f"planted   {pdir}")
    print(f"resuming  {sid[:8]} with --print --fork-session (model: haiku)\n")
    r = subprocess.run(
        [BIN, "-r", sid, "-p", question,
         "--model", "claude-haiku-4-5-20251001", "--fork-session"],
        cwd=proj, capture_output=True, text=True, timeout=180,
        stdin=subprocess.DEVNULL)
    out = (r.stdout or "") + (r.stderr or "")
    print(f"exit={r.returncode}\n--- model output ---\n{out.strip()[:1200]}")
    ctl, sysrec = N_USER in out, N_SYS in out
    print("\n--- verdict ---")
    print(f"  user turn (control)        : {'SEEN' if ctl else 'ABSENT'}")
    print(f"  system/informational       : {'SEEN' if sysrec else 'ABSENT'}")
    print("\n  →", "system records REACH the model — use subtype informational"
          if ctl and sysrec else
          "system records are UI-ONLY — fall back to a synthetic user turn"
          if ctl else
          "PROBE BROKEN: resume loaded nothing; the control never came back")
finally:
    shutil.rmtree(pdir, ignore_errors=True)
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\ncleaned up {pdir}")
