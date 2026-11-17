"""Tests for stage_stamp.py (contract: ~/.claude/docs/workflow-stage-runs.md).

Run: python3 -m unittest discover -s tool/workflow-stats -p 'test_*.py'
"""

import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE / "stage_stamp.py"
DRIVER = pathlib.Path.home() / ".claude" / "workflow-driver"
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
FENCE = re.compile(r"(?ms)^(```+)json[ \t]*\n(.*?)^\1[ \t]*$")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo)] + list(args),
                          capture_output=True, text=True, check=True).stdout


def sequence_text(block, indent=2):
    return ("# Planning sequence\n\nProse.\n\n```json\n"
            + json.dumps(block, indent=indent) + "\n```\n\nTail prose.\n")


def read_block(path):
    text = pathlib.Path(path).read_text(encoding="utf-8")
    for match in FENCE.finditer(text):
        data = json.loads(match.group(2))
        if isinstance(data, dict) and "tasks" in data:
            return data
    raise AssertionError("no block in %s" % path)


class StampTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self.tmp.name).resolve()
        git(self.repo, "init", "-q", "-b", "master")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "T")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "init")
        git(self.repo, "switch", "-q", "-c", "feat/epic")
        self.common = pathlib.Path(
            self.repo, git(self.repo, "rev-parse", "--git-common-dir").strip()
        ).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def run_stamp(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT)] + list(args),
                              cwd=self.repo, capture_output=True, text=True)

    def write_seq(self, epic="ep-1", slug="ep", date="2026-11-09",
                  stage_runs=None, tasks=None, commit=True, indent=2):
        block = {}
        if stage_runs is not None:
            block["stage_runs"] = stage_runs
        block["epic"] = epic
        block["tasks"] = tasks if tasks is not None else [{"bead_id": "t-1"}]
        path = (self.repo / "docs" / "plans" / slug
                / ("%s-%s-planning-sequence.md" % (date, slug)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sequence_text(block, indent), encoding="utf-8")
        if commit:
            git(self.repo, "add", "--", str(path))
            git(self.repo, "commit", "-q", "-m", "seq", "--", str(path))
        return path

    def marker(self, name, stage):
        return self.common / "workflow-stage-start" / ("%s--%s.txt" % (name, stage))


class StartTests(StampTestCase):
    def test_start_writes_marker_with_utc_time(self):
        out = self.run_stamp("start", "--epic", "ep-1", "--stage", "writing-plans")
        self.assertEqual(out.returncode, 0, out.stderr)
        text = self.marker("ep-1", "writing-plans").read_text().strip()
        self.assertRegex(text, TS_RE)

    def test_start_with_key(self):
        out = self.run_stamp("start", "--key", "my-spec", "--stage",
                             "planning-sequence")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(self.marker("my-spec", "planning-sequence").is_file())

    def test_start_rejects_unknown_stage(self):
        out = self.run_stamp("start", "--epic", "ep-1", "--stage", "bogus")
        self.assertNotEqual(out.returncode, 0)

    def test_start_needs_epic_or_key(self):
        out = self.run_stamp("start", "--stage", "writing-plans")
        self.assertNotEqual(out.returncode, 0)


class EndTests(StampTestCase):
    def test_end_adds_stage_runs_as_first_key(self):
        path = self.write_seq()
        self.run_stamp("start", "--epic", "ep-1", "--stage", "planning-sequence")
        out = self.run_stamp("end", "--epic", "ep-1", "--stage",
                             "planning-sequence", "--done", "t-2,t-1",
                             "--left", "")
        self.assertEqual(out.returncode, 0, out.stderr)
        block = read_block(path)
        self.assertEqual(list(block)[0], "stage_runs")
        (entry,) = block["stage_runs"]
        self.assertEqual(list(entry), ["stage", "started_at", "ended_at",
                                       "complete", "done", "left", "note"])
        self.assertEqual(entry["stage"], "planning-sequence")
        self.assertRegex(entry["started_at"], TS_RE)
        self.assertRegex(entry["ended_at"], TS_RE)
        self.assertTrue(entry["complete"])
        self.assertEqual(entry["done"], ["t-1", "t-2"])
        self.assertEqual(entry["left"], [])
        self.assertEqual(entry["note"], "")
        self.assertFalse(self.marker("ep-1", "planning-sequence").exists())
        lines = out.stdout.strip().splitlines()
        self.assertEqual(lines[0], "file: docs/plans/ep/2026-11-09-ep-planning-sequence.md")
        self.assertEqual(lines[-1], "commit-ok: yes")

    def test_entry_is_one_line_and_prose_is_kept(self):
        path = self.write_seq()
        self.run_stamp("end", "--epic", "ep-1", "--stage", "writing-plans",
                       "--done", "t-1", "--left", "t-2")
        text = path.read_text()
        self.assertTrue(text.startswith("# Planning sequence\n\nProse.\n"))
        self.assertTrue(text.endswith("```\n\nTail prose.\n"))
        entry_lines = [ln for ln in text.splitlines() if '"stage":' in ln]
        self.assertEqual(len(entry_lines), 1)
        json.loads(entry_lines[0].strip().rstrip(","))

    def test_end_appends_and_keeps_existing_entries_text(self):
        old = {"stage": "planning-sequence", "started_at": None,
               "ended_at": "2026-11-01T00:00:00Z", "complete": True,
               "done": ["t-1"], "left": [], "note": "start not recorded"}
        path = self.write_seq(stage_runs=[old])
        before = path.read_text()
        old_text = before[before.index('    {\n      "stage"'):
                          before.index("    }") + 5]
        out = self.run_stamp("end", "--epic", "ep-1", "--stage", "writing-plans",
                             "--done", "t-1", "--left", "t-2 t-3")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(old_text, path.read_text())
        runs = read_block(path)["stage_runs"]
        self.assertEqual(runs[0], old)
        self.assertEqual(runs[1]["stage"], "writing-plans")
        self.assertFalse(runs[1]["complete"])
        self.assertEqual(runs[1]["left"], ["t-2", "t-3"])

    def test_end_appends_to_empty_list(self):
        path = self.write_seq(stage_runs=[])
        out = self.run_stamp("end", "--epic", "ep-1", "--stage", "writing-plans",
                             "--done", "t-1", "--left", "")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(read_block(path)["stage_runs"]), 1)

    def test_end_works_on_compact_block(self):
        path = self.write_seq(indent=None)
        out = self.run_stamp("end", "--epic", "ep-1", "--stage", "writing-plans",
                             "--done", "t-1", "--left", "")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(read_block(path)["stage_runs"]), 1)
        out = self.run_stamp("end", "--epic", "ep-1", "--stage", "writing-plans",
                             "--done", "t-1", "--left", "")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(read_block(path)["stage_runs"]), 2)

    def test_start_not_recorded(self):
        path = self.write_seq()
        self.run_stamp("end", "--epic", "ep-1", "--stage", "execute-plans",
                       "--done", "t-1", "--left", "")
        (entry,) = read_block(path)["stage_runs"]
        self.assertIsNone(entry["started_at"])
        self.assertEqual(entry["note"], "start not recorded")

    def test_key_marker_is_used_and_key_folder_wins(self):
        self.write_seq(slug="ep", date="2026-11-10")
        keyed = self.write_seq(slug="my-spec", date="2026-11-09")
        self.run_stamp("start", "--key", "my-spec", "--stage", "planning-sequence")
        out = self.run_stamp("end", "--epic", "ep-1", "--key", "my-spec",
                             "--stage", "planning-sequence", "--done", "t-1",
                             "--left", "")
        self.assertEqual(out.returncode, 0, out.stderr)
        (entry,) = read_block(keyed)["stage_runs"]
        self.assertIsNotNone(entry["started_at"])
        self.assertFalse(self.marker("my-spec", "planning-sequence").exists())

    def test_newest_file_naming_epic_is_chosen(self):
        old = self.write_seq(slug="ep", date="2026-11-01")
        new = self.write_seq(slug="ep2", date="2026-11-05")
        self.write_seq(epic="other", slug="ep3", date="2026-11-08")
        self.run_stamp("end", "--epic", "ep-1", "--stage", "writing-plans",
                       "--done", "t-1", "--left", "")
        self.assertNotIn("stage_runs", read_block(old))
        self.assertIn("stage_runs", read_block(new))

    def test_sequence_file_flag(self):
        old = self.write_seq(slug="ep", date="2026-11-01")
        self.write_seq(slug="ep2", date="2026-11-05")
        out = self.run_stamp("end", "--epic", "ep-1", "--stage", "writing-plans",
                             "--sequence-file", str(old.relative_to(self.repo)),
                             "--done", "t-1", "--left", "")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("stage_runs", read_block(old))

    def test_owner_pending(self):
        path = self.write_seq()
        out = self.run_stamp("end", "--epic", "ep-1", "--stage", "execute-spikes",
                             "--done", "", "--left", "s-2,s-1",
                             "--owner-pending", "s-1,s-2")
        self.assertEqual(out.returncode, 0, out.stderr)
        (entry,) = read_block(path)["stage_runs"]
        self.assertEqual(entry["owner_pending"], ["s-1", "s-2"])
        self.assertEqual(list(entry)[-1], "owner_pending")

    def test_owner_pending_must_be_in_left(self):
        path = self.write_seq()
        before = path.read_text()
        out = self.run_stamp("end", "--epic", "ep-1", "--stage", "execute-spikes",
                             "--done", "", "--left", "s-1",
                             "--owner-pending", "s-9")
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(path.read_text(), before)

    def test_done_and_left_must_be_disjoint(self):
        path = self.write_seq()
        before = path.read_text()
        out = self.run_stamp("end", "--epic", "ep-1", "--stage", "writing-plans",
                             "--done", "t-1", "--left", "t-1")
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(path.read_text(), before)

    def test_no_sequence_file(self):
        out = self.run_stamp("end", "--epic", "ep-1", "--stage", "writing-plans",
                             "--done", "t-1", "--left", "")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("no planning-sequence file", out.stderr)


