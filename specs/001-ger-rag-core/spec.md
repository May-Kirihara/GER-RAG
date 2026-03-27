# Feature Specification: GER-RAG Core Retrieval System

**Feature Branch**: `001-ger-rag-core`
**Created**: 2026-03-27
**Status**: Draft
**Input**: User description: "plan.mdに示すRAGの実装を行って下さい"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Document Indexing (Priority: P1)

A developer registers documents into the system so that they become searchable. The developer sends document text and receives confirmation that the documents have been indexed and are available for retrieval.

**Why this priority**: Without indexed documents, no retrieval is possible. This is the foundational capability.

**Independent Test**: Can be fully tested by submitting a set of documents and verifying they are stored, embedded, and queryable. Delivers the base data layer.

**Acceptance Scenarios**:

1. **Given** the system is running with no documents, **When** a user submits one or more documents via the index endpoint, **Then** the system stores the documents, generates embeddings, adds them to the search index, and initializes their dynamic state (mass=1.0, temperature=0.0).
2. **Given** documents are already indexed, **When** a user submits additional documents, **Then** the new documents are added without affecting existing documents or their dynamic state.
3. **Given** a user submits a document with no text content, **When** the index request is processed, **Then** the system rejects the request with a clear error message.

---

### User Story 2 - Query and Dynamic Retrieval (Priority: P1)

A user submits a natural language query and receives relevant documents ranked by a combination of semantic similarity and dynamic factors (mass, decay, temperature, co-occurrence graph boost). The system updates the state of retrieved nodes after each query.

**Why this priority**: This is the core value proposition of GER-RAG -- retrieval that adapts over time based on usage patterns.

**Independent Test**: Can be tested by indexing documents, issuing queries, and verifying that results are returned ranked by the dynamic scoring formula. Repeated queries should show changing scores.

**Acceptance Scenarios**:

1. **Given** documents are indexed, **When** a user submits a query, **Then** the system returns the top-K most relevant documents ranked by final_score (combining similarity, mass boost, decay, temperature noise, and graph boost).
2. **Given** a document has been retrieved multiple times, **When** a new query matches it, **Then** its mass-boosted score is higher than an equally similar document that has never been retrieved.
3. **Given** a document has not been retrieved for a long period, **When** a new query matches it, **Then** its score is reduced by temporal decay compared to a recently retrieved document of equal similarity.
4. **Given** two documents are frequently co-retrieved, **When** a query matches one of them, **Then** the other receives a graph boost to its score.

---

### User Story 3 - Node State Inspection (Priority: P2)

A developer inspects the current dynamic state of any node (mass, temperature, last access time, similarity history) for debugging and tuning purposes.

**Why this priority**: Essential for understanding system behavior and tuning hyperparameters, but not required for core retrieval functionality.

**Independent Test**: Can be tested by indexing documents, running queries, then inspecting node state to verify mass, temperature, and timestamps have been updated correctly.

**Acceptance Scenarios**:

1. **Given** a document has been indexed and queried, **When** a developer requests the node state, **Then** the system returns the current mass, temperature, last access timestamp, and similarity history.
2. **Given** a node ID that does not exist, **When** a developer requests its state, **Then** the system returns a clear not-found error.

---

### User Story 4 - Co-occurrence Graph Inspection (Priority: P2)

A developer views the emergent co-occurrence graph to understand which documents are frequently retrieved together and how strong their associations are.

**Why this priority**: Useful for understanding emergent knowledge structure, but secondary to core retrieval.

**Independent Test**: Can be tested by running multiple queries that co-retrieve certain documents, then inspecting the graph to verify edges and weights.

**Acceptance Scenarios**:

1. **Given** multiple queries have co-retrieved certain documents, **When** a developer requests the graph, **Then** the system returns edges with weights reflecting co-occurrence frequency.
2. **Given** no queries have been run, **When** a developer requests the graph, **Then** the system returns an empty graph.

---

### User Story 5 - State Reset (Priority: P3)

A developer resets all dynamic state (mass, temperature, timestamps, co-occurrence graph) back to initial values while preserving indexed documents and embeddings.

**Why this priority**: Useful for experimentation and testing but not needed for normal operation.

**Independent Test**: Can be tested by running queries to build up state, resetting, then verifying all dynamic values are back to defaults.

**Acceptance Scenarios**:

1. **Given** the system has accumulated dynamic state from queries, **When** a developer triggers a reset, **Then** all mass values return to default, temperatures reset to zero, similarity histories are cleared, and the co-occurrence graph is emptied -- but documents and embeddings remain intact.

---

### Edge Cases

