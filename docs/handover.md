# 引継書

## プロジェクト概要

**GER-RAG** (Gravity-Based Event-Driven RAG) は、知識ノードが質量・温度・重力変位といった物理的メタデータを持ち、**共起した文書同士が重力で引き寄せ合う**ことで創発的な検索結果を生み出すシステムである。

目的は検索精度の最適化ではなく、**セレンディピティと創発** — 使い込むほど予想外のつながりが発見される検索体験。

## 現在の状態 (Phase A + B + C 完了済み)

### 実装済み機能

#### REST API（FastAPI）

| 機能 | エンドポイント / スクリプト | 状態 |
|------|--------------------------|------|
| ドキュメント登録 | POST /index | 完了 (SHA-256重複チェック付き) |
| 重力変位付き検索 | POST /query | 完了 (二段階: FAISS候補→仮想座標再計算) |
| ノード状態確認 | GET /node/{id} | 完了 (displacement_norm含む) |
| 共起グラフ確認 | GET /graph | 完了 |
| 状態リセット | POST /reset | 完了 (displacement含む) |

#### MCP ツール（`ger_rag.server.mcp_server`）

LLM 向けの長期記憶インターフェイス。`SKILL.md` がプロトコル仕様書。

| ツール | 機能ID | 概要 |
|------|----|------|
| remember | (基本)+F4+F7 | 知識保存。`source="hypothesis"` か `ttl_seconds` で揮発、`emotion`/`certainty` で重み付け |
| recall | (基本) | 重力波伝播による検索 |
| explore | (基本) | 温度を上げた創発的探索 |
| reflect | (基本)+F2+F3 | 状態分析。aspect ∈ {summary, hot_topics, connections, dormant, **duplicates**, **relations**} |
| ingest | (基本) | ファイル一括取り込み |
| **auto_remember** | F1 | transcript から保存候補を抽出（保存はしない） |
| **forget** | F5 | ソフトアーカイブ (`hard=True` で物理削除) |
| **restore** | F5 | ソフトアーカイブの復元 |
| **merge** | F2.1 | 重力衝突合体（質量加算 + 運動量保存 + エッジ移譲） |
| **compact** | F2+F4+F5+F3 | 定期メンテ: TTL expire + FAISS rebuild + 任意 auto-merge + orphan-edge 掃除 |
| **revalidate** | F7 | 確信度の再検証（last_verified_at を更新） |
| **relate** | F3 | 有向リレーション作成（supersedes/derived_from/contradicts） |
| **unrelate** | F3 | 有向リレーション削除 |
| **get_relations** | F3 | 有向リレーション一覧 |
| **prefetch** | F6 | バックグラウンド recall を予熱（アストロサイト的事前発火） |
| **prefetch_status** | F6 | キャッシュ + プールの状態確認 |
| **recall** | F6 | `force_refresh=False`（既定）で prefetch キャッシュを透過消費 |

#### スクリプト

| スクリプト | 用途 |
|---|---|
| scripts/load_csv.py | CSV 投入 |
| scripts/test_queries.py | basic / full / stress 3 モード |
| scripts/visualize_3d.py | Cosmic 3D 可視化（恒星表現、原始/仮想座標比較） |
| scripts/benchmark.py | ベンチ SC-001〜SC-007 |
| **scripts/run_benchmark_isolated.sh** | 本番 DB を退避し `/tmp/ger-rag-bench` で隔離ベンチ実行 |
| scripts/eval_export.py | LLM-as-judge 用書き出し |
| scripts/eval_compute.py | nDCG / MRR / Precision 算出 |

#### 削除概念の使い分け

