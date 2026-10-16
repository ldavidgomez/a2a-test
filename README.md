# A2A first contact — server, client and two agents talking to each other (Python)

A minimal [A2A](https://a2a-protocol.org/) server built on the official **`a2a-sdk` 1.2.2**, with local models through **Ollama** (no API key needed).

| File | What it holds |
| --- | --- |
| [`agent_executor.py`](agent_executor.py) | The executors: `EchoAgentExecutor`, `OllamaAgentExecutor`, `DelegatingAgentExecutor` (an A2A client inside an agent) and `ClaudeAgentExecutor` |
| [`server.py`](server.py) | Agent Card + `DefaultRequestHandler` + `InMemoryTaskStore` + JSON-RPC routes on Starlette, served with Uvicorn. One process = one agent |
| [`client_test.py`](client_test.py) | Test client: resolves the Agent Card and sends a message |

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
| `--mode` | `echo` | `echo`, `ollama`, `delegate` or `claude` |
| `--name` | `A2A <mode> agent` | Name in the Agent Card |
| `--model` | per mode | LLM model (in `delegate`, the one used to summarise) |
| `--system` | — | System prompt (`ollama` mode) |
| `--peer` | — | Base URL of the agent to delegate to (required in `delegate`) |
| `--ollama-url` | `http://localhost:11434` | Where Ollama lives |
| `--public-url` | `http://<host>:<port>` | URL published in the Agent Card (useful behind a proxy) |

Every option has an equivalent environment variable: `A2A_HOST`, `A2A_PORT`, `AGENT_MODE`, `OLLAMA_URL`, `A2A_PEER_URL`, `A2A_PUBLIC_URL`.

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

## How the pieces fit

1. The client fetches `/.well-known/agent-card.json` and picks the transport (JSON-RPC here) from `supportedInterfaces`.
2. It sends a `SendMessageRequest` to `POST /`; `DefaultRequestHandler` builds the `RequestContext` and calls `execute`.
3. The executor **must enqueue the `Task` first** and only then status events: starting with a `TaskStatusUpdateEvent` makes the server answer `InvalidAgentResponseError: Agent should enqueue Task before TaskStatusUpdateEvent event`.
4. The answer is published as an artifact (`TaskUpdater.add_artifact`) and the task is closed with `TaskUpdater.complete()`. If the executor raises, `ReplyExecutor` turns it into `TaskUpdater.failed()` with the reason, so the client can see what went wrong.
5. `InMemoryTaskStore` keeps tasks in memory: restart the process and they are gone. For persistence, use `DatabaseTaskStore` with the `a2a-sdk[sql]` extra.

## Next steps

- Real streaming: emit several `TaskStatusUpdateEvent` / artifacts as work progresses (today the answer arrives in one piece at the end).
- An `input-required` flow to ask the user for data mid-task.
- Authentication: `security_schemes` in the Agent Card + client interceptors (`a2a.client.auth`).
- More than two agents: `DelegatingAgentExecutor` takes a single `--peer`; for a router with several peers, keep one client per URL and choose by the skill each Agent Card advertises.
