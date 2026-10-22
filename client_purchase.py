"""Conversational client for the purchase workflow.

Usage examples (Spanish mini-grammar):

    client_purchase.py "busco un teclado"
    client_purchase.py --context <id> --task <id> "elijo 1"
    client_purchase.py --context <id> --task <id> "comprar"
    client_purchase.py --get-task <task-id>          # tasks/get (durable)
    client_purchase.py --interactive                 # REPL keeping ids

Each call prints the A2A event stream plus the structured answer (the
data part is the canonical one: tests and the demo assert on it). If the
previous task is waiting for input, re-send with ``--task`` to continue
the SAME task (that is what exercises A2A's input-required round trip).
"""

from __future__ import annotations

import argparse
import asyncio

from shop_client import a2a_get_task, a2a_send, print_result


async def send_once(
    url: str, message: str, context_id: str | None, task_id: str | None,
    show_events: bool = True,
):
    print(f'> {message}')
    result = await a2a_send(
        url, text=message, context_id=context_id, task_id=task_id
    )
    if show_events:
        kinds = ' '.join(f'[{kind}]' for kind in result.events)
        print(f'  events: {kinds}')
    print_result(result, prefix='  ' if show_events else '')
    return result


async def interactive(url: str) -> None:
    context_id: str | None = None
    task_id: str | None = None
    print(f'Cliente interactivo contra {url}. "salir" para terminar.')
    while True:
        try:
            message = input('\ntú> ').strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not message or message.lower() in ('salir', 'exit', 'quit'):
            break
        result = await send_once(url, message, context_id, task_id,
                                 show_events=False)
        context_id = result.context_id or context_id
        # Continue the open task while it waits for input, else a new one.
        task_id = result.task_id if result.final_state == 'INPUT_REQUIRED' else None


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--url', default='http://127.0.0.1:9999')
    parser.add_argument('text', nargs='*', default=[])
    parser.add_argument('--context', default=None,
                        help='contextId to continue a conversation')
    parser.add_argument('--task', default=None,
                        help='taskId to resume (input-required / stuck)')
    parser.add_argument('--get-task', default=None, metavar='TASK_ID',
                        help='call tasks/get and print the stored task')
    parser.add_argument('--interactive', action='store_true')
    args = parser.parse_args()

    if args.get_task:
        task = await a2a_get_task(args.url, args.get_task)
        print(task if task else 'task not found')
        return

    if args.interactive:
        await interactive(args.url)
        return

    message = ' '.join(args.text) or 'busco un teclado'
    await send_once(args.url, message, args.context, args.task)


if __name__ == '__main__':
    asyncio.run(main())
