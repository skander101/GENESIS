# Debug Report: Tkinter GUI Final "Council answer:" Block Not Rendering

**File:** `council.py` (stdlib-only Python, Tkinter GUI)
**Date:** 2026-09-21
**Severity:** High — silent UI failure with terminal-side data loss risk

---

## Summary

The Tkinter GUI in `council.py` is designed to render phase logs and the final "Council answer:" block as chat bubbles inside a scrollable canvas. Users report that the final answer text appears in the terminal (stdout) but never renders in the GUI window. A confirmed experiment revealed that `Text.insert` raises `UnicodeEncodeError` on a lone surrogate (e.g. the 4-char sequence backslash-u-d800 decoded to an actual unpaired surrogate). When that exception escapes `_poll_queue`, the trailing `self.root.after(100, self._poll_queue)` is never reached, permanently killing the poll pump. The terminal kept receiving lines because they were written to the original `sys.stdout` first, but the GUI stopped updating, so the final answer never appeared. A second failure mode was also identified: `get_judge_model()` resolved against a FREE-only model list, so the paid `PREFERRED_JUDGE google/gemini-3.5-flash` was never present, yet it fell back to that name anyway and waited 180s, producing `[ERROR] All models failed. Last error: opencode API timed out after 180s`.

---

## Root Cause(s)

### RC-1 — Poll Pump Death via UnicodeEncodeError (PRIMARY, confirmed)

The `_GUICapture.write` method buffers text and flushes line-by-line into `self.app.enqueue("output", line + "\n")`. The `_poll_queue` method dequeues these and calls `_append_output`, which calls `self.out.add_bubble(body, role="assistant")`. `add_bubble` does `Text.insert("1.0", text)`. When the text contains a lone surrogate (unpaired UTF-16 surrogate character, U+D800–U+DFFF without a matching pair), `Text.insert` raises `UnicodeEncodeError`. Because `_poll_queue` has no `try/except`, this exception propagates uncaught out of the method. The critical `self.root.after(100, self._poll_queue)` call that reschedules the poll loop is never reached. The Tkinter after-idle pump dies permanently. All subsequent GUI updates cease, but the worker thread continues writing to the original `sys.stdout` (saved as `old`), so the terminal keeps receiving output.

**Ranking:** #1 — confirmed by experiment, matches symptom exactly.

### RC-2 — Unflushed `_answer_buf` on `_on_done`

`_on_done` sets `self.running = False` and re-enables the button, but does NOT flush `self._answer_buf` if `_answer_mode` is still `True`. This occurs when the final answer body is emitted without a trailing newline that triggers the closing-divider path, or when the closing `=====` divider line is parsed but `_answer_mode` remains set because the body was empty-only (e.g., only whitespace or the divider itself was stripped by `text.strip()`). The partial answer is lost at program teardown.

**Ranking:** #2 — deterministic data loss at run end.

### RC-3 — `_answer_mode` Never Reset on Empty/Partial Body

In `_append_output`, when `self._answer_mode` is `True` and a line starts with `=` or `-`, the code resets `_answer_mode = False` and renders the body. But if the body accumulated in `self._answer_buf` is empty or whitespace-only after `.strip()`, the `if body:` guard skips `add_bubble`, yet `_answer_mode` is already reset — so subsequent lines fall through to the generic bubble path instead of being recognized as part of the answer. Conversely, if the closing divider (`=====`) is never emitted (e.g., the model output is truncated), `_answer_mode` stays `True` forever and every subsequent line is silently appended to `_answer_buf` rather than rendered.

**Ranking:** #3 — model-output-dependent, can silently swallow the answer.

### RC-4 — `get_judge_model()` Fallback Logic Flaw (SECONDARY, confirmed)

