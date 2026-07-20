import tempfile
import unittest
import sqlite3
from pathlib import Path

from workflow_branch_inventory import State, export_csv, extract_usages


class ExtractUsagesTests(unittest.TestCase):
    def test_job_and_step_uses(self):
        text = """
name: CI
jobs:
  reusable:
    uses: b3sa/reusable/workflow-ci@branch01
  build:
    steps:
      - uses: actions/checkout@v4
      - uses: ./local-action
"""
        rows, error = extract_usages(text)
        self.assertIsNone(error)
        self.assertEqual(rows, [
            ("jobs.reusable.uses", "b3sa/reusable/workflow-ci", "branch01"),
            ("jobs.build.steps[0].uses", "actions/checkout", "v4"),
        ])

    def test_ref_can_be_expression(self):
        text = "jobs:\n  call:\n    uses: org/repo/.github/workflows/ci.yml@${{ inputs.ref }}\n"
        rows, error = extract_usages(text)
        self.assertIsNone(error)
        self.assertEqual(rows, [
            ("jobs.call.uses", "org/repo/.github/workflows/ci.yml", "${{ inputs.ref }}"),
        ])

    def test_invalid_yaml_is_reported(self):
        rows, detail = extract_usages("jobs: [build\n")
        self.assertEqual(rows, [])
        self.assertTrue(detail)


class StateTests(unittest.TestCase):
    def test_old_checkpoint_marks_done_repositories_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE repositories (
                    id INTEGER PRIMARY KEY, full_name TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL, owner TEXT NOT NULL, default_branch TEXT,
                    archived INTEGER NOT NULL, disabled INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT, updated_at TEXT
                );
                INSERT INTO repositories
                    (id,full_name,name,owner,default_branch,archived,disabled,status)
                    VALUES (1,'acme/api','api','acme','main',0,0,'done');
            """)
            conn.close()
            state = State(path)
            self.assertEqual([row["full_name"] for row in state.pending(False, True)], ["acme/api"])
            state.close()

    def test_completed_repository_is_not_pending_and_exports_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = State(root / "state.sqlite3")
            state.add_repositories([{
                "id": 1, "full_name": "acme/api", "name": "api",
                "owner": {"login": "acme"}, "default_branch": "main",
                "archived": False, "disabled": False,
            }])
            repo = state.pending(False, True)[0]
            state.begin_repo(1)
            state.finish_repo(repo, [
                (1, "acme/api", "main", ".github/workflows/ci.yml",
                 "jobs.ci.uses", "b3sa/reusable/workflow-ci", "branch01")
            ])
            self.assertEqual(state.pending(False, True), [])
            output = root / "out.csv"
            export_csv(state, output)
            csv_text = output.read_text(encoding="utf-8-sig")
            self.assertIn("acme/api", csv_text)
            self.assertIn("branch01", csv_text)
            self.assertNotIn("filter_type", csv_text)
            state.close()


if __name__ == "__main__":
    unittest.main()
