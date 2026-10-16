"""Minimal A2A server: Agent Card + JSON-RPC on Starlette/Uvicorn.

One process = one agent. To bring up two agents that talk to each other, start
two instances on different ports and point the second one at the first with
``--peer``.
"""

from __future__ import annotations

import argparse
import os

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn

from starlette.applications import Starlette

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
)
from a2a.utils import DEFAULT_RPC_URL, TransportProtocol
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT

from agent_executor import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OLLAMA_URL,
    ClaudeAgentExecutor,
    DelegatingAgentExecutor,
    EchoAgentExecutor,
    OllamaAgentExecutor,
    ReplyExecutor,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=os.getenv('A2A_HOST', '127.0.0.1'))
    parser.add_argument(
        '--port', type=int, default=int(os.getenv('A2A_PORT', '9999'))
    )
    parser.add_argument(
        '--mode',
        choices=['echo', 'ollama', 'delegate', 'claude'],
        default=os.getenv('AGENT_MODE', 'echo'),
        help='What the agent does with the message it receives.',
    )
    parser.add_argument(
        '--name', default=None, help='Name published in the Agent Card.'
    )
    parser.add_argument(
        '--model',
        default=None,
        help='LLM model (defaults to the one for the chosen mode).',
    )
    parser.add_argument(
        '--ollama-url', default=os.getenv('OLLAMA_URL', DEFAULT_OLLAMA_URL)
    )
    parser.add_argument(
        '--system', default=None, help='System prompt (ollama mode).'
    )
    parser.add_argument(
        '--peer',
        default=os.getenv('A2A_PEER_URL'),
        help='Base URL of the A2A agent to delegate to (delegate mode).',
    )
    parser.add_argument(
        '--public-url',
        default=os.getenv('A2A_PUBLIC_URL'),
        help='URL published in the Agent Card (useful behind a proxy).',
    )
    return parser.parse_args()


def build_executor(args: argparse.Namespace) -> ReplyExecutor:
    if args.mode == 'echo':
        return EchoAgentExecutor()

    if args.mode == 'ollama':
        return OllamaAgentExecutor(
            model=args.model or DEFAULT_OLLAMA_MODEL,
            base_url=args.ollama_url,
            system=args.system,
        )

    if args.mode == 'claude':
        return ClaudeAgentExecutor(model=args.model or DEFAULT_CLAUDE_MODEL)

    if not args.peer:
        raise SystemExit('delegate mode needs --peer http://host:port')

    # The reviewer is optional: with no model the agent forwards the answer.
    reviewer = (
        OllamaAgentExecutor(model=args.model, base_url=args.ollama_url)
        if args.model
        else None
    )
    return DelegatingAgentExecutor(peer_url=args.peer, reviewer=reviewer)


def build_agent_card(args: argparse.Namespace, public_url: str) -> AgentCard:
    """Agent Card served at /.well-known/agent-card.json."""
    descriptions = {
        'echo': 'Sample A2A agent: returns the text it received.',
        'ollama': f'Answers with the local model {args.model or DEFAULT_OLLAMA_MODEL} (Ollama).',
        'claude': f'Answers with {args.model or DEFAULT_CLAUDE_MODEL} (Claude API).',
        'delegate': f'Delegates the question to the A2A agent at {args.peer}.',
    }
    skills = {
        'echo': AgentSkill(
            id='echo',
            name='Echo',
            description='Repeats the user message.',
            tags=['echo', 'demo'],
            examples=['Hello, can you hear me?'],
        ),
        'ollama': AgentSkill(
            id='chat',
            name='Local chat',
            description='Answers questions with a local model.',
            tags=['chat', 'ollama', 'local'],
            examples=['Explain the A2A protocol in two sentences.'],
        ),
        'claude': AgentSkill(
            id='chat',
            name='Chat',
            description='Answers questions with Claude.',
            tags=['chat', 'claude'],
            examples=['Explain the A2A protocol in two sentences.'],
        ),
        'delegate': AgentSkill(
            id='delegate',
            name='A2A delegation',
            description='Forwards the question to another A2A agent and returns its answer.',
            tags=['a2a', 'orchestration'],
            examples=['Ask your peer what an Agent Card is.'],
        ),
    }

    return AgentCard(
        name=args.name or f'A2A {args.mode} agent',
        description=descriptions[args.mode],
        version='0.1.0',
        supported_interfaces=[
            AgentInterface(
                url=f'{public_url}{DEFAULT_RPC_URL}',
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=['text/plain'],
        default_output_modes=['text/plain'],
        skills=[skills[args.mode]],
    )


def build_app(args: argparse.Namespace) -> Starlette:
    public_url = args.public_url or f'http://{args.host}:{args.port}'
    agent_card = build_agent_card(args, public_url)
    executor = build_executor(args)

    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    routes = [
        *create_agent_card_routes(agent_card),
        *create_jsonrpc_routes(request_handler, rpc_url=DEFAULT_RPC_URL),
    ]

    # Starlette 1.x only takes lifespan (no more on_startup/on_shutdown).
    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await executor.aclose()

    return Starlette(routes=routes, lifespan=lifespan)


if __name__ == '__main__':
    arguments = parse_args()
    uvicorn.run(
        build_app(arguments), host=arguments.host, port=arguments.port
    )
