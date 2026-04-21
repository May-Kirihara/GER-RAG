# Guide — Cosmic 3D Visualization

GaOTTT の記憶宇宙を、**恒星と銀河のように** Plotly で可視化するツール。

## 起動

サーバー停止後に実行（DB と FAISS ファイルを直接読む）:

```bash
# 仮想座標ビュー（重力変位後）
.venv/bin/python scripts/visualize_3d.py --sample 3000 --open

# 原始座標 vs 仮想座標の並列比較
.venv/bin/python scripts/visualize_3d.py --compare --sample 3000 --open

# UMAP（局所構造保存、遅め）
.venv/bin/python scripts/visualize_3d.py --method umap --sample 3000 --open

# 全件
.venv/bin/python scripts/visualize_3d.py --compare --open
```

## 視覚エンコーディング

| 視覚要素 | 動的状態 | 恒星アナロジー |
|---|---|---|
| サイズ | Mass | 赤色巨星 vs 矮星 |
| 色温度 | Temperature | M赤 → K橙 → G黄 → F白 → A/B青白 |
| 明るさ | Decay × Mass | 最近アクセス + 高質量が最も明るい |
| シアン矢印 | 速度ベクトル | 次のステップでの移動方向 |
| 金色リング | 重力半径 | mass 由来の重力圏 |
| 紫◆ | 共起 BH | 共起クラスタの重心引力源 |
| フィラメント | 共起エッジ | 宇宙の大規模構造 |

## 恒星分類の例

- **赤色巨星** (高 mass + 低 temperature) — 安定して頻繁に検索されるドキュメント
- **青色超巨星** (高 mass + 高 temperature) — 多様な文脈で活発に検索される不安定な恒星
- **赤色矮星** (低 mass + 低 temperature) — まだあまり検索されていない記憶
- **ダスト** (未検索) — 背景にぼんやり

## 動的変化を見る手順

1. サーバー起動
2. データ投入（`load_csv.py` 等）
3. クエリ実行（`test_queries.py --mode stress --rounds 10`）
4. サーバー停止
5. `visualize_3d.py --compare` で Before/After を比較
6. サーバー再起動 → 追加クエリ → 停止 → 再可視化 → 星の移動・色変化を観察

## ヒント

- 24,000 ノード全件はブラウザが重い場合がある。`--sample 3000` で間引く
- `--method pca`（既定）は速い、`--method umap` は遅いが局所構造を保存
- ホバーで質量・温度・スペクトル型・変位量が表示される

→ 関連: [Architecture — Gravity Model](Architecture-Gravity-Model.md) — 何が可視化されているか
