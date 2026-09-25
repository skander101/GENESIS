# Council

A multi-LLM orchestrator for collaborative coding assistance. Runs free LLM models through a multi-phase pipeline to generate, debate, and judge code proposals.

## How It Works

1. **Triage** — Classifies your request as simple/moderate/complex
2. **Proposals** — Multiple models generate solutions in parallel
3. **Agreement** — Checks whether proposals agree
4. **Debate** — Disagreeing models see each other's proposals and revise
5. **Judge** — A preferred model resolves remaining disagreements
6. **Apply** — Optionally applies the unified diff to a file (requires confirmation)

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

- Python 3
- `opencode` CLI tool (used to discover available free LLM models)
- No external Python dependencies — standard library only
