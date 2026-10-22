"""Minimal A2A server: Agent Card + JSON-RPC on Starlette/Uvicorn.

One process = one agent. To bring up two agents that talk to each other, start
two instances on different ports and point the second one at the first with
``--peer``.
"""

from __future__ import annotations

import argparse
import logging
import os

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn

from starlette.applications import Starlette

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import DatabaseTaskStore, InMemoryTaskStore
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
)
from llm_client import PROVIDERS, build_llm_client
from shop_executors import SalesAgentExecutor, StockAgentExecutor
from shop_state import SalesSessionStore

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=os.getenv('A2A_HOST', '127.0.0.1'))
    parser.add_argument(
        '--port', type=int, default=int(os.getenv('A2A_PORT', '9999'))
    )
    parser.add_argument(
        '--mode',
        choices=['echo', 'ollama', 'delegate', 'claude', 'sales', 'stock'],
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
        help='Base URL of the A2A agent to delegate to (delegate/sales modes).',
    )
    parser.add_argument(
        '--public-url',
        default=os.getenv('A2A_PUBLIC_URL'),
        help='URL published in the Agent Card (useful behind a proxy).',
    )
    # --- purchase workflow (sales / stock) -----------------------------
    parser.add_argument(
        '--mcp-url',
        default=os.getenv('MCP_URL', 'http://127.0.0.1:9996'),
        help='Base URL of the shop MCP server (sales/stock modes).',
    )
    parser.add_argument(
        '--sales-db',
        default=os.getenv('A2A_SALES_DB', os.path.join('data', 'sales.db')),
        help='SQLite file for the sales sessions (sales mode).',
    )
    parser.add_argument(
        '--task-db',
        default=os.getenv('A2A_TASK_DB'),
        help='SQLite file for the A2A task store. Default for sales/stock: '
        'data/tasks_<mode>.db; other modes keep tasks in memory.',
    )
    parser.add_argument(
        '--llm',
        choices=PROVIDERS,
        default=os.getenv(
            'AGENT_LLM', 'glm' if os.getenv('GLM_API_KEY') else 'none'
        ),
        help='LLM that decorates the sales answers (glm | ollama | none).',
    )
    parser.add_argument(
        '--llm-model', default=os.getenv('GLM_MODEL'),
        help='Model name for the LLM (defaults: glm-5-3 / llama3.2).',
    )
    parser.add_argument(
        '--llm-base-url', default=os.getenv('GLM_BASE_URL'),
        help='OpenAI-compatible base URL (defaults per provider).',
    )
    parser.add_argument(
        '--pause-after',
        choices=['reserve'],
        default=os.getenv('A2A_PAUSE_AFTER'),
        help='Crash-drill checkpoint: sleep after the reservation is '
        'confirmed, before creating the order (sales mode).',
    )
    parser.add_argument(
        '--reset', action='store_true',
        help='Delete task/session DB files before starting.',
    )
    return parser.parse_args()


def build_executor(args: argparse.Namespace):
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

    if args.mode == 'stock':
        return StockAgentExecutor(mcp_url=args.mcp_url)

    if args.mode == 'sales':
        if not args.peer:
            raise SystemExit('sales mode needs --peer http://host:port '
                             '(the stock agent URL)')
        llm = build_llm_client(
            args.llm,
            model=args.llm_model,
            base_url=args.llm_base_url,
            ollama_url=args.ollama_url,
        )
        sessions = SalesSessionStore(args.sales_db)
        executor = SalesAgentExecutor(
            peer_url=args.peer,
            mcp_url=args.mcp_url,
            sessions=sessions,
            llm=llm,
            pause_after=args.pause_after,
        )
        return executor

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
        'sales': 'Shop sales agent: proposes keyboards, reserves via the '
                 'stock agent (A2A) and creates the order (MCP).',
        'stock': 'Stock agent: queries inventory and reserves units '
                 'through the shop MCP server.',
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
        'sales': AgentSkill(
            id='purchase',
            name='Keyboard purchase assistant',
            description='Client-facing purchase workflow: search, selection '
                        '(input-required), reservation and order.',
            tags=['a2a', 'mcp', 'saga', 'purchase'],
            examples=['Busco un teclado mecánico', 'elijo 1', 'comprar'],
        ),
        'stock': AgentSkill(
            id='inventory',
            name='Inventory and reservations',
            description='Searches products and reserves stock units '
                        'through MCP with atomic, idempotent semantics.',
            tags=['a2a', 'mcp', 'stock'],
            examples=['{"action": "search", "query": "tkl"}'],
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


def _default_task_db(args: argparse.Namespace) -> str | None:
    """sales/stock persist tasks by default (they must survive a restart)."""
    if args.task_db:
        return args.task_db
    if args.mode in ('sales', 'stock'):
        return os.path.join('data', f'tasks_{args.mode}.db')
    return None


def build_task_store(args: argparse.Namespace):
    """InMemory by default; DatabaseTaskStore (SQLite) for sales/stock."""
    task_db = _default_task_db(args)
    if not task_db:
        return InMemoryTaskStore(), None

    task_db = task_db.replace('\\', '/')
    directory = os.path.dirname(task_db)
    if directory:
        os.makedirs(directory, exist_ok=True)
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f'sqlite+aiosqlite:///{task_db}')
    return DatabaseTaskStore(engine=engine), task_db


def build_app(args: argparse.Namespace) -> Starlette:
    public_url = args.public_url or f'http://{args.host}:{args.port}'
    agent_card = build_agent_card(args, public_url)
    executor = build_executor(args)
    task_store, task_db = build_task_store(args)

    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=agent_card,
    )

    routes = [
        *create_agent_card_routes(agent_card),
        *create_jsonrpc_routes(request_handler, rpc_url=DEFAULT_RPC_URL),
    ]

    # Starlette 1.x only takes lifespan (no more on_startup/on_shutdown).
    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        if task_db:
            await task_store.initialize()
            logger.info('[%s] task store ready at %s', args.mode, task_db)
        startup = getattr(executor, 'startup', None)
        if startup is not None:
            await startup()  # recovery pass (sales)
        try:
            yield
        finally:
            await executor.aclose()

    return Starlette(routes=routes, lifespan=lifespan)


if __name__ == '__main__':
    logging.basicConfig(
        level=os.getenv('LOG_LEVEL', 'INFO').upper(),
        format='[%(name)s] %(asctime)s %(levelname)s %(message)s',
    )
    arguments = parse_args()

    if arguments.reset:
        for path in filter(None, (arguments.sales_db,
                                  _default_task_db(arguments))):
            for suffix in ('', '-wal', '-shm'):
                try:
                    os.remove(path + suffix)
                    print(f'[reset] deleted {path + suffix}')
                except FileNotFoundError:
                    pass
                except OSError as exc:  # e.g. held by another process
                    print(f'[reset] could not delete {path + suffix}: {exc}')

    uvicorn.run(
        build_app(arguments), host=arguments.host, port=arguments.port
    )
