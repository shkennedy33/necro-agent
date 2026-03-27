"""The Loom — append-only turn recording.

Every turn is recorded. The loom is append-only. It captures:
  - Entity ID (which entity was acting)
  - Parent ID (which turn spawned this, for composition)
  - Utterance (what the model said)
  - Observation (what the circle returned)
  - Gate calls (structured records)
  - Token usage
  - Duration
  - Terminated vs. truncated

The loom is stored at ~/.hermes/loom/{session_id}.jsonl.
One file per session. This is the raw material for:
  - Debugging (what did the model actually do?)
  - Training data (terminated threads are complete episodes)
  - Cost tracking (token usage per turn, per entity, per crystal tier)

Implementation notes:
  - Parallel to the session DB (hermes_state.py), not a replacement
  - JSONL format for append-only writes and easy streaming reads
  - LOOM-5: Folding never destroys history (full turns stay in loom)
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class GateCallRecord:
    """Structured record of a single gate (tool) call."""
    name: str
    arguments: str = ""
    result_preview: str = ""
    duration_ms: int = 0
    error: bool = False


@dataclass
class Turn:
    """A single turn in the loom."""
    id: str
    parent_id: str | None = None
    entity_id: str = ""
    sequence: int = 0
    crystal_tier: str = ""          # "primary", "worker", "utility"
    utterance: str = ""             # model output (text + tool calls)
    observation: str = ""           # circle response (tool results)
    gate_calls: List[GateCallRecord] = field(default_factory=list)
    tokens_prompt: int = 0
    tokens_completion: int = 0
    duration_ms: int = 0
    status: str = "running"         # "running" | "terminated" | "truncated"
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for JSONL storage."""
        d = asdict(self)
        return d


class Loom:
    """Append-only turn log. JSONL on disk."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._turns: List[Turn] = []

    def record(self, turn: Turn) -> None:
        """Record a turn to memory and (optionally) disk."""
        self._turns.append(turn)
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(turn.to_dict(), default=str) + "\n")
            except Exception as e:
                logger.debug("Loom write failed: %s", e)

    def load(self) -> None:
        """Load existing turns from disk."""
        if not self.path or not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    # Reconstruct gate calls
                    gate_calls = [
                        GateCallRecord(**gc)
                        for gc in data.pop("gate_calls", [])
                    ]
                    self._turns.append(Turn(**data, gate_calls=gate_calls))
        except Exception as e:
            logger.warning("Loom load failed: %s", e)

    def thread(self, entity_id: str) -> List[Turn]:
        """Get all turns for a specific entity."""
        return [t for t in self._turns if t.entity_id == entity_id]

    def tree(self, root_entity_id: str) -> Dict[str, List[Turn]]:
        """Build the delegation tree from a root entity.

        Returns a dict mapping entity_id → list of turns,
        including the root and all child entities spawned from it.
        """
        result: Dict[str, List[Turn]] = {}
        # Start with the root
        root_turns = self.thread(root_entity_id)
        result[root_entity_id] = root_turns

        # Find child entities (turns whose parent_id matches any turn in root)
        root_turn_ids = {t.id for t in root_turns}
        for turn in self._turns:
            if turn.parent_id in root_turn_ids and turn.entity_id != root_entity_id:
                if turn.entity_id not in result:
                    result[turn.entity_id] = []
                result[turn.entity_id].append(turn)

        return result

    @property
    def turns(self) -> List[Turn]:
        """All recorded turns."""
        return list(self._turns)

    def summary(self) -> Dict[str, Any]:
        """Generate a cost/usage summary for the session."""
        entities: Dict[str, Dict[str, int]] = {}
        for turn in self._turns:
            key = turn.crystal_tier or "unknown"
            if key not in entities:
                entities[key] = {
                    "turns": 0,
                    "tokens_prompt": 0,
                    "tokens_completion": 0,
                    "gate_calls": 0,
                }
            entities[key]["turns"] += 1
            entities[key]["tokens_prompt"] += turn.tokens_prompt
            entities[key]["tokens_completion"] += turn.tokens_completion
            entities[key]["gate_calls"] += len(turn.gate_calls)

        return {
            "total_turns": len(self._turns),
            "by_crystal_tier": entities,
        }


def get_loom_path(session_id: str) -> Path:
    """Get the loom file path for a session."""
    from hermes_constants import get_nchat_home
    return get_nchat_home() / "loom" / f"{session_id}.jsonl"


def get_or_create_loom(session_id: str) -> Loom:
    """Get or create a loom for the given session."""
    path = get_loom_path(session_id)
    loom = Loom(path)
    loom.load()
    return loom
