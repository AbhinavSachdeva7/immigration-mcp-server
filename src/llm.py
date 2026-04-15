"""Model-agnostic LLM client for summarization.

Supports any provider with an OpenAI-compatible chat completions endpoint.
Configured via environment variables:

    LLM_PROVIDER    — "gemini" (default), "openai", or "custom"
    LLM_API_KEY     — API key for the provider
    LLM_MODEL       — Model name (default: "gemini-2.0-flash-lite")
    LLM_BASE_URL    — Base URL override (for custom providers)
    LLM_RPM         — Rate limit: requests per minute (default: 14, conservative for free tier)
    LLM_MAX_RETRIES — Max retries on transient failures (default: 3)

Gemini free tier limits (Flash-Lite):
    15 RPM, 1,000 RPD, 250K TPM

Usage:
    client = LLMClient.from_env()
    summary = await client.summarize("Long legal text here...")
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
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "openai": "https://api.openai.com/v1",
}

# Provider-specific default models
_PROVIDER_MODELS = {
    "gemini": "gemini-2.0-flash-lite",
    "openai": "gpt-4o-mini",
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
# LLM Client
# ---------------------------------------------------------------------------

@dataclass
class LLMClient:
    """Async LLM client for summarization with rate limiting and retries."""

    base_url: str
    api_key: str
    model: str
    rate_limiter: TokenBucketRateLimiter
    max_retries: int = 3
    _http_client: httpx.AsyncClient = field(default=None, repr=False)

    def __post_init__(self):
        self._http_client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    @classmethod
    def from_env(cls) -> "LLMClient":
        """Create an LLMClient from environment variables."""
        provider = os.getenv("LLM_PROVIDER", "gemini").lower()
        api_key = os.getenv("LLM_API_KEY", "")
        model = os.getenv("LLM_MODEL", _PROVIDER_MODELS.get(provider, ""))
        base_url = os.getenv("LLM_BASE_URL", _PROVIDER_URLS.get(provider, ""))
        rpm = int(os.getenv("LLM_RPM", "14"))
        max_retries = int(os.getenv("LLM_MAX_RETRIES", "3"))

        if not api_key:
            raise ValueError(
                "LLM_API_KEY environment variable is required. "
                "Set it in .env or export it. "
                f"Provider: {provider}, Model: {model}"
            )

        if not base_url:
            raise ValueError(
                f"No base URL for provider '{provider}'. "
                "Set LLM_BASE_URL or use a known provider (gemini, openai)."
            )

        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            rate_limiter=TokenBucketRateLimiter(rpm),
            max_retries=max_retries,
        )

    async def close(self):
        if self._http_client:
            await self._http_client.aclose()

    async def _chat_completion(self, system_prompt: str, user_content: str, max_tokens: int = 200) -> str:
        """Make a chat completion request with rate limiting and retries."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }

        last_error = None
        for attempt in range(self.max_retries + 1):
            await self.rate_limiter.acquire()

            try:
                response = await self._http_client.post(
                    "/chat/completions",
                    json=payload,
                )

                if response.status_code == 429:
                    # Rate limited — back off exponentially
                    wait = min(2 ** attempt * 5, 60)
                    print(f"    Rate limited, waiting {wait}s (attempt {attempt + 1})...")
                    await asyncio.sleep(wait)
                    continue

                if response.status_code >= 500:
                    # Server error — retry
                    wait = 2 ** attempt
                    print(f"    Server error {response.status_code}, retrying in {wait}s...")
                    await asyncio.sleep(wait)
                    continue

                response.raise_for_status()
                data = response.json()

                choices = data.get("choices", [])
                if choices:
                    return choices[0]["message"]["content"].strip()
                return ""

            except httpx.TimeoutException:
                last_error = "Request timed out"
                await asyncio.sleep(2 ** attempt)
            except httpx.HTTPStatusError as e:
                last_error = f"HTTP {e.response.status_code}"
                if e.response.status_code < 500:
                    raise
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError(f"LLM request failed after {self.max_retries + 1} attempts: {last_error}")

    async def summarize_text(self, text: str, max_tokens: int = 200) -> str:
        """Summarize a piece of legal text (for leaf nodes)."""
        # Truncate very long texts to avoid token limits
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
