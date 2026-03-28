# PHASE 7: THE MEDIUM SHIFT

> The entity stops using tools and starts writing programs.
> Gates become host functions. The action space becomes compositional.
> One Opus turn does the work of ten.

**For:** Claude Code executing this autonomously  
**Depends on:** Phases 1-6 complete (crystal routing, clean call, wards, review cantrip, new loop, loom)  
**Reference implementations:** `deepfates/cantrip` (ts/src/, py/cantrip/), Hermes `tools/code_execution_tool.py`

---

## Overview

Phase 7 has two sub-phases. **7a** promotes code execution from "one tool among twenty" to the primary medium — every entity utterance is code, gates are host functions in the sandbox, observations follow the viewport principle. **7b** adds the familiar pattern — `cantrip()` and `cast()` become host functions, and the entity constructs and orchestrates child entities from inside the sandbox at runtime.

7a is the foundation. 7b is the payoff. Both are independently shippable.

---

# PHASE 7a: CODE AS PRIMARY MEDIUM

## What changes

### Current behavior (tool-calling mode)
```
System prompt: identity + capability presentation + memory + skills
Tools: [terminal, read_file, write_file, search_files, patch, web_search, ...]
        (20+ tool schemas, ~3,300 tokens of descriptions)
tool_choice: "auto"
Entity utterance: natural language text + optional structured tool_calls
Observation: tool result JSON strings
```

### New behavior (code medium mode)
```
System prompt: identity + medium documentation + gate reference
Tools: [execute]  (ONE tool, ~200 tokens)
tool_choice: "required"
Entity utterance: JavaScript or Python code
Observation: viewport-formatted execution result
Gates: injected as async host functions in the sandbox
```

The entity doesn't "use a tool to run code." The entity *thinks in code*. Every turn, it writes a program. The program calls gate functions to interact with the world. The sandbox runs the program and returns a viewport-formatted observation.

## Config

Add to `cli-config.yaml`:

```yaml
medium:
  # "conversation" (default, current behavior) or "code"
  type: conversation
  
  # Only applies when type: code
  code:
    language: javascript    # "javascript" or "python"
    viewport_limit: 500     # max chars of raw output shown in observation
    persist_state: true     # variables survive across turns
    error_as_steering: true # exceptions become observations, not crashes
```

Add a CLI command to switch:

```bash
hermes config set medium.type code
hermes config set medium.type conversation
```

When `medium.type` is `code`, the agent loop uses the code medium path. When `conversation`, current behavior is preserved exactly. The operator can switch between them.

## Implementation

### File: `nchat/medium.py`

This is the core new file. It manages the sandbox, gate injection, and viewport formatting.

```python
"""
Code medium — the entity writes programs, gates are host functions.

Responsibilities:
  1. Maintain a persistent sandbox (node:vm or Python subprocess)
  2. Inject gate functions as async host functions in the sandbox
  3. Execute entity utterances as programs
  4. Format observations using the viewport principle
  5. Handle errors as steering (exceptions → observations)
"""

class CodeMedium:
    def __init__(
        self,
        language: str = "javascript",
        viewport_limit: int = 500,
        persist_state: bool = True,
    ):
        self.language = language
        self.viewport_limit = viewport_limit
        self.persist_state = persist_state
        self._sandbox = None
        self._gates: dict[str, Callable] = {}
        self._gate_call_log: list[GateCallRecord] = []
    
    def register_gate(self, name: str, handler: Callable, description: str):
        """Register a gate as a host function in the sandbox."""
        
    async def execute(self, code: str) -> Observation:
        """Run the entity's code in the sandbox. Return viewport-formatted result."""
        
    def format_viewport(self, raw_output: str, gate_calls: list) -> str:
        """Apply viewport principle: metadata + preview, not raw dump."""
        
    def capability_text(self) -> str:
        """Generate the medium documentation for the system prompt."""
```

### Gate injection

