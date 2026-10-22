"""Process orchestration for the purchase-workflow stack.

Used by ``demo_purchase.py`` and by the tests with ``--boot``. Manages:

- shop MCP server (default :9996)
- stock agent             (default :9998, A2A + DatabaseTaskStore)
- sales agent             (default :9999, A2A + DatabaseTaskStore)

Everything logs under ``data/logs``. ``kill_sales`` + ``restart_sales``
implement the crash drill; the sales process can be relaunched with
extra arguments (e.g. ``--pause-after reserve``).
"""

from __future__ import annotations

import asyncio
import httpx
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, 'data')
LOG_DIR = os.path.join(DATA_DIR, 'logs')


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(('127.0.0.1', port)) != 0


def wait_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _free(port):
            return
        time.sleep(0.2)
    raise TimeoutError(f'port {port} never came up')


def wait_port_free(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _free(port):
            return
        time.sleep(0.2)
    raise TimeoutError(f'port {port} never went free')


class Stack:
    """Owns the three processes. Use ``start()`` and ``stop()``."""

    def __init__(
        self,
        mcp_port: int = 9996,
        stock_port: int = 9998,
        sales_port: int = 9999,
        llm: str = 'none',
        llm_config: str | None = None,
        log_prefix: str = '',
    ) -> None:
        self.mcp_port = mcp_port
        self.stock_port = stock_port
        self.sales_port = sales_port
        self.llm = llm
        self.llm_config = llm_config
        self.log_prefix = log_prefix
        self._processes: dict[str, subprocess.Popen] = {}
        self._log_files: list = []
        self._log_paths: dict[str, str] = {}
        os.makedirs(LOG_DIR, exist_ok=True)

    # -- paths ----------------------------------------------------------
    @property
    def mcp_url(self) -> str:
        return f'http://127.0.0.1:{self.mcp_port}'

    @property
    def stock_url(self) -> str:
        return f'http://127.0.0.1:{self.stock_port}'

    @property
    def sales_url(self) -> str:
        return f'http://127.0.0.1:{self.sales_port}'

    # -- lifecycle ------------------------------------------------------
    def _spawn(self, name: str, args: list[str], log_name: str) -> subprocess.Popen:
        log_path = os.path.join(LOG_DIR, log_name)
        log_file = open(log_path, 'a', encoding='utf-8')
        self._log_files.append(log_file)
        self._log_paths[name] = log_path
        env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}

        proc = subprocess.Popen(
            [sys.executable, '-u', *args],
            cwd=ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0),
        )
        self._processes[name] = proc
        print(f'{self.log_prefix}[stack] {name} pid={proc.pid} '
              f'-> {log_path}')
        return proc

    # -- startup diagnostics --------------------------------------------
    def _tail(self, name: str, lines: int = 12) -> str:
        """Last lines of a process log (best effort, never raises)."""
        path = self._log_paths.get(name)
        try:
            with open(path, encoding='utf-8', errors='replace') as handle:
                return '\n'.join(handle.read().splitlines()[-lines:])
        except OSError:
            return ''

    def _raise_startup(self, name: str, reason: str) -> None:
        """Distinguishes 'it died' from 'it hangs', with the log tail."""
        proc = self._processes.get(name)
        tail = self._tail(name)
        suffix = (
            f'\n--- últimas líneas de {self._log_paths.get(name)} ---\n{tail}'
            if tail else ''
        )
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(
                f'[{name}] el proceso murió durante el arranque '
                f'(código de salida {proc.returncode}){suffix}'
            )
        raise TimeoutError(f'[{name}] {reason}{suffix}')

    def _check_alive(self, name: str) -> None:
        proc = self._processes.get(name)
        if proc is not None and proc.poll() is not None:
            self._raise_startup(name, 'no llegó a arrancar')

    def _wait_port(self, name: str, port: int, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_alive(name)
            if not _free(port):
                return
            time.sleep(0.2)
        self._raise_startup(name, f'puerto {port} nunca se abrió')

    def _sales_args(self, extra: list[str] | None = None) -> list[str]:
        args = [
            'server.py', '--mode', 'sales', '--port', str(self.sales_port),
            '--name', 'Agente de Ventas', '--peer', self.stock_url,
            '--llm', str(self.llm),
        ]
        if self.llm_config:
            args.extend(['--llm-config', self.llm_config])
        if extra:
            args.extend(extra)
        return args

    def start(self, reset: bool = False) -> None:
        """Boots MCP + stock + sales. ``reset`` wipes ``data/`` first."""
        if reset:
            self.reset_data()

        self._spawn(
            'mcp', ['shop_mcp.py', '--port', str(self.mcp_port)],
            'mcp.log',
        )
        self._wait_port('mcp', self.mcp_port)

        self._spawn(
            'stock',
            ['server.py', '--mode', 'stock', '--port', str(self.stock_port),
             '--name', 'Agente de Stock'],
            'stock.log',
        )
        self._wait_agent('stock', self.stock_url)

        self._spawn('sales', self._sales_args(), 'sales.log')
        self._wait_agent('sales', self.sales_url)

    def _wait_agent(self, name: str, url: str, timeout: float = 30.0) -> None:
        """Polls the agent card endpoint; sync so async callers can use it."""
        with httpx.Client(timeout=3) as http:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                self._check_alive(name)
                try:
                    response = http.get(
                        f'{url}/.well-known/agent-card.json'
                    )
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.3)
        self._raise_startup(name, f'agent card at {url} never came up')

    def restart_sales(self, extra_args: list[str] | None = None) -> None:
        self.start_sales(self._sales_args(extra_args))

    def start_sales(self, args: list[str]) -> None:
        self._spawn('sales', args, 'sales.log')
        self._wait_agent('sales', self.sales_url)

    def kill_agent(self, name: str) -> None:
        proc = self._processes.get(name)
        if proc is None:
            return
        print(f'{self.log_prefix}[stack] killing {name} (pid={proc.pid})')
        proc.terminate()  # SIGTERM / TerminateProcess
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        del self._processes[name]

    def stop(self) -> None:
        for name in ('sales', 'stock', 'mcp'):
            proc = self._processes.get(name)
            if proc and proc.poll() is None:
                print(f'{self.log_prefix}[stack] stopping {name}...')
                try:
                    proc.terminate()
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        self._processes.clear()
        for log_file in self._log_files:
            log_file.close()
        self._log_files.clear()

    @staticmethod
    def reset_data() -> None:
        import shutil

        if os.path.isdir(DATA_DIR):
            shutil.rmtree(DATA_DIR)
        os.makedirs(LOG_DIR, exist_ok=True)
        print('[stack] data/ wiped (fresh inventory, no tasks/sessions)')

    # -- context manager -------------------------------------------------
    def __enter__(self) -> 'Stack':
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()
