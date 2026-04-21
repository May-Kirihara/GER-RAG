# GaOTTT

**Gravity as Optimizer Test-Time Training** — A retrieval system whose update rule, read as an optimizer, behaves like Test-Time Training.

> Built as a long-term memory for LLMs. The gravity-based update rule turns out to have a term-for-term correspondence with Heavy ball SGD (Hebbian gradient + L2 regularization, integrated by Verlet) — once you treat retrieval scores as a stochastic gradient signal. Under that reading, the system is a Test-Time Training framework: it keeps learning as you use it, without touching the LLM's weights.
>
> *(formerly GER-RAG — the gravity model was never just decoration; read the right way, it also describes an optimizer.)*

[日本語 README](README_ja.md) · **[📖 Documentation Wiki](docs/wiki/Home.md)**

---

## Overview

GaOTTT is **long-term external memory for AI agents** — and, structurally, an online optimizer that runs at inference time. The more you use it, the more its representations change: knowledge gravitates toward co-used knowledge, producing **serendipitous connections and creative insights**.

It runs as an MCP server (compatible with Claude Code, Claude Desktop, and other agent frameworks) and as a REST API. Documents become nodes with mass, temperature, and gravitational displacement; co-retrieved documents drift closer together; the knowledge space self-organizes with every query. Because the update rule can be read as an optimizer, that drift looks closer to **online learning of the retrieval geometry than to plain caching**.

### Five-layer design

Originally built with a physics→biology two-layer metaphor. Then two things were noticed: (1) when you transcribe the physics formally, the update rule lines up with a TTT-style optimizer; (2) when the memory is shared across agents, the biology becomes a coordination substrate. That gives five layers:

| Layer | Mechanism | Emergent role |
|---|---|---|
| **Physics** | mass, gravity wave, orbital mechanics | (design intent — the equations you would write for a gravity system) |
| **TTT mechanism** | Heavy ball SGD + Hebbian gradient + L2 + Verlet integration | representations change at inference time — **readable as** test-time training |
| **Biology** | dark-matter halo, astrocyte | silently supports the LLM neuron's token reasoning |
| **Relations** | typed directed edges, completed-edge chronology | shared memory between agents |
| **Persona** (Phase D) | declared values/intentions/commitments + `inherit_persona` | session-spanning self-continuity |

The bottom layer is physics (design intent). TTT is the first re-reading (structural correspondence between the physics and a known optimizer family). Biology is the first emergence (observed behavior). Relations and persona are the second and third emergences (observed when deployed among multiple agents and across sessions).

→ Full philosophy: [Five-Layer Philosophy](docs/wiki/Reflections-Five-Layer-Philosophy.md)

### What we've measured vs. what we're claiming

Because the TTT framing is the most load-bearing claim, here is an honest split.

**Measured** (small-scale evaluation, full report in [Phase 2 Evaluation](docs/wiki/Research-Phase-2-Evaluation.md)):
- **nDCG@10**: 0.9457 (static-RAG baseline) → 0.9708 (GaOTTT after 500-query adaptation). +2.7%.
- **MRR**: 0.8833 → 1.0000. +13.2%.
- One mixed-domain scenario (film × food × travel) improved **+15.0% nDCG** after adaptation; cross-scenario average **+3.8%**.
- **Latency**: p50 = 15.1 ms at 200 docs; 50 concurrent queries complete with 0 errors.
- **Drift**: rank-shift rate and serendipity index are qualitatively distinct from a static retriever under repeated queries.

**Claimed** (interpretive, not directly measured):
- The gravity-based update rule has a term-for-term correspondence with Heavy ball SGD + Hebbian gradient + L2, integrated by Verlet — once retrieval scores are read as a stochastic gradient signal. Under that reading, the system behaves as Test-Time Training on the retrieval geometry.
- "Every `recall` plays the role of a gradient step" and similar statements throughout the docs are structural readings of the physics, not measured equivalences with a trained optimizer.

**Open** (honest caveats):
- No formal isomorphism is proved with a fully-specified, estimable loss. The implicit potential energy is named but not fit.
- Benchmarks above are hundreds of documents per scenario. We have not replicated at 100K-doc scale with adversarial queries or against modern re-ranking baselines.
- The "astrocyte" / "persona preservation" layers are observed qualitatively in multi-agent and cross-session use; they are not yet quantified.

The project is best read as **a working implementation whose physics and optimizer forms coincide — and which empirically drifts in useful directions** — rather than as a finished proof that gravity-based retrieval *is* TTT. The Research notes are where the correspondence argument lives; they welcome scrutiny.

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
# Install (the old URL May-Kirihara/GER-RAG.git still redirects to
# May-Kirihara/GaOTTT.git via GitHub's rename facility)
git clone https://github.com/May-Kirihara/GaOTTT.git && cd GaOTTT
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
