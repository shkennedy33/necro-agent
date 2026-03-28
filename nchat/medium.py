"""Code medium — the entity writes programs, gates are host functions.

When medium.type is "code", the entity thinks in code. Every turn, it writes
a Python program. The program calls gate functions to interact with the world.
The sandbox runs the program and returns a viewport-formatted observation.

The entity doesn't "use a tool to run code." The entity *thinks in code*.

Responsibilities:
  1. Maintain a PythonSandbox with injected gate functions
  2. Format observations using the viewport principle
  3. Generate medium documentation for the system prompt
  4. Track submit_answer() state for loop termination

Viewport principle (CRITICAL):
  The observation does NOT include full raw output. It includes metadata +
  previews. The entity has data in sandbox variables — it doesn't need the
  data in its context window. It needs to know *what happened*.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

from nchat.loom import GateCallRecord
from nchat.sandbox import PythonSandbox, SandboxResult, generate_gate_code

logger = logging.getLogger(__name__)


# ── Gate signatures for the system prompt ─────────────────────────────
# One-line signatures shown in the medium documentation block.

GATE_SIGNATURES: Dict[str, str] = {
    "terminal": "terminal(command: str, timeout: int = None, workdir: str = None) → str",
    "read_file": "read_file(path: str, offset: int = 1, limit: int = 500) → str",
    "write_file": "write_file(path: str, content: str) → str",
    "search_files": "search_files(pattern: str, target: str = 'content', path: str = '.', file_glob: str = None, limit: int = 50) → str",
    "patch": "patch(path: str, old_string: str, new_string: str, replace_all: bool = False) → str",
    "web_search": "web_search(query: str, limit: int = 5) → str",
    "web_extract": "web_extract(urls: list) → str",
    "memory": "memory(action: str, content: str = None, target: str = None) → str",
    "session_search": "session_search(query: str, limit: int = 5) → str",
    "skill_view": "skill_view(name: str) → str",
    "skill_manage": "skill_manage(action: str, name: str = None, content: str = None) → str",
    "delegate_task": "delegate_task(goal: str, context: str = None, crystal: str = 'worker') → str",
    "todo": "todo(action: str, task: str = None, id: int = None) → str",
    "clarify": "clarify(question: str) → str",
    "image_generation": "image_generation(prompt: str) → str",
    "vision_analyze": "vision_analyze(image_path: str, prompt: str = None) → str",
    "tts": "tts(text: str, voice: str = None) → str",
    "submit_answer": "submit_answer(result: str) → None  [terminates the loop]",
    # 7b composition gates
    "cantrip": "cantrip(config: dict) → str  [returns handle ID]",
    "cast": "cast(handle: str, intent: str) → str  [runs child, blocks until done]",
    "cast_batch": "cast_batch(items: list[dict]) → list[str]  [parallel, returns in order]",
    "dispose": "dispose(handle: str) → None  [clean up unused handle]",
}


# ── GateSpec ──────────────────────────────────────────────────────────

@dataclass
class GateSpec:
    """Specification for a gate function in the code medium."""
    name: str
    handler: Callable  # (args_dict) → str
    signature: str = ""  # For system prompt (e.g., "read_file(path: str) → str")
    description: str = ""  # One-liner for documentation


# ── Observation ───────────────────────────────────────────────────────

@dataclass
class Observation:
    """Viewport-formatted execution result."""
    viewport: str  # The formatted observation string
    gate_calls: List[GateCallRecord] = field(default_factory=list)
    is_error: bool = False
    done: bool = False
    answer: str = ""
    duration_ms: int = 0
    raw_output: str = ""  # Untruncated stdout (not sent to model)


def _code_preview(code: str) -> str | None:
    """Extract first meaningful line of code for progress display."""
    for line in code.strip().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("import "):
            return line[:80]
    return None


# ── CodeMedium ────────────────────────────────────────────────────────

class CodeMedium:
    """Code execution medium — entity thinks in Python code.

    Usage:
        medium = CodeMedium(viewport_limit=500)
        medium.register_gate(GateSpec(name="read_file", handler=handler_fn, ...))
        medium.start()

        observation = medium.execute("content = read_file('main.py')\\nprint(len(content))")
        if medium.is_done():
            print(medium.answer)

        medium.close()
    """

    def __init__(
        self,
        language: str = "python",
        viewport_limit: int = 500,
        persist_state: bool = True,
        timeout: int = 300,
        progress_callback: Callable | None = None,
    ):
        self.language = language
        self.viewport_limit = viewport_limit
        self.persist_state = persist_state
        self.timeout = timeout
        self.progress_callback = progress_callback

        self._gates: Dict[str, GateSpec] = {}
        self._sandbox: Optional[PythonSandbox] = None
        self._done = False
        self._answer = ""

    def register_gate(self, spec: GateSpec) -> None:
        """Register a gate function."""
        if not spec.signature and spec.name in GATE_SIGNATURES:
            spec.signature = GATE_SIGNATURES[spec.name]
        self._gates[spec.name] = spec

    def register_gates_from_registry(
        self,
        gate_names: Set[str],
        task_id: str | None = None,
    ) -> None:
        """Register gates from the tool registry.

        Wraps each tool handler from tools/registry.py as a gate function.
        The wrapper translates between the sandbox RPC format and the
        existing handle_function_call dispatch.
        """
        try:
            from model_tools import handle_function_call
        except ImportError:
            logger.warning("Could not import handle_function_call; gates not registered")
            return

        for name in gate_names:
            # submit_answer is handled internally, not via registry
            if name in ("submit_answer", "done"):
                continue
            # Composition gates (cantrip, cast, etc.) are registered separately
            if name in ("cantrip", "cast", "cast_batch", "dispose"):
                continue

            def _make_handler(tool_name: str, tid: str | None) -> Callable:
                def handler(args: Dict) -> str:
                    try:
                        result = handle_function_call(
                            tool_name, args, task_id=tid,
                        )
                        return result if isinstance(result, str) else json.dumps(result, default=str)
                    except Exception as e:
                        return json.dumps({"error": str(e)})
                return handler

            self.register_gate(GateSpec(
                name=name,
                handler=_make_handler(name, task_id),
                signature=GATE_SIGNATURES.get(name, f"{name}(**kwargs) → str"),
            ))

    def start(self) -> None:
        """Spawn the sandbox subprocess with all registered gates."""
        if self._sandbox and self._sandbox.alive:
            return

        gate_handlers = {name: spec.handler for name, spec in self._gates.items()}
        gate_names = list(self._gates.keys())

        self._sandbox = PythonSandbox(
            gate_handlers=gate_handlers,
            gate_names=gate_names,
            timeout=self.timeout,
            progress_callback=self.progress_callback,
        )
        self._sandbox.start()
        self._done = False
        self._answer = ""

    def execute(self, code: str) -> Observation:
        """Execute entity code in the sandbox. Return viewport-formatted observation."""
        if not self._sandbox or not self._sandbox.alive:
            return Observation(
                viewport="[Error: Sandbox is not running]",
                is_error=True,
            )

        # Fire progress: entity is executing code
        if self.progress_callback:
            try:
                # Show first meaningful line of code as preview
                preview = _code_preview(code)
                self.progress_callback("execute", preview, {"code": code[:200]})
            except Exception:
                pass

        result = self._sandbox.execute(code)

        # Track done/answer state
        if result.done:
            self._done = True
            self._answer = result.answer

        # Format viewport observation
        viewport = self.format_viewport(result)

        return Observation(
            viewport=viewport,
            gate_calls=result.gate_calls,
            is_error=result.error is not None,
            done=result.done,
            answer=result.answer,
            duration_ms=result.duration_ms,
            raw_output=result.output,
        )

    def is_done(self) -> bool:
        """Whether submit_answer() has been called."""
        return self._done

    @property
    def answer(self) -> str:
        """The submit_answer() argument, or empty string."""
        return self._answer

    def reset_done(self) -> None:
        """Reset done state for next turn (normally not needed)."""
        self._done = False
        self._answer = ""

    def format_viewport(self, result: SandboxResult) -> str:
        """Apply viewport principle: metadata + preview, not raw dump.

        Rules:
          - Gate results: show first 150 chars, total length, gate name + args
          - Console output: show in full up to viewport_limit, then truncate
          - Errors: show FULL stack trace (never truncated)
          - Return values: show type + preview
        """
        parts = []

        # Header
        status = "error" if result.error else "complete"
        parts.append(
            f"[Execution {status}. "
            f"{len(result.gate_calls)} gate calls. "
            f"{result.duration_ms}ms]"
        )

        # Gate call summaries
        if result.gate_calls:
            parts.append("\nGate calls:")
            for gc in result.gate_calls:
                if gc.error:
                    parts.append(f"  {gc.name}({gc.arguments}) → ERROR: {gc.result_preview[:150]}")
                else:
                    preview = gc.result_preview[:150].replace("\n", "\\n")
                    total = len(gc.result_preview)
                    if total > 150:
                        parts.append(f'  {gc.name}({gc.arguments}) → [{total} chars] "{preview}..."')
                    elif total > 0:
                        parts.append(f'  {gc.name}({gc.arguments}) → "{preview}"')
                    else:
                        parts.append(f"  {gc.name}({gc.arguments}) → (empty)")

        # Error (full, never truncated — errors are steering)
        if result.error:
            parts.append(f"\nError:\n{result.error}")

        # Console output (truncated per viewport_limit)
        if result.output:
            if len(result.output) <= self.viewport_limit:
                parts.append(f"\nConsole output:\n{result.output}")
            else:
                truncated = result.output[:self.viewport_limit]
                remaining = len(result.output) - self.viewport_limit
                parts.append(
                    f"\nConsole output:\n{truncated}\n"
                    f"[...{remaining} more chars]"
                )

        # Done signal
        if result.done:
            parts.append(f"\n[submit_answer called — loop will terminate]")

        return "\n".join(parts)

    def capability_text(self, include_composition: bool = False) -> str:
        """Generate the medium documentation block for the system prompt.

        This replaces the tool schema listing (~3,300 tokens) with a concise
        medium description (~400 tokens) plus gate signatures (~200 tokens).
        """
        gate_lines = []
        for name in sorted(self._gates.keys()):
            spec = self._gates[name]
            sig = spec.signature or f"{name}(**kwargs) → str"
            gate_lines.append(f"  {sig}")

        # Always include submit_answer
        if "submit_answer" not in self._gates:
            gate_lines.append(f"  {GATE_SIGNATURES['submit_answer']}")

        text = f"""## Medium: Python

