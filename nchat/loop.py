"""The agent loop. The heart of the fork.

run_cast() is the clean turn cycle that replaces run_agent.py's monolithic
run_conversation() for compact mode. It accesses AIAgent infrastructure
through an AgentBridge — a typed interface that documents exactly what the
loop needs without coupling it to the 7,800-line AIAgent class.

Supports two mediums:
  - Conversation (default): entity uses tool_calls, tools are OpenAI schemas
  - Code (Phase 7): entity writes programs, gates are host functions in sandbox

Design principles (from cantrip spec):
  - Strict alternation: utterance, observation, utterance, observation
  - Errors are observations, not failures
  - Per-turn loom recording at every iteration
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set
from uuid import uuid4

from nchat.crystals import Crystal
from nchat.wards import Circle
from nchat.loom import Loom, Turn, GateCallRecord

logger = logging.getLogger(__name__)


# ── Exceptions for loop control flow ──────────────────────────────────

class ContextOverflowError(Exception):
    """API returned context-length or payload-too-large error.

    Raised by the bridge's query_api when compression is needed.
    The loop catches this, compresses, and retries.
    """
    pass


class MaxRetriesError(Exception):
    """All API retries exhausted without a successful response."""
    pass


# ── Result types ──────────────────────────────────────────────────────

@dataclass
class CastResult:
    """Result of a single cast (one run through the loop)."""
    response: str
    status: str  # "terminated" | "truncated" | "interrupted" | "failed"
    turns: int
    tool_calls_total: int = 0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    duration_ms: int = 0
    messages: List[Dict] = field(default_factory=list)
    entity_id: str = ""
    error: str = ""


@dataclass
class TurnRecord:
    """Record of a single turn for the loom."""
    entity_id: str
    sequence: int
    utterance: str
    gate_calls: List[Dict]
    observation: str
    tokens_prompt: int = 0
    tokens_completion: int = 0
    duration_ms: int = 0
    status: str = "running"
    timestamp: float = 0.0


# ── The AgentBridge ───────────────────────────────────────────────────

@dataclass
class AgentBridge:
    """Bridge between run_cast() and AIAgent's infrastructure.

    Constructed by AIAgent._build_agent_bridge() before delegating to
    run_cast(). Provides typed access to the specific capabilities the
    loop needs without coupling it to the full AIAgent class.

    Each callable wraps a method on AIAgent. The loop is ignorant of
    AIAgent internals — it only sees these typed slots.
    """

    # ── API call ──
    # (api_messages: list) → (response_obj, usage_dict)
    # Raises: InterruptedError, ContextOverflowError, MaxRetriesError
    query_api: Callable

    # ── Response normalization ──
    # (response_obj) → (assistant_message_obj, finish_reason_str)
    normalize_response: Callable

    # ── Assistant message builder ──
    # (assistant_message_obj, finish_reason) → dict for messages list
    build_assistant_message: Callable

    # ── Tool dispatch ──
    # (assistant_message_obj, messages_list, task_id, api_call_count)
    # Modifies messages in place (appends tool results)
    execute_tools: Callable

    # ── Message preparation ──
    # (system_prompt, messages) → api_messages_list
    build_api_messages: Callable

    # ── Context compression ──
    # (messages, system_message, approx_tokens, task_id) → (messages, new_system_prompt)
    compress_context: Callable

    # ── Session persistence ──
    # (messages) → None (save session log incrementally)
    save_session_log: Callable

    # ── State queries ──
    is_interrupted: Callable[[], bool]
    has_stream_consumers: Callable[[], bool]

    # ── Content utilities ──
    strip_think_blocks: Callable[[str], str]
    has_content_after_think: Callable[[str], bool]
    repair_tool_call: Callable[[str], Optional[str]]

    # ── State objects (read-only from the loop's perspective) ──
    iteration_budget: Any  # IterationBudget instance
    context_compressor: Any  # ContextCompressor instance
    compression_enabled: bool
    valid_tool_names: Set[str]
    max_iterations: int
    quiet_mode: bool
    task_id: str


# ── The loop ──────────────────────────────────────────────────────────

def run_cast(
    crystal: Crystal,
    call: str,
    circle: Circle,
    intent: str,
    history: List[Dict],
    entity_id: str | None = None,
    loom: Loom | None = None,
    bridge: AgentBridge | None = None,
    medium: Any = None,
) -> CastResult:
    """Run the agent loop.

    Args:
        crystal: Resolved (client, model) for this cast.
        call: The system prompt (identity + capability presentation).
        circle: Gates + wards configuration.
        intent: The user's message for this turn.
        history: Prior conversation messages.
        entity_id: Unique identifier for this entity run.
        loom: Loom for per-turn recording. Optional.
        bridge: AgentBridge for AIAgent infrastructure. When None,
                the loop runs in standalone mode (for testing).
        medium: CodeMedium instance for code mode. None = conversation mode.

    Returns:
        CastResult with the final response, status, and metadata.
    """
    entity_id = entity_id or str(uuid4())

    # Route to code medium path if medium is set
    if medium is not None:
        return _run_code_cast(
            crystal, call, circle, medium, intent, history,
            entity_id, loom, bridge,
        )

    messages = list(history)
    # Intent was already appended by the caller (run_conversation setup)
    # so we don't append it here.

    max_turns = circle.max_turns.limit
    turn_count = 0
    tool_calls_total = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    start_time = time.monotonic()

    # Content-with-tools fallback: when the model delivers its answer
    # alongside tool calls (e.g. "Here's your answer" + memory save),
    # the follow-up turn is often empty. We capture the content here
    # and use it as the final response if the next turn is empty.
    last_content_with_tools = None
    empty_content_retries = 0
    invalid_tool_retries = 0

    # System prompt may change after context compression
    active_system_prompt = call

    # Compression retry counter
    compression_attempts = 0
    max_compression_attempts = 3

    while turn_count < max_turns:
        # ── 1. Interrupt check ────────────────────────────────────────
        if bridge and bridge.is_interrupted():
            return _build_result(
                messages, entity_id, "interrupted", turn_count,
                tool_calls_total, total_prompt_tokens,
                total_completion_tokens, start_time,
            )

        turn_count += 1

        # ── 2. Consume iteration budget ───────────────────────────────
        if bridge and bridge.iteration_budget:
            if not bridge.iteration_budget.consume():
                break  # Budget exhausted → truncated

        turn_start = time.monotonic()

        # ── 3. Build API messages ─────────────────────────────────────
        if bridge:
            api_messages = bridge.build_api_messages(
                active_system_prompt, messages,
            )
        else:
            api_messages = (
                [{"role": "system", "content": active_system_prompt}]
                + messages
            )

        # ── 4. Query the crystal ──────────────────────────────────────
        turn_prompt_tokens = 0
        turn_completion_tokens = 0

        try:
            if bridge:
                response, usage_dict = bridge.query_api(api_messages)
                turn_prompt_tokens = usage_dict.get("prompt_tokens", 0)
                turn_completion_tokens = usage_dict.get("completion_tokens", 0)
            else:
                # Standalone mode — direct API call (testing only)
                try:
                    response = crystal.client.chat.completions.create(
                        model=crystal.model,
                        messages=api_messages,
                    )
                except Exception as e:
                    logger.error("Crystal query failed: %s", e)
                    break
                usage = getattr(response, "usage", None)
                if usage:
                    turn_prompt_tokens = getattr(usage, "prompt_tokens", 0)
                    turn_completion_tokens = getattr(usage, "completion_tokens", 0)

        except ContextOverflowError:
            # Compress and retry this turn
            if bridge and bridge.compression_enabled:
                compression_attempts += 1
                if compression_attempts > max_compression_attempts:
                    break  # Can't compress further
                approx = sum(len(str(m)) for m in api_messages) // 4
                messages, active_system_prompt = bridge.compress_context(
                    messages, None, approx, bridge.task_id,
                )
                turn_count -= 1  # Don't count the failed turn
                if bridge.iteration_budget:
                    bridge.iteration_budget.refund()
                continue
            break  # No compression available

        except InterruptedError:
            return _build_result(
                messages, entity_id, "interrupted", turn_count,
                tool_calls_total, total_prompt_tokens,
                total_completion_tokens, start_time,
            )

        except MaxRetriesError as e:
            return _build_result(
                messages, entity_id, "failed", turn_count,
                tool_calls_total, total_prompt_tokens,
                total_completion_tokens, start_time, error=str(e),
            )

        total_prompt_tokens += turn_prompt_tokens
        total_completion_tokens += turn_completion_tokens

        # ── 5. Normalize response ─────────────────────────────────────
        if bridge:
            assistant_message, finish_reason = bridge.normalize_response(
                response,
            )
        else:
            if not response or not response.choices:
                break
            assistant_message = response.choices[0].message
            finish_reason = (
                getattr(response.choices[0], "finish_reason", None) or "stop"
            )

        turn_duration_ms = int((time.monotonic() - turn_start) * 1000)

        # Normalize content to string
        if (assistant_message.content is not None
                and not isinstance(assistant_message.content, str)):
            raw = assistant_message.content
            if isinstance(raw, dict):
                assistant_message.content = (
                    raw.get("text", "") or raw.get("content", "")
                    or json.dumps(raw)
                )
            elif isinstance(raw, list):
                parts = []
                for part in raw:
                    if isinstance(part, str):
                        parts.append(part)
                    elif isinstance(part, dict) and "text" in part:
                        parts.append(str(part["text"]))
                assistant_message.content = "\n".join(parts)
            else:
                assistant_message.content = str(raw)

        # ── 6. Text-only response (no tool calls) ────────────────────
        if not assistant_message.tool_calls:
            content = assistant_message.content or ""

            # Check if response only has think blocks with no real content
            has_real_content = True
            if bridge:
                has_real_content = bridge.has_content_after_think(content)

            if not has_real_content:
                # Try the content-with-tools fallback
                if last_content_with_tools:
                    final_text = (
                        bridge.strip_think_blocks(last_content_with_tools).strip()
                        if bridge else last_content_with_tools
                    )
                    last_content_with_tools = None
                    empty_content_retries = 0

                    msg = (
                        bridge.build_assistant_message(
                            assistant_message, finish_reason
                        )
                        if bridge
                        else {"role": "assistant", "content": content}
                    )
                    messages.append(msg)

                    _record_loom_turn(
                        loom, entity_id, turn_count, crystal, content, [],
                        turn_prompt_tokens, turn_completion_tokens,
                        turn_duration_ms, "terminated",
                    )

                    return _build_result(
                        messages, entity_id, "terminated", turn_count,
                        tool_calls_total, total_prompt_tokens,
                        total_completion_tokens, start_time,
                        response=final_text,
                    )

                # Retry for empty content (model had a bad generation)
                empty_content_retries += 1
                if empty_content_retries < 3:
                    continue
                empty_content_retries = 0

            # Normal terminal response
            if bridge:
                final_text = bridge.strip_think_blocks(content).strip()
                msg = bridge.build_assistant_message(
                    assistant_message, finish_reason,
                )
            else:
                final_text = content
                msg = {"role": "assistant", "content": content}
            messages.append(msg)

            _record_loom_turn(
                loom, entity_id, turn_count, crystal, content, [],
                turn_prompt_tokens, turn_completion_tokens,
                turn_duration_ms, "terminated",
            )

            return _build_result(
                messages, entity_id, "terminated", turn_count,
                tool_calls_total, total_prompt_tokens,
                total_completion_tokens, start_time,
                response=final_text,
            )

        # ── 7. Validate tool calls ────────────────────────────────────
        if bridge:
            # Name validation + repair
            for tc in assistant_message.tool_calls:
                if tc.function.name not in bridge.valid_tool_names:
                    repaired = bridge.repair_tool_call(tc.function.name)
                    if repaired:
                        tc.function.name = repaired

            still_invalid = [
                tc.function.name
                for tc in assistant_message.tool_calls
                if tc.function.name not in bridge.valid_tool_names
            ]
            if still_invalid:
                invalid_tool_retries += 1
                if invalid_tool_retries >= 3:
                    # Give up after 3 attempts
                    break

                available = ", ".join(sorted(bridge.valid_tool_names))
                msg = bridge.build_assistant_message(
                    assistant_message, finish_reason,
                )
                messages.append(msg)
                for tc in assistant_message.tool_calls:
                    if tc.function.name not in bridge.valid_tool_names:
                        tool_content = (
                            f"Tool '{tc.function.name}' does not exist. "
                            f"Available tools: {available}"
                        )
                    else:
                        tool_content = (
                            "Skipped: another tool call in this turn "
                            "used an invalid name. Please retry."
                        )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_content,
                    })
                continue
            invalid_tool_retries = 0

            # JSON argument validation
            invalid_json = []
            for tc in assistant_message.tool_calls:
                args = tc.function.arguments
                if isinstance(args, (dict, list)):
                    tc.function.arguments = json.dumps(args)
                    continue
                if args is not None and not isinstance(args, str):
                    tc.function.arguments = str(args)
                    args = tc.function.arguments
                if not args or not args.strip():
                    tc.function.arguments = "{}"
                    continue
                try:
                    json.loads(args)
                except json.JSONDecodeError as e:
                    invalid_json.append((tc.function.name, str(e)))

            if invalid_json:
                msg = bridge.build_assistant_message(
                    assistant_message, finish_reason,
                )
                messages.append(msg)
                invalid_set = {name for name, _ in invalid_json}
                for tc in assistant_message.tool_calls:
                    if tc.function.name in invalid_set:
                        err = next(
                            e for n, e in invalid_json
                            if n == tc.function.name
                        )
                        tool_content = (
                            f"Error: Invalid JSON arguments. {err}. "
                            f"Please retry with valid JSON."
                        )
                    else:
                        tool_content = (
                            "Skipped: other tool call had invalid JSON."
                        )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": tool_content,
                    })
                continue

        # ── 8. Build and append assistant message ─────────────────────
        turn_content = assistant_message.content or ""

        # Capture content for the content-with-tools fallback
        if bridge and turn_content:
            if bridge.has_content_after_think(turn_content):
                last_content_with_tools = turn_content

        if bridge:
            assistant_msg = bridge.build_assistant_message(
                assistant_message, finish_reason,
            )
        else:
            assistant_msg = {
                "role": "assistant",
                "content": turn_content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in assistant_message.tool_calls
                ],
            }
        messages.append(assistant_msg)

        # ── 9. Execute tools ──────────────────────────────────────────
        msg_count_before = len(messages)
        tool_calls_total += len(assistant_message.tool_calls)

        if bridge:
            # api_call_count=0 suppresses old budget warning injection
            # (run_cast handles warnings via the ward)
            bridge.execute_tools(
                assistant_message, messages, bridge.task_id, 0,
            )
        else:
            # Standalone mode — empty results
            for tc in assistant_message.tool_calls:
                messages.append({
                    "role": "tool",
                    "content": "",
                    "tool_call_id": tc.id,
                })

        # ── 10. Record per-turn loom ──────────────────────────────────
        gate_calls = _build_gate_calls(
            assistant_message, messages, msg_count_before,
        )
        _record_loom_turn(
            loom, entity_id, turn_count, crystal, turn_content,
            gate_calls, turn_prompt_tokens, turn_completion_tokens,
            turn_duration_ms, "running",
        )

        # ── 11. Context pressure — compress if needed ─────────────────
        if bridge and bridge.compression_enabled:
            compressor = bridge.context_compressor
            new_tool_msgs = messages[msg_count_before:]
            new_chars = sum(
                len(str(m.get("content", "") or "")) for m in new_tool_msgs
            )
            estimated_next = (
                compressor.last_prompt_tokens
                + compressor.last_completion_tokens
                + new_chars // 3
            )
            if compressor.should_compress(estimated_next):
                messages, active_system_prompt = bridge.compress_context(
                    messages, None, compressor.last_prompt_tokens,
                    bridge.task_id,
                )

        # ── 12. Budget warning at threshold ───────────────────────────
        if circle.should_warn(turn_count):
            remaining = max_turns - turn_count
            messages.append({
                "role": "system",
                "content": circle.budget_warning_message(remaining),
            })

        # ── 13. Save session log incrementally ────────────────────────
        if bridge:
            bridge.save_session_log(messages)

        # ── 14. Refund budget for execute_code-only turns ─────────────
        if bridge and bridge.iteration_budget:
            tc_names = {
                tc.function.name for tc in assistant_message.tool_calls
            }
            if tc_names == {"execute_code"}:
                bridge.iteration_budget.refund()

    # ── Ward triggered: truncated ─────────────────────────────────────
    return _build_result(
        messages, entity_id, "truncated", turn_count,
        tool_calls_total, total_prompt_tokens,
        total_completion_tokens, start_time,
    )


# ── Code medium cast ──────────────────────────────────────────────────

def _run_code_cast(
    crystal: Crystal,
    call: str,
    circle: Circle,
    medium: Any,  # CodeMedium
    intent: str,
    history: List[Dict],
    entity_id: str,
    loom: Loom | None = None,
    bridge: AgentBridge | None = None,
) -> CastResult:
    """Code medium cast — entity writes programs, gates are host functions.

    The entity is given ONE tool (execute) and tool_choice is set to require it.
    Each turn: model writes code → sandbox executes → viewport observation returned.
    The loop continues until submit_answer() is called or max_turns is reached.
    """
    messages = list(history)
    if intent:
        messages.append({"role": "user", "content": intent})

    max_turns = circle.max_turns.limit
    turn_count = 0
    tool_calls_total = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    start_time = time.monotonic()

    # System prompt: identity + medium documentation
    active_system_prompt = call

    # The single tool schema
    tools = [medium.execute_tool_schema()]

    # Ensure sandbox is running
    try:
        medium.start()
    except Exception as e:
        return _build_result(
            messages, entity_id, "failed", 0, 0, 0, 0, start_time,
            error=f"Sandbox failed to start: {e}",
        )

    # Compression state
    compression_attempts = 0
    max_compression_attempts = 3

    while turn_count < max_turns:
        # ── 1. Interrupt check ────────────────────────────────────────
        if bridge and bridge.is_interrupted():
            return _build_result(
                messages, entity_id, "interrupted", turn_count,
                tool_calls_total, total_prompt_tokens,
                total_completion_tokens, start_time,
            )

        turn_count += 1
        turn_start = time.monotonic()

        # ── 2. Build API messages ─────────────────────────────────────
        if bridge:
            api_messages = bridge.build_api_messages(
                active_system_prompt, messages,
            )
        else:
            api_messages = (
                [{"role": "system", "content": active_system_prompt}]
                + messages
            )

        # ── 3. Query the crystal ──────────────────────────────────────
        turn_prompt_tokens = 0
        turn_completion_tokens = 0

        try:
            response, usage_dict = _query_crystal_direct(
                crystal, api_messages, tools,
            )
            turn_prompt_tokens = usage_dict.get("prompt_tokens", 0)
            turn_completion_tokens = usage_dict.get("completion_tokens", 0)

        except ContextOverflowError:
            if bridge and bridge.compression_enabled:
                compression_attempts += 1
                if compression_attempts > max_compression_attempts:
                    break
                approx = sum(len(str(m)) for m in api_messages) // 4
                messages, active_system_prompt = bridge.compress_context(
                    messages, None, approx, bridge.task_id,
                )
                turn_count -= 1
                continue
            break

        except InterruptedError:
            return _build_result(
                messages, entity_id, "interrupted", turn_count,
                tool_calls_total, total_prompt_tokens,
                total_completion_tokens, start_time,
            )

        except Exception as e:
            logger.error("Crystal query failed in code cast: %s", e)
            return _build_result(
                messages, entity_id, "failed", turn_count,
                tool_calls_total, total_prompt_tokens,
                total_completion_tokens, start_time,
                error=str(e),
            )

        total_prompt_tokens += turn_prompt_tokens
        total_completion_tokens += turn_completion_tokens

        # ── 4. Extract code from response ─────────────────────────────
        if not response or not response.choices:
            break

        assistant_message = response.choices[0].message
        finish_reason = getattr(response.choices[0], "finish_reason", None) or "stop"
        turn_duration_ms = int((time.monotonic() - turn_start) * 1000)

        code = _extract_code_from_response(assistant_message)

        if not code:
            # Model produced text-only (shouldn't happen with required tool_choice).
            # If the model has content, use it. Otherwise, extract from the last
            # viewport — the model likely print()-ed its answer instead of calling
            # submit_answer().
            content = assistant_message.content or ""
            if not content.strip():
                content = _extract_last_viewport_answer(messages)
            messages.append({"role": "assistant", "content": content})

            _record_loom_turn(
                loom, entity_id, turn_count, crystal, content[:500], [],
                turn_prompt_tokens, turn_completion_tokens,
                turn_duration_ms, "terminated",
            )

            return _build_result(
                messages, entity_id, "terminated", turn_count,
                tool_calls_total, total_prompt_tokens,
                total_completion_tokens, start_time,
                response=content,
            )

        tool_calls_total += 1

        # ── 5. Build and append assistant message ─────────────────────
        tc = assistant_message.tool_calls[0]
        assistant_msg = {
            "role": "assistant",
            "content": assistant_message.content or "",
            "tool_calls": [{
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }],
        }
        messages.append(assistant_msg)

        # ── 6. Execute in sandbox ─────────────────────────────────────
        observation = medium.execute(code)

        # Append viewport as tool result
        messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": observation.viewport,
        })

        # ── 7. Record loom turn ───────────────────────────────────────
        _record_loom_turn(
            loom, entity_id, turn_count, crystal,
            code[:500],  # utterance is the code (truncated for loom)
            observation.gate_calls,
            turn_prompt_tokens, turn_completion_tokens,
            turn_duration_ms, "running",
        )

        # ── 8. Check for done (submit_answer) ─────────────────────────
        if medium.is_done():
            _record_loom_turn(
                loom, entity_id, turn_count, crystal,
                f"submit_answer: {medium.answer[:200]}",
                observation.gate_calls,
                0, 0, 0, "terminated",
            )
            return _build_result(
                messages, entity_id, "terminated", turn_count,
                tool_calls_total, total_prompt_tokens,
                total_completion_tokens, start_time,
                response=medium.answer,
            )

        # ── 9. Budget warning at threshold ────────────────────────────
        if circle.should_warn(turn_count):
            remaining = max_turns - turn_count
            messages.append({
                "role": "system",
                "content": circle.budget_warning_message(remaining),
            })

        # ── 10. Context pressure — compress if needed ─────────────────
        if bridge and bridge.compression_enabled:
            compressor = bridge.context_compressor
            estimated_next = (
                compressor.last_prompt_tokens
                + compressor.last_completion_tokens
                + len(observation.viewport) // 3
            )
            if compressor.should_compress(estimated_next):
                messages, active_system_prompt = bridge.compress_context(
                    messages, None, compressor.last_prompt_tokens,
                    bridge.task_id,
                )

        # ── 11. Save session log ──────────────────────────────────────
        if bridge:
            bridge.save_session_log(messages)

    # ── Ward triggered: truncated ─────────────────────────────────────
    # Extract answer from medium state or last viewport if no submit_answer
    truncated_response = None
    if medium.is_done():
        truncated_response = medium.answer
    else:
        truncated_response = _extract_last_viewport_answer(messages)
    return _build_result(
        messages, entity_id, "truncated", turn_count,
        tool_calls_total, total_prompt_tokens,
        total_completion_tokens, start_time,
        response=truncated_response,
    )


def _query_crystal_direct(
    crystal: Crystal,
    api_messages: List[Dict],
    tools: List[Dict],
    tool_choice: str = "required",
) -> tuple:
    """Direct API call for code medium. Returns (response, usage_dict).

    Simpler than the bridge's query_api — no streaming, no Codex adapter,
    no TUI display. The code medium doesn't need those.
    """
    if not crystal.client:
        raise RuntimeError("Crystal has no client configured")

    try:
        kwargs: Dict[str, Any] = {
            "model": crystal.model,
            "messages": api_messages,
            "tools": tools,
        }
        # Try tool_choice="required" (force tool use); fall back to "auto"
        try:
            kwargs["tool_choice"] = tool_choice
            response = crystal.client.chat.completions.create(**kwargs)
        except Exception as first_err:
            if "tool_choice" in str(first_err).lower():
                kwargs["tool_choice"] = "auto"
                response = crystal.client.chat.completions.create(**kwargs)
            else:
                raise

        usage = getattr(response, "usage", None)
        usage_dict = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
        }
        return response, usage_dict

    except Exception as e:
        err_str = str(e).lower()
        if "context_length" in err_str or "payload" in err_str or "too large" in err_str:
            raise ContextOverflowError(str(e)) from e
        raise


def _extract_code_from_response(assistant_message: Any) -> Optional[str]:
    """Extract code from the model's execute tool call."""
    if not assistant_message.tool_calls:
        return None

    tc = assistant_message.tool_calls[0]
    args_raw = tc.function.arguments

    if isinstance(args_raw, dict):
        return args_raw.get("code", "")

    if isinstance(args_raw, str):
        try:
            args = json.loads(args_raw)
            return args.get("code", "")
        except json.JSONDecodeError:
            # Maybe the model put the code directly as the arguments string
            return args_raw

    return None


