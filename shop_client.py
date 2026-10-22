"""Client helpers shared by client_purchase.py, the tests and the demo.

Two small layers:

- MCP: ``call_mcp_tool`` opens a throw-away streamable-HTTP session per
  call (stateless server, so this is cheap and robust for a PoC).
- A2A: ``a2a_send`` streams a message (optionally with context/task ids
  and a JSON payload) and collects text + structured ``data`` parts, so
  callers can assert on canonical JSON instead of LLM-generated prose.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx

from a2a.client import ClientConfig, create_client
from a2a.helpers import get_stream_response_text
from a2a.helpers.proto_helpers import (
    new_data_part,
    new_message,
    new_text_part,
)
from a2a.types import GetTaskRequest, Role, SendMessageRequest, TaskState

# Cold-starting a local model can take a while; so can a paused task that
# waits to be killed during the restart drill.
REQUEST_TIMEOUT = 240.0


# ----------------------------------------------------------------------
# MCP
# ----------------------------------------------------------------------
def normalize_numbers(value):
    """Integral floats (1.0) -> int, recursively (protobuf Value artifacts)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: normalize_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_numbers(v) for v in value]
    return value


def _extract_mcp_result(result) -> object:
    """Normalises FastMCP tool results across SDK versions."""
    structured = getattr(result, 'structured_content', None)
    if isinstance(structured, dict):
        if structured.get('result') is not None and set(structured) == {'result'}:
            return normalize_numbers(structured['result'])
        return normalize_numbers(structured)
    texts = [
        block.text for block in (result.content or [])
        if getattr(block, 'type', '') == 'text'
    ]
    joined = '\n'.join(texts)
    try:
        return json.loads(joined)
    except (ValueError, TypeError):
        return joined


async def call_mcp_tool(
    base_url: str, tool: str, args: dict | None = None
) -> object:
    """Calls one tool on the shop MCP server; returns dict/str/list."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = base_url.rstrip('/')
    if not url.endswith('/mcp'):
        url = f'{url}/mcp'

    async with streamablehttp_client(url, timeout=60) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args or {})
            if result.isError:
                raise RuntimeError(
                    f'MCP tool {tool} failed: {result.content!r}')
            return _extract_mcp_result(result)


# ----------------------------------------------------------------------
# A2A
# ----------------------------------------------------------------------
@dataclass
class A2aResult:
    task_id: str | None = None
    context_id: str | None = None
    final_state: str | None = None
    text: str = ''
    data: list = field(default_factory=list)
    events: list = field(default_factory=list)
    error: str | None = None  # set when strict=False and the stream broke

    @property
    def ok(self) -> bool:
        return self.error is None

    def last_data(self) -> dict | None:
        for payload in reversed(self.data):
            if isinstance(payload, dict):
                return payload
        return None


async def _collect_response(response, result: A2aResult) -> None:
    """Extracts ids, text and data parts from one streaming response."""
    kind = response.WhichOneof('payload')
    result.events.append(kind)
    if kind == 'task':
        result.task_id = response.task.id
        result.context_id = response.task.context_id
        result.final_state = response.task.status.state
    elif kind == 'status_update':
        result.task_id = response.status_update.task_id
        result.context_id = response.status_update.context_id
        result.final_state = TaskState.Name(  # type: ignore[arg-type]
            response.status_update.status.state
        )
        if response.status_update.status.message is not None:
            result.text = (
                get_stream_response_text(response) or result.text
            )
    elif kind == 'artifact_update':
        result.task_id = response.artifact_update.task_id
        result.context_id = response.artifact_update.context_id
        artifact = response.artifact_update.artifact
        text_bits = []
        for part in artifact.parts:
            content = part.WhichOneof('content')
            if content == 'text':
                text_bits.append(part.text)
            elif content == 'data':
                result.data.append(part.data)
        if text_bits:
            result.text = '\n'.join(text_bits)


def _semantic_value(value):
    """google.protobuf.Value -> plain Python (as parsed JSON)."""
    from google.protobuf.json_format import MessageToJson

    if value in (None, type(None)):
        return None
    try:
        return json.loads(MessageToJson(value))
    except Exception:
        return str(value)


async def a2a_send(
    url: str,
    text: str = '',
    data: object | None = None,
    context_id: str | None = None,
    task_id: str | None = None,
    timeout: float = REQUEST_TIMEOUT,
    strict: bool = True,
) -> A2aResult:
    """Sends one message and consumes the stream until the task completes.

    ``strict=False`` returns a partial result (with ``error`` set) instead
    of raising when the connection breaks mid-task — used by the restart
    drill, where the sales agent is killed on purpose while streaming.
    """
    parts = []
    if text:
        parts.append(new_text_part(text))
    if data is not None:
        parts.append(new_data_part(data))
    if not parts:
        parts.append(new_text_part(''))

    message = new_message(
        parts=parts, context_id=context_id, task_id=task_id,
        role=Role.ROLE_USER,
    )
    result = A2aResult()

    async with httpx.AsyncClient(timeout=timeout) as http:
        client = await create_client(
            url, ClientConfig(streaming=True, httpx_client=http)
        )
        try:
            async with client:
                request = SendMessageRequest(message=message)
                async for response in client.send_message(request):
                    await _collect_response(response, result)
        except Exception as exc:  # noqa: BLE001 - see strict flag above
            if strict:
                raise
            result.error = f'{type(exc).__name__}: {exc}'

    result.data = [  # normalise protobuf Values to plain Python
        normalize_numbers(_semantic_value(value)) for value in result.data
    ]
    if isinstance(result.final_state, int):  # TaskState enum -> name
        result.final_state = TaskState.Name(result.final_state)
    return result


async def a2a_get_task(url: str, task_id: str) -> dict | None:
    """Fetches a persisted task via tasks/get (works after a restart)."""
    async with httpx.AsyncClient(timeout=60) as http:
        client = await create_client(
            url, ClientConfig(streaming=False, httpx_client=http)
        )
        try:
            async with client:
                task = await client.get_task(GetTaskRequest(id=task_id))
                if task is None:
                    return None
                state = task.status.state
                if isinstance(state, int):
                    state = TaskState.Name(state)
                artifacts = []
                for artifact in getattr(task, 'artifacts', []):
                    for part in artifact.parts:
                        if part.WhichOneof('content') == 'text':
                            artifacts.append(part.text)
                return {
                    'id': task.id,
                    'context_id': task.context_id,
                    'state': state,
                    'artifacts': artifacts,
                }
        except Exception:  # noqa: BLE001 - task may not exist, server down...
            return None


def print_result(result: A2aResult, prefix: str = '') -> None:
    """Demo-friendly one-screen summary of an A2aResult."""
    print(f'{prefix}[{result.final_state}] task={result.task_id} '
          f'context={result.context_id}')
    if result.text:
        for line in result.text.splitlines():
            print(f'{prefix}   {line}')
    for payload in result.data:
        print(f'{prefix}   data: {json.dumps(payload, ensure_ascii=False)}')
