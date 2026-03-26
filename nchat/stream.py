"""Streaming response handler — extracted from run_agent.py.

Handles the streaming API response path where tokens are delivered
incrementally. Provides a clean interface for both CLI and gateway
consumers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class StreamEvent:
    """A streaming event from the model."""
    type: str  # "text", "tool_call", "thinking", "done", "error"
    content: str = ""
    tool_name: str = ""
    tool_args: str = ""
    tool_call_id: str = ""


def stream_response(
    client: Any,
    model: str,
    messages: List[Dict],
    tools: List[Dict] | None = None,
    system: str | None = None,
    on_event: Callable[[StreamEvent], None] | None = None,
    **kwargs,
) -> Dict[str, Any]:
    """Stream a model response, emitting events as chunks arrive.

    Args:
        client: OpenAI-compatible client.
        model: Model identifier.
        messages: Conversation messages.
        tools: Tool schemas (optional).
        system: System prompt (prepended as first message if provided).
        on_event: Callback for each streaming event.
        **kwargs: Additional API parameters.

    Returns:
        Dict with the complete response (same format as non-streaming).
    """
    api_messages = messages
    if system:
        api_messages = [{"role": "system", "content": system}] + messages

    create_kwargs = {
        "model": model,
        "messages": api_messages,
        "stream": True,
    }
    if tools:
        create_kwargs["tools"] = tools
    create_kwargs.update(kwargs)

    full_content = ""
    tool_calls = []
    current_tool_call = None
    usage = None

    try:
        stream = client.chat.completions.create(**create_kwargs)

        for chunk in stream:
            if not chunk.choices:
                # Usage chunk at the end
                if hasattr(chunk, "usage") and chunk.usage:
                    usage = chunk.usage
                continue

            delta = chunk.choices[0].delta

            # Text content
            if delta.content:
                full_content += delta.content
                if on_event:
                    on_event(StreamEvent(type="text", content=delta.content))

            # Tool calls
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    while len(tool_calls) <= idx:
                        tool_calls.append({
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                    tc = tool_calls[idx]
                    if tc_delta.id:
                        tc["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tc["function"]["name"] += tc_delta.function.name
                            if on_event:
                                on_event(StreamEvent(
                                    type="tool_call",
                                    tool_name=tc["function"]["name"],
                                    tool_call_id=tc["id"],
                                ))
                        if tc_delta.function.arguments:
                            tc["function"]["arguments"] += tc_delta.function.arguments

        if on_event:
            on_event(StreamEvent(type="done"))

    except Exception as e:
        logger.error("Streaming error: %s", e)
        if on_event:
            on_event(StreamEvent(type="error", content=str(e)))
        raise

    return {
        "content": full_content,
        "tool_calls": tool_calls if tool_calls else None,
        "usage": usage,
    }
