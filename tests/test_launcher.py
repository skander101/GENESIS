import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("genesis_launcher", ROOT / "genesis_launcher.py")
assert SPEC and SPEC.loader
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_find_council_in_source_tree(self):
        found = launcher.find_council()
        self.assertEqual(found, (ROOT / "council.py").resolve())

    def test_resolve_project_root_defaults_to_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(launcher.Path, "cwd", return_value=Path(directory)):
                self.assertEqual(launcher.resolve_project_root(None), Path(directory).resolve())

    def test_resolve_project_root_rejects_files(self):
        with tempfile.NamedTemporaryFile() as handle:
            with self.assertRaises(ValueError):
                launcher.resolve_project_root(handle.name)

    def test_uninstall_refuses_unmarked_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            with mock.patch.object(launcher, "state_dir", return_value=state):
                self.assertEqual(launcher.run_uninstall(assume_yes=True), 1)

    def test_check_reports_missing_opencode(self):
        with mock.patch.object(
            launcher.shutil,
            "which",
            side_effect=lambda name: None if name == "opencode" else "/usr/bin/git",
        ):
            errors, warnings, path = launcher.check_dependencies(gui=False)
        self.assertIsNone(path)
        self.assertTrue(any("opencode" in error for error in errors))
        self.assertEqual(warnings, [])

    def test_default_invocation_uses_gui(self):
        options, council_args = launcher.build_parser().parse_known_args([])
        self.assertIsNone(options.project)
        self.assertEqual(council_args, [])


if __name__ == "__main__":
    unittest.main()
