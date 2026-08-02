#!/usr/bin/env python3
"""Test suite for claude-mv. Stdlib only — run it with:

    python3 claude/claude-mv/tests/test_claude_mv.py          # or -v
    python3 -m unittest discover claude/claude-mv/tests

Two layers:

  * unit tests on the pure helpers (path encoding, config merging), loaded
    straight out of claude-mv.py;
  * end-to-end tests that build a throwaway Claude profile (projects/ dirs,
    session jsonl, .claude.json, history.jsonl) plus a project folder in a
    tmpdir, run claude-mv as a subprocess against it, and assert on the
    resulting on-disk state.

Everything runs against tmpdirs with CLAUDE_MV_RESTORE_ROOT redirected, so
no test can reach the real ~/.claude or ~/.claude-mv.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
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

    def test_config_json_path(self):
        home_claude = os.path.join(os.path.expanduser("~"), ".claude")
        self.assertEqual(cm.config_json_path(home_claude),
                         os.path.expanduser("~/.claude.json"))
        self.assertEqual(cm.config_json_path("/tmp/prof"), "/tmp/prof/.claude.json")


# ── end-to-end scaffolding ──────────────────────────────────────────────────

class FixtureCase(unittest.TestCase):
    """Builds a disposable Claude profile + project tree per test."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="claude-mv-test-")
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

    def run_mv(self, *args, expect=0, stdin=""):
        env = dict(os.environ, CLAUDE_MV_RESTORE_ROOT=self.restore_root)
        env.pop("CLAUDE_MV_FORCE_PROMPT", None)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
