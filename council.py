#!/usr/bin/env python3
"""
council — A multi-LLM orchestrator that runs free models through a
triage → proposal → agreement → debate → judge pipeline to answer
coding questions or propose code changes collaboratively.

Only the final "apply" step (Phase 5) ever writes to disk.

Usage:
    python3 council.py "your request here" [--file path/to/file.py]
    python3 council.py --gui
"""

import argparse
import asyncio
import atexit
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from difflib import SequenceMatcher
from typing import Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Model state constants
MODEL_STATE_REACHED = "reached"
MODEL_STATE_TOUCHING = "touching"
MODEL_STATE_FAILED = "failed"

# Model state → color mapping (Catppuccin Mocha green/yellow/red)
MODEL_STATE_COLORS: dict[str, str] = {
    MODEL_STATE_REACHED: "#a6e3a1",    # green
    MODEL_STATE_TOUCHING: "#f9e2af",   # yellow
    MODEL_STATE_FAILED: "#f38ba8",     # red
}


def get_model_state_color(state: str) -> str:
    """Return the color hex for a given model state, or dim gray if unknown."""
    return MODEL_STATE_COLORS.get(state, "#6c7086")

# Fallback used if ``opencode models`` can't be queried
DEFAULT_FREE_MODELS = [
    "opencode/ling-3.0-flash-fin-free",
    "opencode/mimo-v2.5-free",
    "opencode/muse-spark-1.2-contributor-free",
    "opencode/muse-spark-1.3-contributor-free",
    "opencode/nemotron-3-ultra-free",
    "opencode/nemotron-3.5-lightning-free",
]

# Preferred paid judge; auto-detection falls back to the newest available
# Gemini model if this one isn't present.
PREFERRED_JUDGE = "google/gemini-3.5-flash"

MAX_RETRIES = 2  # retries after the initial attempt (cap total attempts at MAX_RETRIES + 1)
BASE_BACKOFF = 2.0  # seconds; exponential

# ---------------------------------------------------------------------------
# Speed / latency tuning
# ---------------------------------------------------------------------------
# Reuse a single long-lived `opencode serve` instance and call its HTTP API,
# instead of booting a fresh CLI/server process on every model call. Set
# COUNCIL_NO_SERVER=1 to fall back to standalone `opencode run` calls.
_SERVER_ENABLED = os.environ.get("COUNCIL_NO_SERVER", "").strip().lower() not in (
    "1", "true", "yes", "on",
)
# Use an already-running server instead of starting our own.
_CONFIGURED_ATTACH_URL = os.environ.get("COUNCIL_ATTACH_URL", "").strip() or None
# Skip external plugins on standalone calls (consistency with the pure server).
_PURE_CALLS = os.environ.get("COUNCIL_NO_PURE", "").strip().lower() not in (
    "1", "true", "yes", "on",
)
# Reasoning effort for cheap utility phases (triage/agreement). Off by default:
# some providers hang on `variant=minimal`. Set COUNCIL_UTILITY_VARIANT to opt in.
_UTILITY_VARIANT = os.environ.get("COUNCIL_UTILITY_VARIANT", "").strip() or None

# Requests this short, with no code keywords and no file, skip the council
# pipeline entirely (triage -> proposals -> debate -> judge is pure overhead).
_TRIVIAL_MAX_LEN = 60

# Names that suggest a fast/small model, used for the cheap utility phases.
_FAST_MODEL_HINTS = ("flash", "mini", "lightning", "nano", "tiny", "fast", "haiku", "mimo")

# When set (GUI mode), Phase 5 asks for confirmation via this callable
# instead of terminal ``input()``. Signature: callable(prompt) -> bool.
ASK_YES_NO = None

# When set (GUI mode), Phase 5 delivers the final answer text directly via
# this callable instead of relying on stdout scraping. Signature: callable(text).
ANSWER_CALLBACK = None

# ---------------------------------------------------------------------------
# Model state tracking
# ---------------------------------------------------------------------------

_model_states: dict[str, str] = {}


def set_model_state(model: str, state: str) -> None:
    """Set the state of a model and notify the GUI if running."""
    _model_states[model] = state
    if state == MODEL_STATE_REACHED:
        log("STATE", f"✓ {model} reached")
    elif state == MODEL_STATE_TOUCHING:
        log("STATE", f"⟳ {model} contacting...")
    elif state == MODEL_STATE_FAILED:
        log("STATE", f"✗ {model} failed")


def get_model_state(model: str) -> str:
    """Get the current state of a model."""
    return _model_states.get(model, "")


def get_all_model_states() -> dict[str, str]:
    """Get all model states."""
    return dict(_model_states)


def reset_model_states() -> None:
    """Reset all model states."""
    _model_states.clear()




class RateLimited(Exception):
    """Raised when a model/provider returns a rate-limit error."""


# ---------------------------------------------------------------------------
# Trivial-request fast path
# ---------------------------------------------------------------------------
# For short, file-less, code-free messages ("hi", "thanks", "who are you")
# a full council run is pure overhead. Pure greetings get an instant reply;
# other trivial requests get a single model call with no triage/agreement/
# debate/judge stages.

_GREETING_RE = re.compile(
    r"^\s*(hi|hey|hello|yo|sup|howdy|hola|how'?s it going|"
    r"good\s+(morning|afternoon|evening)|greetings|"
    r"thanks|thank\s+you|ty|thx|cheers|ok|okay|cool|nice|great|"
    r"bye|goodbye|see\s+ya|cya|"
    r"who\s+are\s+you|what\s+are\s+you|whats?\s+up|how\s+are\s+you)"
    r"[\s!.?,…]*$",
    re.IGNORECASE,
)

_CODE_HINT_RE = re.compile(
    r"\b(code|function|class|method|bug|error|exception|fix|refactor|"
    r"implement|write|create|build|debug|test|api|file|directory|diff|"
    r"python|javascript|typescript|java|rust|golang|sql|regex|script|"
    r"compile|import|return|html|css|json|yaml|toml|xml|git|cli|"
    r"algorithm|recursion|sort|search|parse|deploy|install|database|"
    r"server|request|response|http|endpoint)\b",
    re.IGNORECASE,
)


def _instant_reply(request: str) -> Optional[str]:
    """Return a canned reply for pure greetings/smalltalk, else None."""
    r = request.strip()
    if not _GREETING_RE.match(r) or _CODE_HINT_RE.search(r):
        return None
    low = r.lower()
    if low.startswith(("thank", "ty", "thx", "cheers")):
        return "You're welcome!"
    if low.startswith(("bye", "goodbye", "see ya", "cya")):
        return "See you!"
    if "who are you" in low or "what are you" in low:
        return "I'm Council — a multi-LLM orchestrator for coding help."
    if "how are you" in low or "how's it going" in low:
        return "Running well. What would you like to build?"
    return "Hi! What would you like to build or debug?"


def _is_trivial_request(request: str, file_path: Optional[str]) -> bool:
    """True for short, file-less, code-free requests that skip the council."""
    if file_path:
        return False
    r = request.strip()
    if not r or len(r) > _TRIVIAL_MAX_LEN:
        return False
    return _CODE_HINT_RE.search(r) is None


# ---------------------------------------------------------------------------
# Model discovery (from `opencode models`)
# ---------------------------------------------------------------------------




_FREE_MODELS_CACHE: dict = {"t": 0.0, "v": None}
_JUDGE_CACHE: dict = {"t": 0.0, "v": None}
_CACHE_TTL = 300.0  # seconds — re-detection happens occasionally on long runs


def first_working_model() -> str:
    """
    First free model that hasn't failed this run.

    Failed (rate-limited / connection-error) models are skipped so we only
    ever talk to models that are actually reachable.
    """
    return next(
        (m for m in get_free_models() if not is_model_failed(m)),
        get_free_models()[0],
    )


def is_model_failed(model: str) -> bool:
    """True if the model failed (rate-limited / connection error) this run."""
    return _model_states.get(model) == MODEL_STATE_FAILED


def get_fast_model() -> str:
    """
    Pick a cheap, fast model for utility phases (triage/agreement/simple
    answers). Prefers names hinting at small/fast variants, else falls back
    to the first working model.
    """
    for m in get_free_models():
        if is_model_failed(m):
            continue
        if any(hint in m.lower() for hint in _FAST_MODEL_HINTS):
            return m
    return first_working_model()


def _query_opencode_models() -> list[str]:
    """
    Auto-detect the free models available right now.

    Free models are discovered by filtering ``opencode models`` for entries
    whose name contains "free", then checked live for reachability. Falls
    back to ``DEFAULT_FREE_MODELS`` if detection fails. Results are cached
    for ``_CACHE_TTL`` seconds.
    """
    now = time.time()
    if _FREE_MODELS_CACHE["v"] is not None and now - _FREE_MODELS_CACHE["t"] < _CACHE_TTL:
        return list(_FREE_MODELS_CACHE["v"])

    try:
        proc = __import__("subprocess").run(
            ["opencode", "models"], capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"exit {proc.returncode}")
        all_models = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    except Exception as e:  # noqa: BLE001
        log("MODELS", f"model discovery failed: {e}")
        all_models = []

    detected = [m for m in all_models if "free" in m.lower()]
    if detected:
        _FREE_MODELS_CACHE.update(t=now, v=detected)
        log("MODELS", f"detected {len(detected)} free model(s)")
        return detected

    log("MODELS", "no free models detected — using default fallback list")
    _FREE_MODELS_CACHE.update(t=now, v=DEFAULT_FREE_MODELS)
    return list(DEFAULT_FREE_MODELS)


# Aliases kept for backward compatibility. ``_query_opencode_models`` is the
# canonical implementation; other modules (interactive mode, GUI, phases)
# still call ``get_free_models()`` and ``get_judge_models()``.
get_free_models = _query_opencode_models
get_judge_models = lambda: [get_judge_model()] if get_judge_model() else []



def get_judge_model() -> str:
    """
    Resolve the judge model: the preferred Gemini if available,
    otherwise the latest usable Gemini in the detected list.
    """
    now = time.time()
    if _JUDGE_CACHE["v"] is not None and now - _JUDGE_CACHE["t"] < _CACHE_TTL:
        return _JUDGE_CACHE["v"]

    banned = ("image", "tts", "live", "embedding", "computer", "research")
    try:
        all_models = _query_opencode_models()
    except Exception:  # noqa: BLE001
        all_models = []

    if PREFERRED_JUDGE in all_models:
        chosen = PREFERRED_JUDGE
    else:
        candidates = sorted(
            (
                m for m in all_models
                if m.startswith("google/gemini-")
                and not any(b in m for b in banned)
            ),
            reverse=True,
        )
        if candidates:
            log("MODELS", f"preferred judge unavailable — using {candidates[0]}")
            chosen = candidates[0]
        else:
            chosen = first_working_model()
            log("MODELS", f"no Gemini judge detected — using free model {chosen}")

    _JUDGE_CACHE.update(t=now, v=chosen)
    return chosen

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(phase: str, msg: str) -> None:
    """Print a timestamped log line for a given phase."""
    tag = f"[{phase}]"
    print(f"  {tag:12s} {msg}", flush=True)


# ---------------------------------------------------------------------------
# Persistent opencode server (spawned once, reused via its HTTP API)
# ---------------------------------------------------------------------------
# Booting a fresh opencode process on every model call is the dominant
# latency cost for trivial work. One long-lived server removes the per-call
# boot; model calls become plain HTTP requests (no client process at all).

_SERVER = {"proc": None, "url": None, "failed": False}
_server_lock = threading.Lock()


def _find_free_port() -> int:
    """Ask the OS for an unused localhost TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _http_healthy(port: int, timeout: float = 1.0) -> bool:
    """True once the server's HTTP API is actually answering (not just TCP-open)."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/global/health", timeout=timeout
        ) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001
        return False


