# Gravity-Based Event-Driven RAG (GER-RAG)

## 1. Overview

GER-RAG is a retrieval system where knowledge nodes maintain dynamic metadata (mass, temperature, decay) that modulates retrieval scoring over time.

Unlike traditional RAG (static vector search) or GraphRAG (explicit graph construction), GER-RAG uses **event-driven dynamic scoring**:

* Embedding space is preserved (no node movement)
* Queries update node metadata (mass, temperature, last access)
* Frequently accessed knowledge gains mass and is prioritized
* Rarely accessed knowledge decays naturally
* Co-retrieval patterns form emergent graph structure

---

## 2. Design Goals

* Reduce computational cost vs GraphRAG
* Enable dynamic, self-organizing knowledge structure
* Incorporate temporal/query history without explicit sequence modeling
* Maintain full compatibility with ANN-based retrieval (FAISS, etc.)
* Preserve embedding model's semantic guarantees
* Minimize query latency (storage-based with in-memory caching)

---

## 3. Architecture

```
[固定embedding空間] → ANN検索 → Top-K候補
                                    ↓
                            [動的スコアリング層]
                            (mass, decay, temperature, graph_boost)
                                    ↓
                              最終ランキング
                                    ↓
                            [状態更新 (Top-Kのみ)]
                            [共起グラフ更新]
                                    ↓
                            [非同期write-behind → SQLite]
```

---

## 4. Technology Stack

| Component       | Selection                                    |
| --------------- | -------------------------------------------- |
| Embedding       | RURI-v3 (SentenceTransformers, GPU)          |
| ANN Search      | FAISS (IndexFlatIP → IndexIVFFlat at scale)  |
| Storage         | SQLite (WAL mode) + in-memory cache          |
| Co-occurrence   | In-memory adjacency + SQLite persistence     |
| API Server      | FastAPI                                      |
| Future          | MCP Server / PostgreSQL migration            |

### Storage Strategy

```
起動時: SQLiteからアクティブノードをメモリにロード
クエリ時:
  1. FAISS検索 → Top-K取得
  2. キャッシュからノード状態取得（ミスならDBフォールバック）
  3. スコアリング+状態更新（メモリ上）
  4. 非同期でDBに書き戻し（バッチ write-behind）

シャットダウン時: ダーティ状態をDBにフラッシュ
```

### Latency Budget (per query, target)

| Step                   | Target   | Notes                        |
| ---------------------- | -------- | ---------------------------- |
| Query embedding        | ~20ms    | RURI-v3 GPU                  |
| FAISS search           | ~1ms     | IndexFlatIP, ~100K nodes     |
| State read (K nodes)   | ~0.01ms  | In-memory cache              |
| Scoring + graph boost  | ~0.1ms   | NumPy operations             |
| State write            | async    | Non-blocking write-behind    |
| **Total (blocking)**   | **~21ms**|                              |

---

## 5. Node Representation

Each document/node `i` maintains:

* `x_i`: Embedding vector (**immutable**, original RURI-v3 output)
* `m_i`: Mass (importance / frequency, with logistic saturation)
* `T_i`: Temperature (context variability, sim-variance based)
* `t_last_i`: Last access timestamp
* `sim_history_i`: Recent similarity scores (ring buffer, size N)

---

## 6. Retrieval Flow

### Step 1: ANN Retrieval

Retrieve Top-K candidates using original embeddings:

```
TopK = ANN_Search(q, {x_i})
```

---

### Step 2: Dynamic Scoring

For each node `i ∈ TopK`:

```
raw_score   = sim(q, x_i)                          # cosine similarity
mass_boost  = α * log(1 + m_i)                     # frequency-based priority (log saturation)
decay       = exp(-δ * (t_now - t_last_i))          # temporal decay
temp_noise  = Normal(0, T_i)                        # exploration noise

final_score = raw_score * decay + mass_boost + temp_noise
```

---

### Step 3: Co-occurrence Graph Propagation

For nodes connected by co-occurrence edges:

```
graph_boost = Σ_{j ∈ neighbors(i)} w_ij * sim(q, x_j)
final_score += ρ * graph_boost
```

---

### Step 4: Re-rank and Return

Sort by `final_score`, return top results.

---

## 7. State Update (Post-Retrieval)

Only Top-K nodes are updated per query. O(K) cost.

### 7.1 Mass Update

```
m_i += η * raw_score * (1 - m_i / m_max)
```

* Logistic saturation prevents unbounded growth
* Frequently retrieved nodes gain inertia, but plateau

### 7.2 Temperature Update

```
sim_history_i.append(raw_score)
T_i = γ * Var(sim_history_i)
```

* Consistent high-sim retrieval → low temperature (stable knowledge)
* Variable sim scores → high temperature (context-dependent knowledge)

### 7.3 Timestamp Update

```
t_last_i = t_now
```

### 7.4 Co-occurrence Graph Update

For each pair `(i, j)` in Top-K result:

```
cooccurrence[i][j] += 1
if cooccurrence[i][j] > edge_threshold:
    add_edge(i, j, weight=cooccurrence[i][j])
```

### 7.5 Edge Pruning & Decay

```
# Periodic (every P queries or T seconds)
for edge in all_edges:
    edge.weight *= edge_decay
    if edge.weight < prune_threshold:
        remove_edge(edge)

# Per-node degree cap
for node in all_nodes:
    if degree(node) > max_degree:
        remove weakest edges
```

---

## 8. Forgetting Mechanism

Implicit via decay:

* Nodes not retrieved → `t_last_i` stales → `decay` shrinks → lower final_score
* Eventually fall below retrieval threshold

Optional explicit mass decay:

```
m_i *= mass_decay   (periodic, for all nodes)
```

---

## 9. Complexity

Per query:

* ANN retrieval: O(log N) or sublinear
* Scoring + graph propagation: O(K * avg_degree)
* State update: O(K) + O(K^2) for co-occurrence pairs

No global recomputation. No ANN reindexing.

---

## 10. Comparison

| System   | Structure                | Cost              | Dynamics | Embedding Integrity |
| -------- | ------------------------ | ----------------- | -------- | ------------------- |
| RAG      | Static vectors           | Low               | None     | Preserved           |
| GraphRAG | Explicit graph (LLM)     | High (build time) | Partial  | Preserved           |
| GER-RAG  | Emergent (co-occurrence) | Low (O(K))        | High     | Preserved           |

---

## 11. Hyperparameters

| Parameter       | Meaning                        | Suggested Range |
| --------------- | ------------------------------ | --------------- |
| α               | Mass boost scaling             | 0.01 - 0.1     |
| δ               | Temporal decay rate            | 0.001 - 0.05   |
| γ               | Temperature scaling            | 0.1 - 1.0      |
| η               | Mass growth rate               | 0.01 - 0.1     |
| m_max           | Mass saturation limit          | 10 - 100        |
| ρ               | Graph propagation weight       | 0.05 - 0.2     |
| edge_threshold  | Co-occurrence count for edge   | 3 - 10          |
| edge_decay      | Edge weight decay factor       | 0.95 - 0.99    |
| prune_threshold | Edge removal threshold         | 0.1 - 1.0      |
| max_degree      | Per-node edge cap              | 10 - 50         |
| N               | Sim history buffer size        | 10 - 50         |
| K               | Top-K retrieval count          | 5 - 20          |

---

## 12. Database Schema

### SQLite Tables

```sql
-- Node state
CREATE TABLE nodes (
    id          TEXT PRIMARY KEY,
    mass        REAL DEFAULT 1.0,
    temperature REAL DEFAULT 0.0,
    last_access REAL,
    sim_history BLOB  -- msgpack serialized float array
);

-- Co-occurrence graph edges
CREATE TABLE edges (
    src         TEXT,
    dst         TEXT,
    weight      REAL DEFAULT 0.0,
    last_update REAL,
    PRIMARY KEY (src, dst)
);
CREATE INDEX idx_edges_src ON edges(src);
CREATE INDEX idx_edges_dst ON edges(dst);

-- Document content (for result display)
CREATE TABLE documents (
    id       TEXT PRIMARY KEY,
    content  TEXT,
    metadata TEXT  -- JSON
);
```

### In-Memory Cache

```python
node_cache: dict[str, NodeState]           # LRU eviction
graph_cache: dict[str, dict[str, float]]   # {node_id: {neighbor_id: weight}}
dirty_nodes: set[str]                      # Pending write-back
dirty_edges: set[tuple[str, str]]          # Pending write-back
```

### Write-Behind Strategy

* Flush every N seconds or when dirty count exceeds threshold
* Batched inside single SQLite transaction
* Reads always served from cache (no blocking on write)

---

## 13. Module Structure

```
ger_rag/
├── core/
│   ├── engine.py           # query → retrieve → score → update → return
│   ├── scorer.py           # dynamic scoring (mass, decay, temp, graph)
│   └── types.py            # NodeState, Edge, QueryResult, etc.
├── embedding/
│   └── ruri.py             # RURI-v3 SentenceTransformers wrapper
├── index/
│   └── faiss_index.py      # FAISS wrapper (add, search, save/load)
├── store/
│   ├── base.py             # Abstract interface (for future DB swap)
│   ├── sqlite_store.py     # SQLite (WAL) implementation
│   └── cache.py            # Write-behind in-memory cache
├── graph/
│   └── cooccurrence.py     # Edge formation, pruning, decay, graph_boost
├── server/
│   └── app.py              # FastAPI endpoints
└── config.py               # All hyperparameters
```

---

## 14. API Endpoints

```
POST /index          # Register documents (embedding + FAISS + state init)
POST /query          # Search (ANN → dynamic scoring → state update → result)
GET  /node/{id}      # Inspect node state (debug)
GET  /graph          # Inspect co-occurrence graph (debug)
POST /reset          # Reset all dynamic state
```

Future: expose `/query` as MCP tool.

---

## 15. Implementation Phases

### Phase 1: Full-Feature Prototype

* FastAPI + RURI-v3 (GPU) + FAISS
* Dynamic scoring (mass, decay, temperature, graph_boost)
* Co-occurrence graph (edge formation + pruning + decay)
* SQLite (WAL) + in-memory cache + async write-behind
* All API endpoints

### Phase 2: Evaluation & Tuning

* Benchmarks (retrieval accuracy, latency, session adaptivity)
* Hyperparameter sensitivity analysis
* Comparison with static RAG baseline

### Phase 3: Production Hardening

* PostgreSQL migration (if scale demands)
* MCP Server integration
* Multi-user state isolation
* Micro-batch embedding for throughput

---

## 16. Conceptual Summary

GER-RAG treats knowledge retrieval as a dynamic system without modifying the embedding space:

* Frequently used knowledge gains priority through mass accumulation
* Rare knowledge fades through temporal decay
* Uncertain knowledge explores through temperature-driven noise
* Conceptual relationships emerge from co-retrieval patterns
* The embedding model's semantic guarantees are fully preserved

---

## 17. Future Extensions

* Energy-inspired scoring constraints
* Hierarchical co-occurrence clustering
* Temporal query weighting
* Hybrid with symbolic graph layers
* Cross-session knowledge transfer

---

End of Specification