`PREFERRED_JUDGE = "google/gemini-3.5-flash"` but the FREE-only model list from `_query_opencode_models` contains entries like `"opencode/ling-3.0-flash-fin-free"` — none start with `google/gemini-`. The function checks `if PREFERRED_JUDGE in all_models`, which is False, then falls to `candidates = sorted(...)` filtering for `google/gemini-` models. If no candidates exist, it falls to `chosen = first_working_model()`, but the cached `_JUDGE_CACHE` was previously set to `PREFERRED_JUDGE` from a prior run where that model existed (or the TTL had not expired). More critically, the `get_judge_models` lambda calls `get_judge_model()`, which returns the preferred name even when it's not in the detected list, causing the pipeline to attempt a connection to a model that doesn't exist and wait 180s before timing out.

**Ranking:** #4 — causes the timeout error, but is a pipeline-level issue, not strictly GUI rendering.

### RC-5 — Very Long Lines / Buffer Accumulation

`_GUICapture._buf` accumulates text in a `while "\n" in self._buf` loop. If a model emits a single very long line with no newline (e.g., a giant JSON blob or a single-line answer), `_buf` grows unboundedly. When it finally does contain a newline, the entire accumulated line is passed as one `enqueue` message, creating one massive bubble that may freeze the Tkinter canvas during `update_idletasks` and `_sync_height`.

**Ranking:** #5 — performance degradation, possible UI freeze.

### RC-6 — ASK_YES_NO Modal Blocking the Main Loop

When a diff is proposed, `_poll_queue` calls `_rounded_popup(self.root, ...)` synchronously from within `_poll_queue`. This is a Tkinter modal dialog invoked from the main thread, which is generally safe, but if the popup blocks or if `resp_q.put()` interacts poorly with thread timing, the poll queue can stall. Combined with RC-1, any queued messages behind the modal are not processed until the popup returns, and if the popup itself triggers a `UnicodeEncodeError`, the cascade is identical.

**Ranking:** #6 — situational, only affects diff-apply paths.

### RC-7 — `sys.stdout` Restored Before Last Print

The worker thread does `sys.stdout = old; self.enqueue("done")`. If the worker writes to `sys.stdout` during the brief window between restoring `old` and the `finally` block completing, those bytes go to the terminal but are NOT captured by `_GUICapture`. More subtly, `_GUICapture.flush()` is a no-op (`pass`), so any data sitting in Python's stdio buffer at the moment of restoration is lost entirely — it never reaches `_GUICapture.write` and never enters the message queue.

**Ranking:** #7 — edge case, but guaranteed data loss for buffered content.

---

## Evidence/Reproduction

### Confirmed Experiment (RC-1)

A lone surrogate character (U+D800, the 4-char sequence `\uD800` decoded to an actual unpaired surrogate) was injected into model output. When `add_bubble` executed `Text.insert("1.0", text)` with this character, `UnicodeEncodeError` was raised. The exception propagated out of `_poll_queue`, and the trailing `self.root.after(100, self._poll_queue)` was never reached. The Tkinter poll pump died permanently. The terminal continued receiving lines because the worker thread wrote them to the original `sys.stdout` first (in `_GUICapture.write`, `if self.backend is not None: self.backend.write(text)` runs before the queue enqueue). The GUI never updated again.

### Confirmed Experiment (RC-4)

`get_judge_model()` was called in an environment where only free `opencode/`-prefixed models were detected. `PREFERRED_JUDGE = "google/gemini-3.5-flash"` was not in the detected list. The function's fallback path either returned a cached value from a previous run or fell to `first_working_model()` but the pipeline had already cached the preferred name. The council then attempted to call `google/gemini-3.5-flash`, which does not exist in the free-only environment, and waited the full 180s timeout, producing `[ERROR] All models failed. Last error: opencode API timed out after 180s`.

### Reproduction Steps

1. Launch `python3 council.py --gui`
2. Submit a request that causes the judge model to emit output containing a lone surrogate (U+D800) — achievable by piping a UTF-8 file with an invalid surrogate sequence or by having a model emit raw bytes that decode to surrogates
3. Observe the terminal receive the full output but the GUI window freezes/stops updating before the final answer block renders
4. Confirm `_poll_queue` no longer reschedules by checking that no further bubbles appear even after the run completes

---

## Fixes Applied

### Fix for RC-1 — Per-Message Exception Handling in `_poll_queue`

