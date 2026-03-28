# Necronomichat v2 — Logbook

## 2026-03-27 — Phase 7 polish: config, progress display, minor fixes

### CLI config for medium
- Added `medium` section to `DEFAULT_CONFIG` in `hermes_cli/config.py`
- Keys: `medium.type` ("conversation" | "code"), `medium.viewport_limit`, `medium.timeout`, `medium.persist_state`
- `hermes config set medium.type code` now works out of the box

### Progress display for code medium (gateway + CLI)
Code medium was invisible to users — no progress messages during execution. Now uses the same `tool_progress_callback` system as conversation mode.

**How it works:**
- `CodeMedium` accepts `progress_callback` — fires on each `execute()` call with a code preview
- `PythonSandbox._dispatch_gate_call()` fires the callback on each gate call with a gate-specific preview
- `_code_preview(code)`: extracts first meaningful line (skips comments/imports), truncated to 80 chars
- `_gate_preview(name, args)`: per-gate preview extraction (path for file ops, command for terminal, query for search, etc.)
- `run_agent.py:_create_code_medium()` passes `self.tool_progress_callback` through
- `ComposeEngine` propagates `progress_callback` to child code mediums

**What users see (telegram/discord/etc):**
```
🔮 execute: "result = read_file(path='src/main.py')"
📄 read_file: "src/main.py"
⚡ terminal: "ls -la /tmp"
🔮 execute: "submit_answer(analysis)"
```

**Files modified:**
- `nchat/sandbox.py` — `progress_callback` param on PythonSandbox, `_gate_preview()` helper
- `nchat/medium.py` — `progress_callback` param on CodeMedium, `_code_preview()` helper
- `nchat/compose.py` — `progress_callback` param on ComposeEngine, propagated to children
- `run_agent.py` — passes `tool_progress_callback` to CodeMedium and ComposeEngine
- `agent/display.py` — added "execute" to `build_tool_preview` primary_args map
- `hermes_cli/config.py` — added `medium` section to DEFAULT_CONFIG

### Bug fix
- `_gate_call` was missing from sandbox child's `_ns` namespace (found during Phase 7 verification). Typed gate stubs referenced it but it wasn't injected. Fixed by adding `"_gate_call": _gate_call` to the namespace dict.

---

## 2026-03-27 — Phase 7: The Medium Shift (7a + 7b)

### Summary
Implemented Phase 7a (code as primary medium) and Phase 7b (familiar pattern / composition). The entity can now think in code — every turn is a Python program with gate functions as host-provided async calls. In 7b, the entity can construct and orchestrate child entities at runtime via `cantrip()/cast()/cast_batch()`.

### Phase 7a: Code as Primary Medium (COMPLETE)

**New files:**
- `nchat/sandbox.py` (~350 lines) — PythonSandbox class
  - Long-lived subprocess with stdin/stdout JSON-RPC protocol
  - Gate functions injected as typed Python stubs (21 typed stubs + generic fallback)
  - State persists across turns (same `_ns` namespace dict for all exec() calls)
  - Cross-platform: works on Windows + Linux (no UDS, no os.setsid dependency)
  - Safe environment: API keys/secrets filtered from child env
  - Thread-safe execute() with lock serialization
  - SANDBOX_CHILD_SCRIPT: self-contained child process (~100 lines, no nchat/hermes imports)

- `nchat/medium.py` (~300 lines) — CodeMedium class
  - `register_gate(GateSpec)` + `register_gates_from_registry(gate_names)` — wraps tool handlers
  - `execute(code) → Observation` — runs in sandbox, returns viewport-formatted result
  - `format_viewport(result)` — THE key design decision: metadata + preview, not raw dumps
    - Gate results: first 150 chars + total length
    - Console output: up to viewport_limit (default 500), then truncated
    - Errors: FULL stack trace (never truncated — errors are steering)
  - `capability_text()` — medium documentation for system prompt (~400 tokens vs ~3,300 tokens of tool schemas)
  - `execute_tool_schema()` — single "execute" tool (replaces 20+ tool schemas)
  - `is_done()` / `answer` — tracks submit_answer() state for loop termination
  - 22 gate signatures (GATE_SIGNATURES) for system prompt documentation

