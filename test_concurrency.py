"""Test 1 — Atomicity: concurrent buyers for the single last unit.

Boots the whole stack (unless --no-boot) and runs N client sessions that
all try to buy the one remaining unit of KD-LAST-03 at the same time.

PASS criteria (all must hold):
  1. exactly ONE session ends with a confirmed order,
  2. the other N-1 get a clean business rejection (SIN_STOCK) and their
     A2A tasks COMPLETED (business rejection != agent failure),
  3. the database shows exactly one reservation and one order, and the
     product stock is 0 (never negative).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

try:  # httpx solo está en el venv del proyecto: el aviso apunta al fix
    import httpx  # noqa: F401
except ModuleNotFoundError:
    raise SystemExit(
        'Faltan dependencias: este script necesita el venv del proyecto. '
        'Ejecuta:\n  .venv\\Scripts\\python.exe '
        + ' '.join(sys.argv)
        + '\no activa el venv antes: .venv\\Scripts\\Activate.ps1'
    )

from shop_client import a2a_send, call_mcp_tool
from shop_stack import Stack

PRODUCT = 'KD-LAST-03'
DEFAULT_SESSIONS = 8


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.checks = 0

    def check(self, condition: bool, description: str) -> None:
        self.checks += 1
        if condition:
            print(f'    [PASS] {description}')
        else:
            self.failures += 1
            print(f'    [FAIL] {description}')


async def _prepare_session(
    url: str, query: str = 'único'
):
    """Walks one session up to (and including) the selection step."""
    search = await a2a_send(url, text=f'busco un teclado {query}')
    selection = await a2a_send(
        url, text='elijo 1',
        context_id=search.context_id, task_id=search.task_id,
    )
    return selection.context_id, (selection.last_data() or {}).get(
        'product', {}
    )


async def run(
    sales_url: str,
    mcp_url: str,
    sessions: int = DEFAULT_SESSIONS,
    report: Report | None = None,
) -> bool:
    report = report or Report()
    print(f'  » Preparando {sessions} sesiones (busca + elige {PRODUCT})…')
    await call_mcp_tool(mcp_url, 'set_stock', {'code': PRODUCT, 'units': 1})
    stats_before = await call_mcp_tool(mcp_url, 'admin_stats', {})

    contexts = []
    for index in range(sessions):
        context_id, chosen = await _prepare_session(sales_url)
        contexts.append(context_id)
    print(f'    {len(contexts)} sesiones listas, todas con {PRODUCT} elegido')

    print('  » Disparando las compras EN PARALELO…')
    results = await asyncio.gather(*(
        a2a_send(sales_url, text='comprar', context_id=context_id)
        for context_id in contexts
    ))

    orders = []
    rejections = []
    broken = []
    for result in results:
        data = result.last_data() or {}
        if result.final_state != 'TASK_STATE_COMPLETED' or result.error:
            broken.append((result.final_state, result.error))
        elif data.get('type') == 'order':
            orders.append(data['order'])
        elif data.get('type') == 'rejection':
            rejections.append(data)

    stats_after = await call_mcp_tool(mcp_url, 'admin_stats', {})

    print(f'    pedidos: {len(orders)} · rechazos: {len(rejections)} '
          f'· tareas rotas: {len(broken)}')
    if broken:
        for state, error in broken:
            print(f'      broken task = {state} {error}')

    report.check(len(orders) == 1,
                 f'exactamente 1 pedido confirmado (hay {len(orders)})')
    report.check(len(rejections) == sessions - 1,
                 f'{sessions - 1} rechazos de negocio SIN_STOCK '
                 f'(hay {len(rejections)})')
    report.check(
        all(r.get('reason') == 'SIN_STOCK' for r in rejections),
        'todos los rechazos son por SIN_STOCK',
    )
    report.check(not broken,
                 'ninguna tarea termina en estado de error: los rechazos '
                 'completan la tarea (negocio != fallo)')
    report.check(
        stats_after['orders'] - stats_before['orders'] == 1,
        f"BD: exactamente 1 pedido nuevo "
        f"(delta={stats_after['orders'] - stats_before['orders']})",
    )
    report.check(
        stats_after['reservations'] - stats_before['reservations'] == 1,
        f"BD: exactamente 1 reserva nueva "
        f"(delta={stats_after['reservations'] - stats_before['reservations']})",
    )
    report.check(stats_after['stock'][PRODUCT] == 0,
                 f"BD: stock final de {PRODUCT} = 0 (sin negativos)")
    return report.failures == 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:9999')
    parser.add_argument('--mcp', default='http://127.0.0.1:9996')
    parser.add_argument('--sessions', type=int, default=DEFAULT_SESSIONS)
    parser.add_argument('--boot', action='store_true', default=True)
    parser.add_argument('--no-boot', dest='boot', action='store_false',
                        help='use an already-running stack')
    args = parser.parse_args()

    async def runner() -> bool:
        print('=' * 68)
        print(' TEST · atomicidad: N compradores, 1 unidad disponible')
        print('=' * 68)
        if args.boot:
            stack = Stack(log_prefix='  ')
            stack.start(reset=True)
            try:
                return await run(args.url, args.mcp, args.sessions)
            finally:
                stack.stop()
        return await run(args.url, args.mcp, args.sessions)

    ok = asyncio.run(runner())
    print('\nRESULTADO:', 'PASS \N{CHECK MARK}' if ok else 'FAIL \N{CROSS MARK}')
    raise SystemExit(0 if ok else 1)


if __name__ == '__main__':
    main()
