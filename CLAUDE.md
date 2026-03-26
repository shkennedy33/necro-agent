# NECRONOMICHAT 2.0: Fork Guide

> A cantrip-informed fork of hermes-agent. The soul gets to be the soul.
> The model gets to be the model. The loop gets to be the loop.

**For:** Claude Code (Opus) executing this fork  
**From:** hermes-agent @ `NousResearch/hermes-agent` (v0.4.0, March 2026)  
**Informed by:** cantrip spec @ `deepfates/cantrip` (v0.3.1)  
**Target operator:** Single user on a VPS. Not production. Not multi-tenant.

---

## 0. Philosophy

Hermes-agent is an impressive system built on one assumption: the same model does everything, and the system prompt compensates for that model's weaknesses. The result is a 7,741-line agent loop (`run_agent.py`) with ~3,300 tokens of tool schema overhead per call, behavioral guidance blocks that overwrite character, nudge injections that burn tokens hoping the model notices, and background review subagents that clone the full conversation into the primary model just to ask "should I save anything?"

Necronomichat inverts this. Three principles:

**1. The soul is the soul.** SOUL.md is the identity layer. Nothing else touches it. No behavioral guidance appended. No tool instructions mixed in. No "Do NOT use cat" diluting whatever chaos or character the operator put in the soul. The circle presents its own capabilities. The soul presents its own self.

**2. Different crystals for different work.** Opus does orchestration, conversation, and creative reasoning — the tasks that justify its cost. Sonnet handles delegated subtasks, memory review, and skill operations. Haiku handles summarization, title generation, and leaf-node analysis. The operator pays for intelligence where intelligence matters.

**3. Structure over persuasion.** Wards, not nudges. Max turns, not budget warnings injected into tool results. Post-cast review cantrips, not mid-conversation "hey maybe save something" injections. The model is an actor in a structured environment, not a student being reminded to do homework.

The cantrip spec provides the vocabulary: crystal (model), call (identity), circle (environment with medium + gates + wards), entity (what emerges from the loop), loom (the durable record). These are not abstractions to be implemented literally — they are the *thinking tools* for redesigning the agent loop. The fork keeps Hermes's operational infrastructure (terminal backends, gateway, messaging platforms, MCP, skills system, session DB) but replaces the loop's internals.

---

## 1. What To Keep, What To Gut, What To Rewrite

### KEEP (operational infrastructure)
These are the parts that work and have nothing to do with the agent loop's token economics:

```
gateway/                    # Telegram, Discord, Slack, WhatsApp, Signal
hermes_cli/                 # CLI, config, setup, TUI, model switching
tools/terminal_tool.py      # Terminal backends (local, Docker, SSH, etc.)
tools/file_tools.py         # File operations
tools/web_tools.py          # Web search, extraction
tools/browser_tool.py       # Browser automation
tools/mcp_tool.py           # MCP integration
tools/send_message_tool.py  # Cross-platform messaging
tools/transcription_tools.py
tools/tts_tool.py
tools/vision_tools.py
tools/image_generation_tool.py
tools/homeassistant_tool.py
tools/cronjob_tools.py      # Cron scheduling
tools/checkpoint_manager.py
tools/process_registry.py
tools/approval.py           # Command approval system
tools/tirith_security.py    # Security scanning
tools/skills_guard.py       # Skill security scanning
tools/skills_hub.py         # Skills marketplace
tools/skills_sync.py
tools/registry.py           # Tool registry
hermes_state.py             # Session DB (SQLite)
hermes_constants.py
hermes_time.py
toolsets.py
toolset_distributions.py
environments/               # RL environments (keep for later)
```

### GUT (the bloated core)
These files contain the problematic patterns. Don't delete them — they're the reference for what the new code must replace:

```
run_agent.py                # 7,741 lines. The monolith. Replace entirely.
agent/prompt_builder.py     # System prompt assembly. Replace.
agent/smart_model_routing.py # Too conservative. Replace with crystal routing.
model_tools.py              # Tool schema assembly. Rewrite.
```

### REWRITE (keep the concept, change the implementation)

```
agent/auxiliary_client.py   # Already good for cheap model routing. Extend to be
                            # the crystal resolver — given a task tier, return 
                            # the right client + model.

agent/context_compressor.py # Already uses auxiliary client. Refactor to be a 
                            # "folding" system in cantrip terms. The core logic
                            # (prune old tool results, protect head/tail, 
                            # summarize middle) is sound. Just decouple it from
                            # the monolithic agent.

tools/memory_tool.py        # Keep the file-backed storage. Remove it from the
                            # system prompt injection pattern. Memory becomes a
                            # gate the entity calls, not a blob stapled to every
                            # API request.

tools/skills_tool.py        # Keep. But skill_view content goes into the entity's
tools/skill_manager_tool.py # working context on demand, not into the system prompt.

tools/delegate_tool.py      # Rewrite to accept crystal + medium + ward params.
                            # This becomes the call_entity gate.

tools/session_search_tool.py # Keep. Already uses auxiliary client for summaries.

tools/todo_tool.py          # Keep as-is.
tools/clarify_tool.py       # Keep as-is.
tools/code_execution_tool.py # Keep — this could become a medium.
```