Every gate from the circle's gate set gets projected into the sandbox as an async host function. The entity calls `await read_file("src/main.py")` in code, not `{"tool": "read_file", ...}` in JSON.

The gate projection wraps the existing tool handlers from `tools/registry.py`:

```python
async def _make_gate_wrapper(self, gate_name: str, handler: Callable):
    """Wrap a tool handler as a sandbox-callable async function.
    
    The wrapper:
      1. Receives arguments from the sandbox
      2. Calls the real tool handler
      3. Logs the gate call to self._gate_call_log
      4. Returns the result to the sandbox as a string
    """
    async def wrapper(**kwargs):
        result = handler(kwargs)  # existing tool handler
        self._gate_call_log.append(GateCallRecord(
            gate_name=gate_name,
            arguments=json.dumps(kwargs),
            result=str(result)[:2000],  # truncate for log
            is_error=False,
        ))
        return result
    return wrapper
```

For JavaScript (node:vm or QuickJS), gate functions are injected as globals in the sandbox context. For Python (subprocess), they're injected via an RPC bridge (stdin/stdout JSON protocol, same pattern as Hermes's existing `execute_code` sandbox).

### The `done` gate becomes `submit_answer`

In code medium, `done` is projected as `submit_answer(result)`. When the entity calls `submit_answer("the answer is 42")`, the medium captures the result and signals loop termination.

```python
# In the sandbox, the entity writes:
submit_answer("Analysis complete. Found 3 critical vulnerabilities in auth.py.")

# The medium intercepts this, sets:
self._done = True
self._answer = "Analysis complete. Found 3 critical vulnerabilities in auth.py."
```

The loop checks `medium.is_done()` after each turn.

### The viewport principle

**This is the most important design decision in Phase 7a.** Without it, the entity's context window fills with raw stdout dumps and the model degrades.

When the entity's code produces output (console.log, print, return values), the observation does NOT include the full raw output. It includes a viewport:

```
[Execution complete. 3 gate calls. Duration: 245ms]

Gate calls:
  read_file("src/auth.py") → [4823 chars] "import hashlib\nimport jwt\nfrom..."
  read_file("src/api.py") → [2156 chars] "from flask import Flask, requ..."
  terminal("grep -r 'eval(' src/") → [312 chars] "src/utils.py:45: eval(user_in..."

Console output:
  Found 12 Python files
  3 files contain potential vulnerabilities

Return value: [object Object]
```

Rules:
- Gate results: show first 150 chars, total length, gate name + args
- Console output: show in full up to `viewport_limit` chars, then truncate with `[...N more chars]`
- Return values: show type + preview
- Errors: show FULL stack trace (errors are steering, never truncated)

The entity has the *data* in sandbox variables. It doesn't need the data in its context window. It needs to know *what happened* so it can decide what code to write next.

```python
def format_viewport(self, raw_output: str, gate_calls: list, 
                    error: str | None, duration_ms: int) -> str:
    parts = []
    
    # Header
    status = "error" if error else "complete"
    parts.append(f"[Execution {status}. {len(gate_calls)} gate calls. {duration_ms}ms]")
    
    # Gate call summaries
    if gate_calls:
        parts.append("\nGate calls:")
        for gc in gate_calls:
            preview = gc.result[:150].replace("\n", "\\n")
            total = len(gc.result)
            parts.append(f"  {gc.gate_name}({gc.arguments}) → [{total} chars] \"{preview}...\"")
    
    # Error (full, never truncated)
    if error:
        parts.append(f"\nError:\n{error}")
    
    # Console output (truncated)
    if raw_output:
        if len(raw_output) <= self.viewport_limit:
            parts.append(f"\nConsole output:\n{raw_output}")
        else:
            truncated = raw_output[:self.viewport_limit]
            remaining = len(raw_output) - self.viewport_limit
            parts.append(f"\nConsole output:\n{truncated}\n[...{remaining} more chars]")
    
    return "\n".join(parts)
```

### System prompt in code medium

