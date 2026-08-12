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

    def test_live_session_records_still_carry_cwd_and_pid(self):
        """What the live-session guard reads before it will let a move run."""
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


# ── end-to-end scaffolding ──────────────────────────────────────────────────

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
                    f.write(json.dumps(obj) + "\n")
        return d

    def add_config(self, cwd, **fields):
        entry = {"allowedTools": [], "hasTrustDialogAccepted": True}
        entry.update(fields)
        self.config["projects"][cwd] = entry

    def add_history(self, cwd, display="do a thing"):
        self.history.append({"display": display, "pastedContents": {},
                             "project": cwd})

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
                f.write(json.dumps(obj) + "\n")

    # -- runner + assertions ------------------------------------------------

    def run_mv(self, *args, expect=0, stdin="", home=None):
        env = dict(os.environ, CLAUDE_MV_RESTORE_ROOT=self.restore_root)
        env.pop("CLAUDE_MV_FORCE_PROMPT", None)
        if home:                      # for the ~ expansion test
            env["HOME"] = home
        r = subprocess.run(
            [sys.executable, SCRIPT, "--profile", self.profile, *args],
            capture_output=True, text=True, input=stdin, env=env)
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

    def test_missing_src_hints_at_already_moved(self):
        r = self.run_mv(os.path.join(self.code, "gone"),
                        os.path.join(self.code, "new"), expect=1)
        self.assertIn("--already-moved", r.stderr)


# ── end-to-end: how src/dst are spelled ─────────────────────────────────────

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
                lines.append(json.dumps(obj) + "\n")
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