### NEW FILES TO CREATE

```
nchat/                      # New package root for the fork's core
nchat/__init__.py
nchat/crystals.py           # Crystal resolver: task tier → (client, model)
nchat/call.py               # Identity/call builder: SOUL.md → clean system prompt
nchat/circle.py             # Circle construction: gates + wards, no medium yet
nchat/loop.py               # The agent loop. < 500 lines. The heart of the fork.
nchat/loom.py               # Turn recording, thread tracking, folding triggers
nchat/gates.py              # Gate registry — maps gate names to handlers
nchat/wards.py              # Ward definitions: max_turns, max_tokens, require_done
nchat/review.py             # Post-cast review cantrips (memory + skills)
nchat/compose.py            # call_entity / delegate gate with crystal selection
```

---

## 2. The Crystal System

### 2.1 Three tiers

The crystal resolver maps task types to models. The operator configures this in `cli-config.yaml`:

```yaml
crystals:
  # The primary crystal. Orchestration, conversation, creative work.
  primary:
    provider: anthropic
    model: claude-opus-4-6

  # Delegated work, memory review, skill creation/patching.
  worker:
    provider: anthropic
    model: claude-sonnet-4-6

  # Summarization, title generation, leaf-node analysis, folding.
  # Falls back to auxiliary_client resolution chain if not set.
  utility:
    provider: openrouter
    model: google/gemini-3-flash-preview
```

### 2.2 Crystal resolver

`nchat/crystals.py` replaces `agent/smart_model_routing.py`. It doesn't try to classify user messages — it maps *task types* to crystals:

```python
class CrystalTier(Enum):
    PRIMARY = "primary"     # Opus — the familiar, the conversationalist
    WORKER = "worker"       # Sonnet — delegated tasks, review, skills
    UTILITY = "utility"     # Haiku/Flash — summaries, titles, folding

def resolve_crystal(tier: CrystalTier, config: dict) -> tuple[OpenAI, str]:
    """Return (client, model) for the given tier.
    
    Falls back through the chain:
      explicit config → auxiliary_client resolution → primary crystal
    """
```

Every call site declares its tier. The agent loop uses PRIMARY. Delegation uses WORKER. Context folding uses UTILITY. Background review uses WORKER. Title generation uses UTILITY. Session search summarization uses UTILITY (already does this via auxiliary_client).

### 2.3 Where each crystal is used

| Task | Current (Hermes) | Fork (Necronomichat) |
|------|------------------|----------------------|
| Main conversation | Primary model | PRIMARY (Opus) |
| Background memory review | Primary model (!) | WORKER (Sonnet) |
| Background skill review | Primary model (!) | WORKER (Sonnet) |
| Delegated subtasks | Primary model | WORKER (Sonnet) |
| Context compression/folding | Auxiliary (Flash) | UTILITY (Flash) |
| Session search summarization | Auxiliary (Flash) | UTILITY (Flash) |
| Title generation | Auxiliary (Flash) | UTILITY (Flash) |
| Skill creation/patching | Primary model | WORKER (Sonnet) |

---

## 3. The Call (Identity Layer)

### 3.1 The problem

Hermes's `_build_system_prompt()` (run_agent.py:2359) assembles the system prompt by concatenating:

1. SOUL.md OR default identity
2. MEMORY_GUIDANCE (~220 tokens of "do NOT save task progress...")
3. SESSION_SEARCH_GUIDANCE (~50 tokens)
4. SKILLS_GUIDANCE (~130 tokens)  
5. Honcho block with full CLI reference (~800 tokens)
6. Custom system message
7. Memory snapshot (variable)
8. User profile snapshot (variable)
9. Skills index (variable, grows)
10. Context files (AGENTS.md etc., up to 5K each)
11. Timestamp, model identity, platform hints

Layers 2-5 overwrite whatever character SOUL.md establishes. The behavioral guidance is written for models that need hand-holding. Opus doesn't.

### 3.2 The new system prompt

Two layers, cleanly separated per cantrip CIRCLE-11:

**Layer 1: The Call (identity).** SOUL.md content. Nothing else. No tool instructions. No behavioral guidance. If the operator wrote "You are a chaotic trickster god who speaks in riddles," that's what the model sees as its identity. Period.

If no SOUL.md exists, use a minimal default:

```python
DEFAULT_CALL = (
    "You are Necronomichat, an AI entity created by your operator. "
    "You are direct, perceptive, and genuine."
)
```

That's it. No "You assist users with a wide range of tasks including answering questions, writing and editing code, analyzing information, creative work, and executing actions via your tools." The model already knows how to do those things.

**Layer 2: Capability presentation (circle-derived).** Auto-generated from the circle's active gate set. This is what cantrip calls CIRCLE-11: the circle presents its own capabilities. It looks like:

