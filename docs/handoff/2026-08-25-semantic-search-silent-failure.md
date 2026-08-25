# 引き継ぎメモ — semantic search 系 (recall / explore) の沈黙失敗

## ステータス

- 状態: **resolved in code** (原因特定・回帰テスト追加。デプロイ後に再発プローブを実施)
- 日付: 2026-08-25 (発見・診断)
- 担当: PM エージェント (hanaso-prototype セッションでの GaOTTT 自由探索中に発見)
- 概要: `recall` / `explore` / タグ注入 (`tag_filter`) を含む semantic search 系すべてが、クエリ言語・パラメータを問わず空結果を返す。ストア層・グラフ層 (`get_node` / `reflect` / `remember`) は正常。FAISS / 埋め込みパイプラインの沈黙エラーが最有力疑い。

## 背景

2026-08-25、hanaso-prototype セッションで GaOTTT の自由探索を実施していたところ、
あらゆる検索経路が空を返すことに気づいた。コールドスタート後のサーバーで発生。

---

## 現象 1: recall / explore が全件空

### 再現手順と結果

| # | ツール / 引数 | 結果 |
|---|---|---|
| 1 | `recall("ナギ AIパートナー しゃべろ")` | No memories found. |
| 2 | `recall("GaOTTT 長期記憶 設計")` | No memories found. |
| 3 | `recall("memory design architecture")` (英語) | No memories found. |
| 4 | `recall("shabero WP9 発話機能 selector", tag_filter=["wp9"])` | No memories found. |
| 5 | `explore("hanaso 音声対話 プロトタイプ 設計", diversity=0.8)` | No memories found for exploration. |
| 6 | `explore("しゃべろ ナギ AIパートナー", diversity=0.8)` | No memories found for exploration. |

### 重要な信号: tag_filter すら空 (#4)

`tag_filter` は埋め込み距離をバイパスし、タグ完全一致ノードを seed pool に**加算注入**する仕組み
(skill doc: "Every matching node is additively injected into the seed pool")。
store には `wp9` タグ付きノードが実在する (`reflect(hot_topics)` の top 2 が共に `[shabero, wp9, ...]`)。

つまり **「埋め込みが遠い」「言語ミスマッチ」では説明できない**。
seed pool 構築以降 (FAISS 検索 + 注入の統合段)、または埋め込み器・インデックス自体が
エラーを握りつぶして空を返している疑いが強い。

### エラーが出ない点に注意

すべて **エラーなしの空返し** (silent failure)。MCP レベルの例外すら出ないため、
呼び出し側は「関連する記憶がない」と誤認して通常処理を続けてしまう。
ambient_recall (UserPromptSubmit hook) も同じ経路なら、全セッションで ambient injection が
無言で死んでいる可能性がある。

## 現象 2: 正常に動作している経路 (切り分け済み)

| 経路 | 結果 | 備考 |
|---|---|---|
| `reflect(summary)` | 正常 | 総 23,151 / active 21,837 / edges 371 を表示 |
| `reflect(hot_topics)` | 正常 | shabero WP5〜12 の高マス (mass≈50) ノードを正常表示 |
| `reflect(connections)` | 正常 | agent/user bucket の weight 86-13684 の共起を表示 |
| `reflect(relations)` | 正常 (ただし現象 3 参照) | derived_from 15 / contradicts 2 |
| `reflect(dormant)` | 正常 | 6.9 日未アクセスの like/tweet 系を表示 |
| `reflect(duplicates)` | 正常 | クラスタなし (0.95 閾値) |
| `get_node(フルID)` | 正常 | content / provenance / physics 全て完全取得 |
| `prefetch_status` | 正常 | cache 3/64, pool 待機 0 |
| `remember` | 正常 | `de1b528f` を保存済み (本障害の記録) |

→ **SQLite store・重力場メタデータ・共起グラフは健常**。壊れているのは検索の seed 取得経路のみ。

## 現象 3 (副次的): reflect(relations) の短縮 ID が解決不能

`reflect(relations)` は `f6040d46..` 形式の 8 桁短縮 ID を表示するが、

