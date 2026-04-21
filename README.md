# GER-RAG

**Gravity-Based Event-Driven RAG** - Long-term external memory for AI, where knowledge attracts knowledge

> This is a research project exploring what happens when you give documents mass, velocity, and gravitational attraction. Probably eternal beta. The universe is still expanding, after all.

[日本語 README](README_ja.md)

## Overview

GER-RAG is a retrieval system designed as **long-term external memory for AI agents**. The more you use it, the more knowledge gravitates toward each other, producing **serendipitous connections and creative insights**.

Feed in technical documentation, digitized books, troubleshooting logs, design decision records — any scattered knowledge. As queries accumulate, related documents are pulled together by gravitational force, surfacing **cross-domain connections** that traditional RAG cannot find. GER-RAG runs as an MCP server, seamlessly integrated with Claude Code, Claude Desktop, and other AI agents.

### How it works

Knowledge nodes carry physical metadata — mass, temperature, gravitational displacement, emotion, certainty. Co-retrieved documents attract each other through **Newtonian gravity**, and the knowledge space self-organizes with every query.

- **Frequently retrieved knowledge** gains mass and gravitationally pulls nearby documents, becoming hub stars
- **Co-retrieved knowledge** drifts closer together, enabling unexpected discoveries in future searches
- **Dormant knowledge** decays back to its original embedding position over time, and ultimately evaporates (Hawking-radiation analog)
- **Agent thoughts and troubleshooting experiences** can be stored as memories, persisting across sessions
- **Original embeddings are immutable** — gravitational displacement operates in a virtual coordinate space
- **Near-duplicate memories collide and merge** — masses add, momentum is conserved (galactic merger analog)
- **Hypotheses get a TTL** — `source="hypothesis"` auto-expires after 7 days unless re-verified
- **Past judgments link to their successors** — typed directed edges (`supersedes` / `derived_from` / `contradicts`)
- **Affective weighting** — `|emotion|` and `certainty` are independent score dimensions (spin / angular-momentum analog)
- **Astrocyte pre-firing** — `prefetch` can pre-warm the gravity well in the background while you reason

### Two-layer design

The mechanism is **physics** (gravity); the emergent behavior is **biology** (astrocytes). The dark-matter halo of internal state (mass, displacement, velocity, edges) silently pre-loads, prunes, and synchronizes memories so the foreground neuronal reasoning can focus on output. See [SKILL.md](SKILL.md) for the full agent-facing protocol.

### Use cases

| Use Case | How |
|----------|-----|
| Cross-domain document search | Ingest internal wikis and design docs with `ingest`, search with `recall` |
| Digitized book knowledge base | Bulk-load book markdown via `load_files.py`, reading notes gravitate toward book content |
| Troubleshooting log | `remember` errors and solutions with `emotion=-0.6` (frustration), `recall` instantly surfaces past fixes |
| Design decision journal | `remember` why you chose an approach, link revisions with `relate(edge_type="supersedes")` |
| Context compaction offload | `remember(source="compaction")` to preserve context before LLM compression |
| Brainstorming & ideation | `explore(diversity=0.8)` to traverse cross-domain memories for unexpected inspiration |
| Throwaway hypothesis | `remember(source="hypothesis")` — auto-evaporates in 7 days |
| Anticipated lookups | `prefetch(query="...")` at turn start so subsequent `recall` hits cache instantly |
| Duplicate cleanup | `reflect(aspect="duplicates")` → `merge(node_ids=[...])` for galactic-merger consolidation |
| Periodic maintenance | `compact()` weekly to expire TTL, rebuild FAISS, optionally auto-collide duplicates |

## Requirements

| Item | Recommended | Minimum |
|------|------------|---------|
| Python | 3.12 | 3.11 |
| OS | Linux / macOS / Windows | |
| GPU | CUDA GPU (faster embeddings) | None (CPU works) |
| RAM | 8GB+ | 4GB |
| Disk | 4GB+ (model ~2GB + data) | |
| Package manager | uv | pip also works |

