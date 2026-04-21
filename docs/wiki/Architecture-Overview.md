# Architecture — Overview

GaOTTT のモジュール構成と二重座標系の概要。

## モジュール構成

```
gaottt/
├── server/
│   ├── app.py            FastAPI (REST)
│   └── mcp_server.py     MCP (LLM 向け、25 ツール)
├── core/
│   ├── engine.py         オーケストレーション
│   ├── gravity.py        軌道力学（純粋関数）
│   ├── scorer.py         スコアリング（mass, decay, emotion, certainty）
│   ├── extractor.py      F1: auto_remember
│   ├── clustering.py     F2: 類似クラスタ (union-find)
│   ├── collision.py      F2.1: 衝突合体
│   ├── prefetch.py       F6: 非同期 prefetch (LRU + Semaphore)
│   └── types.py          Pydantic モデル
├── embedding/            RURI-v3-310m
├── index/                FAISS IndexFlatIP
├── store/
│   ├── base.py           StoreBase 抽象
│   ├── sqlite_store.py   永続化（自動マイグレーション）
│   └── cache.py          In-memory + write-behind
└── graph/                共起グラフ（無向）
```

## 二重座標系

GaOTTT の中核アイディア:

```
[原始 embedding 空間]   ── RURI-v3 が出力、不変
       ↓
[displacement BLOB]     ── 重力で更新される変位ベクトル
       ↓
[仮想座標 = original + displacement] ── recall で実際にスコアされる位置
```

これにより:
- 原始 embedding は破壊されない（再構築可能）
- 重力変位は仮想空間でのみ作用（FAISS リビルド不要）
- レイテンシ維持（FAISS top-K × 3 → 仮想座標で再計算）

## クエリフロー

```
クエリ
  ↓
RURI-v3 で embedding
  ↓
prefetch cache hit? ─ Yes → 即時返却
  ↓ No
gravity wave propagation（再帰的近傍展開、mass 依存 top-k）
  ↓
N ノード到達（シミュレーション層）
  ↓
仮想座標で再スコア（gravity_sim × decay + mass_boost + wave_boost
                  + emotion_boost + certainty_boost）× saturation
  ↓
top-K 返却（プレゼンテーション層）
  ↓
全到達ノードの軌道力学更新（acceleration → velocity → displacement）
```

## データフロー

### インデックス時

1. `POST /index` でドキュメント受信（または MCP `remember`/`ingest`）
2. content SHA-256 ハッシュで重複チェック（重複は embedding 生成前にスキップ）
3. RURI-v3 で embedding 生成（「検索文書: 」プレフィックス付き）
4. L2 正規化後、FAISS インデックスに追加
5. NodeState 初期化（`mass=1.0`, `temperature=0.0`, `displacement=zero`）
6. SQLite にドキュメント + 状態を永続化

### クエリ時

1. `POST /query` または MCP `recall` でクエリ受信
2. RURI-v3 でクエリ embedding 生成（「検索クエリ: 」プレフィックス付き）
3. prefetch cache hit 確認 → hit 時は即返却
4. 重力波伝播で広い候補取得（top-K × candidate_multiplier）
5. 各候補の仮想座標を計算（`original_emb + displacement`）
6. 仮想座標での cosine similarity × decay + mass_boost + wave_boost + emotion_boost + certainty_boost で最終スコア
7. 負スコアを除外、top-K に絞って返却
8. **返却後** に重力更新: 共起ペアに force 適用 → displacement 蓄積、mass/temperature/共起グラフも更新
9. ダーティ状態は write-behind で非同期に SQLite へフラッシュ

### ストレージ戦略

```
起動時:   SQLite → in-memory cache にロード + FAISS インデックスロード
稼働時:   キャッシュから読み取り → 変更は dirty セットに記録
定期的:   write-behind タスクが dirty 状態をバッチで SQLite にフラッシュ（5 秒間隔）
停止時:   write-behind 停止 → 全 dirty 状態フラッシュ → FAISS 保存 → 接続クローズ
異常終了: フラッシュされていない dirty 状態は消失（ドキュメント・embedding は保全）
```

## レイヤ別役割

