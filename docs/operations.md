# 運用・保守ガイド

## 環境要件

| 項目 | 要件 |
|------|------|
| Python | 3.11以上 |
| GPU | CUDA対応GPU（embedding生成用） |
| メモリ | 4GB以上推奨（モデル + キャッシュ） |
| ディスク | 初回モデルダウンロードに約2GB |
| パッケージ管理 | uv |

## セットアップ

```bash
# 仮想環境作成
uv venv .venv --python 3.12

# 依存関係インストール
uv pip install -e ".[dev]"

# 可視化ツール（任意）
uv pip install plotly umap-learn

# GPU版FAISSを使う場合
uv pip install -e ".[gpu]"
```

## サーバー起動・停止

GER-RAG は **2 つのサーバー** を提供する。用途に応じて選ぶ:

### REST API サーバー（FastAPI）

ベンチマーク・評価・REST クライアントから使う。

```bash
# 起動
.venv/bin/uvicorn ger_rag.server.app:app --host 0.0.0.0 --port 8000

# 開発時（自動リロード）
.venv/bin/uvicorn ger_rag.server.app:app --reload

# 停止: Ctrl+C（graceful shutdown → dirty状態フラッシュ → FAISSインデックス保存）
```

### MCP サーバー（Claude Code / Claude Desktop / 他 MCP クライアント）

LLM の長期記憶として使う。プロトコル仕様は `SKILL.md`。

```bash
# stdio (Claude Code / Claude Desktop)
.venv/bin/python -m ger_rag.server.mcp_server

# SSE (リモートクライアント)
.venv/bin/python -m ger_rag.server.mcp_server --transport sse --port 8001
```

公開ツール（詳細は `SKILL.md`）:
`remember` / `recall` / `explore` / `reflect` / `ingest` /
`auto_remember` / `forget` / `restore` / `merge` / `compact` / `revalidate` /
`relate` / `unrelate` / `get_relations` /
`prefetch` / `prefetch_status`

初回起動時にRURI-v3モデル（約1.2GB）がHugging Faceからダウンロードされる。2回目以降はローカルキャッシュから即座にロードされ、HuggingFaceへのHTTPリクエストは発生しない。

## データ投入

```bash
# CSVからの一括投入（DM/group_DMは自動除外、長文は自動チャンク分割）
.venv/bin/python scripts/load_csv.py

# 件数制限付き（テスト用）
.venv/bin/python scripts/load_csv.py --limit 100

# チャンクサイズ変更
.venv/bin/python scripts/load_csv.py --max-chunk-chars 3000
```

重複contentはSHA-256ハッシュで自動スキップされるため、同じCSVを複数回投入しても安全。レスポンスの`skipped`フィールドでスキップ数を確認できる。

## テストクエリ

```bash
# 基本テスト（5クエリ、動作確認用）
.venv/bin/python scripts/test_queries.py --mode basic

# 多様なクエリ（37トピック、幅広い動的変化の蓄積）
.venv/bin/python scripts/test_queries.py --mode full --rounds 3

# ストレステスト（大量クエリでmass/temperature/共起グラフを高速蓄積）
.venv/bin/python scripts/test_queries.py --mode stress --rounds 10
```

| モード | クエリ数/ラウンド | 用途 |
|--------|------------------|------|
| basic | 5 | 動作確認 |
| full | 37（毎回シャッフル） | 多様なトピック網羅、共起パターン生成 |
| stress | 82（37多様 + 45バースト） | 可視化デモ前の大量蓄積 |

## 3D可視化

サーバー停止後に実行する（DB + FAISSファイルを直接読む）。

```bash
# 仮想座標ビュー（重力変位後の宇宙空間）
.venv/bin/python scripts/visualize_3d.py --sample 3000 --open

# 原始座標 vs 仮想座標の並列比較
.venv/bin/python scripts/visualize_3d.py --compare --sample 3000 --open

# UMAP（局所構造をよく保存、遅め）
.venv/bin/python scripts/visualize_3d.py --method umap --sample 3000 --open

# 全件（ブラウザが重い場合あり）
.venv/bin/python scripts/visualize_3d.py --compare --open
```

