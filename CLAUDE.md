# GaOTTT Development Guidelines

*(formerly GER-RAG — 改名プロジェクト Session 2 進行中、`/mnt/holyland/Project/GER-RAG/` のパスは Phase R10 まで残る)*

Last updated: 2026-04-21 (Phase R4 — SKILL/CLAUDE rename)

GaOTTT = **Gravity as Optimizer, Test-Time Training**。物理として設計した重力・軌道力学の更新則が、たまたま Heavy ball SGD + Hebbian gradient + L2 の Verlet 積分と数学的に同型だと判明した。つまり **物理の衣を着た TTT オプティマイザ**。その上に共有時のアストロサイト的協調と Phase D の人格保存基盤が積み上がっている。詳細思想は [`docs/wiki/Reflections-Five-Layer-Philosophy.md`](docs/wiki/Reflections-Five-Layer-Philosophy.md)（物理 → 生物 → TTT 機構 → 関係 → 人格の五層）。

## Tech Stack

Python 3.11+（推奨 3.12）/ FastAPI + uvicorn / aiosqlite + msgpack / FAISS IndexFlatIP / sentence-transformers (RURI-v3-310m) / numpy / pydantic v2 / pytest + pytest-asyncio / **uv**（pip 禁止）

## Project Layout

```text
gaottt/                         # メインパッケージ（formerly ger_rag/）
├── config.py                   # 全ハイパーパラメータ + 後方互換レイヤ（GER_RAG_* env / legacy path）
├── core/                       # engine / gravity / scorer / extractor / clustering / collision / prefetch / types
├── embedding/ruri.py           # キャッシュ自動検出
├── index/faiss_index.py        # 境界チェック付き search
├── store/                      # base / sqlite_store (自動マイグレーション) / cache (write-behind + evict_node)
├── graph/cooccurrence.py       # 無向共起グラフ
└── server/                     # app.py (REST) + mcp_server.py (MCP "gaottt", 25 ツール)
tests/{unit,integration}/       # 112 ケース、すべて asyncio
scripts/                        # load_csv / load_files / test_queries / visualize_3d / benchmark / run_benchmark_isolated.sh / eval_* / migrate-from-ger-rag.sh
docs/                           # wiki/ が SoT、その他 docs/*.md は redirect、docs/research/ は研究アーカイブ
SKILL.md                        # MCP がランタイムで読む（英語、name: gaottt）
.claude/skills/gaottt/SKILL.md  # ★ 上記の同期コピー（必ず両方更新）
CLAUDE.md                       # このファイル
```

## Source-of-Truth マップ — どこを編集するか

| 種別 | SoT | 注意点 |
|---|---|---|
| MCP プロトコル定義 | `SKILL.md` + `.claude/skills/gaottt/SKILL.md` | **両方を必ず同期**（cp で OK） |
| Claude Code 指示 | `CLAUDE.md`（このファイル） | 簡潔に保つ（毎セッション読まれる） |
| GitHub フロントページ | `README.md` / `README_ja.md` | trim 済み、詳細は Wiki リンク |
| 設計判断・アーキテクチャ・実装計画 | **`docs/wiki/*.md`** | 旧 `docs/*.md` は redirect 済 |
| 運用ガイド・トラブルシュート | **`docs/wiki/Operations-*.md`** | |
| ハイパーパラメータ表 | **`docs/wiki/Operations-Tuning.md`** | コードは `gaottt/config.py` |
| MCP ツールリファレンス | **`docs/wiki/MCP-Reference-*.md`** | |
| 研究レポート（長文成果物） | `docs/research/*.md` | Wiki Research-* から要約・リンク |
| 全体 Wiki ナビ | `docs/wiki/_Sidebar.md` + `docs/wiki/Home.md` | 新ページ追加時は両方更新 |

## 実装フロー — 新機能を追加するとき

