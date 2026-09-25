import tempfile
import unittest
from pathlib import Path
from unittest import mock

import council


class DiffSafetyTests(unittest.TestCase):
    def test_diff_targets_only_selected_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "example.py"
            target.write_text("one\ntwo\n", encoding="utf-8")
            with mock.patch.object(council.os, "getcwd", return_value=str(root)):
                self.assertTrue(
                    council._diff_targets_file(
                        "--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-one\n+ONE\n",
                        str(target),
                    )
                )
                self.assertFalse(
                    council._diff_targets_file(
                        "--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-one\n+ONE\n",
                        str(target),
                    )
                )

    def test_diff_deletion_header_resolves_to_selected_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "example.py"
            target.write_text("one\n", encoding="utf-8")
            with mock.patch.object(council.os, "getcwd", return_value=str(root)):
                self.assertTrue(
                    council._diff_targets_file(
                        "--- a/example.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-one\n",
                        str(target),
                    )
                )

    def test_python_fallback_applies_matching_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "example.py"
            target.write_text("one\ntwo\nthree\n", encoding="utf-8")
            diff = (
                "--- a/example.py\n+++ b/example.py\n"
                "@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"
            )
            self.assertTrue(council._python_apply_diff(diff, str(target)))
            self.assertEqual(target.read_text(encoding="utf-8"), "one\nTWO\nthree\n")

    def test_python_fallback_rejects_mismatched_context(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "example.py"
            target.write_text("one\ntwo\n", encoding="utf-8")
            diff = (
                "--- a/example.py\n+++ b/example.py\n"
                "@@ -1,2 +1,2 @@\n-one\n+ONE\n WRONG\n"
            )
            self.assertFalse(council._python_apply_diff(diff, str(target)))
            self.assertEqual(target.read_text(encoding="utf-8"), "one\ntwo\n")


if __name__ == "__main__":
    unittest.main()
