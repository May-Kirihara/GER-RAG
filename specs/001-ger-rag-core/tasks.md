# Tasks: GER-RAG Core Retrieval System

**Input**: Design documents from `/specs/001-ger-rag-core/`
**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, contracts/api.md

**Tests**: Not explicitly requested in the feature specification. Test tasks are omitted.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization, dependencies, and configuration

- [x] T001 Create project directory structure per plan.md: ger_rag/ with core/, embedding/, index/, store/, graph/, server/ subdirectories, each with __init__.py
- [x] T002 Create pyproject.toml with dependencies: fastapi, uvicorn, sentence-transformers>=4.48.0, transformers>=4.48.0, faiss-cpu, aiosqlite, msgpack, numpy, pydantic; dev deps: pytest, pytest-asyncio, httpx
- [x] T003 [P] Implement GERConfig dataclass with all hyperparameters and defaults from data-model.md in ger_rag/config.py

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**CRITICAL**: No user story work can begin until this phase is complete

- [x] T004 [P] Define all shared types (NodeState, CooccurrenceEdge, QueryResult, IndexRequest, IndexResponse, QueryRequest, QueryResponse, NodeResponse, GraphResponse, ResetResponse) in ger_rag/core/types.py using pydantic BaseModel; NodeState includes mass, temperature, last_access, sim_history fields with defaults per data-model.md
- [x] T005 [P] Implement RuriEmbedder in ger_rag/embedding/ruri.py: load cl-nagoya/ruri-v3-310m via SentenceTransformers, encode method that accepts list[str] with "検索クエリ: " prefix for queries and "検索文書: " prefix for documents, returns L2-normalized numpy arrays (768-dim)
- [x] T006 [P] Implement FaissIndex in ger_rag/index/faiss_index.py: __init__ with dimension=768, add(vectors, ids) storing ID mapping array, search(query_vector, top_k) returning (ids, scores), save/load methods using faiss.write_index/read_index, reset method
- [x] T007 [P] Define abstract StoreBase in ger_rag/store/base.py with async methods: save_documents, get_document, save_node_states, get_node_states, get_all_node_states, save_edges, get_edges_for_node, get_all_edges, reset_dynamic_state, close
- [x] T008 Implement SqliteStore in ger_rag/store/sqlite_store.py: create tables (nodes, edges, documents) per plan.md schema, WAL mode, PRAGMA synchronous=NORMAL, implement all StoreBase methods using aiosqlite, serialize sim_history with msgpack
- [x] T009 Implement CacheLayer in ger_rag/store/cache.py: node_cache (dict), graph_cache (dict), dirty_nodes (set), dirty_edges (set), get/set node state, get/set edges, mark_dirty methods, flush_to_store(store) batched write, load_from_store(store) on startup, reset method

**Checkpoint**: Foundation ready - user story implementation can now begin

---

## Phase 3: User Story 1 - Document Indexing (Priority: P1) MVP

**Goal**: Register documents via POST /index, generate embeddings, store in FAISS and SQLite, initialize dynamic state

**Independent Test**: Submit documents via /index, verify they are stored with correct initial state (mass=1.0, temperature=0.0)

### Implementation for User Story 1

- [x] T010 [US1] Implement GEREngine.__init__ in ger_rag/core/engine.py: accept GERConfig, RuriEmbedder, FaissIndex, CacheLayer, SqliteStore; store references; implement async startup() that loads cache and FAISS from store
- [x] T011 [US1] Implement GEREngine.index_documents in ger_rag/core/engine.py: accept list of (content, metadata) pairs, generate UUIDs, call embedder.encode with document prefix, L2-normalize, add to FAISS index, create NodeState with defaults, save documents and node states to cache and store, return list of generated IDs
- [x] T012 [US1] Implement FastAPI app with lifespan in ger_rag/server/app.py: asynccontextmanager lifespan that initializes SqliteStore, CacheLayer, RuriEmbedder, FaissIndex, GEREngine on startup; flushes cache and closes store on shutdown; store engine in app.state
- [x] T013 [US1] Implement POST /index endpoint in ger_rag/server/app.py: validate request body per contracts/api.md (documents array, non-empty content), call engine.index_documents, return IndexResponse with generated IDs and count; return 422 on validation errors

**Checkpoint**: POST /index works end-to-end. Documents are stored, embedded, and persisted.

---

## Phase 4: User Story 2 - Query and Dynamic Retrieval (Priority: P1)

