"""Agent executors for the sample A2A server.

- ``EchoAgentExecutor``       -> returns the text it received.
- ``OllamaAgentExecutor``     -> answers with a local model served by Ollama.
- ``ClaudeAgentExecutor``     -> answers with the Claude API.
- ``DelegatingAgentExecutor`` -> delegates the question to ANOTHER A2A agent.
"""

from __future__ import annotations

import logging

import httpx

from a2a.client import ClientConfig, create_client
from a2a.helpers import get_stream_response_text, new_task, new_text_part
from a2a.helpers.proto_helpers import new_text_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Role, SendMessageRequest, TaskState


logger = logging.getLogger(__name__)

DEFAULT_CLAUDE_MODEL = 'claude-opus-5-5'
DEFAULT_OLLAMA_MODEL = 'llama3.2'
DEFAULT_OLLAMA_URL = 'http://localhost:11434'


class ReplyExecutor(AgentExecutor):
    """Base class: owns the task lifecycle and defers the text to ``reply``."""

    async def reply(self, user_text: str, context: RequestContext) -> str:
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release resources (HTTP clients). Subclasses override it."""

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        user_text = context.get_user_input()
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)

        # The first event must be the Task; status updates are fine after that.
        if context.current_task is None:
            await event_queue.enqueue_event(
                new_task(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    state=TaskState.TASK_STATE_SUBMITTED,
                    history=[context.message] if context.message else None,
                )
            )
        await updater.start_work()

        try:
            answer = await self.reply(user_text, context)
        except Exception as exc:  # noqa: BLE001 - the reason goes to the client
            # Uncaught, the client would only see a generic JSON-RPC error.
            logger.exception('The agent failed to answer')
            await updater.failed(
                updater.new_agent_message(
                    [new_text_part(f'{type(exc).__name__}: {exc}')]
                )
            )
            return

        # The result travels as an artifact, then the task is closed.
        await updater.add_artifact(
            [new_text_part(answer, media_type='text/plain')],
            name='response',
            last_chunk=True,
        )
        await updater.complete()

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


class EchoAgentExecutor(ReplyExecutor):
    """Returns the text it received. Handy to check the transport."""

    async def reply(self, user_text: str, context: RequestContext) -> str:
        return f'Echo: {user_text}'


class OllamaAgentExecutor(ReplyExecutor):
    """Answers with a local model served by Ollama (``POST /api/chat``)."""

    def __init__(
        self,
        model: str = DEFAULT_OLLAMA_MODEL,
        base_url: str = DEFAULT_OLLAMA_URL,
        system: str | None = None,
        timeout: float = 180.0,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip('/')
        self._system = system
        self._http = httpx.AsyncClient(timeout=timeout)

    async def reply(self, user_text: str, context: RequestContext) -> str:
        return await self.ask(user_text)

    async def ask(self, user_text: str) -> str:
        messages: list[dict[str, str]] = []
        if self._system:
            messages.append({'role': 'system', 'content': self._system})
        messages.append({'role': 'user', 'content': user_text})

        try:
            response = await self._http.post(
                f'{self._base_url}/api/chat',
                json={
                    'model': self._model,
                    'messages': messages,
                    'stream': False,
                },
            )
            response.raise_for_status()
        except httpx.ConnectError as exc:
            raise RuntimeError(
                f'Cannot reach Ollama at {self._base_url}. '
                'Check that it is running (ollama serve).'
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f'Ollama returned {exc.response.status_code}: '
                f'{exc.response.text[:200]}'
            ) from exc

        return response.json()['message']['content'].strip()

    async def aclose(self) -> None:
        await self._http.aclose()


class DelegatingAgentExecutor(ReplyExecutor):
    """An agent that asks ANOTHER A2A agent and returns its answer.

    With a ``reviewer``, the peer's text also goes through this agent's own
    model before replying: two agents, two models, and an A2A conversation
    between them.
    """

    def __init__(
        self,
        peer_url: str,
        reviewer: OllamaAgentExecutor | None = None,
        review_prompt: str = (
            'A peer agent replied with the following. Summarise it in one '
            'sentence, without adding any new information:\n\n{answer}'
        ),
    ) -> None:
        self._peer_url = peer_url
        self._reviewer = reviewer
        self._review_prompt = review_prompt
        self._peer_client = None
        self._peer_http = None

    async def reply(self, user_text: str, context: RequestContext) -> str:
        peer_answer = await self._ask_peer(user_text)

        if self._reviewer is None:
            return f'[answer from {self._peer_url}] {peer_answer}'

        summary = await self._reviewer.ask(
            self._review_prompt.format(answer=peer_answer)
        )
        return (
            f'[answer from {self._peer_url}] {peer_answer}\n\n'
            f'[orchestrator summary] {summary}'
        )

    async def _ask_peer(self, user_text: str) -> str:
        if self._peer_client is None:
            # Resolves the peer's Agent Card and picks the transport.
            # httpx's default timeout (5 s) is too short for a peer backed by
            # a local model, so the peer client gets its own.
            self._peer_http = httpx.AsyncClient(timeout=180.0)
            self._peer_client = await create_client(
                self._peer_url,
                ClientConfig(streaming=True, httpx_client=self._peer_http),
            )

        request = SendMessageRequest(
            message=new_text_message(user_text, role=Role.ROLE_USER)
        )
        chunks = [
            text
            async for response in self._peer_client.send_message(request)
            if (text := get_stream_response_text(response))
        ]
        return '\n'.join(chunks) if chunks else '(the peer did not answer)'

    async def aclose(self) -> None:
        if self._peer_client is not None:
            await self._peer_client.close()
        if self._peer_http is not None:
            await self._peer_http.aclose()
        if self._reviewer is not None:
            await self._reviewer.aclose()


class ClaudeAgentExecutor(ReplyExecutor):
    """Answers with the Claude API (needs ``ANTHROPIC_API_KEY``)."""

    def __init__(self, model: str = DEFAULT_CLAUDE_MODEL) -> None:
        self._model = model
        self._client = None

    async def reply(self, user_text: str, context: RequestContext) -> str:
        from anthropic import AsyncAnthropic  # deferred import: it is optional

        if self._client is None:
            # Reads ANTHROPIC_API_KEY (or the `ant auth login` profile).
            self._client = AsyncAnthropic()

        response = await self._client.beta.messages.create(
            model=self._model,
            max_tokens=1024,
            system='You are an A2A agent. Keep answers short.',
            messages=[{'role': 'user', 'content': user_text}],
            output_config={'effort': 'low'},
            # Server-side fallback in case the safety classifier refuses.
            betas=['server-side-fallback-2026-07-01'],
            fallbacks='default',
        )

        if response.stop_reason == 'refusal':
            return 'The model declined to answer this request.'

        return '\n'.join(
            block.text for block in response.content if block.type == 'text'
        )