| レイヤ | 責務 |
|---|---|
| **server/** | プロトコル変換（REST / MCP） |
| **core/** | 物理シミュレーション、スコアリング、ロジック |
| **embedding/** | テキスト → ベクトル |
| **index/** | ベクトル近傍探索 |
| **store/** | 永続化、in-memory cache、write-behind |
| **graph/** | 共起グラフ |

## 設計判断の記録

GaOTTT の主要な設計選択とその根拠:

| 判断事項 | 決定内容 | 経緯 |
|---|---|---|
| 二重座標系 | 原始 embedding（不変）+ 仮想座標（重力変動） | Phase 1 評価で単一空間の限界が判明 |
| 重力モデル | 万有引力 `F = G×m_i×m_j/d²` | 物理的直感に合致、パラメータが明快 |
| 変位上限 | `max_displacement_norm=0.3` | 暴走防止、同一大トピック内の移動に制限 |
| 候補拡張 | FAISS top-K × 3 → 仮想座標で再計算 | FAISS リビルド不要、レイテンシ維持 |
| graph_boost 廃止 | 重力変位に統合 | スコア加算では順位変動が不足 |
| 並行性 | Last-write-wins（ロックなし） | シングルインスタンス前提（後に WAL + busy_timeout で複数 OK） |
| 重複チェック | content SHA-256 ハッシュ | embedding 生成前にスキップ |
| モデルキャッシュ | ローカルキャッシュ自動検出 | HuggingFace API 通信を完全抑制 |
| 軌道力学 | 加速度 → 速度 → 位置の 3 段階物理 | 慣性による公転・彗星軌道、摩擦で減衰 |
| アンカー引力 | Hooke's law (`F=-k×d`) で原始位置に復元 | 脱出防止、銀河の暗黒物質ハローに相当 |
| 重力半径 | `min_sim = 1 - G×mass/(2×a_min)` | 質量から物理的に導出 |
| 重力波伝播 | 再帰的近傍展開、mass 依存 top-k | 高 mass ハブは広い重力圏 |
| 二層分離 | シミュレーション層 + プレゼンテーション層 | 全到達ノードの物理更新、LLM には top-5 のみ |
| 共起ブラックホール | 共起クラスタ重心に BH 形成 | 銀河束縛、edge_decay で自然消滅 |
| 返却飽和 | `saturation = 1/(1+return_count×rate)` | 同じ結果の繰り返し防止、脳の馴化 |
| 温度脱出 | `escape = 1/(1+temp×scale)` | 高温ノードが BH 束縛から脱出、探索促進 |
| **TTL 短期記憶 (F4)** | `source="hypothesis"` は default 7 日で auto-expire | 物理アナロジー: 仮想粒子 |
| **archive vs hard delete (F5)** | デフォルト soft archive、`hard=True` で物理削除 | dormant 剪定の儀式化、物理アナロジー: ホーキング輻射 |
| **重力衝突合体 (F2.1)** | 質量加算 + 運動量保存 + エッジ移譲 | 銀河衝突合体、`merged_into` で履歴保持、不可逆 |
| **情動・確信度の独立軸 (F7)** | scorer に `\|emotion\|` boost と certainty × exp(-age) | 質量と直交する軸、物理アナロジー: 角運動量 / スピン |
| **有向リレーション (F3)** | 別テーブル `directed_edges`、typed | 既存 cooccurrence の hot path を保持しつつ拡張 |
| **engine.compact()** | TTL expire + FAISS rebuild + 任意 auto-merge + orphan-edge 掃除 | 物理シミュレーションの定期メンテ |
| **バックグラウンド prefetch (F6)** | LRU+TTL キャッシュ + asyncio.Semaphore | 物理アナロジー: アストロサイト的事前発火、レイテンシ阻害ゼロ |
| **prefetch キャッシュ無効化** | archive/restore/forget/merge/compact が `prefetch_cache.invalidate()` を呼ぶ | destructive op 後の stale を防ぐ |
| **タスク状態を edge で表現 (Phase D)** | `task_status` 列を持たず、`completed`/`abandoned` エッジの存在で判定 | 完了の重力史が人格の年表になる |
| **人格を多源で表現 (Phase D)** | source = task / commitment / intention / value / style / relationship:* | 既存スキーマで実現、新テーブル不要 |
| **inherit_persona の儀式化** | 散文出力で過去の自分を着る | 柱 X「観測者を創ること即存在」のセッション継承版 |

## エントリポイントの読み方

主要な処理フローの入り口を順に追えば、コード全体が読み解ける:

1. **サーバー起動**: [`server/app.py`](../../gaottt/server/app.py) の `lifespan()` → 全コンポーネント初期化
2. **MCP 起動**: [`server/mcp_server.py`](../../gaottt/server/mcp_server.py) の `get_engine()` → 遅延初期化
3. **クエリ処理**: [`engine.query()`](../../gaottt/core/engine.py) → `gravity.propagate_gravity_wave()` → 仮想座標スコアリング
4. **軌道力学**: `engine._update_simulation()` → `gravity.update_orbital_state()`（3 段階物理）
5. **MCP `remember`**: `mcp_server.remember()` → `_save_memory()` → `engine.index_documents()`
6. **MCP `commit`** (Phase D): `mcp_server.commit()` → `_save_memory(source="task")` → `engine.relate(fulfills, parent)`

## 詳細セクション

- [Storage & Schema](Architecture-Storage-And-Schema.md) ── テーブル定義、列の意味
- [Gravity Model](Architecture-Gravity-Model.md) ── スコア式、軌道力学
- [Concurrency](Architecture-Concurrency.md) ── WAL、busy_timeout、複数プロセス共存

## コード参照

- [`gaottt/`](../../gaottt/) — 全モジュールの実装
- [`gaottt/config.py`](../../gaottt/config.py) — 全ハイパーパラメータ
