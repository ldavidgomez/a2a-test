"""One-shot demo of the keyboard purchase workflow (A2A + MCP).

Boots the whole stack (MCP server, stock agent, sales agent), then runs:

  ACTO 1 · Camino feliz — the client asks for a keyboard, the sales agent
          delegates the search to the stock agent over A2A (which queries
          inventory through MCP), proposes products and parks the task in
          input-required until the client picks one; then reserve (A2A) +
          order (MCP) complete the purchase.
  ACTO 2 · Drill de reinicio — the purchase task is interrupted on
          purpose (crash between reservation and order); the stack is
          relaunched and the SAME A2A task is resumed from durable state
          and completed.
  ACTO 3 · Las dos pruebas críticas — last-unit concurrency (atomicity)
          and repeated requests (idempotency), each with PASS/FAIL verdicts.

Usage:
    python demo_purchase.py                 # deterministic (no LLM)
    python demo_purchase.py --llm glm      # with GLM_API_KEY set
    python demo_purchase.py --llm ollama   # local Ollama
    python demo_purchase.py --acts 2       # only the restart drill
"""

from __future__ import annotations

import argparse
import asyncio
import os
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

from shop_client import a2a_get_task, a2a_send, call_mcp_tool
from shop_stack import Stack
from llm_client import load_config, resolve_provider_name

import test_concurrency
import test_idempotency


def banner(title: str) -> None:
    line = '=' * 68
    print(f'\n{line}\n {title}\n{line}')


class Failed(Exception):
    pass


def require(condition: bool, message: str, ok: list, bad: list) -> None:
    if condition:
        ok.append(message)
        print(f'  [PASS] {message}')
    else:
        bad.append(message)
        print(f'  [FAIL] {message}')


# ----------------------------------------------------------------------
async def act_one(stack: Stack) -> None:
    banner('ACTO 1 · Compra de teclado (happy path)')
    url = stack.sales_url

    print('  cliente: «busco un teclado»')
    search = await a2a_send(url, text='busco un teclado')
    print('  [sales] responde con la propuesta y espera elección '
          '(input-required)')
    products = (search.last_data() or {}).get('products', [])
    print(f'  oferta: {len(products)} teclado(s): '
          + ', '.join(f'{p["code"]} ({p["stock"]} uds)' for p in products))

    print('  cliente: «elijo 1» (reanuda la MISMA tarea abierta)')
    select = await a2a_send(
        url, text='elijo 1',
        context_id=search.context_id, task_id=search.task_id,
    )
    chosen = (select.last_data() or {}).get('product', {})

    print(f'  cliente: «comprar» {chosen.get("code", "")}')
    buy = await a2a_send(url, text='comprar', context_id=select.context_id)
    order = (buy.last_data() or {}).get('order', {})

    ok, bad = [], []
    require(search.final_state == 'TASK_STATE_INPUT_REQUIRED',
            'la búsqueda deja la tarea en input-required', ok, bad)
    require(select.task_id == search.task_id,
            'la elección continúa la misma tarea (taskId)', ok, bad)
    require(select.final_state == 'TASK_STATE_COMPLETED',
            'la tarea de búsqueda se cierra al elegir', ok, bad)
    require(order.get('order_id', '').startswith('PED-'),
            f"pedido creado: {order.get('order_id')}", ok, bad)
    if bad:
        raise Failed('ACTO 1 falló')
    print(f'  respuesta final:\n    {buy.text}')