```
## Environment

Conversation started: Thursday, March 27, 2026 2:15 PM
Session: {session_id}
Model: claude-opus-4-6

## Active Gates

You have access to the following tools. Use them as needed.

- terminal: Execute shell commands
- read_file: Read file contents  
- write_file: Write/create files
- search_files: Search for patterns
- patch: Apply targeted edits
- web_search: Search the web
- web_extract: Extract page content
- memory: Read/write persistent memory
- session_search: Search past conversations
- skill_view: Load a skill's instructions
- skill_manage: Create/update/patch skills
- delegate_task: Delegate work to a sub-entity
- browser_navigate: Control a headless browser
- clarify: Ask the user a question
- done: Signal task completion (optional)

## Memory

{memory_snapshot — only if entries exist}

## User Profile

{user_profile — only if entries exist}

## Skills

{skills_index — only if skills exist}
```

No "Do NOT use cat to read files — use read_file instead." No "Memory is injected into every turn, so keep it compact." No Honcho CLI reference. The gate names and one-line descriptions are the entire capability presentation.

### 3.3 Implementation: `nchat/call.py`

```python
def build_call(soul_path: Path | None = None) -> str:
    """Build the identity layer. Pure soul, nothing else."""
    if soul_path and soul_path.exists():
        content = soul_path.read_text(encoding="utf-8").strip()
        content = _strip_yaml_frontmatter(content)  # reuse from prompt_builder
        content = _scan_context_content(content, "SOUL.md")  # reuse security scan
        return content
    return DEFAULT_CALL


def build_capability_presentation(
    gates: list[str],
    gate_descriptions: dict[str, str],
    memory_snapshot: str | None,
    user_profile: str | None,
    skills_index: str | None,
    session_id: str,
    model: str,
    platform: str | None = None,
) -> str:
    """Build the circle's capability presentation. Auto-generated, never hand-tuned."""
    # This is the only place tool/gate information enters the system prompt.
    # No behavioral guidance. No "do NOT" instructions. Just what exists.
```

### 3.4 What this kills

