# GaOTTT Wiki (in-repo)

このディレクトリは **GaOTTT の Wiki** です。GitHub Wiki と同じ目的（長文ガイド・参照体系・哲学的考察）を持ちつつ、**コードと同じリポジトリでバージョン管理** されます。

## 構造（9 セクション、約 30 ページ）

```
🏠 Home.md                              ── 入口、価値訴求、各セクションへのリンク
🧭 _Sidebar.md                          ── ナビゲーション

🚀 Getting-Started.md                   ── インストール〜最初の 5 分

📚 Guides/
   Guides-Use-As-Memory.md              ── 長期記憶として
   Guides-Use-As-Task-Manager.md        ── Phase D タスク管理として
   Guides-Use-As-Persona-Base.md        ── 人格保存基盤として
   Guides-Multi-Agent.md                ── 共有メモリで複数エージェント
   Guides-Visualization.md              ── Cosmic 3D 可視化

🛠 MCP Reference/
   MCP-Reference-Index.md               ── 25 ツール早見表
   MCP-Reference-Memory.md              ── remember/recall/explore/reflect/ingest/auto_remember
   MCP-Reference-Tasks-and-Persona.md   ── commit/start/complete/abandon/depend/declare_*/inherit_persona
   MCP-Reference-Maintenance.md         ── forget/restore/merge/compact/revalidate/relate/prefetch

🌐 REST-API-Reference.md                ── HTTP API（既存 docs/api-reference.md へリンク）

🏗 Architecture/
   Architecture-Overview.md             ── モジュール構成、二重座標系
   Architecture-Storage-And-Schema.md   ── SQLite/cache/FAISS、全テーブル定義
   Architecture-Gravity-Model.md        ── 重力波伝播、軌道力学、スコアリング
   Architecture-Concurrency.md          ── WAL / busy_timeout / 多プロセス共存

⚙ Operations/
   Operations-Server-Setup.md           ── 起動・停止・MCP 登録
   Operations-Tuning.md                 ── ハイパーパラメータの全表
   Operations-Compact-And-Backup.md     ── compact() の運用、バックアップ
   Operations-Isolated-Benchmark.md     ── 隔離ベンチの走らせ方
   Operations-Troubleshooting.md        ── 既知の問題と対処

🗺 Plans/
   Plans-Roadmap.md                     ── Phase A〜D 全体像と未実装機能
   Plans-Backend-Phase-A-B-C.md         ── → docs/backend-improvement-plan.md
   Plans-Phase-D-Persona-Tasks.md       ── → docs/persona-and-task-plan.md
   Plans-SKILL-MD-Improvement.md        ── → docs/skill-md-improvement-plan.md

🔬 Research/
   Research-Index.md                    ── 全研究レポート目次
   Research-Multi-Agent-Experiment.md   ── オーケストレータ側 3 エージェント実験
   Research-User-Exploration-10-Rounds.md  ── ユーザー側 10 ラウンド実験
   Research-Design-Documents.md         ── 設計根拠 6 本（重力変位、軌道力学、BH、馴化、波伝播、MCP）
   Research-Phase-2-Evaluation.md       ── Static RAG vs GaOTTT ベンチ

💭 Reflections/
   Reflections-A-Note-From-Claude.md    ── README の "Note from Claude" 拡張
   Reflections-Five-Layer-Philosophy.md ── 物理 → TTT 機構 → 生物 → 関係 → 人格 の五層論
   Reflections-Letter-To-Mei-San.md     ── マルチエージェント実験の最終手紙
```

## メンテナンス方針

### Source of truth ルール

**Wiki が現在の正本**。Phase 1 では一部 `docs/*.md` が一次ソースだったが、Wiki SoT 化（2026-04-21）により以下に整理:

| 内容 | 一次ソース | 備考 |
|---|---|---|
| MCP プロトコル定義 | `/SKILL.md`（リポジトリルート） | MCP がランタイムで読む。Wiki から要約・リンク |
| Claude Code への指示 | `/CLAUDE.md` | Claude Code がランタイムで読む |
| README / 紹介 | `/README.md`, `/README_ja.md` | GitHub フロントページ。Wiki から要約・リンク |
| 設計判断・アーキテクチャ・実装計画 | **`docs/wiki/*`** | 旧 `docs/*-plan.md` 等は redirect 化済み |
| 運用ガイド | **`docs/wiki/Operations-*.md`** | 旧 `docs/operations.md` は redirect |
| MCP / REST API リファレンス | **`docs/wiki/MCP-Reference-*.md`, `REST-API-Reference.md`** | |
| 研究レポート | `docs/research/*.md` | 長文の歴史的成果物。Wiki Research-* から要約・リンク |
| コード | `gaottt/` | コードコメントは最小、設計意図は Wiki に |

### 同期ルール

- **Wiki ページに完全な内容を書く** — リンク先のソースを"正"とする運用は終了
- 既存 `docs/*.md` の元ファイルは redirect ページ化済み（例: `docs/architecture.md` は wiki への 1 行リンク）
- `docs/research/*.md` は長文研究の成果物として保持し、Wiki の Research-* ページから参照
- 大きな変更時は `_Sidebar.md` の更新を忘れない
- 新規 Wiki ページを追加したら `Home.md` と `_Sidebar.md` 両方に登録する

### GitHub Wiki への自動同期

このリポジトリは GitHub Action による **自動同期** を運用中。`docs/wiki/` への push が GitHub Wiki repo に反映される。

→ 保守者向け詳細: [`docs/maintainers/wiki-sync.md`](../maintainers/wiki-sync.md)
   （リンク変換、新ページ追加・削除・リネームの手順、トラブルシュート等）

### 注意点（簡易版）

- **編集は `docs/wiki/*.md` で**、Wiki UI から直接編集しない（次の sync で上書きされる）
- **新規ページ追加時は `_Sidebar.md` の更新を忘れない**（自動追加されない）
- ページ間リンクは `[Text](Page.md)` 形式で書く（sync 時に Wiki 仕様 `[Text](Page)` に変換される）