**Goal**: Submit queries via POST /query, retrieve top-K with dynamic scoring (mass, decay, temperature, graph boost), update node state after each query

**Independent Test**: Index documents, query, verify results ranked by final_score; repeat queries and verify mass accumulation changes scores

### Implementation for User Story 2

- [x] T014 [P] [US2] Implement dynamic scoring functions in ger_rag/core/scorer.py: compute_mass_boost(mass, alpha), compute_decay(last_access, now, delta), compute_temp_noise(temperature), compute_final_score(raw_score, mass_boost, decay, temp_noise, graph_boost) per formula in spec FR-005; filter negative final_scores per FR-019
- [x] T015 [P] [US2] Implement CooccurrenceGraph in ger_rag/graph/cooccurrence.py: update_cooccurrence(result_ids) incrementing pair counts, form edges when count exceeds edge_threshold, compute_graph_boost(node_id, query_vector, embedder, faiss_index) per spec FR-008, decay_edges(edge_decay) and prune(prune_threshold, max_degree) per FR-009
- [x] T016 [US2] Implement GEREngine.query in ger_rag/core/engine.py: encode query with query prefix, FAISS search for top-K candidates, load node states from cache, compute dynamic scores via scorer (mass_boost, decay, temp_noise, graph_boost from CooccurrenceGraph), filter negative scores, sort by final_score, return QueryResult list
- [x] T017 [US2] Implement GEREngine._update_state_after_query in ger_rag/core/engine.py: for each top-K result update mass with logistic saturation (FR-016), append raw_score to sim_history ring buffer, recompute temperature as gamma * variance(sim_history) (FR-017), update last_access timestamp, update co-occurrence graph, mark nodes and edges dirty in cache
- [x] T018 [US2] Implement async write-behind task in ger_rag/store/cache.py: background asyncio task that periodically flushes dirty nodes and edges to store in batched transactions; start/stop methods called from lifespan
- [x] T019 [US2] Implement POST /query endpoint in ger_rag/server/app.py: validate request body per contracts/api.md (non-empty text, top_k 1-100 default 10), call engine.query, return QueryResponse with results and count; return 422 on validation errors
- [x] T020 [US2] Integrate CooccurrenceGraph into GEREngine.__init__ and startup; wire periodic edge decay/pruning into write-behind cycle in ger_rag/store/cache.py

**Checkpoint**: Full query pipeline works. Dynamic scoring, state updates, co-occurrence graph formation, and write-behind all functional.

---

## Phase 5: User Story 3 - Node State Inspection (Priority: P2)

**Goal**: Inspect individual node state via GET /node/{node_id} for debugging and tuning

**Independent Test**: Index documents, run queries, GET /node/{id} and verify mass/temperature/last_access values match expectations

### Implementation for User Story 3

- [x] T021 [US3] Implement GEREngine.get_node_state in ger_rag/core/engine.py: lookup node_id in cache (fallback to store), return NodeState or raise not-found error
- [x] T022 [US3] Implement GET /node/{node_id} endpoint in ger_rag/server/app.py: call engine.get_node_state, return NodeResponse per contracts/api.md; return 404 if node not found

**Checkpoint**: Node state inspection endpoint works. Developers can debug mass, temperature, and similarity history.

---

## Phase 6: User Story 4 - Co-occurrence Graph Inspection (Priority: P2)

**Goal**: View the emergent co-occurrence graph via GET /graph for understanding knowledge structure

**Independent Test**: Run multiple queries co-retrieving certain documents, GET /graph and verify edges with correct weights

### Implementation for User Story 4

- [x] T023 [US4] Implement GEREngine.get_graph in ger_rag/core/engine.py: return all edges from cache/store, support optional min_weight filter and node_id filter
- [x] T024 [US4] Implement GET /graph endpoint in ger_rag/server/app.py: accept optional query params min_weight and node_id per contracts/api.md, call engine.get_graph, return GraphResponse with edges and count

**Checkpoint**: Graph inspection endpoint works. Developers can view emergent co-occurrence structure.

---

## Phase 7: User Story 5 - State Reset (Priority: P3)

**Goal**: Reset all dynamic state while preserving documents and embeddings via POST /reset

**Independent Test**: Run queries to build up state, POST /reset, verify all mass=1.0, temperature=0.0, sim_history cleared, graph empty, but documents still queryable

### Implementation for User Story 5

