# Phase U WP-6a — startup 計装実測 (production-scale copy, 2026-08-26)

- 対象: production copy 42,060 nodes / index 40,007 / embedder は外部 service (起動時間に含まず)
- 計測: `engine.startup_timings` (WP-6a 計装, perf_counter)

## 結果

| phase | 秒 |
|---|---|
| **bm25_build** | **147.31** |
| cache_load | 4.37 |
| diagnostics | 0.62 |
| ttl_scan | 0.43 |
| virtual_faiss_load | 0.08 |
| faiss_load | 0.08 |
| store_init | 0.01 |
| manifest / lease / background_loops | ≈0.00 |
| **startup_total** | **152.89** |

## Decision gate 判定

- **BM25 build が 96% を占める → WP-6c (background build) / WP-6d (snapshot) とも GO** (plan §4 WP-6a gate)。
- BM25 を startup 同期処理から外せば **SEMANTIC_READY ≈ 5.6s** — review acceptance「semantic-ready 30 秒以内」は大幅余裕。
- review で観測された production cold start 129s と同じプロファイル (BM25 支配) と推定。
- cache_load 4.4s は軽微 — cache 高速化の descope 判断は不要。

## 補足

- diagnostics Tier B は copy 上 info のみ (faiss 40,007 vs active 42,060 = drift 4.9%, id overlap 100% — TTL archive 済み 2,053 件分の差で既知の orphan 問題と整合)。
