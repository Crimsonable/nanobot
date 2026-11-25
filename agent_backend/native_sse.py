"""Native SSE framing and message access shared by the control client and Gateway."""
import json
from urllib.parse import quote


async def sse(response):
    response.raise_for_status()
    if response.headers.get("content-type", "").split(";", 1)[0] != "text/event-stream":
        raise ValueError("expected native SSE")
    data = []
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield json.loads("\n".join(data))
                data.clear()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))


async def messages(client, session_id, *, message_id=None, since=None):
    """Read the native projected timeline, newest pages first to bound history reads."""
    result, params = [], {"order": "desc", "limit": 200}
    path = "/api/session/" + quote(session_id, safe="") + "/message"
    while True:
        response = await client.get(path, params=params)
        response.raise_for_status()
        page = response.json()
        result.extend(page["data"])
        if any(m["id"] == message_id or (since is not None and m["time"]["created"] < since)
               for m in page["data"]):
            break
        cursor = page.get("cursor", {}).get("next")
        if not cursor:
            break
        params = {"limit": 200, "cursor": cursor}
    result.reverse()
    if message_id is not None:
        start = next((i for i, m in enumerate(result) if m["id"] == message_id), None)
        if start is None:
            return []
        result = result[start:]
    return result
