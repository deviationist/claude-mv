#!/usr/bin/env python3
"""Test suite for claude-mv. Stdlib only — run it with:

    python3 claude/claude-mv/tests/test_claude_mv.py          # or -v
    python3 -m unittest discover claude/claude-mv/tests

Four layers, each closing a gap the one before it can't see:

  * unit tests on the pure helpers (path encoding, canonicalization, config
    merging), loaded straight out of claude-mv.py;
  * end-to-end tests that build a throwaway Claude profile (projects/ dirs,
    session jsonl, .claude.json, history.jsonl) plus a project folder in a
    tmpdir, run claude-mv as a subprocess against it, and assert on the
    resulting on-disk state;
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

import glob
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

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