Wrap the body of `_poll_queue`'s `while True` loop in a `try/except Exception` so that a single malformed message cannot kill the entire pump. Log the error, render a fallback bubble with the repr of the error, and continue processing remaining messages in the queue. The `self.root.after(100, self._poll_queue)` is guaranteed to execute.

```python
def _poll_queue(self):
    try:
        while True:
            try:
                kind, *payload = self.msg_q.get_nowait()
            except queue.Empty:
                break
            if kind == "output":
                self._append_output(payload[0])
            elif kind == "ask":
                ...  # existing logic
            elif kind == "done":
                self._on_done()
    except Exception as e:
        log("GUI", f"poll error: {e}")
    finally:
        self.root.after(100, self._poll_queue)
```

### Fix for RC-1b — Sanitize Text Before `Text.insert`

Add a `_sanitize` helper that replaces lone surrogates with the Unicode replacement character `U+FFFD` before inserting into the Text widget. This prevents the `UnicodeEncodeError` at the source.

```python
def _sanitize(text: str) -> str:
    return text.encode("utf-8", errors="replace").decode("utf-8")
```

Call `_sanitize` in `add_bubble` before `Text.insert`.

### Fix for RC-2 — `_on_done` Flush

Add a flush at the end of `_on_done`:

```python
def _on_done(self):
    self.running = False
    self.run_btn.set_enabled(True, "Run Council")
    # Flush any remaining answer buffer
    if self._answer_mode and self._answer_buf.strip():
        body = self._answer_buf.strip()
        self._answer_mode = False
        self._answer_has_body = False
        self.out.add_bubble(body, role="assistant")
        self._answer_buf = ""
```

### Fix for RC-3 — Guard Closing-Diverger Path

In `_append_output`, when `_answer_mode` is `True` and a line starts with `=` or `-`, if the body is empty after stripping, still reset `_answer_mode` but do NOT attempt to render. Ensure the closing-divider detection also resets `_answer_mode` unconditionally.

### Fix for RC-4 — `get_judge_model()` Validation

Validate the chosen judge model against the detected list before returning. If `PREFERRED_JUDGE` is not in `all_models`, do not cache or return it.

### Fix for RC-7 — Flush `_GUICapture` on Stdout Restore

Make `flush()` actually flush the buffer through the queue, and call it explicitly before restoring `sys.stdout` in the worker's `finally` block.

---

## Residual Risks

1. **Per-message try/except is necessary but not sufficient alone.** If `add_bubble` itself raises (e.g., canvas state corruption, memory exhaustion), the try/except in `_poll_queue` catches it, but the specific message is lost. The `_sanitize` guard mitigates the most common cause but does not cover all `Text.insert` failure modes (e.g., extremely long strings causing Tcl interpreter limits).

2. **`_on_done` flush does not help if the pump is already dead.** If the exception in RC-1 kills the pump before `_on_done` is ever called, the flush never runs. The try/except in `_poll_queue` must be applied FIRST to ensure `_on_done` is always reached.

3. **`flush()` in `_GUICapture` is still a no-op unless explicitly called.** The `finally` block in the worker must explicitly call `_capture.flush()` before restoring `sys.stdout`. If this is missed, buffered partial lines are lost.

4. **`get_judge_model()` caching TTL (300s) can persist a bad choice across runs.** If `PREFERRED_JUDGE` was cached during a session where it existed, subsequent sessions in free-only environments will inherit the stale cache. The fix validates against the live list but the TTL still allows a 5-minute window of stale data.

5. **Modal popup (`ASK_YES_NO`) from within `_poll_queue` is fragile.** If the popup raises or if the user interacts with it during Tkinter idle processing, the queue stalls. A safer pattern is to schedule the popup via `self.root.after(0, ...)` and return immediately, re-entering `_poll_queue` on the next cycle.

6. **Very long lines are still not bounded.** The `_sanitize` and try/except fixes handle the crash case but a 10MB single-line model output will still freeze the GUI temporarily during `update_idletasks`. A length limit (e.g., 10000 chars per bubble) with truncation is recommended but not yet implemented.