- What happens when the query text is empty or contains only whitespace?
- Concurrent queries updating the same node's state use last-write-wins semantics (no locking).
- What happens when the in-memory cache is full and new nodes need to be loaded?
- On unclean shutdown, unflushed dirty state (mass, temperature changes) may be lost; documents and embeddings are preserved. This is acceptable for Phase 1.
- What happens when the co-occurrence graph grows very large (high node degree)?
- Documents with a negative final_score after temperature noise are excluded from the result set.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST accept documents with text content and optional metadata for indexing. Document IDs are system-generated (UUID); users cannot specify IDs.
- **FR-002**: System MUST generate embeddings for submitted documents and store them in a vector search index.
- **FR-003**: System MUST initialize dynamic state (mass, temperature, last access time, similarity history) for each indexed document.
- **FR-004**: System MUST retrieve top-K candidate documents from the vector index based on cosine similarity to the query embedding.
- **FR-005**: System MUST compute a final score for each candidate using the dynamic scoring formula: final_score = raw_score * decay + mass_boost + temp_noise + graph_boost.
- **FR-006**: System MUST update the mass, temperature, last access timestamp, and similarity history of all top-K retrieved nodes after each query.
- **FR-007**: System MUST maintain a co-occurrence graph where edges form between documents that are frequently co-retrieved, with weights reflecting co-occurrence frequency.
- **FR-008**: System MUST apply graph boost to candidate scores based on co-occurrence neighbors' similarity to the query.
- **FR-009**: System MUST periodically decay edge weights and prune edges below a threshold, and cap per-node degree.
- **FR-010**: System MUST persist node state and graph edges to durable storage asynchronously (write-behind) without blocking query responses.
- **FR-011**: System MUST load active node state into an in-memory cache at startup and serve reads from cache during operation.
- **FR-012**: System MUST flush all dirty state to durable storage on shutdown.
- **FR-013**: System MUST expose an endpoint to inspect individual node state (mass, temperature, last access, similarity history).
- **FR-014**: System MUST expose an endpoint to inspect the co-occurrence graph.
- **FR-015**: System MUST expose an endpoint to reset all dynamic state to initial values while preserving documents and embeddings.
- **FR-016**: System MUST apply logistic saturation to mass updates to prevent unbounded growth.
- **FR-017**: System MUST compute temperature as a function of variance in recent similarity scores.
- **FR-018**: System MUST use last-write-wins semantics for concurrent state updates (no locking required).
- **FR-019**: System MUST exclude documents with negative final_score from query results.
- **FR-020**: API endpoints MUST NOT require authentication in this phase (open access).

### Key Entities

- **Document/Node**: A piece of indexed text with an immutable embedding vector and mutable dynamic state (mass, temperature, last access time, similarity history).
- **Co-occurrence Edge**: A weighted relationship between two nodes that emerges when they are frequently retrieved together in the same query result set.
- **Query Result**: A ranked list of documents with their final scores, returned in response to a user query.
- **Node State**: The dynamic metadata (mass, temperature, last access timestamp, similarity history buffer) associated with each document.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Users receive query results within 50 milliseconds for a corpus of up to 100,000 documents.
- **SC-002**: Documents retrieved more than 5 times score measurably higher than equally similar documents retrieved only once, demonstrating mass accumulation.
- **SC-003**: Documents not retrieved for an extended period score measurably lower than recently retrieved documents of equal similarity, demonstrating temporal decay.
- **SC-004**: Documents with strong co-occurrence relationships receive a measurable score boost when their co-retrieved partners match a query, demonstrating emergent graph structure.
- **SC-005**: The system handles at least 50 concurrent queries without errors or data corruption.
- **SC-006**: All dynamic state survives a clean shutdown and restart cycle without data loss.
- **SC-007**: A full state reset completes within 5 seconds and returns all dynamic values to defaults while preserving all indexed documents.

## Clarifications

### Session 2026-03-27

- Q: 並行クエリ時のノード状態更新戦略は？ → A: Last-write-wins（最後の書き込み優先、ロックなし）
- Q: ドキュメントIDの生成方式は？ → A: システムがUUIDを自動生成（ユーザーは指定不可）
- Q: APIの認証・セキュリティは？ → A: 認証なし（開発・検証フェーズ向け）
- Q: 異常終了時のデータ復旧方針は？ → A: ダーティ状態の消失を許容（ドキュメント・エンベディングは保全）
- Q: 負のfinal_scoreの扱いは？ → A: 負スコアのドキュメントを結果から除外

## Assumptions

- The system is deployed as a single-instance service (no distributed deployment in this phase).
- GPU is available for embedding generation.
- The target corpus size is up to 100,000 documents for this phase.
- Users interact with the system via its API endpoints; there is no graphical user interface.
- The system serves a single tenant (no multi-user state isolation in this phase).
- Document content is plain text (no binary/media content processing).
- The embedding model is pre-trained and used as-is; no fine-tuning is required.
- Periodic edge pruning and mass decay run within the same process (no external scheduler needed).
- API endpoints are unauthenticated in this phase; authentication is deferred to Phase 3.
- Unclean shutdown may lose in-flight dynamic state; this is acceptable as dynamic state rebuilds naturally through usage.