class CommitOkTests(StampTestCase):
    def end(self):
        return self.run_stamp("end", "--epic", "ep-1", "--stage", "writing-plans",
                              "--done", "t-1", "--left", "")

    def test_untracked_file(self):
        self.write_seq(commit=False)
        out = self.end()
        self.assertEqual(out.stdout.strip().splitlines()[-1],
                         "commit-ok: yes (new file)")

    def test_dirty_file(self):
        path = self.write_seq()
        path.write_text(path.read_text() + "\nedit\n")
        out = self.end()
        self.assertEqual(out.stdout.strip().splitlines()[-1],
                         "commit-ok: no (file had uncommitted changes)")

    def test_default_branch(self):
        self.write_seq()
        git(self.repo, "switch", "-q", "master")
        git(self.repo, "merge", "-q", "feat/epic")
        out = self.end()
        self.assertEqual(out.stdout.strip().splitlines()[-1],
                         "commit-ok: no (on the default branch)")

    def test_default_branch_from_origin_head(self):
        self.write_seq()
        git(self.repo, "branch", "-q", "trunk")
        git(self.repo, "update-ref", "refs/remotes/origin/trunk", "HEAD")
        git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD",
            "refs/remotes/origin/trunk")
        git(self.repo, "switch", "-q", "trunk")
        out = self.end()
        self.assertEqual(out.stdout.strip().splitlines()[-1],
                         "commit-ok: no (on the default branch)")

    def test_detached_head(self):
        self.write_seq()
        git(self.repo, "switch", "-q", "--detach", "HEAD")
        out = self.end()
        self.assertEqual(out.stdout.strip().splitlines()[-1],
                         "commit-ok: no (detached HEAD)")


