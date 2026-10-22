"""LLM configuration for the sales agent (decoration only, never decisions).

Three interchangeable sources, all speaking the same OpenAI-compatible
``POST /chat/completions`` API:

1. **Built-ins**: ``glm`` (env ``GLM_API_KEY``/``GLM_MODEL``/``GLM_BASE_URL``,
   default endpoint api.z.ai) and ``ollama`` (local OpenAI-compatible
   endpoint, model ``llama3.2``). No extra setup needed.
2. **A providers file** (default ``llm_config.json``, overridable with
   ``--llm-config`` / env ``A2A_LLM_CONFIG``)::

       {
         "default": "glm",
         "providers": {
           "glm":       {"base_url": "https://api.z.ai/api/paas/v4",
                          "model": "glm-5-3", "api_key_env": "GLM_API_KEY"},
           "glm-zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4",
                          "model": "glm-5-3", "api_key_env": "ZHIPU_API_KEY"},
           "ollama":    {"base_url": "http://localhost:11434/v1",
                          "model": "llama3.2"}
         }
       }

   Every entry needs ``base_url`` and ``model``; the API key comes either
   from an inline ``api_key`` (avoid committing it) or from the env var
   named in ``api_key_env``. Entries are selected by name with
   ``--llm <name>``; the file's ``default`` is used when nothing else
   says otherwise. Any OpenAI-compatible endpoint works (the file is the
   place to add custom/local ones). See ``llm_config.example.json``.
3. **CLI overrides**: ``--llm-model`` / ``--llm-base-url`` win over the
   entry; env ``AGENT_LLM`` wins over the file's ``default``; an explicit
   ``--llm`` wins over everything.

Design rule of the PoC: the LLM only *rewrites* deterministic answer
templates for the client. Intents, reservations and orders are parsed and
executed with plain Python so the two critical tests (atomicity and
idempotency) stay reproducible. If the LLM is unavailable or errors, the
template text is returned verbatim.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_FILE = 'llm_config.json'
DEFAULT_GLM_BASE_URL = 'https://api.z.ai/api/paas/v4'
DEFAULT_GLM_MODEL = 'glm-5-3'
DEFAULT_OLLAMA_MODEL = 'llama3.2'
REQUEST_TIMEOUT = 60.0


class LlmError(RuntimeError):
    """The LLM could not be reached or answered with an error."""


def _clean_base_url(url: str) -> str:
    """Normalises a base URL so ``{base}/chat/completions`` always works."""
    url = url.strip().rstrip('/')
    if url.endswith('/chat/completions'):
        url = url[: -len('/chat/completions')]
    return url


# ----------------------------------------------------------------------
# providers file
# ----------------------------------------------------------------------
def _strip_jsonc_comments(text: str) -> str:
    """Removes // and /* */ comments; string-aware (URLs keep their //)."""
    out: list[str] = []
    index = 0
    length = len(text)
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == '\\' and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == '/' and index + 1 < length:
            nxt = text[index + 1]
            if nxt == '/':  # line comment
                index += 2
                while index < length and text[index] != '\n':
                    index += 1
                continue
            if nxt == '*':  # block comment
                index += 2
                while (index + 1 < length and not (
                    text[index] == '*' and text[index + 1] == '/'
                )):
                    index += 1
                index += 2
                continue
        out.append(char)
        index += 1
    return ''.join(out)


def load_config(path: str | None) -> dict:
    """Reads the providers JSON(C). Missing file -> {}; malformed -> ValueError.

    JSONC tolerance: ``//`` line comments and ``/* */`` blocks are ignored
    (handy to disable entries while experimenting), but they never swallow
    ``//`` inside strings such as ``https://...``.
    Accepted shape: ``{"default": "<name>", "providers": {name: entry}}``
    plus optional extra keys (kept for forward compatibility).
    """
    if not path:
        return {}
    try:
        with open(path, encoding='utf-8') as handle:
            config = json.loads(_strip_jsonc_comments(handle.read()))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(
            f'fichero de proveedores inválido ({path}): {exc}'
        ) from exc

    if not isinstance(config, dict):
        raise ValueError(f'fichero de proveedores inválido ({path}): se esperaba un objeto JSON')

    providers = config.get('providers', {})
    if not isinstance(providers, dict) or not all(
        isinstance(entry, dict) for entry in providers.values()
    ):
        raise ValueError(
            f'"providers" de {path} debe ser un objeto cuyos valores sean objetos'
        )
    return config


