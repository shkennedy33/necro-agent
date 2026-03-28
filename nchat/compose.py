"""Composition — cantrip/cast/cast_batch host function implementations.

The entity (running in code medium) can construct and orchestrate child
entities at runtime. This is the "familiar pattern" from the cantrip spec.

Three entity types:
  Leaf cantrip:   No medium. Single LLM call. Cheapest delegation.
  Medium cantrip: Child gets its own sandbox/loop. Full autonomy.
  Conversation:   Child uses tool-calling loop (pre-Phase-7 behavior).

The compose engine lives on the host side. It receives gate calls from the
parent's sandbox (cantrip/cast/cast_batch/dispose) and runs child entities.

Security: children MUST NOT have more capabilities than the parent.
  - Gate sets validated against parent's gate set
  - Wards composed conservatively (WARD-1)
  - Depth limits prevent infinite recursion
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set
from uuid import uuid4

from nchat.crystals import Crystal, CrystalTier, resolve_crystal
from nchat.loom import Loom, Turn, GateCallRecord
from nchat.wards import Circle, MaxTurns, BudgetWarning

logger = logging.getLogger(__name__)


# ── CanTripConfig ─────────────────────────────────────────────────────

@dataclass
class CanTripConfig:
    """Configuration for a child entity."""
    crystal: str = "worker"  # "primary", "worker", "utility", or model ID
    call: str = ""  # System prompt / identity
    medium: str | None = None  # "python", "conversation", None (leaf)
    gates: List[str] = field(default_factory=lambda: ["done"])
    wards: Dict[str, Any] = field(default_factory=dict)
    cwd: str | None = None  # Working directory
    state: Dict | None = None  # Initial state for code medium children

    @classmethod
    def from_dict(cls, d: Dict) -> CanTripConfig:
        return cls(
            crystal=d.get("crystal", "worker"),
            call=d.get("call", ""),
            medium=d.get("medium"),
            gates=d.get("gates", ["done"]),
            wards=d.get("wards", {}),
            cwd=d.get("cwd"),
            state=d.get("state"),
        )


# ── CanTripHandle ─────────────────────────────────────────────────────

@dataclass
class CanTripHandle:
    """Opaque handle to a child entity configuration."""
    id: str
    config: CanTripConfig
    consumed: bool = False  # True after cast() has been called


# ── ComposeEngine ─────────────────────────────────────────────────────

class ComposeEngine:
    """Manages child entity lifecycle — cantrip/cast/cast_batch/dispose.

    Created per-cast (per parent entity invocation). Registered as gate
    handlers in the parent's CodeMedium.

    Args:
        crystal_resolver: Function to resolve crystal tiers.
        run_child_fn: Function to run a child entity loop. Signature:
            (crystal, call, circle, intent, history, entity_id, loom, bridge, medium) → CastResult
        loom: Parent's loom for recording child turns.
        parent_entity_id: Parent's entity ID (for loom tree).
        parent_circle: Parent's circle (for gate set validation).
        parent_remaining_turns: How many turns the parent has left.
        max_depth: Maximum delegation depth. Children get depth-1.
        max_concurrent: Maximum parallel children for cast_batch.
        parent_agent: Optional parent AIAgent for crystal resolution fallback.
    """

    def __init__(
        self,
        crystal_resolver: Callable = None,
        run_child_fn: Callable = None,
        loom: Loom | None = None,
        parent_entity_id: str = "",
        parent_circle: Circle | None = None,
        parent_remaining_turns: int = 90,
        max_depth: int = 2,
        max_concurrent: int = 8,
        parent_agent: Any = None,
        progress_callback: Callable | None = None,
    ):
        self._resolve_crystal = crystal_resolver or resolve_crystal
        self._run_child = run_child_fn
        self._loom = loom
        self._parent_entity_id = parent_entity_id
        self._parent_circle = parent_circle
        self._parent_remaining = parent_remaining_turns
        self._max_depth = max_depth
        self._max_concurrent = max_concurrent
        self._parent_agent = parent_agent
        self._progress_callback = progress_callback

        self._handles: Dict[str, CanTripHandle] = {}

    # ── Gate handlers (registered in the sandbox) ─────────────────────

    def cantrip_gate(self, args: Dict) -> str:
        """Create a child entity configuration. Returns handle ID."""
        config_dict = args.get("config", args)
        if isinstance(config_dict, str):
            try:
                config_dict = json.loads(config_dict)
            except json.JSONDecodeError:
                return json.dumps({"error": "Invalid config: expected dict or JSON string"})

        config = CanTripConfig.from_dict(config_dict)

        # Validate: children can't have more gates than parent
        if self._parent_circle and config.gates != ["done"]:
            parent_gates = set(self._parent_circle.gate_names)
            # Composition gates don't count for validation
            parent_gates.discard("cantrip")
            parent_gates.discard("cast")
            parent_gates.discard("cast_batch")
            parent_gates.discard("dispose")
            child_gates = set(config.gates) - {"done", "submit_answer"}
            invalid = child_gates - parent_gates
            if invalid:
                return json.dumps({
                    "error": f"Child cannot use gates the parent doesn't have: {sorted(invalid)}"
                })

        # Validate: depth limit
        if self._max_depth <= 0:
            return json.dumps({
                "error": "Maximum delegation depth reached. Cannot create more children."
            })

        handle = CanTripHandle(
            id=f"ct-{uuid4().hex[:12]}",
            config=config,
        )
        self._handles[handle.id] = handle
        return handle.id

    def cast_gate(self, args: Dict) -> str:
        """Run a child entity. Blocks until complete. Returns result."""
        handle_id = args.get("handle", "")
        intent = args.get("intent", "")

        handle = self._handles.get(handle_id)
        if not handle:
            return json.dumps({"error": f"Unknown handle: {handle_id}"})
        if handle.consumed:
            return json.dumps({"error": f"Handle already consumed: {handle_id}"})

        handle.consumed = True
        try:
            return self._execute_child(handle, intent)
        except Exception as e:
            logger.error("Child entity failed: %s", e, exc_info=True)
            return json.dumps({"error": f"Child entity failed: {e}"})
        finally:
            # Auto-dispose after cast
            self._handles.pop(handle_id, None)

    def cast_batch_gate(self, args: Dict) -> str:
        """Run multiple children in parallel. Returns JSON array of results."""
        items = args.get("items", [])
        if isinstance(items, str):
            try:
                items = json.loads(items)
            except json.JSONDecodeError:
                return json.dumps({"error": "Invalid items: expected list"})

        if not items:
            return json.dumps([])

        results: List[Optional[str]] = [None] * len(items)
        errors: List[Optional[str]] = [None] * len(items)
        semaphore = threading.Semaphore(self._max_concurrent)

        def _worker(idx: int, item: Dict):
            handle_id = item.get("cantrip", "")
            intent = item.get("intent", "")
            semaphore.acquire()
            try:
                handle = self._handles.get(handle_id)
                if not handle:
                    errors[idx] = f"Unknown handle: {handle_id}"
                    return
                if handle.consumed:
                    errors[idx] = f"Handle already consumed: {handle_id}"
                    return
                handle.consumed = True
                results[idx] = self._execute_child(handle, intent)
                self._handles.pop(handle_id, None)
            except Exception as e:
                errors[idx] = str(e)
            finally:
                semaphore.release()

        threads = []
        for i, item in enumerate(items):
            t = threading.Thread(target=_worker, args=(i, item), daemon=True)
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=self._parent_remaining * 10)  # generous timeout

        # Build results array
        final = []
        for i in range(len(items)):
            if errors[i]:
                final.append(json.dumps({"error": errors[i]}))
            elif results[i] is not None:
                final.append(results[i])
            else:
                final.append(json.dumps({"error": "Child did not produce a result"}))

        return json.dumps(final)

    def dispose_gate(self, args: Dict) -> str:
        """Dispose of an unused handle."""
        handle_id = args.get("handle", "")
        handle = self._handles.pop(handle_id, None)
        if handle:
            return "disposed"
        return json.dumps({"error": f"Unknown handle: {handle_id}"})

    # ── Child execution ───────────────────────────────────────────────

    def _execute_child(self, handle: CanTripHandle, intent: str) -> str:
        """Execute a child entity and return the result string."""
        config = handle.config
        child_entity_id = f"child-{uuid4().hex[:8]}"

        # Resolve crystal
        crystal = self._resolve_child_crystal(config.crystal)

        # Compose wards
        child_wards = self._compose_wards(config.wards)

        if config.medium is None:
            # Leaf cantrip — single LLM call
            return self._run_leaf(crystal, config, intent, child_entity_id)
        elif config.medium == "conversation":
            # Conversation medium — tool-calling loop
            return self._run_conversation_child(
                crystal, config, intent, child_entity_id, child_wards,
            )
        elif config.medium in ("python", "code"):
            # Code medium — new sandbox + code loop
            return self._run_code_child(
                crystal, config, intent, child_entity_id, child_wards,
            )
        else:
            return json.dumps({"error": f"Unknown medium: {config.medium}"})

    def _run_leaf(
        self, crystal: Crystal, config: CanTripConfig,
        intent: str, entity_id: str,
    ) -> str:
        """Leaf cantrip: single LLM call, no loop, cheapest delegation."""
        if not crystal.client:
            return json.dumps({"error": "Crystal has no client configured"})

        try:
            messages = [
                {"role": "system", "content": config.call or "Complete the task."},
                {"role": "user", "content": intent},
            ]
            response = crystal.client.chat.completions.create(
                model=crystal.model,
                messages=messages,
            )
            result = response.choices[0].message.content or ""

            # Record in loom
            if self._loom:
                usage = getattr(response, "usage", None)
                self._loom.record(Turn(
                    id=f"{entity_id}-1",
                    parent_id=f"{self._parent_entity_id}-leaf",
                    entity_id=entity_id,
                    sequence=1,
                    crystal_tier=crystal.tier.value,
                    utterance=result[:500],
                    observation="leaf",
                    tokens_prompt=getattr(usage, "prompt_tokens", 0) if usage else 0,
                    tokens_completion=getattr(usage, "completion_tokens", 0) if usage else 0,
                    status="terminated",
                ))

            return result

        except Exception as e:
            logger.error("Leaf cantrip failed: %s", e)
            return json.dumps({"error": f"Leaf cantrip failed: {e}"})

    def _run_conversation_child(
        self, crystal: Crystal, config: CanTripConfig,
        intent: str, entity_id: str, child_wards: Dict,
    ) -> str:
        """Conversation-medium child: tool-calling loop via run_cast."""
        if not self._run_child:
            return json.dumps({"error": "No run_child function available"})

        child_circle = Circle(
            gate_names=config.gates,
            max_turns=MaxTurns(limit=child_wards.get("max_turns", 25)),
            budget_warning=BudgetWarning(threshold=0.8),
        )

        from nchat.loop import CastResult
        try:
            result: CastResult = self._run_child(
                crystal=crystal,
                call=config.call or "Complete the task.",
                circle=child_circle,
                intent=intent,
                history=[],
                entity_id=entity_id,
                loom=self._loom,
                bridge=None,  # No bridge for child — standalone mode
                medium=None,  # Conversation medium
            )
            return result.response
        except Exception as e:
            logger.error("Conversation child failed: %s", e)
            return json.dumps({"error": f"Conversation child failed: {e}"})

    def _run_code_child(
        self, crystal: Crystal, config: CanTripConfig,
        intent: str, entity_id: str, child_wards: Dict,
    ) -> str:
        """Code-medium child: new sandbox + code loop."""
        if not self._run_child:
            return json.dumps({"error": "No run_child function available"})

        from nchat.medium import CodeMedium

        child_medium = CodeMedium(
            language="python",
            viewport_limit=500,
            timeout=min(300, child_wards.get("max_turns", 25) * 30),
            progress_callback=self._progress_callback,
        )

        # Register child's gates
        child_gate_set = set(config.gates) - {"done", "submit_answer"}
        if child_gate_set:
            child_medium.register_gates_from_registry(child_gate_set)

        # If depth allows, register composition gates for the child too
        child_depth = self._max_depth - 1
        if child_depth > 0:
            child_compose = ComposeEngine(
                crystal_resolver=self._resolve_crystal,
                run_child_fn=self._run_child,
                loom=self._loom,
                parent_entity_id=entity_id,
                parent_remaining_turns=child_wards.get("max_turns", 25),
                max_depth=child_depth,
                max_concurrent=self._max_concurrent,
                parent_agent=self._parent_agent,
                progress_callback=self._progress_callback,
            )
            child_medium.register_gate(GateSpec(
                name="cantrip", handler=child_compose.cantrip_gate,
                signature=GATE_SIGNATURES.get("cantrip", ""),
            ))
            child_medium.register_gate(GateSpec(
                name="cast", handler=child_compose.cast_gate,
                signature=GATE_SIGNATURES.get("cast", ""),
            ))
            child_medium.register_gate(GateSpec(
                name="cast_batch", handler=child_compose.cast_batch_gate,
                signature=GATE_SIGNATURES.get("cast_batch", ""),
            ))
            child_medium.register_gate(GateSpec(
                name="dispose", handler=child_compose.dispose_gate,
                signature=GATE_SIGNATURES.get("dispose", ""),
            ))

        child_circle = Circle(
            gate_names=config.gates,
            max_turns=MaxTurns(limit=child_wards.get("max_turns", 25)),
            budget_warning=BudgetWarning(threshold=0.8),
        )

        try:
            child_medium.start()
            from nchat.loop import CastResult
            result: CastResult = self._run_child(
                crystal=crystal,
                call=config.call or "Complete the task.",
                circle=child_circle,
                intent=intent,
                history=[],
                entity_id=entity_id,
                loom=self._loom,
                bridge=None,
                medium=child_medium,
            )
            return result.response
        except Exception as e:
            logger.error("Code child failed: %s", e)
            return json.dumps({"error": f"Code child failed: {e}"})
        finally:
            child_medium.close()

    # ── Crystal resolution ────────────────────────────────────────────

    def _resolve_child_crystal(self, crystal_spec: str) -> Crystal:
        """Resolve a crystal tier name or model ID to a Crystal."""
        tier_map = {
            "primary": CrystalTier.PRIMARY,
            "worker": CrystalTier.WORKER,
            "utility": CrystalTier.UTILITY,
        }

        if crystal_spec in tier_map:
            return self._resolve_crystal(
                tier_map[crystal_spec],
                parent_agent=self._parent_agent,
            )

        # Model ID passthrough — resolve as custom crystal
        return _resolve_model_id(crystal_spec, self._parent_agent)

    # ── Ward composition (WARD-1) ─────────────────────────────────────

    def _compose_wards(self, child_wards: Dict) -> Dict:
        """Compose child wards with parent constraints.

        Children can only be more restricted than parents.
        """
        child_max = child_wards.get("max_turns", 25)
        # Child can't have more turns than parent has remaining
        composed_max = min(child_max, self._parent_remaining)

        return {
            "max_turns": max(1, composed_max),
            "max_depth": max(0, self._max_depth - 1),
            "require_done": child_wards.get("require_done", False),
        }


# ── Helpers ───────────────────────────────────────────────────────────

# Import GateSpec and GATE_SIGNATURES here for compose gate registration
from nchat.medium import GateSpec, GATE_SIGNATURES


def _resolve_model_id(model_id: str, parent_agent: Any = None) -> Crystal:
    """Resolve a model ID string to a Crystal.

    Supports formats like:
      "anthropic/claude-sonnet-4-6"
      "openrouter/google/gemini-3-flash"
      "claude-haiku-4-5"
    """
    parts = model_id.split("/", 1)
    if len(parts) == 2:
        provider, model = parts
    else:
        # Bare model name — try to detect provider
        model = model_id
        if "claude" in model.lower():
            provider = "anthropic"
        elif "gpt" in model.lower() or "o1" in model.lower():
            provider = "openai"
        else:
            provider = "openrouter"

    try:
        from nchat.crystals import _resolve_from_config
        resolved = _resolve_from_config({"provider": provider, "model": model})
        if resolved:
            client, model_name, prov, base_url, api_key, api_mode = resolved
            return Crystal(
                tier=CrystalTier.WORKER,  # custom models treated as worker tier
                client=client,
                model=model_name,
                provider=prov,
                base_url=base_url,
                api_key=api_key,
                api_mode=api_mode,
            )
    except Exception as e:
        logger.debug("Model ID resolution failed for %s: %s", model_id, e)

    # Fallback to parent
    if parent_agent:
        return Crystal(
            tier=CrystalTier.WORKER,
            client=getattr(parent_agent, "client", None),
            model=model_id,
            provider=getattr(parent_agent, "provider", "") or "",
            base_url=getattr(parent_agent, "base_url", "") or "",
            api_key=getattr(parent_agent, "api_key", "") or "",
        )

    return Crystal(
        tier=CrystalTier.WORKER,
        client=None,
        model=model_id,
        provider="unknown",
        base_url="",
        api_key="",
    )
