"""Context folding — the cantrip spec's §6.8 made real.

Folding is NOT compaction. Compaction summarizes and discards. Folding
integrates old turns into circle state. In code medium, the sandbox IS
circle state — variables persist. In conversation medium, the fold summary
carries forward what matters: decisions, file state, progress.

From the cantrip spec:
  "Folding is the deliberate integration of loom history into circle state.
   Instead of keeping every prior turn in the message list, the circle takes
   the substance of earlier turns and encodes it as state the entity can
   access through code: variables, data structures, summaries in the sandbox."

Supports two mediums:
  - Code medium: sandbox retains variables, fold summary references them
  - Conversation medium: fold summary preserves tool results, decisions, file state

Invariants (from spec):
  LOOM-5: Folding MUST NOT destroy history. Full turns remain in the loom.
  LOOM-6: Folding MUST NOT compress the identity or gate definitions.
  Fidelity: Folded summaries MUST be explicitly marked.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Fold summary generation ──────────────────────────────────────────

_FOLD_PROMPT_TEMPLATE = """You are generating a context fold summary for a code medium entity.

The entity writes Python programs in a persistent sandbox. Variables from folded turns
STILL EXIST in the sandbox — they are not lost. The fold summary tells the entity
what it accomplished and what state is available, so it can continue without re-reading
the full execution history.

TURNS BEING FOLDED (turns {start} through {end}):
{serialized_turns}

SANDBOX STATE (variables currently available):
{sandbox_state}

Write a concise fold summary using this format:

## Folded: turns {start}-{end}

**What was accomplished:**
- [Bullet each significant action: files read, data processed, APIs called, etc.]

**Sandbox state available:**
- [List key variables with their types and what they contain]
- [Only include variables the entity is likely to reference again]

**Decisions made:**
- [Any choices or approaches the entity committed to]

**Errors encountered and resolved:**
- [Only if relevant to ongoing work]

Rules:
- Be CONCISE. The whole point of folding is to shrink context.
- Focus on WHAT STATE EXISTS, not the step-by-step of how it was created.
- The entity can introspect variables with code — don't reproduce data, just name it.
- Target 200-400 tokens. Shorter is better if nothing complex happened.
- Do NOT include any preamble. Start directly with "## Folded:"."""


_CONVERSATION_FOLD_PROMPT_TEMPLATE = """You are generating a context fold summary for a conversation-medium entity (an AI assistant using tool calls).

The entity uses structured tool calls (read_file, terminal, patch, web_search, etc.) to accomplish tasks. Earlier turns are being folded to reduce context size. The fold summary must preserve enough information that the entity can continue its work without repeating actions.

TURNS BEING FOLDED (turns {start} through {end}):
{serialized_turns}

Write a concise fold summary using this format:

## Folded: turns {start}-{end}

**What was accomplished:**
- [Bullet each significant action: files read/written, commands run, searches performed, etc.]

**Key results and findings:**
- [Important outputs, error messages, data discovered — anything the entity may need to reference]

**Files touched:**
- [File paths read, modified, or created — with brief note on what was done to each]

**Decisions made:**
- [Any choices or approaches committed to]

