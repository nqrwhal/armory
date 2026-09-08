"""Minimal MCP client for the z.ai web-search server (the model's web_search tool).

Speaks just enough streamable-HTTP MCP (initialize handshake → tools/call) to
run web searches — no SDK dependency. The tool's input schema is not publicly
documented, so tools/list is introspected on connect and the schema logged;
the call degrades gracefully (valuation continues without fresh web data).
"""

from __future__ import annotations

import json

import httpx

DEFAULT_URL = "https://api.z.ai/api/mcp/web_search_prime/mcp"
# schema verified live 2026-09: required search_query; location "us" gets
# non-CN-region results; additionalProperties is false — no extra keys.
TOOL_NAME = "web_search_prime"
PROTOCOL_VERSION = "2024-11-05"

_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


class ZaiSearchError(Exception):
    pass


def _parse_response(resp: httpx.Response) -> dict:
    """MCP streamable-HTTP replies are either a JSON body or an SSE stream."""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    try:
                        return json.loads(payload)
                    except json.JSONDecodeError:
                        continue
        raise ZaiSearchError("empty SSE stream from MCP server")
    try:
        return resp.json()
    except ValueError as exc:
        raise ZaiSearchError(f"non-JSON MCP reply: {resp.text[:200]}") from exc


class ZaiSearch:
    def __init__(self, url: str | None = None, api_key: str | None = None):
        from .secrets import secret

        self.url = url or secret("ZAI_SEARCH_MCP_URL") or DEFAULT_URL
        self.api_key = api_key or secret("LLM_API_KEY")
        self._client = httpx.Client(timeout=30.0)
        self._session_id: str | None = None
        self._tool_schema: dict | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _post(self, body: dict, expect_response: bool = True) -> httpx.Response:
        headers = dict(_HEADERS)
        headers["Authorization"] = f"Bearer {self.api_key}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        resp = self._client.post(self.url, headers=headers, json=body)
        if resp.status_code == 404 and self._session_id:
            # server dropped the session — rehandshake once on the next call
            self._session_id = None
            raise ZaiSearchError("MCP session expired")
        if resp.status_code >= 400:
            raise ZaiSearchError(f"MCP HTTP {resp.status_code}: {resp.text[:200]}")
        if expect_response and resp.headers.get("mcp-session-id"):
            self._session_id = resp.headers["mcp-session-id"]
        return resp

    def _ensure_session(self) -> None:
        if self._session_id:
            return
        if not self.configured:
            raise ZaiSearchError("no API key (set LLM_API_KEY in .env)")
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "armory", "version": "0.1.0"},
                },
            }
        )
        result = _parse_response(resp)
        if "error" in result:
            raise ZaiSearchError(f"initialize failed: {result['error']}")
        self._post(  # notification — no id, no body expected
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            expect_response=False,
        )

    def tools(self) -> list[dict]:
        self._ensure_session()
        resp = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        result = _parse_response(resp)
        return result.get("result", {}).get("tools", [])

    def search(self, query: str, recency: str | None = None) -> str:
        """Run one web search; returns result text trimmed for an LLM context."""
        self._ensure_session()
        args: dict = {"search_query": query[:70], "location": "us"}
        if recency in ("oneDay", "oneWeek", "oneMonth", "oneYear"):
            args["search_recency_filter"] = recency
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": TOOL_NAME, "arguments": args},
            }
        )
        result = _parse_response(resp)
        if "error" in result:
            raise ZaiSearchError(f"tools/call failed: {result['error']}")
        content = result.get("result", {}).get("content", [])
        texts = [b.get("text", "") for b in content if b.get("type") == "text"]
        joined = "\n".join(t for t in texts if t).strip()
        if not joined:
            raise ZaiSearchError("search returned no text content")
        return joined[:6000]

    def ping(self) -> str:
        try:
            tools = self.tools()
        except ZaiSearchError as exc:
            return str(exc)[:160]
        names = ", ".join(t.get("name", "?") for t in tools) or "none"
        return f"ok ({len(tools)} tool(s): {names})"

    def close(self) -> None:
        self._client.close()