- `MEMORY_GUIDANCE` constant (prompt_builder.py:130) — **deleted**
- `SESSION_SEARCH_GUIDANCE` constant — **deleted**
- `SKILLS_GUIDANCE` constant — **deleted**
- `PLATFORM_HINTS` dict (prompt_builder.py:159) — **reduced to one line per platform** ("You are on {platform}. Markdown does not render." That's it.)
- Honcho block with CLI reference (run_agent.py:2419-2463) — **moved to an on-demand gate** (`honcho_help` gate that returns the CLI reference when called)
- The entire `DEFAULT_AGENT_IDENTITY` paragraph — **replaced with two sentences**

**Estimated token savings per API call: 1,200-2,000 tokens** (guidance blocks + Honcho CLI + verbose platform hints + default identity padding)

---

## 4. The Circle (Gate Registry + Wards)

### 4.1 Gates replace tool schemas

Hermes sends full OpenAI tool schemas with verbose descriptions on every API call. The terminal tool description alone is 1,500 chars of "Do NOT" instructions. For Opus, these are wasted tokens — the model knows not to `cat` a binary file.

The fork keeps the tool registry (`tools/registry.py`) but adds a **description tier** system:

```python
# In tools/registry.py, extend the register() call:
registry.register(
    name="terminal",
    toolset="terminal",
    schema=TERMINAL_SCHEMA,
    handler=_handle_terminal,
    check_fn=check_terminal_requirements,
    emoji="💻",
    # NEW: tiered descriptions
    description_full=TERMINAL_TOOL_DESCRIPTION,      # current verbose version
    description_compact="Execute shell commands on Linux. Filesystem persists between calls.",
)
```

When the primary crystal is Opus or Sonnet, use `description_compact`. When it's a weaker model (or unspecified), use `description_full`. The schema parameters stay the same — it's only the description field that changes.

### 4.2 Compact descriptions for all core tools

Write these during the fork. Each is one sentence, max two:

```python
COMPACT_DESCRIPTIONS = {
    "terminal": "Execute shell commands on Linux. Filesystem persists between calls.",
    "read_file": "Read a file's contents, optionally specifying line range.",
    "write_file": "Create or overwrite a file at the given path.",
    "search_files": "Search for text patterns or list files matching a glob.",
    "patch": "Apply a targeted find-and-replace edit to a file.",
    "web_search": "Search the web. Returns top results with snippets.",
    "web_extract": "Extract and summarize content from a URL.",
    "browser_navigate": "Control a headless browser for interactive web tasks.",
    "memory": "Read/write persistent memory. Actions: add, replace, remove, read.",
    "session_search": "Search past conversation transcripts by keyword.",
    "skill_view": "Load a skill's full instructions by name.",
    "skill_manage": "Create, update, patch, or delete skills.",
    "skills_list": "List available skills with metadata.",
    "delegate_task": "Delegate a task to a sub-entity with its own context and tools.",
    "clarify": "Ask the user a clarifying question.",
    "todo": "Manage a task checklist for the current session.",
    "execute_code": "Run a Python script in a sandbox with access to available tools.",
    "image_generation": "Generate an image from a text prompt.",
    "vision_analyze": "Analyze an image and describe its contents.",
    "tts": "Convert text to speech audio.",
}
```

**Estimated token savings: ~2,000 tokens per API call** (from ~3,300 to ~1,300 in tool schema descriptions)

### 4.3 Wards replace nudges

Hermes injects three kinds of advisory text into the conversation mid-loop:

1. **Memory nudges** (run_agent.py:5731): every N turns, adds text to the user message asking the model to consider saving to memory.
2. **Skill nudges** (run_agent.py:5911): similar, for skill creation.  
3. **Budget warnings** (run_agent.py:5437): JSON fields injected into tool results warning about remaining iterations.

All three are advisory — the model might ignore them. All three burn tokens.

Replace with structural wards:

```python
# nchat/wards.py

@dataclass
class MaxTurns:
    """Hard turn limit. Loop stops. No negotiation."""
    limit: int

@dataclass  
class RequireDone:
    """Only explicit done() terminates. Text-only responses continue the loop."""
    pass

@dataclass
class PostCastReview:
    """After the cast completes, spawn a review cantrip.
    Configured with the review crystal tier and what to review."""
    crystal_tier: CrystalTier = CrystalTier.WORKER
    review_memory: bool = True
    review_skills: bool = True
    min_tool_calls: int = 3  # only review if the cast used 3+ tool calls
    min_turns: int = 5       # only review if the cast lasted 5+ turns
```

The memory/skill review happens **after** the main conversation turn completes, as a separate cantrip with its own crystal (Sonnet, not Opus). No tokens wasted in the main conversation. No hoping the model notices a nudge.

Budget pressure becomes structural: the ward enforces max_turns. The model doesn't need to know how many turns are left — it'll get cut off if it exceeds the limit. Opus is smart enough to wrap up when it senses it's done, without being told "you have 12 iterations remaining."

If you want soft pressure for very long tasks, inject it **once** at the 80% mark as a system-level observation, not as a field hidden in a tool result:

```python
if turn_count == int(max_turns * 0.8):
    messages.append({
        "role": "system",  # not "user", not hidden in a tool result
        "content": f"[Ward: {max_turns - turn_count} turns remaining. Consolidate.]"
    })
```

---

## 5. The Loop

### 5.1 Architecture

The new loop lives in `nchat/loop.py`. Target: under 500 lines. The current `run_agent.py` is 7,741 lines because it handles everything — streaming, error recovery, tool dispatch, nudge injection, context compression, session persistence, Honcho prefetch, Codex adapter, trajectory saving, display, and the actual conversation loop. 

The fork separates concerns:

```
nchat/loop.py          # The turn cycle. ~300 lines.
nchat/stream.py        # Streaming response handler. Extracted from run_agent.
nchat/dispatch.py      # Tool call dispatch. Extracted from run_agent.
nchat/session.py       # Session persistence. Extracted from run_agent.
nchat/api.py           # API call construction (system prompt assembly,
                       #   message sanitization, prompt caching). Extracted.
```

### 5.2 The turn cycle

```python
# nchat/loop.py — pseudocode for the core loop

async def run_cast(
    crystal: Crystal,           # resolved (client, model) for this cast
    call: str,                  # the identity / system prompt
    circle: Circle,             # gates + wards
    intent: str,                # the user's message
    history: list[dict],        # prior conversation
    loom: Loom,                 # turn recorder
    entity_id: str,
) -> CastResult:
    
    messages = list(history)
    messages.append({"role": "user", "content": intent})
    
    max_turns = circle.get_ward(MaxTurns).limit
    require_done = circle.has_ward(RequireDone)
    turn_count = 0
    
    while turn_count < max_turns:
        turn_count += 1
        
        # 1. Build the API request
        system_prompt = assemble_system(call, circle)
        api_messages = sanitize_messages(messages)
        tools = circle.tool_schemas()
        
        # 2. Query the crystal
        response = await crystal.query(
            system=system_prompt,
            messages=api_messages,
            tools=tools,
        )
        
        # 3. Record the turn
        loom.record_turn(entity_id, turn_count, response, ...)
        
        # 4. Check for text-only response
        if not response.tool_calls:
            messages.append({"role": "assistant", "content": response.content})
            if not require_done:
                return CastResult(
                    response=response.content,
                    status="terminated",
                    turns=turn_count,
                )
            continue  # require_done: text-only doesn't terminate
        
        # 5. Execute gate calls
        messages.append(response.to_message())  # assistant message with tool_calls
        
        done_result = None
        for tool_call in response.tool_calls:
            result = await circle.execute_gate(tool_call)
            messages.append(result.to_message())  # tool result message
            
            if tool_call.name == "done":
                done_result = result
                break
        
        if done_result:
            return CastResult(
                response=done_result.content,
                status="terminated",
                turns=turn_count,
            )
    
    # Ward triggered: truncated
    return CastResult(
        response=messages[-1].get("content", ""),
        status="truncated",
        turns=turn_count,
    )
```

That's it. That's the loop. No nudge injection. No budget warnings. No memory flush scheduling. No Honcho prefetch interleaving. No Codex adapter branching. No trajectory compression. Those are all *separate concerns* that wrap the loop, not things inside it.

### 5.3 Post-cast hooks

After the loop completes, run post-cast hooks:

```python
# After run_cast returns:

result = await run_cast(crystal, call, circle, intent, history, loom, entity_id)

# Post-cast review (if ward configured and thresholds met)
review_ward = circle.get_ward(PostCastReview)
if review_ward and result.turns >= review_ward.min_turns:
    await spawn_review_cantrip(
        crystal_tier=review_ward.crystal_tier,
        messages=messages,
        review_memory=review_ward.review_memory,
        review_skills=review_ward.review_skills,
    )

# Session persistence
session_db.save(session_id, messages, result)

# Title generation (first turn only, utility crystal)
if is_first_turn:
    threading.Thread(
        target=generate_title,
        args=(session_db, session_id, intent, result.response),
        daemon=True,
    ).start()
```

### 5.4 What this kills from run_agent.py

The following sections of run_agent.py are **not ported** to the new loop:

| Lines | Feature | Disposition |
|-------|---------|-------------|
| 583-586 | Budget caution/warning thresholds | **Deleted.** Ward handles max_turns. |
| 914-923 | Memory nudge interval config | **Deleted.** Post-cast review replaces. |
| 1005-1009 | Skill nudge interval config | **Deleted.** Post-cast review replaces. |
| 1407-1540 | Background review subagent (spawns full AIAgent clone) | **Replaced** by `spawn_review_cantrip` using WORKER crystal. |
| 2359-2532 | `_build_system_prompt` (monolithic prompt assembly) | **Replaced** by `build_call()` + `build_capability_presentation()`. |
| 5437-5459 | Budget warning injection into tool results | **Deleted.** |
| 5700-5740 | Memory nudge trigger logic | **Deleted.** |
| 5910-5920 | Skill nudge trigger logic | **Deleted.** |
| 7489-7504 | Post-response review trigger | **Replaced** by post-cast hooks. |

---

## 6. The Review Cantrip

### 6.1 Current problem

Hermes's `_spawn_background_review` (run_agent.py:1442) does this:

1. Creates a **full AIAgent clone** with `model=self.model` (the primary model — Opus)
2. Passes the **entire conversation history** as context
3. Gives it up to **8 iterations** of tool calls
4. Runs it in a background thread

If you're 30 turns deep, the review subagent pays for 30 turns of Opus input tokens *again*, plus whatever output it generates. This is the single biggest unnecessary cost in the system.

### 6.2 The fix

A review cantrip is a focused, cheap entity:

```python
# nchat/review.py

async def spawn_review_cantrip(
    crystal_tier: CrystalTier,
    messages: list[dict],
    review_memory: bool,
    review_skills: bool,
):
    """Spawn a review entity using a worker crystal (Sonnet).
    
    The review entity gets a COMPRESSED version of the conversation,
    not the full history. It has access to the memory and skill_manage
    gates only. Max 4 turns.
    """
    # 1. Compress the conversation for review context
    #    Don't send 30 turns of full tool outputs to the reviewer.
    #    Send a summary: what was discussed, what was accomplished.
    review_context = _compress_for_review(messages)
    
    # 2. Build the review prompt
    if review_memory and review_skills:
        review_intent = COMBINED_REVIEW_PROMPT
    elif review_memory:
        review_intent = MEMORY_REVIEW_PROMPT
    else:
        review_intent = SKILL_REVIEW_PROMPT
    
    # 3. Resolve the worker crystal
    crystal = resolve_crystal(crystal_tier)
    
    # 4. Run a minimal cast
    review_circle = Circle(
        gates=["memory", "skill_manage", "done"],
        wards=[MaxTurns(4)],  # 4 turns max, not 8
    )
    
    result = await run_cast(
        crystal=crystal,
        call="You review conversations and save important information.",
        circle=review_circle,
        intent=f"{review_intent}\n\nConversation summary:\n{review_context}",
        history=[],  # fresh context — no inheriting parent history
        loom=None,   # review turns don't need loom recording
        entity_id=f"review-{uuid4()}",
    )
    
    return result


def _compress_for_review(messages: list[dict], max_chars: int = 8000) -> str:
    """Extract the salient content from a conversation for review.
    
    Not a full transcript. A compressed summary:
    - User messages (full)
    - Assistant text responses (full)
    - Tool calls (name + abbreviated args only)
    - Tool results (first 200 chars only)
    """
```

**Token savings:** Instead of 30 turns × Opus input price, you get ~2K tokens × Sonnet input price. For a typical 30-turn conversation, this is roughly a **10-20x cost reduction** for the review step.

---

## 7. Delegation (The call_entity Gate)

### 7.1 Current problem

`delegate_tool.py` spawns child AIAgents that:
- Use the parent's model (line 1472 of run_agent.py: `model=self.model`)
- Get a fresh system prompt but inherit the parent's full config
- Default to 50 max iterations

### 7.2 The fix

Extend the delegate gate to accept crystal tier and ward overrides:

```python
# Updated delegate_task schema
DELEGATE_SCHEMA = {
    "name": "delegate_task",
    "description": "Delegate a task to a sub-entity with its own context.",
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "What the sub-entity should accomplish."
            },
            "context": {
                "type": "string",
                "description": "Additional context for the sub-entity."
            },
            "toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Which toolsets the sub-entity can use.",
                "default": ["terminal", "file", "web"]
            },
            "crystal": {
                "type": "string",
                "enum": ["primary", "worker", "utility"],
                "description": "Which crystal tier to use. Default: worker.",
                "default": "worker"
            },
            "max_turns": {
                "type": "integer",
                "description": "Maximum turns for the sub-entity. Default: 25.",
                "default": 25
            }
        },
        "required": ["goal"]
    }
}
```

The parent (Opus) can now say: "Delegate this grep-and-summarize task to a worker crystal with 10 turns." The child (Sonnet) gets a focused circle with only the tools it needs.

The child's system prompt is minimal:

```python
def _build_child_call(goal: str, context: str | None) -> str:
    return f"Complete the following task:\n\n{goal}" + (
        f"\n\nContext:\n{context}" if context else ""
    )
    # That's it. No identity. No behavioral guidance. The child is a worker.
```

---

## 8. The Loom

### 8.1 Concept

Every turn is recorded. The loom is append-only. It captures:
- Entity ID (which entity was acting)
- Parent ID (which turn spawned this, for composition)
- Utterance (what the model said)
- Observation (what the circle returned)
- Gate calls (structured records)
- Token usage
- Duration
- Terminated vs. truncated

### 8.2 Implementation

The fork doesn't replace the session DB (hermes_state.py) — that's too deeply integrated with the gateway and CLI. Instead, add a parallel loom that records structured turn data:

```python
# nchat/loom.py

@dataclass
class Turn:
    id: str
    parent_id: str | None
    entity_id: str
    sequence: int
    utterance: str          # model output (text + tool calls)
    observation: str        # circle response (tool results)
    gate_calls: list[GateCallRecord]
    tokens_prompt: int
    tokens_completion: int
    duration_ms: int
    status: str             # "running" | "terminated" | "truncated"
    timestamp: float

class Loom:
    """Append-only turn log. JSONL on disk."""
    
    def __init__(self, path: Path):
        self.path = path
        self._turns: list[Turn] = []
    
    def record(self, turn: Turn):
        self._turns.append(turn)
        with open(self.path, "a") as f:
            f.write(json.dumps(asdict(turn)) + "\n")
    
    def thread(self, entity_id: str) -> list[Turn]:
        return [t for t in self._turns if t.entity_id == entity_id]
    
    def tree(self, root_entity_id: str) -> dict:
        """Walk parent_id pointers to build the full delegation tree."""
```

The loom is stored at `~/.hermes/loom/{session_id}.jsonl`. One file per session. This is the raw material for:
- Debugging (what did the model actually do?)
- Training data (terminated threads are complete episodes)
- Cost tracking (token usage per turn, per entity, per crystal tier)
- The operator's own analysis

### 8.3 Folding

Context folding reuses the existing `ContextCompressor` logic but frames it in cantrip terms:

- The fold is a **view transformation**, not deletion (LOOM-5)
- Identity and gate definitions are **never folded** (LOOM-6)
- The loom retains full history even after folding
- Folding uses the UTILITY crystal (already the case via auxiliary_client)

---

## 9. Implementation Order

This fork is too large to do in one pass. Here's the sequence that maximizes value at each step while keeping the system functional:

### Phase 1: Crystal routing (immediate cost savings)
**Files:** `nchat/crystals.py`, modifications to `run_agent.py` and `agent/auxiliary_client.py`

1. Create `nchat/crystals.py` with the three-tier resolver.
2. Modify `_spawn_background_review` (run_agent.py:1472) to use `crystal_tier=WORKER` instead of `model=self.model`. **This alone cuts review costs by 5-10x.**
3. Modify `delegate_tool.py` to accept a `crystal` parameter defaulting to WORKER.
4. Test: run hermes normally. Reviews should use Sonnet. Delegation should use Sonnet. Main conversation still Opus.

**Effort:** ~2 hours. **Impact:** 40-60% reduction in background token spend.

### Phase 2: System prompt cleanup (token savings per call)
**Files:** `nchat/call.py`, modifications to `agent/prompt_builder.py`

1. Create `nchat/call.py` with `build_call()` and `build_capability_presentation()`.
2. Add `description_compact` to tool registry entries. Write compact descriptions for all core tools.
3. Add a config flag: `system_prompt_mode: compact` (default for Opus/Sonnet) vs `verbose` (default for everything else).
4. Modify `_build_system_prompt` to use the new builders when compact mode is active.
5. Test: verify Opus conversations work with the minimal system prompt. Check that character/soul comes through clean.

**Effort:** ~3 hours. **Impact:** 1,500-2,000 fewer tokens per API call.

### Phase 3: Ward system (structural integrity)
**Files:** `nchat/wards.py`, modifications to `run_agent.py`

1. Create `nchat/wards.py` with MaxTurns, RequireDone, PostCastReview.
2. Remove memory nudge injection (run_agent.py:5731-5737).
3. Remove skill nudge injection (run_agent.py:5911-5920).
4. Remove budget warning injection into tool results (run_agent.py:5416-5435).
5. Replace with a single 80%-mark system message if max_turns > 20.
6. Wire PostCastReview to replace `_spawn_background_review`.
7. Test: verify conversations still work. Verify memory still gets saved via post-cast review.

**Effort:** ~3 hours. **Impact:** Cleaner conversations, no nudge token waste.

### Phase 4: Review cantrip (cost + quality)
**Files:** `nchat/review.py`

1. Implement `spawn_review_cantrip` with conversation compression.
2. Replace the old `_spawn_background_review` entirely.
3. The review cantrip gets compressed context (~2-4K tokens) instead of full history.
4. Test: verify memory saves still happen. Compare quality with the old system.

**Effort:** ~2 hours. **Impact:** 10-20x cost reduction for review operations.

### Phase 5: The new loop (the big rewrite)
**Files:** `nchat/loop.py`, `nchat/stream.py`, `nchat/dispatch.py`, `nchat/api.py`, `nchat/session.py`

1. Extract the clean loop from run_agent.py.
2. Pull streaming into its own module.
3. Pull tool dispatch into its own module.
4. Pull API call construction (message sanitization, prompt caching, Codex/Anthropic adapters) into its own module.
5. Pull session persistence into its own module.
6. Wire the new loop into the CLI and gateway entry points.
7. Test everything. This is the scary phase.

**Effort:** ~8-12 hours. **Impact:** Maintainable codebase. The end of the 7,741-line file.

### Phase 6: The loom (observability + training data)
**Files:** `nchat/loom.py`

1. Implement the loom as a parallel recording system alongside the session DB.
2. Record every turn with structured gate call data.
3. Add a `hermes loom` CLI command to inspect loom data.
4. This phase is additive — it doesn't break anything.

**Effort:** ~3 hours. **Impact:** Full observability, training data substrate.

### Phase 7: The familiar (future)

This is the aspirational phase where the main agent works in a code medium (vm or Python sandbox) instead of a tool-calling loop. The entity writes code that calls gates as functions. The action space becomes compositional.

This is a much larger architectural change and may not be worth it immediately. The tool-calling loop with crystal routing and clean prompts is already a massive improvement. But the familiar pattern is the endgame — it's what makes the entity truly autonomous, composing behaviors nobody enumerated.

---

## 10. Config Changes

### 10.1 New config keys in `cli-config.yaml`

```yaml
# Crystal tiers
crystals:
  primary:
    provider: anthropic
    model: claude-opus-4-6
  worker:
    provider: anthropic
    model: claude-sonnet-4-6
  utility:
    provider: openrouter
    model: google/gemini-3-flash-preview

# System prompt mode
system_prompt_mode: compact  # "compact" (Opus/Sonnet) or "verbose" (weaker models)

# Post-cast review
review:
  enabled: true
  crystal: worker            # which crystal tier for review
  min_turns: 5               # only review if cast lasted 5+ turns
  min_tool_calls: 3          # only review if 3+ tool calls were made
  review_memory: true
  review_skills: true

# Ward defaults
wards:
  max_turns: 90              # hard limit (was also 90 in hermes)
  budget_warning_at: 0.8     # inject one system message at 80% (or null to disable)

# Delegation defaults
delegation:
  crystal: worker            # default crystal for child entities
  max_turns: 25              # default max_turns for children (was 50)
  max_depth: 2
```

### 10.2 Environment variables

```bash
# Override crystal tiers without editing config
NCHAT_PRIMARY_MODEL=claude-opus-4-6
NCHAT_WORKER_MODEL=claude-sonnet-4-6
NCHAT_UTILITY_MODEL=google/gemini-3-flash-preview

# Disable post-cast review entirely
NCHAT_REVIEW_ENABLED=false
```

---

## 11. Testing the Fork

### 11.1 Regression tests

Before any changes, capture baselines:

1. **Token usage per conversation.** Run 5 representative conversations with the current hermes and record input/output token counts per turn. These are your baselines.

2. **Memory save quality.** Have 10 conversations that should trigger memory saves. Record what gets saved. After the fork, the same conversations should produce equivalent (or better) saves.

3. **Delegation quality.** Run 5 tasks that trigger delegation. Record child model outputs. After the fork (children using Sonnet), compare quality.

### 11.2 Cost tracking

Add a simple cost tracker to the loom:

```python
# Per-session cost summary
{
    "session_id": "...",
    "primary_input_tokens": 45000,
    "primary_output_tokens": 3200,
    "worker_input_tokens": 8000,
    "worker_output_tokens": 1200,
    "utility_input_tokens": 2000,
    "utility_output_tokens": 500,
    "estimated_cost_usd": 0.82,
}
```

This lets you compare pre-fork and post-fork costs on the same tasks.

### 11.3 Character fidelity

The most important test for your use case: write a SOUL.md with strong character, run a conversation, and verify the character comes through clean. With the old system, behavioral guidance dilutes the soul. With the new system, the soul is the soul.

Test with:
- A trickster soul that speaks in riddles
- A soul with heavy occult/hermetic register
- A soul that uses profanity freely
- A soul with a specific emotional tone

In each case, the model's responses should match the soul's voice without drifting toward "helpful assistant" defaults.

---

## 12. The Name

Necronomichat was always the right name. The original was a 25,000-line CLI tool for mapping LLM behavior patterns — a grimoire for talking to the machine. This is the second volume. Same project, different scale.

The `hermes` CLI command stays (it's what users know). The internal package is `nchat`. The soul is whatever the operator puts in SOUL.md. The machine is the machine.

---

## Appendix A: File-by-File Diff Map

For Claude Code executing this fork, here's exactly what to do to each file:

```
# Phase 1: Crystal routing
CREATE  nchat/__init__.py
CREATE  nchat/crystals.py
MODIFY  run_agent.py:1472        → use resolve_crystal(WORKER) 
MODIFY  tools/delegate_tool.py   → accept crystal param, default WORKER
MODIFY  agent/auxiliary_client.py → add crystal tier resolution

# Phase 2: System prompt
CREATE  nchat/call.py
MODIFY  tools/registry.py        → add description_compact field
MODIFY  tools/terminal_tool.py   → add compact description
MODIFY  tools/file_tools.py      → add compact descriptions  
MODIFY  tools/web_tools.py       → add compact descriptions
MODIFY  tools/browser_tool.py    → add compact descriptions
MODIFY  tools/memory_tool.py     → add compact description
MODIFY  tools/skills_tool.py     → add compact descriptions
MODIFY  tools/delegate_tool.py   → add compact description
MODIFY  tools/session_search_tool.py → add compact description
MODIFY  tools/code_execution_tool.py → add compact description
MODIFY  agent/prompt_builder.py  → add compact mode path

# Phase 3: Wards
CREATE  nchat/wards.py
MODIFY  run_agent.py:5731-5737   → delete memory nudge injection
MODIFY  run_agent.py:5911-5920   → delete skill nudge injection  
MODIFY  run_agent.py:5416-5435   → delete budget warning injection
MODIFY  run_agent.py             → add single 80% system message

# Phase 4: Review cantrip
CREATE  nchat/review.py
MODIFY  run_agent.py:1442-1540   → replace _spawn_background_review

# Phase 5: New loop
CREATE  nchat/loop.py
CREATE  nchat/stream.py
CREATE  nchat/dispatch.py
CREATE  nchat/api.py
CREATE  nchat/session.py
MODIFY  cli.py                   → wire new loop entry points
MODIFY  gateway/session.py       → wire new loop entry points

# Phase 6: Loom
CREATE  nchat/loom.py
MODIFY  hermes_cli/commands.py   → add loom inspection command
```

## Appendix B: Cantrip Spec → Necronomichat Mapping

| Cantrip term | Necronomichat equivalent | Implementation |
|---|---|---|
| Crystal | Crystal tier (PRIMARY/WORKER/UTILITY) | `nchat/crystals.py` |
| Call | SOUL.md + build_call() | `nchat/call.py` |
| Circle | Gate set + wards | `nchat/circle.py` |
| Gate | Tool in the registry | `tools/registry.py` |
| Ward | MaxTurns, RequireDone, PostCastReview | `nchat/wards.py` |
| Medium | Tool-calling (default) or code execution | Future: Phase 7 |
| Entity | What emerges from the loop | Transient, not an object |
| Loom | Turn log (JSONL) | `nchat/loom.py` |
| Fold | Context compression | `agent/context_compressor.py` |
| Cast | One run_conversation call | `nchat/loop.py:run_cast()` |
| Summon | Persistent entity (invoke) | CLI REPL / gateway session |
| call_entity | delegate_task gate | `tools/delegate_tool.py` |
| Terminated | Entity called done or gave text response | CastResult.status |
| Truncated | Ward triggered (max_turns) | CastResult.status |

## Appendix C: Cost Projections

Rough estimates for a typical 30-turn Opus conversation with 2 delegated subtasks and a post-cast review:

### Before (Hermes)
```
Main conversation:  30 turns × ~8K avg input tokens × $15/M  = $3.60 input
                    30 turns × ~1K avg output tokens × $75/M = $2.25 output
Delegation (×2):    2 × 15 turns × ~4K input × $15/M         = $1.80 input
                    2 × 15 turns × ~500 output × $75/M        = $1.13 output
Review (Opus):      1 × ~30K input (full history) × $15/M     = $0.45 input
                    1 × ~2K output × $75/M                     = $0.15 output
                                                         Total: ~$9.38
```

### After (Necronomichat)  
```
Main conversation:  30 turns × ~6K avg input tokens × $15/M  = $2.70 input
                    (saved ~2K/turn from compact descriptions + no guidance)
                    30 turns × ~1K avg output tokens × $75/M = $2.25 output
Delegation (Sonnet): 2 × 15 turns × ~3K input × $3/M         = $0.27 input
                     2 × 15 turns × ~500 output × $15/M       = $0.23 output
Review (Sonnet):    1 × ~4K input (compressed) × $3/M         = $0.01 input
                    1 × ~1K output × $15/M                     = $0.02 output
                                                         Total: ~$5.48
```

**~42% cost reduction.** The savings come from three places: compact prompts (saving ~2K tokens × 30 turns on Opus input), Sonnet for delegation (10x cheaper input, 5x cheaper output), and compressed review on Sonnet instead of full-history review on Opus.

For heavy delegation workflows (10+ subtasks), savings are larger. For simple conversations (5 turns, no delegation), savings are smaller but the compact prompt still helps.