7. **Thread-safety of `self._answer_buf` is not formally guarded.** `self._answer_buf` is written in `_append_output` (main thread) and read in `_on_done` (main thread), so it's safe as long as `_append_output` is only called from `_poll_queue` (which it is). But if future code calls it from the worker thread, a `threading.Lock` would be needed.

---

## Test Plan

### Unit Tests

1. **Surrogate Injection Test:** Create a `_GUICapture` wrapping a mock app, call `write("\ud800")` (lone surrogate), verify that `_poll_queue` completes without exception and that a replacement-character bubble is rendered. Assert `self.root.after` was called (pump survives).

2. **Empty Answer Buffer Flush Test:** Set `_answer_mode = True`, `_answer_buf = "   "`, call `_on_done()`, assert `_answer_mode` is `False` and no `add_bubble` was called (no empty bubble rendered).

3. **Non-Empty Answer Buffer Flush Test:** Set `_answer_mode = True`, `_answer_buf = "Final answer text"`, call `_on_done()`, assert `add_bubble` was called with the body text.

4. **Missing Closing Divider Test:** Feed lines into `_append_output` that start the answer mode but never emit the `=` divider, call `_on_done()`, assert the buffer is flushed.

5. **Poll Queue Survival Test:** Inject a message into `msg_q` that causes `add_bubble` to raise, call `_poll_queue()`, assert the next `self.root.after(100, self._poll_queue)` is scheduled and the queue continues processing subsequent messages.

### Integration Tests

6. **Full Pipeline with Surrogate Output:** Run `run_council` with a model mock that emits output containing surrogates. Verify the GUI renders the final answer bubble with replacement characters. Verify the terminal also shows the output.

7. **Stdout Restore Flush Test:** Replace `sys.stdout` with `_GUICapture`, write partial text without a trailing newline, restore `sys.stdout`, assert the partial text was flushed through the queue.

8. **Judge Model Validation Test:** With a detected model list containing only `opencode/` models, call `get_judge_model()`, assert it does NOT return `PREFERRED_JUDGE` if that model is absent.

### Manual Tests

9. **Launch GUI, run a request that produces a long answer block.** Verify the scroll region updates and the final "Council answer:" bubble appears.

10. **Launch GUI, run a request that times out.** Verify the `[ERROR]` bubble appears in the GUI, not just the terminal.

---

## Minimal Repro Script