**Modified files:**
- `nchat/loop.py` — added `_run_code_cast()` (~200 lines)
  - `run_cast()` now accepts `medium` parameter; routes to code path when set
  - `_run_code_cast()`: clean 11-step turn cycle for code medium
  - `_query_crystal_direct()`: direct API call (no bridge overhead for code medium)
  - `_extract_code_from_response()`: extracts code from execute tool call
  - tool_choice="required" with fallback to "auto" if provider doesn't support it
- `nchat/call.py` — added `medium_capability_text` parameter to `build_system_prompt_compact()`
  - When set, replaces standard capability presentation with medium documentation
  - Memory/user profile/skills still included alongside medium docs

### Phase 7b: The Familiar Pattern (COMPLETE)

**New files:**
- `nchat/compose.py` (~300 lines) — ComposeEngine class
  - `cantrip_gate(args) → handle_id` — creates child entity configuration
  - `cast_gate(args) → str` — runs child, blocks until complete, returns result
  - `cast_batch_gate(args) → JSON` — parallel execution with semaphore (max_concurrent=8)
  - `dispose_gate(args)` — cleanup unused handles
  - Three child types:
    - **Leaf cantrip** (medium=None): single LLM call, cheapest possible delegation
    - **Conversation child** (medium="conversation"): tool-calling loop via run_cast
    - **Code child** (medium="python"): new sandbox + code loop
  - Ward composition (WARD-1): children can only be more restricted than parents
  - Depth limits: at depth 0, cantrip/cast gates are not registered
  - Security: child gate sets validated against parent's gate set
  - Handles consumed on cast; double-cast returns error
  - `_resolve_child_crystal()`: resolves tier names + model ID passthrough
  - Loom integration: child turns recorded with parent_id for tree reconstruction

**Modified files:**
- `nchat/wards.py` — added `max_depth` field to Circle (default 2), `compose_wards()` function
- `nchat/crystals.py` — added `resolve_model_id()` for arbitrary model ID strings
  - Supports "anthropic/claude-sonnet-4-6", "openrouter/google/gemini-3-flash", bare model names
- `nchat/__init__.py` — exports all Phase 7 types (25 total symbols)

### Architecture after Phase 7
```
nchat/
  __init__.py        # 25 exports
  crystals.py        # Three-tier + model ID passthrough
  call.py            # Identity + medium capability text integration
  wards.py           # MaxTurns, BudgetWarning, PostCastReview, compose_wards
  review.py          # Post-cast review cantrip
  loop.py            # Conversation + code medium paths
  dispatch.py        # Tool call routing
  api.py             # API call construction
  session.py         # Session persistence
  stream.py          # Streaming handler
  loom.py            # Turn recording (parent_id for composition trees)
  medium.py          # CodeMedium + viewport formatting (NEW)
  sandbox.py         # PythonSandbox + RPC protocol (NEW)
  compose.py         # ComposeEngine + familiar pattern (NEW)
```

### Key design decisions
1. **Viewport principle**: entity code output is formatted as metadata + preview, not raw dumps. Data lives in sandbox variables, not in context window.
2. **Direct crystal queries**: code medium bypasses the bridge's query_api (no streaming/TUI needed for internal code). Uses `_query_crystal_direct()` directly.
3. **Stdin/stdout RPC**: sandbox uses stdin/stdout pipes (not UDS) for cross-platform compatibility. Child's print() output captured in StringIO; original stdout used for RPC.
4. **Submit_answer as function**: entity calls `submit_answer("result")` in code to terminate. Not a gate call — it's a local function that sets a flag. The result goes to the user.
5. **Ward composition is conservative**: child max_turns = min(child_requested, parent_remaining). Depth decrements. At depth 0, composition gates vanish.
6. **Handles are consumed**: after cast(), the handle is auto-disposed. Double-cast returns an error. This prevents resource leaks.

### Token economics (code medium vs conversation)
- System prompt: ~600 tokens (medium docs + gate signatures) vs ~3,300 tokens (20+ tool schemas)
- Per-turn savings: ~2,700 tokens of input per API call
- Additional savings from compositional code: one Opus turn does the work of N tool-calling turns
- Delegation: leaf cantrips are single API calls (cheapest possible)