- [x] T025 [US5] Implement GEREngine.reset in ger_rag/core/engine.py: reset all NodeState to defaults (mass=1.0, temperature=0.0, sim_history=[]), clear CooccurrenceGraph, flush reset state to store via cache.reset and store.reset_dynamic_state, return counts of nodes reset and edges removed
- [x] T026 [US5] Implement POST /reset endpoint in ger_rag/server/app.py: call engine.reset, return ResetResponse per contracts/api.md with reset=true, nodes_reset count, edges_removed count

**Checkpoint**: Full state reset works. All dynamic state cleared, documents and embeddings preserved.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Edge case handling, validation hardening, and operational readiness

- [x] T027 [P] Add input validation for empty/whitespace query text and empty document content across all endpoints in ger_rag/server/app.py
- [x] T028 [P] Add graceful shutdown flush ensuring all dirty state is persisted in ger_rag/store/cache.py and ger_rag/server/app.py lifespan shutdown
- [x] T029 Validate quickstart.md flow end-to-end (module-level verification complete): install deps, start server, index documents, query, inspect node, inspect graph, reset

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion - BLOCKS all user stories
- **US1 (Phase 3)**: Depends on Foundational - BLOCKS US2 (need indexed docs for queries)
- **US2 (Phase 4)**: Depends on US1 (requires documents to query against)
- **US3 (Phase 5)**: Depends on Foundational only (reads node state), but benefits from US2 being done
- **US4 (Phase 6)**: Depends on US2 (co-occurrence graph only exists after queries)
- **US5 (Phase 7)**: Depends on Foundational only (resets state)
- **Polish (Phase 8)**: Depends on all user stories being complete

### User Story Dependencies

```
Phase 1 (Setup)
    │
Phase 2 (Foundational)
    │
Phase 3 (US1: Indexing) ──────────────┐
    │                                  │
Phase 4 (US2: Query + Scoring)        │
    │                                  │
    ├── Phase 5 (US3: Node Inspect)    │
    │                                  │
    ├── Phase 6 (US4: Graph Inspect)   │
    │                                  │
    └──────────────────────────────────┼── Phase 7 (US5: Reset)
                                       │
                                Phase 8 (Polish)
```

### Within Each User Story

- Models/types before services
- Services before endpoints
- Core implementation before integration

### Parallel Opportunities

- T004, T005, T006, T007 can all run in parallel (Phase 2 foundational modules)
- T014, T015 can run in parallel (scorer and cooccurrence are independent modules)
- T027, T028 can run in parallel (different concerns, different files)
- US3 and US5 can start after Foundational if US2 is not yet complete (limited usefulness but possible)

---

## Parallel Example: Phase 2 (Foundational)

```
# Launch all independent foundational modules together:
Task T004: "Define shared types in ger_rag/core/types.py"
Task T005: "Implement RuriEmbedder in ger_rag/embedding/ruri.py"
Task T006: "Implement FaissIndex in ger_rag/index/faiss_index.py"
Task T007: "Define StoreBase in ger_rag/store/base.py"

# Then sequentially (depends on T007):
Task T008: "Implement SqliteStore in ger_rag/store/sqlite_store.py"
Task T009: "Implement CacheLayer in ger_rag/store/cache.py"
```

## Parallel Example: Phase 4 (US2 - Query)

```
# Launch independent scoring modules together:
Task T014: "Implement scoring functions in ger_rag/core/scorer.py"
Task T015: "Implement CooccurrenceGraph in ger_rag/graph/cooccurrence.py"

# Then sequentially (depends on T014, T015):
Task T016: "Implement GEREngine.query"
Task T017: "Implement state update after query"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL - blocks all stories)
3. Complete Phase 3: User Story 1 (Document Indexing)
4. **STOP and VALIDATE**: POST /index works, documents stored with correct initial state
5. Deploy/demo if ready

### Incremental Delivery

1. Setup + Foundational → Foundation ready
2. Add US1 (Indexing) → Test: POST /index → MVP!
3. Add US2 (Query + Scoring) → Test: POST /query with dynamic scores → Core feature complete
4. Add US3 (Node Inspect) → Test: GET /node/{id} → Debug capability
5. Add US4 (Graph Inspect) → Test: GET /graph → Observability
6. Add US5 (Reset) → Test: POST /reset → Experiment capability
7. Polish → Production-ready

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story for traceability
- Each user story should be independently completable and testable
- Commit after each task or logical group
- Stop at any checkpoint to validate story independently
- US1 → US2 is the critical path; US3-US5 are additive
- Total: 29 tasks across 8 phases
