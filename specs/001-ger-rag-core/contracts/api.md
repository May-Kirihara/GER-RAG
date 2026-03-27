# API Contract: GER-RAG Core Retrieval System

**Date**: 2026-03-27
**Feature**: 001-ger-rag-core
**Authentication**: None (Phase 1)

## POST /index

Register documents for retrieval.

**Request Body**:
```json
{
  "documents": [
    {
      "content": "ドキュメントのテキスト内容",
      "metadata": {"source": "example", "category": "tech"}
    }
  ]
}
```

- `documents`: array, required, min 1 item
- `documents[].content`: string, required, non-empty
- `documents[].metadata`: object, optional

**Response 200**:
```json
{
  "indexed": [
    {"id": "550e8400-e29b-41d4-a716-446655440000"}
  ],
  "count": 1
}
```

**Response 422**: Validation error (empty content, empty array)

---

## POST /query

Search for relevant documents with dynamic scoring.

**Request Body**:
```json
{
  "text": "検索クエリのテキスト",
  "top_k": 10
}
```

- `text`: string, required, non-empty
- `top_k`: integer, optional, default 10, range 1-100

**Response 200**:
```json
{
  "results": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "content": "ドキュメントのテキスト内容",
      "metadata": {"source": "example"},
      "raw_score": 0.85,
      "final_score": 0.92
    }
  ],
  "count": 5
}
```

- Results sorted by `final_score` descending
- Documents with negative `final_score` are excluded
- State updates (mass, temperature, co-occurrence) happen asynchronously after response

**Response 422**: Validation error (empty text)

---

## GET /node/{node_id}

Inspect dynamic state of a single node.

**Path Parameters**:
- `node_id`: string, required (UUID)

**Response 200**:
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "mass": 3.42,
  "temperature": 0.15,
  "last_access": 1711526400.0,
  "sim_history": [0.85, 0.72, 0.91, 0.68]
}
```

**Response 404**: Node not found

---

## GET /graph

Inspect the co-occurrence graph.

**Query Parameters**:
- `min_weight`: float, optional, default 0.0 (filter edges below this weight)
- `node_id`: string, optional (filter edges connected to this node)

**Response 200**:
```json
{
  "edges": [
    {
      "src": "550e8400-...",
      "dst": "661f9511-...",
      "weight": 7.5,
      "last_update": 1711526400.0
    }
  ],
  "count": 42
}
```

---

## POST /reset

Reset all dynamic state to initial values.

**Request Body**: None

**Response 200**:
```json
{
  "reset": true,
  "nodes_reset": 1500,
  "edges_removed": 42
}
```

Documents and embeddings are preserved. Only dynamic state (mass, temperature, sim_history, co-occurrence graph) is reset.