def resolve_provider_name(explicit: str | None, config: dict) -> str:
    """Precedence: explicit --llm > env AGENT_LLM > file default > heuristic."""
    if explicit:
        return explicit.strip()
    env_choice = os.getenv('AGENT_LLM')
    if env_choice:
        return env_choice.strip()
    if config.get('default'):
        return str(config['default'])
    return 'glm' if os.getenv('GLM_API_KEY') else 'none'


def available_providers(config: dict) -> list[str]:
    """Every selectable name: built-ins + file entries (deduplicated)."""
    return list(dict.fromkeys(
        ['none', 'glm', 'ollama'] + sorted((config.get('providers') or {}).keys())
    ))


def validate_provider(name: str, config_path: str | None = None) -> None:
    """Checks the name resolves to a real provider; no client is built.

    Raises ValueError (listing what exists) so the demo can fail fast
    before spawning any process.
    """
    config = load_config(config_path)
    resolved = resolve_provider_name(name, config)
    if (
        resolved == 'none'
        or resolved in ('glm', 'ollama')
        or resolved in (config.get('providers') or {})
    ):
        return
    raise ValueError(
        f'proveedor LLM desconocido "{resolved}". Disponibles: '
        f'{", ".join(available_providers(config))}'
    )


def _from_entry(
    name: str, entry: dict, config_path: str | None,
    model: str | None, base_url: str | None, api_key: str | None,
) -> LlmClient | None:
    """Builds a client from one ``providers`` entry, applying CLI overrides.

    If the entry declares ``api_key_env`` and that variable is not set
    (and no inline key/override exists), the provider is treated as
    unavailable: ``None`` is returned and the deterministic templates
    take over — same rule as the built-in ``glm``.
    """
    resolved_model = model or entry.get('model')
    resolved_base = base_url or entry.get('base_url')
    if not resolved_model or not resolved_base:
        raise ValueError(
            f'el proveedor "{name}" del fichero de proveedores necesita '
            f'"model" y "base_url" (o pásalos con --llm-model/--llm-base-url)'
        )
    resolved_key = api_key or entry.get('api_key')
    key_env = entry.get('api_key_env')
    if key_env:
        resolved_key = resolved_key or os.getenv(key_env)
        if not resolved_key:
            logger.warning(
                'proveedor "%s": la variable %s no está definida; el '
                'proveedor queda deshabilitado (plantillas deterministas)',
                name, key_env,
            )
            return None
    return LlmClient(
        provider=name,
        model=str(resolved_model),
        base_url=str(resolved_base),
        api_key=resolved_key,
    )


def build_llm_client(
    provider: str | None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    config_path: str | None = None,
    ollama_url: str = 'http://localhost:11434',
) -> LlmClient | None:
    """Factory used by ``server.py`` for modes that want an LLM.

    Returns ``None`` for ``none``, and raises ``ValueError`` for unknown
    names (listing what is available). An empty ``provider`` resolves
    through :func:`resolve_provider_name`.
    """
    config = load_config(config_path)
    name = (provider or '').strip()
    if not name:
        name = resolve_provider_name(None, config)
    if name == 'none':
        return None

    entry = (config.get('providers') or {}).get(name)
    if entry is not None:
        return _from_entry(name, entry, config_path, model, base_url, api_key)

    if name == 'glm':
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

    if name == 'ollama':
        return LlmClient(
            provider='ollama',
            model=model or DEFAULT_OLLAMA_MODEL,
            base_url=base_url or f'{ollama_url.rstrip("/")}/v1',
        )

    available = available_providers(config)
    raise ValueError(
        f'proveedor LLM desconocido "{name}". Disponibles: '
        f'{", ".join(available)}'
    )


# ----------------------------------------------------------------------
# OpenAI-compatible chat completions
# ----------------------------------------------------------------------
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
