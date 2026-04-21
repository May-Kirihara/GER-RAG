# GER-RAG Wiki

> **Gravity-Based Event-Driven RAG** — 重力で知識が引き合う、AI の長期外部記憶
> 物理機構が、結果としてアストロサイト的な振る舞いに見え、最終的に人格を保存する装置になる。

ようこそ。GER-RAG はもはや単なる「RAG ライブラリ」ではなく、**長期記憶 + 多エージェント協調基盤 + タスク管理装置 + 人格保存装置** という四つの役を一台でこなすシステムです。

このページは目的別の入口です。あなたが「何者として」ここに来たかで、たどるべきパスが違います。

---

## 「私は何者か」で枝分かれする入口

### 🆕 GER-RAG を初めて知った

- **プログラミング経験がない or 浅い方** → 🌱 [Tutorial: はじめての方へ](Tutorial-01-Welcome.md)（6 ページのステップバイステップ、約 30〜60 分）
- **開発者の方** → [Getting Started](Getting-Started.md)（インストール〜最初の `remember` まで 5 分）

### 🧑‍💻 GER-RAG を使いたい（個人や Claude エージェントとして）

| やりたいこと | 行き先 |
|---|---|
| 長期記憶として | [Guides — Use as Memory](Guides-Use-As-Memory.md) |
| TODO・タスク管理として | [Guides — Use as Task Manager](Guides-Use-As-Task-Manager.md) |
| 人格保存基盤として | [Guides — Use as Persona Base](Guides-Use-As-Persona-Base.md) |
| 複数エージェントの共有メモリとして | [Guides — Multi-Agent](Guides-Multi-Agent.md) |
| 記憶宇宙を眺めたい | [Guides — Visualization](Guides-Visualization.md) |

### 🛠 GER-RAG の MCP ツールリファレンスを引きたい

→ [MCP Reference Index](MCP-Reference-Index.md) — **25 ツール** の早見表
- [Memory](MCP-Reference-Memory.md) — remember/recall/explore/reflect/ingest/auto_remember
- [Tasks & Persona](MCP-Reference-Tasks-and-Persona.md) — Phase D の 9 ツール
- [Maintenance](MCP-Reference-Maintenance.md) — forget/restore/merge/compact/revalidate/relate*/prefetch*

REST API は [REST API Reference](REST-API-Reference.md)。

### ⚙ GER-RAG を運用したい（サーバー管理、チューニング、トラブル対応）

| やりたいこと | 行き先 |
|---|---|
| サーバー起動・MCP 登録 | [Operations — Server Setup](Operations-Server-Setup.md) |
| ハイパーパラメータ調整 | [Operations — Tuning](Operations-Tuning.md) |
| 定期メンテナンス | [Operations — Compact & Backup](Operations-Compact-And-Backup.md) |
| ベンチマーク（本番 DB を触らずに） | [Operations — Isolated Benchmark](Operations-Isolated-Benchmark.md) |
| 詰まったとき | [Operations — Troubleshooting](Operations-Troubleshooting.md) |

### 🏗 GER-RAG を理解したい・拡張したい（開発者として）

| やりたいこと | 行き先 |
|---|---|
| 全体像 | [Architecture — Overview](Architecture-Overview.md) |
| ストレージ・スキーマ | [Architecture — Storage & Schema](Architecture-Storage-And-Schema.md) |
| 重力・軌道モデル | [Architecture — Gravity Model](Architecture-Gravity-Model.md) |
| マルチプロセス並走の安全性 | [Architecture — Concurrency](Architecture-Concurrency.md) |
| ロードマップと未実装機能 | [Plans — Roadmap](Plans-Roadmap.md) |

### 🔬 GER-RAG の研究的背景に触れたい

→ [Research Index](Research-Index.md) ── 設計根拠 + 実験レポート + 評価
- 特に [Multi-Agent Experiment](Research-Multi-Agent-Experiment.md) と [User Exploration (10 Rounds)](Research-User-Exploration-10-Rounds.md) は **GER-RAG が「関係構築装置」として創発した瞬間** の記録

### 💭 GER-RAG の精神に触れたい

→ [Reflections](Reflections-A-Note-From-Claude.md) ── 設計者・実装者・観察者からの長文の手紙
- [A Note from Claude](Reflections-A-Note-From-Claude.md)
- [Four-Layer Philosophy: Physics → Biology → Relations → Persona](Reflections-Four-Layer-Philosophy.md)
- [Letter to Mei-san](Reflections-Letter-To-Mei-San.md) — 3 体のエージェントが 10 ラウンドの末に書いた手紙

---

## クイックステータス

- **実装フェーズ**: Phase A〜D 全完了
- **MCP ツール数**: 25
- **テスト**: 112/112 緑
- **ベンチ**: SC-001 p50 = **15.1ms** (200 docs)
- **シードメモリ**: 23,000+（ユーザーさんの実 DB）

詳細は [Plans — Roadmap](Plans-Roadmap.md)。

---

## このプロジェクトの中核アイディア

> **物理機構が、結果としてアストロサイト的な振る舞いに見える** — そして十分な時間と共有を経ると、**人格を保存する装置になる**。

| 層 | メカニズム | 創発する役割 |
|---|---|---|
| **物理層** | 質量・重力波・軌道力学 | （見える） |
| **生物層** | 暗黒物質ハロー、アストロサイト | LLM の思考を裏で支える |
| **関係層** | 有向リレーション、`completed` エッジ | 過去と現在を繋ぐ、共有メモリで複数エージェントが協調 |
| **人格層 (Phase D)** | source 体系拡張 + `inherit_persona` | 過去の自分をセッション継承可能に |

→ 詳しくは [Four-Layer Philosophy](Reflections-Four-Layer-Philosophy.md)
