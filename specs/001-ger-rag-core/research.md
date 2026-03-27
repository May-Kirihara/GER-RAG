# Research: GER-RAG Core Retrieval System

**Date**: 2026-03-27
**Feature**: 001-ger-rag-core

## R1: Embedding Model Selection

**Decision**: RURI-v3-310m (`cl-nagoya/ruri-v3-310m`)
**Rationale**: Largest variant with 768-dim embeddings provides best semantic quality for Japanese text. Supports 8,192 token sequences (vs 512 in v2). Built on ModernBERT-Ja with FlashAttention.
**Alternatives considered**:
- ruri-v3-130m (512-dim): Lower memory, slightly less accurate
- ruri-v3-70m (384-dim): Faster but less semantic fidelity
- ruri-v3-30m (256-dim): Minimal footprint, insufficient for production retrieval quality

**Key details**:
- Requires `sentence-transformers>=4.48.0`, `transformers>=4.48.0`
- Query prefix: `"検索クエリ: "`, Document prefix: `"検索文書: "`
- GPU batch size: Start at 32, tune per hardware
- Embeddings must be L2-normalized for cosine similarity via FAISS IndexFlatIP

## R2: Vector Index (FAISS)

**Decision**: FAISS IndexFlatIP with L2-normalized vectors
**Rationale**: Exact search is sufficient for up to 100K documents (~1ms). Inner product on normalized vectors equals cosine similarity. No training required for flat index.
**Alternatives considered**:
- IndexIVFFlat: Faster at scale but adds complexity and requires training. Defer to Phase 2 if needed.
- IndexHNSW: Good recall/speed tradeoff but higher memory. Overkill at 100K scale.

**Key details**:
- All vectors must be L2-normalized before `add()` and before `search()`
- Use `add_with_ids()` not available on flat index — use index position to map to node IDs via a separate ID mapping array
- Batch additions for efficiency
- Save/load via `faiss.write_index()` / `faiss.read_index()`

## R3: Storage (SQLite WAL)

**Decision**: aiosqlite with WAL mode for async write-behind
**Rationale**: Native async integration with FastAPI's event loop. WAL mode enables concurrent reads during writes. `PRAGMA synchronous = NORMAL` avoids fsync overhead on commits.
**Alternatives considered**:
- Synchronous sqlite3 in thread pool: Works but less ergonomic with async code
- PostgreSQL: Overkill for Phase 1 single-instance deployment

**Key details**:
- Single writer connection for write-behind flushes
- Multiple reader connections acceptable
- Batch dirty state into single transactions
- Use `PRAGMA journal_mode = WAL` and `PRAGMA synchronous = NORMAL` at connection init

## R4: Serialization (sim_history)

**Decision**: msgpack for similarity history ring buffer serialization
**Rationale**: Compact binary format, fast encode/decode, native Python list support. No need for numpy-specific serialization since ring buffer is a simple float list.
**Alternatives considered**:
- JSON: Human-readable but larger and slower
- pickle: Python-specific, security concerns
- struct.pack: Manual format management

**Key details**:
- Package: `msgpack`
- Serialize: `msgpack.packb(float_list)`
- Deserialize: `msgpack.unpackb(blob)`

## R5: FastAPI Lifespan Pattern

**Decision**: Use `@asynccontextmanager` lifespan for startup/shutdown
**Rationale**: Modern recommended pattern. Code before yield loads cache from SQLite, initializes FAISS index. Code after yield flushes dirty state. Clean resource lifecycle management.
**Alternatives considered**:
- `@app.on_event("startup/shutdown")`: Deprecated in modern FastAPI

**Key details**:
- Startup: Load SQLite → populate node cache → load FAISS index → start write-behind task
- Shutdown: Cancel write-behind task → flush all dirty state → close connections