def _start_server_sync() -> Optional[str]:
    """Start (or reuse) a persistent ``opencode serve``; return its URL or None."""
    if _CONFIGURED_ATTACH_URL:
        return _CONFIGURED_ATTACH_URL
    if _SERVER["url"]:
        return _SERVER["url"]
    if _SERVER["failed"] or not _SERVER_ENABLED:
        return None

    with _server_lock:
        if _SERVER["url"]:
            return _SERVER["url"]
        if _SERVER["failed"]:
            return None

        port = _find_free_port()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.Popen(
                ["opencode", "serve", "--port", str(port),
                 "--hostname", "127.0.0.1", "--pure"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except Exception as e:  # noqa: BLE001
            log("SERVER", f"could not start server ({e}) — using standalone calls")
            _SERVER["failed"] = True
            return None

        deadline = time.time() + 20.0
        while time.time() < deadline:
            if proc.poll() is not None:
                log("SERVER", "server exited during startup — using standalone calls")
                _SERVER["failed"] = True
                return None
            if _http_healthy(port):
                url = f"http://127.0.0.1:{port}"
                _SERVER.update(proc=proc, url=url)
                atexit.register(_stop_server)
                log("SERVER", f"persistent server ready at {url}")
                return url
            time.sleep(0.2)

        log("SERVER", "server never became ready — using standalone calls")
        try:
            proc.terminate()
        except Exception:
            pass
        _SERVER["failed"] = True
        return None


async def _ensure_server() -> Optional[str]:
    """Return the persistent server URL, starting it on first use if needed."""
    if _SERVER["failed"] or not _SERVER_ENABLED:
        return None
    if _SERVER["url"]:
        return _SERVER["url"]
    if _CONFIGURED_ATTACH_URL:
        _SERVER["url"] = _CONFIGURED_ATTACH_URL
        return _CONFIGURED_ATTACH_URL
    return await asyncio.to_thread(_start_server_sync)


def _disable_server() -> None:
    """Mark the persistent server unusable so calls fall back to standalone."""
    _SERVER["failed"] = True
    _SERVER["url"] = None
    proc = _SERVER.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass
    _SERVER["proc"] = None


def _stop_server() -> None:
    proc = _SERVER.get("proc")
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass


class _ServerUnavailable(Exception):
    """Raised when the persistent server can't serve a request."""


def _split_model(model: str) -> tuple[str, str]:
    """Split ``provider/model`` into (providerID, modelID)."""
    if "/" in model:
        provider, mid = model.split("/", 1)
        return provider, mid
    return "opencode", model


def _api_request(
    base_url: str,
    method: str,
    path: str,
    body: Optional[dict] = None,
    timeout: float = 60,
) -> object:
    """One JSON HTTP request against the opencode server."""
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        if e.code == 429:
            raise RateLimited("rate limited")
        if e.code >= 500:
            raise _ServerUnavailable(f"HTTP {e.code}: {raw[:200]}")
        raise RuntimeError(f"HTTP {e.code}: {raw[:200]}")
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        raise _ServerUnavailable(str(e))


def _call_api_sync(
    base_url: str,
    prompt: str,
    model: str,
    timeout: float,
    variant: Optional[str],
) -> str:
    """Create a session, send one prompt, and return the assistant's text."""
    provider, mid = _split_model(model)
    model_ref: dict = {"providerID": provider, "id": mid}
    if variant:
        model_ref["variant"] = variant

    created = _api_request(
        base_url, "POST", "/api/session", {"model": model_ref}, timeout=30
    )
    sid = ((created or {}).get("data") or {}).get("id")
    if not sid:
        raise _ServerUnavailable("session create returned no id")

    try:
        _api_request(
            base_url,
            "POST",
            f"/api/session/{sid}/prompt",
            {"prompt": {"text": prompt}},
            timeout=30,
        )
        deadline = time.time() + timeout
        delay = 0.05
        last_text = ""
        stable = 0
        while time.time() < deadline:
            messages = _api_request(
                base_url, "GET", f"/api/session/{sid}/message", timeout=30
            )
            for msg in ((messages or {}).get("data") or []):
                if msg.get("type") != "assistant":
                    continue
                err = msg.get("error")
                if err:
                    err_text = json.dumps(err)
                    if "rate" in err_text.lower() or "429" in err_text:
                        raise RateLimited("rate limited")
                    raise RuntimeError(f"model error: {err_text[:200]}")
                done = bool(msg.get("finish")) or (
                    (msg.get("time") or {}).get("completed") is not None
                )
                parts = [
                    c.get("text", "")
                    for c in (msg.get("content") or [])
                    if c.get("type") == "text" and c.get("text")
                ]
                if parts:
                    joined = "".join(parts)
                    if done:
                        return joined
                    if joined == last_text:
                        stable += 1
                        if stable >= 3:
                            return joined
                    else:
                        last_text = joined
                        stable = 0
                elif done:
                    return ""  # finished with no text
            time.sleep(delay)
            delay = min(delay * 1.5, 0.25)
        raise RuntimeError(f"opencode API timed out after {timeout}s")
    finally:
        try:
            _api_request(base_url, "DELETE", f"/api/session/{sid}", timeout=10)
        except Exception:
            pass


async def _run_via_api(
    prompt: str,
    model: str,
    timeout: float,
    variant: Optional[str],
) -> tuple[str, str]:
    """Run a model call through the persistent server's HTTP API."""
    url = await _ensure_server()
    if not url:
        raise _ServerUnavailable("no server")
    answer = await asyncio.to_thread(
        _call_api_sync, url, prompt, model, timeout, variant
    )
    if not answer:
        raise RuntimeError("empty response from server API")
    # Wrap as a synthetic event so _extract_json_text is reused unchanged.
    event = json.dumps({"type": "text", "part": {"type": "text", "text": answer}})
    return event, ""


async def _run_via_subprocess(
    prompt: str,
    model: str,
    timeout: float,
    variant: Optional[str],
) -> tuple[str, str]:
    """Standalone ``opencode run`` fallback used when no server is available."""
    cmd = ["opencode", "run", prompt, "--model", model, "--format", "json"]
    if variant:
        cmd += ["--variant", variant]
    if _PURE_CALLS:
        cmd += ["--pure"]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"opencode run timed out after {timeout}s")

    stdout_str = stdout.decode() if stdout else ""
    stderr_str = stderr.decode() if stderr else ""
    low_err = stderr_str.lower()
    rc = proc.returncode if proc.returncode is not None else -1

    # Detect rate-limiting (HTTP 429, or provider wording inside the stream)
    if (
        rc == 429
        or "429" in stderr_str
        or "rate limit" in low_err
        or "too many requests" in low_err
        or "rate_limited" in stdout_str.lower()
    ):
        raise RateLimited("rate limited")

    # Some providers report the error inline as a JSON event but exit 0
    if '"type":"error"' in stdout_str:
        raise RuntimeError(
            "opencode run returned an error event (model/provider error)"
        )

    if rc != 0:
        first_line = (stderr_str.strip().splitlines() or [""])[0]
        raise RuntimeError(f"opencode run failed (exit {rc}): {first_line[:120]}")

    return stdout_str, stderr_str


async def _run_opencode(
    prompt: str,
    model: str,
    timeout: float = 120,
    variant: Optional[str] = None,
) -> tuple[str, str]:
    """
    Call a model and return (stdout, stderr).

    Prefers the persistent server's HTTP API (no per-call process) and falls
    back to a standalone ``opencode run`` if the server is unavailable. Raises
    ``RateLimited`` on HTTP 429-like failures so the caller can mark the model
    as a failed connection.
    """
    if _SERVER_ENABLED and not _SERVER["failed"]:
        try:
            return await _run_via_api(prompt, model, timeout, variant)
        except _ServerUnavailable as e:
            log("SERVER", f"server unavailable ({e}) — using standalone calls")
            _disable_server()
    return await _run_via_subprocess(prompt, model, timeout, variant)


async def call_model(
    prompt: str,
    model: str,
    phase: str = "call",
    timeout: float = 120,
    *,
    rotate: bool = True,
    variant: Optional[str] = None,
) -> str:
    """
    Call a model via ``opencode run``.

    With ``rotate=True`` (default) the slot retries with exponential backoff
    and falls through to the remaining free models, capped at MAX_RETRIES.

    With ``rotate=False`` (used by Phase 1 — proposals) each model is called
    exactly ONCE: one proposal per working modelcars. A rate-limited or
    otherwise failed model is marked FAILED (blacklisted) and is NOT retried
    within this slot nor reached again for the rest of the run.

    ``variant`` optionally selects a provider-specific model variant (e.g.
    "minimal" to reduce reasoning effort on cheap utility phases).

    Returns the model's text output, or an error placeholder string.
    """
    if not rotate:
        try:
            set_model_state(model, MODEL_STATE_TOUCHING)
            log(phase, f"calling {model} (single attempt)")
            stdout, _ = await _run_opencode(
                prompt, model, timeout=timeout, variant=variant
            )
            output = _extract_json_text(stdout)
            set_model_state(model, MODEL_STATE_REACHED)
            log(phase, f"{model} responded ({len(output)} chars)")
            return output
        except Exception as e:
            # Rate limit / connection failure → failed connection, blacklist.
            set_model_state(model, MODEL_STATE_FAILED)
            log(phase, f"{model} failed → blacklisted, not retried: {e}")
            return f"[ERROR from {model}]"

    # Original model first, then other free models as fallbacks, capped so
    # a slot gives up after at most MAX_RETRIES retries no matter how many
    # free models were detected. Failed/blacklisted models are skipped.
    free_models = [m for m in get_free_models() if not is_model_failed(m)]
    models_to_try = (
        [model] + [m for m in free_models if m != model]
    )[:MAX_RETRIES + 1]

    last_err: Optional[Exception] = None
    for attempt, m in enumerate(models_to_try):
        # Exponential backoff, capped so rotations don't drag on forever
        backoff = min(BASE_BACKOFF ** (attempt + 1), 8.0)
        if is_model_failed(m):
            log(phase, f"{m} is blacklisted — skipping rotation target")
            continue
        try:
            set_model_state(m, MODEL_STATE_TOUCHING)
            log(phase, f"calling {m} (attempt {attempt + 1})")
            stdout, _ = await _run_opencode(
                prompt, m, timeout=timeout, variant=variant
            )
            # Extract the reply from the --format json event stream
            output = _extract_json_text(stdout)
            set_model_state(m, MODEL_STATE_REACHED)
            log(phase, f"{m} responded ({len(output)} chars)")
            return output
        except Exception as e:
            last_err = e
            set_model_state(m, MODEL_STATE_FAILED)
            log(phase, f"{m} failed: {e}")
            if attempt < len(models_to_try) - 1:
                log(phase, f"backing off {backoff:.1f}s, then rotating model")
                await asyncio.sleep(backoff)

    err_text = str(last_err)[:200] if last_err is not None else "unknown"
    return f"[ERROR] All models failed. Last error: {err_text}"


def _extract_json_text(raw: str) -> str:
    """
    Extract the assistant's reply from ``opencode run --format json`` output.

    opencode emits newline-delimited JSON events, e.g.:
        {"type":"text", "part":{"type":"text","text":"Hello"}}
    The ``type:"text"`` events are the actual reply. A legacy single-JSON
    envelope ({"content": ...}, {"message": ...}) is also handled, and plain
    text is returned as-is as a final fallback.
    """
    raw = raw.strip()
    if not raw:
        return "[empty response]"

    pieces: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "text":
            part = obj.get("part")
            if isinstance(part, dict):
                txt = part.get("text")
                if isinstance(txt, str) and txt:
                    pieces.append(txt)

    if pieces:
        return "".join(pieces)

    # Legacy: single JSON envelope with a message/content field
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            for key in ("content", "message", "text", "output", "response"):
                if key in data:
                    val = data[key]
                    if isinstance(val, str):
                        return val
                    if isinstance(val, dict) and "content" in val:
                        return str(val["content"])
                    if isinstance(val, list):
                        parts = []
                        for item in val:
                            if isinstance(item, dict) and "text" in item:
                                parts.append(item["text"])
                        if parts:
                            return "\n".join(parts)
                        return json.dumps(val)
                    return json.dumps(val)
    except (json.JSONDecodeError, TypeError):
        pass

    # Fallback: treat raw stdout as plain text
    return raw


# ---------------------------------------------------------------------------
# Phase 0 — Triage
# ---------------------------------------------------------------------------


async def phase_triage(request: str) -> str:
    """Classify the user's request as simple / moderate / complex."""
    prompt = (
        "Classify the following user request by difficulty.\n"
        "Answer with exactly ONE word: simple, moderate, or complex.\n"
        "No explanation — just the word.\n\n"
        f"User request: {request}"
    )
    model = get_fast_model()
    result = await call_model(
        prompt, model, phase="TRIAGE", timeout=60, variant=_UTILITY_VARIANT
    )
    # Some providers reject unknown variants — retry once without it.
    if result.startswith("[ERROR") and _UTILITY_VARIANT:
        log("TRIAGE", "variant rejected — retrying without variant")
        result = await call_model(prompt, model, phase="TRIAGE", timeout=60)
    result_lower = result.strip().lower()
    # Extract the classification word
    for word in ("simple", "moderate", "complex"):
        if word in result_lower:
            return word
    log("TRIAGE", f"Could not parse classification from: {result!r}, defaulting to moderate")
    return "moderate"


# ---------------------------------------------------------------------------
# Phase 1 — Parallel proposal
# ---------------------------------------------------------------------------


async def phase_proposals(
    request: str,
    file_content: Optional[str],
    file_path: Optional[str],
    models: list[str],
) -> list[str]:
    """Run the given models in parallel to get proposals."""
    n_models = len(models)

    if file_content:
        base_prompt = (
            f"You are working on file: {file_path}\n\n"
            f"Here is the current file content:\n"
            f"```\n{file_content}\n```\n\n"
            f"User request: {request}\n\n"
            "Propose a change as a unified diff (standard diff format).\n"
            "Only output the diff — no explanation outside the diff."
        )
    else:
        base_prompt = (
            f"Answer the following coding question concisely.\n\n"
            f"Question: {request}"
        )

    log("PHASE1", f"Launching {n_models} parallel proposal(s)...")
    tasks = [
        call_model(base_prompt, m, phase=f"PROPOSAL-{i}", timeout=180, rotate=False)
        for i, m in enumerate(models)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    proposals = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            log("PHASE1", f"Model {models[i]} raised: {r}")
            proposals.append(f"[ERROR from {models[i]}]")
        else:
            proposals.append(r)
            log("PHASE1", f"Proposal {i} from {models[i]}: {len(r)} chars")

    return proposals


# ---------------------------------------------------------------------------
# Phase 2 — Agreement check
# ---------------------------------------------------------------------------


def _is_diff(text: str) -> bool:
    """Heuristic: does the text look like a unified diff?"""
    return ("--- a/" in text or "--- " in text) and ("+++ b/" in text or "@@ " in text)


def _diff_overlaps(d1: str, d2: str) -> bool:
    """
    Check whether two unified diffs touch overlapping line ranges.
    Returns True if they might conflict.
    """
    def _extract_ranges(diff_text: str) -> list[tuple[int, int]]:
        ranges = []
        for m in re.finditer(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", diff_text):
            start = int(m.group(2))
            # Try to estimate end from the next hunk or just use a generous window
            ranges.append((start, start + 200))
        return ranges

    ranges1 = _extract_ranges(d1)
    ranges2 = _extract_ranges(d2)
    if not ranges1 or not ranges2:
        return True  # can't tell — assume overlap to be safe

    for s1, e1 in ranges1:
        for s2, e2 in ranges2:
            if s1 <= e2 and s2 <= e1:
                return True
    return False


def _text_similar(a: str, b: str, threshold: float = 0.6) -> bool:
    """Simple similarity check for text answers."""
    ratio = SequenceMatcher(None, a.strip(), b.strip()).ratio()
    return ratio >= threshold


async def phase_agreement(proposals: list[str]) -> bool:
    """
    Check if proposals agree.
    Returns True if they agree, False if they disagree.
    """
    if len(proposals) < 2:
        log("AGREEMENT", "Only one proposal — auto-agree")
        return True

    # Check if all proposals are errors
    valid = [p for p in proposals if not p.startswith("[ERROR")]
    if len(valid) < 2:
        log("AGREEMENT", "Fewer than 2 valid proposals — auto-agree")
        return True

    # Fast heuristic check
    all_diffs = all(_is_diff(p) for p in valid)
    if all_diffs:
        # Check pairwise overlap
        for i in range(len(valid)):
            for j in range(i + 1, len(valid)):
                if _diff_overlaps(valid[i], valid[j]):
                    log("AGREEMENT", "Diffs overlap — need classification call")
                    # Fall through to classification call below
                    break
            else:
                continue
            break
        else:
            log("AGREEMENT", "Diffs do NOT overlap — likely agree on different parts")
            return True
    else:
        # Text answers — quick similarity check
        all_similar = True
        for i in range(len(valid)):
            for j in range(i + 1, len(valid)):
                if not _text_similar(valid[i], valid[j]):
                    all_similar = False
                    break
            if not all_similar:
                break
        if all_similar:
            log("AGREEMENT", "Text answers are similar — auto-agree")
            return True

    # Ambiguous — ask one model for a classification
    log("AGREEMENT", "Ambiguous agreement — asking judge model for classification")
    numbered = "\n\n".join(
        f"--- Proposal {i} ---\n{p}" for i, p in enumerate(valid)
    )
    prompt = (
        "Do these proposals agree in substance (i.e. would they produce "
        "the same functional result)?\n"
        "Answer ONLY 'yes' or 'no'.\n\n"
        f"{numbered}"
    )
    model = get_fast_model()
    result = await call_model(
        prompt, model, phase="AGREEMENT", timeout=60, variant=_UTILITY_VARIANT
    )
    if result.startswith("[ERROR") and _UTILITY_VARIANT:
        log("AGREEMENT", "variant rejected — retrying without variant")
        result = await call_model(prompt, model, phase="AGREEMENT", timeout=60)
    agrees = "yes" in result.lower().strip()
    log("AGREEMENT", f"Classification: {'AGREE' if agrees else 'DISAGREE'}")
    return agrees


# ---------------------------------------------------------------------------
# Phase 3 — Debate
# ---------------------------------------------------------------------------


async def phase_debate(
    proposals: list[str],
    request: str,
    file_content: Optional[str],
    rounds: int = 1,
    models: Optional[list[str]] = None,
) -> list[str]:
    """
    Show each model the other proposals and ask them to defend or revise.
    Runs for `rounds` round(s).
    """
    if models is None:
        models = get_free_models()[:len(proposals)]

    valid = [p for p in proposals if not p.startswith("[ERROR")]
    if len(valid) < 2:
        return valid

    current = list(valid)

    for rnd in range(1, rounds + 1):
        log("DEBATE", f"--- Round {rnd} ---")

        tasks = []
        for i, proposal in enumerate(current):
            others = [current[j] for j in range(len(current)) if j != i]
            others_text = "\n\n".join(
                f"--- Other proposal ({j}) ---\n{o}"
                for j, o in enumerate(others)
            )
            prompt = (
                "You proposed the following:\n\n"
                f"--- Your proposal ---\n{proposal}\n\n"
                "Another model proposed:\n\n"
                f"{others_text}\n\n"
                "Given the disagreement, either:\n"
                "1. Defend your proposal and explain why it's better, OR\n"
                "2. Revise your proposal to incorporate valid points from the other.\n\n"
                "Output your FINAL revised (or defended) proposal now.\n"
                "If it's a diff, output only the diff."
            )
            model = models[i] if i < len(models) else get_free_models()[0]
            tasks.append(
                call_model(prompt, model, phase=f"DEBATE-R{rnd}-{i}", timeout=180)
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)
        new = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                log("DEBATE", f"Model failed during debate: {r}")
                new.append(current[i])  # keep old
            else:
                new.append(r)
        current = new

    return current


# ---------------------------------------------------------------------------
# Phase 4 — Judge
# ---------------------------------------------------------------------------


async def phase_judge(
    proposals: list[str],
    request: str,
    file_content: Optional[str],
    unresolved_disagreement: bool,
) -> str:
    """Send final proposals to the judge model for resolution."""
    numbered = "\n\n".join(
        f"--- Proposal {i} ---\n{p}" for i, p in enumerate(proposals)
    )

    conflict_note = ""
    if unresolved_disagreement:
        conflict_note = (
            "\n\n⚠ The proposals above STILL DISAGREE after debate. "
            "You must either:\n"
            "- Merge both intents safely if possible, OR\n"
            "- Output NEEDS_HUMAN_REVIEW followed by both conflicting options.\n"
            "Never silently pick one side on a genuine code conflict.\n"
        )

    file_note = ""
    if file_content:
        file_note = (
            "\n\nOriginal file for reference:\n"
            f"```\n{file_content}\n```\n"
        )

    prompt = (
        "You are the judge in a multi-model council.\n"
        "Below are proposals from different models for the user's request.\n\n"
        f"User request: {request}\n"
        f"{file_note}\n"
        "Proposals:\n\n"
        f"{numbered}\n"
        f"{conflict_note}\n"
        "Output the final, polished answer or diff. Be concise and precise.\n"
        "If you decide the answer requires human review, start with "
        "NEEDS_HUMAN_REVIEW on its own line."
    )

    log("JUDGE", "Calling judge model...")
    result = await call_model(prompt, get_judge_model(), phase="JUDGE", timeout=180)
    log("JUDGE", f"Judge output: {len(result)} chars")
    return result


# ---------------------------------------------------------------------------
# Phase 5 — Apply (the ONLY write step)
# ---------------------------------------------------------------------------


async def _apply_diff(diff_text: str, file_path: str) -> bool:
    """
    Apply a unified diff to a file using Python's built-in capabilities.
    Falls back to ``patch`` or ``git apply`` if available.
    Returns True on success.
    """
    import os

    if not os.path.isfile(file_path):
        log("APPLY", f"File not found: {file_path}")
        return False

    # Try git apply first (handles git-format diffs nicely)
    try:
        ret = await _shell_apply("git apply --check", diff_text)
        if ret == 0:
            await _shell_apply("git apply", diff_text)
            log("APPLY", "Applied via git apply")
            return True
    except Exception:
        pass

    # Try the `patch` command
    try:
        ret = await _shell_apply(
            "patch --no-backup-if-mismatch -p1", diff_text, file_path
        )
        if ret == 0:
            log("APPLY", "Applied via patch")
            return True
    except Exception:
        pass

    # Last resort: Python difflib-based application
    log("APPLY", "Falling back to Python difflib patch application")
    return _python_apply_diff(diff_text, file_path)


async def _shell_apply(cmd: str, diff_text: str) -> int:
    """Shell out to apply a diff via the given command (diff on stdin)."""
    cmd_parts = cmd.split()
    proc = await asyncio.create_subprocess_exec(
        *cmd_parts,
        stdin=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    _, _ = await proc.communicate(input=diff_text.encode())
    return proc.returncode


def _python_apply_diff(diff_text: str, file_path: str) -> bool:
    """
    Minimal pure-Python diff application for unified diffs.
    Handles single-file diffs only.
    """
    import os

    with open(file_path, "r") as f:
        lines = f.readlines()

    new_lines: list[str] = []
    hunk_re = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    current_line = 0
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("---") or raw_line.startswith("+++") or raw_line.startswith("@@"):
            m = hunk_re.match(raw_line)
            if m:
                # Copy unchanged lines up to the hunk start
                target_line = int(m.group(1)) - 1  # 0-indexed
                while current_line < target_line and current_line < len(lines):
                    new_lines.append(lines[current_line])
                    current_line += 1
            continue

        if raw_line.startswith("-"):
            # Deletion — skip the line from original
            current_line += 1
        elif raw_line.startswith("+"):
            # Addition — insert new line
            new_lines.append(raw_line[1:] + "\n")
        else:
            # Context line — copy
            if current_line < len(lines):
                new_lines.append(lines[current_line])
                current_line += 1

    # Copy any remaining lines
    while current_line < len(lines):
        new_lines.append(lines[current_line])
        current_line += 1

    with open(file_path, "w") as f:
        f.writelines(new_lines)

    log("APPLY", "Applied via Python fallback")
    return True


def _deliver_answer(text: str) -> None:
    """Hand the final answer to the GUI callback, if one is registered."""
    if ANSWER_CALLBACK is not None:
        ANSWER_CALLBACK(text)


async def phase_apply(judge_output: str, file_path: Optional[str]) -> None:
    """Handle the final output: display and optionally apply."""
    needs_review = judge_output.strip().startswith("NEEDS_HUMAN_REVIEW")

    if needs_review:
        print("\n" + "=" * 70)
        print("⚠  NEEDS_HUMAN_REVIEW")
        print("=" * 70)
        print(judge_output)
        print("=" * 70)
        print("\nConflicting proposals could not be merged automatically.")
        print("Please review and apply manually.\n")
        _deliver_answer("⚠  NEEDS_HUMAN_REVIEW\n\n" + judge_output)
        return

    is_diff = _is_diff(judge_output)
    if is_diff and file_path:
        print("\n" + "=" * 70)
        print("Proposed diff:")
        print("=" * 70)
        print(judge_output)
        print("=" * 70)
        _deliver_answer(judge_output)

        if ASK_YES_NO is not None:
            answer = "y" if ASK_YES_NO(f"Apply this diff to:\n\n{file_path}") else "n"
        else:
            answer = input("\nApply this diff? [y/N] ").strip().lower()
        if answer == "y":
            success = await _apply_diff(judge_output, file_path)
            if success:
                log("APPLY", f"Successfully applied to {file_path}")
            else:
                log("APPLY", "Failed to apply diff — please apply manually")
        else:
            log("APPLY", "Skipped by user")
    else:
        # Non-diff answer — just print it
        print("\n" + "=" * 70)
        print("Council answer:")
        print("=" * 70)
        print(judge_output)
        print("=" * 70)
        _deliver_answer(judge_output)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


async def run_council(
    request: str,
    file_path: Optional[str] = None,
    models: Optional[list[str]] = None,
    debate_rounds: int = 1,
) -> None:
    """Run the full council pipeline."""
    print("\n🏛  COUNCIL — Multi-LLM Orchestrator")
    print("=" * 50)
    print(f"  Request: {request[:80]}{'...' if len(request) > 80 else ''}")
    if file_path:
        print(f"  File:    {file_path}")
    print("=" * 50 + "\n")

    # Read file content if provided (read-only — we never pass filesystem access)
    file_content = None
    if file_path:
        try:
            with open(file_path, "r") as f:
                file_content = f.read()
            log("INIT", f"Read {len(file_content)} chars from {file_path}")
        except FileNotFoundError:
            log("INIT", f"File not found: {file_path} — proceeding without file context")
        except Exception as e:
            log("INIT", f"Could not read {file_path}: {e}")

    reset_model_states()

    # --- Phase 0: Triage (skipped if the user picked models interactively) ---
    print("─" * 50)
    if models is None:
        # Fast path: trivial, file-less requests skip the entire pipeline.
        if not file_path:
            reply = _instant_reply(request)
            if reply is not None:
                log("PHASE0", "Greeting detected — instant reply (no model calls)")
                await phase_apply(reply, file_path)
                print("\n🏛  Council session complete.\n")
                return
            if _is_trivial_request(request, file_path):
                log("PHASE0", "Trivial request — single model, triage skipped")
                result = await call_model(
                    request, get_fast_model(), phase="SIMPLE", timeout=60
                )
                await phase_apply(result, file_path)
                print("\n🏛  Council session complete.\n")
                return

        log("PHASE0", "Classifying request difficulty...")
        difficulty = await phase_triage(request)
        log("PHASE0", f"Difficulty: {difficulty}")

        if difficulty == "simple":
            log("PHASE0", "Simple task — routing to single model")
            result = await call_model(
                request, get_fast_model(), phase="SIMPLE", timeout=120,
                variant=_UTILITY_VARIANT,
            )
            if result.startswith("[ERROR") and _UTILITY_VARIANT:
                log("PHASE0", "variant rejected — retrying without variant")
                result = await call_model(request, get_fast_model(), phase="SIMPLE")
            await phase_apply(result, file_path)
            print("\n🏛  Council session complete.\n")
            return

        n_models = 2 if difficulty == "moderate" else 3
        models = get_free_models()[:n_models]
    else:
        log("PHASE0", "Interactive mode — using your chosen models (triage skipped)")
        log("PHASE0", f"Models: {', '.join(models)}")
    print("─" * 50)

    # --- Phase 1: Parallel proposals ---
    log("PHASE1", f"Requesting proposals from {len(models)} models...")
    proposals = await phase_proposals(request, file_content, file_path, models)
    print("─" * 50)

    # --- Phase 2: Agreement check ---
    log("PHASE2", "Checking agreement between proposals...")
    agrees = await phase_agreement(proposals)
    print("─" * 50)

    # --- Phase 3: Debate (only if disagreement) ---
    final_proposals = proposals
    unresolved_disagreement = False

    if not agrees:
        if debate_rounds > 0:
            log("PHASE3", f"Disagreement detected — launching {debate_rounds} debate round(s)...")
            final_proposals = await phase_debate(
                proposals,
                request,
                file_content,
                rounds=debate_rounds,
                models=models,
            )
        else:
            log("PHASE3", "Disagreement detected but debate rounds = 0 — skipping debate")
        # Re-check agreement after debate (or use original proposals if no debate)
        log("PHASE3", "Re-checking agreement after debate...")
        post_debate_agrees = await phase_agreement(final_proposals)
        unresolved_disagreement = not post_debate_agrees
        log("PHASE3", f"After debate: {'still disagree' if unresolved_disagreement else 'now agree'}")
    else:
        log("PHASE2", "Proposals agree — skipping debate")
    print("─" * 50)

    # --- Phase 4: Judge ---
    valid_final = [p for p in final_proposals if not p.startswith("[ERROR")]
    if not unresolved_disagreement and not file_content:
        # Agreed, file-less (pure text) answers need no resolution step.
        # Keep the original proposals as candidates too: debate replies are
        # often short meta-comments and would otherwise discard a longer,
        # valid answer produced in Phase 1.
        candidates = list(valid_final)
        for p in proposals:
            if not p.startswith("[ERROR") and p not in candidates:
                candidates.append(p)
        if candidates:
            log("PHASE4", "Proposals agree and no file — skipping judge")
            best = max(candidates, key=len)
            await phase_apply(best, file_path)
            print("\n🏛  Council session complete.\n")
            return

    log("PHASE4", "Sending to judge for final resolution...")
    judge_output = await phase_judge(
        final_proposals, request, file_content, unresolved_disagreement
    )
    print("─" * 50)

    # --- Phase 5: Apply ---
    log("PHASE5", "Handling final output...")
    await phase_apply(judge_output, file_path)

    print("\n🏛  Council session complete.\n")


# ---------------------------------------------------------------------------
# GUI (Tkinter) interface — pops up its own window when no request is given
# ---------------------------------------------------------------------------


def launch_gui() -> None:
    """
    Launch the council GUI window. Blocking (runs the Tk mainloop).

    The council pipeline runs in a background thread; its stdout output
    (phase logs, final answer) is streamed into the window's output pane.
    """
    global ASK_YES_NO

    import queue
    import threading
    import tkinter as tk
    import tkinter.ttk as ttk
    from tkinter import filedialog

    DARK = {
        "bg": "#0b1020",
        "bg_deep": "#070b16",
        "card": "#141d31",
        "card2": "#18243b",
        "surface": "#1b2942",
        "surface2": "#233452",
        "surface3": "#33496d",
        "fg": "#edf4ff",
        "muted": "#9caac1",
        "dim": "#62718b",
        "accent": "#77a9ff",
        "accent2": "#49e0c3",
        "teal": "#49e0c3",
        "blue": "#77a9ff",
        "violet": "#b899ff",
        "amber": "#f5c66b",
        "coral": "#ff8e9d",
        "cyan": "#67d9ee",
        "danger": "#ff718d",
        "warning": "#f5c66b",
        "success": "#5ee0a0",
        "border": "#2a3b5b",
    }

    def _round_rect(cv, x1, y1, x2, y2, r, **kw):
        cv.create_arc(x1, y1, x1 + 2 * r, y1 + 2 * r, start=90, extent=90, **kw)
        cv.create_arc(x2 - 2 * r, y1, x2, y1 + 2 * r, start=0, extent=90, **kw)
        cv.create_arc(x2 - 2 * r, y2 - 2 * r, x2, y2, start=270, extent=90, **kw)
        cv.create_arc(x1, y2 - 2 * r, x1 + 2 * r, y2, start=180, extent=90, **kw)
        cv.create_rectangle(x1 + r, y1, x2 - r, y2, **kw)
        cv.create_rectangle(x1, y1 + r, x2, y2 - r, **kw)

    class PillButton(tk.Canvas):
        def __init__(self, parent, text, command, bg=DARK["accent"],
                     fg=DARK["bg"], hover=None, font=("Segoe UI", 11, "bold"),
                     padx=18, pady=10, radius=10, height=40, width=180):
            try:
                parent_bg = parent.cget("bg")
            except tk.TclError:
                parent_bg = DARK["bg"]
            super().__init__(parent, width=width, height=height, bg=parent_bg,
                             highlightthickness=0, bd=0, cursor="hand2",
                             takefocus=1)
            self._bg = bg
            self._fg = fg
            self._hover = hover or _blend(bg, "#ffffff", 0.16)
            self._text = text
            self._cmd = command
            self._font = font
            self._r = radius
            self._h = height
            self._enabled = True
            self._pressed = False
            self._inside = False
            self.bind("<Configure>", lambda _e: self._draw())
            self.bind("<Enter>", self._enter)
            self.bind("<Leave>", self._leave)
            self.bind("<ButtonPress-1>", self._press)
            self.bind("<ButtonRelease-1>", self._release)
            self.bind("<Return>", self._keyboard_fire)
            self.bind("<space>", self._keyboard_fire)
            self.after_idle(self._draw)

        def _draw(self):
            self.delete("all")
            w = max(self.winfo_width(), self._h + 12)
            h = self._h
            base = self._bg if self._enabled else DARK["surface2"]
            fill = self._hover if self._pressed and self._enabled else base
            if self._pressed and self._enabled:
                fill = _blend(self._hover, "#ffffff", 0.06)
            y = 2 if self._pressed else 1
            shadow = _blend(DARK["bg_deep"], DARK["bg"], 0.25)
            _round_rect(self, 4, 5, w - 2, h + 1, self._r,
                        fill=shadow, outline=shadow)
            if self._enabled and (self._inside or self._pressed):
                glow = _blend(DARK["bg"], self._hover, 0.2)
                _round_rect(self, 1, y, w - 1, h - 1, self._r,
                            fill=glow, outline=glow)
            _round_rect(self, 3, y, w - 3, h - 3, self._r,
                        fill=fill, outline=_blend(fill, "#ffffff", 0.12))
            _round_rect(self, 5, y + 2, w - 5, max(y + 3, h // 2),
                        max(2, self._r - 3), fill=_blend(fill, "#ffffff", 0.08),
                        outline="")
            self.create_text(
                w // 2, h // 2 + (1 if self._pressed else 0),
                text=self._text, anchor="center",
                fill=self._fg if self._enabled else DARK["muted"],
                font=self._font,
            )

        def _enter(self, _event=None):
            self._inside = True
            self._draw()

        def _leave(self, _event=None):
            self._inside = False
            self._pressed = False
            self._draw()

        def _press(self, _event=None):
            if self._enabled:
                self._pressed = True
                self._draw()

        def _release(self, event=None):
            should_fire = self._enabled and self._pressed and self._inside
            self._pressed = False
            self._draw()
            if should_fire and self._cmd:
                self._cmd()
            return "break"

        def _keyboard_fire(self, _event=None):
            if self._enabled and self._cmd:
                self._pressed = True
                self._draw()
                self.after(70, self._finish_keyboard)
            return "break"

        def _finish_keyboard(self):
            self._pressed = False
            self._draw()
            if self._cmd:
                self._cmd()

        def set_text(self, text):
            self._text = text
            self._draw()

        def set_enabled(self, on: bool, text: str | None = None):
            self._enabled = on
            if text is not None:
                self._text = text
            self.configure(cursor="hand2" if on else "arrow")
            self._draw()

    def _hex_to_rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

    def _rgb_to_hex(rgb):
        return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"

    def _blend(c1, c2, t):
        a, b = _hex_to_rgb(c1), _hex_to_rgb(c2)
        return _rgb_to_hex(tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3)))

    def _widget_bg(widget):
        try:
            return widget.cget("bg")
        except tk.TclError:
            return DARK["bg"]

    def _sanitize_text(value):
        return ("" if value is None else str(value)).encode(
            "utf-8", errors="replace").decode("utf-8")

    def _safe_text(value):
        text = _sanitize_text(value)
        if len(text) > 18000:
            text = text[:17980] + "\n… output truncated …"
        return text

    def _short_model(model):
        name = model or "COUNCIL"
        return name.rsplit("/", 1)[-1]

    def _initials(name):
        parts = re.findall(r"[A-Za-z0-9]+", name or "COUNCIL")
        if not parts:
            return "•"
        return "".join(parts[:2]).upper()[:2] or "•"

    class Surface(tk.Frame):
        def __init__(self, parent, bg=None, accent=None, radius=12, **kw):
            self._parent_bg = _widget_bg(parent)
            self._card_bg = bg or DARK["card"]
            self._accent = accent or DARK["teal"]
            self._radius = radius
            self._last_size = None
            self._drawing = False
            super().__init__(parent, bg=self._parent_bg,
                             highlightthickness=0, bd=0, **kw)
            self.canvas = tk.Canvas(self, bg=self._parent_bg,
                                    highlightthickness=0, bd=0)
            self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
            self.body = tk.Frame(self, bg=self._card_bg, bd=0)
            self.body.pack(fill="both", expand=True, padx=14, pady=(8, 12))
            self.canvas.bind("<Configure>", lambda _e: self._redraw())
            self.after_idle(self._redraw)

        def _redraw(self):
            if self._drawing:
                return
            try:
                w = max(self.winfo_width(), 180)
                h = max(self.winfo_height(), 74)
                size = (w, h)
                if self._last_size == size:
                    return
                self._drawing = True
                self._last_size = size
                self.canvas.delete("surface_shape")
                for layer in range(5, 0, -1):
                    shadow = _blend(self._parent_bg, DARK["bg_deep"],
                                     0.25 + layer * 0.08)
                    _round_rect(self.canvas, layer, layer + 1,
                                w - layer + 1, h - layer + 2,
                                self._radius + layer, fill=shadow,
                                outline=shadow, tags="surface_shape")
                _round_rect(self.canvas, 3, 2, w - 3, h - 3,
                            self._radius, fill=self._card_bg,
                            outline=DARK["border"], tags="surface_shape")
                for i in range(5):
                    col = _blend(self._accent, self._card_bg, i / 5)
                    self.canvas.create_line(14, 3 + i, w - 14, 3 + i,
                                            fill=col, width=1,
                                            tags="surface_shape")
            except tk.TclError:
                pass
            finally:
                self._drawing = False

    class BreathingDot(tk.Canvas):
        def __init__(self, parent, size=14, color=None, bg=None, **kw):
            self._size = size
            self._color = color or DARK["teal"]
            self._bg = bg or _widget_bg(parent)
            self._phase = 0
            self._job = None
            super().__init__(parent, width=size, height=size, bg=self._bg,
                             highlightthickness=0, bd=0, **kw)
            self.bind("<Destroy>", self._stop)
            self.after(60, self._draw)

        def _stop(self, _event=None):
            if self._job is not None:
                try:
                    self.after_cancel(self._job)
                except tk.TclError:
                    pass
                self._job = None

        def set_color(self, color):
            self._color = color
            self._draw()

        def _draw(self):
            self._stop()
            try:
                if not self.winfo_exists():
                    return
                self.delete("all")
                p = (self._phase % 100) / 100
                wave = (1 - abs((p * 2) - 1))
                c = self._size / 2
                halo = _blend(self._bg, self._color, 0.08 + wave * 0.16)
                self.create_oval(c - 7 - wave, c - 7 - wave,
                                 c + 7 + wave, c + 7 + wave,
                                 fill=halo, outline="")
                core = _blend(self._color, "#ffffff", 0.08 + wave * 0.18)
                r = 3.0 + wave * 0.8
                self.create_oval(c - r, c - r, c + r, c + r,
                                 fill=core, outline=self._color)
                self._phase += 8
                self._job = self.after(80, self._draw)
            except tk.TclError:
                self._job = None

    class FocusEditor(tk.Frame):
        def __init__(self, parent, height=104, **kw):
            self._base = _widget_bg(parent)
            self._accent = DARK["teal"]
            self._value = 0
            self._target = 0
            self._job = None
            super().__init__(parent, bg=DARK["border"], highlightthickness=1,
                             highlightbackground=DARK["border"],
                             highlightcolor=self._accent, bd=0, **kw)
            self.text = tk.Text(
                self, height=4, wrap="word", font=("Segoe UI", 12),
                bg=DARK["card"], fg=DARK["fg"], insertbackground=self._accent,
                selectbackground=DARK["surface2"], selectforeground=DARK["fg"],
                relief="flat", borderwidth=0, highlightthickness=0,
                padx=13, pady=11, spacing1=0, spacing2=0, spacing3=2,
                cursor="xterm",
            )
            self.text.pack(fill="both", expand=True, padx=2, pady=2)
            self.text.bind("<FocusIn>", self._focus_in, add="+")
            self.text.bind("<FocusOut>", self._focus_out, add="+")
            self.bind("<Destroy>", self._stop)
            self.configure(height=height)

        def _stop(self, _event=None):
            if self._job is not None:
                try:
                    self.after_cancel(self._job)
                except tk.TclError:
                    pass
                self._job = None

        def _focus_in(self, _event=None):
            self._target = 1
            self._animate()

        def _focus_out(self, _event=None):
            self._target = 0
            self._animate()

        def _animate(self):
            if self._job is not None:
                try:
                    self.after_cancel(self._job)
                except tk.TclError:
                    pass
                self._job = None
            self._value += (self._target - self._value) * 0.42
            if abs(self._target - self._value) < 0.04:
                self._value = self._target
            border = _blend(DARK["border"], self._accent, self._value)
            self.configure(bg=border, highlightbackground=border,
                           highlightcolor=border)
            self.text.configure(bg=_blend(DARK["card"], DARK["surface"],
                                            self._value * 0.12))
            if self._value != self._target:
                self._job = self.after(28, self._animate)

    class ModelCard(tk.Frame):
        def __init__(self, parent, model, variable, command, accent, index=0):
            self.model = model
            self.variable = variable
            self._command = command
            self._accent = accent
            self._index = index
            self._base_bg = DARK["card"]
            self._base_border = DARK["border"]
            self._hover = 0
            self._hover_target = 0
            self._hover_job = None
            self._pulse_job = None
            self._pulse_phase = 0
            self._state = ""
            self._status_key = "idle"
            self._state_color = DARK["dim"]
            self._selected = bool(variable.get())
            super().__init__(parent, bg=self._base_bg, highlightthickness=1,
                             highlightbackground=self._base_border,
                             highlightcolor=accent, bd=0, cursor="hand2",
                             takefocus=1)
            self.avatar = tk.Label(
                self, text=_initials(model), bg=accent, fg=DARK["bg"],
                font=("Segoe UI", 8, "bold"), width=3, padx=1, pady=3,
            )
            self.avatar.pack(side="left", padx=(9, 8), pady=10)
            label = _short_model(model)
            if len(label) > 25:
                label = label[:23] + "…"
            self.name = tk.Label(
                self, text=label, bg=self._base_bg, fg=DARK["fg"],
                font=("Segoe UI", 9, "bold"), anchor="w",
                justify="left",
            )
            self.name.pack(side="left", fill="x", expand=True)
            self.status = tk.Label(
                self, text="idle", bg=self._base_bg, fg=DARK["muted"],
                font=("Segoe UI", 8), anchor="e",
            )
            self.status.pack(side="right", padx=(2, 7))
            self.state_dot = tk.Label(
                self, text="●", bg=self._base_bg, fg=DARK["dim"],
                font=("Segoe UI", 10),
            )
            self.state_dot.pack(side="right", padx=(0, 2))
            self.check = tk.Canvas(
                self, width=22, height=22, bg=self._base_bg,
                highlightthickness=0, bd=0, cursor="hand2",
            )
            self.check.pack(side="right", padx=(0, 5))
            self._bind_widget(self)
            for widget in (self.avatar, self.name, self.status,
                           self.state_dot, self.check):
                self._bind_widget(widget)
            self._trace = self.variable.trace_add("write", self._variable_changed)
            self._draw()
            self.after(20, self._draw)

        def _bind_widget(self, widget):
            widget.bind("<Enter>", self._enter, add="+")
            widget.bind("<Leave>", self._leave, add="+")
            widget.bind("<Button-1>", self._click, add="+")
            widget.bind("<Return>", self._click, add="+")
            widget.bind("<space>", self._click, add="+")

        def _enter(self, _event=None):
            self._hover_target = 1
            self._animate_hover()

        def _leave(self, _event=None):
            self._hover_target = 0
            self._animate_hover()

        def _click(self, _event=None):
            self.variable.set(not bool(self.variable.get()))
            if self._command:
                self._command()
            self.focus_set()
            return "break"

        def _variable_changed(self, *_args):
            self._selected = bool(self.variable.get())
            self._draw()

        def _animate_hover(self):
            if self._hover_job is not None:
                try:
                    self.after_cancel(self._hover_job)
                except tk.TclError:
                    pass
            self._hover += (self._hover_target - self._hover) * 0.38
            if abs(self._hover_target - self._hover) < 0.04:
                self._hover = self._hover_target
            self._draw()
            if self._hover != self._hover_target:
                self._hover_job = self.after(26, self._animate_hover)
            else:
                self._hover_job = None

        def _draw(self):
            try:
                bg = _blend(self._base_bg, self._accent,
                            0.025 + self._hover * 0.09)
                border = _blend(self._base_border, self._accent,
                                0.18 * self._selected + 0.72 * self._hover)
                if self._status_key in ("thinking", "done", "error"):
                    border = _blend(border, self._state_color, 0.5)
                self.configure(bg=bg, highlightbackground=border)
                for widget in (self.avatar, self.name, self.status,
                               self.state_dot, self.check):
                    widget.configure(bg=bg)
                self.status.configure(fg=self._state_color if self._status_key != "idle" else DARK["muted"])
                self.state_dot.configure(fg=self._state_color)
                self._draw_check(bg)
            except tk.TclError:
                pass

        def _draw_check(self, bg):
            self.check.delete("all")
            c = 11
            r = 7 + int(self._hover * 1.5)
            if self._hover > 0:
                self.check.create_oval(
                    c - r - 3, c - r - 3, c + r + 3, c + r + 3,
                    outline=_blend(bg, self._accent, 0.45), width=1,
                )
            fill = DARK["teal"] if self._selected else _blend(bg, DARK["surface2"], 0.8)
            outline = DARK["teal"] if self._selected else DARK["surface3"]
            self.check.create_oval(c - r, c - r, c + r, c + r,
                                   fill=fill, outline=outline, width=1)
            if self._selected:
                self.check.create_line(c - 3, c, c - 1, c + 3, c + 4, c - 3,
                                       fill=DARK["bg"], width=2, capstyle="round",
                                       joinstyle="round")
            else:
                self.check.create_oval(c - 2, c - 2, c + 2, c + 2,
                                       outline=DARK["dim"], width=1)

        def set_state(self, state):
            state = state or ""
            if state == self._state:
                return
            self._state = state
            if state == MODEL_STATE_TOUCHING:
                self._status_key = "thinking"
                self._state_color = DARK["amber"]
                self.status.configure(text="thinking")
                self._start_pulse()
            elif state == MODEL_STATE_REACHED:
                self._status_key = "done"
                self._state_color = DARK["success"]
                self.status.configure(text="done")
                self._stop_pulse()
            elif state == MODEL_STATE_FAILED:
                self._status_key = "error"
                self._state_color = DARK["danger"]
                self.status.configure(text="error")
                self._stop_pulse()
            else:
                self._status_key = "idle"
                self._state_color = DARK["dim"]
                self.status.configure(text="idle")
                self._stop_pulse()
            self._draw()

        def _start_pulse(self):
            self._stop_pulse()
            self._pulse()

        def _pulse(self):
            if self._status_key != "thinking":
                return
            try:
                self._pulse_phase = (self._pulse_phase + 1) % 6
                pulse = 0.12 + (self._pulse_phase % 3) * 0.12
                self.state_dot.configure(
                    fg=_blend(self._state_color, "#ffffff", pulse))
                self._pulse_job = self.after(120, self._pulse)
            except tk.TclError:
                self._pulse_job = None

        def _stop_pulse(self):
            if self._pulse_job is not None:
                try:
                    self.after_cancel(self._pulse_job)
                except tk.TclError:
                    pass
                self._pulse_job = None

        def destroy(self):
            self._stop_pulse()
            if self._hover_job is not None:
                try:
                    self.after_cancel(self._hover_job)
                except tk.TclError:
                    pass
            try:
                self.variable.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass
            super().destroy()

    class ActivityProgress(tk.Frame):
        _steps = (("BRIEF", "brief"), ("PROPOSE", "propose"),
                  ("DEBATE", "debate"), ("JUDGE", "judge"),
                  ("APPLY", "apply"))

        def __init__(self, parent):
            self._current = -1
            self._progress = 0.04
            self._target = 0.04
            self._job = None
            self._round_total = 0
            self._round = 0
            super().__init__(parent, bg=DARK["card"], bd=0, highlightthickness=0)
            head = tk.Frame(self, bg=DARK["card"])
            head.pack(fill="x", padx=14, pady=(12, 2))
            self._dot = BreathingDot(head, size=11, color=DARK["teal"],
                                     bg=DARK["card"])
            self._dot.pack(side="left", pady=1)
            self._head = tk.Label(head, text="LIVE ACTIVITY", bg=DARK["card"],
                                   fg=DARK["muted"], font=("Segoe UI", 8, "bold"))
            self._head.pack(side="left", padx=(7, 0))
            self._phase = tk.Label(head, text="AWAITING INPUT", bg=DARK["card"],
                                    fg=DARK["teal"], font=("Segoe UI", 9, "bold"),
                                    anchor="e")
            self._phase.pack(side="right")
            self.track = tk.Canvas(self, height=47, bg=DARK["card"],
                                   highlightthickness=0, bd=0)
            self.track.pack(fill="x", padx=10, pady=(0, 0))
            self._detail = tk.Label(self, text="Ready when you are",
                                    bg=DARK["card"], fg=DARK["dim"],
                                    font=("Segoe UI", 8), anchor="w")
            self._detail.pack(fill="x", padx=14, pady=(0, 3))
            self._round_row = tk.Frame(self, bg=DARK["card"])
            self._round_row.pack(fill="x", padx=14, pady=(0, 10))
            self._round_label = tk.Label(self._round_row, text="DEBATE ROUND",
                                         bg=DARK["card"], fg=DARK["dim"],
                                         font=("Segoe UI", 8, "bold"))
            self._round_label.pack(side="left")
            self._rounds = tk.Frame(self._round_row, bg=DARK["card"])
            self._rounds.pack(side="left", padx=(9, 0))
            self._round_text = tk.Label(self._round_row, text="—",
                                        bg=DARK["card"], fg=DARK["muted"],
                                        font=("Segoe UI", 8))
            self._round_text.pack(side="right")
            self.track.bind("<Configure>", lambda _e: self._draw_track())
            self.after(20, self._draw_track)

        def _draw_track(self):
            try:
                self.track.delete("all")
                w = max(self.track.winfo_width(), 260)
                left, right = 19, w - 19
                y = 15
                self.track.create_line(left, y, right, y,
                                       fill=DARK["surface2"], width=3)
                fraction = max(0, min(1, self._progress))
                active_x = left + (right - left) * fraction
                if fraction > 0:
                    self.track.create_line(left, y, active_x, y,
                                           fill=DARK["teal"], width=3)
                for i, (label, _) in enumerate(self._steps):
                    x = left + (right - left) * i / (len(self._steps) - 1)
                    done = i < self._current
                    current = i == self._current
                    col = DARK["teal"] if done or current else DARK["surface2"]
                    outline = DARK["teal"] if current else col
                    r = 7 if current else 5
                    self.track.create_oval(x - r, y - r, x + r, y + r,
                                           fill=col, outline=outline,
                                           width=1)
                    self.track.create_text(x, y + 20, text=label,
                                           fill=DARK["fg"] if (done or current) else DARK["dim"],
                                           font=("Segoe UI", 7, "bold" if current else "normal"))
            except tk.TclError:
                pass

        def set_phase(self, phase, detail=None):
            phase_key = str(phase).upper()
            mapping = {
                "BRIEF": 0, "TRIAGE": 0, "SIMPLE": 0, "PHASE0": 0,
                "PROPOSE": 1, "PHASE1": 1, "PROPOSAL": 1,
                "DEBATE": 2, "PHASE3": 2, "AGREEMENT": 1,
                "JUDGE": 3, "PHASE4": 3,
                "APPLY": 4, "PHASE5": 4, "COMPLETE": 4,
            }
            self._current = mapping.get(phase_key, self._current)
            name = self._steps[self._current][0] if self._current >= 0 else phase_key
            self._phase.configure(text=f"{self._current + 1:02d}  {name}")
            if detail:
                self._detail.configure(text=detail)
            self._target = 0.96 if self._current >= 0 else 0.04
            self._animate()

        def handle_log(self, tag, message):
            tag = tag.upper()
            if tag.startswith("PHASE1") or tag.startswith("PROPOSAL"):
                self.set_phase("PROPOSE", message)
            elif tag.startswith("PHASE2") or tag == "AGREEMENT":
                self.set_phase("PROPOSE", message)
            elif tag.startswith("PHASE3") or tag.startswith("DEBATE"):
                self.set_phase("DEBATE", message)
            elif tag.startswith("PHASE4") or tag == "JUDGE":
                self.set_phase("JUDGE", message)
            elif tag.startswith("PHASE5") or tag == "APPLY":
                self.set_phase("APPLY", message)
            elif tag.startswith("PHASE0") or tag in ("TRIAGE", "SIMPLE"):
                self.set_phase("BRIEF", message)
            else:
                self._detail.configure(text=message[:72])

        def set_round(self, round_no, total=None):
            self._round = round_no or 0
            if total:
                self._round_total = total
            for child in self._rounds.winfo_children():
                child.destroy()
            if not self._round_total:
                self._round_text.configure(text="—")
                return
            for i in range(1, self._round_total + 1):
                active = i <= self._round
                tk.Label(
                    self._rounds, text=f"R{i}",
                    bg=DARK["teal"] if active else DARK["surface"],
                    fg=DARK["bg"] if active else DARK["muted"],
                    font=("Segoe UI", 7, "bold"), padx=5, pady=2,
                ).pack(side="left", padx=(0, 4))
            self._round_text.configure(
                text=f"{self._round or 0} / {self._round_total}")

        def reset(self):
            self._current = -1
            self._progress = 0.04
            self._target = 0.04
            self._round = 0
            self._round_total = 0
            self._phase.configure(text="AWAITING INPUT")
            self._detail.configure(text="Ready when you are")
            for child in self._rounds.winfo_children():
                child.destroy()
            self._round_text.configure(text="—")
            self._draw_track()

        def complete(self):
            self._current = 4
            self._phase.configure(text="05  COMPLETE")
            self._detail.configure(text="Session finished — ready for the next question")
            self._target = 1.0
            self._progress = 1.0
            self._draw_track()

        def _animate(self):
            if self._job is not None:
                try:
                    self.after_cancel(self._job)
                except tk.TclError:
                    pass
            self._progress += (self._target - self._progress) * 0.25
            if abs(self._target - self._progress) < 0.01:
                self._progress = self._target
            self._draw_track()
            if self._progress != self._target:
                self._job = self.after(35, self._animate)
            else:
                self._job = None

    class ConsoleMessage(tk.Frame):
        def __init__(self, parent, text, role="assistant", bg=None, fg=None,
                     accent=None, model=None, phase=None, font=("Consolas", 10)):
            self._parent_bg = _widget_bg(parent)
            self._actual_bg = bg or DARK["card"]
            self._actual_fg = fg or DARK["fg"]
            self._actual_border = _blend(self._actual_bg, DARK["border"], 0.8)
            self._accent = accent or DARK["teal"]
            self._role = role
            self._model = model
            self._phase = phase
            self._font = font
            self._paint = 0
            self._slide = 10
            self._job = None
            self._last_lines = 0
            super().__init__(parent, bg=self._parent_bg, bd=0, highlightthickness=0)
            self.surface = tk.Frame(self, bg=self._actual_bg,
                                    highlightthickness=1,
                                    highlightbackground=self._actual_border,
                                    bd=0)
            self.surface.pack(fill="x")
            meta = tk.Frame(self.surface, bg=self._actual_bg)
            meta.pack(fill="x", padx=11, pady=(8, 0))
            tag_name = _short_model(model) if model else (phase or role.upper())
            self.avatar = tk.Label(
                meta, text=_initials(model or tag_name), bg=self._accent,
                fg=DARK["bg"], font=("Segoe UI", 7, "bold"),
                width=3, padx=1, pady=2,
            )
            self.avatar.pack(side="left")
            self.tag = tk.Label(meta, text=tag_name[:28], bg=self._actual_bg,
                                fg=self._accent, font=("Segoe UI", 8, "bold"),
                                anchor="w")
            self.tag.pack(side="left", padx=(7, 0))
            self.time = tk.Label(meta, text=time.strftime("%H:%M:%S"),
                                 bg=self._actual_bg, fg=DARK["dim"],
                                 font=("Consolas", 8), anchor="e")
            self.time.pack(side="right")
            self.text = tk.Text(
                self.surface, height=1, width=1, wrap="word", state="disabled",
                font=self._font, bg=self._actual_bg, fg=self._actual_fg,
                relief="flat", borderwidth=0, highlightthickness=0,
                padx=13, pady=7, spacing1=0, spacing2=0, spacing3=2,
                cursor="arrow",
            )
            self.text.pack(fill="x")
            self.text.bind("<Configure>", lambda _e: self._sync_height(), add="+")
            self.text.config(state="normal")
            self.text.insert("1.0", _safe_text(text))
            self.text.config(state="disabled")
            self.pack(fill="x", padx=8, pady=(self._slide, 4))
            self.after(20, self._animate_in)
            self.after(50, self._sync_height)

        def _set_paint(self, value):
            try:
                bg = _blend(self._parent_bg, self._actual_bg, value)
                fg = _blend(self._parent_bg, self._actual_fg, value)
                border = _blend(self._parent_bg, self._actual_border, value)
                self.surface.configure(bg=bg, highlightbackground=border)
                self.avatar.configure(bg=_blend(self._parent_bg, self._accent, value))
                self.tag.configure(bg=bg, fg=_blend(self._parent_bg, self._accent, value))
                self.time.configure(bg=bg)
                self.text.configure(bg=bg, fg=fg)
            except tk.TclError:
                pass

        def _animate_in(self, step=0):
            try:
                value = min(1.0, step / 8)
                self._set_paint(value)
                self.pack_configure(pady=(int(self._slide * (1 - value)), 4))
                if value < 1:
                    self._job = self.after(28, self._animate_in, step + 1)
                else:
                    self._job = None
                    self._sync_height()
            except tk.TclError:
                self._job = None

        def _sync_height(self):
            try:
                result = self.text.count("1.0", "end-1c", "displaylines")
                lines = max(1, int(result[0] if result else 1))
                if lines != self._last_lines:
                    self._last_lines = lines
                    self.text.configure(height=min(lines, 60))
            except tk.TclError:
                pass

        def destroy(self):
            if self._job is not None:
                try:
                    self.after_cancel(self._job)
                except tk.TclError:
                    pass
            super().destroy()

    class ThinkingRow(tk.Frame):
        def __init__(self, parent, model, accent, round_no=0):
            self._parent_bg = _widget_bg(parent)
            self._model = model
            self._accent = accent
            self._round = round_no
            self._phase = 0
            self._job = None
            self._status = "thinking"
            bg = _blend(self._parent_bg, DARK["card"], 0.9)
            super().__init__(parent, bg=bg, highlightthickness=1,
                             highlightbackground=_blend(bg, accent, 0.48),
                             bd=0)
            self.avatar = tk.Label(self, text=_initials(model), bg=accent,
                                    fg=DARK["bg"], font=("Segoe UI", 7, "bold"),
                                    width=3, padx=1, pady=2)
            self.avatar.pack(side="left", padx=(8, 0), pady=6)
            self.label = tk.Label(self, text="", bg=bg, fg=DARK["fg"],
                                   font=("Segoe UI", 8, "bold"), anchor="w")
            self.label.pack(side="left", fill="x", expand=True, padx=7)
            self.dots = tk.Canvas(self, width=32, height=17, bg=bg,
                                  highlightthickness=0, bd=0)
            self.dots.pack(side="right", padx=(0, 8))
            self.set_state("thinking", round_no)
            self._animate()

        def set_round(self, round_no):
            self._round = round_no
            if self._status == "thinking":
                self._update_label()

        def set_state(self, status, round_no=None):
            previous_status = self._status
            previous_round = self._round
            if round_no is not None:
                self._round = round_no
            if previous_status == status and previous_round == self._round:
                return
            self._status = status
            self._update_label()
            if status == "thinking":
                self._animate()
            else:
                self._stop()
                self._draw_dots()

        def _update_label(self):
            short = _short_model(self._model)
            if self._status == "thinking":
                suffix = f"  ·  round {self._round}" if self._round else "  ·  debating"
                text = short + suffix
                color = DARK["fg"]
            elif self._status == "done":
                text = short + "  ·  complete"
                color = DARK["success"]
            else:
                text = short + "  ·  error"
                color = DARK["danger"]
            self.label.configure(text=text[:48], fg=color)

        def _stop(self):
            if self._job is not None:
                try:
                    self.after_cancel(self._job)
                except tk.TclError:
                    pass
                self._job = None

        def _animate(self):
            if self._status != "thinking":
                return
            self._stop()
            try:
                self._phase = (self._phase + 1) % 9
                self._draw_dots()
                self._job = self.after(95, self._animate)
            except tk.TclError:
                self._job = None

        def _draw_dots(self):
            try:
                self.dots.delete("all")
                if self._status == "done":
                    self.dots.create_text(22, 8, text="✓", fill=DARK["success"],
                                          font=("Segoe UI", 10, "bold"))
                    return
                if self._status == "error":
                    self.dots.create_text(22, 8, text="!", fill=DARK["danger"],
                                          font=("Segoe UI", 10, "bold"))
                    return
                for i in range(3):
                    active = (self._phase // 3) % 3 == i
                    col = _blend(DARK["surface2"], self._accent,
                                 0.9 if active else 0.22)
                    self.dots.create_oval(7 + i * 8, 5, 12 + i * 8, 10,
                                          fill=col, outline="")
            except tk.TclError:
                pass

        def destroy(self):
            self._stop()
            super().destroy()

    class ChatArea(tk.Frame):
        def __init__(self, parent, height=18, model_colors=None, **kw):
            self._bg = _widget_bg(parent)
            self._model_colors = dict(model_colors or {})
            self._thinking = {}
            self._round = 0
            try:
                import tkinter.font as tkfont
                line_h = tkfont.Font(font="TkDefaultFont").metrics("linespace")
            except Exception:
                line_h = 17
            super().__init__(parent, bg=self._bg, **kw)
            self.configure(height=height * line_h + 24)
            self.pack_propagate(False)
            self._canvas = tk.Canvas(self, bg=self._bg, highlightthickness=0, bd=0)
            self._vsb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
            self._inner = tk.Frame(self._canvas, bg=self._bg)
            self._win = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
            self._canvas.configure(yscrollcommand=self._vsb.set)
            self._canvas.pack(side="left", fill="both", expand=True)
            self._vsb.pack(side="right", fill="y")
            self._activity_host = tk.Frame(self._inner, bg=self._bg)
            self._activity_host.pack(fill="x", padx=8, pady=(7, 0))
            self._activity_caption = tk.Label(
                self._activity_host, text="ACTIVE MODEL SIGNALS", bg=self._bg,
                fg=DARK["dim"], font=("Segoe UI", 8, "bold"),
            )
            self._activity_caption.pack(anchor="w", padx=4, pady=(0, 3))
            self._messages_host = tk.Frame(self._inner, bg=self._bg)
            self._messages_host.pack(fill="both", expand=True)
            self._inner.bind("<Configure>", self._update_region)
            self._canvas.bind("<Configure>", self._canvas_configure)
            self._bind_wheel(self._canvas)
            self._show_empty()

        def _canvas_configure(self, event):
            try:
                self._canvas.itemconfigure(self._win, width=event.width)
            except tk.TclError:
                pass

        def _update_region(self, _event=None):
            try:
                self._canvas.configure(scrollregion=self._canvas.bbox("all"))
            except tk.TclError:
                pass

        def _bind_wheel(self, widget):
            widget.bind("<MouseWheel>", self._on_wheel, add=True)
            widget.bind("<Button-4>", self._on_wheel, add=True)
            widget.bind("<Button-5>", self._on_wheel, add=True)

        def _on_wheel(self, event):
            if event.num == 4:
                self._canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                self._canvas.yview_scroll(1, "units")
            elif event.delta:
                self._canvas.yview_scroll(-1 * (event.delta // 120), "units")
            return "break"

        def _color_for(self, model):
            if model in self._model_colors:
                return self._model_colors[model]
            if not model:
                return DARK["teal"]
            palette = (DARK["teal"], DARK["blue"], DARK["violet"],
                       DARK["amber"], DARK["coral"], DARK["cyan"])
            return palette[sum(ord(ch) for ch in model) % len(palette)]

        def _show_empty(self):
            self._empty = tk.Label(
                self._messages_host,
                text="Streaming activity will appear here\nmodels report in real time",
                bg=self._bg, fg=DARK["dim"], font=("Consolas", 10),
                justify="center",
            )
            self._empty.pack(pady=48)

        def add_bubble(self, text, role="assistant", bg=None, fg=None,
                       accent=None, font=("Consolas", 10), model=None,
                       phase=None):
            try:
                self._empty.pack_forget()
            except tk.TclError:
                pass
            if role == "user":
                bg = bg or _blend(DARK["card"], DARK["teal"], 0.1)
                fg = fg or DARK["fg"]
                accent = accent or DARK["teal"]
                phase = phase or "YOU"
                font = ("Segoe UI", 10, "bold")
            elif role == "system":
                bg = bg or _blend(self._bg, DARK["card"], 0.74)
                fg = fg or DARK["muted"]
                accent = accent or DARK["blue"]
                font = ("Segoe UI", 9)
            elif role == "error":
                bg = bg or _blend(self._bg, DARK["danger"], 0.14)
                fg = fg or DARK["danger"]
                accent = accent or DARK["danger"]
            else:
                bg = bg or _blend(self._bg, DARK["card"], 0.92)
                fg = fg or DARK["fg"]
                accent = accent or self._color_for(model)
            message = ConsoleMessage(
                self._messages_host, text, role=role, bg=bg, fg=fg,
                accent=accent, model=model, phase=phase, font=font,
            )
            self._bind_wheel(message.text)
            self._canvas.update_idletasks()
            message._sync_height()
            self._update_region()
            self._canvas.yview_moveto(1.0)
            self.after(180, self._scroll_to_end)
            return message

        def _scroll_to_end(self):
            try:
                self._canvas.yview_moveto(1.0)
                self._update_region()
            except tk.TclError:
                pass

        def set_round(self, round_no):
            self._round = round_no or 0
            for row in self._thinking.values():
                row.set_round(self._round)

        def set_model_state(self, model, state, round_no=None):
            if not model:
                return
            if state == MODEL_STATE_TOUCHING:
                row = self._thinking.get(model)
                if row is None:
                    row = ThinkingRow(self._activity_host, model,
                                      self._color_for(model),
                                      round_no or self._round)
                    row.pack(fill="x", pady=(2, 0))
                    self._thinking[model] = row
                else:
                    row.set_state("thinking", round_no or self._round)
            elif state == MODEL_STATE_REACHED:
                row = self._thinking.get(model)
                if row is not None:
                    row.set_state("done", round_no or self._round)
            elif state == MODEL_STATE_FAILED:
                row = self._thinking.get(model)
                if row is None:
                    row = ThinkingRow(self._activity_host, model,
                                      self._color_for(model),
                                      round_no or self._round)
                    row.pack(fill="x", pady=(2, 0))
                    self._thinking[model] = row
                row.set_state("error", round_no or self._round)
            elif state == "":
                row = self._thinking.pop(model, None)
                if row is not None:
                    row.destroy()

        def clear(self):
            for row in list(self._thinking.values()):
                row.destroy()
            self._thinking.clear()
            for child in self._messages_host.winfo_children():
                child.destroy()
            self._round = 0
            self._show_empty()
            self._update_region()

        def begin_session(self):
            self.clear()
            self._activity_caption.configure(text="ACTIVE MODEL SIGNALS")


    def _rounded_popup(parent, title, message, kind="yesno"):
        """Beautiful dark rounded modal popup with round forms and dark tones. kind: yesno|info|warn."""
        top = tk.Toplevel(parent)
        top.withdraw()
        top.overrideredirect(True)
        top.configure(bg=DARK["bg"])
        top.attributes("-topmost", True)
        try:
            top.attributes("-alpha", 0)
        except tk.TclError:
            pass

        W, H = 480, 310 if kind == "yesno" else 250
        top.geometry(f"{W}x{H}")
        cv = tk.Canvas(top, width=W, height=H, bg=DARK["bg"], highlightthickness=0, bd=0)
        cv.pack()

        accent_col = {"yesno": DARK["accent"], "info": DARK["accent2"],
                      "warn": DARK["warning"]}.get(kind, DARK["accent"])
        icon_syms = {"yesno": "◉", "info": "ⓘ", "warn": "⚠"}
        icon = icon_syms.get(kind, "◉")
        accent_rgb = _hex_to_rgb(accent_col)
        accent_light = _rgb_to_hex(tuple(min(255, c + 60) for c in accent_rgb))
        accent_mid = _rgb_to_hex(tuple(min(255, c + 30) for c in accent_rgb))
        accent_dark = _rgb_to_hex(tuple(max(0, c - 50) for c in accent_rgb))
        shadow_base = _hex_to_rgb(DARK["bg"])
        shadow_dark = tuple(max(0, c - 22) for c in shadow_base)

        CARD_R = 38
        PAD = 20

        for i in range(PAD, 0, -1):
            t = i / PAD
            ease = t * t * (3 - 2 * t)
            sr = int(shadow_dark[0] + (shadow_base[0] - shadow_dark[0]) * (1 - ease))
            sg = int(shadow_dark[1] + (shadow_base[1] - shadow_dark[1]) * (1 - ease))
            sb = int(shadow_dark[2] + (shadow_base[2] - shadow_dark[2]) * (1 - ease))
            sc = _rgb_to_hex((sr, sg, sb))
            spread = int(i * 1.8)
            _round_rect(cv, spread, spread, W - spread, H - spread, CARD_R + i,
                        fill=sc, outline=sc)

        for i in range(6):
            alpha_t = 0.35 - i * 0.05
            gc = _blend(DARK["card"], accent_dark, max(0, alpha_t))
            _round_rect(cv, 2 - i, 2 - i, W - 2 + i, H - 2 + i, CARD_R + i,
                        fill="", outline=gc)

        _round_rect(cv, 1, 1, W - 1, H - 1, CARD_R,
                    fill=DARK["card"], outline=DARK["surface2"])
        _round_rect(cv, 3, 3, W - 3, H - 3, CARD_R - 1,
                    fill=DARK["card"], outline="")

        BANNER_H = 88
        BANNER_Y = 48
        banner_top = BANNER_Y
        banner_bot = BANNER_Y + BANNER_H
        grad_steps = 56
        for i in range(grad_steps):
            t = i / (grad_steps - 1)
            if t < 0.25:
                hex_col = _blend(accent_dark, accent_col, t / 0.25)
            elif t < 0.5:
                hex_col = accent_col
            elif t < 0.75:
                hex_col = _blend(accent_col, accent_mid, (t - 0.5) / 0.25)
            else:
                hex_col = _blend(accent_mid, accent_light, (t - 0.75) / 0.25)
            y0 = banner_top + int(i * BANNER_H / grad_steps)
            y1 = banner_top + int((i + 1) * BANNER_H / grad_steps)
            if y1 > y0:
                cv.create_rectangle(PAD + 4, y0, W - PAD - 4, y1, fill=hex_col, outline="")

        clip_r = CARD_R - 2
        cx0, cy0, cx1, cy1 = PAD + 4, banner_top, W - PAD - 4, banner_bot
        for (ax, ay, bx, by, st, ext) in [
            (cx0, cy0, cx0 + 2 * clip_r, cy0 + 2 * clip_r, 90, 90),
            (cx1 - 2 * clip_r, cy0, cx1, cy0 + 2 * clip_r, 0, 90),
            (cx0, cy1 - 2 * clip_r, cx0 + 2 * clip_r, cy1, 180, 90),
            (cx1 - 2 * clip_r, cy1 - 2 * clip_r, cx1, cy1, 270, 90),
        ]:
            cv.create_arc(ax, ay, bx, by, start=st, extent=ext,
                          fill=accent_mid, outline="", style="pieslice")
        cv.create_rectangle(cx0 + clip_r, cy0, cx1 - clip_r, cy1,
                            fill=accent_mid, outline="")
        cv.create_rectangle(cx0, cy0 + clip_r, cx1, cy1 - clip_r,
                            fill=accent_mid, outline="")

        close_r = 16
        close_cx, close_cy = W - PAD - close_r - 4, PAD + close_r + 2
        cv.create_oval(close_cx - close_r - 3, close_cy - close_r - 3,
                       close_cx + close_r + 3, close_cy + close_r + 3,
                       fill=_blend(DARK["surface3"], DARK["bg"], 0.3), outline="", tags="closebox_halo")
        cv.create_oval(close_cx - close_r, close_cy - close_r,
                       close_cx + close_r, close_cy + close_r,
                       fill=DARK["surface2"], outline="", tags="closebox")
        cv.create_text(close_cx, close_cy, text="✕", fill=DARK["muted"],
                       font=("Segoe UI", 13, "bold"), tags="closebtn")

        icon_size = 52
        icon_cx, icon_cy = PAD + 42, BANNER_Y + BANNER_H // 2
        cv.create_oval(icon_cx - icon_size // 2 - 4, icon_cy - icon_size // 2 - 4,
                       icon_cx + icon_size // 2 + 4, icon_cy + icon_size // 2 + 4,
                       fill=_blend(accent_dark, DARK["bg"], 0.35), outline="")
        cv.create_oval(icon_cx - icon_size // 2, icon_cy - icon_size // 2,
                       icon_cx + icon_size // 2, icon_cy + icon_size // 2,
                       fill=accent_dark, outline="")
        cv.create_oval(icon_cx - icon_size // 2 + 6, icon_cy - icon_size // 2 + 6,
                       icon_cx + icon_size // 2 - 6, icon_cy + icon_size // 2 - 6,
                       fill=DARK["card"], outline="")
        cv.create_text(icon_cx, icon_cy, text=icon, fill=accent_light,
                       font=("Segoe UI", 24, "bold"))

        title_x = PAD + 72
        title_y = BANNER_Y + 16
        cv.create_text(title_x, title_y, text=title, fill=DARK["fg"],
                       font=("Segoe UI", 15, "bold"), anchor="nw", width=W - title_x - 44)

        msg_y = banner_bot + 20
        wrapped = message if len(message) < 220 else message[:217] + "…"
        cv.create_text(PAD + 24, msg_y, text=wrapped, fill=DARK["muted"],
                       font=("Segoe UI", 10), anchor="nw", width=W - PAD * 2 - 48,
                       justify="left")

        sep_y = H - 76
        for dy in range(4):
            alpha_t = 1.0 - dy * 0.25
            sep_col = _blend(DARK["surface3"], DARK["bg"], alpha_t)
            cv.create_line(PAD + 14, sep_y + dy, W - PAD - 14, sep_y + dy,
                           fill=sep_col, width=1)

        out = {"v": None if kind == "yesno" else True}

        def _on_close(e=None):
            out.update(v=None if kind == "yesno" else True)
            top.destroy()

        def _mk_round_btn(bx, by, bw, bh, text, bg, fg, hover_bg, action, tag_prefix="btn"):
            r = bh // 2
            tag_fill = f"{tag_prefix}_fill"
            tag_txt = f"{tag_prefix}_txt"
            tag_halo = f"{tag_prefix}_halo"

            cv.create_oval(bx - 3, by - 3, bx + bw + 3, by + bh + 3,
                           fill=_blend(bg, DARK["bg"], 0.45), outline="", tags=tag_halo)
            _round_rect(cv, bx, by, bx + bw, by + bh, r, fill=bg, outline=bg, tags=tag_fill)
            _round_rect(cv, bx + 2, by + 2, bx + bw - 2, by + bh // 2, r - 2,
                        fill=_blend(bg, "#ffffff", 0.08), outline="")
            cx, cy = bx + bw // 2, by + bh // 2
            cv.create_text(cx, cy, text=text, fill=fg,
                           font=("Segoe UI", 11, "bold"), tags=tag_txt)

            def _enter(e):
                _round_rect(cv, bx, by, bx + bw, by + bh, r, fill=hover_bg,
                            outline=hover_bg, tags=tag_fill)
                _round_rect(cv, bx + 2, by + 2, bx + bw - 2, by + bh // 2, r - 2,
                            fill=_blend(hover_bg, "#ffffff", 0.1), outline="",
                            tags=tag_fill)
                cv.itemconfigure(tag_halo, fill=_blend(hover_bg, DARK["bg"], 0.25))

            def _leave(e):
                _round_rect(cv, bx, by, bx + bw, by + bh, r, fill=bg,
                            outline=bg, tags=tag_fill)
                _round_rect(cv, bx + 2, by + 2, bx + bw - 2, by + bh // 2, r - 2,
                            fill=_blend(bg, "#ffffff", 0.08), outline="",
                            tags=tag_fill)
                cv.itemconfigure(tag_halo, fill=_blend(bg, DARK["bg"], 0.45))

            for tag in (tag_fill, tag_txt):
                cv.tag_bind(tag, "<Button-1>", action)
                cv.tag_bind(tag, "<Enter>", _enter)
                cv.tag_bind(tag, "<Leave>", _leave)

        btn_w, btn_h = 110, 40
        gap = 18
        btn_y = H - 60

        if kind == "yesno":
            cancel_x = W - PAD - btn_w
            _mk_round_btn(cancel_x, btn_y, btn_w, btn_h, "Cancel",
                          DARK["surface"], DARK["fg"], DARK["surface2"],
                          lambda e: (out.update(v=False), top.destroy()),
                          tag_prefix="btn_cancel")
            apply_x = cancel_x - gap - btn_w
            _mk_round_btn(apply_x, btn_y, btn_w, btn_h, "Apply  ✓",
                          accent_col, "#12121a", accent_light,
                          lambda e: (out.update(v=True), top.destroy()),
                          tag_prefix="btn_apply")
        else:
            ok_x = W - PAD - btn_w
            _mk_round_btn(ok_x, btn_y, btn_w, btn_h, "OK",
                          accent_col, "#12121a", accent_light,
                          lambda e: (out.update(v=True), top.destroy()),
                          tag_prefix="btn_ok")

        def _fade_in(step=0):
            if step <= 24:
                a = step * (100 / 24)
                try:
                    top.attributes("-alpha", min(a / 100, 1.0))
                except tk.TclError:
                    pass
                top.after(10, _fade_in, step + 1)
        _fade_in()

        def _close_hover(e=None):
            cv.itemconfigure("closebtn", fill=DARK["danger"])
            cv.itemconfigure("closebox", fill="#3a2030")
            cv.itemconfigure("closebox_halo", fill="#2a1520")

        def _close_leave(e=None):
            cv.itemconfigure("closebtn", fill=DARK["muted"])
            cv.itemconfigure("closebox", fill=DARK["surface2"])
            cv.itemconfigure("closebox_halo", fill=_blend(DARK["surface3"], DARK["bg"], 0.3))

        for tag in ("closebtn", "closebox", "closebox_halo"):
            cv.tag_bind(tag, "<Button-1>", _on_close)
            cv.tag_bind(tag, "<Enter>", _close_hover)
            cv.tag_bind(tag, "<Leave>", _close_leave)

        top.update_idletasks()
        x = parent.winfo_rootx() + parent.winfo_width() // 2 - W // 2
        y = parent.winfo_rooty() + parent.winfo_height() // 2 - H // 2
        top.geometry(f"+{x}+{y}")
        top.deiconify()
        top.grab_set()
        top.protocol("WM_DELETE_WINDOW", _on_close)
        parent.wait_window(top)
        return out["v"]

    class _GUICapture:
        """File-like object that forwards output lines to the GUI widget."""

        def __init__(self, app, backend):
            self.app = app
            self.backend = backend  # original sys.stdout
            self._buf = ""

        def write(self, text: str) -> None:
            if not text:
                return
            text = _sanitize_text(text)
            if self.backend is not None:
                self.backend.write(text)
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self.app.enqueue("output", line + "\n")

        def flush(self) -> None:
            if self._buf:
                line, self._buf = self._buf, ""
                self.app.enqueue("output", line)

    class _CouncilApp:
        def __init__(self, root):
            self.root = root
            self.msg_q: queue.Queue = queue.Queue()
            self.running = False
            self.worker: Optional[threading.Thread] = None
            self._answer_mode = False
            self._answer_has_body = False
            self._answer_buf = ""

            root.title("Council — Multi-LLM Orchestrator")
            root.geometry("1180x960")
            root.minsize(980, 760)
            root.configure(bg=DARK["bg"], highlightthickness=1,
                           highlightbackground=DARK["border"])

            styles = ttk.Style(root)
            try:
                styles.theme_use("clam")
            except tk.TclError:
                pass

            self._colors = dict(DARK)
            self._colors.update({"text": DARK["fg"], "fg_dim": DARK["muted"]})
            self._radius = 14
            self._active_models = []
            self._current_round = 0
            self._debate_total = 0

            styles.configure(".", font=("Segoe UI", 10))
            styles.configure("TFrame", background=self._colors["bg"])
            styles.configure("Card.TFrame", background=self._colors["card"])
            styles.configure("TLabel", background=self._colors["card"],
                             foreground=self._colors["fg"], font=("Segoe UI", 11))
            styles.configure("CardHeader.TLabel", background=self._colors["card"],
                             foreground=self._colors["fg"],
                             font=("Segoe UI", 15, "bold"))
            styles.configure("Info.TLabel", background=self._colors["card"],
                             foreground=self._colors["muted"],
                             font=("Segoe UI", 9))
            styles.configure("Status.TLabel", background=self._colors["card"],
                             foreground=self._colors["teal"],
                             font=("Segoe UI", 9))
            styles.configure("TSpinbox", fieldbackground=self._colors["surface2"],
                             foreground=self._colors["fg"], borderwidth=1,
                             relief="flat", arrowsize=13, padding=6,
                             font=("Segoe UI", 10))
            styles.map("TSpinbox",
                       fieldbackground=[("focus", self._colors["surface3"])],
                       foreground=[("disabled", self._colors["dim"])])
            styles.configure("TEntry", fieldbackground=self._colors["surface2"],
                             foreground=self._colors["fg"], borderwidth=1,
                             padding=7, font=("Segoe UI", 10))
            styles.map("TEntry",
                       fieldbackground=[("focus", self._colors["surface3"])],
                       foreground=[("disabled", self._colors["dim"])])
            styles.configure("Vertical.TScrollbar",
                             background=self._colors["surface2"],
                             troughcolor=self._colors["bg"],
                             borderwidth=0, arrowsize=12, relief="flat")

            self.models: list[str] = get_free_models() or list(DEFAULT_FREE_MODELS)
            palette = (DARK["teal"], DARK["blue"], DARK["violet"],
                       DARK["amber"], DARK["coral"], DARK["cyan"])
            self.model_colors = {
                model: palette[i % len(palette)]
                for i, model in enumerate(self.models)
            }
            log("MODELS", f"discovered {len(self.models)} free model(s) "
                          f"({', '.join(self.models[:3])}{'...' if len(self.models) > 3 else ''})")

            self._build_widgets()
            self.root.after(100, self._poll_queue)

        def _card(self, parent, **kw):
            return Surface(parent, bg=self._colors["card"],
                           accent=self._colors["teal"], **kw)

        def _step_header(self, parent, num: int, title: str, subtitle: str = ""):
            body = getattr(parent, "body", parent)
            row = tk.Frame(body, bg=self._colors["card"])
            row.pack(fill="x", padx=17, pady=(16, 5))
            dot = tk.Canvas(row, width=32, height=32, bg=self._colors["card"],
                            highlightthickness=0, bd=0)
            dot.pack(side="left")
            dot.create_oval(2, 2, 30, 30, fill=self._colors["teal"],
                            outline=self._colors["teal"])
            dot.create_text(16, 16, text=str(num), fill=self._colors["bg"],
                            font=("Segoe UI", 12, "bold"))
            text_box = tk.Frame(row, bg=self._colors["card"])
            text_box.pack(side="left", fill="x", expand=True, padx=(12, 0))
            tk.Label(text_box, text=title, bg=self._colors["card"],
                     fg=self._colors["fg"], font=("Segoe UI", 15, "bold"),
                     anchor="w").pack(anchor="w")
            if subtitle:
                tk.Label(text_box, text=subtitle, bg=self._colors["card"],
                         fg=self._colors["dim"], font=("Segoe UI", 8),
                         anchor="w").pack(anchor="w", pady=(1, 0))
            return row

        def _build_widgets(self) -> None:
            c = self._colors
            self.root.bind("<Control-Return>", lambda _e: self._on_run())

            banner = self._card(self.root)
            banner.pack(fill="x", padx=24, pady=(20, 0))
            header = tk.Frame(banner.body, bg=c["card"], height=82)
            header.pack(fill="x", padx=20, pady=(14, 12))
            header.pack_propagate(False)
            logo = tk.Canvas(header, width=48, height=48, bg=c["card"],
                             highlightthickness=0, bd=0)
            logo.pack(side="left", pady=5)
            logo.create_oval(3, 3, 45, 45, fill=_blend(c["teal"], c["bg"], 0.58),
                             outline=c["teal"], width=2)
            logo.create_line(15, 17, 15, 31, fill=c["teal"], width=3)
            logo.create_line(24, 13, 24, 35, fill=c["teal"], width=3)
            logo.create_line(33, 17, 33, 31, fill=c["teal"], width=3)
            title_box = tk.Frame(header, bg=c["card"])
            title_box.pack(side="left", fill="x", expand=True, padx=(14, 0))
            tk.Label(title_box, text="COUNCIL", bg=c["card"], fg=c["fg"],
                     font=("Segoe UI", 24, "bold"), anchor="w").pack(anchor="w")
            tk.Label(title_box, text="MULTI-LLM ORCHESTRATOR  /  DEBATE · JUDGE · APPLY",
                     bg=c["card"], fg=c["muted"], font=("Segoe UI", 8, "bold"),
                     anchor="w").pack(anchor="w", pady=(1, 0))
            header_state = tk.Frame(header, bg=_blend(c["card"], c["surface"], 0.65),
                                    highlightthickness=1,
                                    highlightbackground=c["border"])
            header_state.pack(side="right", pady=18)
            BreathingDot(header_state, size=10, color=c["teal"],
                         bg=_blend(c["card"], c["surface"], 0.65)).pack(
                             side="left", padx=(9, 2))
            tk.Label(header_state, text="LOCAL WORKSPACE", bg=_blend(c["card"], c["surface"], 0.65),
                     fg=c["muted"], font=("Segoe UI", 8, "bold")).pack(
                         side="left", padx=(0, 10), pady=7)

            outer = tk.Frame(self.root, bg=c["bg"])
            outer.pack(fill="both", expand=True, padx=24, pady=0)
            canvas = tk.Canvas(outer, bg=c["bg"], highlightthickness=0, bd=0)
            self._page_canvas = canvas
            vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
            content = tk.Frame(canvas, bg=c["bg"])
            content_window = canvas.create_window((0, 0), window=content, anchor="nw")
            canvas.configure(yscrollcommand=vsb.set)
            canvas.pack(side="left", fill="both", expand=True)
            vsb.pack(side="right", fill="y")
            canvas.bind("<Configure>",
                        lambda e: canvas.itemconfig(content_window, width=e.width))
            content.bind("<Configure>",
                         lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

            q_frame = self._card(content)
            q_frame.pack(fill="x", pady=(24, 0))
            self._step_header(q_frame, 1, "Your coding question",
                              "Give the council a focused prompt")
            self.q_editor = FocusEditor(q_frame.body, height=106)
            self.q_editor.pack(fill="x", padx=16, pady=(0, 16))
            self.q_text = self.q_editor.text

            setup = self._card(content)
            setup.pack(fill="x", pady=(24, 0))
            self._step_header(setup, 2, "Council setup",
                              "Choose the voices in the room")
            n_models = len(self.models)
            self.model_vars = [tk.BooleanVar(value=True) for _ in self.models]
            self.model_state_labels = {}
            self.model_cards = {}
            self.selection_var = tk.StringVar()
            model_head = tk.Frame(setup.body, bg=c["card"])
            model_head.pack(fill="x", padx=16, pady=(3, 0))
            tk.Label(model_head, text="AVAILABLE MODELS", bg=c["card"],
                     fg=c["dim"], font=("Segoe UI", 8, "bold")).pack(side="left")
            tk.Label(model_head, textvariable=self.selection_var, bg=c["card"],
                     fg=c["teal"], font=("Segoe UI", 8, "bold")).pack(side="right")
            model_box = tk.Frame(setup.body, bg=c["card"])
            model_box.pack(fill="x", padx=12, pady=(5, 8))
            n_cols = min(3, max(1, n_models))
            for col in range(n_cols):
                model_box.columnconfigure(col, weight=1, uniform="models")
            n_rows = (n_models + n_cols - 1) // n_cols
            for col in range(n_cols):
                for row in range(n_rows):
                    i = col * n_rows + row
                    if i >= n_models:
                        continue
                    model = self.models[i]
                    card = ModelCard(model_box, model, self.model_vars[i],
                                     self._sync_count, self.model_colors[model], i)
                    card.grid(row=row, column=col, sticky="ew", padx=4, pady=4)
                    self.model_cards[model] = card
                    self.model_state_labels[model] = card
            self._update_selection_summary()

            controls = tk.Frame(setup.body, bg=c["card"])
            controls.pack(fill="x", padx=16, pady=(3, 0))
            tk.Label(controls, text="MODELS IN PARALLEL", bg=c["card"],
                     fg=c["dim"], font=("Segoe UI", 8, "bold")).pack(side="left")
            self.count_spin = ttk.Spinbox(controls, from_=1, to=n_models, width=4)
            self.count_spin.set(n_models)
            self.count_spin.pack(side="left", padx=(8, 24))
            tk.Label(controls, text="DEBATE ROUNDS", bg=c["card"],
                     fg=c["dim"], font=("Segoe UI", 8, "bold")).pack(side="left")
            self.rounds_spin = ttk.Spinbox(controls, from_=0, to=3, width=4)
            self.rounds_spin.set(1)
            self.rounds_spin.pack(side="left", padx=(8, 0))

            file_row = tk.Frame(setup.body, bg=c["card"])
            file_row.pack(fill="x", padx=16, pady=(13, 0))
            tk.Label(file_row, text="CONTEXT FILE", bg=c["card"], fg=c["dim"],
                     font=("Segoe UI", 8, "bold"), anchor="w").pack(
                         side="left", fill="x", ipady=4)
            self.file_entry = ttk.Entry(file_row)
            self.file_entry.pack(side="left", fill="x", expand=True, padx=(9, 7))
            self.browse_btn = PillButton(
                file_row, text="Browse…", command=self._browse,
                bg=c["surface2"], fg=c["teal"], hover=c["surface3"],
                font=("Segoe UI", 10, "bold"), radius=9, height=35, width=112,
            )
            self.browse_btn.pack(side="left")
            tk.Label(setup.body,
                     text="READ ONLY  ·  file contents enter prompts; Council never writes without your OK.",
                     bg=c["card"], fg=c["dim"], font=("Segoe UI", 8),
                     anchor="w").pack(fill="x", padx=16, pady=(8, 15))

            self._run_text = tk.StringVar(value="▶  Run Council")
            self.run_btn = PillButton(
                content, text="▶  Run Council", command=self._on_run,
                bg=c["teal"], fg=c["bg"], hover="#6af0d6",
                font=("Segoe UI", 12, "bold"), radius=11, height=46, width=400,
            )
            self.run_btn.pack(fill="x", pady=(24, 0))

            out_frame = self._card(content)
            out_frame.pack(fill="both", expand=True, pady=(24, 28))
            self._step_header(out_frame, 3, "Live council activity",
                              "Streaming console  /  model-level trace")
            self.activity = ActivityProgress(out_frame.body)
            self.activity.pack(fill="x", padx=10, pady=(0, 5))
            out_wrap = tk.Frame(out_frame.body, bg=c["bg_deep"],
                                highlightthickness=1,
                                highlightbackground=c["border"], bd=0)
            out_wrap.pack(fill="both", expand=True, padx=16, pady=(0, 16))
            self.out = ChatArea(out_wrap, height=18,
                                model_colors=self.model_colors)
            self.out.pack(fill="both", expand=True, padx=2, pady=2)
            self._bind_page_wheel(content)

            self.status_var = tk.StringVar(
                value="Ready — enter a question and run the council")
            status_bar = self._card(self.root)
            status_bar.pack(fill="x", side="bottom", padx=20, pady=(0, 12))
            status_body = status_bar.body
            self.status_dot = BreathingDot(status_body, size=14,
                                           color=c["teal"], bg=c["card"])
            self.status_dot.pack(side="left", padx=(14, 0), pady=8)
            self.status_label = tk.Label(status_body, textvariable=self.status_var,
                                         bg=c["card"], fg=c["teal"],
                                         font=("Segoe UI", 9, "bold"), anchor="w")
            self.status_label.pack(side="left", fill="x", expand=True,
                                   padx=(8, 12), pady=8)
            tk.Label(status_body, text="v1.0  ·  LIVE CONSOLE", bg=c["card"],
                     fg=c["dim"], font=("Segoe UI", 8, "bold"),
                     anchor="e").pack(side="right", padx=14)

        def _page_wheel(self, event):
            if getattr(event, "num", None) == 4:
                amount = -3
            elif getattr(event, "num", None) == 5:
                amount = 3
            elif getattr(event, "delta", 0):
                amount = -3 if event.delta > 0 else 3
            else:
                return "break"
            canvas = self._page_canvas
            if canvas is not None:
                canvas.yview_scroll(amount, "units")
            return "break"

        def _bind_page_wheel(self, widget):
            if isinstance(widget, (ChatArea, tk.Text)):
                return
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                widget.bind(sequence, self._page_wheel, add="+")
            for child in widget.winfo_children():
                self._bind_page_wheel(child)

        def _update_selection_summary(self) -> None:
            selected = sum(1 for var in self.model_vars if var.get())
            self.selection_var.set(f"{selected} SELECTED")

        def _set_status(self, text: str, state: str = "ready") -> None:
            self.status_var.set(text)
            colors = {
                "ready": self._colors["teal"],
                "running": self._colors["amber"],
                "done": self._colors["success"],
                "error": self._colors["danger"],
            }
            color = colors.get(state, self._colors["teal"])
            self.status_label.configure(fg=color)
            self.status_dot.set_color(color)

        def _sync_count(self) -> None:
            n = sum(1 for v in self.model_vars if v.get())
            if n < 1:
                for v in self.model_vars:
                    if not v.get():
                        v.set(True)
                n = sum(1 for v in self.model_vars if v.get())
            self.count_spin.configure(to=n)
            try:
                cur = int(self.count_spin.get())
                if cur > n:
                    self.count_spin.set(n)
            except ValueError:
                self.count_spin.set(n)
            self._update_selection_summary()

        def _browse(self) -> None:
            f = filedialog.askopenfilename(title="Select file for context (optional)")
            if f:
                self.file_entry.delete(0, "end")
                self.file_entry.insert(0, f)

        def _on_run(self) -> None:
            if self.running:
                return
            question = self.q_text.get("1.0", "end").strip()
            if not question:
                _rounded_popup(self.root, "Council", "Please enter a question first.", kind="warn")
                return
            chosen = [self.models[i] for i, v in enumerate(self.model_vars) if v.get()]
            if not chosen:
                _rounded_popup(self.root, "Council", "Select at least one model.", kind="warn")
                return
            try:
                n_use = int(self.count_spin.get())
                rounds = int(self.rounds_spin.get())
            except ValueError:
                _rounded_popup(self.root, "Council", "Invalid parallel/rounds value.", kind="warn")
                return
            if not 0 <= rounds <= 3:
                _rounded_popup(self.root, "Council", "Debate rounds must be between 0 and 3.", kind="warn")
                return
            n_use = max(1, min(n_use, len(chosen)))
            use_models = chosen[:n_use]
            file_path = self.file_entry.get().strip() or None
            self._active_models = list(use_models)
            self._current_round = 0
            self._debate_total = rounds

            reset_model_states()
            for card in self.model_cards.values():
                card.set_state("")
            self._set_status("Council running — streaming model activity", "running")
            self.root.config(cursor="watch")
            self.running = True
            self.run_btn.set_enabled(False, "COUNCIL RUNNING")
            self._answer_mode = False
            self._answer_has_body = False
            self._answer_buf = ""
            self.activity.reset()
            self.out.begin_session()
            self.out.add_bubble(question, role="user")
            self.activity.set_phase("BRIEF", "Preparing the council")
            self._spawn_worker(question, file_path, use_models, rounds)

        def _update_model_states(self) -> None:
            if not self.running:
                return
            states = get_all_model_states()
            for model, card in self.model_cards.items():
                state = states.get(model, "")
                card.set_state(state)
                self.out.set_model_state(model, state, self._current_round)
            self.root.after(500, self._update_model_states)

        def _spawn_worker(self, question, file_path, use_models, rounds) -> None:
            self.root.after(500, self._update_model_states)
            global ASK_YES_NO, ANSWER_CALLBACK
            ASK_YES_NO = self._ask_yes_no
            ANSWER_CALLBACK = self._on_answer

            def worker() -> None:
                old_stdout = sys.stdout
                capture = _GUICapture(self, old_stdout)
                sys.stdout = capture
                worker_error = None
                try:
                    asyncio.run(
                        run_council(
                            question,
                            file_path,
                            use_models,
                            debate_rounds=rounds,
                        )
                    )
                except Exception as e:  # noqa: BLE001 — surface crashes in the GUI
                    worker_error = str(e)
                    print(f"\n[ERROR] Council crashed: {e}", flush=True)
                finally:
                    capture.flush()
                    sys.stdout = old_stdout
                    self.enqueue("done", worker_error)

            self.worker = threading.Thread(target=worker, daemon=True)
            self.worker.start()

        def _ask_yes_no(self, prompt: str) -> bool:
            """Called from the worker thread; shows a modal dialog on the main thread."""
            resp_q: queue.Queue = queue.Queue()
            self.enqueue("ask", prompt, resp_q)
            return bool(resp_q.get())

        def _on_answer(self, text: str) -> None:
            """Called from the worker thread; delivers the final answer directly."""
            self.enqueue("answer", text)

        # ---------------- thread <-> GUI plumbing ----------------

        def enqueue(self, kind: str, *payload) -> None:
            self.msg_q.put((kind, *payload))

        def _poll_queue(self) -> None:
            while True:
                try:
                    kind, *payload = self.msg_q.get_nowait()
                except queue.Empty:
                    break
                try:
                    if kind == "output":
                        self._append_output(payload[0])
                    elif kind == "answer":
                        self._answer_mode = False
                        self._answer_has_body = False
                        self._answer_buf = ""
                        self.out.add_bubble(payload[0], role="assistant",
                                            model="COUNCIL", phase="RESULT")
                        self.activity.set_phase("JUDGE", "Final answer received")
                    elif kind == "ask":
                        prompt, resp_q = payload
                        ans = False
                        try:
                            ans = bool(_rounded_popup(
                                self.root,
                                "Council — confirm",
                                prompt + "\n\nApply this change?",
                                kind="yesno",
                            ))
                        finally:
                            resp_q.put(ans)
                        self._append_output(
                            "\n-- apply: " + ("YES" if ans else "NO") + " --\n"
                        )
                    elif kind == "done":
                        self._on_done(payload[0] if payload else None)
                except Exception as e:  # noqa: BLE001
                    print(f"[ERROR] GUI render error: {e}", file=sys.stderr, flush=True)
            self.root.after(100, self._poll_queue)

        def _model_for_log(self, tag: str, message: str) -> Optional[str]:
            tag = tag.upper()
            if tag.startswith("PROPOSAL-"):
                match = re.search(r"(\d+)$", tag)
                if match:
                    index = int(match.group(1))
                    models = self._active_models or self.models
                    if 0 <= index < len(models):
                        return models[index]
            if tag.startswith("DEBATE-R"):
                match = re.search(r"-(\d+)$", tag)
                if match:
                    index = int(match.group(1))
                    if 0 <= index < len(self._active_models):
                        return self._active_models[index]
            for model in self.models:
                if model in message:
                    return model
            match = re.search(r"(?:calling|responded|failed|from)\s+([^\s(]+)", message)
            if match:
                candidate = match.group(1).rstrip(".,")
                if candidate in self.models:
                    return candidate
            return None

        def _render_pipeline_log(self, tag: str, message: str) -> None:
            tag_upper = tag.upper()
            message = message or tag
            model = self._model_for_log(tag_upper, message)
            self.activity.handle_log(tag_upper, message)
            low = message.lower()
            if "contacting" in low or "calling " in low:
                if model:
                    self.model_cards[model].set_state(MODEL_STATE_TOUCHING)
                    self.out.set_model_state(model, MODEL_STATE_TOUCHING,
                                             self._current_round)
            elif "reached" in low or "responded" in low:
                if model:
                    self.model_cards[model].set_state(MODEL_STATE_REACHED)
                    self.out.set_model_state(model, MODEL_STATE_REACHED,
                                             self._current_round)
            elif "failed" in low or "blacklisted" in low:
                if model:
                    self.model_cards[model].set_state(MODEL_STATE_FAILED)
                    self.out.set_model_state(model, MODEL_STATE_FAILED,
                                             self._current_round)
            if tag_upper.startswith("DEBATE") or tag_upper == "PHASE3":
                round_match = re.search(r"round\s+(\d+)", message, re.I)
                total_match = re.search(r"(\d+)\s+debate round", message, re.I)
                if total_match:
                    self._debate_total = int(total_match.group(1))
                if round_match:
                    self._current_round = int(round_match.group(1))
                    self.activity.set_round(self._current_round, self._debate_total)
                    self.out.set_round(self._current_round)
            role = "system"
            if "failed" in low or "error" in low or "[error" in low:
                role = "error"
            elif "responded" in low or "proposal" in low or "judge output" in low:
                role = "assistant"
            self.out.add_bubble(message, role=role, model=model, phase=tag_upper)

        def _append_output(self, text: str) -> None:
            t = text.strip()
            if not t:
                return
            if self._answer_mode:
                if t.startswith("=") or t.startswith("─"):
                    if not self._answer_has_body:
                        self._answer_buf = ""
                        return
                    body = self._answer_buf.strip()
                    if body and ANSWER_CALLBACK is None:
                        self.out.add_bubble(body, role="assistant",
                                            model="COUNCIL", phase="RESULT")
                        self._answer_buf = ""
                    elif not body:
                        self._answer_buf = ""
                    self._answer_mode = False
                    self._answer_has_body = False
                    return
                self._answer_buf += text + "\n"
                self._answer_has_body = True
                return
            if len(t) > 1 and set(t) <= set("=─═·"):
                return
            if ("Council answer:" in t or "Proposed diff:" in t
                    or "NEEDS_HUMAN_REVIEW" in t):
                self._answer_mode = True
                self._answer_buf = t + "\n"
                return
            match = re.match(r"^\[([^\]]+)\]\s*(.*)$", t)
            if match:
                self._render_pipeline_log(match.group(1), match.group(2))
                return
            if t.startswith("🏛") or t.startswith("[SESSION"):
                self.out.add_bubble(t, role="system", phase="SESSION")
            elif "[ERROR]" in t:
                self.out.add_bubble(t, role="error", phase="ERROR")
            elif t.startswith("-- apply:"):
                self.out.add_bubble(t, role="system", phase="APPLY")
            elif t.startswith(("Request:", "File:")):
                self.out.add_bubble(t, role="system", phase="REQUEST")
            else:
                self.out.add_bubble(t, role="assistant", model="COUNCIL",
                                    phase="STREAM")

        def _on_done(self, error: Optional[str] = None) -> None:
            self.running = False
            self.root.config(cursor="arrow")
            self.run_btn.set_enabled(True, "▶  Run Council")
            self.activity.complete()
            if error:
                self._set_status("Council failed — review the console", "error")
            else:
                self._set_status("Session complete — ready for the next question", "done")
            states = get_all_model_states()
            for model, card in self.model_cards.items():
                state = states.get(model, "")
                card.set_state(state)
                self.out.set_model_state(model, state, self._current_round)
            if self._answer_buf.strip():
                body = self._answer_buf.strip()
                self._answer_buf = ""
                self._answer_mode = False
                self._answer_has_body = False
                self.out.add_bubble(body, role="assistant",
                                    model="COUNCIL", phase="RESULT")

    root = tk.Tk()
    app = _CouncilApp(root)
    root.mainloop()


# ---------------------------------------------------------------------------
# Interactive mode (terminal fallback if the GUI can't open)
# ---------------------------------------------------------------------------


def _parse_indices(raw: str, max_idx: int) -> list[int]:
    """
    Parse a user selection string like "1,3", "1-3", "all", or "1 2 4"
    into a list of 0-based indices. Returns [] on invalid input.
    """
    raw = raw.strip().lower()
    if raw in ("all", "*"):
        return list(range(max_idx))

    idxs: list[int] = []
    tokens = re.split(r"[,;\s]+", raw)
    for tok in tokens:
        if not tok:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            if 1 <= a and b <= max_idx:
                idxs.extend(range(a - 1, b))
            continue
        if tok.isdigit():
            n = int(tok)
            if 1 <= n <= max_idx:
                idxs.append(n - 1)
    # Dedupe, preserve order
    seen: set[int] = set()
    out = []
    for i in idxs:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _prompt_int(prompt: str, min_val: int, max_val: int, default: int) -> int:
    """Ask for an integer within [min_val, max_val], fallback to default."""
    while True:
        raw = input(prompt).strip()
        if raw == "":
            return default
        try:
            v = int(raw)
        except ValueError:
            print(f"  Please enter a number between {min_val} and {max_val}.")
            continue
        if min_val <= v <= max_val:
            return v
        print(f"  Please enter a number between {min_val} and {max_val}.")


def interactive_mode() -> None:
    """Interactive setup: choose models, count, then enter your question."""
    print("\n" + "=" * 60)
    print("🏛  COUNCIL — Interactive Mode")
    print("=" * 60)
    print("Ask a coding question; the council will debate and answer it.\n")

    # 1. Detect available free models
    models = get_free_models()
    print("Available free models:")
    for i, m in enumerate(models, start=1):
        print(f"  [{i}] {m}")
    print("  all — select every model")

    selected: list[int] = []
    while not selected:
        sel = input(
            "\nSelect models to use (e.g. '1', '1,3', '1-3', 'all'): "
        ).strip()
        selected = _parse_indices(sel, len(models))
        if not selected:
            print("  Sorry, that selection wasn't valid — try again.")
    chosen_models = [models[i] for i in selected]
    print(f"  → Selected: {', '.join(chosen_models)}")

    # 2. Choose how many of them actually deliberate
    n_use = _prompt_int(
        f"\nHow many should run in parallel? [1-{len(chosen_models)}] (default: all): ",
        1,
        len(chosen_models),
        len(chosen_models),
    )
    use_models = chosen_models[:n_use]
    print(f"  → Using {n_use} model(s): {', '.join(use_models)}")

    # 3. Optional file for context
    print("\nOptional: a file to give the council as context (NOT required —")
    print("just press Enter to skip). This will be read only, and the models")
    print("will only ever see its contents in the prompt.")
    file_path = input("File path (Enter to skip): ").strip() or None

    # 4. The question itself
    print()
    question = input("Your coding question:\n> ").strip()
    while not question:
        print("  A question is required.")
        question = input("Your coding question:\n> ").strip()

    # 5. Debated rounds (configurable)
    rounds = _prompt_int(
        "\nDebate rounds (when the models disagree)? [0-3] (default: 1): ",
        0,
        3,
        1,
    )

    print()
    print("Launching council...")
    print()

    asyncio.run(run_council(question, file_path, use_models, debate_rounds=rounds))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="council",
        description="Multi-LLM orchestrator for collaborative coding assistance",
    )
    parser.add_argument(
        "request",
        nargs="?",
        help="The coding question or request to discuss (omit to launch interactive mode)",
    )
    parser.add_argument(
        "--file",
        "-f",
        dest="file_path",
        default=None,
        help="Path to a file for context (read-only — council never writes without asking)",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        default=False,
        help="Launch the GUI window (Tkinter)",
    )
    args = parser.parse_args()

    if args.gui or not args.request:
        try:
            launch_gui()
        except Exception as e:
            print(
                f"\n[WARN] Could not open the GUI window ({e}).\n"
                "Falling back to the terminal interface.\n"
            )
            interactive_mode()
        sys.exit(0)

    asyncio.run(run_council(args.request, args.file_path))


if __name__ == "__main__":
    main()
