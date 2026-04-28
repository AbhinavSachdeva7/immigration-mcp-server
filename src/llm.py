"""Model-agnostic LLM client for summarization with fallback chain support.

Supports any provider with an OpenAI-compatible chat completions endpoint.
Configured via environment variables:

    LLM_CHAIN       — Ordered fallback chain: "groq:llama-3.1-8b-instant,nvidia:deepseek-ai/deepseek-v4-flash"
                      When a model hits its rate limit (429), the next slot is tried automatically.
                      If unset, falls back to single-provider mode below.

    LLM_PROVIDER    — Single provider: "groq" or "nvidia" (used when LLM_CHAIN is not set)
    LLM_MODEL       — Model override for single-provider mode
    LLM_BASE_URL    — Base URL override (for custom providers)
    LLM_RPM         — Rate limit override for single-provider mode
    LLM_MAX_RETRIES — Max retries on 5xx errors per slot (default: 3)

    Per-provider API keys (set all, LLM_PROVIDER selects which is active):
    LLM_NVIDIA_API_KEY, LLM_GROQ_API_KEY, etc.
    LLM_API_KEY     — Generic fallback if provider-specific key is not set

Usage:
    # Single provider
    client = LLMClient.from_env()

    # Fallback chain (set LLM_CHAIN in .env)
    client = LLMClient.from_env()

    summary = await client.summarize_text("Long legal text here...")
"""

import asyncio
import os
import time
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv

load_dotenv()


# Provider-specific base URLs
_PROVIDER_URLS = {
    # "gemini":  "https://generativelanguage.googleapis.com/v1beta/openai/",
    # "openai":  "https://api.openai.com/v1/",
    # "mistral": "https://api.mistral.ai/v1/",
    "groq":    "https://api.groq.com/openai/v1/",
    # "ollama":  "http://localhost:11434/v1/",
    "nvidia":  "https://integrate.api.nvidia.com/v1/",
}

# Provider-specific default models (used in single-provider mode)
_PROVIDER_MODELS = {
    # "gemini":  "gemini-2.0-flash-lite",
    # "openai":  "gpt-4o-mini",
    # "mistral": "mistral-small-latest",
    "groq":    "llama-3.1-8b-instant",
    # "ollama":  "llama3.2",
    "nvidia":  "deepseek-ai/deepseek-v4-flash",
}

# Per-model RPM defaults (check your plan — these are free/tier-1 limits)
_MODEL_RPM: dict[str, int] = {
    # "gemini-2.0-flash-lite":     14,
    # "gemini-2.0-flash":          14,
    # "gemini-1.5-flash":          14,
    # "gemini-1.5-pro":             2,
    # "gpt-4o-mini":              500,
    # "gpt-4o":                   500,
    # "mistral-small-latest":      60,
    # "mistral-large-latest":      60,
    "llama-3.1-8b-instant":          30,
    # "llama-3.3-70b-versatile":   30,
    "deepseek-ai/deepseek-v4-flash": 40,
    "openai/gpt-oss-120b":           30,
    "openai/gpt-oss-20b":            30,
}

# Provider-level RPM fallback when model is not listed above
_PROVIDER_RPM: dict[str, int] = {
    "gemini":  14,
    "openai":  500,
    "mistral": 60,
    "groq":    30,
    "ollama":  999,
    "nvidia":  40,
}

# Summarization prompt template
_SUMMARIZE_SYSTEM_PROMPT = (
    "You are a legal document summarizer. Produce a concise summary of the "
    "provided text that captures the key legal provisions, requirements, and "
    "conditions. The summary should help a reader decide whether this section "
    "is relevant to their immigration question. Keep the summary under 3 sentences. "
    "Do not add opinions or legal advice."
)

_SUMMARIZE_CHILDREN_SYSTEM_PROMPT = (
    "You are a legal document summarizer. You are given summaries of subsections "
    "within a legal document section. Produce a single concise summary that "
    "captures what this parent section covers overall. The summary should help "
    "a reader decide whether to explore this section's children for more detail. "
    "Keep the summary under 3 sentences. Do not add opinions or legal advice."
)


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class TokenBucketRateLimiter:
    """Simple async token-bucket rate limiter."""

    def __init__(self, requests_per_minute: int):
        self._rpm = requests_per_minute
        self._interval = 60.0 / requests_per_minute
        self._semaphore = asyncio.Semaphore(1)
        self._last_request_time = 0.0

    async def acquire(self):
        async with self._semaphore:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self._interval:
                await asyncio.sleep(self._interval - elapsed)
            self._last_request_time = time.monotonic()


# ---------------------------------------------------------------------------
# Model slot — one entry in the fallback chain
# ---------------------------------------------------------------------------

@dataclass
class _ModelSlot:
    """A single provider+model combination with its own HTTP client and rate limiter."""

    provider: str
    model: str
    base_url: str
    api_key: str
    rate_limiter: TokenBucketRateLimiter
    exhausted_until: float = field(default=0.0, init=False, repr=False)
    _http_client: httpx.AsyncClient = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self._http_client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )

    def is_available(self) -> bool:
        return time.monotonic() >= self.exhausted_until

    def mark_exhausted(self, cooldown: float = 60.0):
        """Mark this slot as rate-limited for `cooldown` seconds."""
        self.exhausted_until = time.monotonic() + cooldown

    async def close(self):
        if self._http_client:
            await self._http_client.aclose()