出力はPlotly HTMLファイル。ブラウザでドラッグ回転、ホバーでノード詳細（質量、温度、スペクトル型、変位量）表示。

### 視覚エンコーディング（Cosmic View）

ドキュメントを宇宙空間の恒星として表現する。

| 視覚要素 | 動的状態 | 恒星アナロジー |
|---------|---------|--------------|
| サイズ | Mass (質量) | 赤色巨星（大きい）vs 矮星（小さい） |
| 色温度 | Temperature | M赤 → K橙 → G黄 → F白 → A/B青白 |
| 明るさ | Decay × Mass | 最近アクセスされた高質量ノードが最も明るい |
| フィラメント | 共起エッジ | 宇宙の大規模構造 |

恒星分類の例：
- **赤色巨星**: 高mass + 低temperature — 安定して頻繁に検索されるドキュメント
- **青色超巨星**: 高mass + 高temperature — 多様な文脈で活発に検索される不安定なドキュメント
- **赤色矮星**: 低mass + 低temperature — まだあまり検索されていないドキュメント
- **ダスト**: 未検索ノード — ほぼ見えない背景

### 動的変化を確認する手順

1. サーバー起動 → `load_csv.py` → `test_queries.py --mode stress --rounds 10` → サーバー停止
2. `visualize_3d.py --compare --sample 3000 --open` で Before/After 比較
3. サーバー再起動 → 追加クエリ実行 → サーバー停止 → 再度可視化 → 星の移動・色変化を観察

## 隔離ベンチマーク（本番 DB を触らずに測る）

ベンチマーク・性能検証は本番 DB に触らない `scripts/run_benchmark_isolated.sh` を使う。

```bash
# 200 件で隔離ベンチ（既定）
.venv/bin/bash scripts/run_benchmark_isolated.sh

# 件数を変える
.venv/bin/bash scripts/run_benchmark_isolated.sh 1000

# 別ポートで実行（既定 8765）
BENCH_PORT=8800 ./scripts/run_benchmark_isolated.sh

# 別ディレクトリに退避
BENCH_DIR=/tmp/my-bench ./scripts/run_benchmark_isolated.sh
```

挙動:

1. `GER_RAG_DATA_DIR=/tmp/ger-rag-bench` を設定して uvicorn を起動 → 本番 DB は不可触
2. 200 件（or 指定数）の文書をベンチ DB に投入
3. SC-001〜SC-007 + Baseline drift を実行
4. 結果は `/tmp/ger-rag-bench/report.json` に保存
5. ベンチ DB は確認用に残す（`rm -rf /tmp/ger-rag-bench` で消去）

## メンテナンス（compact）

長期運用では archived/expired ノードのベクトルが FAISS に残り、wave 伝播のヒット率を下げる。週次〜月次で `compact` を呼ぶ:

```python
# Python（FastAPI 経由ではなく engine 直叩き、または MCP 経由）
await engine.compact()                         # 安全な既定（TTL + FAISS rebuild）
await engine.compact(auto_merge=True,          # 重複も自動合体
                     merge_threshold=0.95)
await engine.compact(rebuild_faiss=False,      # TTL のみ
                     auto_merge=False)
```

MCP 経由の場合:

```
compact()                                  # 既定
compact(auto_merge=true, merge_threshold=0.95)
```

レポート例:
```
Compaction complete:
  TTL-expired:    3
  Auto-merged:    2 pairs
  FAISS rebuilt:  True
  FAISS vectors:  1503 → 1496
```

## 永続化ファイル

| ファイル | 内容 | 消失時の影響 |
|---------|------|------------|
| `ger_rag.db` | SQLite DB（ドキュメント、ノード状態、共起エッジ） | 全データ消失、再投入が必要 |
| `ger_rag.faiss` | FAISSベクトルインデックス | 起動時に再構築不可、再投入が必要 |
| `ger_rag.faiss.ids` | FAISS位置→ドキュメントIDマッピング | 上記と同様 |

