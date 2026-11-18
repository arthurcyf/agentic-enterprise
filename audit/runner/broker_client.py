"""Minimal MCP client for the remote broker (used by save_extract and the scripted agent)."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


class BrokerError(Exception):
    pass


def _decode(result: Any) -> dict:
    text = "".join(getattr(c, "text", "") for c in getattr(result, "content", []))
    data = json.loads(text or "{}")
    if "error" in data:
        raise BrokerError(data["error"])
    return data


class BrokerClient:
    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ClientSession]:
        async with create_mcp_http_client(headers={"X-Case-Token": self.token}) as http:
            async with streamable_http_client(self.url, http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    # Errors are raised only after the session closes: raising inside it would come out
    # wrapped in an ExceptionGroup from the session's task group.
    async def call(self, tool: str, args: dict) -> dict:
        async with self.session() as s:
            result = await s.call_tool(tool, args)
        return _decode(result)

    async def fetch_all(self, table: str) -> list[dict]:
        rows: list[dict] = []
        offset: int | None = 0
        error: BrokerError | None = None
        async with self.session() as s:
            while offset is not None:
                try:
                    page = _decode(await s.call_tool("extract", {"table": table, "offset": offset, "limit": 200}))
                except BrokerError as e:
                    error = e
                    break
                rows.extend(page["rows"])
                offset = page.get("next_offset")
        if error:
            raise error
        return rows
