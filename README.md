# Council

A multi-LLM orchestrator for collaborative coding assistance. Runs free LLM models through a multi-phase pipeline to generate, debate, and judge code proposals.

## How It Works

1. **Triage** — Classifies your request as simple/moderate/complex
2. **Proposals** — Multiple models generate solutions in parallel
3. **Agreement** — Checks whether proposals agree
4. **Debate** — Disagreeing models see each other's proposals and revise
5. **Judge** — A preferred model resolves remaining disagreements
6. **Apply** — Optionally applies the unified diff to a file (requires confirmation)

## Linux launcher

Build a Debian/Ubuntu package from the repository root:

```bash
./scripts/build-deb.sh
sudo apt install ./dist/genesis-council_0.1.0_amd64.deb
```

The package installs a system command named `genesis` and keeps the Council
implementation in `/usr/lib/genesis/council.py`. Run it from the project you
want it to work on:

```bash
cd /path/to/project
genesis
```

With no arguments, `genesis` opens the GUI. Arguments are passed to Council,
so the existing CLI remains available:

```bash
genesis "explain this module"
genesis --file src/module.py "refactor this function"
genesis --project /path/to/project "find the bug"
```

The project root is the current directory unless `--project` is supplied.
Council does not recursively scan a project; it only reads files explicitly
passed with `--file`.

Check the local runtime without starting Council:

```bash
genesis check
```

The launcher requires Python 3.10+ and an authenticated `opencode` command on
`PATH`. It reports missing requirements instead of silently installing system
software or provider credentials. Set `GENESIS_OPENCODE=/absolute/path/to/opencode`
when the command is installed outside the desktop environment's `PATH`. Git is
optional but recommended for applying unified diffs.

Remove Genesis user state explicitly; this never removes project files,
Python, OpenCode, or provider credentials:

```bash
genesis uninstall
sudo apt remove genesis-council
```

## Usage

### CLI

```bash
python3 council.py "your question"
python3 council.py "your question" --file path/to/file.py
```

### Interactive Mode

```bash
python3 council.py
```

Opens a terminal menu for model selection, configuration, and Q&A.

### GUI Mode

```bash
python3 council.py --gui
```

Launches a Tkinter desktop window with a dark theme (Catppuccin Mocha).

## Performance

The orchestrator starts a single persistent `opencode serve` instance and
calls its HTTP API directly, so model calls don't pay a process-boot cost.
Pure greetings ("hi") are answered instantly, and short file-less requests
skip the triage/agreement/debate/judge pipeline entirely.

Environment knobs:

- `COUNCIL_NO_SERVER=1` — disable the persistent server, use standalone calls
- `COUNCIL_ATTACH_URL=http://host:port` — reuse an already-running server
- `COUNCIL_UTILITY_VARIANT=minimal` — lower reasoning effort for triage/agreement

## Requirements

- Python 3.10+
- `opencode` CLI tool (used to discover available free LLM models)
- Tkinter for GUI mode
- No external Python dependencies — standard library only