The system prompt changes when the medium is code. The capability presentation layer (from Phase 2) is replaced with medium documentation:

```python
def capability_text(self) -> str:
    """Generate the medium documentation block."""
    gate_docs = []
    for name, meta in self._gates.items():
        gate_docs.append(f"  {name}({meta['params']}) → {meta['returns']}")
    
    return f"""## Medium: {self.language}

You work IN code. {self.language.title()} is your medium — not a tool, but the 
substance you think in. Every response is a program that runs in a persistent sandbox.

### Persistence
- Variables declared with `var` (JS) or at module scope (Python) persist across turns.
- For async code in JS, use `globalThis.name = value` to persist.
- `let`/`const` (JS) are block-scoped to the current turn.

### Gate functions (host-provided)
{chr(10).join(gate_docs)}

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
"""
```

The full tool schema list (20+ tools, ~3,300 tokens) is replaced with this block (~400 tokens) plus the gate reference lines (~200 tokens). **Net savings: ~2,700 tokens per API call** on top of the Phase 2 compact description savings.

### Gate reference format

Each gate gets a one-line signature in the system prompt:

```
read_file(path: string, options?: {offset?: number, limit?: number}) → string
write_file(path: string, content: string) → string
search_files(query: string, options?: {path?: string, target?: string}) → string
terminal(command: string, options?: {timeout?: number, workdir?: string}) → string
web_search(query: string) → string
web_extract(url: string) → string
memory(action: string, options?: {content?: string, target?: string}) → string
session_search(query: string, options?: {limit?: number}) → string
skill_view(name: string) → string
skill_manage(action: string, options?: {name?: string, content?: string}) → string
delegate_task(goal: string, options?: {context?: string, crystal?: string}) → string
submit_answer(result: string) → void  [terminates the loop]
```

That's the entire capability presentation. The entity composes everything else in code.

### Modifications to the loop (`nchat/loop.py`)

The loop from Phase 5 needs a code-medium path:

```python
async def run_cast(crystal, call, circle, intent, history, loom, entity_id):
    medium = circle.medium  # None for conversation, CodeMedium for code
    
    if medium:
        return await _run_code_cast(crystal, call, circle, medium, intent, history, loom, entity_id)
    else:
        return await _run_conversation_cast(crystal, call, circle, intent, history, loom, entity_id)


async def _run_code_cast(crystal, call, circle, medium, intent, history, loom, entity_id):
    messages = list(history)
    messages.append({"role": "user", "content": intent})
    
    max_turns = circle.get_ward(MaxTurns).limit
    turn_count = 0
    
    # Single tool: the code execution interface
    tools = [{
        "type": "function",
        "function": {
            "name": "execute",
            "description": f"Run {medium.language} code in the sandbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The code to execute."
                    }
                },
                "required": ["code"]
            }
        }
    }]
    
    while turn_count < max_turns:
        turn_count += 1
        
        system = build_system(call, medium.capability_text())
        
        response = await crystal.query(
            system=system,
            messages=sanitize_messages(messages),
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "execute"}},
        )
        
        # Extract code from the tool call
        code = _extract_code(response)
        if not code:
            # Model produced text-only (shouldn't happen with tool_choice required,
            # but handle gracefully)
            messages.append({"role": "assistant", "content": response.content})
            continue
        
        # Execute in sandbox
        observation = await medium.execute(code)
        
        # Record in loom
        loom.record_turn(entity_id, turn_count, code, observation)
        
        # Append to message history
        messages.append(response.to_message())  # assistant with tool_call
        messages.append({
            "role": "tool",
            "tool_call_id": response.tool_calls[0].id,
            "content": observation.viewport,  # viewport-formatted, not raw
        })
        
        # Check for done
        if medium.is_done():
            return CastResult(
                response=medium.answer,
                status="terminated",
                turns=turn_count,
            )
    
    # Truncated by ward
    return CastResult(
        response=_extract_last_meaningful_output(messages),
        status="truncated",
        turns=turn_count,
    )
```

