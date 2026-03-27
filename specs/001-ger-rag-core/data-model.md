# Data Model: GER-RAG Core Retrieval System

**Date**: 2026-03-27
**Feature**: 001-ger-rag-core

## Entities

### Document

The indexed unit of text content with metadata.

| Field    | Type   | Constraints                        |
| -------- | ------ | ---------------------------------- |
| id       | string | UUID, system-generated, immutable  |
| content  | string | Non-empty, plain text              |
| metadata | object | Optional, arbitrary key-value JSON |

**Lifecycle**: Created on index → persists through reset → deleted only if system is fully wiped.

### NodeState

Dynamic metadata attached to each Document, updated on every retrieval.

| Field       | Type         | Default | Constraints                          |
| ----------- | ------------ | ------- | ------------------------------------ |
| id          | string       | —       | FK to Document.id, immutable         |
| mass        | float        | 1.0     | >= 0, capped by m_max (logistic)     |
| temperature | float        | 0.0     | >= 0, derived from sim_history variance |
| last_access | float        | creation time | Unix timestamp                 |
| sim_history | list[float]  | []      | Ring buffer, max size N              |

**Lifecycle**: Initialized on document index → updated on each retrieval → reset to defaults on `/reset` → survives clean shutdown.

**State transitions**:
- `idle` → `retrieved`: mass increases, temperature recalculated, last_access updated
- `retrieved` → `decaying`: no retrieval for extended period, decay reduces effective score
- Any state → `reset`: `/reset` endpoint returns all fields to defaults

### Embedding

Immutable vector representation of a Document.

| Field     | Type         | Constraints                           |
| --------- | ------------ | ------------------------------------- |
| id        | string       | FK to Document.id                     |
| vector    | float[768]   | L2-normalized, 768-dim (RURI-v3-310m) |

**Lifecycle**: Created once on index → immutable → persists through reset.

### CooccurrenceEdge

Emergent relationship between two documents co-retrieved in query results.

| Field       | Type   | Constraints                              |
| ----------- | ------ | ---------------------------------------- |
| src         | string | FK to Document.id                        |
| dst         | string | FK to Document.id, src < dst (canonical) |
| weight      | float  | >= 0, starts at 0, incremented on co-retrieval |
| last_update | float  | Unix timestamp of last weight change     |

**Lifecycle**: Created when co-occurrence count exceeds edge_threshold → weight decays periodically → pruned when weight drops below prune_threshold → cleared on `/reset`.

**Constraints**:
- Edges are undirected; stored with canonical ordering (src < dst)
- Per-node degree capped at max_degree; weakest edges pruned when exceeded

### QueryResult

Transient response object (not persisted).

| Field       | Type         | Description                          |
| ----------- | ------------ | ------------------------------------ |
| id          | string       | Document ID                          |
| content     | string       | Document text                        |
| metadata    | object       | Document metadata                    |
| raw_score   | float        | Cosine similarity to query           |
| final_score | float        | After dynamic scoring (> 0 only)     |

## Relationships

```
Document 1──1 NodeState     (each document has exactly one state)
Document 1──1 Embedding     (each document has exactly one embedding)
Document M──N CooccurrenceEdge  (documents linked by co-retrieval patterns)
```

## In-Memory Cache Structure

| Cache             | Key               | Value                    | Eviction     |
| ----------------- | ----------------- | ------------------------ | ------------ |
| node_cache        | Document.id       | NodeState                | LRU          |
| graph_cache       | Document.id       | dict[neighbor_id, weight]| LRU          |
| dirty_nodes       | —                 | set[Document.id]         | Flush on write-behind |
| dirty_edges       | —                 | set[(src, dst)]          | Flush on write-behind |

## Hyperparameters

| Parameter       | Type  | Default | Description                     |
| --------------- | ----- | ------- | ------------------------------- |
| alpha (α)       | float | 0.05    | Mass boost scaling              |
| delta (δ)       | float | 0.01    | Temporal decay rate             |
| gamma (γ)       | float | 0.5     | Temperature scaling             |
| eta (η)         | float | 0.05    | Mass growth rate                |
| m_max           | float | 50.0    | Mass saturation limit           |
| rho (ρ)         | float | 0.1     | Graph propagation weight        |
| edge_threshold  | int   | 5       | Co-occurrence count for edge    |
| edge_decay      | float | 0.97    | Edge weight decay factor        |
| prune_threshold | float | 0.5     | Edge removal threshold          |
| max_degree      | int   | 20      | Per-node edge cap               |
| sim_buffer_size | int   | 20      | Similarity history ring buffer  |
| top_k           | int   | 10      | Top-K retrieval count           |