| 操作 | 復元可能 | DB から消える | FAISS から消える | 用途 |
|---|---|---|---|---|
| `forget(hard=False)` | ✅ `restore` で可 | ❌ | ❌（compact で除去） | dormant 記憶の剪定（儀式的） |
| `forget(hard=True)` | ❌ | ✅ | ❌（compact で除去） | 確実に消したいとき |
| `merge` | ❌（履歴は merged_into で残る） | ❌ | ❌（compact で除去） | 類似記憶の統合 |
| TTL 期限切れ | ✅ `restore` で可 | ❌（archived） | ❌（compact で除去） | hypothesis の自動消去 |
| `reset` | ❌ | dynamic 状態のみ | ✅（全消去） | 全リセット（破壊的） |
| `compact` | ✅ | ❌ | ✅（archived ベクトルを除去） | 定期メンテナンス |

### 技術スタック

| コンポーネント | 選定技術 | 選定理由 |
|-------------|---------|---------|
| Embedding | RURI-v3-310m (768次元) | 日本語特化、8192トークン対応 |
| ベクトル検索 | FAISS IndexFlatIP | 100K件で~1ms、exact search |
| 重力計算 | NumPyベクトル演算 | 純粋関数、gravity.pyに集約 |
| ストレージ | SQLite WAL + aiosqlite | 非同期、displacement BLOB永続化 |
| キャッシュ | インメモリdict + write-behind | displacement_cacheを含む |
| API | FastAPI + uvicorn | 非同期、自動ドキュメント生成 |
| 可視化 | Plotly + PCA/UMAP | 恒星色温度表現、原始/仮想座標比較 |
| パッケージ管理 | uv | 高速、Python環境構築 |

### 設計判断の記録

| 判断事項 | 決定内容 | 経緯 |
|---------|---------|------|
| 二重座標系 | 原始embedding(不変) + 仮想座標(重力変動) | Phase 1評価で単一空間での限界が判明 |
| 重力モデル | 万有引力 F = G*m_i*m_j/d² | 物理的直感に合致、パラメータが明快 |
| 変位上限 | max_displacement_norm=0.3 | 暴走防止、同一大トピック内の移動に制限 |
| 候補拡張 | FAISS top-K×3 → 仮想座標で再計算 | FAISSリビルド不要、レイテンシ維持 |
| graph_boost廃止 | 重力変位に統合 | スコア加算では順位変動が不足 |
| 並行性 | Last-write-wins（ロックなし） | シングルインスタンス前提 |
| 重複チェック | content SHA-256ハッシュ | embedding生成前にスキップ |
| モデルキャッシュ | ローカルキャッシュ自動検出 | HuggingFace API通信を完全抑制 |
| 軌道力学 | 加速度→速度→位置の3段階物理 | 慣性による公転・彗星軌道、摩擦で減衰 |
| アンカー引力 | Hooke's law (F=-k*d) で原始位置に復元 | 脱出防止、銀河の暗黒物質ハローに相当 |
| 重力半径 | min_sim = 1 - G*mass/(2*a_min) | 質量から物理的に導出、周囲に星がなければ引き込まない |
| 重力波伝播 | 再帰的近傍展開、mass依存top-k | 深さ優先の探索、高massハブは広い重力圏 |
| 二層分離 | シミュレーション層 + プレゼンテーション層 | 全到達ノードの物理更新、LLMにはtop-5のみ |
| 創発性指標 | Rank Shift Rate, Serendipity Index | nDCGでは測れない創発的変化を定量化 |
| 共起ブラックホール | 共起クラスタ重心にBH形成 | 銀河束縛、edge_decayで自然消滅 |
| 返却飽和 | saturation = 1/(1+return_count*rate) | 同じ結果の繰り返しを防止、脳の馴化 |
| 温度脱出 | escape = 1/(1+temp*scale) | 高温ノードがBH束縛から脱出、探索促進 |
| **TTL 短期記憶 (F4)** | source="hypothesis" は default 7 日で auto-expire | Thinking ログから救った仮説を永続記憶と分離。物理アナロジー: 仮想粒子 |
| **archive vs hard delete (F5)** | デフォルト soft archive、`hard=True` で物理削除 | dormant 剪定はユーザー対話を儀式化。物理アナロジー: ホーキング輻射 |
| **重力衝突合体 (F2.1)** | 質量加算（飽和上限）+ 運動量保存（速度の質量加重平均）+ エッジ移譲 | 銀河衝突合体。`merged_into` で履歴保持、不可逆 |
| **情動・確信度の独立軸 (F7)** | scorer に \|emotion\| boost と certainty * exp(-age/half_life) を追加 | 質量とは直交する重み軸。物理アナロジー: 角運動量 / スピン量子数 |
| **有向リレーション (F3)** | 別テーブル `directed_edges` で typed directed edges。supersedes/derived_from/contradicts | 既存 cooccurrence (無向) の hot path を保持しつつ拡張 |
| **engine.compact()** | TTL expire + FAISS rebuild + 任意 auto-merge + orphan-edge 掃除 | 物理シミュレーションの定期メンテ。手動 or 将来 cron |
| **バックグラウンド prefetch (F6)** | LRU+TTL キャッシュ + asyncio.Semaphore で並行数を抑制 | 物理アナロジー: アストロサイト的事前発火。レイテンシ阻害ゼロを保証 |
| **prefetch キャッシュ無効化** | archive/restore/forget/merge/compact が `prefetch_cache.invalidate()` を呼ぶ | destructive op 後の stale を防ぐ。relate/unrelate は無効化しない（読みパスに影響しない） |

