# Necronomichat v2 — Logbook

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