### Gateway integration

The gateway (Telegram, Discord, etc.) needs to know that in code medium, the user-facing response is the `submit_answer()` output, not the model's raw utterance.

```python
# In gateway/session.py, after run_cast returns:

result = await run_cast(...)

if result.status == "terminated":
    user_response = result.response  # this is submit_answer() output
elif result.status == "truncated":
    user_response = f"[Task incomplete after {result.turns} turns]\n\n{result.response}"

# Stream/send user_response to the platform
```

The entity's code is NOT shown to the user by default. It lives in the loom. If the user wants to see what the entity did, they can inspect the loom (`hermes loom` command).

Optional: in CLI mode, show a compact progress indicator while the entity's code runs:

```
⚙ Turn 1: 3 gate calls (read_file ×2, terminal ×1) [245ms]
⚙ Turn 2: 1 gate call (delegate_task) [1.2s]
⚙ Turn 3: submit_answer [12ms]

[Answer]
Analysis complete. Found 3 critical vulnerabilities...
```

### Sandbox implementation: choose one

Two options. Pick based on what's easier to integrate with Hermes's existing infrastructure.

**Option A: Python subprocess sandbox (recommended for initial implementation)**

Hermes already has `tools/code_execution_tool.py` with a Python sandbox. Extend it:
- Add gate injection via an RPC protocol (JSON over stdin/stdout)
- Add state persistence (pickle/JSON the sandbox globals between turns)
- Add viewport formatting to the output

The sandbox process is long-lived (spawned once per session, killed on session end). The entity's code is sent as a message, executed, results returned as a message.

```
Host ──[JSON: {"type": "execute", "code": "..."}]──→ Sandbox
Host ←─[JSON: {"type": "result", "output": "...", "gates": [...]}]── Sandbox

Host ──[JSON: {"type": "gate_result", "id": "...", "result": "..."}]──→ Sandbox
     (when a gate call completes)
```

Gate calls from inside the sandbox are synchronous from the entity's perspective. The sandbox sends a gate request, blocks, the host executes the real tool handler, sends the result back, the sandbox continues.

**Option B: node:vm sandbox**

If switching to JavaScript, use Node's `vm` module (same approach as cantrip's ts implementation). Lighter weight, faster startup, native async/await support. But requires the host to be Node (or Bun), which may conflict with Hermes's Python stack.

Hybrid approach: run the node:vm sandbox as a subprocess from the Python host, same RPC protocol as Option A.

**Recommendation:** Start with Option A (Python subprocess). It integrates cleanly with Hermes's existing code_execution_tool infrastructure. Switch to node:vm later if JavaScript proves to be a better medium for the entity.

### Error handling

Errors in the entity's code are **observations**, not crashes. The sandbox catches all exceptions and returns them as part of the viewport:

```python
# In CodeMedium.execute():
try:
    result = await self._sandbox.run(code)
    return Observation(
        viewport=self.format_viewport(result.stdout, self._gate_call_log, None, duration),
        gate_calls=self._gate_call_log.copy(),
        is_error=False,
    )
except SandboxError as e:
    return Observation(
        viewport=self.format_viewport("", self._gate_call_log, str(e), duration),
        gate_calls=self._gate_call_log.copy(),
        is_error=True,  # informational flag, does NOT terminate the loop
    )
finally:
    self._gate_call_log.clear()
```

The entity sees the full stack trace and gets another turn. This is error-as-steering: the entity adapts based on what broke.

Ward implication: budget extra turns for error recovery. If the operator sets `max_turns: 20` for conversation mode, consider `max_turns: 30` for code medium (or let the operator configure this separately in `medium.code.max_turns`).

### What NOT to do in 7a