## コードの読み方

### エントリポイント

1. **サーバー起動**: `server/app.py` の `lifespan()` → 全コンポーネント初期化（displacement + velocity含む）
2. **MCP起動**: `server/mcp_server.py` の `get_engine()` → 遅延初期化
3. **クエリ処理**: `engine.query()` → `gravity.propagate_gravity_wave()` → 仮想座標スコアリング → top-5返却
4. **軌道力学**: `engine._update_simulation()` → `gravity.update_orbital_state()` (加速度→速度→位置の3段階)
5. **アンカー引力**: `gravity.compute_acceleration()` 内で `a -= k * displacement` (原始位置への復元)
6. **MCP remember**: `mcp_server.remember()` → `engine.index_documents()`
7. **MCP explore**: diversity制御で wave_depth/wave_k/gamma を一時変更

### 主要クラスの役割

| クラス | ファイル | 責務 |
|-------|---------|------|
| GEREngine | core/engine.py | wave探索 + 二層分離 + 軌道力学 + archive/forget/merge/compact/relate オーケストレーション |
| gravity.py | core/gravity.py | 軌道力学（加速度, 速度, wave伝播, アンカー, 重力半径, virtual_pos） |
| scorer.py | core/scorer.py | mass_boost, decay, **emotion_boost (F7)**, **certainty_boost (F7)** |
| **collision.py (F2.1)** | core/collision.py | 衝突合体（pick_survivor, compose_mass/velocity/displacement, merge_pair） |
| **clustering.py (F2)** | core/clustering.py | 類似クラスタリング（union-find）+ find_merge_candidates |
| **extractor.py (F1)** | core/extractor.py | auto_remember のヒューリスティック抽出 |
| **prefetch.py (F6)** | core/prefetch.py | PrefetchCache（LRU+TTL）+ PrefetchPool（Semaphore で並行制御） |
| RuriEmbedder | embedding/ruri.py | テキスト→ベクトル変換（ローカルキャッシュ自動検出） |
| FaissIndex | index/faiss_index.py | ベクトル近傍探索 + search_by_id() + get_vectors() |
| CacheLayer | store/cache.py | インメモリ状態管理 + displacement/velocity_cache + write-behind + **evict_node (F5)** |
| SqliteStore | store/sqlite_store.py | 永続化 + **archive/forget/expire (F4/F5) + directed_edges CRUD (F3)** |
| CooccurrenceGraph | graph/cooccurrence.py | 共起エッジの形成・減衰・剪定 (無向、untyped) |

### gravity.py は純粋関数

`core/gravity.py` の全関数は副作用なしの純粋関数。ユニットテストが書きやすい。

