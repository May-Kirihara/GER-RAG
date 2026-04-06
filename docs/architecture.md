# アーキテクチャ概要

## システム構成

GER-RAGは**二重座標系 + 軌道力学**による創発的検索システムである。原始embedding空間は不変のまま、共起した文書同士が**万有引力で引き寄せ合い、速度を持って仮想座標空間を周回する**。使い込むほど知識の星々が自己組織化し、意外なつながりが発見される。

```
クエリ入力
  │
  ▼
[RURI-v3 Embedding] ──→ クエリベクトル (768次元, L2正規化済)
  │                      ※キャッシュ済みならローカルから即座にロード
  ▼
[重力波伝播] ──→ seed top-k=3 → 再帰的近傍展開 (mass依存top-k、重力半径カットオフ)
  │               → N ノード到達 (シミュレーション層)
  ▼
[仮想座標でのスコアリング]
  │  virtual_pos = normalize(original_emb + displacement)
  │  gravity_sim = dot(query, virtual_pos)
  │  final = gravity_sim * decay + mass_boost + wave_boost
  │
  ├─ プレゼンテーション層: top-k=5 をLLMに返却
  └─ シミュレーション層: 全到達ノードの物理更新
  │
  ▼
[軌道力学] ──→ Stage 1: 加速度 = 近傍引力 + アンカー復元力
  │            Stage 2: 速度更新 (慣性 + 摩擦)
  │            Stage 3: 位置更新 (displacement += velocity)
  │            + mass増加, temperature再計算, 共起グラフ更新
  ▼
[Write-behind] ──→ 非同期でSQLiteにフラッシュ (displacement + velocity)
```

## 二重座標系

```
┌──────────────────────────────────────────────────┐
│  原始embedding空間 (immutable)                    │
│  ・RURI-v3の出力そのまま、FAISSに格納              │
│  ・広い候補取得 (top-K * 3) に使用                 │
│  ・変更されない（意味的保証を維持）                  │
└──────────────────────────────────────────────────┘
                    ↓ 候補取得
┌──────────────────────────────────────────────────┐
│  仮想座標空間 (mutable, gravity-driven)            │
│  ・virtual_pos = normalize(original + displacement)│
│  ・共起した文書同士が重力で引き寄せ合う              │
│  ・massが大きいノードほど強い引力                    │
│  ・最終ランキングの類似度計算に使用                   │
│  ・クエリのたびに更新される                          │
└──────────────────────────────────────────────────┘
```

## 重力モデル

3段階の物理ステップ（加速度→速度→位置）で軌道力学を実現する。

```
Stage 1 - 加速度:
  a_neighbors = Σ [ G * m_j / (r² + ε) * direction(i→j) ]           # 近傍引力
  a_anchor    = -k * displacement_i                                   # 原始位置への復元力
  a_bh        = G * bh_mass / r² * dir(→centroid) * escape_factor     # 共起BH引力
  a_total     = a_neighbors + a_anchor + a_bh

Stage 2 - 速度:
  v += a_total * dt
  v *= (1 - friction)      # 摩擦
  v = clamp(v, max_vel)    # 速度上限

Stage 3 - 位置:
  displacement += v * dt
  displacement = clamp(displacement, max_norm)  # 位置上限
```

### 重力半径

各ノードの質量から重力圏の広さが決まる。重力圏外の近傍は引き込まない。

```
min_sim = 1 - G * mass / (2 * a_min)    # 実際の重力物理から導出
```

mass=1 → min_sim=0.95（ごく近傍のみ）、mass=10 → min_sim=0.50（中距離）、mass=50 → min_sim=0.05（広大な重力圏）

### アンカー引力（脱出防止）

各ノードの原始embedding位置が重力アンカーとして機能する（Hooke's law: F = -k * displacement）。近傍引力で加速したノードが外に飛び出しても、アンカーが引き戻す。

### 共起ブラックホール（銀河形成）

共起エッジのクラスタ重心に仮想的なブラックホール（超大質量引力源）が形成される。

```
bh_mass = scale * log(1 + Σ edge_weight * saturation(neighbor))
centroid = Σ (weight * saturation * position_j) / Σ (weight * saturation)
a_bh = G * bh_mass / r² * direction(→centroid) * escape_factor
```

- 頻繁に共起するノード群が「銀河」として重力的に束縛される
- edge_decayでエッジが減衰すればBHも弱まり、銀河が解散する（忘却）

### 返却飽和（Habituation）

LLMに同じ文書を繰り返し返すほど、その文書の影響力が減衰する。脳の馴化と同じメカニズム。

```
saturation = 1 / (1 + return_count * saturation_rate)
final_score *= saturation
bh_edge_weight *= saturation(neighbor)
```

- return_countはLLMへの返却時のみインクリメント（シミュレーション層は影響しない）
- habituation_recovery_rateで時間とともに回復（脱馴化）
- 「もう知っている知識」のスコアとBH寄与が下がり、未知の知識が浮上する

### 温度ベースBH脱出（Thermal Escape）

高temperatureのノード（多様な文脈で検索される）はBHの束縛を振り切る。

```
escape_factor = 1 / (1 + temperature * thermal_escape_scale)
a_bh *= escape_factor
```

