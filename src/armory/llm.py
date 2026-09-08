"""OpenAI-compatible LLM layer: batched extraction + natural-language rules + scam scoring.

One call per source per poll cycle classifies every new listing. If no API key
is configured, everything here is skipped and keyword matching still works.
"""

from __future__ import annotations

import json
import re

import httpx

from .models import Listing

DEFAULT_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

# listings per classify call; a busy cycle splits into multiple calls.
# Small batches keep flash-tier JSON generation well inside the timeout.
BATCH_SIZE = 10

SYSTEM_PROMPT = """You analyze firearm marketplace/classifieds listings for a personal monitor.

For each listing return structured fields and evaluate it against the user's watch rules.

Return STRICT JSON only, shaped exactly:
{"results": [{
  "id": "<the listing id verbatim>",
  "brand": "string or null",
  "model": "string or null",
  "item_type": "firearm|optic|ammo|holster|accessory|part|knife|gear|service|other",
  "price_usd": number or null,
  "condition": "new|like new|very good|good|fair|used|null",
  "wants_to": "wts|wtt|wtb|null",
  "city": "seller city if stated anywhere in the listing, else null",
  "state": "two-letter state if stated, else null",
  "zip": "5-digit zip if stated, else null",
  "ffl_required": true|false|null,
  "scam_risk": "low|medium|high",
  "scam_reason": "short justification when scam_risk is medium/high, else null",
  "matched_rules": ["<rule text verbatim for each rule this listing satisfies>", ...]
}]}

Rules are natural-language descriptions of what the user wants alerts for. A rule
matches only if the listing genuinely satisfies it — brand/model compatibility,
price ceiling, condition, and location constraints all count. When unsure, don't
match. NEVER match rules on want-to-buy or want-to-trade listings (wants_to wtb
or wtt): the user is looking to ACQUIRE items, so posts from people seeking the
same items are noise — matched_rules must be [] for them.
Scam signals: too-good-to-be-true price vs item, new/no-history sellers,
off-platform payment requests, stock-photo-only listings, pressure tactics."""


class LLMError(Exception):
    pass