### バックアップ

```bash
# サーバー停止中に実行
cp ger_rag.db ger_rag.db.bak
cp ger_rag.faiss ger_rag.faiss.bak
cp ger_rag.faiss.ids ger_rag.faiss.ids.bak
```

**注意**: サーバー稼働中のバックアップは、dirty状態がフラッシュされていない可能性がある。確実なバックアップにはサーバー停止が必要。

### 完全リセット

```bash
# サーバー停止後
rm ger_rag.db ger_rag.faiss ger_rag.faiss.ids
# サーバー再起動で空の状態から開始
```

## ハイパーパラメータの変更

`ger_rag/config.py` の `GERConfig` を編集する。サーバー再起動で反映。

### チューニングの指針

### スコアリング・質量

| パラメータ | 影響 | 上げると | 下げると |
|-----------|------|---------|---------|
| alpha (0.05) | mass boostの重み | 頻出ドキュメントをより強く優先 | 類似度ベースに近づく |
| delta (0.01) | 時間減衰の速さ | 古いアクセスが早く忘れられる | 長期間アクセスが維持される |
| gamma (0.5) | temperatureの感度 | ノイズが大きくなり探索的に | 安定的な検索結果 |
| eta (0.05) | mass増加速度 | 少ないクエリで重要度が上がる | ゆっくり重要度が蓄積 |
| edge_threshold (5) | エッジ形成の閾値 | 強い共起のみエッジ化 | 弱い共起でもエッジ化 |
| top_k (10) | 返却件数 | 多くの結果を返す | 上位のみに絞る |

### 重力変位

| パラメータ | 影響 | 上げると | 下げると |
|-----------|------|---------|---------|
| gravity_G (0.01) | 万有引力定数 | 急速に引き寄せ合う（創発的） | 穏やかな変位（安定） |
| gravity_eta (0.005) | 変位の学習率 | 1回のクエリでの変位が大きい | 段階的に変位 |
| displacement_decay (0.995) | 変位の定期減衰 | 変位が長く維持される | 早く元に戻る |
| max_displacement_norm (0.3) | 変位の上限 | 遠くまで移動可能（探索的） | 原始位置から離れにくい（安全） |
| candidate_multiplier (3) | FAISS候補倍率 | 広い候補から選べる（多様性↑） | 高速だが候補が狭い |

### 馴化・温度脱出

| パラメータ | 影響 | 上げると | 下げると |
|-----------|------|---------|---------|
| saturation_rate (0.2) | 返却飽和の速さ | 少ない返却で飽和（新鮮さ重視） | 何度も同じ結果を返す（安定重視） |
| habituation_recovery_rate (0.01) | 馴化からの回復速度 | 早く新鮮さを回復 | 長く飽和が持続 |
| thermal_escape_scale (5000) | 温度によるBH脱出効果 | 高温ノードがBHから脱出しやすい | 温度に関わらずBHに束縛 |
| bh_mass_scale (0.5) | BH質量のスケーリング | BH引力が強い（密なクラスタ） | BH引力が弱い（緩いクラスタ） |

### TTL 短期記憶（F4）

| パラメータ | 影響 | 上げると | 下げると |
|-----------|------|---------|---------|
| default_hypothesis_ttl_seconds (604800 = 7日) | hypothesis ソースの既定寿命 | 仮説が長く残る | 早く消える |

`remember(ttl_seconds=...)` で個別上書き可能。

### auto_remember（F1）

| パラメータ | 影響 | 上げると | 下げると |
|-----------|------|---------|---------|
| auto_remember_default_max (5) | 候補数の既定 | 多めに提示 | 厳選 |
| auto_remember_min_chars (12) | 候補の最短文字数 | 長い行のみ | 短い行も対象に |
| auto_remember_max_chars (400) | 候補の最長文字数 | 長文も対象に | 短文に絞る |

### 情動・確信度（F7）