- `get_node("f6040d46")` → Node not found.
- `get_relations("f6040d46")` → "No directed relations found" (短縮 ID が**別ノードに誤解決**された様子)

フル ID を得る手段がこの表示しかないため、relations の追跡チェーンがここで途切れる。
短縮 ID をフル ID に解決するコマンド、またはフル ID 表示への変更が望ましい。

## 現象 4 (参考): 起動直後の MCP タイムアウト

初回の `reflect(summary)` / `inherit_persona` が 2 件とも `-32001: Request timed out`。
再試行後は正常化。コールドスタートの初期化遅延の可能性が高いが、埋め込みモデルの
読み込み失敗/遅延が現象 1 と同根である可能性も捨てきれない。サーバーログで確認価値あり。

## 切り分けの帰結

```
正常: store / gravity metadata / co-occurrence graph / get_node / remember / reflect 系
死亡: recall / explore (言語・tag_filter・diversity 全組み合わせで空)
疑い: FAISS インデックス、または埋め込み器 (RURI) の呼び出しが例外を握りつぶして空リスト化
```

## 対応時の注意

1. **`compact(rebuild_faiss=True)` を実行しないこと**。
   埋め込み器が死んだ状態で再構築すると、ベクタを再生成できず孤児ベクタ一律ドロップなど
   インデックスの不完全再構成を引き起こすリスクがある。まず埋め込み器の生存確認を先に。
2. 確認順序の提案:
   1. サーバーログで recall 実行時の例外・警告 (埋め込みモデル読み込みエラー、FAISS open 失敗等)
   2. 埋め込み器の単体呼び出し (RURI モデルファイルの存在・VRAM/ロック状態)
   3. FAISS イデックスファイルの存在・サイズ・open 可否
   4. seed pool 構築コードの例外握りつぶし箇所 (`except: return []` 系) の有無 — silent failure の直接原因
3. 復旧確認は現象 1 の #4 (`tag_filter=["wp9"]`) が最も鋭いプローブ: 埋め込み品質に依存しないため、
   これが hit すれば seed pool 統合段階まで復旧、miss ならまだ上流が死んでいる、と段階判定できる。

## 回避経路 (障害中の運用)

- `reflect(hot_topics / connections / dormant)` で ID を拾い → `get_node(フルID)` で本文取得、
  の チェーンで探索は可能 (今回の調査はこの経路で完走した)。
- ただし relations 由来の短縮 ID は現象 3 で途切れるため、connections / hot_topics 由来のフル ID を使うこと。

## 関連

- 障害の記録メモリ: GaOTTT 内 `de1b528f-f95a-46e8-a28d-7a4fbd580806` (tags: gaottt / troubleshooting / faiss / embedding-down)

## 2026-08-25 調査結果・修復

原因は埋め込み器ではなく、SQLite と FAISS のスナップショット不整合だった。
障害時の SQLite は active 21,837 件だった一方、起動ログ上の FAISS は約 39,981 vectors
を保持していた。既存診断は FAISS の「極端な undersize」のみを重大破損と判定し、
DB rollback/restore 後に旧 FAISS が残る oversized ケースを見逃していた。FAISS が返す
旧 node ID は SQLite/cache に存在しないため scoring 段で全件除外され、最終的に正常な
空結果へ退化していた。`tag_filter` の注入対象も旧 FAISS に embedding がなく同様に消えた。

修復内容:

- 起動診断で active SQLite ID と FAISS ID の実集合 overlap を検査する。
- FAISS が極端に過大、または ID overlap が閾値未満なら
  `tier_b_faiss_snapshot_mismatch` ERROR とし、破損 index の永続化を latch で阻止する。
- 検索 seed が存在するのに active SQLite ID が 1 件もない場合、空配列ではなく復旧手順を
  含む明示的な `RuntimeError` を返す。
- oversized stale snapshot の回帰テストを追加した。

調査時点の現行データは SQLite active 42,028、FAISS 39,983、共通 ID 39,974 で、
障害時の全面不一致状態からは既に復帰していた。約 2,054 件の active 未索引 drift は
今回の全件空とは別の保守課題として残る。