@unittest.skipUnless((DRIVER / "signals.py").is_file(), "driver not installed")
class DriverReadsStampsTests(StampTestCase):
    def setUp(self):
        super().setUp()
        if str(DRIVER) not in sys.path:
            sys.path.insert(0, str(DRIVER))
        import signals  # noqa: E402
        import policy  # noqa: E402
        self.signals, self.policy = signals, policy

    def test_driver_sees_entries_and_advances(self):
        self.write_seq(slug="my-spec")
        self.run_stamp("start", "--key", "my-spec", "--stage", "planning-sequence")
        self.run_stamp("end", "--epic", "ep-1", "--key", "my-spec", "--stage",
                       "planning-sequence", "--done", "t-1", "--left", "")
        pairs = self.signals.key_stage_runs(self.repo, "my-spec")
        self.assertEqual([(e, x["stage"]) for e, x in pairs],
                         [("ep-1", "planning-sequence")])
        runs = self.signals.stage_runs(self.repo, "ep-1")
        nxt = self.policy.next_stage(runs, [{"bead_id": "t-1"}])
        self.assertEqual(nxt[0], "writing-plans")

    def test_driver_continues_past_owner_pending(self):
        self.write_seq()
        self.run_stamp("end", "--epic", "ep-1", "--stage", "execute-spikes",
                       "--done", "", "--left", "s-1", "--owner-pending", "s-1")
        runs = self.signals.stage_runs(self.repo, "ep-1")
        nxt = self.policy.next_stage(runs, [])
        self.assertEqual(nxt[0], "writing-plans")

    def test_driver_start_marker_is_seen(self):
        self.run_stamp("start", "--epic", "ep-1", "--stage", "writing-plans")
        self.assertTrue(self.signals.stage_started(
            self.common, "ep-1", "writing-plans", self.signals.parse_ts(
                "2000-01-01T00:00:00Z")))


if __name__ == "__main__":
    unittest.main()
