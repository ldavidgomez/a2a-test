"""Test 2 — Idempotency: repeated reservation/order requests.

Three layers of "say again":

  A. Session level — the client repeats "comprar" after the order exists:
     the sales agent returns the SAME order (replayed=True), no new
     reservation, no new order, no stock change.
  B. Reserve at MCP level — the same reserve_stock call twice with the
     same idempotency key: same reservation_id, replayed=True, the stock
     is decremented exactly once.
  C. Order at MCP level — create_order twice with the same key over the
     same reservation: same order_id, exactly one order row, and the
     reservation is consumed exactly once.

A duplicate with a DIFFERENT key over a consumed reservation is rejected
with RESERVA_YA_CONSUMIDA (genuine duplicate, not a retry).
"""

from __future__ import annotations

import argparse
import asyncio

from shop_client import a2a_send, call_mcp_tool
from shop_stack import Stack

SESSION_PRODUCT = 'KD-PRO-01'
MCP_PRODUCT = 'KD-RGB-02'


class Report:
    def __init__(self) -> None:
        self.failures = 0
        self.checks = 0

    def check(self, condition: bool, description: str) -> None:
        self.checks += 1
        if condition:
            print(f'    [PASS] {description}')
        else:
            print(f'    [FAIL] {description}')


async def run(
    sales_url: str, mcp_url: str, report: Report | None = None
) -> bool:
    report = report or Report()

    # ---------------- A. session-level replay -------------------------
    print('  » A · repetir «comprar» con el pedido ya creado (sesión A2A)…')
    await call_mcp_tool(mcp_url, 'set_stock',
                        {'code': SESSION_PRODUCT, 'units': 2})
    search = await a2a_send(sales_url, text='busco un teclado mecánico')
    select = await a2a_send(
        sales_url, text='elijo 1',
        context_id=search.context_id, task_id=search.task_id,
    )
    buy = await a2a_send(sales_url, text='comprar',
                         context_id=select.context_id)
    order = (buy.last_data() or {}).get('order', {})
    stats1 = await call_mcp_tool(mcp_url, 'admin_stats', {})

    replay = await a2a_send(sales_url, text='comprar',
                            context_id=select.context_id)
    replay_data = replay.last_data() or {}
    stats2 = await call_mcp_tool(mcp_url, 'admin_stats', {})

    report.check(
        buy.final_state == 'TASK_STATE_COMPLETED'
        and order.get('order_id'),
        f"compra inicial OK (pedido {order.get('order_id')})",
    )
    report.check(
        replay_data.get('replayed') is True
        and (replay_data.get('order') or {}).get('order_id')
        == order.get('order_id'),
        'repetir «comprar» devuelve el MISMO pedido (idempotencia de sesión)',
    )
    report.check(stats2 == stats1,
                 'BD sin cambios tras el replay (stock/reservas/pedidos)')

    # ---------------- B. MCP reserve replay --------------------------
    print('  » B · misma llamada reserve_stock dos veces (misma clave)…')
    await call_mcp_tool(mcp_url, 'set_stock',
                        {'code': MCP_PRODUCT, 'units': 2})
    key = 'test-idempotencia:reserve:1'
    first = await call_mcp_tool(mcp_url, 'reserve_stock', {
        'code': MCP_PRODUCT, 'qty': 1, 'idempotency_key': key,
    })
    second = await call_mcp_tool(mcp_url, 'reserve_stock', {
        'code': MCP_PRODUCT, 'qty': 1, 'idempotency_key': key,
    })
    stats3 = await call_mcp_tool(mcp_url, 'admin_stats', {})

    report.check(
        first.get('status') == 'CONFIRMED'
        and second.get('reservation_id') == first.get('reservation_id'),
        f"la reserva repetida devuelve el MISMO id "
        f"({first.get('reservation_id')})",
    )
    report.check(second.get('replayed') is True,
                 'el resultado está marcado como replay (replayed=True)')
    report.check(stats3['stock'][MCP_PRODUCT] == 1,
                 f'el stock bajó exactamente 1 unidad '
                 f'(quedan {stats3["stock"][MCP_PRODUCT]})')
    report.check(
        stats3['reservations'] - stats2['reservations'] == 1,
        f"solo 1 fila nueva de reserva "
        f"(delta={stats3['reservations'] - stats2['reservations']})",
    )

    # ---------------- C. order replay + duplicate ---------------------
    print('  » C · create_order repetido (misma clave) y duplicado real…')
    order_key = 'test-idempotencia:order:1'
    first_order = await call_mcp_tool(mcp_url, 'create_order', {
        'reservation_id': first.get('reservation_id'),
        'customer': 'Test',
        'idempotency_key': order_key,
    })
    stats_created = await call_mcp_tool(mcp_url, 'admin_stats', {})
    replay_order = await call_mcp_tool(mcp_url, 'create_order', {
        'reservation_id': first.get('reservation_id'),
        'customer': 'Test',
        'idempotency_key': order_key,
    })
    duplicate = await call_mcp_tool(mcp_url, 'create_order', {
        'reservation_id': first.get('reservation_id'),
        'customer': 'Test',
        'idempotency_key': 'test-idempotencia:order:DIFERENTE',
    })
    stats4 = await call_mcp_tool(mcp_url, 'admin_stats', {})

    report.check(
        replay_order.get('order_id') == first_order.get('order_id')
        and replay_order.get('replayed') is True,
        f"create_order repetido devuelve el MISMO pedido "
        f"({first_order.get('order_id')})",
    )
    report.check(
        duplicate.get('status') == 'REJECTED'
        and duplicate.get('reason') == 'RESERVA_YA_CONSUMIDA',
        'un duplicado con OTRA clave sobre reserva consumida se RECHAZA',
    )
    report.check(
        stats_created['orders'] - stats3['orders'] == 1,
        f"create_order dejó exactamente 1 pedido nuevo "
        f"(delta={stats_created['orders'] - stats3['orders']})",
    )
    report.check(stats4 == stats_created,
                 'BD sin cambios tras el replay y el duplicado: '
                 'ni pedidos ni reservas extra')

    return report.failures == 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:9999')
    parser.add_argument('--mcp', default='http://127.0.0.1:9996')
    parser.add_argument('--boot', action='store_true', default=True)
    parser.add_argument('--no-boot', dest='boot', action='store_false',
                        help='use an already-running stack')
    args = parser.parse_args()

    async def runner() -> bool:
        print('=' * 68)
        print(' TEST · idempotencia: reps de reserva y de pedido')
        print('=' * 68)
        if args.boot:
            stack = Stack(log_prefix='  ')
            stack.start(reset=True)
            try:
                return await run(args.url, args.mcp)
            finally:
                stack.stop()
        return await run(args.url, args.mcp)

    ok = asyncio.run(runner())
    print('\nRESULTADO:', 'PASS \N{CHECK MARK}' if ok else 'FAIL \N{CROSS MARK}')
    raise SystemExit(0 if ok else 1)


if __name__ == '__main__':
    main()