def _extract_last_viewport_answer(messages: List[Dict]) -> str:
    """Extract the last meaningful console output from code medium viewports.

    When the model print()-ed its answer instead of calling submit_answer(),
    the answer is in the tool result message's "Console output:" section.
    Walk backwards through tool results and return the first non-empty output.
    """
    for msg in reversed(messages):
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        if "Console output:" not in content:
            continue
        # Extract everything after "Console output:\n"
        idx = content.index("Console output:")
        output = content[idx + len("Console output:"):].strip()
        # Strip trailing "[...N more chars]" truncation notice
        if output.endswith("]") and "[..." in output:
            last_bracket = output.rfind("[...")
            output = output[:last_bracket].strip()
        if output:
            return output
    return ""


# ── Helpers ───────────────────────────────────────────────────────────

def _build_result(
    messages: List[Dict],
    entity_id: str,
    status: str,
    turns: int,
    tool_calls_total: int,
    tokens_prompt: int,
    tokens_completion: int,
    start_time: float,
    response: str | None = None,
    error: str = "",
) -> CastResult:
    """Build a CastResult, extracting response from messages if not given."""
    if response is None:
        response = _last_assistant_content(messages)
    return CastResult(
        response=response,
        status=status,
        turns=turns,
        tool_calls_total=tool_calls_total,
        tokens_prompt=tokens_prompt,
        tokens_completion=tokens_completion,
        duration_ms=int((time.monotonic() - start_time) * 1000),
        messages=messages,
        entity_id=entity_id,
        error=error,
    )