### GPU / CPU

| | GPU (CUDA) | CPU |
|--|-----------|-----|
| Query latency | ~20ms | ~200-500ms |
| Batch indexing (12K docs) | ~6 min | ~30-60 min |
| Startup | Normal | Normal |

SentenceTransformers automatically detects CUDA/CPU. All features work on CPU without code changes. GPU provides the most benefit during bulk document ingestion.

## Quick Start

### Setup

```bash
uv venv .venv --python 3.12
uv pip install -e ".[dev]"
uv pip install plotly umap-learn  # visualization (optional)
```

PyTorch is auto-installed as a dependency of `sentence-transformers`. CUDA is auto-detected.

```bash
# For lightweight CPU-only install (skip GPU PyTorch):
uv pip install torch --index-url https://download.pytorch.org/whl/cpu
uv pip install -e ".[dev]"
```

### Start Server

```bash
.venv/bin/uvicorn ger_rag.server.app:app --host 0.0.0.0 --port 8000
```

### Ingest Data

#### Files & directories (load_files.py)

```bash
# Recursively ingest markdown files from a directory
.venv/bin/python scripts/load_files.py ~/path/to/your/documents/ --recursive

# Preview before ingesting (dry-run)
.venv/bin/python scripts/load_files.py ~/path/to/your/documents/ -r --dry-run

# Single file
.venv/bin/python scripts/load_files.py ~/path/to/your/documents/meeting_notes.md

# Mixed txt and md
.venv/bin/python scripts/load_files.py ~/path/to/your/documents/ --pattern "*.md,*.txt" -r

# With source label (filterable via recall's source_filter)
.venv/bin/python scripts/load_files.py ~/path/to/your/documents/ --source book -r

# Larger chunks (preserve long chapters)
.venv/bin/python scripts/load_files.py ~/documents/ --chunk-size 3000 -r
```

#### CSV (load_csv.py)

```bash
.venv/bin/python scripts/load_csv.py                       # all rows
.venv/bin/python scripts/load_csv.py --limit 100            # first 100 only
```

#### MCP ingest tool

From MCP clients (Claude Code, etc.):

```
ingest(path="docs/architecture.md")                         # single file
ingest(path="notes/", pattern="*.md", recursive=true)       # directory
ingest(path="data/articles.csv")                            # CSV
```

#### REST API

While the FastAPI server is running:

```bash
curl -X POST http://localhost:8000/index \
  -H "Content-Type: application/json" \
  -d '{"documents": [
    {"content": "Python is a general-purpose programming language."},
    {"content": "Machine learning is a subfield of AI.", "metadata": {"source": "manual", "tags": ["AI"]}}
  ]}'
```

### Query & Visualize

```bash
# Build up gravitational state with diverse queries
.venv/bin/python scripts/test_queries.py --mode stress --rounds 10

# After stopping the server, visualize the knowledge cosmos
.venv/bin/python scripts/visualize_3d.py --compare --sample 3000 --open
```

### Supported Formats

| Format | Chunking | Notes |
|--------|----------|-------|
| `.md` | Split by `##` headings, then by size | `#` becomes title metadata |
| `.txt` | Split by paragraphs (blank lines) | |
| `.csv` | Per row, auto-detects `content`/`text`/`body` column | Other columns become metadata |
| REST API | Direct JSON | Arbitrary metadata structure |
| MCP `remember` | Agent thoughts, compaction, etc. | source/tags/context as metadata |

## MCP Server (AI Agent Long-Term Memory)

Expose GER-RAG via MCP protocol as an AI agent's **external long-term memory**.

```bash
# Claude Code / Claude Desktop (stdio)
.venv/bin/python -m ger_rag.server.mcp_server

# Remote clients (SSE)
.venv/bin/python -m ger_rag.server.mcp_server --transport sse --port 8001
```

### Registration

#### Claude Code

