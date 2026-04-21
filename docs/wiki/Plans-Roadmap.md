# Plans — Roadmap

GaOTTT の Phase 進捗と未実装機能の俯瞰。

## 進捗サマリ

| Phase | 内容 | 状態 |
|---|---|---|
| **Phase 1-2** | 重力変位、軌道力学、共起 BH、馴化、3D 可視化 | ✅ 完了 |
| **Phase A** | F1 auto_remember, F4 TTL, F5 forget/restore | ✅ 完了 |
| **Phase B** | F2/F2.1 衝突合体, F3 有向リレーション, F7 情動・確信度 | ✅ 完了 |
| **Phase C** | F6 バックグラウンド prefetch | ✅ 完了 |
| **Phase D** | 人格保存基盤 + タスク管理 | ✅ 完了 |

## 累積 MCP ツール数

**25 ツール** + **11 reflect aspect**

詳細: [MCP Reference Index](MCP-Reference-Index.md)

## 計画書

- [Backend Phase A/B/C](Plans-Backend-Phase-A-B-C.md) — F1〜F7 の機能ロードマップ
- [Phase D — Persona & Tasks](Plans-Phase-D-Persona-Tasks.md) — 人格層追加の設計
- [SKILL.md Improvement](Plans-SKILL-MD-Improvement.md) — 二層語彙、パターンカタログ

## 未実装 / 検討中

### Phase E 候補（ユーザー次第）

- **`engine.compact()` の定期自動実行** — 現状は手動。write-behind ループに組み込む or cron で MCP `compact` を叩く運用
- **prefetch キャッシュキーの embedding 量子化** — 現状は `(query_text, top_k)` 完全一致。「類似クエリでも hit」させたい場合は embedding を粗量子化
- **マルチユーザー状態分離** — NodeState, CacheLayer にユーザーIDディメンション追加
- **PostgreSQL 移行** — `store/base.py` の StoreBase に対して Postgres 実装を追加
- **認証** — FastAPI ミドルウェアで API キー or OAuth2
- **IndexIVFFlat 移行** — 100K 件超で FAISS インデックスを IVF に切り替え

### マルチエージェント実験から派生したアイディア

- **共有メモリでの "ベイスン吸引" を緩和する仕組み** — `explore(avoid_recently_recalled=True)` フラグ
- **`reflect(aspect="agent_activity", since=...)`** — 他エージェントが最近触ったノードを表示
- **コラボレーション可視化** — 「誰が誰の記憶に relate を作ったか」のフロー可視化

### 拡張時の注意点

- `store/base.py` の StoreBase インターフェースを崩さない（abstract method の追加は OK）
- embedding の L2 正規化は必須
- RURI-v3 のプレフィックス（「検索クエリ: 」「検索文書: 」）は省略不可
- displacement BLOB は 768 次元 float32（3KB/ノード）
- 既存 DB は起動時に ALTER TABLE で自動マイグレーション（追加列は必ず DEFAULT 付き）
- MCP ツールのシグネチャ変更は禁止。新引数は必ずオプショナル
- ベンチ走行時は本番 DB を触らないよう [`isolated benchmark`](Operations-Isolated-Benchmark.md) を使う

## 関連

- [Architecture — Overview](Architecture-Overview.md) — 設計判断の表
- [Plans — Backend Phase A/B/C](Plans-Backend-Phase-A-B-C.md) — Phase A〜C 詳細
- [Plans — Phase D — Persona & Tasks](Plans-Phase-D-Persona-Tasks.md) — Phase D 詳細