### Wiring into hermes runtime (COMPLETE)

**`run_agent.py`:**
- `_get_medium_config()`: reads `medium` section from cli-config.yaml
- `_create_code_medium()`: creates CodeMedium, registers all gates from tool registry, sets up ComposeEngine if depth > 0
- `_run_via_cast()`: detects `medium.type: code`, creates/passes/cleans up CodeMedium
- `_build_system_prompt_compact()`: passes `medium_capability_text` through to call.py
- Status line: `"🔮 Code medium active: N gates, viewport=500, depth=2"`

**Gateway (no changes needed):**
- submit_answer output flows through existing plumbing: `CastResult.response` → `final_response` → `agent_result["final_response"]` → gateway display

**Bug fix during verification:**
- `_gate_call` was missing from sandbox child's `_ns` namespace — typed gate stubs referenced it but it wasn't injected. Fixed by adding `"_gate_call": _gate_call` to `_ns`.

### Verified end-to-end
- All 25 nchat symbols import cleanly
- All 8 modules load without circular dependencies
- run_agent.py parses (112 methods, all 4 Phase 7 methods present)
- Sandbox: start, execute Python, gate calls dispatched to host, state persists across turns, submit_answer terminates, close
- Viewport formatting: gate results previewed, console output truncated, errors shown in full
- Ward composition: child clamped to parent remaining, depth decrements
- System prompt: medium capability text replaces standard tool listing when active

### What's NOT wired yet
- CLI config: `hermes config set medium.type code` command not implemented
- CLI progress indicator for code medium turns not implemented
- The compose engine's `_run_conversation_child()` uses standalone mode (bridge=None) — won't have access to streaming/TUI features

---

## 2026-03-26 — Fork initiated, all 6 phases complete

### Summary
Forked hermes-agent v0.4.0 into ~/necronomichat-v2. Full spec in CLAUDE.md. Completed all 6 phases of the cantrip-informed redesign in a single session.

### Phase 1: Crystal routing (COMPLETE)
- Created `nchat/crystals.py` — three-tier resolver (PRIMARY/WORKER/UTILITY)
- Modified `run_agent.py:_spawn_background_review()` → WORKER crystal (was self.model/Opus)
- Modified `tools/delegate_tool.py` — `crystal` param in schema, defaults to WORKER
- Backward-compatible: no `crystals` config = same behavior as pre-fork

### Phase 2: System prompt cleanup (COMPLETE)
- Created `nchat/call.py` — `build_call()` (pure SOUL.md) + `build_capability_presentation()` (circle-derived)
- Added `description_compact` to `ToolEntry` in `tools/registry.py` (+39 compact descriptions across all tool files)
- `get_definitions(compact=True)` swaps in short descriptions (~2K tokens saved/call)
- `_should_use_compact_mode()` auto-detects Opus/Sonnet models
- `_build_system_prompt_compact()` on AIAgent bypasses MEMORY_GUIDANCE, SKILLS_GUIDANCE, verbose platform hints
- Added `system_prompt_mode` config key and `NCHAT_PROMPT_MODE` env var

### Phase 3: Ward system (COMPLETE)
- Created `nchat/wards.py` — `MaxTurns`, `RequireDone`, `BudgetWarning`, `PostCastReview`, `Circle`
- `_get_budget_warning()` now uses single 80% ward message in compact mode (was two-tier 70%/90%)
- Memory nudge injection disabled in compact mode (was `_memory_nudge_interval` turn counting)
- Skill nudge injection disabled in compact mode (was `_skill_nudge_interval` turn counting)
- Post-cast review via `PostCastReview` ward with min_turns/min_tool_calls thresholds
- `load_circle_from_config()` reads from `wards.*` and `review.*` config keys

### Phase 4: Review cantrip (COMPLETE)
- Created `nchat/review.py` — `spawn_review_cantrip()` with `_compress_for_review()`
- Review gets compressed context (~2-4K tokens) instead of full history
- Uses WORKER crystal (Sonnet) instead of PRIMARY (Opus) — 10-20x cost reduction
- Memory store shared with parent, nudges disabled, max 4 turns
- Actions surfaced to user same as before (💾 summary)

