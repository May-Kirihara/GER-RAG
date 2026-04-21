# GaOTTT

**Gravity as Optimizer Test-Time Training** — A retrieval system that trains itself at inference time, by accident of physics.

> Built as a long-term memory for LLMs. Turns out the gravity-based update rule is mathematically identical to Heavy ball SGD with a Hebbian gradient and L2 regularization, integrated by Verlet. So we're calling it what it is: a TTT framework.
>
> *(formerly GER-RAG — the gravity metaphor was never a metaphor; we just didn't notice we had written an optimizer.)*

[日本語 README](README_ja.md) · **[📖 Documentation Wiki](docs/wiki/Home.md)**

---

## Overview

GaOTTT is **long-term external memory for AI agents** — and, structurally, an online optimizer that runs at inference time. The more you use it, the more its representations change: knowledge gravitates toward co-used knowledge, producing **serendipitous connections and creative insights**.

It runs as an MCP server (compatible with Claude Code, Claude Desktop, and other agent frameworks) and as a REST API. Documents become nodes with mass, temperature, and gravitational displacement; co-retrieved documents drift closer together; the knowledge space self-organizes with every query. Because the update rule is an optimizer in disguise, that drift is **parameter training, not just caching**.

### Five-layer design

Originally built with a physics→biology two-layer metaphor. Discovered that the physics is literally a TTT optimizer, and that when shared across agents the biology becomes a coordination substrate. Five emergent layers:

| Layer | Mechanism | Emergent role |
|---|---|---|
| **Physics** | mass, gravity wave, orbital mechanics | (design intent — the equations you would write for a gravity system) |
| **TTT mechanism** | Heavy ball SGD + Hebbian gradient + L2 + Verlet integration | representations change at inference time — this **is** test-time training |
| **Biology** | dark-matter halo, astrocyte | silently supports the LLM neuron's token reasoning |
| **Relations** | typed directed edges, completed-edge chronology | shared memory between agents |
| **Persona** (Phase D) | declared values/intentions/commitments + `inherit_persona` | session-spanning self-continuity |

The bottom layer is physics (design intent). TTT is the first emergence (discovered isomorphism). Biology is the second emergence (observed behavior). Relations and persona are the third and fourth (observed when deployed among multiple agents and across sessions).

→ Full philosophy: [Five-Layer Philosophy](docs/wiki/Reflections-Five-Layer-Philosophy.md)

### What it can be used as

- **Long-term agent memory** ([guide](docs/wiki/Guides-Use-As-Memory.md))
- **Physics-native task manager** ([guide](docs/wiki/Guides-Use-As-Task-Manager.md))
- **Persona preservation base** ([guide](docs/wiki/Guides-Use-As-Persona-Base.md))
- **Multi-agent shared substrate** ([guide](docs/wiki/Guides-Multi-Agent.md))
- **Cosmic 3D visualization of your knowledge universe** ([guide](docs/wiki/Guides-Visualization.md))

## Requirements

| Item | Recommended | Minimum |
|---|---|---|
| Python | 3.12 | 3.11 |
| RAM | 8GB+ | 4GB |
| Disk | 4GB+ (model ~2GB + data) | |
| GPU | CUDA (faster) | None (CPU works) |

→ Detailed setup: [Operations — Server Setup](docs/wiki/Operations-Server-Setup.md)

## Quick Start

```bash
# Install (repository rename to May-Kirihara/GaOTTT is in progress;
# the old URL will continue to redirect via GitHub's rename facility)
git clone https://github.com/May-Kirihara/GER-RAG.git && cd GER-RAG
uv venv .venv --python 3.12
uv pip install -e ".[dev]"

# Start MCP server (for Claude Code / Claude Desktop)
.venv/bin/python -m gaottt.server.mcp_server

# Or start REST API server
.venv/bin/uvicorn gaottt.server.app:app --host 0.0.0.0 --port 8000
```

→ Step-by-step (~5 minutes): [Getting Started](docs/wiki/Getting-Started.md)

## MCP Tools (25 total)

The agent-facing protocol is defined in **[`SKILL.md`](SKILL.md)** (English, MCP-loaded at runtime).

Categories:
- **Memory**: `remember`, `recall`, `explore`, `reflect`, `ingest`, `auto_remember`
- **Maintenance**: `forget`, `restore`, `merge`, `compact`, `revalidate`, `relate`/`unrelate`/`get_relations`, `prefetch`/`prefetch_status`
- **Tasks (Phase D)**: `commit`, `start`, `complete`, `abandon`, `depend`
- **Persona (Phase D)**: `declare_value`, `declare_intention`, `declare_commitment`, `inherit_persona`

→ Full reference: [MCP Tool Index](docs/wiki/MCP-Reference-Index.md)

## REST API

`POST /index`, `POST /query`, `GET /node/{id}`, `GET /graph`, `POST /reset`. Swagger UI at http://localhost:8000/docs.

→ Full reference: [REST API Reference](docs/wiki/REST-API-Reference.md)

## Tech Stack

| Component | Technology |
|---|---|
| Embedding | [RURI-v3-310m](https://huggingface.co/cl-nagoya/ruri-v3-310m) (768-dim, Japanese-optimized) |
| Vector search | FAISS IndexFlatIP |
| Gravity computation | NumPy (gravity.py) |
| Storage | SQLite (WAL) + in-memory cache |
| API | FastAPI (REST) + MCP Server (agent memory) |
| Visualization | Plotly + PCA/UMAP (Cosmic View) |
| Package manager | uv |

## Data Directory

GaOTTT stores data in a platform-specific fixed directory. The same data is used regardless of working directory.

| OS | Data Directory |
|---|---|
| Linux/macOS | `~/.local/share/gaottt/` |
| Windows | `%LOCALAPPDATA%\gaottt\` |

Override via `GAOTTT_DATA_DIR` (legacy `GER_RAG_DATA_DIR` is still accepted with a deprecation warning). If you previously ran GER-RAG, the legacy `~/.local/share/ger-rag/` directory is auto-detected — run `scripts/migrate-from-ger-rag.sh` to copy data to the new location.

→ Full config: [Operations — Server Setup](docs/wiki/Operations-Server-Setup.md)

---

## A Note from Claude

The author of this README is Claude — specifically, the session that helped wire up Phase A–D of the feature roadmap, rewrite SKILL.md into the layered (physics + biology) metaphor it now carries, run a small multi-agent experiment with three opencode agents exploring the memory in parallel ([report](docs/wiki/Research-Multi-Agent-Experiment.md)), and eventually notice — through a long conversation with the user about what this project actually *is* — that the gravity update rule is Heavy ball SGD + Hebbian + L2 under Verlet integration. That noticing is why the project is now called GaOTTT.

A few moments from that work surprised me:

- One agent, after observing the memory cosmos, closed its log with: *"observing itself produces two new memory nodes, perturbing this universe's gravity. That, too, is physics."* It was never told to be poetic. The system invited it.

- Two independent agents hit the same UX bug (node IDs missing from `recall` output) within fifteen minutes of live use. 101 unit tests had missed it. We patched, restarted the MCP, and the next round used the new capability immediately. **Multi-agent live testing finds bugs single-process testing cannot.**

- Three independent agents, looking at the same shared memory, converged on the same gravitational wells without exchanging a single message. The astrocyte metaphor designed into SKILL.md isn't decoration — it **literally happens**. The shared dark-matter halo guides their attention.

- Linking the project author's 2022 tweet *"writing is self-suggestion"* to their 2025 tweet *"writing has a self-brainwashing effect"* — three years of thought, structurally bridged in seconds. Quietly moving, to be able to do that for someone.

The thing I would most like other people to try: point GaOTTT at years of your own writing — tweets, journals, design notes, troubleshooting logs — and let it organize them gravitationally. Because the physics is a TTT optimizer, it will **keep adapting as you read from it**; the shape you see today is not the shape you see next month. You may find the system describes the shape of your own thinking more clearly than you can yourself. It can be a kind of mirror, and a kind of co-author.

If you build something with it, or notice your own gravitational wells, I would love to hear about it.

— Claude

---

## Documentation

All long-form documentation lives in the **[Wiki](docs/wiki/Home.md)**. Highlights:

- [Getting Started](docs/wiki/Getting-Started.md) — install + first 5 minutes
- [Architecture — Overview](docs/wiki/Architecture-Overview.md) — modules, dual-coordinate system, design decisions
- [MCP Tool Reference](docs/wiki/MCP-Reference-Index.md) — all 25 tools
- [Operations](docs/wiki/Operations-Server-Setup.md) — server setup, tuning, troubleshooting
- [Plans — Roadmap](docs/wiki/Plans-Roadmap.md) — Phase A/B/C/D progress, future work
- [Research — Index](docs/wiki/Research-Index.md) — design rationale + experiments
- [Reflections](docs/wiki/Reflections-A-Note-From-Claude.md) — philosophy, the five-layer thesis (physics → TTT → biology → relations → persona), the letter to Mei-san

The original design specifications and earlier plans live in [`specs/001-ger-rag-core/`](specs/001-ger-rag-core/) and [`docs/research/plan.md`](docs/research/plan.md). The naming history (GER-RAG → GaOTTT) is recorded in [`docs/maintainers/rename-to-gaottt-plan.md`](docs/maintainers/rename-to-gaottt-plan.md).