1. **計画書を確認** — `docs/wiki/Plans-Roadmap.md` で全体像、関連 Plans-* で詳細
2. **TaskCreate でタスク分割** — 実装/テスト/ドキュメント/ベンチに分ける
3. **実装**（順序）:
   - `core/types.py` に Pydantic モデル追加
   - `store/sqlite_store.py` でスキーマ追加 + 自動マイグレーション (`ALTER TABLE ADD COLUMN ... DEFAULT`)
   - `store/base.py` に abstract method 追加
   - `core/engine.py` でロジック実装、destructive op は `prefetch_cache.invalidate()` を忘れない
   - `core/<feature>.py` を新設（純粋関数 or 独立クラス）
   - `server/mcp_server.py` で MCP ツール公開、既存ツールのシグネチャは破壊しない（オプショナル引数のみ追加）
   - `server/mcp_server.py` の `instructions=` 文字列を更新
4. **テスト**:
   - `tests/unit/test_<feature>.py` — 純粋関数
   - `tests/integration/test_engine_<feature>.py` — engine 経由（StubEmbedder 使用、`tests/integration/test_engine_archive_ttl.py` を参照）
   - `tests/integration/test_mcp_phase_d.py` の形式で MCP round-trip
5. **検証**:
   ```bash
   /mnt/holyland/Project/GER-RAG/.venv/bin/python -m pytest tests/ -q
   ruff check gaottt/ tests/
   ```
6. **ベンチマーク（破壊的変更や hot path 触ったとき）**:
   ```bash
   rm -rf /tmp/gaottt-bench
   .venv/bin/bash scripts/run_benchmark_isolated.sh 200
   # 本番 DB は触らない。p50 < 50ms を必達
   ```
7. **ドキュメント更新**（後述のチェックリスト）

## ドキュメント更新フロー

実装が一段落したら、以下を **すべて** 更新:

1. **SKILL.md** — 新ツール追加なら必ず（+ `.claude/skills/gaottt/SKILL.md` に `cp` で同期）
2. **`docs/wiki/MCP-Reference-*`** — 該当カテゴリページに完全な API 仕様を追加
3. **`docs/wiki/MCP-Reference-Index.md`** — 全ツール表に行を追加、ツール選択フローも更新
4. **`docs/wiki/Plans-Roadmap.md`** — Phase ロードマップに完了マーク
5. **`docs/wiki/Architecture-Overview.md`** の「設計判断の記録」表 — 新しい設計判断を追加
6. **`docs/wiki/Operations-Tuning.md`** — 新ハイパラがあれば追加
7. **`docs/wiki/Operations-Troubleshooting.md`** — 想定される問題があれば追加
8. **`README.md` / `README_ja.md`** — 「四層構造表」「カテゴリ表」が変わるならここも（ほとんどは Wiki リンクで済む）
9. **`docs/wiki/_Sidebar.md` + `Home.md`** — 新規 Wiki ページを追加した場合は **必須**（自動追加されない）
10. **`CLAUDE.md`**（このファイル）— 重要な workflow 変更があったら
11. push 前に `node --check scripts/sync-docs-to-wiki.js` でスクリプト構文チェック（変更したとき）

### ドキュメント書きの原則

- **Wiki が SoT**。`docs/*.md`（wiki 以外）は redirect のまま放置 OK
- **三層語彙**（物理 → TTT 機構 → 生物）を保つ（[Plans — SKILL.md Improvement](docs/wiki/Plans-SKILL-MD-Improvement.md) 参照）。TTT 機構は「物理と生物を繋ぐ数学的同型」として位置付ける（比喩ではない）
- **個人的な感動・読者への招待** は歓迎（Reflections セクション、または README の "A Note from Claude"）
- 物理アナロジーが新概念にあれば必ず命名する（Hawking radiation、Lagrange point 等）

## Wiki sync ワークフロー

`docs/wiki/*.md` を編集すると、push 後に **GitHub Action が自動で wiki repo に sync** する。詳細は [`docs/maintainers/wiki-sync.md`](docs/maintainers/wiki-sync.md)（保守者専用）。要点だけ:

