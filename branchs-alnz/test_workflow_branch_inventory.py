import tempfile
import unittest
from pathlib import Path

from workflow_branch_inventory import State, extract_filters, export_csv


class ExtractFiltersTests(unittest.TestCase):
    def test_explicit_filters_and_globs(self):
        text = """
name: CI
on:
  push:
    branches: [main, 'release/**']
  pull_request:
    branches-ignore:
      - 'draft/**'
"""
        name, rows, status, detail = extract_filters(text)
        self.assertEqual(name, "CI")
        self.assertEqual(status, "ok")
        self.assertIsNone(detail)
        self.assertEqual(rows, [
            ("push", "branches", "main"),
            ("push", "branches", "release/**"),
            ("pull_request", "branches-ignore", "draft/**"),
        ])

    def test_on_is_not_parsed_as_boolean(self):
        _, rows, status, _ = extract_filters("on: [push, pull_request]\n")
        self.assertEqual(status, "ok")
        self.assertEqual(rows, [
            ("push", "implicit_all", "*"),
            ("pull_request", "implicit_all", "*"),
        ])

    def test_invalid_yaml_is_reported(self):
        _, rows, status, detail = extract_filters("on: [push\n")
        self.assertEqual(rows, [])
        self.assertEqual(status, "yaml_error")
        self.assertTrue(detail)


class StateTests(unittest.TestCase):
    def test_completed_repository_is_not_pending_and_exports(self):
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
                (1, "acme/api", "main", ".github/workflows/ci.yml", "CI",
                 "push", "branches", "main", "ok", None)
            ])
            self.assertEqual(state.pending(False, True), [])
            output = root / "out.csv"
            export_csv(state, output)
            self.assertIn("acme/api", output.read_text(encoding="utf-8-sig"))
            state.close()


if __name__ == "__main__":
    unittest.main()
