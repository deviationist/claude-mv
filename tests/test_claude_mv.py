#!/usr/bin/env python3
"""Test suite for claude-mv. Stdlib only — run it with:

    python3 claude/claude-mv/tests/test_claude_mv.py          # or -v
    python3 -m unittest discover claude/claude-mv/tests

Seven layers, each closing a gap the ones before it can't see:

  * unit tests on the pure helpers (path encoding, canonicalization, config
    merging, and the reporting layer — when colour is on, what the tally
    says, which policy an answer at the conflict prompt selects), loaded
    straight out of claude-mv.py;
  * end-to-end tests that build a throwaway Claude profile (projects/ dirs,
    session jsonl, .claude.json, history.jsonl) plus a project folder in a
    tmpdir, run claude-mv as a subprocess against it, and assert on the
    resulting on-disk state;
  * multi-profile tests (TestMultiProfile) that run SEVERAL profiles at once,
    in the two config layouts a real machine mixes — the end-to-end harness
    passes exactly one --profile, so nothing there can see a second profile
    being skipped or written into the wrong file;
  * wrapper tests (TestWrapperProfileResolution) on claude-mv.zsh, which picks
    which profiles the python is even told about — a decision every layer
    above bypasses by passing --profile itself. Needs zsh; skipped without it;
  * conformance tests (TestFormatConformance) that read the REAL ~/.claude
    and check the format the fixtures above imitate is still the format
    Claude actually writes — otherwise a format change leaves every test
    green while the tool silently breaks. Read-only; skipped if absent;
  * a live test (TestAgainstRealClaude) that drives the real claude binary
    and uses it as the oracle for its own cwd encoding;
  * a UI test (TestResumePickerWithTmux) that runs `claude --resume` under
    tmux at the moved path and reads the picker off the screen — the only
    layer that checks the thing a user actually asks for, with a plain-mv
    negative control proving it can fail.

The last two are opt-in via CLAUDE_MV_LIVE_TEST=1, so the default suite
shells out to nothing and stays fast. The first two run against tmpdirs
with CLAUDE_MV_RESTORE_ROOT redirected, so no test can write to the real
~/.claude or ~/.claude-mv; the third only ever reads it, and the live ones
point Claude at a throwaway CLAUDE_CONFIG_DIR.
"""

import ast
import glob
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "claude-mv.py")


def _load_module():
    """Import claude-mv.py despite the hyphen (not a valid module name)."""
    spec = importlib.util.spec_from_file_location("claude_mv", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cm = _load_module()


# ── unit: pure helpers ──────────────────────────────────────────────────────

class TestHelpers(unittest.TestCase):
    def test_enc_replaces_every_non_alnum(self):
        self.assertEqual(cm.enc("/Users/me/.zsh"), "-Users-me--zsh")
        self.assertEqual(cm.enc("/a/b_c.d"), "-a-b-c-d")

    def test_enc_is_ambiguous_in_reverse(self):
        # The reason nested dirs are confirmed against a session cwd.
        self.assertEqual(cm.enc("/a/foo/bar"), cm.enc("/a/foo-bar"))

    def test_under(self):
        self.assertTrue(cm.under("/a/b", "/a/b"))
        self.assertTrue(cm.under("/a/b/c", "/a/b"))
        self.assertFalse(cm.under("/a/bc", "/a/b"))
        self.assertFalse(cm.under("/a", "/a/b"))

    def test_remap(self):
        self.assertEqual(cm.remap("/a/old/sub", "/a/old", "/a/new"), "/a/new/sub")
        self.assertEqual(cm.remap("/a/old", "/a/old", "/a/new"), "/a/new")

    def test_merge_entries(self):
        src = {"hasTrustDialogAccepted": True, "allowedTools": ["Bash", "Read"],
               "projectOnboardingSeenCount": 5, "onlyInSrc": "keep",
               "exampleFiles": ["a.py"]}
        dst = {"hasTrustDialogAccepted": False, "allowedTools": ["Read", "Edit"],
               "projectOnboardingSeenCount": 2, "exampleFiles": []}
        out = cm.merge_entries(src, dst)
        self.assertTrue(out["hasTrustDialogAccepted"])          # booleans OR
        self.assertEqual(out["allowedTools"], ["Read", "Edit", "Bash"])
        self.assertEqual(out["projectOnboardingSeenCount"], 5)  # counters max
        self.assertEqual(out["onlyInSrc"], "keep")              # src fills gaps
        self.assertEqual(out["exampleFiles"], ["a.py"])         # default filled

    def test_merge_entries_dst_wins_for_populated_scalars(self):
        out = cm.merge_entries({"model": "old"}, {"model": "new"})
        self.assertEqual(out["model"], "new")

    def test_canonical_expands_and_normalizes(self):
        home = os.path.realpath(os.path.expanduser("~"))
        self.assertEqual(cm.canonical("~/code"), os.path.join(home, "code"))
        self.assertEqual(cm.canonical("/a/b/"), "/a/b")
        self.assertEqual(cm.canonical("/a/b/../c"), "/a/c")
        cwd = os.path.realpath(os.getcwd())
        self.assertEqual(cm.canonical("rel"), os.path.join(cwd, "rel"))

    def test_canonical_resolves_ancestors_but_not_the_last_component(self):
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="canon-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        real = os.path.join(tmp, "real")
        os.makedirs(os.path.join(real, "proj"))
        link = os.path.join(tmp, "link")
        os.symlink(real, link)

        # ancestor symlink: resolved, so enc() matches the recorded cwd
        self.assertEqual(cm.canonical(os.path.join(link, "proj")),
                         os.path.join(real, "proj"))
        # last component is itself a symlink: left alone — mv renames the
        # link, and the folder its sessions were recorded in stays put
        self.assertEqual(cm.canonical(link), link)

    def test_config_json_path(self):
        home_claude = os.path.join(os.path.expanduser("~"), ".claude")
        self.assertEqual(cm.config_json_path(home_claude),
                         os.path.expanduser("~/.claude.json"))
        self.assertEqual(cm.config_json_path("/tmp/prof"), "/tmp/prof/.claude.json")


# ── the reporting layer: colour, the tally, the conflict prompt ─────────────

def _plain(text):
    """Strip SGR sequences. Everything below asserts on what colour *does* —
    never on which codes it picks. Whether cyan is 36 is a fact about ECMA-48,
    not about claude-mv, and pinning it would only make the suite brittle."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class _Stream:
    """Minimal stand-in for stdout/stderr: color_enabled() only ever asks
    whether it is a tty, which is not a thing a test should have to own."""

    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


class TestReporting(unittest.TestCase):
    """The output IS the product here — there is no porcelain, every line is
    read by a person — so the decisions behind it are worth testing. The
    decisions, though, not the rendering."""

    def setUp(self):
        # color_enabled() and can_prompt() read os.environ directly.
        self._saved = {k: os.environ.get(k) for k in
                       ("CLAUDE_MV_COLOR", "NO_COLOR", "CLAUDE_MV_FORCE_PROMPT")}
        for k in self._saved:
            os.environ.pop(k, None)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    # -- is colour on? ------------------------------------------------------

    def test_colour_follows_the_stream_by_default(self):
        self.assertTrue(cm.color_enabled(_Stream(True)))
        self.assertFalse(cm.color_enabled(_Stream(False)))

    def test_no_color_wins_over_a_tty(self):
        os.environ["NO_COLOR"] = "1"
        self.assertFalse(cm.color_enabled(_Stream(True)))

    def test_an_explicit_mode_wins_over_everything(self):
        os.environ["CLAUDE_MV_COLOR"] = "always"
        self.assertTrue(cm.color_enabled(_Stream(False)))
        os.environ["NO_COLOR"] = "1"
        self.assertTrue(cm.color_enabled(_Stream(False)),
                        "an explicit `always` should beat NO_COLOR")
        os.environ.pop("NO_COLOR")
        os.environ["CLAUDE_MV_COLOR"] = "never"
        self.assertFalse(cm.color_enabled(_Stream(True)))

    def test_an_unrecognised_mode_falls_back_to_the_stream(self):
        os.environ["CLAUDE_MV_COLOR"] = "yes-please"
        self.assertTrue(cm.color_enabled(_Stream(True)))
        self.assertFalse(cm.color_enabled(_Stream(False)))

    # -- what colour is allowed to change: nothing ---------------------------

    def test_colour_is_a_pure_overlay(self):
        """The invariant the design rests on. Every other test in this file
        captures a pipe, so it reads the uncoloured text — those assertions
        are only valid for a terminal if colour adds escapes and nothing
        else."""
        os.environ["CLAUDE_MV_COLOR"] = "always"
        for text in ("moving", "/a/b \u2192 /c/d", "1 project dir",
                     "projects/-Users-you-code-foo", "\u26a0\ufe0f  careful"):
            self.assertEqual(_plain(cm.c(text, "bold", "cyan")), text)

    def test_disabled_colour_returns_the_string_untouched(self):
        os.environ["CLAUDE_MV_COLOR"] = "never"
        self.assertEqual(cm.c("moving", "bold", "cyan"), "moving")

    def test_every_style_name_is_one_the_tool_knows(self):
        """c() indexes _SGR directly, so a mistyped style name is a KeyError
        raised on a terminal in the middle of a migration — the one place an
        exception is most expensive."""
        os.environ["CLAUDE_MV_COLOR"] = "always"
        for style in cm._SGR:
            self.assertEqual(_plain(cm.c("x", style)), "x", style)

    def test_every_style_a_caller_asks_for_is_defined(self):
        """The complement, and the one with teeth: an unused entry in _SGR is
        harmless, but a CALL SITE naming a style that isn't there is a
        KeyError, raised on a terminal mid-migration — on the very code path
        the piped suite never colours. So read the call sites rather than
        trust them."""
        tree = ast.parse(io.open(SCRIPT, encoding="utf-8").read())
        used = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "c"):
                for arg in node.args[1:]:          # arg 0 is the text
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        used.add(arg.value)
        self.assertGreater(len(used), 3, "found almost no c() call sites — "
                                         "has the helper been renamed?")
        self.assertEqual(sorted(used - set(cm._SGR)), [])

    def test_the_message_prefixes_keep_their_plain_form(self):
        for mode in ("always", "never"):
            os.environ["CLAUDE_MV_COLOR"] = mode
            self.assertEqual(_plain(cm.emsg("boom")), "claude-mv: boom", mode)
            # the warning sign is drawn double-width, hence the two spaces
            self.assertEqual(_plain(cm.wmsg("careful")), "\u26a0\ufe0f  careful", mode)

    # -- the tally -----------------------------------------------------------

    def test_the_tally_reports_every_store(self):
        self.assertEqual(
            cm.summarize({"dirs": 3, "sessions": 4, "keys": 3, "history": 12}),
            "3 project dirs \u00b7 4 session files \u00b7 3 config keys "
            "\u00b7 12 history entries")

    def test_the_tally_is_singular_for_the_commonest_run(self):
        """One folder, one profile, nothing nested — the hero case, and the
        one that would read `1 project dir(s)` if nobody looked."""
        self.assertEqual(
            cm.summarize({"dirs": 1, "sessions": 1, "keys": 1, "history": 1}),
            "1 project dir \u00b7 1 session file \u00b7 1 config key "
            "\u00b7 1 history entry")

    def test_the_tally_drops_whatever_is_zero(self):
        self.assertEqual(
            cm.summarize({"dirs": 2, "sessions": 0, "keys": 1, "history": 0}),
            "2 project dirs \u00b7 1 config key")

    def test_an_empty_tally_says_nothing_rather_than_zeroes(self):
        self.assertEqual(cm.summarize(cm.new_tally()), "")

    # -- the conflict prompt -------------------------------------------------

    def _ask(self, answers, already_moved=False):
        """Drive ask_conflict_mode with canned answers. stdout is swallowed:
        the menu is rendering, and rendering is not what is under test."""
        os.environ["CLAUDE_MV_FORCE_PROMPT"] = "1"
        if answers is EOFError:
            def fake(_prompt=""):
                raise EOFError
        else:
            pending = iter(answers)

            def fake(_prompt=""):
                return next(pending)
        with mock.patch("builtins.input", fake), \
                mock.patch("sys.stdout", new=io.StringIO()):
            return cm.ask_conflict_mode(already_moved)

    def test_each_answer_selects_its_policy(self):
        for key, mode in (("o", "overwrite"), ("c", "consolidate"),
                          ("r", "rename-only"), ("a", "abort")):
            self.assertEqual(self._ask([key]), mode, key)

    def test_the_default_is_the_one_that_changes_nothing(self):
        self.assertEqual(self._ask([""]), "abort")

    def test_answers_are_case_and_whitespace_insensitive(self):
        self.assertEqual(self._ask(["  C  "]), "consolidate")

    def test_it_reprompts_rather_than_guessing(self):
        self.assertEqual(self._ask(["x", "consolidate", "o"]), "overwrite")

    def test_rename_only_is_withheld_after_already_moved(self):
        """With the folder already moved there is no mv left to do on its
        own, so `r` is just abort. It must not be quietly accepted as a
        distinct policy — the run would report a rename that never happened."""
        self.assertEqual(self._ask(["r", "c"], already_moved=True),
                         "consolidate")

    def test_end_of_input_aborts(self):
        self.assertEqual(self._ask(EOFError), "abort")

    def test_it_declines_to_ask_when_nobody_is_there(self):
        """can_prompt() is the gate main() uses to exit 2 instead of hanging
        on a closed stdin in a script or a CI job."""
        with mock.patch.object(cm, "can_prompt", lambda: False):
            self.assertIsNone(cm.ask_conflict_mode())


# ── conformance: does the real profile still match our fixtures? ────────────

REAL_PROFILE = os.path.expanduser("~/.claude")


@unittest.skipUnless(os.path.isdir(REAL_PROFILE), "no ~/.claude here")
class TestFormatConformance(unittest.TestCase):
    """Read-only checks of the live profile against what the fixtures assume.

    Everything else in this file tests claude-mv against *our model* of
    Claude Code's on-disk format. If Claude ever changes that format, those
    tests keep passing while the tool quietly stops working. These assert
    the model still matches reality, so the next test run is what tells us.

    Strictly read-only — nothing here writes, moves, or deletes.
    """

    def test_claude_agrees_with_our_model_of_project_state(self):
        """Piggyback on Claude Code's own enumeration of what a project owns.

        `claude project purge --dry-run <path>` lists exactly the question
        --export has to answer, from the vendor rather than from us. It is
        read as an ORACLE, never parsed at runtime: the output is prose and
        would be a fragile dependency, but as a test it is the earliest
        warning we can get that the on-disk model moved.

        Two claims the bundle design rests on are pinned here — that the
        project dir is project state, and that shell-snapshots is not.
        """
        binary = _claude_bin()
        if not os.path.exists(binary):
            self.skipTest("no claude binary")
        root = os.path.join(REAL_PROFILE, "projects")
        if not os.path.isdir(root):
            self.skipTest("no projects/ dir")
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            cwd = cm.jsonl_first_cwd(d) if os.path.isdir(d) else None
            if cwd and os.path.isdir(cwd) and cm.enc(cwd) == name:
                break
        else:
            self.skipTest("no project dir whose recorded cwd still exists")
        r = subprocess.run([binary, "project", "purge", "--dry-run", cwd],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            self.skipTest(f"purge --dry-run unavailable: {r.stderr[:200]}")
        out = r.stdout
        self.assertIn(os.path.join("projects", name), out,
                      "Claude no longer counts the project dir as project "
                      "state — --export's central assumption moved")
        # Conditional on purpose: Claude only mentions shell-snapshots for a
        # project that has one, so its absence is not a finding. Its presence
        # saying something OTHER than "not project-scoped" would be.
        if "shell-snapshots" in out:
            self.assertRegex(
                out, r"shell-snapshots/? are not project-scoped",
                "Claude used to say shell-snapshots is not project-scoped; "
                "--export refuses to carry it on that basis")

    def test_project_dir_names_are_enc_of_their_recorded_cwd(self):
        """The load-bearing assumption: dir name == enc(session cwd)."""
        root = os.path.join(REAL_PROFILE, "projects")
        if not os.path.isdir(root):
            self.skipTest("no projects/ dir")
        checked, bad = 0, []
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            cwd = cm.jsonl_first_cwd(d)
            if not cwd:
                continue          # no session recorded a cwd; nothing to check
            checked += 1
            if cm.enc(cwd) != name:
                bad.append(f"{name} holds sessions with cwd {cwd} "
                           f"(encodes to {cm.enc(cwd)})")
        if not checked:
            self.skipTest("no project dir had a recorded cwd")
        self.assertEqual(bad, [], "\n".join(
            ["Claude's cwd→dirname encoding no longer matches enc():"] + bad))

    def test_config_projects_is_a_map_keyed_by_absolute_path(self):
        cfg = cm.config_json_path(REAL_PROFILE)
        if not os.path.isfile(cfg):
            self.skipTest("no config json")
        with open(cfg, encoding="utf-8") as f:
            projects = json.load(f).get("projects")
        self.assertIsInstance(projects, dict, "projects map is gone")
        self.assertTrue(projects, "projects map is empty")
        self.assertTrue(all(k.startswith("/") for k in projects),
                        "project keys are no longer absolute paths")

    def test_history_entries_carry_an_absolute_project_path(self):
        hist = os.path.join(REAL_PROFILE, "history.jsonl")
        if not os.path.isfile(hist):
            self.skipTest("no history.jsonl")
        seen = 0
        with open(hist, encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                p = obj.get("project")
                if isinstance(p, str):
                    self.assertTrue(p.startswith("/"))
                    seen += 1
                if seen >= 50:
                    break
        self.assertTrue(seen, "no entry had a 'project' field any more")

    def test_history_entries_carry_the_session_that_wrote_them(self):
        """What makes --extract possible at all.

        The folder path re-keys history by path prefix; moving ONE session out
        of a shared project needs the entries attributable to it, and
        sessionId is the only field that can do that. If Claude drops it,
        selective re-keying stops being expressible and the mode has to go
        back to the drawing board rather than quietly re-key too much.
        """
        hist = os.path.join(REAL_PROFILE, "history.jsonl")
        if not os.path.isfile(hist):
            self.skipTest("no history.jsonl")
        seen = with_session = 0
        with open(hist, encoding="utf-8") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj.get("project"), str):
                    seen += 1
                    with_session += isinstance(obj.get("sessionId"), str)
                if seen >= 50:
                    break
        if not seen:
            self.skipTest("no entries with a project field")
        self.assertEqual(seen, with_session,
                         "history entries no longer carry sessionId — "
                         "--extract cannot tell one session's prompts apart")

    def test_user_turn_content_is_a_string_or_a_list_of_typed_blocks(self):
        """What the picker's labels are read out of.

        Both shapes occur and the list is much the commoner one, with its
        blocks carrying a `type` we filter on — take that away and every row
        would be labelled with tool output instead of the question asked.
        """
        root = os.path.join(REAL_PROFILE, "projects")
        if not os.path.isdir(root):
            self.skipTest("no projects/ dir")
        seen_str = seen_blocks = 0
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".jsonl"):
                    continue
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    for line in f:
                        try:
                            obj = json.loads(line)
                        except ValueError:
                            continue
                        if obj.get("type") != "user":
                            continue
                        content = (obj.get("message") or {}).get("content")
                        if isinstance(content, str):
                            seen_str += 1
                        elif isinstance(content, list):
                            for b in content:
                                self.assertIsInstance(b, dict)
                                self.assertIn("type", b, "content blocks no "
                                              "longer carry a type")
                                seen_blocks += 1
            if seen_str and seen_blocks:
                return
        if not (seen_str or seen_blocks):
            self.skipTest("no user turns with content")

    def test_a_session_sidecar_sits_beside_its_transcript(self):
        """The one session-keyed store that lives INSIDE a project dir.

        todos/, file-history/ and friends are keyed by session id at the
        profile root, so they follow a session anywhere for free. This one
        does not: <id>/ sits next to <id>.jsonl, so moving a single session
        has to carry it by hand. If the layout changes, that hand-carry is
        either wrong or unnecessary — either way this should say so.
        """
        root = os.path.join(REAL_PROFILE, "projects")
        if not os.path.isdir(root):
            self.skipTest("no projects/ dir")
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            entries = set(os.listdir(d))
            for entry in sorted(entries):
                if (os.path.isdir(os.path.join(d, entry))
                        and f"{entry}.jsonl" in entries):
                    return          # found the shape; nothing more to prove
        self.skipTest("no session sidecar dir in the real profile")

    def test_session_transcripts_are_written_compactly(self):
        """Not claude-mv's own concern — it parses JSON — but the fixtures
        imitate this byte shape, and ccfind (our session source when it is
        installed) reads the cwd out with a regex that assumes no space after
        the colon. A prettier format upstream would make the two sources
        disagree, so notice it here rather than in a user's picker."""
        root = os.path.join(REAL_PROFILE, "projects")
        if not os.path.isdir(root):
            self.skipTest("no projects/ dir")
        for name in sorted(os.listdir(root)):
            d = os.path.join(root, name)
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".jsonl"):
                    continue
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    for line in f:
                        if '"cwd"' not in line:
                            continue
                        self.assertIn('"cwd":"', line,
                                      "session jsonl is no longer compact")
                        return
        self.skipTest("no session line with a cwd")

    def test_live_session_records_still_carry_cwd_and_pid(self):
        """What the live-session guard reads before it will let a move run.

        sessionId too: the folder guard asks "is anything running in this
        path", the session guard asks "is THIS session running", and only the
        second can answer for a move that leaves the folder alone.
        """
        d = os.path.join(REAL_PROFILE, "sessions")
        if not os.path.isdir(d):
            self.skipTest("no sessions/ dir")
        names = [n for n in os.listdir(d) if n.endswith(".json")]
        if not names:
            self.skipTest("no session records")
        with open(os.path.join(d, names[0]), encoding="utf-8") as f:
            obj = json.load(f)
        self.assertIn("cwd", obj)
        self.assertIn("pid", obj)
        self.assertIn("sessionId", obj)