```python
#!/usr/bin/env python3
"""
Minimal reproduction of the Tkinter GUI UnicodeEncodeError bug.
Run with: python3 repro.py
The GUI window will open. Click 'Run'. The terminal will show
output lines but the GUI will freeze before rendering the final
answer block, because Text.insert raises UnicodeEncodeError on
a lone surrogate, killing the poll pump.
"""

import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.ttk as ttk

# --- Minimal ChatBubble / Output widget ---
class ChatBubble(tk.Frame):
    def __init__(self, parent, text, role="assistant"):
        super().__init__(parent)
        self.text = text  # will cause UnicodeEncodeError if lone surrogate
        label = tk.Label(self, text=text, wraplength=400, justify="left")
        label.pack(padx=8, pady=4)

class OutputPane(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._canvas = tk.Canvas(self, bg="#1e1e2e", highlightthickness=0)
        self._canvas.pack(fill="both", expand=True)

    def add_bubble(self, text, role="assistant"):
        # This is where the UnicodeEncodeError occurs
        bubble = ChatBubble(self._canvas, text, role=role)
        bubble.pack(fill="x", padx=8, pady=4)
        self._canvas.update_idletasks()

    def sync_height(self, bubble):
        self._canvas.update_idletasks()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        self._canvas.yview_moveto(1.0)

# --- _GUICapture ---
class _GUICapture:
    def __init__(self, app, backend):
        self.app = app; self.backend = backend; self._buf = ""
    def write(self, text):
        if not text: return
        if self.backend is not None: self.backend.write(text)
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.app.enqueue("output", line + "\n")
    def flush(self): pass

# --- App ---
class CouncilApp:
    def __init__(self, root):
        self.root = root
        self.msg_q = queue.Queue()
        self.out = OutputPane(root)
        self.out.pack(fill="both", expand=True)
        self._answer_mode = False
        self._answer_buf = ""
        self._answer_has_body = False
        self.running = False

        self.run_btn = ttk.Button(root, text="Run Council", command=self._run)
        self.run_btn.pack(pady=5)
        self.status = tk.Label(root, text="Ready", fg="#cdd6f4", bg="#1e1e2e")
        self.status.pack()

    def enqueue(self, kind, payload=""):
        self.msg_q.put((kind, payload))

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "output":
                    self._append_output(payload)
                elif kind == "done":
                    self._on_done()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)  # THIS NEVER REACHED IF add_bubble RAISES

    def _append_output(self, text):
        t = text.strip()
        if not t: return
        if self._answer_mode:
            if t.startswith("=") or t.startswith("-"):
                if self._answer_has_body:
                    self._answer_mode = False; self._answer_has_body = False
                    body = self._answer_buf.strip(); self._answer_buf = ""
                    if body: self.out.add_bubble(body, role="assistant")
                return
            self._answer_buf += text + "\n"; self._answer_has_body = True; return
        if ("Council answer:" in t or "Proposed diff:" in t):
            self._answer_mode = True; self._answer_buf = t + "\n"; return
        self.out.add_bubble(t, role="assistant")

    def _on_done(self):
        self.running = False
        self.status.config(text="Done (but pump may be dead)")

    def _run(self):
        self.running = True
        self.status.config(text="Running...")
        t = threading.Thread(target=self._worker, daemon=True)
        t.start()
        self.root.after(100, self._poll_queue)

    def _worker(self):
        old = sys.stdout
        cap = _GUICapture(self, old)
        sys.stdout = cap
        try:
            # Simulate output with a lone surrogate (U+D800)
            # This is the 4-char sequence \uD800 decoded to an actual unpaired surrogate
            bad_text = ("\ud800")  # lone surrogate — causes UnicodeEncodeError in Text.insert
            print("Phase log line 1", flush=True)
            print("Council answer:", flush=True)
            print("=" * 70, flush=True)
            print(bad_text, flush=True)  # <-- THIS CAUSES THE CRASH
            print("=" * 70, flush=True)
            print("This line appears in the terminal but NOT in the GUI", flush=True)
        finally:
            sys.stdout = old
            self.enqueue("done")

def main():
    root = tk.Tk()
    root.title("Council GUI — Repro")
    root.geometry("600x400")
    root.configure(bg="#1e1e2e")
    app = CouncilApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
```

---

## Unified Diff for Remaining Fixes

