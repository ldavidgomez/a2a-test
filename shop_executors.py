"""Executors for the purchase-workflow agents (modes ``stock`` and ``sales``).

``StockAgentExecutor``
    Thin A2A-to-MCP bridge. Receives JSON actions over A2A (``search``,
    ``reserve``) and resolves them with the shop MCP server. Business
    rejections (out of stock) complete the task normally with a rejection
    artifact; only transport/agent errors ``fail`` the task.

``SalesAgentExecutor``
    The conversational front-end and saga coordinator:

    1. parses the client intent (deterministic Spanish mini-grammar; the
       optional LLM only *decorates* the answers),
    2. delegates search and reservation to the stock agent over A2A
       (one A2A client per call context),
    3. keeps per-``context_id`` business state in a durable session store,
    4. turns product selection into an A2A ``input-required`` round trip,
    5. runs reserve+order as one long task (crash point: ``--pause-after``),
    6. resumes interrupted purchases from durable state when the client
       replies (with ``taskId`` to continue the SAME task).

Idempotency keys are derived from the session, so retries at any layer
(client -> sales -> stock -> MCP) collapse onto the same reservation or
order.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata

from google.protobuf.json_format import MessageToJson

from a2a.client import ClientConfig, create_client
from a2a.helpers.proto_helpers import (
    new_data_part,
    new_message,
    new_task,
    new_text_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Role, SendMessageRequest, TaskState

import httpx

from llm_client import LlmClient
from shop_client import call_mcp_tool, normalize_numbers
from shop_state import SalesSessionStore, new_session

logger = logging.getLogger(__name__)

PAUSE_SECONDS = 600  # crash-simulation window (someone will kill us before)


# ----------------------------------------------------------------------
# shared helpers
# ----------------------------------------------------------------------
def _normalize(text: str) -> str:
    """lowercase + accent-free, for tolerant intent matching."""
    decomposed = unicodedata.normalize('NFD', text)
    stripped = ''.join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.lower().strip(' .!?¿¡,;:')


def semantic_value(value):
    """google.protobuf.Value -> plain Python (as parsed JSON, ints clean)."""
    if value is None:
        return None
    try:
        return normalize_numbers(json.loads(MessageToJson(value)))
    except Exception:  # noqa: BLE001 - best effort display
        return str(value)


def message_payload(message) -> tuple[str, dict | None]:
    """Returns (text, embedded data) of an A2A message."""
    text_bits: list[str] = []
    data: dict | None = None
    for part in message.parts:
        content = part.WhichOneof('content')
        if content == 'text':
            text_bits.append(part.text)
        elif content == 'data':
            payload = semantic_value(part.data)
            if isinstance(payload, dict):
                data = payload
    text = '\n'.join(text_bits).strip()
    if data is None and text.startswith('{'):
        try:
            candidate = json.loads(text)
            if isinstance(candidate, dict):
                data = candidate
        except json.JSONDecodeError:
            pass
    return text, data


async def ensure_task(
    context: RequestContext, event_queue: EventQueue
) -> TaskUpdater:
    """Enqueues the Task (only when new) and moves it to WORKING.

    On resume (message sent with the ``taskId`` of an open task —
    input-required or a task interrupted by a crash) the task already
    exists in the store, so no new Task event is emitted.
    """
    if context.current_task is None:
        await event_queue.enqueue_event(
            new_task(
                task_id=context.task_id,
                context_id=context.context_id,
                state=TaskState.TASK_STATE_SUBMITTED,
                history=[context.message] if context.message else None,
            )
        )
    updater = TaskUpdater(event_queue, context.task_id, context.context_id)
    await updater.start_work()
    return updater


async def add_response(
    updater: TaskUpdater, payload: dict, template: str,
    llm: LlmClient | None,
) -> None:
    """Artifact with prose + canonical JSON, then task completed."""
    text = template
    if llm is not None:
        try:
            text = await llm.complete(
                system=(
                    'Eres el comercial de una tienda de teclados. Reescribe '
                    'la plantilla de forma natural y breve (maximo 3 frases, '
                    'sin markdown). Regla absoluta: no inventes y no alteres '
                    'codigos, precios, cantidades, IDs, estados ni fechas. '
                    'Responde solo con el mensaje reescrito.'
                ),
                user=json.dumps(
                    {'plantilla': template, 'datos': payload},
                    ensure_ascii=False,
                ),
                max_tokens=300,
            )
        except Exception as exc:  # noqa: BLE001 - decoration is best-effort
            logger.warning('LLM decoration failed, using template: %s', exc)
            text = template
    await updater.add_artifact(
        [new_text_part(text or template), new_data_part(payload)],
        name='response',
        last_chunk=True,
    )
    await updater.complete()


async def ask_for_input(
    updater: TaskUpdater, payload: dict, prompt: str,
) -> None:
    """Artifact + task parked in INPUT_REQUIRED awaiting the user."""
    await updater.add_artifact(
        [new_text_part(prompt), new_data_part(payload)],
        name='response',
        last_chunk=True,
    )
    await updater.requires_input(
        updater.new_agent_message([new_text_part(prompt)])
    )


# ----------------------------------------------------------------------
# Stock agent
# ----------------------------------------------------------------------
class StockAgentExecutor(AgentExecutor):
    """A2A -> MCP bridge for inventory and reservations."""

    def __init__(self, mcp_url: str) -> None:
        self._mcp_url = mcp_url.rstrip('/')

    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        _text, payload = message_payload(context.message or new_text_message(''))
        action = (payload or {}).get('action')
        updater = await ensure_task(context, event_queue)

        try:
            if action == 'search':
                result = await call_mcp_tool(
                    self._mcp_url, 'search_stock',
                    {'query': payload.get('query', ''),
                     'category': payload.get('category', '')},
                )
                reply = {
                    'type': 'stock_search',
                    'query': result.get('query'),
                    'count': result.get('count', 0),
                    'products': result.get('products', []),
                }
            elif action == 'reserve':
                result = await call_mcp_tool(
                    self._mcp_url, 'reserve_stock',
                    {'code': payload['code'],
                     'qty': int(payload.get('qty', 1)),
                     'idempotency_key': payload['idempotency_key']},
                )
                reply = {
                    'type': 'reservation',
                    'code': payload['code'],
                    'result': result,
                    'status': result.get('status', '?'),
                }
            elif action == 'get_reservation':
                result = await call_mcp_tool(
                    self._mcp_url, 'get_reservation',
                    {'reservation_id': payload['reservation_id']},
                )
                reply = {'type': 'reservation_lookup', 'result': result}
            else:
                reply = {
                    'type': 'error',
                    'reason': 'ACCION_DESCONOCIDA',
                    'detail': f'stock agent got action={action!r}',
                }
        except Exception as exc:  # noqa: BLE001 - transport errors fail
            logger.exception('stock agent failed')
            await updater.failed(
                updater.new_agent_message(
                    [new_text_part(f'{type(exc).__name__}: {exc}')]
                )
            )
            return

        # Business rejections complete the task; failed is for agent errors.
        await updater.add_artifact(
            [new_text_part(json.dumps(reply, ensure_ascii=False)),
             new_data_part(reply)],
            name='response',
            last_chunk=True,
        )
        await updater.complete()

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


# ----------------------------------------------------------------------
# Sales agent — intent parsing
# ----------------------------------------------------------------------
_BUY_EXACT = re.compile(
    r'^(?:comprar|reservar|reservalo|reservarlo|confirmar|confirmalo|pagar|'
    r'pagalo|finalizar|finalizalo|dale|hazlo|adelante|sigue|seguir|continuar|'
    r'reintentar|intentalo|lo quiero|lo compro|lo pillo|sigamos)[\s.!¿?]*$'
)
_SEARCH_PREFIX = re.compile(
    r'^(?:busco|buscar|busca|buscame|buscame|necesito|dame|ensename|muestrame|'
    r'muestrame|hazme una busqueda|tienes|hay|quieren|quiero|quisiera|'
    r'me gustaria|quiero ver|quiero comprar)\b(.*)$'
)
_SELECT_NUM = re.compile(
    r'(?:elijo|elige|eligo|escogo|escojo|cojo|tomo|quiero el|dame el|'
    r'el numero|numero|num|opcion|la opcion|el modelo|modelo)'
    r'[^0-9]{0,14}(\d{1,2})'
)
_STATUS = re.compile(
    r'^(?:estado|estatus|situacion|como va|como vamos|como lo llevas|'
    r'en que estamos|que tal|en que punto|tengo algo pendiente)'
)
_CANCEL = re.compile(
    r'^(?:cancelar|cancela|cancelo|reinicia|reiniciar|empezar de nuevo|'
    r'borra todo|olvídalo|olvidalo)'
)
_CODE = re.compile(r'\b([A-Z]{2,4}(?:-[A-Z0-9]+){1,2})\b')


def _query_after_verb(rest: str) -> str:
    rest = rest.strip(' .!?')
    rest = re.sub(
        r'^(?:comprar|ver|mirar|comprar un|comprar una|un|una|unos|unas|'
        r'el|la|los|las|algun|alguna|algún|alguna|mi|para)\b\s*', '', rest,
    ).strip()
    return rest


def parse_intent(text: str, session: dict) -> dict:
    """Maps a client sentence to {intent, ...} — deterministic."""
    norm = _normalize(text)

    if _CANCEL.match(norm):
        return {'intent': 'cancel'}
    if _STATUS.match(norm):
        return {'intent': 'status'}

    # Selection: "elijo 2" / "el numero 3" / "ese" / a product code.
    if re.match(r'^elijo\b|^elige\b|^escogo\b|^escojo\b', norm) or \
            re.match(r'^(?:ese|ese mismo|el mismo|aquel)\b', norm) or \
            _SELECT_NUM.search(norm):
        number = _SELECT_NUM.search(norm)
        code = _CODE.search(text.upper())
        if number:
            return {'intent': 'select', 'index': int(number.group(1)) - 1,
                    'raw': number.group(1)}
        if code:
            return {'intent': 'select', 'code': code.group(1)}
        if re.match(r'^(?:ese|ese mismo|el mismo|aquel)\b', norm):
            return {'intent': 'select', 'any': True}
    if _BUY_EXACT.match(norm):
        return {'intent': 'buy'}

    matched = _SEARCH_PREFIX.match(norm)
    if matched:
        query = _query_after_verb(matched.group(1))
        return {'intent': 'search', 'query': query or 'teclado'}

    if session.get('step') == 'new':
        # First message that mentions keyboards: treat as a search.
        if 'teclado' in norm or 'keyboard' in norm:
            return {'intent': 'search', 'query': 'teclado'}

    return {'intent': 'chat', 'text': text}


# ----------------------------------------------------------------------
# Sales agent
# ----------------------------------------------------------------------
class SalesAgentExecutor(AgentExecutor):
    """Conversational front-end coordinating stock (A2A) and orders (MCP)."""

    def __init__(
        self,
        peer_url: str,
        mcp_url: str,
        sessions: SalesSessionStore,
        llm: LlmClient | None = None,
        pause_after: str | None = None,
    ) -> None:
        self._peer_url = peer_url
        self._mcp_url = mcp_url.rstrip('/')
        self._sessions = sessions
        self._llm = llm
        self._pause_after = pause_after
        self._peer_client = None
        self._peer_http: httpx.AsyncClient | None = None

    # -- infrastructure -------------------------------------------------
    async def startup(self) -> None:
        """Recovery pass after a restart: validate pending purchases."""
        pending = self._sessions.by_step('reserved')
        for session in pending:
            reservation = session.get('reservation') or {}
            result = None
            try:
                result = await call_mcp_tool(
                    self._mcp_url, 'get_reservation',
                    {'reservation_id': reservation.get('reservation_id', '')},
                )
                status = result.get('status') if result else None
            except Exception as exc:  # noqa: BLE001 - log and move on
                logger.warning('recovery lookup failed for %s: %s',
                               reservation.get('reservation_id'), exc)
                status = 'UNKNOWN'
            if status == 'EXPIRED':
                session['step'] = 'expired'
                session['attempt'] = session.get('attempt', 0) + 1
                self._sessions.save(session)
                status = 'EXPIRED (marcada, hay que reintentar)'
            logger.info(
                '[sales recovery] sesión %s pendiente con reserva %s -> %s',
                session['context_id'][:8],
                reservation.get('reservation_id'), status or '?',
            )
        if pending:
            logger.info(
                '[sales recovery] %d compra(s) interrumpida(s) quedan '
                'recuperables desde estado durable', len(pending),
            )

    async def aclose(self) -> None:
        if self._peer_client is not None:
            await self._peer_client.close()
        if self._peer_http is not None:
            await self._peer_http.aclose()
        if self._llm is not None:
            await self._llm.aclose()
        self._sessions.close()

    async def cancel(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()

    # -- A2A peer calls --------------------------------------------------
    async def _ask_peer(self, payload: dict) -> dict:
        """Sends one structured request to the stock agent; returns its data."""
        if self._peer_client is None:
            # Ollama-backed peers can be slow, so the peer gets its own
            # long-lived client with a generous timeout.
            self._peer_http = httpx.AsyncClient(timeout=httpx.Timeout(180.0))
            try:
                self._peer_client = await create_client(
                    self._peer_url,
                    ClientConfig(streaming=True, httpx_client=self._peer_http),
                )
            except httpx.HTTPError as exc:
                raise RuntimeError(
                    f'No se puede contactar con el agente de stock en '
                    f'{self._peer_url}: {exc}'
                ) from exc

        message = new_message(
            parts=[new_text_part(json.dumps(payload, ensure_ascii=False)),
                   new_data_part(payload)],
            role=Role.ROLE_USER,
        )
        try:
            chunks: list = []
            async for response in self._peer_client.send_message(
                SendMessageRequest(message=message)
            ):
                kind = response.WhichOneof('payload')
                if kind == 'artifact_update':
                    for part in response.artifact_update.artifact.parts:
                        if part.WhichOneof('content') == 'data':
                            chunks.append(semantic_value(part.data))
            for chunk in reversed(chunks):
                if isinstance(chunk, dict):
                    return chunk
            raise RuntimeError('el agente de stock no devolvió datos')
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - wrap transport errors
            raise RuntimeError(
                f'Fallo al hablar con el agente de stock: {exc}'
            ) from exc

    async def _status_note(self, updater: TaskUpdater, text: str) -> None:
        await updater.update_status(
            TaskState.TASK_STATE_WORKING,
            message=updater.new_agent_message([new_text_part(text)]),
        )

    # -- main execute ----------------------------------------------------
    async def execute(
        self, context: RequestContext, event_queue: EventQueue
    ) -> None:
        text = (context.get_user_input() or '').strip()
        session = self._sessions.get(context.context_id) or new_session(
            context.context_id
        )
        intent = parse_intent(text or '', session)
        updater = await ensure_task(context, event_queue)

        try:
            handler = {
                'search': self._do_search,
                'select': self._do_select,
                'buy': self._do_buy,
                'status': self._do_status,
                'cancel': self._do_cancel,
                'chat': self._do_chat,
            }[intent['intent']]
            await handler(intent, session, updater, context, text)
        except Exception as exc:  # noqa: BLE001 - protocol-level failure
            logger.exception('sales agent failed')
            await updater.failed(
                updater.new_agent_message(
                    [new_text_part(f'{type(exc).__name__}: {exc}')]
                )
            )

    # -- handlers ---------------------------------------------------------
    async def _do_search(self, intent, session, updater, context, text):
        query = intent['query']
        await self._status_note(
            updater, f'Consultando el stock de «{query}» via el agente de stock…'
        )
        answer = await self._ask_peer({'action': 'search', 'query': query})
        products = answer.get('products', [])

        session['step'] = 'searched'
        session['query'] = query
        session['proposals'] = products
        session['selection'] = None
        self._sessions.save(session)

        payload = {
            'type': 'proposal',
            'query': query,
            'count': len(products),
            'products': products,
        }
        if not products:
            template = (
                f'No tengo teclados que coincidan con «{query}». '
                'Prueba con otra búsqueda (mecánico, TKL, gaming…).'
            )
            await add_response(updater, payload, template, self._llm)
            return

        lines = [
            f'{index}. {product["name"]} ({product["code"]}) — '
            f'{product["price_eur"]:.2f} € — {product["stock"]} uds.'
            for index, product in enumerate(products, start=1)
        ]
        prompt = (
            f'He encontrado {len(products)} teclados para «{query}»:\n'
            + '\n'.join(lines)
            + '\n\nDime «elijo <número>» (o el código) para continuar.'
        )
        await ask_for_input(updater, payload, prompt)

    async def _do_select(self, intent, session, updater, context, text):
        proposals = session.get('proposals') or []
        if not proposals:
            await add_response(
                updater,
                {'type': 'error', 'reason': 'SIN_PROPUESTAS'},
                'Todavía no te he propuesto nada: dime primero qué teclado '
                'buscas (por ejemplo, «busco un teclado mecánico»).',
                self._llm,
            )
            return

        chosen = None
        if 'code' in intent:
            for product in proposals:
                if product['code'].upper() == intent['code']:
                    chosen = product
                    break
        elif intent.get('any') and len(proposals) == 1:
            chosen = proposals[0]
        elif 'index' in intent and 0 <= intent['index'] < len(proposals):
            chosen = proposals[intent['index']]

        if chosen is None:
            prompt = (
                'No me ha quedado claro cuál eliges. Di «elijo <número>» '
                f'entre 1 y {len(proposals)}, o el código del producto.'
            )
            await ask_for_input(
                updater,
                {'type': 'proposal', 'count': len(proposals),
                 'products': proposals, 'retry': True},
                prompt,
            )
            return

        session['step'] = 'selected'
        session['selection'] = chosen
        self._sessions.save(session)

        payload = {'type': 'selection', 'product': chosen}
        template = (
            f'Elegido: {chosen["name"]} ({chosen["code"]}) a '
            f'{chosen["price_eur"]:.2f} €. Di «comprar» y lo reservo.'
        )
        await add_response(updater, payload, template, self._llm)

    async def _do_buy(self, intent, session, updater, context, text):
        step = session.get('step')

        # Session-level idempotency: repeat 'comprar' returns the order.
        if step == 'ordered' and session.get('order'):
            order = session['order']
            payload = {'type': 'order', 'order': order, 'replayed': True}
            template = (
                f'Ese pedido ya estaba hecho: {order["order_id"]} '
                f'({order.get("code", "")}, {order.get("total_eur", 0):.2f} €).'
            )
            await add_response(updater, payload, template, self._llm)
            return

        if step == 'expired':
            # TTL went by: new attempt, fresh idempotency key.
            session['attempt'] = session.get('attempt', 0) + 1
            session['step'] = 'selected'
            session['reservation'] = None

        if step in ('new', 'searched') and not session.get('selection'):
            prompt = (
                'Primero busca y elige un teclado: «busco un teclado…» y '
                'después «elijo <número>».'
            )
            await ask_for_input(
                updater, {'type': 'error', 'reason': 'SIN_SELECCION'}, prompt
            )
            return

        selection = session['selection']
        reservation = session.get('reservation') or {}

        # ---- reserve phase (skipped when resuming an interrupted buy)
        if session.get('step') != 'reserved':
            await self._status_note(
                updater,
                f'Solicitando al agente de stock la reserva de '
                f'{selection["code"]} x1…',
            )
            key = (
                f"reserve:{context.context_id}:{selection['code']}:"
                f"{session.get('attempt', 0)}"
            )
            answer = await self._ask_peer({
                'action': 'reserve',
                'code': selection['code'],
                'qty': 1,
                'idempotency_key': key,
            })
            result = answer.get('result') or {}
            if result.get('status') != 'CONFIRMED':
                reason = result.get('reason', 'desconocido')
                detail = ('sin unidades' if reason == 'SIN_STOCK'
                          else f'motivo: {reason}')
                payload = {
                    'type': 'rejection',
                    'reason': reason,
                    'code': selection['code'],
                    'result': result,
                }
                template = (
                    f'No he podido reservar {selection["code"]} ({detail}). '
                    'Puedes elegir otro teclado de la propuesta.'
                )
                await add_response(updater, payload, template, self._llm)
                return

            reservation = result
            session['step'] = 'reserved'
            session['reservation'] = reservation
            # Durable BEFORE the pause: this is what makes recovery possible.
            session['purchase_task_id'] = context.task_id
            self._sessions.save(session)
            await self._status_note(
                updater,
                f'Reserva {reservation["reservation_id"]} confirmada '
                f'(caduca {reservation.get("expires_at", "?")[11:19]} UTC); '
                'creando el pedido…',
            )
            if self._pause_after == 'reserve':
                logger.warning(
                    '[pause-after reserve] tarea %s dormida %ds: este es '
                    'el punto de cruce planificado (mátame y reinicia)',
                    context.task_id, PAUSE_SECONDS,
                )
                await asyncio.sleep(PAUSE_SECONDS)

        # ---- order phase (also the resume path after a crash/restart)
        if session.get('step') == 'reserved':
            await self._status_note(
                updater,
                'Creando el pedido sobre la reserva '
                f'{reservation["reservation_id"]}…'
                if reservation else 'Creando el pedido…',
            )
        order = await call_mcp_tool(
            self._mcp_url, 'create_order',
            {
                'reservation_id': reservation.get('reservation_id'),
                'customer': 'Cliente A2A',
                'idempotency_key': (
                    f"order:{context.context_id}:"
                    f"{reservation.get('reservation_id')}"
                ),
            },
        )
        if not isinstance(order, dict) or order.get('status') != 'CREATED':
            reason = (order or {}).get('reason', 'desconocido') \
                if isinstance(order, dict) else str(order)
            if reason == 'RESERVA_CADUCADA':
                session['step'] = 'expired'
                self._sessions.save(session)
            payload = {
                'type': 'rejection',
                'reason': reason,
                'reservation_id': reservation.get('reservation_id'),
            }
            template = (
                f'No pude crear el pedido ({reason}). Di «comprar» de nuevo '
                'para reintentarlo con una reserva nueva.'
            )
            await add_response(updater, payload, template, self._llm)
            return

        session['step'] = 'ordered'
        session['order'] = order
        session['purchase_task_id'] = None
        self._sessions.save(session)
        payload = {'type': 'order', 'order': order}
        template = (
            f'¡Compra hecha! Pedido {order["order_id"]}: {order.get("code")} '
            f'x{order.get("qty")} por {order.get("total_eur", 0):.2f} € '
            f'(reserva {order.get("reservation_id")}).'
        )
        await add_response(updater, payload, template, self._llm)

    async def _do_status(self, intent, session, updater, context, text):
        payload = {
            'type': 'session',
            'step': session.get('step'),
            'query': session.get('query'),
            'selection': session.get('selection'),
            'reservation': session.get('reservation'),
            'order': session.get('order'),
        }
        if session.get('step') == 'reserved':
            template = (
                'Tienes la reserva '
                f'{session["reservation"]["reservation_id"]} confirmada y '
                'pendiente de pedido. Di «continuar» y la completo.'
            )
        elif session.get('step') == 'ordered':
            template = (
                f'Compra completada: pedido {session["order"]["order_id"]}.'
            )
        else:
            template = (
                f'Estamos en el paso «{session.get("step")}». '
                'Puedes buscar, elegir o comprar.'
            )
        await add_response(updater, payload, template, self._llm)

    async def _do_cancel(self, intent, session, updater, context, text):
        self._sessions.delete(context.context_id)
        payload = {'type': 'cancelled'}
        await add_response(
            updater, payload,
            'Conversación reiniciada. ¿Qué teclado buscas?', self._llm,
        )

    async def _do_chat(self, intent, session, updater, context, text):
        if self._llm is None:
            payload = {'type': 'chat', 'hint': True}
            template = (
                'Puedo buscarte teclados («busco un teclado mecánico»), '
                'que elijas uno («elijo 1») y comprarlo («comprar»).'
            )
            await add_response(updater, payload, template, None)
            return
        try:
            reply = await self._llm.complete(
                system=(
                    'Eres el comercial simpatico y eficiente de una tienda '
                    'online de teclados. Estado de la conversacion (JSON): '
                    + json.dumps(
                        {k: session.get(k) for k in
                         ('step', 'query', 'selection', 'order')},
                        ensure_ascii=False, default=str)
                    + '. Comandos disponibles: busco <criterio>, elijo '
                    '<numero>, comprar, estado, cancelar. Guia al cliente '
                    'hacia ellos en una o dos frases; no inventes productos.'
                ),
                user=text,
                max_tokens=300,
            )
        except Exception as exc:  # noqa: BLE001 - decoration is best-effort
            logger.warning('LLM chat failed: %s', exc)
            reply = (
                'Puedo buscarte teclados («busco un teclado»), que elijas '
                'uno («elijo 1») y comprarlo («comprar»).'
            )
        payload = {'type': 'chat'}
        await add_response(updater, payload, reply, None)
