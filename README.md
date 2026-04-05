# GER-RAG

**Gravity-Based Event-Driven RAG** - Long-term external memory for AI, where knowledge attracts knowledge

[日本語 README](README_ja.md)

## Overview

GER-RAG is a retrieval system designed as **long-term external memory for AI agents**. The more you use it, the more knowledge gravitates toward each other, producing **serendipitous connections and creative insights**.

Feed in technical documentation, digitized books, troubleshooting logs, design decision records — any scattered knowledge. As queries accumulate, related documents are pulled together by gravitational force, surfacing **cross-domain connections** that traditional RAG cannot find. GER-RAG runs as an MCP server, seamlessly integrated with Claude Code, Claude Desktop, and other AI agents.

### How it works

Knowledge nodes carry physical metadata — mass, temperature, and gravitational displacement. Co-retrieved documents attract each other through **Newtonian gravity**, and the knowledge space self-organizes with every query.

- **Frequently retrieved knowledge** gains mass and gravitationally pulls nearby documents, becoming hub stars
- **Co-retrieved knowledge** drifts closer together, enabling unexpected discoveries in future searches
- **Dormant knowledge** decays back to its original embedding position over time
- **Agent thoughts and troubleshooting experiences** can be stored as memories, persisting across sessions
- **Original embeddings are immutable** — gravitational displacement operates in a virtual coordinate space

### Use cases

| Use Case | How |
|----------|-----|
| Cross-domain document search | Ingest internal wikis and design docs with `ingest`, search with `recall` |
| Digitized book knowledge base | Bulk-load book markdown via `load_files.py`, reading notes gravitate toward book content |
| Troubleshooting log | `remember` errors and solutions, `recall` instantly surfaces past fixes for similar issues |
| Design decision journal | `remember` why you chose an approach, `recall` the reasoning months later |
| Context compaction offload | `remember(source="compaction")` to preserve context before LLM compression |
| Brainstorming & ideation | `explore(diversity=0.8)` to traverse cross-domain memories for unexpected inspiration |

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

| Tool | Purpose |
|------|---------|
| `remember` | Store thoughts, discoveries, user preferences, troubleshooting, context compaction |
| `recall` | Search with gravitational relevance (related memories surface more easily over time) |
| `explore` | Serendipitous exploration with increased temperature (discover unexpected connections) |
| `reflect` | Self-analyze memory state ("What have I been thinking about recently?") |
| `ingest` | Bulk-load files/directories (md, txt, csv) |

### SKILL.md (Agent Skill Definition)

[SKILL.md](SKILL.md) defines how AI agents should use GER-RAG long-term memory — when to use each tool, calling conventions, and usage patterns. Compatible with OpenClaw and other agent frameworks.

Contents:
- Tool invocation examples
- Usage patterns (context compaction, context restoration, decision logging, troubleshooting, user preference tracking, creative exploration)
- Source classification (agent / user / compaction / system)

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
# Dual coordinate system
Original embedding (immutable) ──→ FAISS broad candidate retrieval (top-K × 3)
                                        ↓
Virtual position = normalize(original_emb + displacement)
                                        ↓
gravity_sim = dot(query, virtual_pos)
final_score = gravity_sim * decay + mass_boost
                                        ↓
Gravity update: F = G * m_i * m_j / d²  →  displacement accumulation
```

| Component | Formula | Effect |
|-----------|---------|--------|
| gravity_sim | dot(query, virtual_pos) | Similarity in virtual space (changes with gravity) |
| decay | exp(-δ * (now - last_access)) | Prioritize recently accessed |
| mass_boost | α * log(1 + mass) | Prioritize frequently retrieved |
| gravitational force | G * m_i * m_j / d² | Attraction between co-occurring nodes |
| displacement decay | Asymptotic to zero over time | Dormant nodes return to original position |

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

## Documentation

### Operations & Maintenance

- [Architecture](docs/architecture.md) - Dual coordinate system, gravity model, data flow, module structure
- [API Reference](docs/api-reference.md) - Full endpoint specifications
- [Operations Guide](docs/operations.md) - Setup, testing, visualization, tuning
- [Handover](docs/handover.md) - Design decisions, code walkthrough, scripts, roadmap

### Evaluation & Research

- [Phase 2 Evaluation Report](docs/research/evaluation-report.md) - Static RAG comparison, session adaptivity, benchmarks
- [Gravitational Displacement Design](docs/research/gravitational-displacement-design.md) - Gravity coordinate displacement design
- [MCP Server Design](docs/research/mcp-server-design.md) - AI agent external long-term memory MCP design

### Design Documents

- [Feature Spec](specs/001-ger-rag-core/spec.md) - User stories, requirements, success criteria
- [Implementation Plan](specs/001-ger-rag-core/plan.md) - Tech selection, project structure
- [Technical Research](specs/001-ger-rag-core/research.md) - RURI-v3, FAISS, SQLite WAL research
- [Data Model](specs/001-ger-rag-core/data-model.md) - Entity definitions, hyperparameters
- [API Contract](specs/001-ger-rag-core/contracts/api.md) - API design specification
- [Original Design](plan.md) - GER-RAG concept and mathematical foundations