"""
LLM provider abstraction for the apply pipeline.

Uses the Anthropic API when ANTHROPIC_API_KEY is set:
  - tier="smart" → claude-sonnet   (resume tailoring, answer generation)
  - tier="fast"  → claude-haiku    (classification, extraction)
Falls back to the existing Groq setup (llama-3.3-70b) when it is not,
so nothing breaks on the current Render deployment.

Usage:
    from llm import complete, complete_json
    text = complete("...", tier="fast")
    data = complete_json("... reply with ONLY JSON ...", tier="fast")
"""

import os
import json
import re
import logging

logger = logging.getLogger("agent_logger")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_SMART_MODEL = os.getenv("ANTHROPIC_SMART_MODEL", "claude-sonnet-5")
ANTHROPIC_FAST_MODEL = os.getenv("ANTHROPIC_FAST_MODEL", "claude-haiku-4-5-20251001")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
# Hard override: LLM_PROVIDER=groq forces the free path even if an Anthropic key
# is present (handy for staying at $0). Default: use Anthropic only when its key
# is set, otherwise Groq.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "").strip().lower()

_anthropic_client = None
_groq_llm = None


def use_anthropic() -> bool:
    if LLM_PROVIDER == "groq":
        return False
    if LLM_PROVIDER == "anthropic":
        return bool(ANTHROPIC_API_KEY)
    return bool(ANTHROPIC_API_KEY)


def provider() -> str:
    return "anthropic" if use_anthropic() else "groq"


def _anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client


def _groq():
    global _groq_llm
    if _groq_llm is None:
        from langchain_groq import ChatGroq
        _groq_llm = ChatGroq(model_name=GROQ_MODEL, temperature=0.2)
    return _groq_llm


def complete(prompt: str, tier: str = "fast", system: str = "",
             max_tokens: int = 4096, temperature: float = 0.2) -> str:
    """Single-shot completion. Raises on hard provider errors."""
    if use_anthropic():
        model = ANTHROPIC_SMART_MODEL if tier == "smart" else ANTHROPIC_FAST_MODEL
        resp = _anthropic().messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system or "You are a precise assistant for a job-application pipeline.",
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    # Groq fallback — one model for both tiers
    full = f"{system}\n\n{prompt}" if system else prompt
    return _groq().invoke(full).content.strip()


def complete_json(prompt: str, tier: str = "fast", system: str = "",
                  max_tokens: int = 4096):
    """Completion that must return JSON. Extracts the first JSON object/array
    from the reply; returns None if nothing parseable came back."""
    raw = complete(prompt, tier=tier, system=system, max_tokens=max_tokens)
    # strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = re.search(r"(\[.*\]|\{.*\})", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    logger.warning(f"[LLM] could not parse JSON from reply: {raw[:200]}")
    return None