| パラメータ | 影響 | 上げると | 下げると |
|-----------|------|---------|---------|
| emotion_alpha (0.04) | \|emotion\| の boost 重み | 情動的な記憶を強く優先 | 情動の影響を抑える |
| certainty_alpha (0.02) | certainty の boost 重み | 高確信度の記憶を強く優先 | 確信度の影響を抑える |
| certainty_half_life_seconds (2,592,000 = 30日) | 確信度の半減期 | 確信度が長く保たれる | 早く減衰、`revalidate` 推奨頻度↑ |

### バックグラウンド prefetch（F6）

| パラメータ | 影響 | 上げると | 下げると |
|-----------|------|---------|---------|
| prefetch_cache_size (64) | LRU エントリ上限 | hit 率↑、メモリ消費↑ | エビクション頻度↑ |
| prefetch_ttl_seconds (90) | キャッシュ寿命 | 同じクエリの hit 期間が伸びる | 早めに stale 扱い、最新性↑ |
| prefetch_max_concurrent (4) | 同時実行プレフェッチ数 | スループット↑、foreground recall への影響↑ | 完全に backgrounded、待ち発生↑ |

## トラブルシューティング

### クエリのスコアが初回だけ極端に低い

正常動作。初回クエリ時、`last_access` がインデックス時のタイムスタンプのため、`decay = exp(-δ * 経過時間)` が非常に小さくなる。2回目以降は直近アクセスなので decay ≈ 1.0 になる。

### メモリ使用量が大きい

- embeddingモデル: 約1.5GB（GPU VRAM）
- FAISSインデックス: 768次元 * 4byte * ドキュメント数（100K件で約300MB）
- ノードキャッシュ: ドキュメント数に比例

### SQLiteロックエラー

WALモードでも単一writerの制限あり。write-behindタスクとの競合の場合、`flush_interval_seconds` を長くすることで緩和可能。

### 異常終了後の起動

フラッシュされていないdirty状態は消失するが、ドキュメントとembeddingは保全される。動的状態（mass, temperature）はクエリを繰り返すことで自然に再構築される。

### 可視化が重い

24,000ノード全件はブラウザが重くなる場合がある。`--sample 3000` でサンプリングするか、`--method pca`（デフォルト）を使う。UMAPは計算に時間がかかるが局所構造の保存が優れる。

### archived ノードが大量に溜まった

`forget(hard=False)` の蓄積、または TTL hypothesis の自動 expire が積み重なると、FAISS index に「使われないベクトル」が残り続ける。症状: wave 伝播のヒット率低下、レイテンシ増加。

**対処**: `compact(rebuild_faiss=True)` を実行すると active ノードのみで FAISS を再構築する。週次〜月次推奨。

### 重力衝突合体 (merge) が暴走する

`compact(auto_merge=True, merge_threshold=...)` の閾値が低すぎると、似て非なる記憶を融合してしまう。
**対処**:
- `merge_threshold` を 0.95 以上に保つ
- `auto_merge` は default OFF。明示的に有効化したときのみ動く
- 心配な場合は手動で `reflect(aspect="duplicates")` → 中身を確認 → `merge(node_ids=[...])`

### 確信度が古いまま下がっていく（F7）

`certainty_half_life_seconds`（既定 30 日）を超えると certainty boost が指数減衰する。記憶が確実に有効と確認した時点で `revalidate(node_id)` を呼ぶと last_verified_at が更新され、boost が回復する。

### prefetch のヒット率が低い（F6）

`prefetch_status` を見て `hit_rate` が極端に低い場合:

- **クエリ文字列が完全一致しない** — F6 のキャッシュキーは `(query_text, top_k)` の完全一致。クエリの揺らぎ（句読点、語順）でキャッシュミスする。LLM 側で「prefetch と recall に渡す query を完全一致させる」プロトコル徹底が必要
- **TTL が短すぎる** — `prefetch_ttl_seconds` を伸ばす（既定 90s → 300s 等）
- **destructive op が頻繁** — `forget`/`merge`/`compact` のたびに全エントリが invalidate される。それで OK な設計だが、頻発するなら戦略再考