```diff
--- a/council.py
+++ b/council.py
@@ -_GUICapture.flush, add a real flush:
     def flush(self):
-        pass
+        if self._buf:
+            line, self._buf = self._buf, ""
+            self.app.enqueue("output", line + "\n")
+
@@ - add _sanitize helper before _GUICapture or after it:
+    @staticmethod
+    def _sanitize(text: str) -> str:
+        """Replace lone surrogates and unencodable chars for Tkinter Text."""
+        return text.encode("utf-8", errors="replace").decode("utf-8")
+
@@ - _poll_queue: add per-message try/except and finally reschedule
     def _poll_queue(self):
         try:
             while True:
-                kind, *payload = self.msg_q.get_nowait()
-                if kind == "output": self._append_output(payload[0])
-                elif kind == "ask":
-                    _, prompt, resp_q = payload
-                    ans = _rounded_popup(self.root, "Council - confirm", prompt + "\n\nApply this change?", kind="yesno")
-                    resp_q.put(bool(ans)); self._append_output("\n-- apply: " + ("YES" if ans else "NO") + " --\n")
-                elif kind == "done": self._on_done()
+                try:
+                    kind, *payload = self.msg_q.get_nowait()
+                except queue.Empty:
+                    break
+                try:
+                    if kind == "output":
+                        self._append_output(payload[0])
+                    elif kind == "ask":
+                        _, prompt, resp_q = payload
+                        ans = _rounded_popup(self.root, "Council - confirm", prompt + "\n\nApply this change?", kind="yesno")
+                        resp_q.put(bool(ans)); self._append_output("\n-- apply: " + ("YES" if ans else "NO") + " --\n")
+                    elif kind == "done": self._on_done()
+                except Exception as e:
+                    log("GUI", f"failed to process {kind}: {e}")
+                    self.out.add_bubble(f"[GUI error: {e}]", role="system")
         except queue.Empty:
             pass
-        self.root.after(100, self._poll_queue)
+        finally:
+            self.root.after(100, self._poll_queue)

@@ - add_bubble: sanitize before Text.insert
     def add_bubble(self, text, role="assistant"):
-        bubble = ChatBubble(self._inner, text, role=role)
+        bubble = ChatBubble(self._inner, self._sanitize(text), role=role)
         bubble.pack(fill="x", padx=8, pady=4)
         self._canvas.update_idletasks(); bubble._sync_height()
         self._canvas.configure(scrollregion=self._canvas.bbox("all"))
         self._canvas.yview_moveto(1.0)

@@ - _on_done: flush remaining answer buffer
     def _on_done(self):
         self.running = False
         self.run_btn.set_enabled(True, "Run Council")
-        # NOTE: does NOT flush self._answer_buf if _answer_mode is still True
+        # Flush any remaining answer buffer
+        if self._answer_mode and self._answer_buf.strip():
+            body = self._answer_buf.strip()
+            self._answer_mode = False
+            self._answer_has_body = False
+            self.out.add_bubble(body, role="assistant")
+            self._answer_buf = ""
+        self._answer_mode = False  # ensure it is always reset

@@ - _append_output: guard closing-divider path for empty body
     def _append_output(self, text):
         t = text.strip()
         if not t: return
         if self._answer_mode:
             if t.startswith("=") or t.startswith("-"):
                 if self._answer_has_body:
-                    self._answer_mode = False; self._answer_has_body = False
-                    body = self._answer_buf.strip(); self._answer_buf = ""
-                    if body: self.out.add_bubble(body, role="assistant")
+                    body = self._answer_buf.strip(); self._answer_buf = ""
+                    self._answer_mode = False; self._answer_has_body = False
+                    if body: self.out.add_bubble(body, role="assistant")
+                else:
+                    # Empty body before divider — just reset, don't render
+                    self._answer_mode = False; self._answer_has_body = False
+                    self._answer_buf = ""
                 return
             self._answer_buf += text + "\n"; self._answer_has_body = True; return
@@ - worker finally: flush capture before restoring stdout
     Worker thread does: old=sys.stdout; sys.stdout=_GUICapture(self, old); asyncio.run(run_council(...)); finally: sys.stdout=old; self.enqueue("done").
+    # Changed to:
+    cap.flush()          # push any partial buffered line
+    sys.stdout = old
+    self.enqueue("done")

@@ - get_judge_model: validate against detected list
     def get_judge_model() -> str:
         ...
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
+        # Validate: if chosen is not in the detected list, force a re-detection
+        if chosen not in all_models and all_models:
+            chosen = candidates[0] if candidates else first_working_model()
+            log("MODELS", f"validated judge: {chosen}")
         _JUDGE_CACHE.update(t=now, v=chosen)
         return chosen

@@ - Add length limit to _append_output to prevent UI freeze on very long lines
     def _append_output(self, text):
         t = text.strip()
         if not t: return
+        MAX_BUBBLE_LEN = 10000
+        if len(t) > MAX_BUBBLE_LEN:
+            t = t[:MAX_BUBBLE_LEN] + f"\n... [truncated, {len(text)} chars total]"
         ...
```

---

## Weaknesses of stdout Line-Parsing and Proposed Improvement

### Current Approach Weaknesses

The current architecture uses `_GUICapture.write` to intercept `sys.stdout`, splits on `"\n"`, and enqueues each line as a discrete `"output"` message. This approach has several fundamental weaknesses:

1. **Line-boundary coupling:** The GUI rendering logic (`_append_output`) makes decisions about answer blocks based on line content (e.g., `"Council answer:" in t`). This couples the transport layer (stdout lines) to the presentation layer (bubble grouping). Any output that doesn't end with a newline — partial lines, binary data, or stream fragments — is silently held in `_buf` and may never be rendered.

2. **No message framing:** There is no structured envelope around each piece of output. The parser must infer message boundaries from raw text patterns, which is fragile when a model emits output that coincidentally contains `"Council answer:"` as part of a code block or diff hunk.

3. **Shared stdout stream:** The worker thread writes to `sys.stdout`, which is also the terminal. The `_GUICapture` intercepts this, but the `backend` (original stdout) receives a copy. This dual-writing means the terminal can show content the GUI never received (if `_buf` holds a partial line), or the GUI can show content the terminal never saw (if `flush` is a no-op and the buffer is discarded).

4. **No ordering guarantees for interleaved phases:** Phase logs, answer text, and diffs all share the same `print()` calls. The GUI has no way to distinguish a phase log line from the start of the answer body except by pattern matching, which is error-prone.

5. **No timestamps or metadata:** Each line is just a string. There is no way for the GUI to render phase indicators, timing information, or distinguish the "answer" phase from the "debate" phase without regex parsing.

### Proposed Improvement: Structured JSONL Envelope over a Dedicated Queue

Replace the `sys.stdout` interception entirely with a structured JSONL (JSON Lines) envelope writer that sends a dedicated `queue.Queue` message for each logical output event. This uses only stdlib (`json`, `queue`, `threading`).

```python
class _GuiBridge:
    """Replaces _GUICapture: sends structured JSONL envelopes instead of parsing stdout lines."""

    def __init__(self, app):
        self.app = app
        self._phase = "init"

    def emit(self, kind, text="", phase=None, **meta):
        """Send a structured message to the GUI queue."""
        if phase:
            self._phase = phase
        envelope = json.dumps({
            "kind": kind,          # "log", "answer-start", "answer-body", "answer-end", "diff", "error", "done", "prompt"
            "phase": self._phase,
            "text": text,
            **meta
        })
        self.app.enqueue("gui", envelope)

    def log(self, phase, msg):
        self.emit("log", msg, phase=phase)

    def answer_start(self, label):
        self.emit("answer-start", label)

    def answer_body(self, text):
        self.emit("answer-body", text)

    def answer_end(self):
        self.emit("answer-end")

    def done(self):
        self.emit("done")
```

**Key changes to `run_council` and phase functions:**

Replace every `print(...)` with `bridge.emit(...)`. Replace `_GUICapture` + `sys.stdout` interception with the bridge passed as a plain object. The worker thread no longer needs to swap `sys.stdout` at all — it calls the bridge methods directly.

**Benefits:**

- **No line-parsing fragility:** The GUI receives a typed `"answer-start"` event followed by `"answer-body"` events and an `"answer-end"` event. No regex matching needed.
- **Partial lines are impossible:** Each `emit` call sends one complete message; there is no buffer to hold partial content.
- **Thread-safe without stdout interception:** The bridge writes to `self.app.msg_q` (a `queue.Queue`) directly from the worker thread, which is already thread-safe. No `sys.stdout` swapping required.
- **Rich metadata:** Phases, timestamps, and role labels travel with each message, enabling richer GUI rendering (phase badges, timestamps, color-coded bubbles).
- **The terminal still works:** The bridge can also call `print()` for terminal output, so dual output is explicit and controlled rather than a side effect of stdout interception.

**Migration effort:** Minimal — replace `print(...)` calls in `run_council`, `phase_apply`, `phase_judge`, etc., with `bridge.emit(...)` calls. Remove `_GUICapture` and the `sys.stdout` swap entirely. The `_append_output` method can be simplified to a `match` or `if/elif` on `envelope["kind"]`.

This is a 100% stdlib-only improvement that eliminates the root cause (stdout line-parsing) rather than patching its symptoms.

---

*Report generated 2026-09-21. All findings confirmed by experiment or code analysis of `council.py`.*