def _last_assistant_content(messages: List[Dict]) -> str:
    """Extract the last assistant content from the message list."""
    for msg in reversed(messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            return msg["content"]
    return ""


def _build_gate_calls(
    assistant_message: Any,
    messages: List[Dict],
    msg_count_before: int,
) -> List[GateCallRecord]:
    """Build GateCallRecords from the tool calls and their results."""
    gate_calls = []
    new_tool_msgs = messages[msg_count_before:]
    for tc in assistant_message.tool_calls:
        result_preview = ""
        for tm in new_tool_msgs:
            if tm.get("tool_call_id") == tc.id:
                result_preview = (tm.get("content") or "")[:200]
                break
        gate_calls.append(GateCallRecord(
            name=tc.function.name,
            arguments=(tc.function.arguments or "")[:100],
            result_preview=result_preview,
        ))
    return gate_calls


def _record_loom_turn(
    loom: Loom | None,
    entity_id: str,
    sequence: int,
    crystal: Crystal,
    utterance: str,
    gate_calls: list,
    tokens_prompt: int,
    tokens_completion: int,
    duration_ms: int,
    status: str,
) -> None:
    """Record a single turn to the loom."""
    if not loom:
        return
    try:
        loom.record(Turn(
            id=f"{entity_id}-{sequence}",
            entity_id=entity_id,
            sequence=sequence,
            crystal_tier=crystal.tier.value,
            utterance=(utterance or "")[:500],
            observation=f"gates={len(gate_calls)}",
            gate_calls=gate_calls,
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
            duration_ms=duration_ms,
            status=status,
            timestamp=time.time(),
        ))
    except Exception as e:
        logger.debug("Loom recording failed: %s", e)