- **編集は必ず `docs/wiki/*.md`** で行う（Wiki UI で直接編集しない、上書きされる）
- **新ページを追加したら `docs/wiki/_Sidebar.md` を必ず更新** — 手動キュレーション方式
- リンク変換は自動: `[X](Foo.md)` → `[X](Foo)`、`../path` → 絶対 GitHub URL
- `_Sidebar.md` はカテゴリ + 絵文字つきで作り込んである。編集時は構造を保つ
- **新ページの命名規約**: `<Section>-<PageName>.md`（ハイフン区切り）。例: `Operations-Tuning.md`, `Architecture-Storage-And-Schema.md`
- ページを **削除/リネーム** したら、`_Sidebar.md` と他ページ内のリンクも `grep -rl '<old>.md' docs/wiki/` で発見して更新
- Wiki にだけ置きたいページ（ドラフト等）は `[private]<Name>` で命名 → スクリプトが削除対象から除外
- 手順詳細・ローカルでの変換確認コマンド・トラブルシュート全て [`docs/maintainers/wiki-sync.md`](docs/maintainers/wiki-sync.md) に集約

## テスト・ベンチ・lint コマンド早見表

```bash
# 単体 + 統合テスト
/mnt/holyland/Project/GER-RAG/.venv/bin/python -m pytest tests/ -q

# 特定ファイル
.venv/bin/python -m pytest tests/integration/test_mcp_phase_d.py -x -v

# Lint（pre-existing 4 件は無視 OK: ruri.py の os, cooccurrence.py の time, mcp_server.py の os と pathlib.Path）
ruff check gaottt/ tests/

# 隔離ベンチ（本番 DB 不可触）
rm -rf /tmp/gaottt-bench
.venv/bin/bash scripts/run_benchmark_isolated.sh 200

# MCP サーバー単体テスト起動（手動確認）
.venv/bin/python -m gaottt.server.mcp_server
```

## マルチプロセス / 共有 DB の罠

GaOTTT の DB は **複数 MCP プロセスから共有される** ことがある（複数エージェント、ユーザーの並行ターミナル等）:

- SQLite WAL + `PRAGMA busy_timeout = 30000`（30 秒待機）で並行 write 安全
- ただし **各プロセスは独自の cache + FAISS index** を持つ — プロセス A の `remember` は B には即時反映されない（B の cache reload まで stale）
- `faiss_index.search` は境界チェック付き（`.ids` 破損対策）
- Destructive op（`forget`/`merge`/`compact` 等）は **必ず `prefetch_cache.invalidate()` を呼ぶ**
- ベンチマークは絶対に本番 DB を触らない → `scripts/run_benchmark_isolated.sh` を使う

## よくある罠

- **テスト fixture でランダム埋め込みを使うと flaky** — `tests/integration/test_engine_archive_ttl.py:StubEmbedder` のようにトークンベースの決定論的埋め込みを使う
- **新しい source / edge_type 追加時は `KNOWN_EDGE_TYPES` (`core/types.py`) に追加** — そうしないと relate のドキュメント整合性が壊れる
- **新スキーマ列は必ず `DEFAULT` 付き** — 既存 DB が起動できなくなる
- **MCP ツールの新引数は必ずオプショナル** — 既存呼び出し元を壊さない
- **タスク管理系（`commit`/`complete`/`abandon`）は `_save_memory` ヘルパーを使う** — 直接 `engine.index_documents()` を叩かない
- **`inherit_persona` は明示的に declared された value/intention/commitment のみ集める** — 普通の `remember` は対象外
- **`SKILL.md` を編集したら必ず `.claude/skills/gaottt/SKILL.md` にも `cp`** — 両方が同期されている必要

## 主要参照ポインタ

- 全体ナビ: [`docs/wiki/Home.md`](docs/wiki/Home.md)
- ロードマップ: [`docs/wiki/Plans-Roadmap.md`](docs/wiki/Plans-Roadmap.md)
- アーキテクチャ全体: [`docs/wiki/Architecture-Overview.md`](docs/wiki/Architecture-Overview.md)
- 全 25 MCP ツール: [`docs/wiki/MCP-Reference-Index.md`](docs/wiki/MCP-Reference-Index.md)
- ハイパラチューニング: [`docs/wiki/Operations-Tuning.md`](docs/wiki/Operations-Tuning.md)
- トラブルシュート: [`docs/wiki/Operations-Troubleshooting.md`](docs/wiki/Operations-Troubleshooting.md)
- 哲学（五層論）: [`docs/wiki/Reflections-Five-Layer-Philosophy.md`](docs/wiki/Reflections-Five-Layer-Philosophy.md)

<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->
