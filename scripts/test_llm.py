"""Quick smoke test for the LLM client.

Run from the project root:
    python scripts/test_llm.py

Prints the exact request (URL, headers, body) and the raw response.
No retries — fails immediately so you can see exactly what went wrong.
"""

import asyncio
import json
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, "src")
from llm import _PROVIDER_MODELS, _PROVIDER_URLS

provider = os.getenv("LLM_PROVIDER", "nvidia")
api_key  = os.getenv("LLM_API_KEY", "")
base_url = os.getenv("LLM_BASE_URL", _PROVIDER_URLS.get(provider, ""))
model    = os.getenv("LLM_MODEL", _PROVIDER_MODELS.get(provider, ""))

FULL_URL = base_url.rstrip("/") + "/chat/completions"

PAYLOAD = {
    "model": model,
    "messages": [
        {"role": "system", "content": "You are a legal document summarizer. Reply with one sentence."},
        {"role": "user",   "content": "Summarize: An alien seeking to perform skilled labor is inadmissible unless the Secretary of Labor certifies there are insufficient U.S. workers available."},
    ],
    "stream": False,
}


async def main() -> int:
    if not api_key:
        print("[FAIL] LLM_API_KEY is not set")
        return 1

    masked_key = f"{api_key[:8]}...{api_key[-4:]}"
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "authorization": f"Bearer {api_key}",
    }

    print("=== Request ===")
    print(f"POST {FULL_URL}")
    print(f"  authorization: Bearer {masked_key}")
    print(f"  accept: application/json")
    print(f"  content-type: application/json")
    print("Body:")
    print(json.dumps(PAYLOAD, indent=2))
    print()

    async with httpx.AsyncClient(timeout=500.0) as client:
        try:
            response = await client.post(FULL_URL, headers=headers, json=PAYLOAD)
        except httpx.TimeoutException:
            print("[FAIL] Request timed out (30s)")
            return 1
        except httpx.ConnectError as e:
            print(f"[FAIL] Connection error: {e}")
            return 1

    print("=== Response ===")
    print(f"Status : {response.status_code} {response.reason_phrase}")
    print("Body:")
    try:
        print(json.dumps(response.json(), indent=2))
    except Exception:
        print(response.text)
    print()

    if response.status_code == 200:
        choices = response.json().get("choices", [])
        if choices:
            print("[PASS]", choices[0]["message"]["content"].strip())
        return 0
    else:
        print(f"[FAIL] Status {response.status_code}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