```python
# 軌道力学（メインパス）
compute_acceleration()         # 近傍引力 + アンカー復元力 + BH引力(飽和+温度脱出) → 加速度
compute_bh_acceleration()      # 共起クラスタBHへの引力（saturation + thermal escape適用）
update_velocity()              # v += a*dt, 摩擦適用, クランプ
update_orbital_state()         # 全ノードの3段階物理ステップ（acceleration→velocity→displacement）

# 座標・クランプ
compute_virtual_position()     # original + displacement + temp_noise → L2正規化
clamp_vector()                 # ベクトルのL2ノルム上限

# 重力波伝播
propagate_gravity_wave()       # 再帰的近傍展開、mass依存top-k、重力半径カットオフ

# レガシー（後方互換）
compute_gravitational_force()  # 旧: 力→直接displacement加算
apply_displacement_decay()     # 旧: 位置の定期減衰（摩擦に置換済み）
```

## スクリプト詳細

### scripts/load_csv.py
- `input/documents.csv` を読み込み、`POST /index` に分割投入
- DM/group_DMはプライバシー保護のため自動除外
- 長文（2000文字超）は `---` セパレータまたは段落区切りで自動チャンク分割
- 結果: 9,060行 → 約12,010チャンク

### scripts/test_queries.py
- **basic**: 5クエリ（動作確認）
- **full**: 37クエリ（多様なトピック + 架橋クエリで共起促進）
- **stress**: 82クエリ/ラウンド（多様 + 集中バーストでmass/displacement急速蓄積）

### scripts/visualize_3d.py (Cosmic View)
- FAISSから原始embedding + SQLiteからdisplacement を読み取り
- PCA/UMAPで3Dに次元削減
- 恒星色温度表現: temperature→スペクトル型 (M赤 → K橙 → G黄 → F白 → A/B青白)
- mass→恒星サイズ、decay×mass→明るさ
- `--compare` モードで原始座標と仮想座標を並列表示

### scripts/benchmark.py
- SC-001〜SC-007の成功基準を自動検証
- レイテンシ、mass蓄積、temporal decay、共起エッジ、並行処理を測定

### scripts/eval_export.py + eval_compute.py
- 静的RAG vs GER-RAGの比較データ書き出し
- セッション適応性: Before/After方式（リセット→観測→トレーニング→再観測）
- LLM-as-judge用プロンプト生成 → 外部LLMで判定 → nDCG/MRR/Precision算出

## Phase 2 評価結果サマリ

### 静的RAGとの比較

| メトリクス | Static RAG | GER-RAG | 差分 |
|-----------|-----------|---------|------|
| nDCG@10 | 0.9457 | 0.9708 | +2.7% |
| MRR | 0.8833 | 1.0000 | +13.2% |

### セッション適応性（重力変位後）

- 500クエリのトレーニングで10,000+エッジ、350+ノードが変位
- S2 (映画×食×旅) で nDCG +15.0% の改善
- 全シナリオ平均 nDCG +3.8%

詳細: [docs/research/evaluation-report.md](research/evaluation-report.md)

## 将来の拡張

### Phase 後の改善候補

- **engine.compact() の定期実行** — 現状は手動。write-behind ループに組み込む or cron で MCP `compact` を叩く運用
- **prefetch キャッシュキーを embedding 量子化に変更** — 現状は `(query_text, top_k)` 完全一致。「類似クエリでも hit」させたい場合は embedding を粗量子化したキーに

### 本番強化

- **PostgreSQL移行**: `store/base.py` のStoreBaseに対してPostgres実装を追加
- **マルチユーザー状態分離**: NodeState, CacheLayerにユーザーIDディメンション追加
- **認証**: FastAPIミドルウェアでAPIキー or OAuth2
- **IndexIVFFlat移行**: 100K件超でFAISSインデックスをIVFに切り替え

### 拡張時の注意点

