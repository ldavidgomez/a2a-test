# A2A first contact — server, client and two agents talking to each other (Python)

A minimal [A2A](https://a2a-protocol.org/) server built on the official **`a2a-sdk` 1.2.2**, with local models through **Ollama** (no API key needed).

| File | What it holds |
| --- | --- |
| [`agent_executor.py`](agent_executor.py) | The executors: `EchoAgentExecutor`, `OllamaAgentExecutor`, `DelegatingAgentExecutor` (an A2A client inside an agent) and `ClaudeAgentExecutor` |
| [`server.py`](server.py) | Agent Card + `DefaultRequestHandler` + JSON-RPC routes on Starlette, served with Uvicorn. One process = one agent |
| [`client_test.py`](client_test.py) | Test client: resolves the Agent Card and sends a message |
| [`shop_mcp.py`](shop_mcp.py) | MCP server for the purchase PoC: inventory, atomic + idempotent reservations, orders (SQLite) |
| [`shop_executors.py`](shop_executors.py) | Purchase PoC executors: `SalesAgentExecutor` (conversation + saga) and `StockAgentExecutor` (A2A→MCP bridge) |
| [`shop_state.py`](shop_state.py) | Durable per-context sales sessions (SQLite) |
| [`shop_client.py`](shop_client.py) | Shared clients: MCP tool calls + A2A send/tasks-get with structured data parts |
| [`client_purchase.py`](client_purchase.py) | Conversational client for the purchase workflow |
| [`shop_stack.py`](shop_stack.py) | Process orchestration (binds MCP + stock + sales) |
| [`demo_purchase.py`](demo_purchase.py) | One-shot demo: happy path, restart drill, atomicity & idempotency tests |
| [`test_concurrency.py`](test_concurrency.py) | PASS/FAIL test: N concurrent buyers for the last unit |
| [`test_idempotency.py`](test_idempotency.py) | PASS/FAIL test: repeated reservation/order requests |

> **About the SDK version:** in `a2a-sdk` 1.x the types (`AgentCard`, `Message`, `Part`, …) are **protobuf messages**, not Pydantic models, and the app is no longer assembled with `A2AStarletteApplication`: you compose routes (`create_agent_card_routes` + `create_jsonrpc_routes`) into a Starlette app. This repo targets the 1.2.2 API.

## Requirements

- Python 3.10+ (tested with 3.11.9 on Windows 11)
- [Ollama](https://ollama.com/download) for the local-model modes

## Install (Windows / PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

To activate the environment instead: `.\.venv\Scripts\Activate.ps1` (if PowerShell blocks it, `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`).

## 1. Quick check: an echo agent

```powershell
.\.venv\Scripts\python.exe server.py
```

In **another** window:

```powershell
.\.venv\Scripts\python.exe client_test.py "Hello, can you hear me?"
```

```text
> Hello, can you hear me?
[task]
[status_update]
[artifact_update] Echo: Hello, can you hear me?
[status_update]
```

## 2. An agent backed by a local model (Ollama)

Ollama exposes its API on `http://localhost:11434` as soon as it is installed and running (on Windows the installer leaves it as a service; otherwise run `ollama serve`).

```powershell
ollama pull llama3.2
ollama list                       # check the model is there
curl.exe http://localhost:11434/api/tags
```

```powershell
.\.venv\Scripts\python.exe server.py --mode ollama --model llama3.2 --system "Answer in one sentence."
```

```powershell
.\.venv\Scripts\python.exe client_test.py "What is the A2A protocol?"
```

The agent talks to Ollama over `POST /api/chat` with `stream: false` and reads the answer from `message.content`. The first call is slower because Ollama loads the model into memory (the HTTP client timeout is 180 s).

## 3. Two Ollama agents talking over A2A

The idea: **each agent is its own A2A process**, and the second one carries an A2A *client* inside its executor. Agent B does not call a function on A — it resolves A's Agent Card and sends it a real `message/send` over JSON-RPC.

```text
client_test.py ──A2A──▶ Agent B (:9998)  ──A2A──▶ Agent A (:9999)
                        delegate + summary           ollama
                              │                         │
                              └───────▶ Ollama ◀────────┘
                                      (:11434)
```

Window 1 — agent A, the specialist:

```powershell
.\.venv\Scripts\python.exe server.py --port 9999 --mode ollama --model llama3.2 `
  --name "Agent A (specialist)" `
  --system "You are an expert on agent protocols. Answer in two sentences at most."
```

Window 2 — agent B, which asks A and summarises the answer with its own model:

```powershell
.\.venv\Scripts\python.exe server.py --port 9998 --mode delegate `
  --peer http://127.0.0.1:9999 --model llama3.2 `
  --name "Agent B (orchestrator)"
```

Window 3 — the client talks **only** to B:

```powershell
.\.venv\Scripts\python.exe client_test.py --url http://127.0.0.1:9998 "What is an Agent Card?"
```

```text
> What is an Agent Card?
[task]
[status_update]
[artifact_update] [answer from http://127.0.0.1:9999] <whatever agent A replied>

[orchestrator summary] <agent B's summary>
[status_update]
```

**How to confirm the A2A conversation is real** (and not an in-process call):

1. Agent A's window shows the traffic B generates, not the client's:
   `GET /.well-known/agent-card.json 200` followed by `POST / 200`.
2. `--model` on B is optional: without it, B forwards A's answer verbatim and you can see the text tagged with the peer's URL.
3. Stop agent A and ask again: B fails with a connection error and the task ends as `failed` with the reason in the message.

Notes when both agents share one Ollama:

- Requests are queued; to have them served in parallel, start Ollama with `OLLAMA_NUM_PARALLEL=2` (environment variable) or give each agent a different model.
- Two different models take twice the memory. On a tight RAM/VRAM budget, use the same model in both agents or leave B without `--model`.

## `server.py` options

| Option | Default | What it does |
| --- | --- | --- |
| `--host` / `--port` | `127.0.0.1` / `9999` | Where it listens |
| `--mode` | `echo` | `echo`, `ollama`, `delegate`, `claude`, `sales`, `stock` |
| `--name` | `A2A <mode> agent` | Name in the Agent Card |
| `--model` | per mode | LLM model (in `delegate`, the one used to summarise) |
| `--system` | — | System prompt (`ollama` mode) |
| `--peer` | — | Base URL of the agent to delegate to (required in `delegate`/`sales`) |
| `--ollama-url` | `http://localhost:11434` | Where Ollama lives |
| `--public-url` | `http://<host>:<port>` | URL published in the Agent Card (useful behind a proxy) |
| `--mcp-url` | `http://127.0.0.1:9996` | Shop MCP server (used by `sales`/`stock`) |
| `--task-db` | `data/tasks_<mode>.db` ¹ | A2A task store: SQLite for `sales`/`stock`, in memory otherwise |
| `--sales-db` | `data/sales.db` | Durable sales sessions (`sales` mode) |
| `--llm` | `AGENT_LLM` > file `default` > `glm` if key set > `none` | `none`, `glm`, `ollama` or **any entry name in `llm_config.json`**: decorates answers via an OpenAI-compatible endpoint |
| `--llm-model` | per provider | Model name (overrides the built-in/entry value) |
| `--llm-base-url` | per provider | OpenAI-compatible base URL (overrides the built-in/entry value) |
| `--llm-config` | `llm_config.json` | JSON with a `"providers"` section naming extra LLM endpoints |
| `--pause-after` | — | Crash drill: `reserve` = sleep between reservation and order |
| `--reset` | — | Delete the task/session DB files before starting |

¹ only `sales`/`stock` persist by default; other modes keep tasks in memory unless `--task-db` is given.

Every option has an equivalent environment variable: `A2A_HOST`, `A2A_PORT`, `AGENT_MODE`, `OLLAMA_URL`, `A2A_PEER_URL`, `A2A_PUBLIC_URL`, `MCP_URL`, `A2A_TASK_DB`, `A2A_SALES_DB`, `AGENT_LLM`, `A2A_PAUSE_AFTER`.

## Endpoints

| Path | Method | What it is for |
| --- | --- | --- |
| `/.well-known/agent-card.json` | `GET` | Agent Card (discovery) |
| `/` | `POST` | A2A JSON-RPC endpoint |

```powershell
curl.exe http://127.0.0.1:9999/.well-known/agent-card.json
```

## Claude mode (optional)

If an API key shows up one day, the same server answers with Claude:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
.\.venv\Scripts\python.exe server.py --mode claude
```

It uses `claude-opus-5-5` with `effort: low` and the server-side fallback enabled. **Not tested on this machine** (no key configured).

## 4. The keyboard purchase workflow (A2A + MCP + saga)

The full PoC from the purchase diagram: three processes and a client.

```text
client_purchase.py ──A2A──▶  Agente de Ventas (:9999)
                             ├─ delega búsqueda y reserva ──A2A──▶  Agente de Stock (:9998)
                             └─ crea pedidos ────────MCP──▶  Tienda MCP (:9996)
                                                             └── data/shop.db (SQLite WAL)
```

What it demonstrates (mapa con los pasos del workflow):

1. The client asks ventas for a keyboard (Spanish mini-grammar: `busco …`,
   `elijo 2`, `comprar`, `estado`…).
2. Ventas delegates the search to stock over **real A2A** (`message/send`,
   its own Agent Card resolution, one task per request).
3. Stock queries the inventory through **MCP** (`search_stock` over
   streamable HTTP).
4. Ventas proposes products and parks the task in **`input-required`**;
   the client answers with `taskId` and **the same task** continues.
5. The purchase is one long task: reserva via A2A (stock → MCP
   `reserve_stock`) **then** pedido via MCP directly (`create_order`).
6. **Atomicity & idempotency live in the SQLite layer**: the stock update
   and the reservation/order inserts run in `BEGIN IMMEDIATE`
   transactions and are deduplicated by `UNIQUE(idempotency_key)` columns
   (keys derive from the A2A context, so retries at any layer collapse:
   client → ventas → stock → MCP).
7. **Durability across crashes**: ventas keeps sessions in `data/sales.db`
   and both agents keep A2A tasks in `DatabaseTaskStore` (SQLite). Kill
   ventas between reservation and order (`--pause-after reserve` is the
   crash point), restart it: `tasks/get` still shows the interrupted task
   as `WORKING`, and replying with its `taskId` (`continuar`)
   **completes that same task** from durable state.

Business rejections are **not** protocol errors: a buyer that loses the
last-unit race gets a `COMPLETED` task with a rejection artifact
(`SIN_STOCK`); only transport/agent errors set the task to `failed`.

### One-shot demo

```powershell
# deterministic (no LLM): ventas uses plain templates
.\.venv\Scripts\python.exe demo_purchase.py

# with GLM-5-3 decorating the sales agent's answers
$env:GLM_API_KEY = "..."
.\.venv\Scripts\python.exe demo_purchase.py --llm glm

# with a local Ollama model instead
.\.venv\Scripts\python.exe demo_purchase.py --llm ollama

# with an endpoint declared in llm_config.json (see below)
.\.venv\Scripts\python.exe demo_purchase.py --llm glm-zhipu --llm-config llm_config.json
```

It runs three acts — happy path, crash/restart drill and the two critical
tests — with `[PASS]`/`[FAIL]` verdicts and a summary. All process logs go
to `data/logs/`. The LLM only rewrites deterministic templates (codes,
prices and IDs may not change); if it is unreachable the templates are used
and the demo still passes, so the tests are reproducible.

### The providers file (`--llm-config`)

Beyond the built-ins (`glm`, `ollama`, `none`), any OpenAI-compatible
endpoint can be declared in a JSON file — default path `llm_config.json`
(overridable with `--llm-config` or env `A2A_LLM_CONFIG`; see
[`llm_config.example.json`](llm_config.example.json),
which is also the shape to copy):

```json
{
  "default": "glm",
  "providers": {
    "glm":       { "base_url": "https://api.z.ai/api/paas/v4",
                    "model": "glm-5-3", "api_key_env": "GLM_API_KEY" },
    "glm-zhipu": { "base_url": "https://open.bigmodel.cn/api/paas/v4",
                    "model": "glm-5-3", "api_key_env": "ZHIPU_API_KEY" },
    "ollama":    { "base_url": "http://localhost:11434/v1",
                    "model": "llama3.2" }
  }
}
```

- Each entry needs `base_url` + `model`; the key comes from inline
  `api_key` (the file is gitignored) or from the env var named in
  `api_key_env`. An entry whose `api_key_env` is not set is disabled and
  ventas falls back to templates.
- Select one with `--llm <name>` (any entry name). Which provider is used
  when nothing is given: `AGENT_LLM` env > the file's `default` > `glm` if
  `GLM_API_KEY` is set > `none`.
- `--llm-model` / `--llm-base-url` still override the entry, and unknown
  names fail with the list of available ones.
- Every entry speaks `POST {base_url}/chat/completions` — that covers
  GLM (z.ai/Zhipu), Ollama's `/v1`, vLLM, LM Studio, etc.

### Manual run (three windows + client)

```powershell
# 1) MCP shop server (:9996)
.\.venv\Scripts\python.exe shop_mcp.py

# 2) stock agent (:9998, tasks in data/tasks_stock.db)
.\.venv\Scripts\python.exe server.py --mode stock --port 9998 --name "Agente de Stock"

# 3) sales agent (:9999, sessions in data/sales.db)
.\.venv\Scripts\python.exe server.py --mode sales --peer http://127.0.0.1:9998 `
  --name "Agente de Ventas" [--llm glm|ollama|none]

# 4) talk to the sales agent
.\.venv\Scripts\python.exe client_purchase.py "busco un teclado"
.\.venv\Scripts\python.exe client_purchase.py --task <taskId> --context <contextId> "elijo 1"
.\.venv\Scripts\python.exe client_purchase.py --context <contextId> "comprar"
.\.venv\Scripts\python.exe client_purchase.py --context <contextId> "estado"
```

The search task stays in `input-required` until the client replies with
that `taskId` (A2A's mid-task input flow). `--get-task <id>` dumps a stored
task; `--interactive` opens a small REPL (also usable without args).

### The two critical tests

```powershell
# each boots its own fresh stack; --no-boot to reuse a running one
.\.venv\Scripts\python.exe test_concurrency.py      # last unit, 8 buyers
.\.venv\Scripts\python.exe test_idempotency.py     # repeated requests
```

- `test_concurrency`: seeds `KD-LAST-03` with **1 unit**, runs 8 buyer
  sessions and fires all purchases at once → exactly one confirmed order,
  seven `SIN_STOCK` rejections whose tasks are `COMPLETED` (never
  `failed`), one new reservation/order row in the DB and stock 0 (never
  negative).
- `test_idempotency`: (A) repeating «comprar» returns the same order with
  no DB change; (B) the same `reserve_stock` call twice returns the same
  reservation and decrements stock once; (C) `create_order` twice with the
  same key returns the same order, while a different key over a consumed
  reservation is rejected (`RESERVA_YA_CONSUMIDA`).

### Restart drill by hand

1. Start the stack, then relaunch ventas with `--pause-after reserve`.
2. `buscar` → `elijo` → `comprar`: after `Reserva … confirmada` the task
   falls asleep between reservation and order — **kill the process** in
   that window.
3. Relaunch ventas normally (leave `data/` intact). The startup recovery
   pass logs the pending purchase it finds.
4. `client_purchase.py --get-task <taskId>` → the interrupted task is
   still `WORKING` (persisted in SQLite).
5. `client_purchase.py --task <taskId> --context <contextId> "continuar"`
   → the SAME task completes with the order: the saga resumes from
   `sales.db` + `shop.db` — no double reservation, because the order
   reuses the persisted idempotency key.

Reservations expire after 90 s (`DEFAULT_RESERVATION_TTL_SECONDS`), so a
recovered-but-expired attempt is rejected with `RESERVA_CADUCADA` and the
next `comprar` starts a new attempt with a fresh idempotency key.

## 5. How to test it (checklist)

Everything below uses only the venv (no sqlite3 CLI needed) and exits with
`0` on success / `1` on failure, so any of it fits a CI job. `data/` is
wiped automatically unless `--keep-data`.

### Quick smoke — one command, whole story

```powershell
.\.venv\Scripts\python.exe demo_purchase.py
```

Runs acts 1–3 and ends with the summary (`ACTO 1: OK`, `ACTO 2: OK`,
`ACTO 3 (pruebas): PASS`). What each act shows, in one table:

| Act | Proves | PASS looks like |
| --- | --- | --- |
| 1 · camino feliz | steps 1–6: A2A delegation ventas→stock, MCP query, `input-required` selection resumed with `taskId`, reserve + order in one task | `[PASS] la búsqueda deja la tarea en input-required`, `pedido creado: PED-…` |
| 2 · drill de reinicio | durability: crash between reservation and order, task stays `WORKING` in SQLite, next reply with the same `taskId` completes **that same task** | `tasks/get tras el reinicio -> TASK_STATE_WORKING` → `-> TASK_STATE_COMPLETED` |
| 3 · prueba de concurrencia | atomicity of the last unit: 1 winner, 7 clean business rejections, stock 0 and never negative | `[PASS] exactamente 1 pedido confirmado`, `[PASS] ninguna tarea termina en estado de error` |
| 3 · prueba de idempotencia | repeats collapse: same order on «comprar» replay, same reservation on repeated `reserve_stock`, same order on repeated `create_order` | `[PASS] repetir «comprar» devuelve el MISMO pedido`, `[PASS] BD sin cambios` |

Run a single act with `--acts 1` / `--acts 2` / `--acts 3`, and add
`--llm glm` o `--llm ollama` to run the same checks with decorated prose
(the LLM is best-effort: unreachable → templates, tests unaffected).

### The critical tests, standalone

```powershell
.\.venv\Scripts\python.exe test_concurrency.py            # +--sessions 16, --no-boot
.\.venv\Scripts\python.exe test_idempotency.py           # --no-boot to reuse a running stack
```

Each prints its `[PASS]`/`[FAIL]` lines and ends with
`RESULTADO: ✔` / `✘`. Each boots a **fresh** stack (reset inventory),
so they can run repeatedly in any order.

### By hand, while the stack runs

Boot it once (three windows from section 4 or the one-shot Python):

```powershell
.\.venv\Scripts\python.exe client_purchase.py "busco un teclado"
.\.venv\Scripts\python.exe client_purchase.py --task <taskId> --context <contextId> "elijo 1"
.\.venv\Scripts\python.exe client_purchase.py --context <contextId> "comprar"
.\.venv\Scripts\python.exe client_purchase.py --context <contextId> "estado"
.\.venv\Scripts\python.exe client_purchase.py --interactive
```

Scenarios worth typing once:

- **Business rejection ≠ failure**: `busco el teclado ergonómico` → `elijo 1` →
  `comprar` — KD-OUT-04 has 0 units, so the task **completes** with a
  `SIN_STOCK` rejection artifact (state shown before the data line).
- **Free text** (`"¿qué sabes hacer?"`) hits the chat handler — GLM-5-3 forms
  the guidance when `--llm` is configured, templates otherwise.
- **Everything is durable**: `--get-task <id>` works after killing and
  relaunching both agents (walk the restart drill from section 4).

### Inspecting what the processes actually did

Every process logs to `data/logs/` (`mcp.log`, `stock.log`, `sales.log`). The
A2A conversations are visible there — the proof that ventas really calls
stock over the protocol (not in-process) is the traffic in `stock.log` when
ventas runs:

```text
[uvicorn] ... "GET /.well-known/agent-card.json HTTP/1.1" 200
[uvicorn] ... "POST / HTTP/1.1" 200 OK                      <- sales -> stock
```

And the durable state is plain SQLite, readable with the venv itself:

```powershell
# inventory, reservations and orders after a run
.\.venv\Scripts\python.exe -c "import sqlite3; \
  c = sqlite3.connect('data/shop.db'); \
  print(c.execute('select code, stock from products').fetchall()); \
  print(c.execute('select id, idem_key, status from reservations').fetchall()); \
  print(c.execute('select id, reservation_id from orders').fetchall())"

# A2A tasks persisted by the agents (minus the stack's stored blobs)
.\.venv\Scripts\python.exe -c "import sqlite3; \
  c = sqlite3.connect('data/tasks_sales.db'); \
  print(c.execute('select id, context_id from tasks').fetchall())"
```

Sanity checks you should see there after the tests: only the winner of the
last-unit race has a reservation row for `KD-LAST-03` (a `SIN_STOCK`
rejection leaves no row), `stock` is 0 for it after the concurrency test,
and each replayed idempotency key maps to exactly one row — never two —
which is the whole point of the idempotency drill.

Total reset for a clean slate: delete `data/` (or rerun `demo_purchase.py`,
which wipes it unless told otherwise).

## How the pieces fit

1. The client fetches `/.well-known/agent-card.json` and picks the transport (JSON-RPC here) from `supportedInterfaces`.
2. It sends a `SendMessageRequest` to `POST /`; `DefaultRequestHandler` builds the `RequestContext` and calls `execute`.
3. The executor **must enqueue the `Task` first** and only then status events: starting with a `TaskStatusUpdateEvent` makes the server answer `InvalidAgentResponseError: Agent should enqueue Task before TaskStatusUpdateEvent event`.
4. The answer is published as an artifact (`TaskUpdater.add_artifact`) and the task is closed with `TaskUpdater.complete()`. If the executor raises, `ReplyExecutor` turns it into `TaskUpdater.failed()` with the reason, so the client can see what went wrong.
5. `InMemoryTaskStore` keeps tasks in memory: restart the process and they are gone. For persistence, use `DatabaseTaskStore` with the `a2a-sdk[sql]` extra.

## Next steps

- ~~Real streaming~~ — the purchase workflow emits intermediate `TaskStatusUpdateEvent` messages (`Consultando el stock…`, `Reserva confirmada…`) via `update_status`, plus artifacts as work progresses.
- ~~An `input-required` flow~~ — used in the purchase workflow for the product selection.
- Authentication: `security_schemes` in the Agent Card + client interceptors (`a2a.client.auth`).
- More than two agents: `DelegatingAgentExecutor` and `SalesAgentExecutor` take a single `--peer`; for a router with several peers, keep one client per URL and choose by the skill each Agent Card advertises.
- Multi-cantidad / multi-item carts: `qty` is fixed at 1 per purchase in this PoC.
- Push notifications (`TaskPushNotificationConfig`) so ventas can finish recovered orders without waiting for the client's `continuar`.