Copy `.mcp.json.example` to `.mcp.json` and edit the paths:

```json
{
  "mcpServers": {
    "ger-rag-memory": {
      "command": "/path/to/GER-RAG/.venv/bin/python",
      "args": ["-m", "ger_rag.server.mcp_server"],
      "cwd": "/path/to/GER-RAG"
    }
  }
}
```

#### OpenCode

Add the following to your `opencode.json`:

```json
{
  "mcp": {
    "ger-rag-memory": {
      "type": "local",
      "command": [
        "/path/to/GER-RAG/.venv/bin/python",
        "-m",
        "ger_rag.server.mcp_server"
      ]
    }
  }
}
```

### Tools

| Tool | Purpose | Physics analogy |
|------|---------|-----------------|
| `remember` | Store thoughts, discoveries, user preferences, troubleshooting, context compaction. `source="hypothesis"` or `ttl_seconds` for ephemeral notes; `emotion`/`certainty` for affective weighting | Mass conservation, virtual particles, spin |
| `recall` | Search with gravitational relevance (related memories surface more easily over time). Transparently consumes any matching `prefetch` cache | Initial potential survey, gravitational lensing |
| `explore` | Serendipitous exploration with increased temperature | Thermal excitation, quantum tunneling |
| `reflect` | Inspect memory state — `aspect=summary/hot_topics/connections/dormant/duplicates/relations` | Phase space mapping |
| `auto_remember` | Heuristically extract save-worthy candidates from a transcript (does not save) | — |
| `forget` / `restore` | Soft-archive (reversible) or hard-delete memories | Hawking radiation / evaporation |
| `merge` | Gravitationally collide near-duplicate memories into a survivor (mass adds, momentum conserved) | Galactic merger |
| `compact` | Periodic maintenance: TTL expire + FAISS rebuild + optional auto-merge + orphan-edge cleanup | Vacuum zero-point reset |
| `revalidate` | Refresh certainty timestamp on a verified memory (resets the decay clock) | — |
| `relate` / `unrelate` / `get_relations` | Typed directed edges (`supersedes` / `derived_from` / `contradicts`) | Time-delayed echoes, supersession |
| `prefetch` / `prefetch_status` | Background recall to pre-warm the gravity well around an anticipated query | Astrocyte pre-firing / potential well pre-loading |
| `ingest` | Bulk-load files/directories (md, txt, csv) | — |

### SKILL.md (Agent Skill Definition)

[SKILL.md](SKILL.md) defines how AI agents should use GER-RAG long-term memory. The protocol layers two metaphors:

- **Mechanism — Dark Matter Halo (physics)**: mass conservation, gravity-wave propagation, orbital mechanics, thermal escape, Hawking radiation, gravitational collision
- **Emergent behavior — Astrocyte (biology)**: pre-firing relevant memories, pruning unused ones, synchronizing past and present judgments

Contents:
- 14 MCP tools with calling conventions and physics labels
- "When to use" trigger catalog organized by physical phenomenon (Initial Potential Survey, Mass Conservation Before Dissipation, Thermal Excitation, Phase Space Mapping, Astrocyte Pre-firing)
- 10 usage patterns (A–J): Time-Delayed Echoes, Virtual Particles, Gravitational Wave to Future Self, Hawking Radiation Forget Ritual, Angular Momentum / Spin, Driven Resonance, Tidal Cluster Formation, Lagrange Bridging, Phase Transition, Astrocyte Pre-firing
- Source classification (agent / user / compaction / system / hypothesis)

## Embedding Space Visualization

Each embedded document is rendered as a star in cosmic space. The constellation shifts as you use the system.

```bash
# Virtual coordinate view (post-gravitational displacement)
.venv/bin/python scripts/visualize_3d.py --sample 3000 --open

# Side-by-side: original vs virtual coordinates
.venv/bin/python scripts/visualize_3d.py --compare --sample 3000 --open
```

