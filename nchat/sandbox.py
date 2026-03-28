"""Sandbox process management — code medium subprocess with RPC gate injection.

The sandbox runs entity code in a persistent Python subprocess. Gate functions
(read_file, terminal, etc.) are injected as host functions that communicate
via JSON-RPC over stdin/stdout.

Architecture:
  - Host spawns a long-lived Python subprocess
  - Gate stubs are registered at startup (typed Python functions)
  - Code is sent per-turn via stdin JSON messages
  - Gate calls from entity code travel back via stdout JSON
  - Host dispatches gate calls through existing tool handlers
  - Results flow back to the sandbox via stdin
  - print() output from entity code is captured in a StringIO buffer

Protocol (per turn):
  Host  → Child: {"type": "execute", "code": "..."}
  Child → Host:  {"type": "gate_call", "id": "0", "name": "read_file", "args": {...}}
  Host  → Child: {"type": "gate_result", "id": "0", "result": "..."}
  ...   (repeat for each gate call)
  Child → Host:  {"type": "result", "output": "...", "error": null, "done": false, ...}

Cross-platform: uses stdin/stdout pipes (no Unix domain sockets).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from nchat.loom import GateCallRecord

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"


# ── The sandbox child script ──────────────────────────────────────────
# Self-contained Python script that runs in the subprocess.
# No imports from nchat or hermes. Communicates via stdin/stdout JSON.

SANDBOX_CHILD_SCRIPT = r'''#!/usr/bin/env python3
"""Necronomichat code medium sandbox — child process."""
import sys, os, json, io, traceback, importlib

# ── RPC setup ──
# Save original stdout for RPC output; redirect print() to capture buffer.
_rpc_out = sys.stdout
_capture = io.StringIO()
sys.stdout = _capture
# stdin is RPC input from host.
_rpc_in = sys.stdin

def _send(msg):
    _rpc_out.write(json.dumps(msg, default=str) + "\n")
    _rpc_out.flush()

def _recv():
    line = _rpc_in.readline()
    if not line:
        sys.exit(0)
    return json.loads(line.strip())

# ── Gate call tracking ──
_gate_call_log = []

def _gate_call(name, args):
    """Call a host gate function. Blocks until result arrives."""
    filtered = {k: v for k, v in args.items() if v is not None}
    call_id = str(len(_gate_call_log))
    _send({"type": "gate_call", "id": call_id, "name": name, "args": filtered})
    resp = _recv()
    if resp.get("type") == "gate_error":
        error_msg = resp.get("error", "Unknown gate error")
        _gate_call_log.append({"name": name, "args": str(filtered)[:80], "error": True})
        raise RuntimeError(f"Gate '{name}' error: {error_msg}")
    _gate_call_log.append({"name": name, "args": str(filtered)[:80], "error": False})
    return resp.get("result", "")

# ── Convenience helpers ──
def json_parse(text):
    """Parse JSON tolerant of control characters (strict=False)."""
    return json.loads(text, strict=False)

# ── submit_answer ──
_done = False
_answer = ""

def submit_answer(result):
    """Signal completion with a result for the user."""
    global _done, _answer
    _done = True
    _answer = str(result)

# ── Persistent namespace ──
_ns = {
    "__builtins__": __builtins__,
    "_gate_call": _gate_call,
    "json_parse": json_parse,
    "submit_answer": submit_answer,
    "json": json,
    "os": os,
    "sys": sys,
    "io": io,
    "importlib": importlib,
}

# ── Main message loop ──
_send({"type": "started"})

while True:
    try:
        msg = _recv()
    except (json.JSONDecodeError, ValueError):
        continue

    msg_type = msg.get("type", "")

    if msg_type == "register_typed_gates":
        # Host sends Python function code strings — exec them into namespace
        for gate in msg.get("gates", []):
            try:
                exec(gate["code"], _ns)
            except Exception as e:
                pass  # gate registration failure is non-fatal
        _send({"type": "ready"})

    elif msg_type == "register_gates":
        # Generic gates — create **kwargs wrappers
        for gate in msg.get("gates", []):
            name = gate["name"]
            def _make_fn(n):
                def fn(**kwargs):
                    return _gate_call(n, kwargs)
                fn.__name__ = n
                return fn
            _ns[name] = _make_fn(name)
        _send({"type": "ready"})

    elif msg_type == "execute":
        # Reset per-turn state
        _capture.truncate(0)
        _capture.seek(0)
        _gate_call_log.clear()
        _done = False
        _answer = ""
        # Re-inject submit_answer (in case entity overwrote it)
        _ns["submit_answer"] = submit_answer

        error = None
        try:
            exec(msg["code"], _ns)
        except SystemExit:
            pass
        except Exception:
            error = traceback.format_exc()

        output = _capture.getvalue()
        _send({
            "type": "result",
            "output": output,
            "error": error,
            "done": _done,
            "answer": _answer,
            "gate_calls": list(_gate_call_log),
        })

    elif msg_type == "shutdown":
        _send({"type": "shutdown_ack"})
        break
'''


# ── Typed gate stub templates ─────────────────────────────────────────
# Python function definitions for common gates. These allow positional
# arguments (e.g., read_file("path") not read_file(path="path")).

TYPED_GATE_STUBS: Dict[str, str] = {
    "terminal": (
        "def terminal(command, timeout=None, workdir=None):\n"
        "    return _gate_call('terminal', {'command': command, 'timeout': timeout, 'workdir': workdir})\n"
    ),
    "read_file": (
        "def read_file(path, offset=1, limit=500):\n"
        "    return _gate_call('read_file', {'path': path, 'offset': offset, 'limit': limit})\n"
    ),
    "write_file": (
        "def write_file(path, content):\n"
        "    return _gate_call('write_file', {'path': path, 'content': content})\n"
    ),
    "search_files": (
        "def search_files(pattern, target='content', path='.', file_glob=None, limit=50, offset=0):\n"
        "    return _gate_call('search_files', {'pattern': pattern, 'target': target, 'path': path, "
        "'file_glob': file_glob, 'limit': limit, 'offset': offset})\n"
    ),
    "patch": (
        "def patch(path, old_string, new_string, replace_all=False):\n"
        "    return _gate_call('patch', {'path': path, 'old_string': old_string, "
        "'new_string': new_string, 'replace_all': replace_all})\n"
    ),
    "web_search": (
        "def web_search(query, limit=5):\n"
        "    return _gate_call('web_search', {'query': query, 'limit': limit})\n"
    ),
    "web_extract": (
        "def web_extract(urls):\n"
        "    return _gate_call('web_extract', {'urls': urls})\n"
    ),
    "memory": (
        "def memory(action, content=None, target=None):\n"
        "    return _gate_call('memory', {'action': action, 'content': content, 'target': target})\n"
    ),
    "session_search": (
        "def session_search(query, limit=5):\n"
        "    return _gate_call('session_search', {'query': query, 'limit': limit})\n"
    ),
    "skill_view": (
        "def skill_view(name):\n"
        "    return _gate_call('skill_view', {'name': name})\n"
    ),
    "skill_manage": (
        "def skill_manage(action, name=None, content=None):\n"
        "    return _gate_call('skill_manage', {'action': action, 'name': name, 'content': content})\n"
    ),
    "delegate_task": (
        "def delegate_task(goal, context=None, crystal='worker'):\n"
        "    return _gate_call('delegate_task', {'goal': goal, 'context': context, 'crystal': crystal})\n"
    ),
    "todo": (
        "def todo(action, task=None, id=None):\n"
        "    return _gate_call('todo', {'action': action, 'task': task, 'id': id})\n"
    ),
    "clarify": (
        "def clarify(question):\n"
        "    return _gate_call('clarify', {'question': question})\n"
    ),
    "image_generation": (
        "def image_generation(prompt):\n"
        "    return _gate_call('image_generation', {'prompt': prompt})\n"
    ),
    "vision_analyze": (
        "def vision_analyze(image_path, prompt=None):\n"
        "    return _gate_call('vision_analyze', {'image_path': image_path, 'prompt': prompt})\n"
    ),
    "tts": (
        "def tts(text, voice=None):\n"
        "    return _gate_call('tts', {'text': text, 'voice': voice})\n"
    ),
    # 7b composition gates — these are also typed stubs
    "cantrip": (
        "def cantrip(config):\n"
        "    return _gate_call('cantrip', {'config': config})\n"
    ),
    "cast": (
        "def cast(handle, intent):\n"
        "    return _gate_call('cast', {'handle': handle, 'intent': intent})\n"
    ),
    "cast_batch": (
        "def cast_batch(items):\n"
        "    result = _gate_call('cast_batch', {'items': items})\n"
        "    try:\n"
        "        return json.loads(result) if isinstance(result, str) else result\n"
        "    except (json.JSONDecodeError, TypeError):\n"
        "        return result\n"
    ),
    "dispose": (
        "def dispose(handle):\n"
        "    return _gate_call('dispose', {'handle': handle})\n"
    ),
}


def generate_gate_code(name: str) -> str:
    """Generate Python function code for a gate stub.

    Uses typed stub if available, otherwise a generic **kwargs wrapper.
    """
    if name in TYPED_GATE_STUBS:
        return TYPED_GATE_STUBS[name]
    return (
        f"def {name}(**kwargs):\n"
        f"    return _gate_call('{name}', kwargs)\n"
    )


# ── SandboxResult ─────────────────────────────────────────────────────

@dataclass
class SandboxResult:
    """Result of a single code execution in the sandbox."""
    output: str = ""
    error: str | None = None
    gate_calls: List[GateCallRecord] = field(default_factory=list)
    done: bool = False
    answer: str = ""
    duration_ms: int = 0


def _gate_preview(name: str, args: Dict) -> str | None:
    """Build a short preview string for a gate call (for progress display)."""
    if name == "terminal":
        cmd = args.get("command", "")
        return cmd[:80] if cmd else None
    if name in ("read_file", "write_file"):
        return args.get("path", "")[:80]
    if name == "search_files":
        return args.get("pattern", "")[:80]
    if name == "patch":
        return args.get("path", "")[:80]
    if name in ("web_search",):
        return args.get("query", "")[:80]
    if name == "web_extract":
        urls = args.get("urls", [])
        return urls[0][:80] if urls else None
    if name == "memory":
        action = args.get("action", "")
        key = args.get("key", "")
        return f"{action} {key}".strip()[:80] if action else None
    if name in ("cantrip", "cast"):
        crystal = args.get("crystal", "")
        return f"crystal={crystal}" if crystal else None
    return None


# ── PythonSandbox ─────────────────────────────────────────────────────

class PythonSandbox:
    """Persistent Python subprocess sandbox with RPC gate injection.

    The sandbox process stays alive across multiple execute() calls.
    Variables persist in the sandbox namespace between turns.
    Gate functions are injected at startup as typed Python functions.

    Usage:
        sandbox = PythonSandbox(gate_handlers={"read_file": handler_fn, ...})
        sandbox.start()
        result = sandbox.execute("content = read_file('src/main.py')\\nprint(content[:100])")
        result2 = sandbox.execute("print(len(content))")  # 'content' persists
        sandbox.close()
    """

    def __init__(
        self,
        gate_handlers: Dict[str, Callable] | None = None,
        gate_names: List[str] | None = None,
        timeout: int = 300,
        progress_callback: Callable | None = None,
    ):
        self._gate_handlers: Dict[str, Callable] = gate_handlers or {}
        self._gate_names: List[str] = gate_names or list(self._gate_handlers.keys())
        self._timeout = timeout
        self._progress_callback = progress_callback
        self._proc: Optional[subprocess.Popen] = None
        self._script_path: Optional[str] = None
        self._tmpdir: Optional[str] = None
        self._alive = False
        self._lock = threading.Lock()

    def start(self) -> None:
        """Spawn the sandbox subprocess and register gate functions."""
        if self._alive:
            return

        # Write child script to temp file
        self._tmpdir = tempfile.mkdtemp(prefix="nchat_sandbox_")
        self._script_path = os.path.join(self._tmpdir, "sandbox_child.py")
        with open(self._script_path, "w", encoding="utf-8") as f:
            f.write(SANDBOX_CHILD_SCRIPT)

        # Build safe environment (no API keys)
        child_env = self._build_child_env()

        # Spawn subprocess
        # -u: unbuffered stdout/stderr for reliable RPC
        popen_kwargs: Dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": child_env,
            "bufsize": 0,  # unbuffered
        }
        if _IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["preexec_fn"] = os.setsid

        self._proc = subprocess.Popen(
            [sys.executable, "-u", self._script_path],
            **popen_kwargs,
        )

        # Wait for "started" message
        msg = self._recv(timeout=10)
        if not msg or msg.get("type") != "started":
            self.close()
            raise RuntimeError(f"Sandbox failed to start: {msg}")

        # Register gate functions
        typed_gates = []
        generic_gates = []
        for name in self._gate_names:
            if name in TYPED_GATE_STUBS:
                typed_gates.append({"name": name, "code": TYPED_GATE_STUBS[name]})
            else:
                generic_gates.append({"name": name})

        if typed_gates:
            self._send({"type": "register_typed_gates", "gates": typed_gates})
            ack = self._recv(timeout=10)
            if not ack or ack.get("type") != "ready":
                self.close()
                raise RuntimeError(f"Sandbox gate registration failed: {ack}")

        if generic_gates:
            self._send({"type": "register_gates", "gates": generic_gates})
            ack = self._recv(timeout=10)
            if not ack or ack.get("type") != "ready":
                self.close()
                raise RuntimeError(f"Sandbox generic gate registration failed: {ack}")

        self._alive = True
        logger.debug("Sandbox started with %d gates", len(self._gate_names))

    def execute(self, code: str) -> SandboxResult:
        """Execute code in the sandbox. Handles gate calls during execution.

        This method blocks until execution completes (or times out).
        Gate calls from the entity's code are dispatched synchronously.
        """
        if not self.alive:
            return SandboxResult(error="Sandbox is not running")

        with self._lock:
            return self._execute_locked(code)

    def _execute_locked(self, code: str) -> SandboxResult:
        """Execute with lock held."""
        start = time.monotonic()
        gate_calls: List[GateCallRecord] = []

        self._send({"type": "execute", "code": code})

        # RPC loop: handle gate calls until we get a "result"
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            msg = self._recv(timeout=max(1, deadline - time.monotonic()))
            if msg is None:
                # Subprocess died or timed out
                duration = int((time.monotonic() - start) * 1000)
                stderr = self._drain_stderr()
                return SandboxResult(
                    error=f"Sandbox communication lost.\n{stderr}",
                    gate_calls=gate_calls,
                    duration_ms=duration,
                )

            msg_type = msg.get("type", "")

            if msg_type == "gate_call":
                gate_start = time.monotonic()
                gc_record = self._dispatch_gate_call(
                    msg.get("name", ""),
                    msg.get("args", {}),
                    msg.get("id", ""),
                )
                gc_record.duration_ms = int((time.monotonic() - gate_start) * 1000)
                gate_calls.append(gc_record)

            elif msg_type == "result":
                duration = int((time.monotonic() - start) * 1000)
                # Parse gate call log from child (for cross-referencing)
                child_gc_log = msg.get("gate_calls", [])
                return SandboxResult(
                    output=msg.get("output", ""),
                    error=msg.get("error"),
                    gate_calls=gate_calls,
                    done=msg.get("done", False),
                    answer=msg.get("answer", ""),
                    duration_ms=duration,
                )
            else:
                logger.debug("Sandbox: unexpected message type: %s", msg_type)

        # Timeout
        duration = int((time.monotonic() - start) * 1000)
        return SandboxResult(
            error=f"Execution timed out after {self._timeout}s",
            gate_calls=gate_calls,
            duration_ms=duration,
        )

    def _dispatch_gate_call(
        self, name: str, args: Dict, call_id: str,
    ) -> GateCallRecord:
        """Dispatch a gate call to the registered handler."""
        handler = self._gate_handlers.get(name)
        args_preview = str(args)[:100]

        # Fire progress callback so gateway/CLI can show what's happening
        if self._progress_callback:
            try:
                self._progress_callback(name, _gate_preview(name, args), args)
            except Exception:
                pass

        if not handler:
            error_msg = f"Unknown gate: {name}"
            self._send({"type": "gate_error", "id": call_id, "error": error_msg})
            return GateCallRecord(
                name=name, arguments=args_preview,
                result_preview=error_msg, error=True,
            )

        try:
            result = handler(args)
            result_str = str(result) if result is not None else ""
            self._send({"type": "gate_result", "id": call_id, "result": result_str})
            return GateCallRecord(
                name=name, arguments=args_preview,
                result_preview=result_str[:200],
            )
        except Exception as e:
            error_msg = str(e)
            logger.debug("Gate %s error: %s", name, error_msg)
            self._send({"type": "gate_error", "id": call_id, "error": error_msg})
            return GateCallRecord(
                name=name, arguments=args_preview,
                result_preview=error_msg[:200], error=True,
            )

    def close(self) -> None:
        """Shutdown the sandbox subprocess and clean up."""
        self._alive = False
        if self._proc:
            try:
                self._send({"type": "shutdown"})
                self._proc.wait(timeout=3)
            except Exception:
                pass
            try:
                if _IS_WINDOWS:
                    self._proc.terminate()
                else:
                    import signal
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                self._proc.kill()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            self._proc = None

        if self._tmpdir:
            import shutil
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    @property
    def alive(self) -> bool:
        return self._alive and self._proc is not None and self._proc.poll() is None

    # ── Internal RPC helpers ──────────────────────────────────────────

    def _send(self, msg: Dict) -> None:
        """Send a JSON message to the sandbox via stdin."""
        if not self._proc or not self._proc.stdin:
            return
        try:
            data = (json.dumps(msg, default=str) + "\n").encode("utf-8")
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            logger.debug("Sandbox stdin write failed: %s", e)
            self._alive = False

    def _recv(self, timeout: float = 300) -> Optional[Dict]:
        """Read a JSON message from the sandbox via stdout."""
        if not self._proc or not self._proc.stdout:
            return None

        # Use a thread + event to implement timeout on readline
        result_holder: List[Optional[str]] = [None]
        error_holder: List[Optional[Exception]] = [None]

        def _reader():
            try:
                line = self._proc.stdout.readline()
                result_holder[0] = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
            except Exception as e:
                error_holder[0] = e

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        reader.join(timeout=timeout)

        if reader.is_alive():
            # Timeout — reader thread is stuck on readline
            return None

        if error_holder[0]:
            logger.debug("Sandbox stdout read error: %s", error_holder[0])
            return None

        line = result_holder[0]
        if not line or not line.strip():
            return None

        try:
            return json.loads(line.strip())
        except json.JSONDecodeError as e:
            logger.debug("Sandbox: invalid JSON from child: %s", e)
            return None

    def _drain_stderr(self) -> str:
        """Read any available stderr from the subprocess."""
        if not self._proc or not self._proc.stderr:
            return ""
        try:
            # Non-blocking read of whatever's available
            import select
            if hasattr(select, "select"):
                ready, _, _ = select.select([self._proc.stderr], [], [], 0.1)
                if ready:
                    data = self._proc.stderr.read(10000)
                    if data:
                        return data.decode("utf-8", errors="replace")
        except Exception:
            pass
        return ""

    def _build_child_env(self) -> Dict[str, str]:
        """Build a safe environment for the child process.

        Excludes API keys and secrets. Passes through safe env vars.
        Same pattern as tools/code_execution_tool.py.
        """
        _SAFE_PREFIXES = (
            "PATH", "HOME", "USER", "LANG", "LC_", "TERM",
            "TMPDIR", "TMP", "TEMP", "SHELL", "LOGNAME",
            "XDG_", "PYTHONPATH", "VIRTUAL_ENV", "CONDA",
            "USERPROFILE", "APPDATA", "LOCALAPPDATA",  # Windows
            "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC",  # Windows
        )
        _SECRET_SUBSTRINGS = (
            "KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL",
            "PASSWD", "AUTH",
        )

        child_env: Dict[str, str] = {}
        for k, v in os.environ.items():
            if any(s in k.upper() for s in _SECRET_SUBSTRINGS):
                continue
            if any(k.upper().startswith(p) for p in _SAFE_PREFIXES):
                child_env[k] = v

        child_env["PYTHONDONTWRITEBYTECODE"] = "1"

        # Ensure hermes-agent root is importable
        hermes_root = str(Path(__file__).parent.parent)
        existing = child_env.get("PYTHONPATH", "")
        child_env["PYTHONPATH"] = hermes_root + (os.pathsep + existing if existing else "")

        # Timezone passthrough
        tz = os.getenv("HERMES_TIMEZONE", "").strip()
        if tz:
            child_env["TZ"] = tz

        return child_env