def extract_json(text: str) -> dict:
    """Parse a JSON object out of a model reply, tolerating code fences and prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start == -1:
        raise LLMError("no JSON object in model reply")
    depth, in_string, escaped = 0, False, False
    for i, ch in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError as exc:
                    raise LLMError(f"malformed JSON in model reply: {exc}")
    raise LLMError("unterminated JSON in model reply")


class LLMClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None):
        from .secrets import secret

        self.api_key = api_key or secret("LLM_API_KEY")
        self.base_url = (base_url or secret("LLM_API_BASE") or DEFAULT_BASE).rstrip("/")
        self.model = model or secret("LLM_MODEL") or DEFAULT_MODEL
        # Anthropic-style base URLs (e.g. Z.ai coding plan: .../api/anthropic)
        # use /v1/messages with x-api-key instead of OpenAI chat/completions.
        self.anthropic = "/anthropic" in self.base_url
        # disabled | low | on — benchmarked 2026-09: extraction/rules/scam show
        # no quality loss without reasoning, at half the latency (5.2s vs 10.9s).
        self.thinking = (secret("LLM_THINKING") or "disabled").lower()
        self._client = httpx.Client(timeout=120.0)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def chat(self, system: str, user: str) -> str:
        if not self.configured:
            raise LLMError("LLM_API_KEY not set in .env")
        last_error: LLMError | None = None
        for attempt in range(2):
            try:
                if self.anthropic:
                    return self._chat_anthropic(system, user)
                return self._chat_openai(system, user)
            except LLMError as exc:
                last_error = exc
                if attempt == 0 and "timed out" in str(exc).lower():
                    continue  # one retry on timeouts only
                raise
        raise last_error  # type: ignore[misc]

    def _chat_openai(self, system: str, user: str) -> str:
        try:
            resp = self._client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "temperature": 0,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"request failed: {exc}")
        if resp.status_code == 401:
            raise LLMError("API key rejected (HTTP 401)")
        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"unexpected API response shape: {exc}")

    def _chat_anthropic(self, system: str, user: str) -> str:
        body: dict = {
            "model": self.model,
            "max_tokens": 4096,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if self.thinking == "disabled":
            body["thinking"] = {"type": "disabled"}
        elif self.thinking == "low":
            body["thinking"] = {"type": "enabled", "budget_tokens": 1024}
        try:
            resp = self._client.post(
                f"{self.base_url}/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
                json=body,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"request failed: {exc}")
        if resp.status_code == 401:
            raise LLMError("API key rejected (HTTP 401)")
        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            blocks = resp.json()["content"]
            return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        except (KeyError, TypeError, ValueError) as exc:
            raise LLMError(f"unexpected API response shape: {exc}")

    # --- agentic tool loop (valuation and other multi-round work) ---

    def _anthropic_messages(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        thinking: str | None = None,
    ) -> dict:
        """Full-control Anthropic call returning the parsed response body.

        Raises LLMError; transient failures bubble to the caller for retry.
        """
        body: dict = {
            "model": model or self.model,
            "max_tokens": 16384,
            "system": system,
            "messages": messages,
        }
        mode = thinking or self.thinking
        if mode == "low":
            body["max_tokens"] = 8192
            body["thinking"] = {"type": "enabled", "budget_tokens": 1024}
        elif mode != "disabled":
            body["thinking"] = {"type": "enabled", "budget_tokens": 8192}
        else:
            body["thinking"] = {"type": "disabled"}
        if tools:
            body["tools"] = tools
        try:
            resp = self._client.post(
                f"{self.base_url}/v1/messages",
                headers={"x-api-key": self.api_key, "anthropic-version": "2023-06-01"},
                json=body,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"request failed: {exc}")
        if resp.status_code == 401:
            raise LLMError("API key rejected (HTTP 401)")
        if resp.status_code != 200:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise LLMError(f"unexpected API response shape: {exc}")

    def run_tool_loop(
        self,
        system: str,
        user: str,
        tool_specs: list[dict],
        executor,
        model: str | None = None,
        thinking: str | None = None,
        max_rounds: int = 4,
    ) -> str:
        """Agentic loop: the model may call client-side tools until it answers.

        executor(name, arguments) -> str feeds each tool_use back as a
        tool_result. Returns the final text (thinking blocks are ignored).
        Anthropic-protocol only — the valuation stack targets z.ai.
        """
        if not self.anthropic:
            raise LLMError("tool loop requires an Anthropic-style LLM_API_BASE")
        messages: list[dict] = [{"role": "user", "content": user}]
        for _ in range(max_rounds):
            data = self._anthropic_messages(system, messages, tools=tool_specs, model=model, thinking=thinking)
            content = data.get("content", [])
            messages.append({"role": "assistant", "content": content})
            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if not tool_uses:
                return "".join(b.get("text", "") for b in content if b.get("type") == "text")
            results = []
            for block in tool_uses:
                try:
                    output = executor(block.get("name", ""), block.get("input", {}) or {})
                except Exception as exc:  # noqa: BLE001 — report, don't kill the loop
                    output = f"tool error: {exc}"
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.get("id"),
                        "content": [{"type": "text", "text": str(output)[:6000]}],
                    }
                )
            messages.append({"role": "user", "content": results})
        raise LLMError(f"tool loop did not converge in {max_rounds} rounds")

    # --- listing classification ---

    def classify_batch(self, listings: list[Listing], rules: list[str]) -> dict[str, dict]:
        """One call: extract fields + match rules + score scams for a batch.

        Returns {external_id: {field: value}}. Raises LLMError on failure; the
        poller treats that as "skip LLM for this cycle".
        """
        out: dict[str, dict] = {}
        for i in range(0, len(listings), BATCH_SIZE):
            batch = listings[i : i + BATCH_SIZE]
            payload = {"rules": rules or [], "listings": []}
            for listing in batch:
                payload["listings"].append(
                    {
                        "id": listing.external_id,
                        "source": listing.source,
                        "title": listing.title,
                        "price": listing.price,
                        "location": listing.location,
                        "category": listing.category,
                        "seller": listing.author,
                        "description": (listing.body or "")[:900],
                    }
                )
            reply = self.chat(SYSTEM_PROMPT, json.dumps(payload))
            data = extract_json(reply)
            for result in data.get("results", []):
                rid = result.get("id")
                if rid:
                    out[str(rid)] = result
        return out

    def ping(self) -> str:
        """Cheap credential check; returns detail text."""
        if not self.configured:
            return "no API key"
        if self.anthropic:
            try:
                self._chat_anthropic("Reply with: ok", "ping")
                return f"key OK (model: {self.model})"
            except LLMError as exc:
                return str(exc)[:120]
        try:
            resp = self._client.get(
                f"{self.base_url}/models", headers={"Authorization": f"Bearer {self.api_key}"}
            )
        except httpx.HTTPError as exc:
            return f"unreachable: {exc}"
        if resp.status_code == 200:
            return f"key OK (model: {self.model})"
        return f"HTTP {resp.status_code} — check LLM_API_KEY"


def apply_result(listing: Listing, result: dict) -> None:
    """Merge one classify result onto its Listing in place."""
    listing.brand = (result.get("brand") or None) or listing.brand
    listing.model = (result.get("model") or None) or listing.model
    listing.item_type = (result.get("item_type") or None) or listing.item_type
    listing.condition = (result.get("condition") or None) or listing.condition
    listing.city = (result.get("city") or None) or listing.city
    listing.state = (result.get("state") or None) or listing.state
    listing.zip = (result.get("zip") or None) or listing.zip
    wants_to = result.get("wants_to")
    if wants_to in ("wts", "wtt", "wtb"):
        listing.wants_to = wants_to
    if result.get("price_usd") is not None and listing.price_usd is None:
        try:
            listing.price_usd = float(result["price_usd"])
        except (TypeError, ValueError):
            pass
    scam = result.get("scam_risk")
    if scam in ("low", "medium", "high"):
        listing.scam_risk = scam
        listing.scam_reason = result.get("scam_reason")
    hits = [r for r in result.get("matched_rules", []) if isinstance(r, str)]
    if hits:
        listing.matched_rules = hits