- Do NOT implement cantrip/cast host functions. That's 7b.
- Do NOT make code medium the default. Conversation mode remains default.
- Do NOT remove conversation mode. Both must work. The operator chooses.
- Do NOT try to auto-detect whether a message should use code or conversation. The medium is a session-level setting, not a per-turn decision.
- Do NOT expose the entity's code to the user by default. The answer is what they see. The code is the thinking.

### Acceptance criteria for 7a

1. `hermes config set medium.type code` switches to code medium.
2. In code medium, the entity writes JavaScript/Python on every turn.
3. Gate functions (read_file, terminal, etc.) work as host functions in the sandbox.
4. Variables persist across turns within a session.
5. Errors produce observations with full stack traces. The entity gets another turn.
6. `submit_answer()` terminates the loop and delivers the answer to the user.
7. Observations follow the viewport principle (metadata + preview, not raw dumps).
8. The system prompt is ~400 tokens of medium documentation + ~200 tokens of gate signatures (not ~3,300 tokens of tool schemas).
9. Gateway platforms (Telegram, etc.) show the `submit_answer()` output, not raw code.
10. Conversation mode still works exactly as before when `medium.type: conversation`.
11. Loom records code utterances and viewport observations with gate call details.
12. Cost per turn is measurably lower than equivalent work in conversation mode (fewer input tokens from smaller system prompt + fewer turns from compositional code).

### Files to create/modify for 7a

```
CREATE  nchat/medium.py           # CodeMedium class, gate injection, viewport
CREATE  nchat/sandbox.py          # Sandbox process management, RPC protocol
MODIFY  nchat/loop.py             # Add _run_code_cast path
MODIFY  nchat/call.py             # Add medium.capability_text() to prompt assembly
MODIFY  nchat/loom.py             # Record code utterances + viewport observations
MODIFY  gateway/session.py        # Extract submit_answer for user-facing response
MODIFY  hermes_cli/config.py      # Add medium.type config key
MODIFY  hermes_cli/commands.py    # Add medium switching command
```

---

# PHASE 7b: THE FAMILIAR PATTERN

**Depends on:** Phase 7a complete and stable.

## What changes

The entity gains the ability to construct and cast child entities from inside the sandbox. Two new host functions appear:

```
cantrip(config) → handle       Create a child entity configuration
cast(handle, intent) → string  Run the child, get the result
cast_batch(items) → string[]   Run multiple children in parallel
dispose(handle) → void         Clean up a child's resources
```

The entity writes code like:

```javascript
const worker = await cantrip({
    crystal: "worker",
    call: "You execute shell commands and report results.",
    medium: "bash",
    wards: { max_turns: 5 },
});

const test_output = await cast(worker, "Run pytest and summarize failures");
await dispose(worker);

// Or parallel:
const files = JSON.parse(await search_files("src/**/*.py"));
const handles = [];
for (const file of files) {
    const h = await cantrip({
        crystal: "utility",
        call: "Analyze this code for security issues.",
    });
    handles.push({ cantrip: h, intent: await read_file(file) });
}
const results = await cast_batch(handles);
// results is string[] in request order
```

## The cantrip() host function

Creates a child entity configuration. Does NOT start the loop — that happens on `cast()`.

```python
async def _cantrip_gate(config: dict) -> str:
    """Host function injected into sandbox as cantrip().
    
    Config fields:
        crystal: str        - "primary", "worker", "utility", or a model ID
        call: str           - system prompt / identity for the child
        medium: str | None  - "bash", "js", "python", "conversation", or None (leaf)
        gates: list[str]    - which gates the child can use (default: ["done"])
        wards: dict         - ward overrides (max_turns, require_done)
        cwd: str | None     - working directory for bash medium
        state: dict | None  - initial state for code medium children
    
    Returns: a handle (opaque string ID) for use with cast()/dispose().
    """
```

### Crystal resolution from inside the sandbox

The entity specifies a crystal tier or a model ID:

```javascript
// By tier (recommended)
await cantrip({ crystal: "worker", ... });    // resolves to Sonnet
await cantrip({ crystal: "utility", ... });   // resolves to Haiku/Flash
await cantrip({ crystal: "primary", ... });   // resolves to Opus (expensive!)

// By model ID (escape hatch)
await cantrip({ crystal: "anthropic/claude-haiku-4.5", ... });
await cantrip({ crystal: "openrouter/google/gemini-3-flash", ... });
```

The crystal resolver from Phase 1 (`nchat/crystals.py`) handles this. Model IDs are passed through to the provider. Tier names resolve via config.

### Leaf cantrips (no medium)

When no medium is specified, the child is a **leaf cantrip** — a single LLM call with no loop. The intent goes in, one response comes back. No tools, no gates (except implicit termination), no turns.

```javascript
// Leaf cantrip: one LLM call, cheapest possible
const thinker = await cantrip({
    crystal: "utility",
    call: "You analyze code and identify patterns.",
});
const analysis = await cast(thinker, "What design patterns does this use?\n" + code);
```

This is the cheapest possible delegation. One API call to Haiku/Flash. No tool schemas. No loop. The result is the model's text response.

Implementation: skip the loop entirely. Build a single API request with the call as system prompt and the intent as user message. Return the response content.

### Medium children

When a medium is specified, the child gets its own sandbox and loop:

```javascript
// Bash child: entity works in shell
const shell_worker = await cantrip({
    crystal: "worker",
    call: "Execute commands and report output.",
    medium: "bash",
    wards: { max_turns: 5 },
    cwd: "/home/user/project",
});

// Code child: entity works in JS/Python
const analyst = await cantrip({
    crystal: "worker",
    call: "Analyze data structures.",
    medium: "javascript",
    state: { data: my_data },  // inject data into child's sandbox
    wards: { max_turns: 10 },
});

// Conversation child: entity uses tool calls (legacy mode)
const researcher = await cantrip({
    crystal: "worker",
    call: "Research this topic using web search.",
    medium: "conversation",
    gates: ["web_search", "web_extract", "done"],
    wards: { max_turns: 8 },
});
```

Each medium type creates the appropriate sandbox. Bash children get a shell session. Code children get a sandbox with gate injection. Conversation children get a tool-calling loop (the pre-Phase-7 behavior).

### cast() host function

Runs the child entity on an intent. Blocks until the child completes (terminated or truncated). Returns the result as a string.

```python
async def _cast_gate(handle: str, intent: str) -> str:
    """Host function injected into sandbox as cast().
    
    Runs the child entity identified by handle.
    Blocks until the child terminates or is truncated.
    Returns the child's answer (submit_answer output or final response).
    
    The child gets:
      - Its own crystal (resolved from the cantrip config)
      - Its own call (from the cantrip config)  
      - Its own circle (from the cantrip config: medium + gates + wards)
      - Its own context (fresh — no parent history)
      - Its own loom thread (child of the parent's current turn)
    """
```

The parent's turn is suspended while the child runs. From the parent's perspective, `cast()` is a synchronous function call that returns a string. Under the hood, the child runs its own loop (potentially multiple turns with its own tool calls or code execution).

### cast_batch() host function

Runs multiple children in parallel:

```python
async def _cast_batch_gate(items: list[dict]) -> str:
    """Host function injected into sandbox as cast_batch().
    
    items: [{ cantrip: handle, intent: string }, ...]
    
    Runs all children concurrently (up to 8 parallel, configurable).
    Returns JSON array of results in request order.
    """
```

The entity receives results as a JSON array: `["result1", "result2", "result3"]`. Parse with `JSON.parse()` in the sandbox.

Concurrency limit: 8 by default (same as cantrip spec COMP-3). Configurable in `cli-config.yaml`:

```yaml
medium:
  code:
    max_concurrent_children: 8
    max_batch_size: 50
```

### Ward composition

Child wards compose with parent wards per WARD-1:

- `max_turns`: child gets `min(child_config.max_turns, parent_remaining_turns)`. A parent with 5 turns left can't spawn a child with max_turns: 50.
- `max_depth`: decremented. If parent depth is 2, child gets depth 1. At depth 0, `cantrip()` and `cast()` gates are removed from the child's sandbox.
- `require_done`: logical OR. If parent requires it, child requires it.

```python
def compose_wards(parent_wards: dict, child_wards: dict, parent_remaining: int) -> dict:
    return {
        "max_turns": min(
            child_wards.get("max_turns", 25),
            parent_remaining,
        ),
        "max_depth": max(0, parent_wards.get("max_depth", 2) - 1),
        "require_done": parent_wards.get("require_done", False) or 
                        child_wards.get("require_done", False),
    }
```

### Depth limits

Every cantrip config has an implicit depth. The default max_depth is 2:

```
Depth 2: The familiar (parent). Can call cantrip/cast.
Depth 1: Children. Can call cantrip/cast (spawning grandchildren).
Depth 0: Grandchildren. cantrip/cast gates are NOT injected. No further delegation.
```

When `max_depth` reaches 0, the `cantrip`, `cast`, `cast_batch`, and `dispose` host functions are simply not registered in the child's sandbox. The child can't attempt delegation because the functions don't exist.

### Loom integration

Child turns are recorded in the same loom as the parent. The child's turns form a subtree rooted at the parent turn that spawned them:

```
Parent turn 1 (code: reads files, processes data)
Parent turn 2 (code: calls cast(worker, "analyze auth.py"))
├── Child turn 1 (reads auth.py, starts analysis)  
├── Child turn 2 (finds vulnerability)
└── Child turn 3 (submit_answer with findings)
Parent turn 3 (code: processes child result, calls submit_answer)
```

The loom records this via `parent_id` on each turn. Walk the tree by following parent pointers.

### System prompt for the familiar

The system prompt in code medium (from 7a) gets extended with cantrip documentation:

```python
# Append to medium.capability_text():

CANTRIP_DOCS = """
### Composition: cantrip/cast

Create child entities and delegate work:

  cantrip(config) → handle     Create a child configuration
  cast(handle, intent) → string   Run the child, get result (blocks until done)
  cast_batch(items) → string[]    Run multiple children in parallel
  dispose(handle) → void          Clean up (automatic on cast completion)

Config fields:
  crystal: "primary" | "worker" | "utility" | model-id
  call: string (child's identity/system prompt)  
  medium: "bash" | "javascript" | "python" | "conversation" | omit for leaf
  gates: string[] (default: ["done"])
  wards: { max_turns?: number } (default: { max_turns: 15 })
  cwd: string (for bash medium)
  state: object (for code medium — injected into child sandbox)

Leaf cantrip (no medium): single LLM call. Cheapest delegation.
Medium cantrip: child gets its own sandbox and loop.

Use "utility" crystal for simple analysis/summarization.
Use "worker" crystal for multi-step tasks needing tool access.
Use "primary" crystal only when the child needs your level of reasoning.
"""
```

### Disposal and cleanup

Handles are consumed on cast. After `cast(handle, intent)` returns, the handle is automatically disposed (sandbox killed, resources freed). Calling `cast()` on an already-used handle returns an error.

`dispose(handle)` is for cases where the entity creates a handle but decides not to use it. It's not required after a successful cast.

```python
# Entity writes:
const h = await cantrip({ crystal: "utility", call: "..." });
// Changed my mind, don't need this one
await dispose(h);
```

### Security constraints

Children MUST NOT have more capabilities than the parent. Enforced at construction:

1. A child's gate set is validated against the parent's gate set. A child cannot request gates the parent doesn't have.
2. A child's wards are composed conservatively (WARD-1).
3. A child with `medium: "bash"` gets a restricted gate set by default (just `done`). The entity writes bash commands IN the medium — it doesn't need read_file/write_file gates because bash can do that natively.

The `cantrip()` host function validates the config and returns an error observation if the config violates constraints. The entity sees the error and adapts (error as steering).