Rules:
- Be CONCISE. The whole point of folding is to shrink context.
- Preserve specific values: file paths, error messages, command outputs, variable names.
- The entity can re-read files or re-run commands — don't reproduce full contents, just summarize what was found.
- Target 300-500 tokens. Include more detail than code-medium folds since there's no persistent sandbox state.
- Do NOT include any preamble. Start directly with "## Folded:"."""


def _serialize_turns_for_fold(
    messages: List[Dict[str, Any]],
    start_idx: int,
    end_idx: int,
    is_code_medium: bool = True,
) -> str:
    """Serialize a range of messages into labeled text for the fold summarizer.

    Adapts output based on medium type:
    - Code medium: assistant messages contain execute(code) tool calls
    - Conversation medium: assistant messages contain named tool calls with args
    """
    import json as _json

    parts = []
    for i in range(start_idx, end_idx):
        msg = messages[i]
        role = msg.get("role", "unknown")
        content = msg.get("content", "") or ""

        if role == "assistant":
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                if is_code_medium:
                    # Code medium: extract the code from execute() call
                    tc = tool_calls[0]
                    if isinstance(tc, dict):
                        try:
                            args = _json.loads(tc.get("function", {}).get("arguments", "{}"))
                            code = args.get("code", "")
                        except (_json.JSONDecodeError, TypeError):
                            code = tc.get("function", {}).get("arguments", "")
                    else:
                        code = ""
                    if len(code) > 600:
                        code = code[:400] + "\n# ...[truncated]...\n" + code[-150:]
                    parts.append(f"[CODE TURN]: {code}")
                else:
                    # Conversation medium: show tool name + truncated args
                    tc_lines = []
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            fn = tc.get("function", {})
                            name = fn.get("name", "?")
                            args = fn.get("arguments", "")
                            if len(args) > 300:
                                args = args[:250] + "..."
                            tc_lines.append(f"  {name}({args})")
                        else:
                            fn = getattr(tc, "function", None)
                            name = getattr(fn, "name", "?") if fn else "?"
                            tc_lines.append(f"  {name}(...)")
                    if content:
                        tc_lines.insert(0, content[:300])
                    parts.append(f"[ASSISTANT tool calls]:\n" + "\n".join(tc_lines))
            elif content:
                parts.append(f"[ASSISTANT]: {content[:500]}")

        elif role == "tool":
            if len(content) > 400:
                content = content[:300] + "\n...[truncated]..."
            parts.append(f"[TOOL RESULT]: {content}")

        elif role == "system":
            parts.append(f"[SYSTEM]: {content}")

        elif role == "user":
            parts.append(f"[USER]: {content[:300]}")

    return "\n\n".join(parts)


def _format_sandbox_state(variables: Dict[str, Any]) -> str:
    """Format sandbox introspection results for the fold prompt."""
    if not variables:
        return "(no user-defined variables)"
    lines = []
    for name, info in sorted(variables.items()):
        vtype = info.get("type", "unknown")
        if "len" in info:
            lines.append(f"  {name}: {vtype} (len={info['len']})")
        elif "keys" in info:
            lines.append(f"  {name}: {vtype} ({info['keys']} keys)")
        elif "value" in info:
            lines.append(f"  {name}: {vtype} = {info['value']}")
        else:
            lines.append(f"  {name}: {vtype}")
    return "\n".join(lines)


def generate_fold_summary(
    messages: List[Dict[str, Any]],
    start_idx: int,
    end_idx: int,
    sandbox_variables: Dict[str, Any] | None = None,
    turn_start: int = 1,
    turn_end: int = 0,
    is_code_medium: bool = True,
) -> Optional[str]:
    """Generate a fold summary for a range of turns using a cheap LLM.

    Args:
        messages: Full message list.
        start_idx: Index of first message to fold (inclusive).
        end_idx: Index of last message to fold (exclusive).
        sandbox_variables: Current sandbox state from introspect() (code medium only).
        turn_start: Human-readable turn number for fold label.
        turn_end: Human-readable turn number for fold label.
        is_code_medium: Whether this is code medium (sandbox state) or conversation.

    Returns:
        Fold summary string, or None on failure.
    """
    try:
        from agent.auxiliary_client import call_llm
    except ImportError:
        logger.warning("Cannot generate fold summary: auxiliary_client not available")
        return None

    serialized = _serialize_turns_for_fold(
        messages, start_idx, end_idx, is_code_medium=is_code_medium,
    )

    if is_code_medium:
        sandbox_state = _format_sandbox_state(sandbox_variables or {})
        prompt = _FOLD_PROMPT_TEMPLATE.format(
            start=turn_start,
            end=turn_end,
            serialized_turns=serialized,
            sandbox_state=sandbox_state,
        )
        max_tokens = 800
    else:
        prompt = _CONVERSATION_FOLD_PROMPT_TEMPLATE.format(
            start=turn_start,
            end=turn_end,
            serialized_turns=serialized,
        )
        max_tokens = 1000  # Conversation folds need more detail (no sandbox backup)

    try:
        response = call_llm(
            task="compression",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=max_tokens,
            timeout=30.0,
        )
        content = response.choices[0].message.content
        if not isinstance(content, str):
            content = str(content) if content else ""
        summary = content.strip()
        if summary:
            return summary
        return None
    except Exception as e:
        logger.warning("Failed to generate fold summary: %s", e)
        return None


# ── The fold operation ───────────────────────────────────────────────

def fold_code_context(
    messages: List[Dict[str, Any]],
    sandbox_variables: Dict[str, Any],
    protect_last_n: int = 6,
    protect_first_n: int = 2,
    current_turn: int = 0,
) -> tuple[List[Dict[str, Any]], bool]:
    """Fold old code medium turns into a summary node.

    Replaces messages[protect_first_n:fold_boundary] with a single
    fold summary message. The sandbox retains all state — folding
    only shrinks the prompt.

    Args:
        messages: The working message list (mutated in place).
        sandbox_variables: From medium.introspect_sandbox().
        protect_last_n: Number of recent messages to keep unfolded.
            Default 6 = ~3 code turns (assistant+tool pairs).
        protect_first_n: Number of head messages to protect
            (typically user intent + first assistant response).
        current_turn: Current turn number for labeling.

    Returns:
        (new_messages, did_fold) — new message list and whether folding occurred.
    """
    n = len(messages)

    # Need enough messages to make folding worthwhile
    min_messages = protect_first_n + protect_last_n + 4  # at least 2 turn-pairs to fold
    if n < min_messages:
        return messages, False

    # Boundaries
    fold_start = protect_first_n
    fold_end = n - protect_last_n

    # Align fold_end backward to avoid splitting assistant+tool pairs
    fold_end = _align_to_turn_boundary(messages, fold_end)

    # Align fold_start forward past any orphaned tool results
    while fold_start < fold_end and messages[fold_start].get("role") == "tool":
        fold_start += 1

    if fold_end - fold_start < 4:
        # Not enough turns to justify a fold
        return messages, False

    # Count actual code turns being folded (assistant messages with tool_calls)
    folded_turns = sum(
        1 for i in range(fold_start, fold_end)
        if messages[i].get("role") == "assistant" and messages[i].get("tool_calls")
    )

    # Calculate human-readable turn range
    turn_start = _count_turns_before(messages, fold_start) + 1
    turn_end = turn_start + folded_turns - 1

    logger.info(
        "Folding code context: messages[%d:%d] (%d messages, ~%d code turns). "
        "Protecting first %d and last %d messages.",
        fold_start, fold_end, fold_end - fold_start,
        folded_turns, protect_first_n, protect_last_n,
    )

    # Generate fold summary
    summary = generate_fold_summary(
        messages, fold_start, fold_end,
        sandbox_variables,
        turn_start=turn_start,
        turn_end=turn_end,
    )

    if not summary:
        # Fallback: generate a minimal fold marker without LLM
        var_names = ", ".join(sorted(sandbox_variables.keys())[:15])
        summary = (
            f"## Folded: turns {turn_start}-{turn_end}\n\n"
            f"*{folded_turns} code execution turns folded. "
            f"Sandbox state persists — variables still accessible.*\n\n"
            f"**Sandbox variables:** {var_names or '(none)'}"
        )

    # Assemble new message list
    # Head: protected messages
    new_messages = list(messages[:fold_start])

    # Fold node: insert as user message (since it follows assistant typically)
    # Choose role to avoid consecutive same-role
    prev_role = new_messages[-1].get("role", "user") if new_messages else "user"
    next_role = messages[fold_end].get("role", "assistant") if fold_end < n else "assistant"

    fold_role = "user"
    if prev_role == "user":
        fold_role = "assistant"
    if fold_role == next_role:
        flipped = "assistant" if fold_role == "user" else "user"
        if flipped != prev_role:
            fold_role = flipped

    new_messages.append({
        "role": fold_role,
        "content": summary,
    })

    # Tail: protected recent messages
    new_messages.extend(messages[fold_end:])

    # Sanitize: fix any orphaned tool_call/tool_result pairs
    new_messages = _sanitize_tool_pairs(new_messages)

    logger.info(
        "Fold complete: %d → %d messages (%d removed)",
        n, len(new_messages), n - len(new_messages),
    )

    return new_messages, True


def fold_conversation_context(
    messages: List[Dict[str, Any]],
    protect_last_n: int = 10,
    protect_first_n: int = 2,
    current_turn: int = 0,
) -> tuple[List[Dict[str, Any]], bool]:
    """Fold old conversation medium turns into a summary node.

    Same principle as code medium folding, but without sandbox state.
    The fold summary must carry more detail since there's no persistent
    environment backing it up — the entity loses access to folded tool
    results and must re-call tools if it needs that data again.

    Args:
        messages: The working message list.
        protect_last_n: Number of recent messages to keep unfolded.
            Default 10 — higher than code medium because conversation
            turns reference prior tool results more heavily.
        protect_first_n: Number of head messages to protect.
        current_turn: Current turn number for labeling.

    Returns:
        (new_messages, did_fold) — new message list and whether folding occurred.
    """
    n = len(messages)

    min_messages = protect_first_n + protect_last_n + 6
    if n < min_messages:
        return messages, False

    fold_start = protect_first_n
    fold_end = n - protect_last_n

    fold_end = _align_to_turn_boundary(messages, fold_end)

    while fold_start < fold_end and messages[fold_start].get("role") == "tool":
        fold_start += 1

    if fold_end - fold_start < 6:
        return messages, False

    folded_turns = sum(
        1 for i in range(fold_start, fold_end)
        if messages[i].get("role") == "assistant" and messages[i].get("tool_calls")
    )

    turn_start = _count_turns_before(messages, fold_start) + 1
    turn_end = turn_start + folded_turns - 1

    logger.info(
        "Folding conversation context: messages[%d:%d] (%d messages, ~%d tool turns). "
        "Protecting first %d and last %d messages.",
        fold_start, fold_end, fold_end - fold_start,
        folded_turns, protect_first_n, protect_last_n,
    )

    summary = generate_fold_summary(
        messages, fold_start, fold_end,
        sandbox_variables=None,
        turn_start=turn_start,
        turn_end=turn_end,
        is_code_medium=False,
    )

    if not summary:
        summary = (
            f"## Folded: turns {turn_start}-{turn_end}\n\n"
            f"*{folded_turns} tool-calling turns folded. "
            f"Tool results from these turns are no longer in context — "
            f"re-call tools if you need that data again.*"
        )

    # Assemble new message list
    new_messages = list(messages[:fold_start])

    prev_role = new_messages[-1].get("role", "user") if new_messages else "user"
    next_role = messages[fold_end].get("role", "assistant") if fold_end < n else "assistant"

    fold_role = "user"
    if prev_role == "user":
        fold_role = "assistant"
    if fold_role == next_role:
        flipped = "assistant" if fold_role == "user" else "user"
        if flipped != prev_role:
            fold_role = flipped

    new_messages.append({
        "role": fold_role,
        "content": summary,
    })

    new_messages.extend(messages[fold_end:])
    new_messages = _sanitize_tool_pairs(new_messages)

    logger.info(
        "Conversation fold complete: %d → %d messages (%d removed)",
        n, len(new_messages), n - len(new_messages),
    )

    return new_messages, True


def _align_to_turn_boundary(messages: List[Dict], idx: int) -> int:
    """Pull boundary backward to avoid splitting assistant+tool pairs."""
    while idx > 0 and messages[idx - 1].get("role") == "tool":
        idx -= 1
    # If we're now pointing at an assistant with tool_calls, include it
    if idx > 0 and messages[idx - 1].get("role") == "assistant" and messages[idx - 1].get("tool_calls"):
        idx -= 1
    return idx


def _count_turns_before(messages: List[Dict], idx: int) -> int:
    """Count code turns (assistant messages with tool_calls) before idx."""
    return sum(
        1 for i in range(idx)
        if messages[i].get("role") == "assistant" and messages[i].get("tool_calls")
    )


def _sanitize_tool_pairs(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fix orphaned tool_call / tool_result pairs after folding.

    Same logic as ContextCompressor._sanitize_tool_pairs but standalone.
    """
    # Collect all tool_call IDs from assistant messages
    surviving_call_ids: set = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                cid = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                if cid:
                    surviving_call_ids.add(cid)

    # Collect all tool result call IDs
    result_call_ids: set = set()
    for msg in messages:
        if msg.get("role") == "tool":
            cid = msg.get("tool_call_id")
            if cid:
                result_call_ids.add(cid)

    # Remove orphaned tool results (result exists but no matching call)
    orphaned_results = result_call_ids - surviving_call_ids
    if orphaned_results:
        messages = [
            m for m in messages
            if not (m.get("role") == "tool" and m.get("tool_call_id") in orphaned_results)
        ]
        logger.debug("Fold sanitizer: removed %d orphaned tool result(s)", len(orphaned_results))

    # Add stub results for calls without results
    missing_results = surviving_call_ids - result_call_ids
    if missing_results:
        patched = []
        for msg in messages:
            patched.append(msg)
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    cid = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                    if cid in missing_results:
                        patched.append({
                            "role": "tool",
                            "tool_call_id": cid,
                            "content": "[Result from folded turns — see fold summary above]",
                        })
        messages = patched
        logger.debug("Fold sanitizer: added %d stub tool result(s)", len(missing_results))

    return messages


# ── Fold trigger logic ───────────────────────────────────────────────

def should_fold(
    turn_count: int,
    total_messages: int,
    total_tokens: int,
    fold_count: int,
    min_turns_before_fold: int = 8,
    token_threshold: int = 100_000,
    message_threshold: int = 20,
) -> bool:
    """Decide whether folding should trigger.

    Folding triggers when ANY of these conditions are met:
    - Total tokens exceed token_threshold (context pressure)
    - Total messages exceed message_threshold (message count pressure)

    But NOT before min_turns_before_fold turns have passed (need enough
    history to make a meaningful fold).
    """
    if turn_count < min_turns_before_fold:
        return False
    if total_tokens >= token_threshold:
        return True
    if total_messages >= message_threshold:
        return True
    return False