- `store/base.py` のStoreBaseインターフェースを崩さない（abstract method の追加は OK）
- embeddingのL2正規化は必須（仮想座標もL2正規化している）
- RURI-v3のプレフィックス（「検索クエリ: 」「検索文書: 」）は省略不可
- displacement BLOB は768次元 float32（3KB/ノード）
- 既存DBは起動時にALTER TABLEで自動マイグレーション（追加列は必ず DEFAULT 付き）
- MCPツールのシグネチャ変更は禁止。新引数は必ずオプショナル
- ベンチ走行時は本番 DB を触らないよう `scripts/run_benchmark_isolated.sh` を使う

## ファイル構成

```
GER-RAG/
├── ger_rag/                       # メインパッケージ
│   ├── config.py                  # ハイパーパラメータ (★チューニング対象、F4/F7含む)
│   ├── core/
│   │   ├── engine.py              # オーケストレーション + archive/forget/merge/compact/relate/revalidate
│   │   ├── gravity.py             # 軌道力学（純粋関数）
│   │   ├── scorer.py              # スコアリング (mass/decay/emotion/certainty)
│   │   ├── extractor.py           # F1: auto_remember ヒューリスティック
│   │   ├── clustering.py          # F2: 類似クラスタリング (union-find)
│   │   ├── collision.py           # F2.1: 衝突合体ロジック
│   │   ├── prefetch.py            # F6: 非同期 prefetch (LRU+TTL+Semaphore)
│   │   └── types.py               # Pydantic モデル (NodeState/DirectedEdge等)
│   ├── embedding/                 # Embedding (キャッシュ自動検出)
│   ├── index/                     # FAISS (get_vectors 逆引き対応)
│   ├── store/                     # 永続化
│   │   ├── base.py                # StoreBase 抽象
│   │   ├── sqlite_store.py        # 全機能の永続化、自動マイグレーション
│   │   └── cache.py               # In-memory + write-behind + evict_node
│   ├── graph/                     # 共起グラフ (無向)
│   └── server/
│       ├── app.py                 # FastAPI（REST API）
│       └── mcp_server.py          # MCP（LLM 向け、SKILL.md と対応）
├── scripts/
│   ├── load_csv.py
│   ├── test_queries.py            # basic/full/stress
│   ├── visualize_3d.py            # Cosmic 3D
│   ├── benchmark.py               # SC-001〜SC-007
│   ├── run_benchmark_isolated.sh  # ★ 本番 DB を触らない隔離ベンチ
│   ├── eval_export.py
│   └── eval_compute.py
├── tests/                         # ★ Phase A/B で整備
│   ├── unit/                      # extractor, clustering, collision, scorer, store, cache
│   └── integration/               # engine end-to-end + MCP tool round-trip
├── input/                         # documents.csv
├── eval_output/                   # 評価結果
├── docs/
│   ├── handover.md                # このファイル
│   ├── operations.md              # 運用ガイド
│   ├── architecture.md            # アーキテクチャ全体像
│   ├── api-reference.md           # REST API リファレンス
│   ├── skill-md-improvement-plan.md  # SKILL.md 改良計画 (日本語)
│   ├── backend-improvement-plan.md   # 本体改修計画 (日本語、F1-F7)
│   └── research/                  # 評価レポート、設計書
├── specs/                         # 設計ドキュメント (speckit)
├── SKILL.md                       # MCP スキル定義 (英語、LLM 向けプロトコル)
├── .claude/skills/ger-rag/SKILL.md  # 同期コピー
├── pyproject.toml
└── .gitignore
```

## 連絡事項

- 設計経緯: `specs/001-ger-rag-core/` 以下の spec.md, plan.md, research.md
- 重力変位の設計根拠: `docs/research/gravitational-displacement-design.md`
- Phase 2評価の詳細: `docs/research/evaluation-report.md`
- Phase A/B 改修計画: `docs/backend-improvement-plan.md`
- SKILL.md 改良計画（物理＋生物の二層語彙）: `docs/skill-md-improvement-plan.md`
- スコアリング数式: `plan.md` Section 6 + `core/scorer.py`（F7 で emotion/certainty 拡張）
- ハイパーパラメータ推奨範囲: `plan.md` Section 11 + `config.py`