You work IN code. Python is your medium — not a tool, but the substance you
think in. Every response is a program that runs in a persistent sandbox.

### Persistence
- Variables at module scope persist across turns.
- Data you've read stays in variables — access it with code, not re-reading.
- Imports persist. Standard library is available (json, re, math, csv, etc.).

### Gate functions (host-provided)
{chr(10).join(gate_lines)}

### Submitting answers
Call `submit_answer(result)` when you have a final answer for the user.
The result string is what the user sees. Your code is your thinking —
the answer is what you deliver.

### Observations
After each turn, you see a viewport summary: gate call results (first 150 chars),
console output (first {self.viewport_limit} chars), and any errors (full trace).
Data lives in your variables, not in the viewport. Process data with code.

### Error handling
Exceptions become observations. You see the full stack trace and get another turn.
Errors are information, not failures. Adapt and continue.

### Helpers
- `json_parse(text)` — json.loads with strict=False (for terminal output with control chars)
- `json` module is pre-imported"""

        if include_composition:
            text += """

### Composition: cantrip/cast

Create child entities and delegate work:

  cantrip(config) → handle     Create a child configuration
  cast(handle, intent) → str   Run the child, get result (blocks until done)
  cast_batch(items) → list     Run multiple children in parallel
  dispose(handle) → None       Clean up (automatic on cast completion)

Config fields:
  crystal: "primary" | "worker" | "utility" | model-id
  call: str (child's identity/system prompt)
  medium: "python" | "conversation" | None (omit for leaf cantrip)
  gates: list[str] (default: ["done"])
  wards: dict (e.g., {"max_turns": 15})

Leaf cantrip (no medium): single LLM call. Cheapest delegation.
Medium cantrip: child gets its own sandbox and loop.

Use "utility" crystal for simple analysis/summarization.
Use "worker" crystal for multi-step tasks needing tool access.
Use "primary" crystal only when the child needs your level of reasoning."""

        return text

    def execute_tool_schema(self) -> Dict:
        """Return the single 'execute' tool schema for API calls.

        In code medium, there is ONE tool. The entity writes code into it.
        """
        return {
            "type": "function",
            "function": {
                "name": "execute",
                "description": f"Run {self.language} code in the persistent sandbox.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "The code to execute.",
                        },
                    },
                    "required": ["code"],
                },
            },
        }

    def close(self) -> None:
        """Shutdown the sandbox and release resources."""
        if self._sandbox:
            self._sandbox.close()
            self._sandbox = None
        self._done = False
        self._answer = ""

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