# ----------------------------------------------------------------------
async def act_two(stack: Stack) -> None:
    banner('ACTO 2 · Drill de reinicio (tarea interrumpida y recuperada)')
    url = stack.sales_url

    print('  reinicio del agente de ventas con --pause-after reserve…')
    stack.kill_agent('sales')
    stack.restart_sales(extra_args=['--pause-after', 'reserve'])

    search = await a2a_send(url, text='busco un teclado mecánico')
    select = await a2a_send(
        url, text='elijo 1',
        context_id=search.context_id, task_id=search.task_id,
    )
    print('  cliente: «comprar» → ventas reserva… y se detiene en el '
          'punto de cruce')

    buy = asyncio.create_task(a2a_send(
        url, text='comprar', context_id=select.context_id, strict=False,
    ))
    await asyncio.sleep(6)  # status events arrive, then it sleeps at the checkpoint
    print('  *** CRASH controlado: matamos ventas tras confirmar la reserva ***')
    stack.kill_agent('sales')
    partial = await asyncio.wait_for(buy, timeout=20)
    stuck_task_id = partial.task_id
    print(f'  tarea interrumpida: {stuck_task_id} '
          f'(stream del cliente roto: sí)')

    print('  relanzando el agente de ventas (arranque limpio, sin flag)…')
    stack.restart_sales()
    print('  (el recovery pass del arranque valida la compra pendiente '
          'desde estado durable)')

    retrieved = await a2a_get_task(url, stuck_task_id)
    print(f'  tasks/get tras el reinicio -> {retrieved and retrieved["state"]}')

    print('  cliente: «continuar» con el taskId de la tarea atascada…')
    resume = await a2a_send(
        url, text='continuar',
        context_id=partial.context_id, task_id=stuck_task_id,
    )
    order = (resume.last_data() or {}).get('order', {})
    final = await a2a_get_task(url, stuck_task_id)
    print(f'  tasks/get tras reanudar   -> {final and final["state"]}')

    ok, bad = [], []
    require(retrieved is not None
            and retrieved['state'] == 'TASK_STATE_WORKING',
            'la tarea interrumpida está persistida como WORKING', ok, bad)
    require(resume.final_state == 'TASK_STATE_COMPLETED',
            'la MISMA tarea se completa tras el reinicio', ok, bad)
    require(resume.task_id == stuck_task_id,
            'la tarea recuperada es la original (mismo taskId)', ok, bad)
    require(order.get('order_id', '').startswith('PED-'),
            f"pedido creado tras la recuperación: {order.get('order_id')}",
            ok, bad)
    if bad:
        raise Failed('ACTO 2 falló')
    print(f'  respuesta final:\n    {resume.text}')


# ----------------------------------------------------------------------
async def act_three(stack: Stack) -> None:
    banner('ACTO 3 · Pruebas críticas: atomicidad e idempotencia')

    print('\n>> prueba de concurrencia sobre la última unidad disponible')
    report1 = test_concurrency.Report()
    ok1 = await test_concurrency.run(
        stack.sales_url, stack.mcp_url, report=report1,
    )

    print('\n>> prueba de idempotencia (repetición de peticiones)')
    report2 = test_idempotency.Report()
    ok2 = await test_idempotency.run(stack.sales_url, stack.mcp_url,
                                     report=report2)

    if not (ok1 and ok2):
        raise Failed('ACTO 3 falló')


# ----------------------------------------------------------------------
async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--llm', default=None,
        help='none | glm | ollama | nombre de un proveedor del fichero. '
             'Default: AGENT_LLM > "default" del fichero > glm si hay '
             'GLM_API_KEY > none.',
    )
    parser.add_argument('--llm-config', default='llm_config.json',
                        help='fichero JSON con la sección "providers"')
    parser.add_argument('--acts', default='1,2,3',
                        help='comma-separated acts to run (default 1,2,3)')
    parser.add_argument('--keep-data', action='store_true',
                        help='do not wipe data/ before starting')
    args = parser.parse_args()

    try:
        llm_config = load_config(args.llm_config)
    except ValueError as exc:
        raise SystemExit(f'ERROR de configuración: {exc}') from None
    llm = args.llm or resolve_provider_name(None, llm_config)
    acts = {part for part in args.acts.split(',') if part}

    print('PoC compra de teclados · A2A (ventas <-> stock) + MCP (tienda)')
    print(f'LLM: {llm}' + (
        f' (fichero {args.llm_config})'
        if llm in (llm_config.get('providers') or {}) else ''))

    stack = Stack(llm=llm, llm_config=args.llm_config, log_prefix='  ')
    failures = 0
    try:
        banner('ARRANQUE · MCP (:9996) + stock (:9998) + ventas (:9999)')
        stack.start(reset=not args.keep_data)
        health = await call_mcp_tool(stack.mcp_url, 'admin_stats', {})
        print(f"  inventario inicial: {health['stock']}")

        if '1' in acts:
            await act_one(stack)
        if '2' in acts:
            await act_two(stack)
        if '3' in acts:
            await act_three(stack)
    except Failed as exc:
        failures += 1
        print(f'\n  *** {exc}')
    finally:
        stack.stop()

    banner('RESUMEN')
    for number in sorted(acts):
        if number in ('1', '2'):
            print(f'  ACTO {number}: '
                  + ('OK' if failures == 0 else 'revisar'))
    if '3' in acts:
        print('  ACTO 3 (pruebas): ' + ('PASS' if failures == 0 else 'FAIL'))
    print(f'\n  logs en {os.path.abspath(os.path.join("data", "logs"))}')
    raise SystemExit(1 if failures else 0)


if __name__ == '__main__':
    asyncio.run(main())