ホットプラズマが銀河から脱出するのと同じ物理。低温ノードはクラスタに留まり、高温ノードは自由飛行して新しいクラスタに合流する。

### 自然発生する軌道

- **公転**: 異なる方向からの引力が合成 → 角運動量 → ハブ周囲を周回
- **彗星**: 一時的共起で加速 → 別クラスタ通過 → スイングバイ → セレンディピティ
- **落下**: 摩擦で減速 → ハブに吸収 → 安定した関連文書に
- **静止**: 未アクセスノードは摩擦で停止 → 原始位置に留まる
- **馴化脱出**: 同じ結果が繰り返されるとスコア低下 → 新しいノードが浮上

## モジュール構成

```
ger_rag/
├── config.py               # GERConfig: 全ハイパーパラメータ（重力パラメータ含む）
├── core/
│   ├── engine.py           # GEREngine: wave探索 + 二層分離 + 軌道力学
│   ├── gravity.py          # 軌道力学（加速度, 速度, wave伝播, アンカー, 重力半径）
│   ├── scorer.py           # スコアリング関数群 (純粋関数)
│   └── types.py            # Pydanticモデル (リクエスト/レスポンス/内部型)
├── embedding/
│   └── ruri.py             # RuriEmbedder: RURI-v3ラッパー (ローカルキャッシュ自動検出)
├── index/
│   └── faiss_index.py      # FaissIndex: FAISS IndexFlatIPラッパー + ベクトル逆引き
├── store/
│   ├── base.py             # StoreBase: 抽象ストアインターフェース
│   ├── sqlite_store.py     # SqliteStore: SQLite WAL実装 (displacement永続化含む)
│   └── cache.py            # CacheLayer: インメモリキャッシュ + displacement + write-behind
├── graph/
│   └── cooccurrence.py     # CooccurrenceGraph: 共起グラフ管理
└── server/
    └── app.py              # FastAPIアプリ + lifespan管理
```

## データフロー

### インデックス時

1. `POST /index` でドキュメント受信
2. content SHA-256ハッシュで重複チェック（重複はembedding生成前にスキップ）
3. RURI-v3で embedding生成 (「検索文書: 」プレフィックス付き)
4. L2正規化後、FAISSインデックスに追加
5. NodeState初期化 (mass=1.0, temperature=0.0, displacement=zero)
6. SQLiteにドキュメント + 状態を永続化

### クエリ時

1. `POST /query` でクエリ受信
2. RURI-v3でクエリembedding生成 (「検索クエリ: 」プレフィックス付き)
3. FAISS検索で広い候補取得 (top-K × candidate_multiplier)
4. 各候補の仮想座標を計算 (original_emb + displacement)
5. 仮想座標でのcosine similarity × decay + mass_boostで最終スコア
6. 負スコアを除外、top-Kに絞って返却
7. **返却後**に重力更新: 共起ペアにforce適用 → displacement蓄積
8. mass, temperature, 共起グラフも更新
9. ダーティ状態（displacement含む）はwrite-behindで非同期にSQLiteへフラッシュ

## ストレージ戦略

```
起動時:   SQLite → インメモリキャッシュ（node状態 + displacement）にロード + FAISSインデックスロード
稼働時:   キャッシュから読み取り → 変更はdirtyセットに記録
定期的:   write-behindタスクがdirty状態をバッチでSQLiteにフラッシュ (5秒間隔)
停止時:   write-behind停止 → 全dirty状態フラッシュ → FAISSインデックス保存 → 接続クローズ
異常終了: フラッシュされていないdirty状態は消失 (ドキュメント・embeddingは保全)
```

### SQLiteスキーマ

- `documents`: content_hash (SHA-256) + UNIQUEインデックスで重複防止
- `nodes`: displacement BLOB カラム（768次元 float32、3KB/ノード）
- 既存DBは起動時に `ALTER TABLE nodes ADD COLUMN displacement BLOB` で自動マイグレーション

## 並行性モデル

- **シングルインスタンス**前提
- ノード状態更新は**last-write-wins**（ロックなし）
- SQLite WALモードで読み書き並行可能
- write-behindは単一asyncioタスクで実行

## Embeddingモデルのキャッシュ

`RuriEmbedder`は起動時にHuggingFaceキャッシュ（`~/.cache/huggingface/`）を検査し、モデルがキャッシュ済みであれば`local_files_only=True`でロードする。オフライン環境でも動作。

## Cosmic 3D可視化

`scripts/visualize_3d.py`はFAISSインデックスとSQLiteから直接データを読み取り、原始座標と仮想座標（重力変位後）をインタラクティブなPlotly HTMLとして出力する。

宇宙空間に浮かぶ恒星としてドキュメントを表現：

| 視覚要素 | 対応する動的状態 | 恒星アナロジー |
|---------|---------------|--------------|
| サイズ | Mass (質量) | 赤色巨星（大きい）vs 矮星（小さい） |
| 色温度 | Temperature | M赤 → K橙 → G黄 → F白 → A/B青白 |
| 明るさ | Decay × Mass | 最近アクセスされた高質量ノードが最も明るい |
| フィラメント | 共起エッジ | 宇宙の大規模構造 |
| `--compare`モード | 原始 vs 仮想座標の並列比較 | 重力による星団の再配置を観察 |