### Phase 5: New loop (COMPLETE — structural)
- Created `nchat/loop.py` — `run_cast()` async function with clean turn cycle
- Created `nchat/dispatch.py` — tool call routing through registry
- Created `nchat/api.py` — system prompt assembly + message sanitization
- Created `nchat/session.py` — session persistence + async title generation
- Created `nchat/stream.py` — streaming response handler with `StreamEvent`
- These are the target architecture — the existing `run_conversation()` still handles execution
- Incremental migration: each module can be wired in independently

### Phase 6: Loom (COMPLETE)
- Created `nchat/loom.py` — `Loom`, `Turn`, `GateCallRecord` classes
- JSONL append-only storage at `~/.hermes/loom/{session_id}.jsonl`
- Thread/tree views for entity and delegation tracking
- Cost summary by crystal tier
- Wired into `AIAgent.__init__` (compact mode only) and result assembly
- Tested: record, load, thread, tree, summary all work

### Architecture
```
nchat/
  __init__.py        # Public API exports
  crystals.py        # Three-tier model routing
  call.py            # Identity layer (SOUL.md → system prompt)
  wards.py           # Structural constraints
  review.py          # Post-cast review cantrip
  loop.py            # Clean agent loop (target)
  dispatch.py        # Tool call routing
  api.py             # API call construction
  session.py         # Session persistence
  stream.py          # Streaming handler
  loom.py            # Turn recording
```

### Key modifications to hermes source
- `run_agent.py`: compact mode flag, WORKER crystal for review, loom recording, ward-based budget warnings, disabled nudges in compact mode, post-cast review via ward
- `tools/registry.py`: `description_compact` field on ToolEntry, `compact` param on `get_definitions`
- `tools/delegate_tool.py`: `crystal` param in schema and handler, WORKER default routing
- `tools/*.py`: 39 files with `description_compact` added to registry.register()
- `model_tools.py`: `compact` param on `get_tool_definitions`

---

## 2026-03-26 — Loop migration: run_conversation() → run_cast()

### Summary
Migrated the agent loop for compact mode. When `_compact_mode` is active (Opus/Sonnet), `run_conversation()` now delegates to `nchat/loop.py:run_cast()` via a bridge pattern, bypassing the 1,860-line monolithic loop entirely. Per-turn loom recording replaces the old single-summary approach.

### What was built

**`nchat/loop.py` — full rewrite** (was 250-line skeleton, now ~675 lines)
- `AgentBridge` dataclass: typed interface to AIAgent infrastructure (13 callable + 7 state fields)
- `ContextOverflowError` / `MaxRetriesError`: custom exceptions for clean loop control flow
- `run_cast()`: the real loop — 14-step turn cycle with per-turn loom recording
- Helpers: `_build_result`, `_last_assistant_content`, `_build_gate_calls`, `_record_loom_turn`
- Synchronous (was async in skeleton) — matches codebase

**`run_agent.py` — 6 new methods + 1 delegation branch**
- `_make_loop_api_call()`: extracted retry loop (~200 lines) — streaming/non-streaming, 3-retry backoff, fallback, context-length detection, token tracking
- `_interruptible_sleep()`: 200ms-increment sleep with interrupt checking
- `_normalize_api_response()`: response normalization across codex/anthropic/chat_completions
- `_build_loop_api_messages()`: message preparation (reasoning continuity, sanitization, caching, prefill)
- `_build_agent_bridge()`: constructs AgentBridge from AIAgent state
- `_run_via_cast()`: compact-mode entry point — bridge construction, cast invocation, result conversion, post-cast hooks (review, persist, honcho, trajectory)
- **Delegation branch** (line ~6512): `if self._compact_mode: return self._run_via_cast(...)` — placed after preflight compression, before the main loop

**`nchat/__init__.py` — updated exports**
- Added: `AgentBridge`, `ContextOverflowError`, `MaxRetriesError`

