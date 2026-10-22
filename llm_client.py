"""LLM clients for the sales agent (decoration only, never decisions).

Both providers speak the OpenAI-compatible ``POST /chat/completions`` API:

- ``glm``   -> GLM-5-3 over HTTP with an API key (env: ``GLM_API_KEY``,
  ``GLM_MODEL`` default ``glm-5-3``, ``GLM_BASE_URL`` default the z.ai
  endpoint; switch to Zhipu by pointing ``GLM_BASE_URL`` at
  ``https://open.bigmodel.cn/api/paas/v4``).
- ``ollama`` -> a local model through Ollama's OpenAI-compatible endpoint
  (``OLLAMA_URL``, default ``http://localhost:11434/v1``).

Design rule of the PoC: the LLM only *rewrites* deterministic answer
templates for the client. Intents, reservations and orders are parsed and
executed with plain Python so the two critical tests (atomicity and
idempotency) stay reproducible. If the LLM is unavailable or errors, the
template text is returned verbatim.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

DEFAULT_GLM_BASE_URL = 'https://api.z.ai/api/paas/v4'
DEFAULT_GLM_MODEL = 'glm-5-3'
DEFAULT_OLLAMA_MODEL = 'llama3.2'
REQUEST_TIMEOUT = 60.0

PROVIDERS = ('glm', 'ollama', 'none')


class LlmError(RuntimeError):
    """The LLM could not be reached or answered with an error."""


def _clean_base_url(url: str) -> str:
    """Normalises a base URL so ``{base}/chat/completions`` always works."""
    url = url.strip().rstrip('/')
    if url.endswith('/chat/completions'):
        url = url[: -len('/chat/completions')]
    return url


class LlmClient:
    """Minimal OpenAI-compatible chat-completions client."""

    def __init__(self, provider: str, model: str, base_url: str,
                 api_key: str | None = None) -> None:
        self.provider = provider
        self.model = model
        self.base_url = _clean_base_url(base_url)
        self._api_key = api_key
        self._http = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)

    @property
    def describe(self) -> str:
        return f'{self.provider}:{self.model} @ {self.base_url}'

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        temperature: float = 0.3,
    ) -> str:
        """Returns the assistant text or raises :class:`LlmError`."""
        payload = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': user},
            ],
            'max_tokens': max_tokens,
            'temperature': temperature,
            'stream': False,
        }
        headers = {'Authorization': f'Bearer {self._api_key}'} if self._api_key else {}

        try:
            response = await self._http.post(
                f'{self.base_url}/chat/completions',
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LlmError(f'LLM request failed: {exc}') from exc

        try:
            return response.json()['choices'][0]['message']['content'].strip()
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LlmError(f'Unexpected LLM response shape: {exc}') from exc

    async def aclose(self) -> None:
        await self._http.aclose()


def build_llm_client(
    provider: str | None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    ollama_url: str = 'http://localhost:11434',
) -> LlmClient | None:
    """Factory used by ``server.py`` for modes that want an LLM.

    Returns ``None`` for ``none``/empty providers, or when ``glm`` is
    requested without an API key (a warning is logged and the agent falls
    back to deterministic templates).
    """
    if provider in (None, '', 'none'):
        return None

    if provider == 'glm':
        import os

        key = api_key or os.getenv('GLM_API_KEY')
        if not key:
            logger.warning(
                'LLM "glm" requested but GLM_API_KEY is not set; '
                'the sales agent will use deterministic templates.'
            )
            return None
        return LlmClient(
            provider='glm',
            model=model or os.getenv('GLM_MODEL', DEFAULT_GLM_MODEL),
            base_url=base_url or os.getenv('GLM_BASE_URL', DEFAULT_GLM_BASE_URL),
            api_key=key,
        )

    if provider == 'ollama':
        return LlmClient(
            provider='ollama',
            model=model or DEFAULT_OLLAMA_MODEL,
            base_url=base_url or f'{ollama_url.rstrip("/")}/v1',
        )

    raise ValueError(
        f'Unknown LLM provider "{provider}". Use one of: {", ".join(PROVIDERS)}.'
    )