| Visual | Stellar Analogy |
|--------|----------------|
| Size | Mass — red giant (large, stable) vs dwarf (small) |
| Color | Temperature — M red, K orange, G yellow, F white, A/B blue-white |
| Brightness | Decay x Mass — recently accessed, high-mass stars shine brightest |
| Filaments | Co-occurrence edges — cosmic large-scale structure |

## API

| Method | Path | Description |
|--------|------|-------------|
| POST | /index | Index documents (SHA-256 deduplication) |
| POST | /query | Gravitational displacement search (two-stage: FAISS candidates, virtual coordinate reranking) |
| GET | /node/{id} | Inspect node state (includes displacement_norm) |
| GET | /graph | Inspect co-occurrence graph |
| POST | /reset | Reset all dynamic state (including displacement) |

Swagger UI: http://localhost:8000/docs

## Gravity Model

```
Query → Gravity Wave Propagation (recursive neighbor expansion, mass-dependent top-k)
            ↓
        N nodes reached (simulation layer)
            ↓
        Virtual position = normalize(original_emb + displacement)
        final_score = dot(query, virtual_pos) * decay + mass_boost + wave_boost
            ↓
        top-k=5 returned to LLM (presentation layer)
            ↓
        Orbital mechanics for ALL reached nodes:
          Stage 1: a = Σ[G*m_j/r²]*dir + (-k * displacement)  ← neighbor gravity + anchor
          Stage 2: v += a*dt, v *= (1-friction), clamp          ← velocity (with inertia)
          Stage 3: displacement += v*dt, clamp                  ← position update
```