# ── end-to-end scaffolding ──────────────────────────────────────────────────

def jsonl(obj):
    """Serialize a fixture line the way Claude Code actually writes one.

    Compact, with no space after ':' — verified against the real profile by
    the conformance layer. It matters beyond byte-fidelity: tools that scan
    transcripts with a regex rather than a JSON parser (ccfind extracts its
    cwd column with `grep -o '"cwd":"[^"]*"'`) simply do not see a spaced
    fixture, so a prettier one would quietly break the cross-source test.
    """
    return json.dumps(obj, separators=(",", ":")) + "\n"


class FixtureCase(unittest.TestCase):
    """Builds a disposable Claude profile + project tree per test."""

    def setUp(self):
        # realpath: on macOS mkdtemp hands back /var/folders/… , which is a
        # symlink to /private/var/… . Claude records the resolved path, so an
        # unresolved fixture would be testing a path shape that never occurs
        # (and would mask exactly the symlink bug canonical() fixes).
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="claude-mv-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.profile = os.path.join(self.tmp, "profile")
        self.projects = os.path.join(self.profile, "projects")
        self.restore_root = os.path.join(self.tmp, "restore")
        os.makedirs(self.projects)
        self.code = os.path.join(self.tmp, "code")
        os.makedirs(self.code)
        self.config = {"projects": {}}
        self.history = []

    # -- fixture builders ---------------------------------------------------

    def make_folder(self, name):
        p = os.path.join(self.code, name)
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, "README.md"), "w") as f:
            f.write(f"# {name}\n")
        return p

    def make_project(self, cwd, sessions=("aaaa-1111",), extra_line=True):
        """A projects/<encoded-cwd> dir with one jsonl per session id."""
        d = os.path.join(self.projects, cm.enc(cwd))
        os.makedirs(d, exist_ok=True)
        for sid in sessions:
            lines = [{"type": "user", "cwd": cwd, "sessionId": sid,
                      "message": {"role": "user", "content": "hi"}}]
            if extra_line:
                # A line with no cwd must pass through byte-identical.
                lines.append({"type": "summary", "summary": "s", "leafUuid": sid})
            with open(os.path.join(d, f"{sid}.jsonl"), "w") as f:
                for obj in lines:
                    f.write(jsonl(obj))
        return d

    def make_session(self, home_cwd, sid, prompt="hi", later_cwd=None,
                     sidecar=False, mtime=None, says=(), raw=()):
        """One session homed in `home_cwd` — the --extract unit of work.

        `later_cwd` adds a second turn recorded somewhere else, which is the
        whole shape this feature exists for: a session that starts in a parent
        directory and cd's into the project it just created. `sidecar` adds the
        <id>/ dir Claude puts beside the transcript for subagents and tool
        results, which lives INSIDE the project dir and so has to be carried
        by hand when a single session moves.

        `says` is what the conversation went on to say — (role, text) turns in
        the block form Claude actually writes — and `raw` is whole records
        passed through untouched, for the lines --search has to match without
        being able to read them as speech.
        """
        d = os.path.join(self.projects, cm.enc(home_cwd))
        os.makedirs(d, exist_ok=True)
        lines = [{"type": "user", "cwd": home_cwd, "sessionId": sid,
                  "message": {"role": "user", "content": prompt}}]
        if later_cwd:
            lines.append({"type": "user", "cwd": later_cwd, "sessionId": sid,
                          "message": {"role": "user", "content": "and now here"}})
        for role, text in says:
            lines.append({"type": role, "cwd": home_cwd, "sessionId": sid,
                          "message": {"role": role,
                                      "content": [{"type": "text",
                                                   "text": text}]}})
        lines.extend(raw)
        lines.append({"type": "summary", "summary": "s", "leafUuid": sid})
        path = os.path.join(d, f"{sid}.jsonl")
        with open(path, "w") as f:
            for obj in lines:
                f.write(jsonl(obj))
        if sidecar:
            sub = os.path.join(d, sid, "subagents")
            os.makedirs(sub, exist_ok=True)
            with open(os.path.join(sub, "agent-1.jsonl"), "w") as f:
                f.write(jsonl({"type": "user", "cwd": home_cwd}))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def add_config(self, cwd, **fields):
        entry = {"allowedTools": [], "hasTrustDialogAccepted": True}
        entry.update(fields)
        self.config["projects"][cwd] = entry

    def add_history(self, cwd, display="do a thing", session=None):
        entry = {"display": display, "pastedContents": {}, "project": cwd}
        if session is not None:
            entry["sessionId"] = session
        self.history.append(entry)

    def add_live_session(self, cwd, pid):
        d = os.path.join(self.profile, "sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{pid}.json"), "w") as f:
            json.dump({"cwd": cwd, "pid": pid}, f)

    def write_fixture(self):
        with open(os.path.join(self.profile, ".claude.json"), "w") as f:
            json.dump(self.config, f, indent=2)
        with open(os.path.join(self.profile, "history.jsonl"), "w") as f:
            for obj in self.history:
                f.write(jsonl(obj))

    # -- runner + assertions ------------------------------------------------

    def run_mv(self, *args, expect=0, stdin="", home=None, env_extra=None,
               cwd=None):
        env = dict(os.environ, CLAUDE_MV_RESTORE_ROOT=self.restore_root)
        env.pop("CLAUDE_MV_FORCE_PROMPT", None)
        # A ccfind on the developer's machine must not decide what the suite
        # tests: every session test pins its own source explicitly.
        for k in ("CLAUDE_MV_SOURCE", "CLAUDE_MV_PICKER",
                  "CLAUDE_MV_CCFIND_SOURCE", "CLAUDE_MV_CCFIND_BIN"):
            env.pop(k, None)
        if home:                      # for the ~ expansion test
            env["HOME"] = home
        env.update(env_extra or {})
        r = subprocess.run(
            [sys.executable, SCRIPT, "--profile", self.profile, *args],
            capture_output=True, text=True, input=stdin, env=env, cwd=cwd)
        self.assertEqual(
            r.returncode, expect,
            f"exit {r.returncode} != {expect}\n--- stdout ---\n{r.stdout}\n"
            f"--- stderr ---\n{r.stderr}")
        return r

    def read_config(self):
        with open(os.path.join(self.profile, ".claude.json")) as f:
            return json.load(f)

    def read_history(self):
        p = os.path.join(self.profile, "history.jsonl")
        with open(p) as f:
            return [json.loads(line) for line in f if line.strip()]

    def project_dirs(self):
        return sorted(os.listdir(self.projects))

    def session_cwds(self, cwd):
        """Every top-level cwd recorded in projects/<enc(cwd)>/*.jsonl."""
        d = os.path.join(self.projects, cm.enc(cwd))
        out = []
        for name in sorted(os.listdir(d)):
            if not name.endswith(".jsonl"):
                continue
            with open(os.path.join(d, name)) as f:
                for line in f:
                    obj = json.loads(line)
                    if "cwd" in obj:
                        out.append(obj["cwd"])
        return out

    def restore_stamps(self):
        if not os.path.isdir(self.restore_root):
            return []
        return sorted(os.listdir(self.restore_root))


# ── end-to-end: the plain move (regression cover for the existing path) ─────

class TestPlainMove(FixtureCase):
    def test_move_rekeys_everything(self):
        old = self.make_folder("old-name")
        new = os.path.join(self.code, "new-name")
        self.make_project(old)
        self.add_config(old)
        self.add_history(old)
        self.write_fixture()

        self.run_mv(old, new)

        self.assertTrue(os.path.isdir(new))
        self.assertFalse(os.path.exists(old))
        self.assertEqual(self.project_dirs(), [cm.enc(new)])
        self.assertEqual(self.session_cwds(new), [new])
        self.assertEqual(list(self.read_config()["projects"]), [new])
        self.assertEqual(self.read_history()[0]["project"], new)
        self.assertEqual(self.restore_stamps(), [])  # cleaned up on success

    def test_nested_project_moves_and_sibling_is_untouched(self):
        old = self.make_folder("proj")
        nested = os.path.join(old, "sub")
        os.makedirs(nested)
        sibling = self.make_folder("proj-other")  # encodes like proj/other
        new = os.path.join(self.code, "renamed")

        self.make_project(old)
        self.make_project(nested, sessions=("bbbb-2222",))
        self.make_project(sibling, sessions=("cccc-3333",))
        self.write_fixture()

        self.run_mv(old, new)

        self.assertIn(cm.enc(new), self.project_dirs())
        self.assertIn(cm.enc(os.path.join(new, "sub")), self.project_dirs())
        self.assertIn(cm.enc(sibling), self.project_dirs())  # never touched
        self.assertEqual(self.session_cwds(sibling), [sibling])

    def test_mv_into_existing_dir_uses_mv_semantics(self):
        old = self.make_folder("proj")
        archive = os.path.join(self.tmp, "archive")
        os.makedirs(archive)
        self.make_project(old)
        self.write_fixture()

        self.run_mv(old, archive)

        landed = os.path.join(archive, "proj")
        self.assertTrue(os.path.isdir(landed))
        self.assertEqual(self.project_dirs(), [cm.enc(landed)])

    def test_dry_run_changes_nothing(self):
        old = self.make_folder("old")
        new = os.path.join(self.code, "new")
        self.make_project(old)
        self.add_config(old)
        self.write_fixture()

        r = self.run_mv("-n", old, new)

        self.assertIn("would", r.stdout)
        self.assertTrue(os.path.isdir(old))
        self.assertFalse(os.path.exists(new))
        self.assertEqual(self.project_dirs(), [cm.enc(old)])
        self.assertEqual(list(self.read_config()["projects"]), [old])
        self.assertEqual(self.restore_stamps(), [])

    def test_live_session_blocks_without_force(self):
        old = self.make_folder("old")
        new = os.path.join(self.code, "new")
        self.make_project(old)
        self.add_live_session(old, os.getpid())  # this test process = alive
        self.write_fixture()

        r = self.run_mv(old, new, expect=1)
        self.assertIn("live Claude session", r.stderr)
        self.assertTrue(os.path.isdir(old))

        self.run_mv("--force", old, new)
        self.assertTrue(os.path.isdir(new))

    def test_the_guard_rails_refuse_before_anything_moves(self):
        """The folder move's four refusals. Each was reachable and none was
        exercised — a coverage sweep found them, so they are pinned now.
        Grouped because they share one shape: report, change nothing, exit 1.
        """
        old = self.make_folder("old")
        self.make_project(old)
        self.write_fixture()

        # A path that exists but is not a directory: mv-into-dir does not
        # apply, so it lands on the collision check rather than becoming
        # `<dir>/old`, which is what an existing *directory* correctly does.
        occupied = os.path.join(self.code, "occupied")
        with open(occupied, "w") as f:
            f.write("in the way")

        cases = [
            ((old, occupied), "destination exists"),
            ((old, os.path.join(old, "inside")), "into itself"),
            ((old, os.path.join(self.tmp, "no-such-parent", "x")),
             "no such directory"),
        ]
        for args, expected in cases:
            r = self.run_mv(*args, expect=1)
            self.assertIn(expected, r.stderr, args)
            self.assertTrue(os.path.isdir(old), "src moved despite refusing")
        self.assertEqual(self.restore_stamps(), [])

    def test_missing_src_hints_at_already_moved(self):
        r = self.run_mv(os.path.join(self.code, "gone"),
                        os.path.join(self.code, "new"), expect=1)
        self.assertIn("--already-moved", r.stderr)


# ── the offer: a folder that is already gone ────────────────────────────────

class TestOfferedReconcile(FixtureCase):
    """`claude-mv old new` when `old` is already gone and `new` is there.

    The plain move and --already-moved are one migration with different
    amounts of it already done, and the disk knows which. Rather than refuse
    and name a flag, the run says what is stranded and asks — so the tool can
    do the whole job or just the half that is left, from the same command.
    """

    PROMPT = {"CLAUDE_MV_FORCE_PROMPT": "1"}

    def setUp(self):
        super().setUp()
        self.old = os.path.join(self.code, "lipsum")     # never created
        self.new = self.make_folder("foo")
        self.make_project(self.old, sessions=("aaaa-1111", "bbbb-2222"))
        self.make_project(os.path.join(self.old, "api"),
                          sessions=("cccc-3333",))
        self.add_config(self.old)
        self.add_history(self.old)
        self.write_fixture()

    def keyed_dirs(self):
        return [d for d in self.project_dirs() if cm.enc(self.new) in d]

    def test_the_offer_says_what_is_stranded(self):
        r = self.run_mv(self.old, self.new, stdin="n\n",
                        env_extra=self.PROMPT, expect=1)
        self.assertIn("looks moved already", r.stdout)
        self.assertIn("2 project dirs", r.stdout)     # lipsum and lipsum/api
        self.assertIn("3 session files", r.stdout)
        self.assertIn("1 config key", r.stdout)
        self.assertIn("1 history entry", r.stdout)

    def test_declining_changes_nothing(self):
        r = self.run_mv(self.old, self.new, stdin="n\n",
                        env_extra=self.PROMPT, expect=1)
        self.assertIn("cancelled", r.stdout)
        self.assertEqual(self.keyed_dirs(), [])
        self.assertEqual(self.restore_stamps(), [])

    def test_accepting_re_keys_the_history_and_moves_no_folder(self):
        self.run_mv(self.old, self.new, stdin="y\n", env_extra=self.PROMPT)
        self.assertEqual(sorted(self.keyed_dirs()),
                         sorted([cm.enc(self.new),
                                 cm.enc(os.path.join(self.new, "api"))]))
        self.assertIn(self.new, self.read_config()["projects"])
        self.assertFalse(os.path.exists(self.old))    # nothing was created
        self.assertTrue(os.path.isdir(self.new))

    def test_end_of_input_is_not_consent(self):
        self.run_mv(self.old, self.new, env_extra=self.PROMPT, expect=1)
        self.assertEqual(self.keyed_dirs(), [])

    def test_a_pipe_is_told_what_to_say_rather_than_guessed_at(self):
        """No tty is nobody to ask, and re-keying history onto a path nobody
        confirmed is the one thing this tool will not do quietly. The stderr
        half has to carry the finding: a run redirected into a log is one
        where stdout is not read."""
        r = self.run_mv(self.old, self.new, expect=1)
        self.assertIn("confirmation needed", r.stderr)
        self.assertIn("3 session files", r.stderr)
        self.assertIn(f"--already-moved {self.old} {self.new}", r.stderr)
        self.assertEqual(self.keyed_dirs(), [])

    def test_force_is_taken_as_the_answer(self):
        self.run_mv("--force", self.old, self.new)
        self.assertIn(cm.enc(self.new), self.project_dirs())

    def test_a_dry_run_previews_it_without_asking(self):
        r = self.run_mv("-n", self.old, self.new, env_extra=self.PROMPT)
        self.assertIn("dry run", r.stdout)
        self.assertNotIn("[y/N]", r.stdout)
        self.assertEqual(self.keyed_dirs(), [])

    def test_nothing_stranded_is_a_typo_not_an_offer(self):
        """A src that never had history is a mistyped path. Offering to
        migrate nothing would dress that up as a plan."""
        r = self.run_mv(os.path.join(self.code, "never-existed"), self.new,
                        env_extra=self.PROMPT, expect=1)
        self.assertIn("no Claude history is keyed on", r.stderr)
        self.assertNotIn("[y/N]", r.stdout)

    def test_with_no_destination_either_it_is_just_a_missing_folder(self):
        """The offer needs somewhere to re-key ONTO. Without that there is
        nothing to propose, so the old hint stands."""
        r = self.run_mv(self.old, os.path.join(self.code, "nope"),
                        env_extra=self.PROMPT, expect=1)
        self.assertIn("--already-moved", r.stderr)
        self.assertNotIn("looks moved already", r.stdout)

    def test_a_folder_moved_INTO_a_directory_is_read_that_way(self):
        """`mv old somewhere/` and `mv old new` are the same command with
        different intent, and once old is gone only the disk can say which
        happened. A folder of src's name sitting inside dst is the mv-into
        reading — and the path the history has to land on."""
        archive = self.make_folder("archive")
        landed = os.path.join(archive, "lipsum")
        os.makedirs(landed)
        r = self.run_mv(self.old, archive, stdin="y\n", env_extra=self.PROMPT)
        self.assertIn(landed, r.stdout)
        self.assertIn(cm.enc(landed), self.project_dirs())
        self.assertNotIn(cm.enc(archive), self.project_dirs())

    def test_the_flag_still_takes_the_destination_as_typed(self):
        """--already-moved is the user saying which reading is right, so the
        mv-into guess must not second-guess them."""
        os.makedirs(os.path.join(self.new, "lipsum"))
        self.run_mv("--already-moved", self.old, self.new)
        self.assertIn(cm.enc(self.new), self.project_dirs())
        self.assertNotIn(cm.enc(os.path.join(self.new, "lipsum")),
                         self.project_dirs())


# ── end-to-end: how src/dst are spelled ─────────────────────────────────────

class TestFolderConflicts(FixtureCase):
    """The folder move's conflict policies, end to end. The prompt itself is
    unit-tested above; these are the two answers that change what happens on
    disk, plus the preview a dry run shows when no policy was given."""

    def _conflicting(self):
        old = self.make_folder("old")
        new = os.path.join(self.code, "new")
        self.make_project(old, sessions=("aaaa-1111",))
        self.make_project(new, sessions=("dddd-4444",))
        self.add_config(old)
        self.add_config(new)
        self.write_fixture()
        return old, new

    def test_abort_really_means_nothing_happened(self):
        """The claim the whole up-front conflict detection exists to make."""
        old, new = self._conflicting()
        before = sorted(os.listdir(self.projects))

        r = self.run_mv("--on-conflict", "abort", old, new, expect=1)

        self.assertIn("aborted", r.stdout)
        self.assertTrue(os.path.isdir(old), "the folder moved despite abort")
        self.assertFalse(os.path.exists(new))
        self.assertEqual(sorted(os.listdir(self.projects)), before)
        self.assertEqual(self.restore_stamps(), [])

    def test_rename_only_moves_the_folder_and_leaves_history_alone(self):
        old, new = self._conflicting()
        before = sorted(os.listdir(self.projects))

        r = self.run_mv("--on-conflict", "rename-only", old, new)

        self.assertIn("rename only", r.stdout)
        self.assertTrue(os.path.isdir(new), "the folder did not move")
        self.assertFalse(os.path.exists(old))
        self.assertEqual(sorted(os.listdir(self.projects)), before)
        self.assertEqual(self.restore_stamps(), [])

    def test_consolidate_keeps_both_when_a_session_file_collides(self):
        """Same session id on both sides. Merging cannot pick a winner, so it
        keeps the destination's and says the source copy was left behind —
        rather than silently overwriting one conversation with another."""
        old = self.make_folder("old")
        new = os.path.join(self.code, "new")
        self.make_project(old, sessions=("aaaa-1111",))
        self.make_project(new, sessions=("aaaa-1111",))
        self.write_fixture()

        r = self.run_mv("--on-conflict", "consolidate", old, new)

        self.assertIn("keep both", r.stderr)
        self.assertIn("not empty after merge", r.stderr)
        # the source dir survives, still holding the copy that could not move
        self.assertIn(cm.enc(old), self.project_dirs())

    def test_a_failure_midway_through_a_folder_move_exits_3(self):
        """The folder move's half of the promise --extract's twin makes: the
        restore point outlives the failure, and the error names it."""
        old = self.make_folder("old")
        new = os.path.join(self.code, "new")
        self.make_project(old, sessions=("aaaa-1111",))
        self.add_config(old)
        self.write_fixture()
        # The projects/ dir the migration must create, made unwritable so the
        # rename into it fails after the folder itself has already moved.
        os.makedirs(self.projects, exist_ok=True)
        os.chmod(self.projects, 0o500)
        self.addCleanup(os.chmod, self.projects, 0o700)

        r = self.run_mv(old, new, expect=3)

        self.assertIn("migration failed midway", r.stderr)
        stamps = self.restore_stamps()
        self.assertEqual(len(stamps), 1)
        self.assertIn(stamps[0], r.stderr)

    def test_a_dry_run_with_no_policy_previews_the_safest_one(self):
        """Nobody can be asked in a dry run that was piped, and refusing
        would make -n useless exactly when it is most wanted — so it shows
        what the least destructive policy would do, and says which."""
        old, new = self._conflicting()

        r = self.run_mv("-n", old, new)

        self.assertIn("previewing --on-conflict consolidate", r.stdout)
        self.assertTrue(os.path.isdir(old))
        self.assertEqual(self.restore_stamps(), [])


