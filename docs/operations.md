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

```bash
# 起動
.venv/bin/uvicorn ger_rag.server.app:app --host 0.0.0.0 --port 8000

# 開発時（自動リロード）
.venv/bin/uvicorn ger_rag.server.app:app --reload

# 停止: Ctrl+C（graceful shutdown → dirty状態フラッシュ → FAISSインデックス保存）
```

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