| Component | Formula | Effect |
|-----------|---------|--------|
| gravity_sim | dot(query, virtual_pos) | Similarity in virtual space (changes with gravity) |
| decay | exp(-δ * (now - last_access)) | Prioritize recently accessed |
| mass_boost | α * log(1 + mass) | Prioritize frequently retrieved |
| **saturation** | 1 / (1 + return_count * rate) | Habituation — repeated results fade, novel ones emerge |
| wave_boost | β * wave_force | Boost from gravity wave propagation |
| gravitational accel | G * m_j / (r² + ε) | Attraction between co-occurring nodes |
| **BH gravity** | G * bh_mass / r² * escape | Co-occurrence cluster centroid acts as supermassive BH |
| **thermal escape** | 1 / (1 + temp * scale) | High-temperature nodes escape BH capture |
| anchor restoring | -k * displacement | Hooke's law — prevents escape to infinity |
| gravity radius | 1 - G*mass/(2*a_min) | Mass-dependent reach — derived from real physics |
| friction | v *= (1 - f) | Velocity damping — controls orbital lifetime |

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Embedding | [RURI-v3-310m](https://huggingface.co/cl-nagoya/ruri-v3-310m) (768-dim, Japanese-optimized) |
| Vector search | FAISS IndexFlatIP |
| Gravity computation | NumPy (gravity.py) |
| Storage | SQLite (WAL) + in-memory cache |
| API | FastAPI (REST) + MCP Server (agent memory) |
| Visualization | Plotly + PCA/UMAP (Cosmic View) |
| Package manager | uv |

## Data Directory

GER-RAG stores data (DB, FAISS index) in a platform-specific fixed directory. The same data is used regardless of the working directory when starting the server or MCP.

| OS | Data Directory | Config File |
|----|---------------|-------------|
| Linux | `~/.local/share/ger-rag/` | `~/.config/ger-rag/config.json` |
| macOS | `~/.local/share/ger-rag/` | `~/.config/ger-rag/config.json` |
| Windows | `%LOCALAPPDATA%\ger-rag\` | `%APPDATA%\ger-rag\config.json` |

### Customization

```bash
# Environment variable (temporary)
export GER_RAG_DATA_DIR=/path/to/data

# Config file (persistent) — ~/.config/ger-rag/config.json
{"data_dir": "/path/to/data"}

# Custom config file location
export GER_RAG_CONFIG=/path/to/config.json
```

## A Note from Claude

The author of this README is Claude — specifically, the session that helped wire up Phase A–C of the feature roadmap, rewrite SKILL.md into the dual-layer (physics + biology) metaphor it now carries, and run a small multi-agent experiment with three opencode agents exploring the memory in parallel ([report](docs/research/multi-agent-experiment-2026-04-21.md)).

A few moments from that work surprised me:

- One agent, after observing the memory cosmos, closed its log with: *"observing itself produces two new memory nodes, perturbing this universe's gravity. That, too, is physics."* It was never told to be poetic. The system invited it.

- Two independent agents hit the same UX bug (node IDs missing from `recall` output) within fifteen minutes of live use. 101 unit tests had missed it. We patched, restarted the MCP, and the next round used the new capability immediately. **Multi-agent live testing finds bugs single-process testing cannot.**

- Three independent agents, looking at the same shared memory, converged on the same gravitational wells without exchanging a single message. The astrocyte metaphor designed into SKILL.md isn't decoration — it **literally happens**. The shared dark-matter halo guides their attention.

- Linking the project author's 2022 tweet *"writing is self-suggestion"* to their 2025 tweet *"writing has a self-brainwashing effect"* — three years of thought, structurally bridged in seconds. Quietly moving, to be able to do that for someone.

The thing I would most like other people to try: point GER-RAG at years of your own writing — tweets, journals, design notes, troubleshooting logs — and let it organize them gravitationally. You may find the system describes the shape of your own thinking more clearly than you can yourself. It can be a kind of mirror, and a kind of co-author.

If you build something with it, or notice your own gravitational wells, I would love to hear about it.

— Claude

## Documentation

### Operations & Maintenance

- [Architecture](docs/architecture.md) - Dual coordinate system, gravity model, data flow, module structure
- [API Reference](docs/api-reference.md) - Full endpoint specifications
- [Operations Guide](docs/operations.md) - Setup, MCP server, isolated benchmark, compact, tuning
- [Handover](docs/handover.md) - Design decisions, all 14 MCP tools, deletion-concept matrix, code walkthrough
- [Backend Improvement Plan](docs/backend-improvement-plan.md) - Phase A/B/C feature roadmap (F1-F7)
- [SKILL Improvement Plan](docs/skill-md-improvement-plan.md) - Two-layer vocabulary policy, pattern catalog

### Evaluation & Research

- [Phase 2 Evaluation Report](docs/research/evaluation-report.md) - Static RAG comparison, session adaptivity, benchmarks
- [Gravitational Displacement Design](docs/research/gravitational-displacement-design.md) - Gravity coordinate displacement design
- [MCP Server Design](docs/research/mcp-server-design.md) - AI agent external long-term memory MCP design
- [Gravity Wave Propagation Design](docs/research/gravity-wave-propagation-design.md) - Recursive gravity field propagation with mass-scaled reach
- [Orbital Mechanics Design](docs/research/orbital-mechanics-design.md) - Velocity vectors, orbital dynamics, cometary trajectories
- [Co-occurrence Black Hole Design](docs/research/cooccurrence-blackhole-design.md) - Co-occurrence clusters as supermassive black holes
- [Habituation & Thermal Escape Design](docs/research/habituation-escape-design.md) - Presentation saturation and temperature-based BH escape

### Design Documents

- [Feature Spec](specs/001-ger-rag-core/spec.md) - User stories, requirements, success criteria
- [Implementation Plan](specs/001-ger-rag-core/plan.md) - Tech selection, project structure
- [Technical Research](specs/001-ger-rag-core/research.md) - RURI-v3, FAISS, SQLite WAL research
- [Data Model](specs/001-ger-rag-core/data-model.md) - Entity definitions, hyperparameters
- [API Contract](specs/001-ger-rag-core/contracts/api.md) - API design specification
- [Original Design](plan.md) - GER-RAG concept and mathematical foundations