class TestPathForms(FixtureCase):
    """Every spelling of the same folder must migrate the same history.

    The fixture always records the canonical cwd (that is what Claude
    writes); only the argv spelling varies.
    """

    def _fixture(self, name="proj"):
        old = self.make_folder(name)
        self.make_project(old)
        self.add_config(old)
        self.add_history(old)
        self.write_fixture()
        return old, os.path.join(self.code, "renamed")

    def _assert_migrated(self, old, new):
        self.assertTrue(os.path.isdir(new))
        self.assertFalse(os.path.exists(old))
        self.assertEqual(self.project_dirs(), [cm.enc(new)])
        self.assertEqual(self.session_cwds(new), [new])
        self.assertEqual(list(self.read_config()["projects"]), [new])
        self.assertEqual(self.read_history()[0]["project"], new)

    def test_trailing_slashes(self):
        old, new = self._fixture()
        self.run_mv(old + "/", new + "/")
        self._assert_migrated(old, new)

    def test_relative_paths(self):
        old, new = self._fixture()
        cwd = os.getcwd()
        os.chdir(self.code)
        self.addCleanup(os.chdir, cwd)
        self.run_mv("proj", "renamed")
        self._assert_migrated(old, new)

    def test_dot_dot_segments(self):
        old, new = self._fixture()
        self.run_mv(os.path.join(self.code, "..", "code", "proj"), new)
        self._assert_migrated(old, new)

    def test_tilde(self):
        # HOME is redirected at self.tmp, so ~ lands in the fixture tree.
        old, new = self._fixture()
        r = self.run_mv("~/code/proj", "~/code/renamed", home=self.tmp)
        self.assertNotIn("no Claude project history", r.stdout)
        self._assert_migrated(old, new)

    def test_symlinked_ancestor_still_finds_the_history(self):
        # The regression this whole class exists for: reached via a symlinked
        # parent, the encoding used to miss and the move silently migrated
        # nothing.
        old, new = self._fixture()
        link = os.path.join(self.tmp, "link")
        os.symlink(self.code, link)

        r = self.run_mv(os.path.join(link, "proj"),
                        os.path.join(link, "renamed"))

        self.assertNotIn("no Claude project history", r.stdout)
        self._assert_migrated(old, new)

    def test_symlinked_dst_dir_resolves_for_mv_into(self):
        old, _ = self._fixture()
        archive = os.path.join(self.tmp, "archive")
        os.makedirs(archive)
        link = os.path.join(self.tmp, "archive-link")
        os.symlink(archive, link)

        self.run_mv(old, link)          # mv-into-dir through a symlink

        landed = os.path.join(archive, "proj")   # physical, not via the link
        self.assertEqual(self.project_dirs(), [cm.enc(landed)])
        self.assertEqual(self.session_cwds(landed), [landed])

    def test_src_that_is_itself_a_symlink_warns_and_leaves_history(self):
        old, _ = self._fixture()
        link = os.path.join(self.code, "proj-link")
        os.symlink(old, link)

        r = self.run_mv(link, os.path.join(self.code, "moved-link"))

        self.assertIn("is a symlink", r.stdout)
        self.assertTrue(os.path.islink(os.path.join(self.code, "moved-link")))
        self.assertTrue(os.path.isdir(old))          # real folder stayed
        self.assertEqual(self.project_dirs(), [cm.enc(old)])  # history stayed

    def test_folder_with_no_history_warns_but_still_moves(self):
        self.write_fixture()
        plain = self.make_folder("no-sessions-here")
        new = os.path.join(self.code, "renamed")

        r = self.run_mv(plain, new)

        self.assertIn("no Claude project history", r.stdout)
        self.assertTrue(os.path.isdir(new))          # the mv is not blocked


# ── end-to-end: --already-moved ─────────────────────────────────────────────

class TestAlreadyMoved(FixtureCase):
    def test_rekeys_without_touching_the_folder(self):
        old = os.path.join(self.code, "pilot-name")   # never created on disk
        new = self.make_folder("real-name")
        self.make_project(old)
        self.add_config(old)
        self.add_history(old)
        self.write_fixture()
        before = os.listdir(new)

        self.run_mv("--already-moved", old, new)

        self.assertEqual(os.listdir(new), before)     # folder untouched
        self.assertFalse(os.path.exists(old))
        self.assertEqual(self.project_dirs(), [cm.enc(new)])
        self.assertEqual(self.session_cwds(new), [new])
        self.assertEqual(list(self.read_config()["projects"]), [new])
        self.assertEqual(self.read_history()[0]["project"], new)
        self.assertEqual(self.restore_stamps(), [])

    def test_nested_projects_follow(self):
        old = os.path.join(self.code, "pilot")
        new = self.make_folder("final")
        os.makedirs(os.path.join(new, "sub"))
        self.make_project(old)
        self.make_project(os.path.join(old, "sub"), sessions=("bbbb-2222",))
        self.write_fixture()

        self.run_mv("--already-moved", old, new)

        self.assertEqual(self.project_dirs(),
                         sorted([cm.enc(new), cm.enc(os.path.join(new, "sub"))]))
        self.assertEqual(self.session_cwds(os.path.join(new, "sub")),
                         [os.path.join(new, "sub")])

    def test_consolidate_merges_the_stub_history_made_after_the_rename(self):
        # The realistic case: you kept working in the renamed folder, so the
        # new path already has sessions + a config entry of its own.
        old = os.path.join(self.code, "pilot")
        new = self.make_folder("final")
        self.make_project(old, sessions=("aaaa-1111", "aaaa-2222"))
        self.make_project(new, sessions=("dddd-4444",))
        self.add_config(old, allowedTools=["Bash"], hasTrustDialogAccepted=True)
        self.add_config(new, allowedTools=["Read"], hasTrustDialogAccepted=False)
        self.write_fixture()

        self.run_mv("--already-moved", "--on-conflict", "consolidate", old, new)

        self.assertEqual(self.project_dirs(), [cm.enc(new)])
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.projects, cm.enc(new)))),
            ["aaaa-1111.jsonl", "aaaa-2222.jsonl", "dddd-4444.jsonl"])
        self.assertEqual(self.session_cwds(new), [new, new, new])
        entry = self.read_config()["projects"][new]
        self.assertEqual(sorted(entry["allowedTools"]), ["Bash", "Read"])
        self.assertTrue(entry["hasTrustDialogAccepted"])  # booleans OR

    def test_overwrite_discards_destination_and_keeps_restore_point(self):
        old = os.path.join(self.code, "pilot")
        new = self.make_folder("final")
        self.make_project(old, sessions=("aaaa-1111",))
        self.make_project(new, sessions=("dddd-4444",))
        self.write_fixture()

        r = self.run_mv("--already-moved", "--on-conflict", "overwrite",
                        old, new)

        self.assertEqual(os.listdir(os.path.join(self.projects, cm.enc(new))),
                         ["aaaa-1111.jsonl"])
        self.assertIn("restore point", r.stdout)
        self.assertEqual(len(self.restore_stamps()), 1)  # kept as the archive

    def test_refuses_when_old_path_still_exists(self):
        old = self.make_folder("still-here")
        new = self.make_folder("new")
        self.make_project(old)
        self.write_fixture()

        r = self.run_mv("--already-moved", old, new, expect=1)
        self.assertIn("still exists", r.stderr)
        self.assertEqual(self.project_dirs(), [cm.enc(old)])

    def test_refuses_when_new_path_missing(self):
        old = os.path.join(self.code, "pilot")
        new = os.path.join(self.code, "nope")
        self.make_project(old)
        self.write_fixture()

        r = self.run_mv("--already-moved", old, new, expect=1)
        self.assertIn("not a directory", r.stderr)

    def test_refuses_when_the_two_paths_are_the_same(self):
        """Refused — though by the "old path still exists" guard rather than
        by the same-path one, which cannot be reached here: getting that far
        needs dst present and src gone, so the two can never be equal. What
        matters to a user is that it stops and says why."""
        new = self.make_folder("same")
        self.make_project(new)
        self.write_fixture()
        r = self.run_mv("--already-moved", new, new, expect=1)
        self.assertIn("old path still exists", r.stderr)
        self.assertEqual(self.restore_stamps(), [])

    def test_refuses_when_no_history_matches_the_old_path(self):
        new = self.make_folder("final")
        self.write_fixture()

        r = self.run_mv("--already-moved", os.path.join(self.code, "typo"),
                        new, expect=1)
        self.assertIn("no Claude history", r.stderr)

    def test_rejects_rename_only(self):
        old = os.path.join(self.code, "pilot")
        new = self.make_folder("final")
        self.make_project(old)
        self.write_fixture()

        r = self.run_mv("--already-moved", "--on-conflict", "rename-only",
                        old, new, expect=2)
        self.assertIn("rename-only", r.stderr)

    def test_conflicts_without_tty_exit_2_and_change_nothing(self):
        old = os.path.join(self.code, "pilot")
        new = self.make_folder("final")
        self.make_project(old)
        self.make_project(new, sessions=("dddd-4444",))
        self.write_fixture()

        r = self.run_mv("--already-moved", old, new, expect=2)
        self.assertIn("--on-conflict", r.stderr)
        self.assertNotIn("rename-only", r.stderr)  # not offered in this mode
        self.assertEqual(self.project_dirs(),
                         sorted([cm.enc(old), cm.enc(new)]))

    def test_dry_run_changes_nothing(self):
        old = os.path.join(self.code, "pilot")
        new = self.make_folder("final")
        self.make_project(old)
        self.add_config(old)
        self.write_fixture()

        self.run_mv("-n", "--already-moved", old, new)

        self.assertEqual(self.project_dirs(), [cm.enc(old)])
        self.assertEqual(list(self.read_config()["projects"]), [old])
        self.assertEqual(self.restore_stamps(), [])

    def test_live_session_in_destination_blocks(self):
        # Unique to this mode: the folder is already renamed, so a session
        # running in it is writing to a project dir we may merge into.
        old = os.path.join(self.code, "pilot")
        new = self.make_folder("final")
        self.make_project(old)
        self.add_live_session(new, os.getpid())
        self.write_fixture()

        r = self.run_mv("--already-moved", old, new, expect=1)
        self.assertIn("live Claude session", r.stderr)


# ── end-to-end: restore points ──────────────────────────────────────────────

class TestRestore(FixtureCase):
    def _snapshot(self):
        """Everything the migration can touch, as comparable plain data."""
        snap = {"dirs": {}, "config": self.read_config(),
                "history": self.read_history()}
        for name in self.project_dirs():
            d = os.path.join(self.projects, name)
            snap["dirs"][name] = {}
            for fn in sorted(os.listdir(d)):
                with open(os.path.join(d, fn)) as f:
                    snap["dirs"][name][fn] = f.read()
        return snap

    def test_restore_after_plain_move_puts_the_folder_back(self):
        old = self.make_folder("old")
        new = os.path.join(self.code, "new")
        self.make_project(old, sessions=("aaaa-1111", "aaaa-2222"))
        self.make_project(new, sessions=("dddd-4444",))  # forces a conflict
        self.add_config(old)
        self.add_history(old)
        self.write_fixture()
        before = self._snapshot()

        self.run_mv("--on-conflict", "overwrite", old, new)
        self.assertTrue(os.path.isdir(new))
        stamps = self.restore_stamps()
        self.assertEqual(len(stamps), 1)

        self.run_mv("--restore", stamps[0], "--force")

        self.assertTrue(os.path.isdir(old))       # folder moved back
        self.assertFalse(os.path.exists(new))
        self.assertEqual(self._snapshot(), before)  # byte-exact round trip
        self.assertEqual(self.restore_stamps(), [])

    def test_restore_after_already_moved_leaves_the_folder_alone(self):
        old = os.path.join(self.code, "pilot")
        new = self.make_folder("final")
        self.make_project(old, sessions=("aaaa-1111",))
        self.make_project(new, sessions=("dddd-4444",))
        self.add_config(old)
        self.add_config(new)
        self.add_history(old)
        self.write_fixture()
        before = self._snapshot()

        self.run_mv("--already-moved", "--on-conflict", "overwrite", old, new)
        stamps = self.restore_stamps()
        self.assertEqual(len(stamps), 1)

        r = self.run_mv("--restore", stamps[0], "--force")

        self.assertIn("no folder move to undo", r.stdout)
        self.assertTrue(os.path.isdir(new))        # never moved back
        self.assertFalse(os.path.exists(old))      # never recreated
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.restore_stamps(), [])

    def test_restore_asks_before_it_rolls_anything_back(self):
        """--restore rewrites history in the other direction, so it confirms.
        Without a tty and without --force there is nobody to ask, and it says
        so rather than assuming yes."""
        old = self.make_folder("old")
        new = os.path.join(self.code, "new")
        self.make_project(old)
        self.make_project(new, sessions=("dddd-4444",))
        self.write_fixture()
        self.run_mv("--on-conflict", "overwrite", old, new)
        stamp = self.restore_stamps()[0]

        r = self.run_mv("--restore", stamp, expect=2)
        self.assertIn("confirmation needed", r.stderr)
        self.assertTrue(os.path.isdir(new), "restored without being asked")
        self.assertEqual(self.restore_stamps(), [stamp])

    def test_declining_the_restore_prompt_changes_nothing(self):
        old = self.make_folder("old")
        new = os.path.join(self.code, "new")
        self.make_project(old)
        self.make_project(new, sessions=("dddd-4444",))
        self.write_fixture()
        self.run_mv("--on-conflict", "overwrite", old, new)
        stamp = self.restore_stamps()[0]

        r = self.run_mv("--restore", stamp, stdin="n\n", expect=1,
                        env_extra={"CLAUDE_MV_FORCE_PROMPT": "1"})
        self.assertIn("aborted", r.stdout)
        self.assertTrue(os.path.isdir(new))
        self.assertEqual(self.restore_stamps(), [stamp])

    def test_an_unknown_restore_point_is_named_not_guessed(self):
        old = self.make_folder("old")
        new = os.path.join(self.code, "new")
        self.make_project(old)
        self.make_project(new, sessions=("dddd-4444",))
        self.write_fixture()
        self.run_mv("--on-conflict", "overwrite", old, new)

        r = self.run_mv("--restore", "20990101-000000", "--force", expect=1)
        self.assertIn("no restore point", r.stderr)

    def test_restore_with_nothing_to_restore_says_so(self):
        r = self.run_mv("--restore", expect=1)
        self.assertIn("no restore points", r.stderr)

    def test_restore_list(self):
        old = os.path.join(self.code, "pilot")
        new = self.make_folder("final")
        self.make_project(old)
        self.make_project(new, sessions=("dddd-4444",))
        self.write_fixture()
        self.run_mv("--already-moved", "--on-conflict", "overwrite", old, new)

        r = self.run_mv("--restore")
        self.assertIn(self.restore_stamps()[0], r.stdout)
        self.assertIn("overwrite", r.stdout)


# ── multi-profile: is every configured profile actually migrated? ───────────

class TestMultiProfile(unittest.TestCase):
    """Several profiles in one run.

    FixtureCase above passes exactly one --profile, so nothing there can see a
    second one being skipped, half-migrated, or written into the wrong file —
    and "across every configured profile" is a claim the README makes.

    Laid out the way a real machine is, because the shapes differ: $HOME/.claude
    keeps its config at $HOME/.claude.json, while every other profile keeps its
    own inside the profile dir (the CLAUDE_CONFIG_DIR layout). A tool that wrote
    both to one place would still pass a single-profile fixture.
    """

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="claude-mv-multi-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.code = os.path.join(self.home, "code")
        os.makedirs(self.code)
        self.restore_root = os.path.join(self.tmp, "restore")

    # -- fixture ------------------------------------------------------------

    def profile(self, name):
        d = os.path.join(self.home, name)
        os.makedirs(os.path.join(d, "projects"))
        return d

    def cfg_path(self, profile):
        """Mirror of claude-mv's own rule, computed here rather than imported:
        if the test agreed with the tool by construction it could not catch the
        tool moving the goalposts."""
        return (os.path.join(self.home, ".claude.json")
                if profile == os.path.join(self.home, ".claude")
                else os.path.join(profile, ".claude.json"))

    def folder(self, name):
        p = os.path.join(self.code, name)
        os.makedirs(p, exist_ok=True)
        return p

    def seed(self, profile, cwd, sessions=("s1",)):
        """Key `cwd` into all four of this profile's stores."""
        d = os.path.join(profile, "projects", cm.enc(cwd))
        os.makedirs(d, exist_ok=True)
        for sid in sessions:
            with open(os.path.join(d, sid + ".jsonl"), "w") as f:
                f.write(json.dumps({"type": "user", "cwd": cwd,
                                    "sessionId": sid}) + "\n")
        cfg = self.cfg_path(profile)
        data = {"projects": {}}
        if os.path.isfile(cfg):
            with open(cfg) as f:
                data = json.load(f)
        data["projects"][cwd] = {"allowedTools": [],
                                 "hasTrustDialogAccepted": True}
        with open(cfg, "w") as f:
            json.dump(data, f)
        with open(os.path.join(profile, "history.jsonl"), "a") as f:
            f.write(json.dumps({"display": "hi", "project": cwd}) + "\n")

    def run_mv(self, profiles, *args, expect=0):
        prof = [a for p in profiles for a in ("--profile", p)]
        env = dict(os.environ, HOME=self.home,
                   CLAUDE_MV_RESTORE_ROOT=self.restore_root)
        env.pop("CLAUDE_MV_FORCE_PROMPT", None)
        r = subprocess.run([sys.executable, SCRIPT, *prof, *args],
                           capture_output=True, text=True, env=env)
        self.assertEqual(
            r.returncode, expect,
            "exit %s != %s\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (r.returncode, expect, r.stdout, r.stderr))
        return r

    # -- readers ------------------------------------------------------------

    def project_dirs(self, profile):
        return sorted(os.listdir(os.path.join(profile, "projects")))

    def keys(self, profile):
        with open(self.cfg_path(profile)) as f:
            return sorted(json.load(f)["projects"])

    def history_projects(self, profile):
        with open(os.path.join(profile, "history.jsonl")) as f:
            return [json.loads(line)["project"] for line in f if line.strip()]

    def session_cwds(self, profile):
        out = []
        root = os.path.join(profile, "projects")
        for d in sorted(os.listdir(root)):
            for name in sorted(os.listdir(os.path.join(root, d))):
                with open(os.path.join(root, d, name)) as f:
                    for line in f:
                        cwd = json.loads(line).get("cwd")
                        if cwd:
                            out.append(cwd)
        return out

    def snapshot(self, root):
        """Every file under `root` as path → bytes, for proving non-interference."""
        out = {}
        for dirpath, _, names in os.walk(root):
            for n in names:
                p = os.path.join(dirpath, n)
                with open(p, "rb") as f:
                    out[os.path.relpath(p, root)] = f.read()
        return out

    # -- tests --------------------------------------------------------------

    def test_every_profile_is_migrated(self):
        work, personal = self.profile(".claude"), self.profile(".claude-personal")
        old, new = self.folder("lipsum"), os.path.join(self.code, "foo")
        self.seed(work, old, sessions=("s1", "s2"))
        self.seed(personal, old)

        self.run_mv([work, personal], old, new)

        for p in (work, personal):
            self.assertEqual(self.project_dirs(p), [cm.enc(new)], p)
            self.assertEqual(self.keys(p), [new], p)
            self.assertEqual(self.history_projects(p), [new], p)
            self.assertEqual(set(self.session_cwds(p)), {new}, p)
        # Each profile's key landed in ITS OWN config file, not one shared one.
        self.assertEqual(self.keys(work), [new])
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".claude.json")))
        self.assertTrue(os.path.isfile(os.path.join(personal, ".claude.json")))
        self.assertFalse(os.path.exists(os.path.join(work, ".claude.json")))

    def test_a_profile_that_never_saw_the_folder_is_left_byte_identical(self):
        work, personal = self.profile(".claude"), self.profile(".claude-personal")
        old, new = self.folder("lipsum"), os.path.join(self.code, "foo")
        self.seed(work, old)
        self.seed(personal, self.folder("unrelated"))

        before = self.snapshot(personal)
        self.run_mv([work, personal], old, new)

        self.assertEqual(self.project_dirs(work), [cm.enc(new)])
        self.assertEqual(self.snapshot(personal), before)

    def test_one_policy_resolves_each_profile_on_its_own_terms(self):
        """A conflict in one profile does not change what happens in another.

        `overwrite` is chosen once for the run, but only `work` has history at
        the destination; `personal` has a plain rename to do and must still do
        exactly that.
        """
        work, personal = self.profile(".claude"), self.profile(".claude-personal")
        old, new = self.folder("lipsum"), os.path.join(self.code, "foo")
        self.seed(work, old, sessions=("moved",))
        self.seed(work, new, sessions=("doomed",))   # destination history
        self.seed(personal, old, sessions=("mine",))

        self.run_mv([work, personal], "--on-conflict", "overwrite", old, new)

        self.assertEqual(self.project_dirs(work), [cm.enc(new)])
        self.assertEqual(
            sorted(os.listdir(os.path.join(work, "projects", cm.enc(new)))),
            ["moved.jsonl"], "the destination's own history should be gone")
        self.assertEqual(self.project_dirs(personal), [cm.enc(new)])
        self.assertEqual(
            sorted(os.listdir(os.path.join(personal, "projects", cm.enc(new)))),
            ["mine.jsonl"])

    def test_nothing_keyed_anywhere_warns_instead_of_claiming_success(self):
        work, personal = self.profile(".claude"), self.profile(".claude-personal")
        old, new = self.folder("lipsum"), os.path.join(self.code, "foo")
        self.seed(work, self.folder("elsewhere"))

        r = self.run_mv([work, personal], "-n", old, new)
        self.assertIn("no Claude project history", r.stdout)


# ── unit: choosing sessions ─────────────────────────────────────────────────

