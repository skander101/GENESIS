#!/usr/bin/env python3
"""System launcher for the GENESIS Council application."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

MIN_PYTHON = (3, 10)
INSTALLED_COUNCIL = Path("/usr/lib/genesis/council.py")
STATE_MARKER = ".genesis-owned"


def state_dir() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    return base / "genesis"


def find_council() -> Path | None:
    candidates = []
    configured = os.environ.get("GENESIS_COUNCIL")
    if configured:
        candidates.append(Path(configured).expanduser())
    script_dir = Path(__file__).resolve().parent
    candidates.extend(
        [
            script_dir / "council.py",
            script_dir.parent / "council.py",
            INSTALLED_COUNCIL,
        ]
    )
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    return None


def resolve_project_root(value: str | None) -> Path:
    root = Path(value).expanduser() if value else Path.cwd()
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"project root is not a directory: {root}")
    return root


def find_opencode() -> str | None:
    configured = os.environ.get("GENESIS_OPENCODE")
    if configured:
        candidate = Path(configured).expanduser().resolve()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None
    return shutil.which("opencode")


def check_dependencies(gui: bool) -> tuple[list[str], list[str], str | None]:
    errors: list[str] = []
    warnings: list[str] = []
    opencode_path = find_opencode()

    if sys.version_info[:2] < MIN_PYTHON:
        errors.append(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required; "
            f"found {sys.version.split()[0]}"
        )

    if opencode_path is None:
        if os.environ.get("GENESIS_OPENCODE"):
            errors.append(
                "GENESIS_OPENCODE does not point to an executable opencode command"
            )
        else:
            errors.append(
                "opencode was not found on PATH; install and authenticate it before running Genesis"
            )
    else:
        try:
            result = subprocess.run(
                [opencode_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"opencode could not be executed: {exc}")
        else:
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                errors.append(f"opencode --check failed: {detail or result.returncode}")

    if gui:
        if importlib.util.find_spec("tkinter") is None:
            errors.append("Tkinter is required for GUI mode; install the system Tk package")
        elif not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            warnings.append("no DISPLAY or WAYLAND_DISPLAY is set; GUI mode may fall back to the terminal")

    if shutil.which("git") is None:
        warnings.append("git was not found; diff application will use patch or fail safely")

    return errors, warnings, opencode_path


def print_dependency_report(opencode_path: str | None, errors: list[str], warnings: list[str]) -> None:
    print(f"Python: {sys.version.split()[0]}")
    print(f"opencode: {opencode_path or 'not found'}")
    print(f"tkinter: {'available' if importlib.util.find_spec('tkinter') else 'not found'}")
    print(f"git: {shutil.which('git') or 'not found'}")
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)


def mark_state_dir() -> Path:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / STATE_MARKER).write_text("genesis-owned\n", encoding="utf-8")
    return directory


def run_uninstall(assume_yes: bool) -> int:
    directory = state_dir()
    if not directory.exists():
        print("No Genesis user state found.")
        return 0
    if not (directory / STATE_MARKER).is_file():
        print(
            f"Refusing to remove {directory}: it is not marked as Genesis-owned.",
            file=sys.stderr,
        )
        return 1
    if not assume_yes:
        try:
            answer = input(f"Remove Genesis user state at {directory}? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"y", "yes"}:
            print("Uninstall cancelled.")
            return 0
    shutil.rmtree(directory)
    print(f"Removed {directory}")
    print("To remove the Debian package, run: sudo apt remove genesis-council")
    return 0


def terminate_process_group(process: subprocess.Popen[bytes], signum: int) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signum)
    except (OSError, ProcessLookupError):
        try:
            process.send_signal(signum)
        except (OSError, ProcessLookupError):
            pass


def run_council(
    council: Path,
    project_root: Path,
    council_args: list[str],
    opencode_path: str | None = None,
) -> int:
    command = [sys.executable, "-u", str(council)]
    if not council_args:
        command.append("--gui")
    else:
        command.extend(council_args)

    environment = os.environ.copy()
    environment["GENESIS_PROJECT_ROOT"] = str(project_root)
    environment.setdefault("PYTHONUNBUFFERED", "1")
    if opencode_path:
        environment["GENESIS_OPENCODE"] = opencode_path
        opencode_dir = str(Path(opencode_path).parent)
        path_entries = environment.get("PATH", "").split(os.pathsep)
        if opencode_dir not in path_entries:
            environment["PATH"] = os.pathsep.join([opencode_dir, environment.get("PATH", "")])

    process = subprocess.Popen(
        command,
        cwd=str(project_root),
        env=environment,
        start_new_session=True,
    )

    def forward_signal(signum: int, _frame: object) -> None:
        terminate_process_group(process, signum)
        raise KeyboardInterrupt

    previous_handlers = {
        signum: signal.signal(signum, forward_signal)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        return process.wait()
    except KeyboardInterrupt:
        terminate_process_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            terminate_process_group(process, signal.SIGKILL)
        return 130
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="genesis",
        description="Run the GENESIS Council orchestrator against a project directory",
    )
    parser.add_argument(
        "--project",
        metavar="PATH",
        help="project root (defaults to the current directory)",
    )
    parser.add_argument(
        "--no-dependency-check",
        action="store_true",
        help="skip the launcher dependency preflight",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="genesis 0.1.0",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["check"]:
        errors, warnings, opencode_path = check_dependencies(gui=True)
        print_dependency_report(opencode_path, errors, warnings)
        return 1 if errors else 0
    if arguments[:1] == ["uninstall"]:
        parser = argparse.ArgumentParser(prog="genesis uninstall")
        parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
        options = parser.parse_args(arguments[1:])
        return run_uninstall(options.yes)

    parser = build_parser()
    options, council_args = parser.parse_known_args(arguments)
    gui_requested = not council_args or "--gui" in council_args
    opencode_path = find_opencode()

    if not options.no_dependency_check:
        errors, warnings, opencode_path = check_dependencies(gui=gui_requested)
        print_dependency_report(opencode_path, errors, warnings)
        if errors:
            return 2

    try:
        project_root = resolve_project_root(options.project)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    council = find_council()
    if council is None:
        print(
            "error: council.py was not found; set GENESIS_COUNCIL or install the "
            "genesis-council package",
            file=sys.stderr,
        )
        return 2

    try:
        mark_state_dir()
        return run_council(council, project_root, council_args, opencode_path)
    except OSError as exc:
        print(f"error: could not start Council: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