def _build_slot(provider: str, model: str, api_key: str, base_url: str, rpm: int) -> _ModelSlot:
    if not api_key:
        raise ValueError(
            f"No API key for provider '{provider}'. "
            f"Set LLM_{provider.upper()}_API_KEY or LLM_API_KEY in .env."
        )
    if not base_url:
        raise ValueError(
            f"No base URL for provider '{provider}'. "
            "Set LLM_BASE_URL or add it to _PROVIDER_URLS."
        )
    return _ModelSlot(
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        rate_limiter=TokenBucketRateLimiter(rpm),
    )


# ---------------------------------------------------------------------------
# LLM Client
# ---------------------------------------------------------------------------

@dataclass
class LLMClient:
    """Async LLM client with fallback chain: tries each slot in order, skipping rate-limited ones."""

    slots: list[_ModelSlot]
    max_retries: int = 3

    @property
    def model(self) -> str:
        """Primary model (first slot). Used for display/logging."""
        return self.slots[0].model if self.slots else ""

    @property
    def base_url(self) -> str:
        """Primary base URL (first slot). Used for display/logging."""
        return self.slots[0].base_url if self.slots else ""

    @classmethod
    def from_env(cls) -> "LLMClient":
        """Create an LLMClient from environment variables.

        If LLM_CHAIN is set, builds a fallback chain.
        Otherwise, uses LLM_PROVIDER / LLM_MODEL for a single slot.
        """
        max_retries = int(os.getenv("LLM_MAX_RETRIES", "3"))
        chain_str = os.getenv("LLM_CHAIN", "").strip()

        if chain_str:
            # e.g. LLM_CHAIN=groq:llama-3.1-8b-instant,nvidia:deepseek-ai/deepseek-v4-flash
            slots = []
            for entry in chain_str.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                provider, model = entry.split(":", 1)
                provider = provider.lower().strip()
                model = model.strip()
                api_key = os.getenv(f"LLM_{provider.upper()}_API_KEY") or os.getenv("LLM_API_KEY", "")
                base_url = _PROVIDER_URLS.get(provider, "")
                rpm = _MODEL_RPM.get(model) or _PROVIDER_RPM.get(provider, 14)
                slots.append(_build_slot(provider, model, api_key, base_url, rpm))
            return cls(slots=slots, max_retries=max_retries)

        # Single-provider mode (backward compatible)
        provider = os.getenv("LLM_PROVIDER", "gemini").lower()
        api_key = os.getenv(f"LLM_{provider.upper()}_API_KEY") or os.getenv("LLM_API_KEY", "")
        model = os.getenv("LLM_MODEL", _PROVIDER_MODELS.get(provider, ""))
        base_url = os.getenv("LLM_BASE_URL", _PROVIDER_URLS.get(provider, ""))
        default_rpm = _MODEL_RPM.get(model) or _PROVIDER_RPM.get(provider, 14)
        rpm = int(os.getenv("LLM_RPM", str(default_rpm)))
        return cls(
            slots=[_build_slot(provider, model, api_key, base_url, rpm)],
            max_retries=max_retries,
        )

    async def close(self):
        for slot in self.slots:
            await slot.close()

    async def _chat_completion(self, system_prompt: str, user_content: str, max_tokens: int = 200) -> str:
        """Make a chat completion request, falling back through slots on 429."""
        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_content},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }

        last_error = None

        for slot in self.slots:
            if not slot.is_available():
                print(f"    [{slot.provider}/{slot.model}] Skipping — still cooling down.")
                continue

            payload["model"] = slot.model

            for attempt in range(self.max_retries + 1):
                await slot.rate_limiter.acquire()

                try:
                    response = await slot._http_client.post("chat/completions", json=payload)

                    if response.status_code == 429:
                        slot.mark_exhausted(60.0)
                        print(f"    [{slot.provider}/{slot.model}] Rate limited — moving to next slot.")
                        break  # exit retry loop, try next slot

                    if response.status_code >= 500:
                        wait = 2 ** attempt
                        print(f"    [{slot.provider}/{slot.model}] Server error {response.status_code}, retrying in {wait}s...")
                        await asyncio.sleep(wait)
                        continue

                    response.raise_for_status()
                    data = response.json()
                    choices = data.get("choices", [])
                    if choices:
                        return choices[0]["message"]["content"].strip()
                    return ""

                except httpx.TimeoutException:
                    last_error = f"[{slot.provider}/{slot.model}] timed out"
                    slot.mark_exhausted(30.0)
                    print(f"    {last_error} — moving to next slot.")
                    break  # exit retry loop, try next slot

                except httpx.HTTPStatusError as e:
                    last_error = f"HTTP {e.response.status_code}"
                    if e.response.status_code < 500:
                        raise
                    await asyncio.sleep(2 ** attempt)

        raise RuntimeError(
            f"All model slots exhausted or failed. Last error: {last_error}. "
            f"Slots tried: {[f'{s.provider}/{s.model}' for s in self.slots]}"
        )

    async def summarize_text(self, text: str, max_tokens: int = 200) -> str:
        """Summarize a piece of legal text (for leaf nodes)."""
        if len(text) > 12000:
            text = text[:6000] + "\n\n[...truncated...]\n\n" + text[-6000:]

        return await self._chat_completion(
            _SUMMARIZE_SYSTEM_PROMPT,
            f"Summarize the following legal text:\n\n{text}",
            max_tokens=max_tokens,
        )

    async def summarize_children(self, title: str, child_summaries: list[str], max_tokens: int = 200) -> str:
        """Summarize a parent node from its children's summaries (for intermediate nodes)."""
        combined = "\n\n".join(
            f"- {summary}" for summary in child_summaries if summary
        )

        return await self._chat_completion(
            _SUMMARIZE_CHILDREN_SYSTEM_PROMPT,
            f"Section: {title}\n\nChild section summaries:\n{combined}",
            max_tokens=max_tokens,
        )