class TestSelectionParsing(unittest.TestCase):
    """The numbered picker's input. Strict on purpose: a half-understood
    selection re-homes the wrong session's history, and there is always
    another prompt to be had."""

    def test_the_forms_a_person_would_type(self):
        self.assertEqual(cm.parse_selection("1", 3), [0])
        self.assertEqual(cm.parse_selection("1,3", 3), [0, 2])
        self.assertEqual(cm.parse_selection("2-4", 5), [1, 2, 3])
        self.assertEqual(cm.parse_selection("1 3", 3), [0, 2])
        self.assertEqual(cm.parse_selection("all", 3), [0, 1, 2])
        self.assertEqual(cm.parse_selection(" ALL ", 2), [0, 1])

    def test_a_repeat_selects_once(self):
        self.assertEqual(cm.parse_selection("2,2,1", 3), [1, 0])

    def test_anything_out_of_range_or_unparseable_is_refused(self):
        for bad in ("0", "4", "1-9", "3-1", "", "  ", "x", "1,x", "1-", "-2",
                    "1..2"):
            self.assertIsNone(cm.parse_selection(bad, 3), bad)

    def test_a_snippet_is_one_line_and_bounded(self):
        """Picker rows are a column; an unbounded or multi-line prompt would
        wreck the alignment of every row under it."""
        tmp = tempfile.mkdtemp(prefix="snip-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        p = os.path.join(tmp, "s.jsonl")
        with open(p, "w") as f:
            f.write(json.dumps({"type": "summary", "summary": "skip me"}) + "\n")
            f.write(json.dumps({"type": "user", "message": {
                "role": "user", "content": "first\nline   and   more " * 40}}) + "\n")
        out = cm.session_snippet(p, limit=40)
        self.assertNotIn("\n", out)
        self.assertLessEqual(len(out), 40)
        self.assertTrue(out.startswith("first line and more"))

    def _snip(self, *turns):
        tmp = tempfile.mkdtemp(prefix="snip-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        p = os.path.join(tmp, "s.jsonl")
        with open(p, "w") as f:
            for content in turns:
                f.write(jsonl({"type": "user",
                               "message": {"role": "user", "content": content}}))
        return cm.session_snippet(p)

    def test_content_is_usually_a_list_of_blocks_not_a_string(self):
        """The shape most real turns have — and the one that would have gone
        untested. On this machine 14127 of 15330 user turns carry a list."""
        self.assertEqual(
            self._snip([{"type": "text", "text": "the typed question"}]),
            "the typed question")

    def test_a_tool_result_turn_is_not_a_prompt(self):
        """Tool output is fed back as a `user` turn, and it is the bulk of
        them — 14049 of 14132 blocks here. A picker row showing a diff or a
        grep result identifies nothing."""
        self.assertEqual(
            self._snip([{"type": "tool_result", "tool_use_id": "x",
                         "content": "0e91feb Fold session row actions"}],
                       [{"type": "text", "text": "the typed question"}]),
            "the typed question")

    def test_only_text_blocks_count_even_if_another_carries_text(self):
        """No block type outside `text` carries a `text` key in the real
        profile today, so the type check is guarding against a format that
        has not arrived. It is still the intent — a thinking or image block
        is not the question someone asked — and pinning it here means the
        day one does arrive, the picker does not start labelling rows with
        the model's internal monologue."""
        self.assertEqual(
            self._snip([{"type": "thinking", "text": "internal reasoning"}],
                       [{"type": "text", "text": "the typed question"}]),
            "the typed question")

    def test_an_interruption_marker_is_not_a_prompt(self):
        self.assertEqual(
            self._snip([{"type": "text", "text": "[Request interrupted by user]"}],
                       [{"type": "text", "text": "the typed question"}]),
            "the typed question")

    def test_a_prompt_that_opens_with_a_pasted_image_is_kept(self):
        """The reason the machine markers are matched by exact prefix rather
        than "starts with a bracket": this is a real question."""
        self.assertEqual(
            self._snip([{"type": "text",
                         "text": "[Image #4] So these are the fields used"}]),
            "[Image #4] So these are the fields used")

    def test_machine_written_user_turns_are_not_mistaken_for_prompts(self):
        """Claude injects caveats, slash-command expansions and command output
        as `user` messages. A picker row labelled "<local-command-caveat>"
        identifies nothing, so keep looking for something a person wrote."""
        tmp = tempfile.mkdtemp(prefix="snip-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        p = os.path.join(tmp, "s.jsonl")
        with open(p, "w") as f:
            for content in ("<local-command-caveat>Caveat: the messages below",
                            "Caveat: The messages below were generated by",
                            "<command-name>/clear</command-name>",
                            "the actual question someone typed"):
                f.write(jsonl({"type": "user",
                               "message": {"role": "user", "content": content}}))
        self.assertEqual(cm.session_snippet(p),
                         "the actual question someone typed")

    def test_a_session_with_nothing_readable_still_gets_a_row(self):
        """It is the id that gets acted on, not the snippet."""
        tmp = tempfile.mkdtemp(prefix="snip-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        p = os.path.join(tmp, "s.jsonl")
        with open(p, "w") as f:
            f.write("not json at all\n")
        self.assertEqual(cm.session_snippet(p), "(no prompt recorded)")
        self.assertEqual(cm.session_snippet(os.path.join(tmp, "gone.jsonl")),
                         "(no prompt recorded)")


# ── the session source seam: ccfind, the walk, and their agreement ──────────

class SessionFixture(FixtureCase):
    """A parent dir holding sessions, one of which spawned a subproject."""

    HERO = "aaaaaaaa-1111-1111-1111-111111111111"   # born in code, cd'd in
    SIBLING = "bbbbbbbb-2222-2222-2222-222222222222"  # must stay behind
    OTHER = "cccccccc-3333-3333-3333-333333333333"    # a third, also staying

    def setUp(self):
        super().setUp()
        self.proj = self.make_folder("newproj")
        self.make_session(self.code, self.HERO, prompt="build me a thing",
                          later_cwd=self.proj, sidecar=True, mtime=3000)
        self.make_session(self.code, self.SIBLING, prompt="unrelated work",
                          mtime=2000)
        self.make_session(self.code, self.OTHER, prompt="also unrelated",
                          mtime=1000)
        self.add_config(self.code)
        self.add_history(self.code, "build me a thing", session=self.HERO)
        self.add_history(self.code, "and now here", session=self.HERO)
        self.add_history(self.code, "unrelated work", session=self.SIBLING)
        self.write_fixture()


CCFIND_STUB = r'''#!/usr/bin/env python3
"""Stand-in for ccfind: answers --json -x -d <dir> from a canned document.

The document is read from $STUB_DOC, so each test can pose the shape it
cares about (a wrong scope, a foreign profile, a truncated answer) without
needing a ccfind that could be talked into producing it.
"""
import json, os, sys
if "--json" not in sys.argv:
    sys.exit(2)
sys.stdout.write(open(os.environ["STUB_DOC"]).read())
'''


class TestSessionSources(FixtureCase):
    """claude-mv accepts ccfind's answer only when it answered OUR question.

    A soft dependency that silently returns a different set depending on what
    is installed is worse than no dependency, so every way ccfind can be
    unusable has to land on the filesystem walk rather than on a wrong list.
    """

    def setUp(self):
        super().setUp()
        self.stub = os.path.join(self.tmp, "ccfind-stub")
        with open(self.stub, "w") as f:
            f.write(CCFIND_STUB)
        os.chmod(self.stub, 0o755)
        self.doc = os.path.join(self.tmp, "doc.json")
        self.sid = "aaaaaaaa-1111-1111-1111-111111111111"
        self.path = self.make_session(self.code, self.sid, mtime=3000)

    def answer(self, **over):
        doc = {"version": 1, "query": "", "scope": self.code,
               "scope_exact": True, "total": 1, "shown": 1, "truncated": False,
               "results": [{"epoch": 3000, "host": "local", "profile": "p",
                            "config_dir": self.profile, "id": self.sid,
                            "cwd": self.code, "mtime": "2026-01-01 00:00:00",
                            "snippet": "from ccfind", "path": self.path}]}
        doc.update(over)
        with open(self.doc, "w") as f:
            json.dump(doc, f)
        return {"CLAUDE_MV_CCFIND_BIN": self.stub, "STUB_DOC": self.doc,
                "CLAUDE_MV_SOURCE": "auto"}

    def rows(self, env):
        """The candidate ids claude-mv ends up with, read off a dry run."""
        r = self.run_mv("--extract", "--no-browse", "-n", "--session", self.sid,
                        self.code, self.proj_path(), env_extra=env,
                        expect=0)
        return r

    def proj_path(self):
        p = os.path.join(self.code, "newproj")
        os.makedirs(p, exist_ok=True)
        return p

    def test_a_usable_answer_is_used(self):
        r = self.rows(self.answer())
        self.assertIn("would re-home", r.stdout)

    def test_an_answer_about_a_different_question_is_refused(self):
        """scope_exact is the compatibility handshake. A ccfind that took -x
        and ignored it would answer about the whole SUBTREE — for ~/code that
        is every sub-repo's sessions, offered up as if they lived here."""
        env = self.answer(scope_exact=False)
        env["CLAUDE_MV_SOURCE"] = "ccfind"
        r = self.run_mv("--extract", "--no-browse", "-n", "--session", self.sid,
                        self.code, self.proj_path(), env_extra=env, expect=1)
        self.assertIn("could not answer", r.stderr)

    def test_a_missing_handshake_is_refused_too(self):
        """A ccfind predating -x that somehow emits JSON has no such field."""
        env = self.answer()
        doc = json.load(open(self.doc))
        del doc["scope_exact"]
        with open(self.doc, "w") as f:
            json.dump(doc, f)
        env["CLAUDE_MV_SOURCE"] = "ccfind"
        r = self.run_mv("--extract", "--no-browse", "-n", "--session", self.sid,
                        self.code, self.proj_path(), env_extra=env, expect=1)
        self.assertIn("could not answer", r.stderr)

    def test_a_hit_in_a_profile_we_were_not_given_is_dropped(self):
        """ccfind resolves profiles independently of us, so it can see config
        dirs this run was never told about. Migrating into one would write to
        a profile the user did not ask claude-mv to touch."""
        env = self.answer(results=[{"config_dir": os.path.join(self.tmp, "other"),
                                    "id": self.sid, "cwd": self.code,
                                    "path": self.path, "snippet": "x"}])
        env["CLAUDE_MV_SOURCE"] = "ccfind"
        r = self.run_mv("--extract", "--no-browse", "-n", "--session", self.sid,
                        self.code, self.proj_path(), env_extra=env, expect=1)
        self.assertIn("no sessions are homed in", r.stderr)

    def test_a_hit_whose_cwd_is_not_ours_is_dropped(self):
        """enc() is lossy: /a/b/c and /a/b-c share a project dir, and Claude
        files both there. The recorded cwd is what tells them apart."""
        env = self.answer(results=[{"config_dir": self.profile, "id": self.sid,
                                    "cwd": self.code + "-scratch",
                                    "path": self.path, "snippet": "x"}])
        env["CLAUDE_MV_SOURCE"] = "ccfind"
        r = self.run_mv("--extract", "--no-browse", "-n", "--session", self.sid,
                        self.code, self.proj_path(), env_extra=env, expect=1)
        self.assertIn("no sessions are homed in", r.stderr)

    def test_a_hit_with_no_cwd_is_confirmed_rather_than_dropped(self):
        """ccfind prints "?" when it could not extract a cwd. That is "don't
        know", not "not ours" — dropping the session on its silence would
        hide it, so we read the file ourselves instead."""
        env = self.answer(results=[{"config_dir": self.profile, "id": self.sid,
                                    "cwd": "?", "path": self.path,
                                    "snippet": "x"}])
        env["CLAUDE_MV_SOURCE"] = "ccfind"
        r = self.run_mv("--extract", "--no-browse", "-n", "--session",
                        self.sid, self.code, self.proj_path(), env_extra=env)
        self.assertIn("would re-home", r.stdout)

    def test_a_hit_with_no_cwd_whose_file_disagrees_is_still_dropped(self):
        """The confirmation has to be a real check, not a rubber stamp."""
        stray = self.make_session(self.code + "-scratch", self.sid)
        env = self.answer(results=[{"config_dir": self.profile, "id": self.sid,
                                    "cwd": "?", "path": stray,
                                    "snippet": "x"}])
        env["CLAUDE_MV_SOURCE"] = "ccfind"
        r = self.run_mv("--extract", "--no-browse", "-n", "--session",
                        self.sid, self.code, self.proj_path(),
                        env_extra=env, expect=1)
        self.assertIn("no sessions are homed in", r.stderr)

    def test_a_hit_we_cannot_stat_is_not_offered(self):
        env = self.answer(results=[{"config_dir": self.profile, "id": self.sid,
                                    "cwd": self.code, "snippet": "x",
                                    "path": os.path.join(self.tmp, "gone.jsonl")}])
        env["CLAUDE_MV_SOURCE"] = "ccfind"
        r = self.run_mv("--extract", "--no-browse", "-n", "--session",
                        self.sid, self.code, self.proj_path(),
                        env_extra=env, expect=1)
        self.assertIn("no sessions are homed in", r.stderr)

    def test_a_malformed_hit_is_skipped_not_crashed_on(self):
        env = self.answer(results=[{"config_dir": self.profile},
                                   {"id": self.sid, "cwd": self.code,
                                    "config_dir": self.profile,
                                    "path": self.path, "snippet": "x"}])
        env["CLAUDE_MV_SOURCE"] = "ccfind"
        r = self.run_mv("--extract", "--no-browse", "-n", "--session",
                        self.sid, self.code, self.proj_path(), env_extra=env)
        self.assertIn("would re-home", r.stdout)

    def test_a_clipped_answer_says_so(self):
        """No silent caps: a picker showing 50 of 200 sessions must not look
        like the whole list."""
        env = self.answer(truncated=True, total=200, shown=1)
        r = self.run_mv("--extract", "--no-browse", "-n", "--session", self.sid,
                        self.code, self.proj_path(), env_extra=env)
        self.assertIn("--limit", r.stderr)

    def test_a_broken_ccfind_falls_through_to_the_walk(self):
        broken = os.path.join(self.tmp, "broken")
        with open(broken, "w") as f:
            f.write("#!/bin/sh\necho nope >&2\nexit 3\n")
        os.chmod(broken, 0o755)
        r = self.run_mv("--extract", "--no-browse", "-n", "--session", self.sid, self.code,
                        self.proj_path(),
                        env_extra={"CLAUDE_MV_CCFIND_BIN": broken,
                                   "CLAUDE_MV_SOURCE": "auto"})
        self.assertIn("would re-home", r.stdout)

    def test_no_ccfind_at_all_falls_through_to_the_walk(self):
        r = self.run_mv("--extract", "--no-browse", "-n", "--session", self.sid, self.code,
                        self.proj_path(), env_extra={"CLAUDE_MV_SOURCE": "fs"})
        self.assertIn("would re-home", r.stdout)

    def test_the_walk_confirms_a_session_belongs_to_this_path(self):
        """enc() is lossy, so one project dir can legitimately hold sessions
        of two different real paths — `<tmp>/code` and `<tmp>-code` both
        encode to the same name, and Claude files both there. Offering a
        stranger's conversation would re-home history that was never ours.

        Constructed rather than imagined: the assertion below proves the
        sibling really does land in the same directory on disk.
        """
        stranger = self.tmp + "-code"          # encodes exactly like self.code
        self.assertEqual(cm.enc(stranger), cm.enc(self.code))
        other = "ffffffff-9999-9999-9999-999999999999"
        self.make_session(stranger, other)
        self.assertTrue(os.path.isfile(os.path.join(
            self.projects, cm.enc(self.code), f"{other}.jsonl")),
            "fixture did not reproduce the collision")

        r = self.run_mv("--extract", "--no-browse", "-n", "--session", other,
                        self.code, self.proj_path(),
                        env_extra={"CLAUDE_MV_SOURCE": "fs"}, expect=1)
        self.assertIn("not homed in", r.stderr)


CCFIND_ZSH = os.environ.get("CLAUDE_MV_CCFIND_SCRIPT") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "ccfind", "ccfind.zsh")


@unittest.skipUnless(shutil.which("zsh") and os.path.isfile(CCFIND_ZSH),
                     "no sibling ccfind checkout to cross-check against")
class TestSessionSourcesAgree(SessionFixture):
    """The two sources must return the SAME sessions for the same fixture.

    The one test that keeps a soft dependency honest. Everything else pins
    each source's own behaviour; if they disagree about which sessions are
    homed in a path, claude-mv quietly does something different depending on
    what the machine has installed — and no per-source test can see it.

    Uses the real ccfind (pointed at the fixture profile via CCFIND_PROFILES),
    not a stub, because a stub agreeing with us proves nothing. Skips where
    there is no ccfind checkout, CI included — so this is a local guard, and
    the suite says so rather than implying the agreement is verified
    everywhere.
    """

    def ids_from(self, source):
        r = self.run_mv("--extract", "--no-browse", "-n", "--session", self.HERO,
                        "--session", self.SIBLING, "--session", self.OTHER,
                        self.code, self.proj,
                        env_extra={"CLAUDE_MV_SOURCE": source,
                                   "CLAUDE_MV_CCFIND_SOURCE": CCFIND_ZSH,
                                   "CCFIND_PROFILES": f"test:{self.profile}"})
        return sorted(re.findall(r"re-home ([0-9a-f]{8})", r.stdout))

    def test_ccfind_and_the_filesystem_walk_see_the_same_sessions(self):
        self.assertEqual(self.ids_from("ccfind"), self.ids_from("fs"))
        self.assertEqual(len(self.ids_from("fs")), 3)

    def test_a_session_that_wandered_is_still_found_by_both(self):
        """The hero case. It STARTED in code and spent the rest of its life
        in the subproject, so anything keying on a session's latest cwd would
        lose the one session this whole mode exists to move."""
        for source in ("ccfind", "fs"):
            r = self.run_mv("--extract", "--no-browse", "-n", "--session", self.HERO,
                            self.code, self.proj,
                            env_extra={"CLAUDE_MV_SOURCE": source,
                                       "CLAUDE_MV_CCFIND_SOURCE": CCFIND_ZSH,
                                       "CCFIND_PROFILES": f"test:{self.profile}"})
            self.assertIn(self.HERO[:8], r.stdout, source)

    def offered_by(self, source, *args):
        """The ids the picker was given, from a run cancelled at the prompt."""
        r = self.run_mv("--extract", "--no-browse", *args, self.code,
                        self.proj, stdin="\n", expect=1,
                        env_extra={"CLAUDE_MV_SOURCE": source,
                                   "CLAUDE_MV_CCFIND_SOURCE": CCFIND_ZSH,
                                   "CCFIND_PROFILES": f"test:{self.profile}",
                                   "CLAUDE_MV_PICKER": "plain",
                                   "CLAUDE_MV_FORCE_PROMPT": "1"})
        return sorted(re.findall(r"^\s+\d+\s+\S+ \S+\s+([0-9a-f]{8})\s",
                                 r.stdout, re.M))

    def test_the_two_sources_answer_the_same_search(self):
        """The hybrid's whole risk in one test. ccfind greps with -F where it
        is installed and claude-mv reads the transcripts where it is not, so
        --search has to mean the same thing either way: a literal substring,
        case folded, matched against a raw transcript line."""
        for query in ("unrelated", "and now here", "WORK", "nrelated wor"):
            self.assertEqual(self.offered_by("ccfind", "--search", query),
                             self.offered_by("fs", "--search", query), query)
        # ... and the answer they agree on is the right one.
        self.assertEqual(self.offered_by("fs", "--search", "unrelated"),
                         sorted([self.SIBLING[:8], self.OTHER[:8]]))

    def test_the_two_sources_agree_about_how_far_down_to_look(self):
        below = "99999999-9999-9999-9999-999999999999"
        self.make_session(self.proj, below, prompt="down here, unrelated too",
                          mtime=500)
        for args in (("-R",), ("-R", "--search", "unrelated")):
            self.assertEqual(self.offered_by("ccfind", *args),
                             self.offered_by("fs", *args), args)
        self.assertIn(below[:8], self.offered_by("fs", "-R", "--search",
                                                 "unrelated"))
        self.assertNotIn(below[:8], self.offered_by("fs", "--search",
                                                    "unrelated"))


# ── end-to-end: moving sessions, not folders ────────────────────────────────

class TestSessionMove(SessionFixture):
    def dst_dir(self, cwd):
        return os.path.join(self.projects, cm.enc(cwd))

    def session_ids_in(self, cwd):
        d = self.dst_dir(cwd)
        if not os.path.isdir(d):
            return []
        return sorted(n[:-len(".jsonl")] for n in os.listdir(d)
                      if n.endswith(".jsonl"))

    FS = {"CLAUDE_MV_SOURCE": "fs"}

    def test_only_the_chosen_session_moves(self):
        self.run_mv("--extract", "--no-browse", "--session", self.HERO, self.code,
                    self.proj, env_extra=self.FS)

        self.assertEqual(self.session_ids_in(self.proj), [self.HERO])
        self.assertEqual(self.session_ids_in(self.code),
                         sorted([self.SIBLING, self.OTHER]))

    def test_the_sidecar_follows_its_session(self):
        """subagents/ and tool-results/ live INSIDE the project dir, so the
        whole-folder path carries them for free and this one must not forget."""
        self.run_mv("--extract", "--no-browse", "--session", self.HERO, self.code,
                    self.proj, env_extra=self.FS)

        self.assertTrue(os.path.isfile(os.path.join(
            self.dst_dir(self.proj), self.HERO, "subagents", "agent-1.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(
            self.dst_dir(self.code), self.HERO)))

    def test_the_transcript_is_not_rewritten(self):
        """Nothing moved on disk — the session really did start in the parent
        — so rewriting cwd would falsify the record. It would also corrupt
        this shape outright: the second turn already names the destination,
        and a prefix remap would take it to newproj/newproj."""
        before = open(os.path.join(self.dst_dir(self.code),
                                   f"{self.HERO}.jsonl")).read()
        self.run_mv("--extract", "--no-browse", "--session", self.HERO, self.code,
                    self.proj, env_extra=self.FS)
        after = open(os.path.join(self.dst_dir(self.proj),
                                  f"{self.HERO}.jsonl")).read()
        self.assertEqual(after, before)
        self.assertIn(json.dumps(self.proj)[1:-1], after)   # …/newproj
        self.assertNotIn("newproj/newproj", after)

    def test_only_that_session_s_history_is_re_keyed(self):
        self.run_mv("--extract", "--no-browse", "--session", self.HERO, self.code,
                    self.proj, env_extra=self.FS)

        by_session = {}
        for e in self.read_history():
            by_session.setdefault(e["sessionId"], set()).add(e["project"])
        self.assertEqual(by_session[self.HERO], {self.proj})
        self.assertEqual(by_session[self.SIBLING], {self.code})

    def test_the_config_map_is_left_alone(self):
        """The source project still exists, and fabricating a destination
        entry would transplant its trust flag and allowedTools onto a path
        the user never approved."""
        before = self.read_config()
        r = self.run_mv("--extract", "--no-browse", "--session", self.HERO, self.code,
                        self.proj, env_extra=self.FS)
        self.assertEqual(self.read_config(), before)
        self.assertIn("no config entry for the destination yet", r.stdout)

    def test_no_folder_is_moved(self):
        self.run_mv("--extract", "--no-browse", "--session", self.HERO, self.code,
                    self.proj, env_extra=self.FS)
        self.assertTrue(os.path.isdir(self.proj))
        self.assertTrue(os.path.isdir(self.code))

    def test_the_destination_may_be_inside_the_source(self):
        """The folder path refuses this outright ("cannot move into itself"),
        and for a folder move it is nonsense. Here it is the norm: the
        subproject was created inside the directory the session started in."""
        r = self.run_mv("--extract", "--no-browse", "--session", self.HERO, self.code,
                        self.proj, env_extra=self.FS)
        self.assertNotIn("into itself", r.stdout + r.stderr)

    def test_an_eight_character_prefix_names_a_session(self):
        self.run_mv("--extract", "--no-browse", "--session", self.HERO[:8], self.code,
                    self.proj, env_extra=self.FS)
        self.assertEqual(self.session_ids_in(self.proj), [self.HERO])

    def test_an_ambiguous_prefix_is_refused_rather_than_guessed(self):
        """Two ids sharing a prefix is unlikely and entirely possible, and
        guessing would re-home the wrong conversation."""
        twin = self.HERO[:8] + "-9999-9999-9999-999999999999"
        self.make_session(self.code, twin, mtime=500)
        r = self.run_mv("--extract", "--no-browse", "--session", self.HERO[:8], self.code,
                        self.proj, env_extra=self.FS, expect=1)
        self.assertIn("matches 2 sessions", r.stderr)
        self.assertEqual(self.session_ids_in(self.proj), [])

    def test_naming_a_session_ignores_the_picker_limit(self):
        """--limit keeps the picker readable; it must not decide whether a
        session named outright can be found."""
        self.run_mv("--extract", "--no-browse", "--limit", "1", "--session", self.OTHER,
                    self.code, self.proj, env_extra=self.FS)
        self.assertEqual(self.session_ids_in(self.proj), [self.OTHER])

    def test_an_unknown_session_is_refused_before_anything_moves(self):
        r = self.run_mv("--extract", "--no-browse", "--session", "deadbeef", self.code,
                        self.proj, env_extra=self.FS, expect=1)
        self.assertIn("not homed in", r.stderr)
        self.assertEqual(self.session_ids_in(self.proj), [])

    def test_dry_run_changes_nothing(self):
        before = self.session_ids_in(self.code)
        r = self.run_mv("--extract", "--no-browse", "-n", "--session", self.HERO, self.code,
                        self.proj, env_extra=self.FS)
        self.assertIn("dry run", r.stdout)
        self.assertEqual(self.session_ids_in(self.code), before)
        self.assertEqual(self.session_ids_in(self.proj), [])
        self.assertEqual(self.restore_stamps(), [])

    def test_a_live_session_among_the_chosen_blocks(self):
        """Sharper than the folder guard's cwd test: the folder is not moving,
        so what matters is whether this session's transcript is being appended
        to while we relocate it."""
        d = os.path.join(self.profile, "sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "1.json"), "w") as f:
            json.dump({"cwd": self.proj, "pid": os.getpid(),
                       "sessionId": self.HERO}, f)
        r = self.run_mv("--extract", "--no-browse", "--session", self.HERO, self.code,
                        self.proj, env_extra=self.FS, expect=1)
        self.assertIn("live Claude session", r.stderr)
        self.assertEqual(self.session_ids_in(self.proj), [])

    def test_a_live_session_we_are_not_moving_does_not_block(self):
        d = os.path.join(self.profile, "sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "1.json"), "w") as f:
            json.dump({"cwd": self.code, "pid": os.getpid(),
                       "sessionId": self.SIBLING}, f)
        self.run_mv("--extract", "--no-browse", "--session", self.HERO, self.code,
                    self.proj, env_extra=self.FS)
        self.assertEqual(self.session_ids_in(self.proj), [self.HERO])

    def test_a_session_already_at_the_destination_is_kept_by_default(self):
        self.make_session(self.proj, self.HERO, prompt="the newer copy")
        r = self.run_mv("--extract", "--no-browse", "--session", self.HERO, self.code,
                        self.proj, env_extra=self.FS)
        self.assertIn("skip", r.stdout)
        with open(os.path.join(self.dst_dir(self.proj),
                               f"{self.HERO}.jsonl")) as f:
            self.assertIn("the newer copy", f.read())
        # left where it was rather than silently dropped
        self.assertIn(self.HERO, self.session_ids_in(self.code))

    def test_a_skipped_session_keeps_its_history_where_it_is(self):
        """The transcript stayed in the source, so its prompt entries must
        too — re-keying them would point the recall at a folder holding a
        different copy of that conversation."""
        self.make_session(self.proj, self.HERO, prompt="the newer copy")
        r = self.run_mv("--extract", "--no-browse", "--session", self.HERO,
                        self.code, self.proj, env_extra=self.FS)
        self.assertIn("skip", r.stdout)
        projects = {e["project"] for e in self.read_history()
                    if e["sessionId"] == self.HERO}
        self.assertEqual(projects, {self.code})

    def test_overwrite_replaces_it_and_keeps_the_restore_point(self):
        self.make_session(self.proj, self.HERO, prompt="the newer copy")
        self.run_mv("--extract", "--no-browse", "--on-conflict", "overwrite", "--session",
                    self.HERO, self.code, self.proj, env_extra=self.FS)
        with open(os.path.join(self.dst_dir(self.proj),
                               f"{self.HERO}.jsonl")) as f:
            self.assertIn("build me a thing", f.read())
        self.assertEqual(len(self.restore_stamps()), 1)

    def test_overwrite_discards_the_destination_sidecar_too(self):
        """A replaced session's subagent transcripts must go with it — leaving
        them behind would attach one conversation's subagents to another."""
        self.make_session(self.proj, self.HERO, prompt="the newer copy",
                          sidecar=True)
        victim = os.path.join(self.dst_dir(self.proj), self.HERO,
                              "subagents", "agent-1.jsonl")
        with open(victim, "w") as f:
            f.write(jsonl({"type": "user", "note": "destination's own"}))

        self.run_mv("--extract", "--no-browse", "--on-conflict", "overwrite",
                    "--session", self.HERO, self.code, self.proj,
                    env_extra=self.FS)

        with open(victim) as f:                       # replaced, not merged
            self.assertNotIn("destination's own", f.read())
        stamps = self.restore_stamps()                # and kept, not lost
        self.assertEqual(len(stamps), 1)
        saved = subprocess.run(["grep", "-rl", "destination's own",
                                os.path.join(self.restore_root, stamps[0])],
                               capture_output=True, text=True)
        self.assertTrue(saved.stdout.strip(),
                        "the discarded sidecar is not in the restore point")

    def test_a_stale_live_session_record_does_not_block(self):
        """sessions/*.json outlives the process it describes. Trusting the
        file alone would make a crashed session block its own move forever."""
        d = os.path.join(self.profile, "sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "1.json"), "w") as f:
            json.dump({"cwd": self.code, "pid": 2 ** 22,   # long gone
                       "sessionId": self.HERO}, f)
        with open(os.path.join(d, "notes.txt"), "w") as f:
            f.write("not a session record")
        with open(os.path.join(d, "2.json"), "w") as f:
            f.write("{ truncated")
        self.run_mv("--extract", "--no-browse", "--session", self.HERO,
                    self.code, self.proj, env_extra=self.FS)
        self.assertEqual(self.session_ids_in(self.proj), [self.HERO])

    def test_force_overrides_a_live_session(self):
        d = os.path.join(self.profile, "sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "1.json"), "w") as f:
            json.dump({"cwd": self.proj, "pid": os.getpid(),
                       "sessionId": self.HERO}, f)
        self.run_mv("--extract", "--no-browse", "--force", "--session",
                    self.HERO, self.code, self.proj, env_extra=self.FS)
        self.assertEqual(self.session_ids_in(self.proj), [self.HERO])

    def test_a_stray_destination_sidecar_is_a_conflict_not_a_half_move(self):
        """The destination has a `<id>/` with no transcript beside it — an
        interrupted run, a half-deleted session. Calling that a clean move
        would land the transcript and *then* fail on the sidecar rename,
        stopping halfway; it would also hand one conversation another's
        subagent transcripts. So it is detected up front, like every other
        conflict, and the default policy leaves both sides alone.
        """
        stray = os.path.join(self.dst_dir(self.proj), self.HERO, "subagents")
        os.makedirs(stray)
        with open(os.path.join(stray, "agent-9.jsonl"), "w") as f:
            f.write(jsonl({"type": "user", "note": "not ours"}))

        r = self.run_mv("--extract", "--no-browse", "--session", self.HERO,
                        self.code, self.proj, env_extra=self.FS)

        # Reported UP FRONT, from the plan — not discovered part-way through
        # by the code doing the moving. That ordering is the whole promise.
        self.assertIn("already present at the destination", r.stdout)
        self.assertLess(r.stdout.index("already present at the destination"),
                        r.stdout.index("re-homing"))
        self.assertIn("skip", r.stdout)
        self.assertEqual(self.session_ids_in(self.proj), [])   # nothing landed
        self.assertIn(self.HERO, self.session_ids_in(self.code))
        with open(os.path.join(stray, "agent-9.jsonl")) as f:
            self.assertIn("not ours", f.read())

    def test_overwrite_clears_a_stray_destination_sidecar_first(self):
        stray = os.path.join(self.dst_dir(self.proj), self.HERO, "subagents")
        os.makedirs(stray)
        with open(os.path.join(stray, "agent-9.jsonl"), "w") as f:
            f.write(jsonl({"type": "user", "note": "not ours"}))

        self.run_mv("--extract", "--no-browse", "--on-conflict", "overwrite",
                    "--session", self.HERO, self.code, self.proj,
                    env_extra=self.FS)

        self.assertEqual(self.session_ids_in(self.proj), [self.HERO])
        self.assertFalse(os.path.exists(os.path.join(stray, "agent-9.jsonl")),
                         "the moved session inherited a stranger's subagents")
        self.assertTrue(os.path.isfile(os.path.join(
            self.dst_dir(self.proj), self.HERO, "subagents", "agent-1.jsonl")),
            "the session's own sidecar did not follow")

    def test_a_failure_midway_keeps_the_restore_point_and_exits_3(self):
        """The contract the error message makes: there is something to roll
        back to, and it is named."""
        target = self.dst_dir(self.proj)
        os.makedirs(target)
        # A file where the sidecar dir has to land: the transcript moves, then
        # the sidecar rename fails — a genuine mid-migration stop.
        with open(os.path.join(target, self.HERO), "w") as f:
            f.write("in the way")
        r = self.run_mv("--extract", "--no-browse", "--session", self.HERO,
                        self.code, self.proj, env_extra=self.FS, expect=3)
        self.assertIn("migration failed midway", r.stderr)
        stamps = self.restore_stamps()
        self.assertEqual(len(stamps), 1)
        self.assertIn(stamps[0], r.stderr)

    def test_overwrite_backup_can_be_opted_out_of(self):
        self.make_session(self.proj, self.HERO, prompt="the newer copy")
        env = dict(self.FS, CLAUDE_MV_OVERWRITE_BACKUP="0")
        self.run_mv("--extract", "--no-browse", "--on-conflict", "overwrite",
                    "--session", self.HERO, self.code, self.proj,
                    env_extra=env)
        self.assertEqual(self.restore_stamps(), [])

    def test_a_destination_that_already_has_a_config_entry_is_not_remarked_on(self):
        self.add_config(self.proj)
        self.write_fixture()
        r = self.run_mv("--extract", "--no-browse", "--session", self.HERO,
                        self.code, self.proj, env_extra=self.FS)
        self.assertNotIn("no config entry", r.stdout)

    def test_a_folder_conflict_policy_is_refused(self):
        r = self.run_mv("--extract", "--no-browse", "--on-conflict", "consolidate",
                        "--session", self.HERO, self.code, self.proj,
                        env_extra=self.FS, expect=2)
        self.assertIn("does not apply to --extract", r.stderr)

    def test_session_and_already_moved_are_different_operations(self):
        r = self.run_mv("--extract", "--no-browse", "--already-moved", "--session",
                        self.HERO, self.code, self.proj, env_extra=self.FS,
                        expect=2)
        self.assertIn("different operations", r.stderr)

    def test_selecting_sessions_needs_the_mode(self):
        r = self.run_mv("--session", self.HERO, self.code, self.proj,
                        env_extra=self.FS, expect=2)
        self.assertIn("needs --extract", r.stderr)

    def test_no_browse_only_means_anything_in_extract_mode(self):
        r = self.run_mv("--no-browse", self.code, self.proj,
                        env_extra=self.FS, expect=2)
        self.assertIn("only applies to --extract", r.stderr)

    def test_a_destination_that_is_not_there_is_refused(self):
        r = self.run_mv("--extract", "--no-browse", "--session", self.HERO, self.code,
                        os.path.join(self.code, "nope"), env_extra=self.FS,
                        expect=1)
        self.assertIn("not a directory", r.stderr)

    def test_a_source_with_no_sessions_says_where_to_look(self):
        empty = self.make_folder("empty")
        r = self.run_mv("--extract", "--no-browse", "--session", self.HERO, empty,
                        self.proj, env_extra=self.FS, expect=1)
        self.assertIn("homed where it STARTED", r.stderr)

    def test_restore_puts_the_session_back_and_moves_no_folder(self):
        # A conflict resolved by overwrite: the one mode that keeps its
        # restore point on success, and so the only one with a point left to
        # roll back to.
        self.make_session(self.proj, self.HERO, prompt="the newer copy")
        before = {"code": self.session_ids_in(self.code),
                  "proj": self.session_ids_in(self.proj),
                  "history": self.read_history(),
                  "config": self.read_config()}
        self.run_mv("--extract", "--no-browse", "--on-conflict", "overwrite", "--session",
                    self.HERO, self.code, self.proj, env_extra=self.FS)
        stamps = self.restore_stamps()
        self.assertEqual(len(stamps), 1)

        r = self.run_mv("--restore", stamps[0], "--force")

        self.assertIn("no folder was moved", r.stdout)
        self.assertEqual(self.session_ids_in(self.code), before["code"])
        self.assertEqual(self.session_ids_in(self.proj), before["proj"])
        self.assertEqual(self.read_history(), before["history"])
        self.assertEqual(self.read_config(), before["config"])
        # and the sidecar came home rather than existing in both places
        self.assertTrue(os.path.isdir(os.path.join(
            self.dst_dir(self.code), self.HERO)))
        self.assertFalse(os.path.exists(os.path.join(
            self.dst_dir(self.proj), self.HERO)))
        self.assertEqual(self.restore_stamps(), [])


class TestSessionPicker(SessionFixture):
    """Choosing without --session. fzf is soft, so the numbered prompt is not
    a consolation prize — it is the path a pipe can drive, and so the one the
    suite can assert on."""

    PLAIN = {"CLAUDE_MV_SOURCE": "fs", "CLAUDE_MV_PICKER": "plain",
             "CLAUDE_MV_FORCE_PROMPT": "1"}

    def test_the_list_is_newest_first(self):
        """Stable order is what lets a person — and every test below — say
        "the second one" and mean it."""
        r = self.run_mv("--extract", "--no-browse", "-n", self.code, self.proj, stdin="\n",
                        env_extra=self.PLAIN, expect=1)
        order = re.findall(r"^\s+\d+\s+\S+ \S+\s+([0-9a-f]{8})\s", r.stdout,
                           re.M)
        self.assertEqual(order[:3], [self.HERO[:8], self.SIBLING[:8],
                                     self.OTHER[:8]])

    def test_a_number_picks_that_session(self):
        self.run_mv("--extract", "--no-browse", self.code, self.proj,
                    stdin="1\ny\n", env_extra=self.PLAIN)
        self.assertEqual(sorted(n[:-6] for n in os.listdir(
            os.path.join(self.projects, cm.enc(self.proj)))
            if n.endswith(".jsonl")), [self.HERO])

    def test_several_can_be_picked_at_once(self):
        self.run_mv("--extract", "--no-browse", self.code, self.proj,
                    stdin="1,3\ny\n", env_extra=self.PLAIN)
        moved = sorted(n[:-6] for n in os.listdir(
            os.path.join(self.projects, cm.enc(self.proj)))
            if n.endswith(".jsonl"))
        self.assertEqual(moved, sorted([self.HERO, self.OTHER]))

    def test_an_empty_answer_cancels(self):
        r = self.run_mv("--extract", "--no-browse", self.code, self.proj, stdin="\n",
                        env_extra=self.PLAIN, expect=1)
        self.assertIn("nothing selected", r.stdout)
        self.assertFalse(os.path.exists(
            os.path.join(self.projects, cm.enc(self.proj))))

    def test_it_reprompts_rather_than_guessing(self):
        self.run_mv("--extract", "--no-browse", self.code, self.proj,
                    stdin="9\n1\ny\n", env_extra=self.PLAIN)
        self.assertTrue(os.path.exists(os.path.join(
            self.projects, cm.enc(self.proj), f"{self.HERO}.jsonl")))

    def test_with_no_fzf_and_no_tty_it_says_so_rather_than_hanging(self):
        r = self.run_mv("--extract", "--no-browse", self.code, self.proj,
                        env_extra={"CLAUDE_MV_SOURCE": "fs",
                                   "CLAUDE_MV_PICKER": "plain"},
                        expect=1)
        self.assertIn("--session", r.stderr)

    def test_fzf_is_not_launched_when_there_is_no_terminal(self):
        """The regression this guards is a HANG, not a failure: fzf draws a
        full-screen UI and reads the keyboard, so starting it on a pipe leaves
        it waiting on input that can never arrive. A test that reproduced it
        would hang too, so this asserts the negative — fzf was never run —
        with a stub that records having been called.

        `auto` is the mode that matters here: it is what a script, a cron job
        or a piped run gets, and it is the one that has to decide for itself.
        """
        bin_dir = os.path.join(self.tmp, "trapbin")
        os.makedirs(bin_dir, exist_ok=True)
        marker = os.path.join(self.tmp, "fzf-was-launched")
        with open(os.path.join(bin_dir, "fzf"), "w") as f:
            f.write("#!/bin/sh\ntouch %s\nexit 1\n" % marker)
        os.chmod(os.path.join(bin_dir, "fzf"), 0o755)

        self.run_mv("--extract", self.code, self.proj,
                    env_extra={"CLAUDE_MV_SOURCE": "fs",
                               "PATH": bin_dir + os.pathsep
                                       + os.environ["PATH"]},
                    expect=1)
        self.assertFalse(os.path.exists(marker),
                         "fzf was launched with no tty — that hangs")

    def test_a_forced_fzf_that_is_not_installed_is_reported(self):
        r = self.run_mv("--extract", "--no-browse", self.code, self.proj,
                        env_extra={"CLAUDE_MV_SOURCE": "fs",
                                   "CLAUDE_MV_PICKER": "fzf",
                                   "PATH": os.path.join(self.tmp, "empty-bin")},
                        expect=1)
        self.assertIn("fzf", r.stderr)

    def test_a_forced_fzf_is_reported_at_the_folder_prompt_too(self):
        """Both pickers honour the same override, so both owe the same
        explanation when it cannot be met."""
        r = self.run_mv("--extract", "--session", self.HERO, self.proj,
                        env_extra={"CLAUDE_MV_SOURCE": "fs",
                                   "CLAUDE_MV_PICKER": "fzf",
                                   "PATH": os.path.join(self.tmp, "empty-bin")},
                        cwd=self.code, expect=1)
        self.assertIn("fzf is not installed", r.stderr)


class TestExtractAcrossProfiles(unittest.TestCase):
    """--extract groups the chosen sessions BY PROFILE and plans each on its
    own. Every other extract test passes one --profile, so none of them can
    see a second profile being skipped, or one profile's session being
    written into another's projects/ tree."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="claude-mv-xp-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.code = os.path.join(self.tmp, "code")
        self.proj = os.path.join(self.code, "newproj")
        os.makedirs(self.proj)
        self.restore_root = os.path.join(self.tmp, "restore")
        self.profiles = []
        for i, name in enumerate(("work", "personal")):
            p = os.path.join(self.tmp, name)
            os.makedirs(os.path.join(p, "projects", cm.enc(self.code)))
            sid = f"{name[0] * 8}-1111-1111-1111-11111111111{i}"
            with open(os.path.join(p, "projects", cm.enc(self.code),
                                   f"{sid}.jsonl"), "w") as f:
                f.write(jsonl({"type": "user", "cwd": self.code,
                               "sessionId": sid,
                               "message": {"role": "user", "content": name}}))
            with open(os.path.join(p, ".claude.json"), "w") as f:
                json.dump({"projects": {self.code: {}}}, f)
            with open(os.path.join(p, "history.jsonl"), "w") as f:
                f.write(jsonl({"display": name, "project": self.code,
                               "sessionId": sid}))
            self.profiles.append((p, sid))

    def run_mv(self, *args, expect=0):
        env = dict(os.environ, CLAUDE_MV_RESTORE_ROOT=self.restore_root,
                   CLAUDE_MV_SOURCE="fs")
        env.pop("CLAUDE_MV_FORCE_PROMPT", None)
        flags = []
        for p, _ in self.profiles:
            flags += ["--profile", p]
        r = subprocess.run([sys.executable, SCRIPT, *flags, *args],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, expect,
                         f"exit {r.returncode}\n{r.stdout}\n{r.stderr}")
        return r

    def test_a_session_from_each_profile_moves_within_its_own_profile(self):
        self.run_mv("--extract", "--no-browse",
                    *[a for _, sid in self.profiles
                      for a in ("--session", sid)],
                    self.code, self.proj)
        for p, sid in self.profiles:
            moved = os.path.join(p, "projects", cm.enc(self.proj),
                                 f"{sid}.jsonl")
            self.assertTrue(os.path.isfile(moved), f"{sid} not in {p}")
            self.assertFalse(os.path.exists(os.path.join(
                p, "projects", cm.enc(self.code), f"{sid}.jsonl")))
            # and its history followed, in ITS profile only
            with open(os.path.join(p, "history.jsonl")) as f:
                entries = [json.loads(line) for line in f if line.strip()]
            self.assertEqual([e["project"] for e in entries], [self.proj])

    def test_moving_one_leaves_the_other_profile_untouched(self):
        (kept, kept_sid) = self.profiles[1]
        before = sorted(os.listdir(os.path.join(kept, "projects")))
        self.run_mv("--extract", "--no-browse", "--session",
                    self.profiles[0][1], self.code, self.proj)
        self.assertEqual(sorted(os.listdir(os.path.join(kept, "projects"))),
                         before)
        with open(os.path.join(kept, "history.jsonl")) as f:
            self.assertIn(self.code, f.read())

    def test_the_tally_counts_both_profiles(self):
        r = self.run_mv("--extract", "--no-browse",
                        *[a for _, sid in self.profiles
                          for a in ("--session", sid)],
                        self.code, self.proj)
        self.assertIn("2 session files", r.stdout)
        self.assertIn("2 history entries", r.stdout)


class TestExtractPathGrammar(SessionFixture):
    """Which positional means what, and what is allowed to be inferred.

    The rule the whole grammar rests on: `src` has a default (the cwd) and
    `dst` never does, so a lone positional can only be the destination.
    """

    NB = {"CLAUDE_MV_SOURCE": "fs"}

    def moved_to(self, cwd):
        d = os.path.join(self.projects, cm.enc(cwd))
        return sorted(n[:-len(".jsonl")] for n in os.listdir(d)
                      if n.endswith(".jsonl")) if os.path.isdir(d) else []

    def test_two_positionals_are_source_then_destination(self):
        self.run_mv("--extract", "--no-browse", "--session", self.HERO,
                    self.code, self.proj, env_extra=self.NB)
        self.assertEqual(self.moved_to(self.proj), [self.HERO])

    def test_one_positional_is_the_destination_and_src_is_the_cwd(self):
        """`claude-mv --extract ~/code/newproj`, run from ~/code.

        The lone path is the destination — never the source — and the source
        prompt comes up already defaulted to the cwd, so a bare Enter takes
        it. Both halves of the rule in one run.
        """
        r = self.run_mv("--extract", "-n", "--session", self.HERO, self.proj,
                        stdin="\n\n", cwd=self.code,
                        env_extra={"CLAUDE_MV_SOURCE": "fs",
                                   "CLAUDE_MV_PICKER": "plain",
                                   "CLAUDE_MV_FORCE_PROMPT": "1"})
        # found in the cwd, headed for the positional
        self.assertIn(f"Enter accepts {self.code}", r.stdout)
        self.assertIn("would re-home", r.stdout)
        self.assertIn(cm.enc(self.proj), r.stdout)

    def test_no_browse_will_not_infer_the_destination(self):
        """The one thing this mode refuses to guess. Without browsing and
        without a dst there is nothing left to go on, so it stops."""
        r = self.run_mv("--extract", "--no-browse", "--session", self.HERO,
                        self.code, env_extra=self.NB, expect=1)
        self.assertIn("needs both paths", r.stderr)
        self.assertEqual(self.moved_to(self.proj), [])

    def test_no_browse_will_not_infer_the_source_either(self):
        r = self.run_mv("--extract", "--no-browse", "--session", self.HERO,
                        env_extra=self.NB, expect=1)
        self.assertIn("needs both paths", r.stderr)

    def test_the_folder_move_still_demands_both(self):
        """--extract fills its own paths in; the folder move must not."""
        r = self.run_mv(self.code, env_extra=self.NB, expect=2)
        self.assertIn("src and dst are required", r.stderr)


class TestFolderPicker(SessionFixture):
    """Browsing for the two folders, on the no-fzf path a pipe can drive."""

    PLAIN = {"CLAUDE_MV_SOURCE": "fs", "CLAUDE_MV_PICKER": "plain",
             "CLAUDE_MV_FORCE_PROMPT": "1"}

    def moved_to(self, cwd):
        d = os.path.join(self.projects, cm.enc(cwd))
        return sorted(n[:-len(".jsonl")] for n in os.listdir(d)
                      if n.endswith(".jsonl")) if os.path.isdir(d) else []

    def test_both_ends_are_asked_for_and_enter_takes_the_default(self):
        """Run from the source with a destination given: the first prompt
        defaults to the cwd, the second to the path passed. Two bare Enters
        accept both."""
        r = self.run_mv("--extract", "--session", self.HERO, self.proj,
                        stdin="\n\ny\n", env_extra=self.PLAIN,
                        cwd=self.code)
        self.assertIn("Which folder holds the sessions?", r.stdout)
        self.assertIn("Where should they go?", r.stdout)
        self.assertEqual(self.moved_to(self.proj), [self.HERO])

    def test_a_typed_path_overrides_the_default(self):
        other = self.make_folder("elsewhere")
        self.run_mv("--extract", "--session", self.HERO, self.proj,
                    stdin=f"\n{other}\ny\n", env_extra=self.PLAIN,
                    cwd=self.code)
        self.assertEqual(self.moved_to(other), [self.HERO])
        self.assertEqual(self.moved_to(self.proj), [])

    def test_the_destination_is_asked_after_the_sessions(self):
        """The order the decision is actually made in: you know which
        conversation you are moving before you know where it belongs."""
        r = self.run_mv("--extract", self.proj, stdin="\n1\n\ny\n",
                        env_extra=self.PLAIN, cwd=self.code)
        self.assertLess(r.stdout.index("sessions available to move"),
                        r.stdout.index("Where should they go?"))

    def test_a_path_that_is_not_a_directory_reprompts(self):
        self.run_mv("--extract", "--session", self.HERO, self.proj,
                    stdin=f"\n{self.code}/nope\n{self.proj}\ny\n",
                    env_extra=self.PLAIN, cwd=self.code)
        self.assertEqual(self.moved_to(self.proj), [self.HERO])

    def test_cancelling_the_source_changes_nothing(self):
        r = self.run_mv("--extract", "--session", self.HERO, self.proj,
                        stdin="", env_extra=self.PLAIN, cwd=self.code,
                        expect=1)
        self.assertIn("cancelled", r.stdout)
        self.assertEqual(self.moved_to(self.proj), [])

    def test_source_and_destination_may_not_be_the_same(self):
        r = self.run_mv("--extract", "--session", self.HERO, stdin="\n\n",
                        env_extra=self.PLAIN, cwd=self.code, expect=1)
        self.assertIn("same path", r.stderr)

    def test_with_no_tty_and_no_fzf_it_says_how_to_proceed(self):
        r = self.run_mv("--extract", "--session", self.HERO, self.proj,
                        env_extra={"CLAUDE_MV_SOURCE": "fs",
                                   "CLAUDE_MV_PICKER": "plain"},
                        cwd=self.code, expect=1)
        self.assertIn("--no-browse", r.stderr)

    def test_cancelling_the_destination_changes_nothing(self):
        """Distinct from cancelling the source: by here the sessions have been
        chosen, so there is a half-made decision to throw away."""
        r = self.run_mv("--extract", "--session", self.HERO, stdin="\n",
                        env_extra=self.PLAIN, cwd=self.code, expect=1)
        self.assertIn("cancelled", r.stdout)
        self.assertEqual(self.moved_to(self.proj), [])
        self.assertEqual(self.restore_stamps(), [])

    def test_a_destination_argument_that_does_not_exist_falls_back_to_src(self):
        """The prompt has to start *somewhere*; a path that isn't there can't
        be it, so the browse opens on the source rather than on nothing."""
        r = self.run_mv("--extract", "--session", self.HERO,
                        os.path.join(self.code, "not-created-yet"),
                        stdin=f"\n{self.proj}\ny\n", env_extra=self.PLAIN,
                        cwd=self.code)
        self.assertIn(f"Enter accepts {self.code}", r.stdout.split(
            "Where should they go?")[1])
        self.assertEqual(self.moved_to(self.proj), [self.HERO])

    def test_the_path_completer_offers_directories_only(self):
        """Tab completion is the whole reason the no-fzf prompt is usable, and
        it is the one piece of the picker a piped test cannot exercise."""
        os.makedirs(os.path.join(self.code, "newer"), exist_ok=True)
        with open(os.path.join(self.code, "newfile.txt"), "w") as f:
            f.write("x")
        completer = cm.make_dir_completer()
        hits = []
        state = 0
        while True:
            hit = completer(os.path.join(self.code, "new"), state)
            if hit is None:
                break
            hits.append(hit)
            state += 1
        self.assertIn(os.path.join(self.code, "newproj") + os.sep, hits)
        self.assertIn(os.path.join(self.code, "newer") + os.sep, hits)
        self.assertNotIn(os.path.join(self.code, "newfile.txt"), hits)

    def test_the_path_completer_expands_a_tilde_and_survives_a_bad_dir(self):
        completer = cm.make_dir_completer()
        self.assertIsNone(completer(os.path.join(self.code, "nope", "x"), 0))
        home = [completer("~/", i) for i in range(1)]
        self.assertTrue(home[0] is None or home[0].startswith(os.path.expanduser("~")))

    def test_a_directory_row_carries_its_session_count(self):
        """What turns the browse into a choice rather than a guess: the rows
        say where the history actually is."""
        self.assertEqual(cm.session_count([self.profile], self.code), 3)
        self.assertEqual(cm.session_count([self.profile], self.proj), 0)
        rows = cm.dir_rows(self.code, [self.profile])
        self.assertIn("3 sessions", rows[0][1])

    def test_the_navigator_offers_this_dir_the_parent_and_the_children(self):
        rows = cm.dir_rows(self.code, [self.profile])
        payloads = [p for p, _ in rows]
        self.assertEqual(payloads[0], self.code)                  # use this
        self.assertEqual(payloads[1], os.path.dirname(self.code))  # up
        self.assertIn(self.proj, payloads)                         # children

    def test_noise_directories_are_not_offered(self):
        os.makedirs(os.path.join(self.code, ".git", "objects"))
        os.makedirs(os.path.join(self.code, "node_modules"))
        payloads = [p for p, _ in cm.dir_rows(self.code, [self.profile])]
        self.assertNotIn(os.path.join(self.code, ".git"), payloads)
        self.assertNotIn(os.path.join(self.code, "node_modules"), payloads)


@unittest.skipUnless(shutil.which("sh"), "no shell")
class TestFolderPickerWithFzf(SessionFixture):
    """The fzf navigator, with a stub for fzf. Under test is the navigation
    contract — that descending, going up and choosing map to the right
    directory — not fzf."""

    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.bin, exist_ok=True)
        # A stub that walks a script of row-numbers, one per invocation: the
        # navigator calls fzf once per level, so a single canned answer could
        # only ever test a one-step browse.
        self.script = os.path.join(self.tmp, "picks")
        with open(os.path.join(self.bin, "fzf"), "w") as f:
            f.write("#!/bin/sh\n"
                    "n=$(head -1 %s); sed -i.bak 1d %s\n"
                    "sed -n \"$((n+1))p\"\n" % (self.script, self.script))
        os.chmod(os.path.join(self.bin, "fzf"), 0o755)

    def picks(self, *rows):
        with open(self.script, "w") as f:
            f.write("".join(f"{r}\n" for r in rows))
        return {"CLAUDE_MV_SOURCE": "fs", "CLAUDE_MV_PICKER": "fzf",
                "PATH": self.bin + os.pathsep + os.environ["PATH"]}

    def test_choosing_this_directory_ends_the_browse(self):
        # row 0 is always "use this directory", at both prompts
        r = self.run_mv("--extract", "--session", self.HERO, self.proj,
                        env_extra=self.picks(0, 0), cwd=self.code)
        self.assertIn("done", r.stdout)
        d = os.path.join(self.projects, cm.enc(self.proj))
        self.assertTrue(os.path.exists(os.path.join(d, f"{self.HERO}.jsonl")))

    def test_descending_into_a_child_then_choosing_it(self):
        """src browse: descend into newproj (row 2 — after "use this" and
        "up"), then choose it. Nothing is homed there, so it stops — which is
        the proof the navigation actually moved."""
        r = self.run_mv("--extract", "--session", self.HERO, self.proj,
                        env_extra=self.picks(2, 0), cwd=self.code, expect=1)
        self.assertIn("no sessions are homed in", r.stderr)
        self.assertIn(self.proj, r.stderr)

    def test_escaping_the_browse_cancels(self):
        with open(os.path.join(self.bin, "fzf"), "w") as f:
            f.write("#!/bin/sh\nexit 130\n")
        os.chmod(os.path.join(self.bin, "fzf"), 0o755)
        r = self.run_mv("--extract", "--session", self.HERO, self.proj,
                        env_extra={"CLAUDE_MV_SOURCE": "fs",
                                   "CLAUDE_MV_PICKER": "fzf",
                                   "PATH": self.bin + os.pathsep
                                           + os.environ["PATH"]},
                        cwd=self.code, expect=1)
        self.assertIn("cancelled", r.stdout)


@unittest.skipUnless(shutil.which("sh"), "no shell")
class TestSessionPickerWithFzf(SessionFixture):
    """The fzf path, with a stub standing in for fzf itself — the same
    technique ccfind's own README generator uses. What is under test is the
    contract with fzf (a marked row comes back and maps to the right
    session), not fzf."""

    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.bin, exist_ok=True)

    def write_fzf(self, body):
        p = os.path.join(self.bin, "fzf")
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o755)

    def env(self):
        return {"CLAUDE_MV_SOURCE": "fs", "CLAUDE_MV_PICKER": "fzf",
                "PATH": self.bin + os.pathsep + os.environ["PATH"]}

    def test_fzf_is_asked_to_stay_inline_and_read_downward(self):
        """Without --height fzf takes the alternate screen, so the command and
        everything above it vanish while you pick — a lot of screen to borrow
        for choosing one row. And its default layout builds upward, against
        every other line this tool prints. Both are easy to drop by accident
        when the invocation is edited, so both are pinned."""
        argv_path = os.path.join(self.tmp, "argv")
        # NB the shell's own %s would collide with a python format string.
        self.write_fzf("#!/bin/sh\nfor a in \"$@\"; do echo \"$a\"; done > "
                       + shlex.quote(argv_path) + "\nsed -n 1p\n")
        # --no-browse so the only fzf run is the session picker; otherwise the
        # two folder pickers run either side of it and the file records
        # whichever went last.
        self.run_mv("--extract", "--no-browse", self.code, self.proj,
                    env_extra=self.env())
        with open(os.path.join(self.tmp, "argv")) as f:
            argv = f.read().split("\n")
        self.assertIn("--reverse", argv)
        height = [a for a in argv if a.startswith("--height=")]
        self.assertTrue(height, f"no --height in {argv}")
        # sized to the list rather than a fixed slab of screen
        self.assertEqual(height[0], "--height=6")   # 3 sessions + 3 chrome

    def test_fzf_keeps_the_terminal_to_draw_on(self):
        """The regression this guards is an invisible HANG.

        In --height mode fzf probes the terminal — and draws — on STDERR, not
        stdout. Capture that stream and the probe lands in a pipe instead of
        the terminal, nothing ever replies, and fzf waits forever for an
        answer that cannot come, having rendered nothing: the command appears
        to do nothing at all until it is killed. Only older fzf is affected
        (newer builds open /dev/tty themselves), which is exactly why it
        survives a laptop with a current fzf and strands an older server.

        So: whatever fzf writes to stderr must still be ours to see. The stub
        writes a marker there, and the assertion is that it came through.
        """
        self.write_fzf("#!/bin/sh\nprintf PROBEMARKER >&2\nsed -n 1p\n")
        r = self.run_mv("--extract", "--no-browse", self.code, self.proj,
                        env_extra=self.env())
        self.assertIn("PROBEMARKER", r.stderr,
                      "fzf's stderr was captured — its --height probe never "
                      "reaches the terminal, which hangs it invisibly")

    def test_the_marked_row_is_the_session_that_moves(self):
        # second row of the menu, mapped back by its index column
        self.write_fzf("#!/bin/sh\nsed -n 2p\n")
        self.run_mv("--extract", "--no-browse", self.code, self.proj, env_extra=self.env())
        moved = [n[:-6] for n in os.listdir(
            os.path.join(self.projects, cm.enc(self.proj)))
            if n.endswith(".jsonl")]
        self.assertEqual(moved, [self.SIBLING])   # newest-first row 2

    def test_multiple_marks_move_together(self):
        self.write_fzf("#!/bin/sh\nsed -n '1p;3p'\n")
        self.run_mv("--extract", "--no-browse", self.code, self.proj, env_extra=self.env())
        moved = sorted(n[:-6] for n in os.listdir(
            os.path.join(self.projects, cm.enc(self.proj)))
            if n.endswith(".jsonl"))
        self.assertEqual(moved, sorted([self.HERO, self.OTHER]))

    def test_escaping_the_picker_cancels(self):
        self.write_fzf("#!/bin/sh\nexit 130\n")
        r = self.run_mv("--extract", "--no-browse", self.code, self.proj,
                        env_extra=self.env(), expect=1)
        self.assertIn("nothing selected", r.stdout)
        self.assertFalse(os.path.exists(
            os.path.join(self.projects, cm.enc(self.proj))))


# ── searching the transcripts, and how wide to look ─────────────────────────

class SearchFixture(FixtureCase):
    """Sessions with something to find in them, in a tree worth recursing into.

    Deliberately spread across the three places a match can hide: the opening
    line the picker already showed, a turn further down that it never did, and
    a record nobody spoke at all.
    """

    HERO = "aaaaaaaa-1111-1111-1111-111111111111"    # says it mid-conversation
    QUIET = "bbbbbbbb-2222-2222-2222-222222222222"   # never says it
    OPENER = "cccccccc-3333-3333-3333-333333333333"  # says it in line one
    NESTED = "dddddddd-4444-4444-4444-444444444444"  # homed one folder down
    COUSIN = "eeeeeeee-5555-5555-5555-555555555555"  # a folder encoding alike
    TOOLY = "ffffffff-6666-6666-6666-666666666666"   # only a tool result says

    FS = {"CLAUDE_MV_SOURCE": "fs", "CLAUDE_MV_PICKER": "plain",
          "CLAUDE_MV_FORCE_PROMPT": "1"}

    def setUp(self):
        super().setUp()
        self.proj = self.make_folder("newproj")
        self.api = self.make_folder("api")
        # Same encoding as `code/api`, a different folder: enc() maps both
        # onto one name, so the recursive walk has to confirm against what a
        # session recorded rather than trust the directory name.
        self.cousin = self.code + "-api"
        os.makedirs(self.cousin, exist_ok=True)
        self.make_session(self.code, self.HERO, prompt="build me a thing",
                          says=[("assistant", "the nginx config wants a "
                                              "server block here")],
                          mtime=3000)
        self.make_session(self.code, self.QUIET, prompt="unrelated work",
                          mtime=2000)
        self.make_session(self.code, self.OPENER,
                          prompt="set up nginx behind the proxy", mtime=1000)
        self.make_session(self.api, self.NESTED, prompt="api scaffolding",
                          says=[("assistant", "nginx sits in front of it")],
                          mtime=2500)
        self.make_session(self.cousin, self.COUSIN,
                          prompt="nginx in the folder that encodes alike",
                          mtime=2200)
        self.make_session(self.code, self.TOOLY, prompt="check the logs",
                          raw=[{"type": "user", "cwd": self.code,
                                "sessionId": self.TOOLY,
                                "message": {"role": "user", "content": [
                                    {"type": "tool_result",
                                     "content": "ENOENT: zephyr.conf missing"}
                                ]}}],
                          mtime=900)
        self.add_config(self.code)
        self.write_fixture()

    def offered(self, *args, env_extra=None):
        """(run, ids) — what the picker was given, from a run cancelled at it.

        Cancelling is the point: the list is the thing under test, and a run
        that went on to move something would be testing the move as well.
        """
        r = self.run_mv("--extract", "--no-browse", *args, self.code,
                        self.proj, stdin="\n", env_extra=env_extra or self.FS,
                        expect=1)
        return r, re.findall(r"^\s+\d+\s+\S+ \S+\s+([0-9a-f]{8})\s", r.stdout,
                             re.M)

    def moved_ids(self, cwd):
        d = os.path.join(self.projects, cm.enc(cwd))
        return sorted(n[:-len(".jsonl")] for n in os.listdir(d)
                      if n.endswith(".jsonl")) if os.path.isdir(d) else []


class TestSessionSearch(SearchFixture):
    """--search: which sessions the query leaves on the list."""

    def test_the_whole_conversation_is_searched_not_the_opening_line(self):
        """The reason the flag exists. HERO's only mention of nginx is in a
        reply, which is precisely what the picker never showed."""
        _, ids = self.offered("--search", "nginx")
        self.assertEqual(sorted(ids),
                         sorted([self.HERO[:8], self.OPENER[:8]]))

    def test_a_session_that_never_mentions_it_is_not_offered(self):
        _, ids = self.offered("--search", "nginx")
        self.assertNotIn(self.QUIET[:8], ids)

    def test_case_is_ignored(self):
        _, ids = self.offered("--search", "NGINX")
        self.assertEqual(sorted(ids),
                         sorted([self.HERO[:8], self.OPENER[:8]]))

    def test_the_words_are_one_phrase_not_a_word_soup(self):
        """ccfind matches with grep -F, so claude-mv does too: the words are
        one literal string in the order they were typed, not an AND."""
        _, ids = self.offered("--search", "nginx config")
        self.assertEqual(ids, [self.HERO[:8]])
        r = self.run_mv("--extract", "--no-browse", "--search", "config nginx",
                        self.code, self.proj, env_extra=self.FS, expect=1)
        self.assertIn("none of the", r.stderr)

    def test_a_line_nobody_said_still_counts(self):
        """Matching runs over the raw record, so a tool result, a path or an
        error message is findable even though no turn ever spoke it — which is
        often exactly how a conversation is remembered."""
        r, ids = self.offered("--search", "zephyr")
        self.assertEqual(ids, [self.TOOLY[:8]])
        self.assertIn("zephyr.conf missing", r.stdout)

    def test_the_matching_line_is_shown_beside_the_opening_one(self):
        r, _ = self.offered("--search", "nginx")
        hero = [ln for ln in r.stdout.splitlines() if self.HERO[:8] in ln][0]
        self.assertIn("build me a thing", hero)      # what it is
        self.assertIn("server block here", hero)     # why it is on the list

    def test_a_match_already_visible_in_the_opening_line_is_not_repeated(self):
        r, _ = self.offered("--search", "nginx")
        opener = [ln for ln in r.stdout.splitlines()
                  if self.OPENER[:8] in ln][0]
        self.assertNotIn("↦", opener)

    def test_nothing_matching_says_how_many_were_searched(self):
        """"No sessions here" and "none of these" are different mistakes, and
        only the first one is about the path."""
        r = self.run_mv("--extract", "--no-browse", "--search", "kubernetes",
                        self.code, self.proj, env_extra=self.FS, expect=1)
        self.assertIn("none of the 4 sessions", r.stderr)

    def test_a_capped_search_says_it_was_capped(self):
        """A plain list is capped quietly — the newest N is a fine answer to
        "show me the sessions". A capped search is not: the one you are
        looking for may be the one that fell off."""
        r, ids = self.offered("--limit", "1", "--search", "nginx")
        self.assertEqual(ids, [self.HERO[:8]])
        self.assertIn("2 sessions match", r.stderr)
        self.assertIn("--limit", r.stderr)

    def test_a_found_session_can_then_be_moved(self):
        self.run_mv("--extract", "--no-browse", "--search", "nginx", self.code,
                    self.proj, stdin="1\ny\n", env_extra=self.FS)
        self.assertEqual(self.moved_ids(self.proj), [self.HERO])

    def test_searching_needs_the_mode(self):
        r = self.run_mv("--search", "nginx", self.code, self.proj,
                        env_extra=self.FS, expect=2)
        self.assertIn("needs --extract", r.stderr)

    def test_an_empty_search_is_refused(self):
        r = self.run_mv("--extract", "--no-browse", "--search", "  ",
                        self.code, self.proj, env_extra=self.FS, expect=2)
        self.assertIn("something to look for", r.stderr)

    def test_naming_ids_and_searching_are_the_same_question_twice(self):
        r = self.run_mv("--extract", "--no-browse", "--search", "nginx",
                        "--session", self.HERO, self.code, self.proj,
                        env_extra=self.FS, expect=2)
        self.assertIn("two ways to say which sessions", r.stderr)


class TestSearchScope(SearchFixture):
    """-R: how far down the tree the candidates come from."""

    def test_one_folder_is_the_default(self):
        _, ids = self.offered("--search", "nginx")
        self.assertNotIn(self.NESTED[:8], ids)

    def test_recursive_reaches_the_folders_below(self):
        _, ids = self.offered("-R", "--search", "nginx")
        self.assertIn(self.NESTED[:8], ids)

    def test_a_folder_that_merely_encodes_alike_is_not_swept_in(self):
        """enc() maps code/api and code-api onto the same name. Only what a
        session recorded can tell a nested project from an unrelated sibling,
        and the sibling's history is not ours to move."""
        _, ids = self.offered("-R", "--search", "nginx")
        self.assertNotIn(self.COUSIN[:8], ids)

    def test_the_row_says_which_folder_it_came_out_of(self):
        r, _ = self.offered("-R", "--search", "nginx")
        nested = [ln for ln in r.stdout.splitlines()
                  if self.NESTED[:8] in ln][0]
        self.assertIn("./api/", nested)
        self.assertIn("./", [ln for ln in r.stdout.splitlines()
                             if self.HERO[:8] in ln][0])

    def test_a_flat_run_has_no_folder_column(self):
        r, _ = self.offered("--search", "nginx")
        self.assertNotIn("./", r.stdout)

    def test_a_match_only_below_says_so_rather_than_nothing(self):
        r = self.run_mv("--extract", "--no-browse", "--search", "scaffolding",
                        self.code, self.proj, env_extra=self.FS, expect=1)
        self.assertIn("1 session below it mentions it", r.stderr)
        self.assertIn("--recursive", r.stderr)

    def test_a_session_from_below_really_moves(self):
        self.run_mv("--extract", "--no-browse", "-R", "--search",
                    "scaffolding", self.code, self.proj, stdin="1\ny\n",
                    env_extra=self.FS)
        self.assertEqual(self.moved_ids(self.proj), [self.NESTED])
        # NB: api/ and the cousin share one encoded dir — that is the trap
        # this fixture exists to set. Only the nested session left it.
        self.assertNotIn(self.NESTED, self.moved_ids(self.api))

    def test_the_whole_tree_can_be_listed_without_a_search(self):
        _, ids = self.offered("-R")
        self.assertIn(self.NESTED[:8], ids)
        self.assertNotIn(self.COUSIN[:8], ids)

    def test_recursing_needs_the_mode(self):
        r = self.run_mv("-R", self.code, self.proj, env_extra=self.FS,
                        expect=2)
        self.assertIn("needs --extract", r.stderr)

    def test_the_folder_browser_counts_the_tree_it_offers(self):
        """Step one annotates each directory with what it holds. Under -R the
        row is offering a tree, so the count has to be the tree's — a count of
        the one folder would be a different number from the list the next step
        goes on to show."""
        self.assertEqual(cm.session_count([self.profile], self.code), 4)
        # 5 are homed under code; the count says 6 because api/ and the
        # cousin share one encoded dir and this is a listdir, not a read. The
        # row is a signpost — see session_count() on why it stays cheap.
        self.assertEqual(
            cm.session_count([self.profile], self.code, recursive=True), 6)
        rows = cm.dir_rows(self.code, [self.profile], recursive=True)
        self.assertIn("6 sessions", rows[0][1])

    def test_the_prompt_says_it_is_about_to_search_a_tree(self):
        r = self.run_mv("--extract", "-R", "--session", self.HERO, self.proj,
                        stdin="\n\ny\n", env_extra=self.FS, cwd=self.code)
        self.assertIn("Which folder to search under?", r.stdout)


CCFIND_SEARCH_STUB = r'''#!/usr/bin/env python3
"""Stand-in for ccfind that answers a SEARCH, and records how it was asked.

Writes its argv to $STUB_ARGV and fills in the echo fields of the canned
document ($STUB_DOC) from the flags it actually received — so a test poses
"ccfind heard a different question" by overriding one field and everything
else still answers honestly.
"""
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["STUB_ARGV"], "w") as f:
    f.write("\n".join(argv))
if "--json" not in argv:
    sys.exit(2)
doc = json.load(open(os.environ["STUB_DOC"]))
doc.setdefault("query", argv[-1] if "--" in argv else "")
doc.setdefault("scope_exact", "-x" in argv)
doc.setdefault("case_sensitive", "-I" not in argv)
json.dump(doc, sys.stdout)
'''


class TestSearchThroughCcfind(SearchFixture):
    """The search ccfind answers has to be the search we asked for.

    ccfind greps with -F wherever it is installed, so it — not claude-mv —
    decides which sessions match. Everything here is the handshake that keeps
    that from silently becoming a different question.
    """

    def setUp(self):
        super().setUp()
        self.stub = os.path.join(self.tmp, "ccfind-stub")
        with open(self.stub, "w") as f:
            f.write(CCFIND_SEARCH_STUB)
        os.chmod(self.stub, 0o755)
        self.argv = os.path.join(self.tmp, "argv")
        self.doc = os.path.join(self.tmp, "doc.json")

    def answer(self, ids=(), **over):
        doc = {"version": 1, "scope": self.code, "total": len(ids),
               "shown": len(ids), "truncated": False,
               "results": [{"epoch": 3000, "host": "local", "profile": "p",
                            "config_dir": self.profile, "id": sid,
                            "cwd": self.code, "mtime": "2026-01-01 00:00:00",
                            "snippet": '{"raw":"json window"}',
                            "path": os.path.join(
                                self.projects, cm.enc(self.code),
                                sid + ".jsonl")} for sid in ids]}
        doc.update(over)
        with open(self.doc, "w") as f:
            json.dump(doc, f)
        return {"CLAUDE_MV_CCFIND_BIN": self.stub, "STUB_DOC": self.doc,
                "STUB_ARGV": self.argv, "CLAUDE_MV_SOURCE": "ccfind",
                "CLAUDE_MV_PICKER": "plain", "CLAUDE_MV_FORCE_PROMPT": "1"}

    def asked(self):
        with open(self.argv) as f:
            return f.read().splitlines()

    def test_the_query_goes_down_to_ccfind(self):
        self.offered("--search", "nginx", env_extra=self.answer([self.HERO]))
        argv = self.asked()
        self.assertEqual(argv[-2:], ["--", "nginx"])
        self.assertIn("-x", argv)          # the default scope: one folder

    def test_case_folding_is_pinned_rather_than_left_to_the_machine(self):
        """Without -I, CCFIND_CASE on this machine would decide what --search
        means, and the fallback matcher would disagree with it on exactly the
        machines that set it."""
        self.offered("--search", "nginx", env_extra=self.answer([self.HERO]))
        self.assertIn("-I", self.asked())

    def test_a_recursive_search_drops_the_exact_scope(self):
        self.offered("-R", "--search", "nginx",
                     env_extra=self.answer([self.HERO], scope_exact=False))
        self.assertNotIn("-x", self.asked())

    def test_a_query_ccfind_heard_differently_is_refused(self):
        """ccfind reads a leading word that names one of its profiles as a
        filter, not as text. The echoed query is how we find out; the walk is
        what answers instead."""
        env = self.answer([self.HERO, self.QUIET], query="soup")
        r, ids = self.offered("--search", "nginx", env_extra=env)
        self.assertIn("could not answer", r.stderr)
        self.assertEqual(ids, [])

    def test_an_answer_matched_with_the_wrong_case_folding_is_refused(self):
        env = self.answer([self.HERO], case_sensitive=True)
        r, _ = self.offered("--search", "nginx", env_extra=env)
        self.assertIn("could not answer", r.stderr)

    def test_an_answer_about_the_wrong_scope_is_refused_both_ways(self):
        """scope_exact has to say back what we asked: True when we sent -x,
        and False when we deliberately did not. An answer about one folder to
        a question about a tree is the same kind of wrong."""
        r, _ = self.offered("-R", "--search", "nginx",
                            env_extra=self.answer([self.HERO],
                                                  scope_exact=True))
        self.assertIn("could not answer", r.stderr)

    def test_ccfinds_hits_are_shown_in_our_own_words(self):
        """ccfind's snippet is a window on the raw JSON line that matched —
        right for a search tool printing lines, wrong for a picker offering
        conversations."""
        r, ids = self.offered("--search", "nginx",
                              env_extra=self.answer([self.HERO]))
        self.assertEqual(ids, [self.HERO[:8]])
        self.assertNotIn("json window", r.stdout)
        self.assertIn("server block here", r.stdout)

    def test_a_hit_we_cannot_point_at_keeps_its_row(self):
        """ccfind greps bytes in the machine's locale; we read decoded text.
        A match it saw and we cannot find is still ccfind's answer — the row
        stays, it just says nothing about where the match was."""
        r, ids = self.offered("--search", "nginx",
                              env_extra=self.answer([self.QUIET]))
        self.assertEqual(ids, [self.QUIET[:8]])
        self.assertNotIn("↦", r.stdout)


# ── the survey page: the last screen before anything is written ─────────────

class TestSurveyPage(SearchFixture):
    """The guide asks three questions and then acts. This is the fourth.

    Less a fourth question than the first chance to see the answers to the
    other three in one place — which is the only place the "wrong row"
    mistake is visible while it is still free to fix.
    """

    def test_it_says_what_moves_where_before_anything_is_written(self):
        r = self.run_mv("--extract", "--no-browse", "--search", "nginx",
                        self.code, self.proj, stdin="1\ny\n",
                        env_extra=self.FS)
        survey = r.stdout.split("about to re-home")[1].split("proceed?")[0]
        self.assertIn(self.HERO[:8], survey)
        self.assertIn(self.proj, survey)
        self.assertIn("no folder is moved", survey)
        self.assertLess(r.stdout.index("about to re-home"),
                        r.stdout.index("restore point"))

    def test_declining_changes_nothing(self):
        r = self.run_mv("--extract", "--no-browse", self.code, self.proj,
                        stdin="1\nn\n", env_extra=self.FS, expect=1)
        self.assertIn("cancelled", r.stdout)
        self.assertEqual(self.moved_ids(self.proj), [])
        self.assertEqual(self.restore_stamps(), [])

    def test_end_of_input_is_not_consent(self):
        r = self.run_mv("--extract", "--no-browse", self.code, self.proj,
                        stdin="1\n", env_extra=self.FS, expect=1)
        self.assertEqual(self.moved_ids(self.proj), [])

    def test_force_says_the_watching_is_over(self):
        self.run_mv("--extract", "--no-browse", "--force", "--session",
                    self.HERO, self.code, self.proj, env_extra=self.FS)
        self.assertEqual(self.moved_ids(self.proj), [self.HERO])

    def test_a_dry_run_has_nothing_to_confirm(self):
        r = self.run_mv("--extract", "--no-browse", "-n", "--session",
                        self.HERO, self.code, self.proj, env_extra=self.FS)
        self.assertNotIn("proceed?", r.stdout)

    def test_a_pipe_is_not_asked_and_still_runs(self):
        """--no-browse with --session is the scripted shape; a prompt nobody
        can answer would turn every one of those runs into a refusal."""
        self.run_mv("--extract", "--no-browse", "--session", self.HERO,
                    self.code, self.proj,
                    env_extra={"CLAUDE_MV_SOURCE": "fs"})
        self.assertEqual(self.moved_ids(self.proj), [self.HERO])

    def test_what_a_conflict_will_do_is_on_the_page(self):
        self.make_session(self.proj, self.HERO, prompt="the newer copy")
        r = self.run_mv("--extract", "--no-browse", "--session", self.HERO,
                        self.code, self.proj, stdin="y\n", env_extra=self.FS)
        self.assertIn("already at the destination", r.stdout)
        self.assertIn("skipped", r.stdout)

    def test_the_page_names_the_tree_when_the_scope_was_a_tree(self):
        r = self.run_mv("--extract", "--no-browse", "-R", "--search",
                        "scaffolding", self.code, self.proj, stdin="1\ny\n",
                        env_extra=self.FS)
        survey = r.stdout.split("about to re-home")[1].split("proceed?")[0]
        self.assertIn("and below", survey)
        self.assertIn("./api/", survey)


# ── wrapper: which profiles the zsh layer decides to pass ───────────────────

ZSH = shutil.which("zsh")


@unittest.skipUnless(ZSH, "zsh not installed")
class TestWrapperProfileResolution(unittest.TestCase):
    """claude-mv.zsh chooses WHICH profiles the python is told about.

    Invisible to every layer above, all of which pass --profile themselves. The
    order is: CLAUDE_PROFILE_DIRS from .env, else claude-profile when it is
    installed, else ~/.claude plus ~/.claude-personal.

    The wrapper is copied into a tmpdir beside a copy of claude-mv.py, so it
    reads a .env under our control and finds no sibling claude-profile clone —
    the real checkout would supply both and mask the case being tested.
    """

    STUB = ("#!/bin/sh\n"
            "[ \"$1\" = list ] || exit 1\n"
            "printf 'work\\t~/.claude\\tactive\\n'\n"
            "printf 'personal\\t~/.claude-personal\\t\\n'\n"
            "printf 'client\\t~/.claude-client\\t\\n'\n")

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="claude-mv-zsh-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        here = os.path.dirname(SCRIPT)
        shutil.copy2(SCRIPT, self.repo)
        shutil.copy2(os.path.join(here, "claude-mv.zsh"), self.repo)

        self.home = os.path.join(self.tmp, "home")
        for d in (".claude", ".claude-personal", ".claude-client", "code/proj"):
            os.makedirs(os.path.join(self.home, d))

        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.stub = os.path.join(self.bin, "claude-profile")
        self.write_stub(self.STUB)

    def write_stub(self, body):
        with open(self.stub, "w") as f:
            f.write(body)
        os.chmod(self.stub, 0o755)

    def resolve(self, prelude="", dotenv=None, stub_on_path=False, **env_extra):
        """The profile dirs a real `claude-mv -n` ends up migrating, read back
        off its own report — the wrapper's decision as the python received it,
        not a re-implementation of the lookup."""
        dotenv_path = os.path.join(self.repo, ".env")
        if dotenv is None:
            if os.path.exists(dotenv_path):
                os.remove(dotenv_path)
        else:
            with open(dotenv_path, "w") as f:
                f.write(dotenv)
        env = dict(os.environ, HOME=self.home,
                   CLAUDE_MV_RESTORE_ROOT=os.path.join(self.tmp, "restore"))
        for k in ("CLAUDE_PROFILE_SCRIPT", "CLAUDE_MV_FORCE_PROMPT"):
            env.pop(k, None)
        env.update(env_extra)
        if stub_on_path:
            env["PATH"] = self.bin + os.pathsep + env["PATH"]
        script = "%s\nsource %s/claude-mv.zsh\nclaude-mv -n %s %s\n" % (
            prelude, shlex.quote(self.repo),
            shlex.quote(os.path.join(self.home, "code/proj")),
            shlex.quote(os.path.join(self.home, "code/renamed")))
        r = subprocess.run([ZSH, "-c", script], capture_output=True, text=True,
                           env=env)
        marker = "\u2500\u2500 profile "
        dirs = [ln.split(marker, 1)[1].strip()
                for ln in r.stdout.splitlines() if marker in ln]
        return [os.path.relpath(d, self.home) for d in dirs]

    DEFAULT = [".claude", ".claude-personal"]
    VIA_PROFILE = [".claude", ".claude-personal", ".claude-client"]

    def test_falls_back_to_the_built_in_default(self):
        self.assertEqual(self.resolve(), self.DEFAULT)

    def test_uses_claude_profile_when_it_is_a_binary_on_path(self):
        self.assertEqual(self.resolve(stub_on_path=True), self.VIA_PROFILE)

    def test_uses_claude_profile_when_it_is_a_zsh_function(self):
        """The shape it actually has in an interactive shell — and the one a
        $commands lookup would miss entirely."""
        prelude = "claude-profile() { %s \"$@\" }" % shlex.quote(self.stub)
        self.assertEqual(self.resolve(prelude=prelude), self.VIA_PROFILE)

    def test_claude_profile_script_override_is_honoured(self):
        py = os.path.join(self.tmp, "cp.py")
        with open(py, "w") as f:
            f.write("print('solo\\t~/.claude-client\\t')\n")
        self.assertEqual(self.resolve(CLAUDE_PROFILE_SCRIPT=py),
                         [".claude-client"])

    def test_a_claude_profile_script_that_is_not_there_means_not_installed(self):
        """Authoritative: the other candidates are not consulted, even with the
        stub sitting on PATH."""
        self.assertEqual(
            self.resolve(stub_on_path=True,
                         CLAUDE_PROFILE_SCRIPT=os.path.join(self.tmp, "nope.py")),
            self.DEFAULT)

    def test_a_failing_claude_profile_falls_through_silently(self):
        self.write_stub("#!/bin/sh\necho boom >&2\nexit 3\n")
        self.assertEqual(self.resolve(stub_on_path=True), self.DEFAULT)

    def test_a_claude_profile_that_answers_nothing_falls_through(self):
        self.write_stub("#!/bin/sh\nexit 0\n")
        self.assertEqual(self.resolve(stub_on_path=True), self.DEFAULT)

    def test_dotenv_pins_the_list_and_skips_claude_profile(self):
        self.assertEqual(
            self.resolve(stub_on_path=True,
                         dotenv='typeset -a CLAUDE_PROFILE_DIRS=("$HOME/.claude")\n'),
            [".claude"])

    def test_a_profile_dir_that_does_not_exist_is_dropped(self):
        self.write_stub("#!/bin/sh\n"
                        "[ \"$1\" = list ] || exit 1\n"
                        "printf 'work\\t~/.claude\\tactive\\n'\n"
                        "printf 'ghost\\t~/.claude-ghost\\t\\n'\n")
        self.assertEqual(self.resolve(stub_on_path=True), [".claude"])


@unittest.skipUnless(ZSH, "zsh not installed")
class TestWrapperCcfindResolution(unittest.TestCase):
    """claude-mv.zsh decides WHERE ccfind is, and the python cannot.

    ccfind is a zsh function in an interactive shell, so there is usually no
    file on PATH to exec — only this layer can see it, and only zsh knows
    (via $functions_source) which script defined it. Everything else in the
    suite bypasses the question by setting CLAUDE_MV_CCFIND_SOURCE itself.
    """

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="claude-mv-ccf-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        here = os.path.dirname(SCRIPT)
        shutil.copy2(SCRIPT, self.repo)
        shutil.copy2(os.path.join(here, "claude-mv.zsh"), self.repo)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        # A stand-in ccfind.zsh: sourcing it defines the function, which is
        # all the resolver looks at.
        self.script = os.path.join(self.tmp, "ccfind.zsh")
        with open(self.script, "w") as f:
            f.write("ccfind() { print -r -- stub }\n")

    def resolved(self, prelude="", **env_extra):
        """What the wrapper hands the python as CLAUDE_MV_CCFIND_SOURCE."""
        env = dict(os.environ, HOME=self.home)
        for k in ("CLAUDE_MV_CCFIND_SCRIPT", "CLAUDE_MV_CCFIND_SOURCE"):
            env.pop(k, None)
        env.update(env_extra)
        # --restore with nothing to restore exits early, so this asks the
        # resolver its question without running a migration.
        script = ("%s\nsource %s/claude-mv.zsh\n"
                  "_claude_mv_ccfind_source || print -r -- NONE\n"
                  % (prelude, shlex.quote(self.repo)))
        r = subprocess.run([ZSH, "-c", script], capture_output=True, text=True,
                           env=env)
        return r.stdout.strip()

    def test_a_loaded_function_is_traced_to_its_file(self):
        """The shape ccfind actually has in an interactive shell. `command -v`
        finds it but cannot say where it came from, and the python cannot call
        it at all — $functions_source is the only thing that bridges them."""
        self.assertEqual(self.resolved(prelude=f"source {shlex.quote(self.script)}"),
                         self.script)

    def test_the_override_wins_and_is_authoritative(self):
        other = os.path.join(self.tmp, "elsewhere.zsh")
        with open(other, "w") as f:
            f.write("ccfind() { : }\n")
        self.assertEqual(
            self.resolved(prelude=f"source {shlex.quote(self.script)}",
                          CLAUDE_MV_CCFIND_SCRIPT=other), other)

    def test_an_override_that_is_not_there_means_not_installed(self):
        """Same contract as CLAUDE_PROFILE_SCRIPT: if the user says where it
        lives and it is not there, it is not installed — the other candidates
        are not consulted, even with a perfectly good ccfind loaded."""
        self.assertEqual(
            self.resolved(prelude=f"source {shlex.quote(self.script)}",
                          CLAUDE_MV_CCFIND_SCRIPT=os.path.join(self.tmp, "nope")),
            "NONE")

    def test_a_sibling_clone_is_found(self):
        sib = os.path.join(self.tmp, "ccfind")
        os.makedirs(sib)
        shutil.copy2(self.script, os.path.join(sib, "ccfind.zsh"))
        self.assertEqual(self.resolved(), os.path.join(sib, "ccfind.zsh"))

    def test_no_ccfind_anywhere_is_not_an_error(self):
        """The soft contract: --extract still works, off the filesystem."""
        self.assertEqual(self.resolved(), "NONE")


# ── cross-host bundles: what --export carries, and what it refuses ──────────

class ExportFixture(FixtureCase):
    """A project with a nested sub-project, an encode-alike sibling, and the
    session-keyed stores on both sides of the carry/refuse line."""

    def seed(self):
        self.proj = self.make_folder("my-project")
        os.makedirs(os.path.join(self.proj, "sub"), exist_ok=True)
        self.sub = os.path.join(self.proj, "sub")
        self.make_project(self.proj, sessions=("aaaa-1111",))
        self.make_project(self.sub, sessions=("bbbb-2222",))
        # The sidecar: session-keyed, but living INSIDE the project dir.
        side = os.path.join(self.projects, cm.enc(self.proj), "aaaa-1111",
                            "subagents")
        os.makedirs(side, exist_ok=True)
        with open(os.path.join(side, "agent-1.jsonl"), "w") as f:
            f.write(jsonl({"type": "user", "cwd": self.proj}))
        # memory/, which `claude project purge` counts as project state.
        mem = os.path.join(self.projects, cm.enc(self.proj), "memory")
        os.makedirs(mem, exist_ok=True)
        with open(os.path.join(mem, "note.md"), "w") as f:
            f.write("# remembered\n")
        # Encodes like a subdirectory of proj, is not one. enc() is lossy, so
        # only a recorded cwd separates them.
        alike = os.path.join(self.projects, cm.enc(self.proj) + "-elsewhere")
        os.makedirs(alike, exist_ok=True)
        with open(os.path.join(alike, "cccc-3333.jsonl"), "w") as f:
            f.write(jsonl({"type": "user", "cwd": "/somewhere/else",
                           "sessionId": "cccc-3333"}))
        self.store("tasks", "aaaa-1111.json", '{"todo":[]}')
        self.store("todos", "aaaa-1111-agent-aaaa-1111.json", "[]")
        self.store("shell-snapshots", "snapshot-zsh-1.sh", "unalias -a\n")
        self.store("session-env", "aaaa-1111", None)
        self.store("file-history", "aaaa-1111", None)
        self.add_config(self.proj, hasTrustDialogAccepted=True,
                        allowedTools=["Bash"])
        self.add_history(self.proj, "mine", session="aaaa-1111")
        self.add_history("/somewhere/else", "theirs")
        self.write_fixture()

    def store(self, name, entry, content):
        """One entry in a session-keyed store; content None makes it a dir."""
        root = os.path.join(self.profile, name)
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, entry)
        if content is None:
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "x"), "w") as f:
                f.write("x")
        else:
            with open(path, "w") as f:
                f.write(content)

    def export(self, *args, expect=0):
        """Run --export to a file and return (tar member names, manifest)."""
        out = os.path.join(self.tmp, "bundle.tgz")
        r = self.run_mv("--export", self.proj, "-o", out, *args, expect=expect)
        if expect != 0 or "--dry-run" in args or "-n" in args:
            return r, None, None
        with tarfile.open(out) as tar:
            names = tar.getnames()
            manifest = json.loads(
                tar.extractfile(cm.BUNDLE_MANIFEST).read().decode())
        return r, names, manifest


class TestExport(ExportFixture):
    """What goes into a bundle is a series of deliberate calls, and every one
    of them is a decision somebody could reasonably reverse. Each gets a test
    naming the reason, the same way --extract's three non-actions do."""

    def setUp(self):
        super().setUp()
        self.seed()

    def test_the_project_dir_travels_with_its_sidecar(self):
        _, names, _ = self.export()
        enc = cm.enc(self.proj)
        self.assertIn(f"projects/{enc}/aaaa-1111.jsonl", names)
        self.assertIn(f"projects/{enc}/aaaa-1111/subagents/agent-1.jsonl",
                      names, "the sidecar is session-keyed but lives inside "
                             "the project dir, so it has to be carried by hand")

    def test_memory_inside_the_project_dir_travels(self):
        """`claude project purge` counts memory/ as project state, and it is
        conversation content rather than anything host-bound."""
        _, names, _ = self.export()
        self.assertIn(f"projects/{cm.enc(self.proj)}/memory/note.md", names)

    def test_a_nested_project_travels(self):
        _, names, manifest = self.export()
        self.assertIn(f"projects/{cm.enc(self.sub)}/bbbb-2222.jsonl", names)
        self.assertIn(self.sub, [p["cwd"] for p in manifest["projects"]])

    def test_a_sibling_that_merely_encodes_alike_does_not(self):
        """enc() maps code/api and code-api onto one name. Only a session's
        recorded cwd separates them, so the confirm has to happen here too."""
        _, names, manifest = self.export()
        self.assertNotIn(f"projects/{cm.enc(self.proj)}-elsewhere/"
                         f"cccc-3333.jsonl", names)
        self.assertNotIn("cccc-3333",
                         [s for p in manifest["projects"]
                          for s in p["sessions"]])

    def test_only_this_projects_history_entries_are_carried(self):
        _, names, manifest = self.export()
        self.assertIn("history.jsonl", names)
        self.assertEqual(manifest["history"], 1)

    def test_session_keyed_stores_travel_by_id_prefix(self):
        """The stores spell themselves differently — <id>.json here,
        <id>-agent-<id>.json there — so the match is a prefix, not a name."""
        _, names, manifest = self.export()
        self.assertIn("stores/tasks/aaaa-1111.json", names)
        self.assertIn("stores/todos/aaaa-1111-agent-aaaa-1111.json", names)
        self.assertEqual(manifest["stores"], {"tasks": 1, "todos": 1})

    def test_host_bound_stores_are_refused(self):
        """shell-snapshots is the source machine's shell; session-env its
        environment; file-history the contents of files as they were THERE.
        The destination is a different machine and a fresh checkout."""
        _, names, manifest = self.export()
        for store in ("shell-snapshots", "session-env", "file-history"):
            self.assertFalse([n for n in names if store in n],
                             f"{store} must not travel")
            self.assertIn(store, manifest["excluded"])

    def test_the_carry_and_refuse_lists_cannot_overlap(self):
        """Asserted on the constants, not on a fixture, because one of the
        refusals is un-catchable through a bundle: shell-snapshots files are
        named snapshot-zsh-<ts>-<rand>.sh, so the session-id prefix match
        could never pick one up whichever list it is on. That makes the
        fixture pass for a reason that has nothing to do with the decision.
        Moving any store across the line has to turn this red on its own."""
        self.assertFalse(set(cm.CARRIED_STORES) & set(cm.REFUSED_STORES),
                         "a store cannot be both carried and refused")
        for store in ("file-history", "shell-snapshots", "session-env"):
            self.assertIn(store, cm.REFUSED_STORES)
            self.assertNotIn(store, cm.CARRIED_STORES)

    def test_the_config_entry_is_not_carried(self):
        """It holds the trust flag, allowedTools AND the project's MCP
        servers, which name binaries on the source host. Claude writes a
        fresh one on first run — the refusal --extract already makes."""
        _, names, manifest = self.export()
        self.assertFalse([n for n in names if "claude.json" in n])
        self.assertIs(manifest["config_entry"], False)
        self.assertFalse([n for n in names if "Bash" in n])

    def test_the_manifest_records_the_source_path(self):
        """The one field --import cannot work without: it is what the
        recorded cwds get remapped FROM."""
        _, _, manifest = self.export()
        self.assertEqual(manifest["source"]["path"], self.proj)
        self.assertEqual(manifest["format"], cm.BUNDLE_FORMAT)
        self.assertEqual(manifest["version"], cm.BUNDLE_VERSION)

    def test_the_source_is_left_untouched(self):
        """A fork, not a move: --export only reads."""
        before = sorted(os.listdir(self.projects))
        self.export()
        self.assertEqual(sorted(os.listdir(self.projects)), before)
        self.assertTrue(os.path.isdir(self.proj))
        self.assertIn(self.proj, self.read_config()["projects"])

    def test_nothing_keyed_on_src_is_an_error_not_an_empty_bundle(self):
        """A bundle carrying nothing is a mistyped path dressed up as a
        transfer — the same refusal the offer in TestOfferedReconcile makes."""
        empty = self.make_folder("never-used")
        out = os.path.join(self.tmp, "nope.tgz")
        r = self.run_mv("--export", empty, "-o", out, expect=1)
        self.assertIn("nothing to export", r.stderr)
        self.assertFalse(os.path.exists(out))

    def test_dry_run_writes_no_bundle(self):
        out = os.path.join(self.tmp, "dry.tgz")
        r = self.run_mv("--export", self.proj, "-o", out, "-n")
        self.assertFalse(os.path.exists(out))
        self.assertIn("dry run", r.stdout + r.stderr)


class TestExportStreaming(ExportFixture):
    """Piping is the whole transport story, so stdout has to be the tar and
    nothing else: `claude-mv --export … | ssh quim claude-mv --import …`."""

    def setUp(self):
        super().setUp()
        self.seed()

    def test_the_bundle_streams_to_stdout_with_the_report_on_stderr(self):
        env = dict(os.environ, CLAUDE_MV_RESTORE_ROOT=self.restore_root)
        r = subprocess.run(
            [sys.executable, SCRIPT, "--profile", self.profile,
             "--export", self.proj],
            capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr.decode(errors="replace"))
        self.assertTrue(r.stdout.startswith(b"\x1f\x8b"),
                        "stdout must be the gzip stream, not the report")
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tar:
            self.assertIn(cm.BUNDLE_MANIFEST, tar.getnames())
        self.assertIn(b"exporting", r.stderr)

    def test_a_written_bundle_reports_on_stdout_instead(self):
        """With -o there is no stream to protect, so the report goes where
        every other claude-mv report goes."""
        out = os.path.join(self.tmp, "b.tgz")
        r = self.run_mv("--export", self.proj, "-o", out)
        self.assertIn("exporting", r.stdout)


class TestExportGrammar(FixtureCase):
    """--export reads and writes a file; it is not a move, and the argument
    grammar should make that impossible to get half-right."""

    def test_a_second_positional_is_refused(self):
        d = self.make_folder("x")
        r = self.run_mv("--export", d, d + "-2", expect=2)
        self.assertIn("--export takes one path", r.stderr)

    def test_output_needs_export(self):
        d = self.make_folder("x")
        r = self.run_mv(d, d + "-2", "-o", "/tmp/x.tgz", expect=2)
        self.assertIn("needs --export", r.stderr)

    def test_export_does_not_combine_with_the_migrating_modes(self):
        d = self.make_folder("x")
        for flag in ("--extract", "--already-moved"):
            r = self.run_mv("--export", d, flag, expect=2)
            self.assertIn("--export only reads", r.stderr)


# ── live: drive the real Claude Code binary ─────────────────────────────────

def _claude_bin():
    return (os.environ.get("CLAUDE_MV_CLAUDE_BIN") or shutil.which("claude")
            or "/opt/homebrew/bin/claude")


@unittest.skipUnless(os.environ.get("CLAUDE_MV_LIVE_TEST"),
                     "set CLAUDE_MV_LIVE_TEST=1 to run against the real "
                     "claude binary")
@unittest.skipUnless(os.path.exists(_claude_bin()), "no claude binary")
class TestAgainstRealClaude(FixtureCase):
    """End-to-end with Claude Code itself, using it as the oracle.

    Every other test asserts claude-mv against our *model* of the on-disk
    format. This one has no model: real Claude writes the project dir, then
    claude-mv migrates it, then real Claude runs again at the new path — and
    if our re-keying matched what Claude would compute, Claude appends to
    the very dir we produced instead of creating a second one beside it.

    Costs nothing and needs no login: Claude lays down projects/<enc-cwd>/,
    the session jsonl and the config *before* it ever checks credentials, so
    an unauthenticated run still writes a genuine profile. Opt-in anyway —
    the default suite stays hermetic and shells out to nothing.
    """

    def _run_claude(self, cwd):
        """A real claude run in `cwd`, writing into the test profile."""
        env = dict(os.environ, CLAUDE_CONFIG_DIR=self.profile)
        subprocess.run([_claude_bin(), "-p", "Reply with exactly: OK"],
                       cwd=cwd, capture_output=True, text=True, timeout=180,
                       env=env)   # exit code ignored: unauthenticated is fine

    def _project_dirs_with_sessions(self):
        return sorted(n for n in os.listdir(self.projects)
                      if os.path.isdir(os.path.join(self.projects, n))
                      and any(f.endswith(".jsonl")
                              for f in os.listdir(os.path.join(self.projects, n))))

    def test_claude_finds_its_own_history_at_the_new_path(self):
        old = self.make_folder("lipsum")
        new = os.path.join(self.code, "foo")
        self.write_fixture()

        self._run_claude(old)
        dirs = self._project_dirs_with_sessions()
        self.assertEqual(dirs, [cm.enc(old)],
                         "real Claude did not encode cwd the way enc() does")
        before = len(os.listdir(os.path.join(self.projects, cm.enc(old))))

        self.run_mv(old, new)

        self.assertEqual(self._project_dirs_with_sessions(), [cm.enc(new)])
        self.assertEqual(self.session_cwds(new), [new] * len(self.session_cwds(new)))
        self.assertTrue(self.session_cwds(new), "no cwd lines survived")

        # The oracle step: Claude runs again at the new path. If claude-mv
        # picked the right dir name, Claude lands in it — no second dir.
        self._run_claude(new)

        self.assertEqual(self._project_dirs_with_sessions(), [cm.enc(new)],
                         "Claude created a second project dir — claude-mv's "
                         "re-keying disagrees with Claude's own encoding")
        after = len(os.listdir(os.path.join(self.projects, cm.enc(new))))
        self.assertGreater(after, before, "Claude wrote no new session file")


@unittest.skipUnless(os.environ.get("CLAUDE_MV_LIVE_TEST"),
                     "set CLAUDE_MV_LIVE_TEST=1 to drive the real claude UI")
@unittest.skipUnless(os.path.exists(_claude_bin()), "no claude binary")
@unittest.skipUnless(shutil.which("tmux"), "no tmux")
@unittest.skipUnless(os.path.isdir(REAL_PROFILE), "no profile to source a "
                                                  "real session from")
class TestResumePickerWithTmux(FixtureCase):
    """The end the user actually cares about: does `claude --resume` list it?

    Everything else stops at "the files are re-keyed correctly". This drives
    the real interactive picker under tmux and reads what Claude puts on the
    screen at the new path.

    Two things make it work without an API call or a login. The picker reads
    session files off disk, so it renders fine unauthenticated. And a session
    written by an unauthenticated run is *not* listed (no assistant turn, so
    Claude filters it out) — which would make a naive version of this test
    pass for the wrong reason, or fail for one. So it plants a genuine
    session file copied from the real profile, cwd rewritten to the fixture,
    rather than trying to generate one.

    test_plain_mv_orphans_the_session is the negative control: same setup,
    plain `mv` instead of claude-mv, and the picker must come up empty. If
    that ever passes, this whole class has stopped proving anything.
    """

    ONBOARDING = {"hasCompletedOnboarding": True, "theme": "dark",
                  "firstStartTime": "2026-01-01T00:00:00.000Z",
                  "installMethod": "cask"}

    def setUp(self):
        super().setUp()
        self._pane_n = 0

    def _seed_profile(self, *trusted):
        """Config that skips first-run onboarding and pre-trusts the folders."""
        ver = subprocess.run([_claude_bin(), "--version"], capture_output=True,
                             text=True).stdout.strip().split()[0]
        cfg = dict(self.ONBOARDING, lastOnboardingVersion=ver, projects={
            p: {"hasTrustDialogAccepted": True, "allowedTools": []}
            for p in trusted})
        with open(os.path.join(self.profile, ".claude.json"), "w") as f:
            json.dump(cfg, f, indent=2)
        with open(os.path.join(self.profile, "history.jsonl"), "w"):
            pass

    def _plant_real_session(self, cwd):
        """Copy a genuine session into projects/<enc(cwd)>/; return its title.

        Generated sessions can't be used: an unauthenticated run produces one
        the picker won't list. A real file is the only offline way to get
        content Claude considers resumable.
        """
        cands = [f for f in glob.glob(
            os.path.join(REAL_PROFILE, "projects", "*", "*.jsonl"))
            if 2_000 < os.path.getsize(f) < 200_000]
        for src in sorted(cands, key=os.path.getsize):
            title, lines = None, []
            with open(src, encoding="utf-8", errors="replace") as fh:
                raw = fh.readlines()
            for line in raw:
                try:
                    obj = json.loads(line)
                except ValueError:
                    lines.append(line)
                    continue
                if isinstance(obj.get("cwd"), str):
                    obj["cwd"] = cwd
                title = obj.get("aiTitle") or title
                lines.append(jsonl(obj))
            if not title:
                continue          # no aiTitle → nothing to match on screen
            d = os.path.join(self.projects, cm.enc(cwd))
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, os.path.basename(src)), "w") as f:
                f.writelines(lines)
            return title
        self.skipTest("no real session with an aiTitle to plant")

    def _picker_pane(self, cwd, wait_for, timeout=60):
        """Open `claude --resume` in cwd under tmux; return the pane text."""
        self._pane_n += 1
        sess = f"cmv-{os.getpid()}-{self._pane_n}"
        cmd = (f"CLAUDE_CONFIG_DIR={shlex.quote(self.profile)} "
               f"{shlex.quote(_claude_bin())} --resume; sleep 120")
        subprocess.run(["tmux", "new-session", "-d", "-s", sess,
                        "-x", "200", "-y", "40", "-c", cwd, cmd],
                       check=True, capture_output=True)
        self.addCleanup(subprocess.run, ["tmux", "kill-session", "-t", sess],
                        capture_output=True)

        def pane():
            return subprocess.run(["tmux", "capture-pane", "-t", sess, "-p"],
                                  capture_output=True, text=True).stdout

        def close(out):
            """Kill the picker before returning.

            Not just tidiness: `claude --resume` is a real live session, so
            leaving it up makes claude-mv's own liveness guard (correctly)
            refuse the next move in this test.
            """
            subprocess.run(["tmux", "kill-session", "-t", sess],
                           capture_output=True)
            for _ in range(20):        # wait for the pid to actually go
                if subprocess.run(["tmux", "has-session", "-t", sess],
                                  capture_output=True).returncode != 0:
                    break
                time.sleep(0.5)
            return out

        deadline, trusted = time.time() + timeout, False
        while time.time() < deadline:
            time.sleep(1)
            out = pane()
            if not trusted and "trust this folder" in out:
                # Belt and braces: the seeded config normally pre-empts this.
                subprocess.run(["tmux", "send-keys", "-t", sess, "Enter"])
                trusted = True
                continue
            if any(w in out for w in wait_for):
                time.sleep(2)     # let the list settle before reading it
                return close(pane())
        return close(pane())

    def test_resume_picker_lists_the_session_at_the_new_path(self):
        old = self.make_folder("lipsum")
        new = os.path.join(self.code, "foo")
        # Seed only the source: the destination must be untouched, or
        # claude-mv sees a pre-existing config key and reports a conflict.
        # hasTrustDialogAccepted rides along through the migration anyway.
        self._seed_profile(old)
        title = self._plant_real_session(old)

        before = self._picker_pane(old, [title, "No conversations"])
        self.assertIn(title, before,
                      "planted session was not listed even before the move — "
                      "fixture problem, not a claude-mv problem")

        self.run_mv(old, new)

        after = self._picker_pane(new, [title, "No conversations"])
        self.assertIn(title, after,
                      "claude --resume at the new path does not list the "
                      "moved session")
        self.assertNotIn("No conversations", after)

    def test_plain_mv_orphans_the_session(self):
        """Negative control — proves the test above can fail."""
        old = self.make_folder("lipsum")
        new = os.path.join(self.code, "foo")
        self._seed_profile(old)
        title = self._plant_real_session(old)

        os.rename(old, new)        # what claude-mv exists to improve on

        after = self._picker_pane(new, [title, "No conversations"])
        self.assertNotIn(title, after,
                         "a plain mv appeared to keep the history — the "
                         "picker assertions prove nothing")
        self.assertIn("No conversations", after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