### What NOT to do in 7b

- Do NOT let children inherit the parent's conversation history. Fresh context per child (COMP-4).
- Do NOT let children write to the parent's memory. Memory gate is not injected in children by default.
- Do NOT let children access the parent's sandbox state. Each sandbox is isolated.
- Do NOT try to implement inter-child communication. Children don't know about each other. The parent coordinates.
- Do NOT implement hot-swapping of mediums mid-session. Medium is fixed at session start.

### Acceptance criteria for 7b

1. `cantrip()` creates child entity configurations from inside the sandbox.
2. `cast()` runs a child and returns the result as a string. Parent blocks during execution.
3. `cast_batch()` runs multiple children in parallel, returns results in request order.
4. Leaf cantrips (no medium) execute as single LLM calls — cheapest possible delegation.
5. Bash-medium children work in a shell session.
6. Code-medium children get their own sandbox with gate injection.
7. Conversation-medium children use the tool-calling loop.
8. Crystal selection works from inside the sandbox ("worker", "utility", model IDs).
9. Ward composition is enforced: children can only be more restricted than parents.
10. Depth limits prevent infinite recursion. At depth 0, cantrip/cast are not available.
11. Loom captures parent-child turn trees with proper parent_id references.
12. Handles are consumed on cast. Double-cast returns an error.
13. The familiar pattern works end-to-end: parent reads files → processes in code → spawns children for analysis → synthesizes results → submit_answer.

### Files to create/modify for 7b

```
CREATE  nchat/compose.py          # cantrip/cast/cast_batch host function impls
MODIFY  nchat/medium.py           # Register cantrip/cast/dispose as host functions
MODIFY  nchat/sandbox.py          # Child sandbox lifecycle management
MODIFY  nchat/loop.py             # Wire composition into cast execution
MODIFY  nchat/loom.py             # Parent-child turn tree recording  
MODIFY  nchat/wards.py            # Ward composition logic
MODIFY  nchat/crystals.py         # Model ID passthrough for custom crystal specs
```

---

## Appendix: The Familiar in Action

Here's what a complete interaction looks like after Phase 7b. The user asks: "Find all the security issues in this project."

The entity (Opus, code medium) writes on turn 1:

```javascript
// Survey the project
const files = JSON.parse(await search_files("**/*.py"));
console.log(`Found ${files.length} Python files`);

// Read each file and check for obvious patterns first
const suspicious = [];
for (const file of files) {
    const content = await read_file(file);
    if (content.includes("eval(") || content.includes("exec(") || 
        content.includes("pickle.loads") || content.includes("subprocess.call") ||
        content.includes("shell=True")) {
        suspicious.push(file);
    }
}
console.log(`${suspicious.length} files with suspicious patterns`);

// Delegate deep analysis to worker crystals in parallel
const handles = [];
for (const file of suspicious) {
    const h = await cantrip({
        crystal: "worker",
        call: "You are a security auditor. Analyze this Python file for vulnerabilities. " +
              "Focus on: injection, auth bypass, data exposure, unsafe deserialization. " +
              "Rate each finding CRITICAL/HIGH/MEDIUM/LOW.",
    });
    const content = await read_file(file);
    handles.push({ cantrip: h, intent: `File: ${file}\n\n${content}` });
}
const analyses = JSON.parse(await cast_batch(handles));

// Synthesize
globalThis.findings = analyses.map((analysis, i) => ({
    file: suspicious[i],
    analysis,
}));

// Quick summary with a leaf cantrip
const summarizer = await cantrip({
    crystal: "utility",
    call: "Summarize security findings. Group by severity. Be concise.",
});
const summary = await cast(summarizer, JSON.stringify(globalThis.findings));

submit_answer(summary);
```

One turn of Opus. N parallel Sonnet calls for deep analysis. One Haiku call for summarization. The user sees the summary. The loom has the full tree.

That's the familiar pattern. That's necronomichat.
