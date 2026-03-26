"""Crystal resolver: task tier → (client, model, provider metadata).

Maps three tiers of work to the right model. The operator configures
this in cli-config.yaml under the 'crystals' key. Falls back through:
  explicit config → delegation config → auxiliary_client → primary model.

Terminology (cantrip → nchat):
  Crystal = the model + client pairing for a given tier of work.
  PRIMARY = Opus — orchestration, conversation, creative reasoning.
  WORKER  = Sonnet — delegated subtasks, review, skill operations.
  UTILITY = Flash/Haiku — summaries, titles, folding, leaf analysis.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class CrystalTier(Enum):
    PRIMARY = "primary"
    WORKER = "worker"
    UTILITY = "utility"


@dataclass(frozen=True)
class Crystal:
    """Resolved crystal: everything needed to make an API call."""
    tier: CrystalTier
    client: Any          # OpenAI-compatible client
    model: str           # Model slug (e.g. "claude-opus-4-6")
    provider: str        # Provider name for logging/routing
    base_url: str        # API base URL
    api_key: str         # API key (may be empty for some providers)
    api_mode: str | None = None  # "chat_completions", "codex_responses", etc.


def _load_crystals_config() -> Dict[str, Any]:
    """Load crystal config from cli-config.yaml."""
    try:
        from hermes_cli.config import load_config
        config = load_config()
        return config.get("crystals", {})
    except Exception:
        return {}


def _load_delegation_config() -> Dict[str, Any]:
    """Load delegation config as fallback for WORKER tier."""
    try:
        from hermes_cli.config import load_config
        config = load_config()
        return config.get("delegation", {})
    except Exception:
        return {}


def _resolve_from_config(
    tier_config: Dict[str, Any],
) -> Optional[Tuple[Any, str, str, str, str, str]]:
    """Try to build a client from explicit crystal config.

    Returns (client, model, provider, base_url, api_key, api_mode) or None.
    """
    if not tier_config:
        return None

    provider = str(tier_config.get("provider", "")).strip().lower()
    model = str(tier_config.get("model", "")).strip()

    if not provider or not model:
        return None

    # Use the existing provider resolution system
    try:
        from agent.auxiliary_client import resolve_provider_client
        client, resolved_model = resolve_provider_client(
            provider=provider,
            model=model,
        )
        if client and resolved_model:
            # Get base_url and api_key from the client
            base_url = getattr(client, "_base_url", "")
            if hasattr(base_url, "host"):
                base_url = str(base_url)
            api_key = getattr(client, "api_key", "")
            return (client, resolved_model, provider, str(base_url), str(api_key or ""), None)
    except Exception as e:
        logger.debug("Crystal config resolution failed for %s: %s", provider, e)

    return None


def _resolve_from_runtime_provider(
    provider_name: str,
    model: str,
) -> Optional[Tuple[Any, str, str, str, str, str]]:
    """Resolve using the hermes runtime provider system."""
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        runtime = resolve_runtime_provider(requested=provider_name)
        if not runtime:
            return None

        from openai import OpenAI
        client = OpenAI(
            base_url=runtime.get("base_url", ""),
            api_key=runtime.get("api_key", ""),
        )
        return (
            client,
            model,
            runtime.get("provider", provider_name),
            runtime.get("base_url", ""),
            runtime.get("api_key", ""),
            runtime.get("api_mode"),
        )
    except Exception as e:
        logger.debug("Runtime provider resolution failed for %s: %s", provider_name, e)
        return None


def resolve_crystal(
    tier: CrystalTier,
    config: Dict[str, Any] | None = None,
    parent_agent: Any = None,
) -> Crystal:
    """Resolve a crystal for the given tier.

    Resolution chain:
      1. Explicit crystals config (cli-config.yaml → crystals.{tier})
      2. Environment variable override (NCHAT_{TIER}_MODEL)
      3. For WORKER: delegation config (cli-config.yaml → delegation.{provider,model})
      4. For UTILITY: auxiliary_client resolution
      5. Fallback: inherit from parent_agent or use PRIMARY defaults

    Args:
        tier: Which tier to resolve.
        config: Optional pre-loaded crystals config dict.
        parent_agent: Optional parent AIAgent to inherit from as fallback.

    Returns:
        A Crystal with all fields populated.
    """
    crystals_config = config or _load_crystals_config()

    # 1. Check environment variable override
    env_model = os.environ.get(f"NCHAT_{tier.value.upper()}_MODEL")

    # 2. Check explicit crystals config
    tier_config = crystals_config.get(tier.value, {})
    if env_model:
        # Env var overrides the model but we still use config's provider
        tier_config = dict(tier_config or {})
        tier_config["model"] = env_model

    resolved = _resolve_from_config(tier_config)
    if resolved:
        client, model, provider, base_url, api_key, api_mode = resolved
        return Crystal(
            tier=tier,
            client=client,
            model=model,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            api_mode=api_mode,
        )

    # 3. Tier-specific fallbacks
    if tier == CrystalTier.WORKER:
        # Try delegation config
        deleg_config = _load_delegation_config()
        deleg_provider = deleg_config.get("provider")
        deleg_model = deleg_config.get("model")
        if deleg_provider and deleg_model:
            resolved = _resolve_from_config({
                "provider": deleg_provider,
                "model": deleg_model,
            })
            if resolved:
                client, model, provider, base_url, api_key, api_mode = resolved
                return Crystal(
                    tier=tier,
                    client=client,
                    model=model,
                    provider=provider,
                    base_url=base_url,
                    api_key=api_key,
                    api_mode=api_mode,
                )

    if tier == CrystalTier.UTILITY:
        # Use auxiliary_client resolution (already handles the full fallback chain)
        try:
            from agent.auxiliary_client import get_text_auxiliary_client
            client, model = get_text_auxiliary_client(task="crystal_utility")
            if client and model:
                base_url = getattr(client, "_base_url", "")
                if hasattr(base_url, "host"):
                    base_url = str(base_url)
                return Crystal(
                    tier=tier,
                    client=client,
                    model=model,
                    provider="auxiliary",
                    base_url=str(base_url),
                    api_key=str(getattr(client, "api_key", "") or ""),
                )
        except Exception as e:
            logger.debug("Auxiliary client resolution failed for UTILITY: %s", e)

    # 4. Final fallback: inherit from parent agent
    if parent_agent:
        return Crystal(
            tier=tier,
            client=getattr(parent_agent, "client", None),
            model=getattr(parent_agent, "model", ""),
            provider=getattr(parent_agent, "provider", "") or "",
            base_url=getattr(parent_agent, "base_url", "") or "",
            api_key=getattr(parent_agent, "api_key", "") or "",
            api_mode=getattr(parent_agent, "api_mode", None),
        )

    # 5. Last resort: return a stub that will fail clearly
    logger.warning(
        "Could not resolve crystal for tier %s. "
        "Configure crystals.%s in cli-config.yaml.",
        tier.value, tier.value,
    )
    return Crystal(
        tier=tier,
        client=None,
        model="",
        provider="none",
        base_url="",
        api_key="",
    )


def resolve_all_crystals(
    config: Dict[str, Any] | None = None,
    parent_agent: Any = None,
) -> Dict[CrystalTier, Crystal]:
    """Resolve all three crystal tiers at once."""
    crystals_config = config or _load_crystals_config()
    return {
        tier: resolve_crystal(tier, crystals_config, parent_agent)
        for tier in CrystalTier
    }
