"""Test client: sends a message to an A2A agent and prints the answer.

Usage:
    python client_test.py "Hello, can you hear me?"
    python client_test.py --url http://127.0.0.1:9998 "Ask your peer"
"""

from __future__ import annotations

import argparse
import asyncio

from a2a.client import ClientConfig, create_client
from a2a.helpers import get_stream_response_text
from a2a.helpers.proto_helpers import new_text_message
from a2a.types import Role, SendMessageRequest


async def main(url: str, text: str) -> None:
    # Resolves /.well-known/agent-card.json and picks the transport (JSON-RPC).
    client = await create_client(url, ClientConfig(streaming=True))

    async with client:
        print(f'> {text}')
        request = SendMessageRequest(
            message=new_text_message(text, role=Role.ROLE_USER)
        )
        async for response in client.send_message(request):
            kind = response.WhichOneof('payload') or '?'
            reply = get_stream_response_text(response)
            print(f'[{kind}] {reply}' if reply else f'[{kind}]')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:9999')
    parser.add_argument('text', nargs='*', default=[])
    args = parser.parse_args()

    message = ' '.join(args.text) or 'Hello, can you hear me?'
    asyncio.run(main(args.url, message))