### Design decisions
- **Bridge pattern**: run_cast() accesses AIAgent via AgentBridge — never imports or touches AIAgent directly
- **Budget warning suppression**: `api_call_count=0` passed to execute_tools through bridge, preventing old two-tier budget warnings from firing (ward handles it at 80%)
- **Old loom guard**: not needed — compact mode returns from `_run_via_cast()` before reaching the old summary loom recording at line ~8097
- **Verbose mode unchanged**: the `if self._compact_mode` branch is a clean early return; all code below it runs exactly as before for non-compact mode

### What's NOT tested yet
- Smoke test: run a compact-mode conversation
- Loom verification: check per-turn JSONL output
- Multi-turn: CLI REPL session continuity
- Context compression: long session → compression trigger
- Delegation: child agent on WORKER crystal
- Post-cast review: background review spawn after 5+ turns
- Interrupt: graceful exit mid-task
- Non-compact fallback: verbose mode unchanged

---

## 2026-03-26 — Centralize HERMES_HOME path construction

### Summary
Replaced all hardcoded `.hermes` path construction with `get_hermes_home()` from `hermes_constants.py`. This ensures every file uses the single source of truth that respects `NCHAT_HOME` / `HERMES_HOME` env vars and defaults to `~/.nchat`.

### Files modified (19 files)
- `agent/context_references.py` — replaced `Path(os.getenv("HERMES_HOME", str(home / ".hermes")))` → `get_hermes_home()`
- `agent/models_dev.py` — replaced `Path(env_val) if env_val else Path.home() / ".hermes"` → `get_hermes_home()`
- `agent/model_metadata.py` — replaced `Path(os.environ.get("HERMES_HOME", ...))` → `get_hermes_home()`
- `tools/browser_tool.py` — replaced 7 occurrences of `Path(os.environ.get("HERMES_HOME", ...))` → `get_hermes_home()`
- `tools/env_passthrough.py` — replaced `Path(os.environ.get("HERMES_HOME", ...))` → `get_hermes_home()`
- `tools/file_tools.py` — replaced `_pathlib.Path("~/.hermes").expanduser()` → `get_hermes_home()`
- `tools/file_operations.py` — replaced `os.path.join(_HOME, ".hermes", ".env")` → `os.path.join(str(get_hermes_home()), ".env")`
- `tools/mcp_tool.py` — replaced `os.path.expanduser(os.getenv("HERMES_HOME", ...))` → `str(get_hermes_home())`
- `tools/mcp_oauth.py` — replaced `Path(os.environ.get("HERMES_HOME", ...))` → `get_hermes_home()`
- `gateway/platforms/matrix.py` — replaced `Path.home() / ".hermes" / "matrix" / "store"` → `get_hermes_home() / "matrix" / "store"`
- `hermes_cli/plugins.py` — replaced `os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))` → `str(get_hermes_home())`
- `hermes_cli/plugins_cmd.py` — replaced `os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))` → `get_hermes_home()`
- `hermes_cli/env_loader.py` — replaced `os.getenv("HERMES_HOME", Path.home() / ".hermes")` → `get_hermes_home()`
- `hermes_cli/setup.py` — replaced `Path(os.environ.get("HERMES_HOME", ...))` → `get_hermes_home()`
- `hermes_cli/main.py` — replaced `Path(os.getenv("HERMES_HOME", ...))` → `get_hermes_home()`
- `scripts/discord-voice-doctor.py` — replaced `Path(os.getenv("HERMES_HOME", ...))` → `get_hermes_home()`
- `skills/productivity/google-workspace/scripts/setup.py` — replaced + added sys.path insert for hermes_constants
- `skills/productivity/google-workspace/scripts/google_api.py` — replaced + added sys.path insert for hermes_constants
- `rl_cli.py` — moved import to fix pre-existing NameError (used before import), removed duplicate import

### Not modified (intentionally)
- `hermes_constants.py` — the source of truth itself
- `hermes_cli/gateway.py:138` — intentional comparison against legacy `~/.hermes` default for service naming
- `hermes_cli/plugins.py:198` — project-local `.hermes/plugins/` directory (not HERMES_HOME)
- `agent/skill_commands.py:39` — project-local `.hermes/plans/` directory (not HERMES_HOME)
- All files in `build/`, `tests/`, `optional-skills/`
- Comments and docstrings
