# Implementation Plan: GER-RAG Core Retrieval System

**Branch**: `001-ger-rag-core` | **Date**: 2026-03-27 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/001-ger-rag-core/spec.md`

## Summary

Implement a gravity-based event-driven RAG system where knowledge nodes maintain dynamic metadata (mass, temperature, decay) that modulates retrieval scoring over time. The system uses RURI-v3 for Japanese text embedding, FAISS for ANN search, and a dynamic scoring layer with co-occurrence graph propagation. Storage is SQLite (WAL mode) with an in-memory write-behind cache. Exposed as a FastAPI web service.

## Technical Context

**Language/Version**: Python 3.11+
**Primary Dependencies**: FastAPI, uvicorn, sentence-transformers (>=4.48.0), faiss-cpu/faiss-gpu, aiosqlite, msgpack, numpy, pydantic
**Storage**: SQLite (WAL mode) + in-memory LRU cache with async write-behind
**Testing**: pytest + pytest-asyncio + httpx (for FastAPI test client)
**Target Platform**: Linux server with CUDA GPU
**Project Type**: web-service
**Performance Goals**: <50ms query latency for 100K documents, 50 concurrent queries
**Constraints**: Single-instance, single-tenant, no authentication, GPU required for embedding
**Scale/Scope**: Up to 100,000 documents, single user/session

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

No constitution file found. Gate passes by default — no constraints to enforce.

**Post-Phase 1 re-check**: Design uses a single Python package with clear module separation. No unnecessary abstractions. All complexity is justified by the spec requirements. Gate passes.

## Project Structure

### Documentation (this feature)

```text
specs/001-ger-rag-core/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   └── api.md           # Phase 1 output
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
ger_rag/
├── __init__.py
├── config.py               # Hyperparameters (dataclass with defaults)
├── core/
│   ├── __init__.py
│   ├── engine.py           # GEREngine: query → retrieve → score → update → return
│   ├── scorer.py           # Dynamic scoring (mass, decay, temp, graph)
│   └── types.py            # NodeState, Edge, QueryResult, IndexRequest, etc.
├── embedding/
│   ├── __init__.py
│   └── ruri.py             # RURI-v3 SentenceTransformers wrapper
├── index/
│   ├── __init__.py
│   └── faiss_index.py      # FAISS wrapper (add, search, save/load, ID mapping)
├── store/
│   ├── __init__.py
│   ├── base.py             # Abstract store interface (for future DB swap)
│   ├── sqlite_store.py     # SQLite (WAL) implementation
│   └── cache.py            # Write-behind in-memory cache layer
├── graph/
│   ├── __init__.py
│   └── cooccurrence.py     # Edge formation, pruning, decay, graph_boost calc
└── server/
    ├── __init__.py
    └── app.py              # FastAPI app with lifespan, all endpoints

tests/
├── conftest.py             # Shared fixtures (test engine, sample docs)
├── unit/
│   ├── test_scorer.py
│   ├── test_cooccurrence.py
│   └── test_cache.py
├── integration/
│   ├── test_engine.py
│   └── test_store.py
└── contract/
    └── test_api.py         # FastAPI endpoint contract tests
```

**Structure Decision**: Single Python package (`ger_rag/`) matching the module structure from plan.md Section 13. Tests organized by type (unit/integration/contract). No frontend — API-only service.

## Complexity Tracking

No constitution violations to justify.
