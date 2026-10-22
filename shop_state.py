"""Durable per-context sessions for the sales agent.

The A2A task store already persists protocol state (tasks, events); this
store persists *business* state — what was proposed, what was selected,
which reservation/order belongs to the conversation. Together they are
what makes the interrupted purchase recoverable after a restart: the
stuck A2A task carries the protocol history, the session record carries
the saga position (``step``).

Session record (JSON):

- step: new -> searched -> selected -> reserved -> ordered | expired
- proposals / selection / reservation / order: dicts from/to the MCP server
- attempt: reservation attempt counter (part of the idempotency key)
- purchase_task_id: A2A task interrupted mid-purchase (for the drill)
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB = os.path.join('data', 'sales.db')

STEPS = ('new', 'searched', 'selected', 'reserved', 'ordered', 'expired')


class SalesSessionStore:
    """SQLite-backed session map keyed by A2A ``context_id``."""

    def __init__(self, db_path: str = DEFAULT_DB) -> None:
        directory = os.path.dirname(db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('PRAGMA busy_timeout=10000')
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                context_id   TEXT PRIMARY KEY,
                session_json TEXT NOT NULL,
                step         TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            )
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # -- CRUD ----------------------------------------------------------
    def get(self, context_id: str) -> dict | None:
        row = self._db.execute(
            'SELECT session_json FROM sessions WHERE context_id = ?',
            (context_id,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, session: dict) -> None:
        session['step'] = session.get('step', 'new')
        self._db.execute(
            """
            INSERT INTO sessions (context_id, session_json, step, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(context_id) DO UPDATE SET
                session_json = excluded.session_json,
                step = excluded.step,
                updated_at = excluded.updated_at
            """,
            (
                session['context_id'],
                json.dumps(session, ensure_ascii=False),
                session['step'],
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._db.commit()

    def delete(self, context_id: str) -> None:
        self._db.execute(
            'DELETE FROM sessions WHERE context_id = ?', (context_id,)
        )
        self._db.commit()

    def by_step(self, step: str) -> list[dict]:
        """Sessions sitting at a given step (e.g. ``reserved`` = pending)."""
        rows = self._db.execute(
            'SELECT session_json FROM sessions WHERE step = ?', (step,)
        ).fetchall()
        return [json.loads(row[0]) for row in rows]


def new_session(context_id: str) -> dict:
    return {
        'context_id': context_id,
        'step': 'new',
        'query': None,
        'proposals': [],
        'selection': None,
        'reservation': None,
        'order': None,
        'attempt': 0,
        'purchase_task_id': None,
    }
