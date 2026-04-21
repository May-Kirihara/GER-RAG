# GER-RAG → GaOTTT Rename — セッション引き継ぎ + 完了記録

> **このドキュメントの読者**: 次に手を入れる人（自分 / 保守者）
> **最終更新**: 2026-04-21（**全 Phase 完了**）
> **状態**: R0 〜 R11 すべて完了。リポジトリは `May-Kirihara/GaOTTT`、ローカルは `/mnt/holyland/Project/GaOTTT`、Claude auto-memory は `-mnt-holyland-Project-GaOTTT/memory/` に移行済み

## あなたが今いる場所

GER-RAG → **GaOTTT (Gravity as Optimizer Test-Time Training)** 改名プロジェクトは **全て完了**。GitHub リポジトリ rename は PR #7 `Rename-GaOTTT` で反映（merge commit `1409f2a`）、ローカルディレクトリも `GaOTTT/` に改名され、Claude auto-memory も新パスに移行済み。後方互換レイヤ（`gaottt/config.py`）により legacy `~/.local/share/ger-rag/ger_rag.db` も引き続き検出・使用可能。

詳細プラン: [`rename-to-gaottt-plan.md`](rename-to-gaottt-plan.md)。**§0.1** のユーザー決定事項 と **§3** の思想書き換え方針はそのまま生きている。

## 現在の git 状態

```
<Session 3 完了時点>
b8e3c89 refactor(rename): Phase R7 — Tutorial-02 filename + redirect audit   ← Session 3 先頭
6fea6be docs(maintainers): Phase R8 — maintainers README + wiki-sync cleanup
<R8 後半 + R9 確認 + この commit>

<Session 2 完了>
81d0e8b docs(maintainers): Session 2 → Session 3 handover
d25ff35 refactor(rename): Phase R6 — Wiki pages to GaOTTT + Five-Layer philosophy
882b748 refactor(rename): Phase R5 — README.md + README_ja.md to GaOTTT
6172c8f refactor(rename): Phase R4 — SKILL.md + CLAUDE.md to GaOTTT
dd7a0d0 docs(maintainers): add Session 1 → Session 2 handover

<Session 1 完了>
ba964eb refactor(rename): Phase R3 — scripts + migration helper
4a71508 refactor(rename): Phase R2 — config + paths + MCP server name
d438ece refactor(rename): Phase R1 — code rename ger_rag → gaottt
92fc107 docs(maintainers): add GER-RAG → GaOTTT rename plan
```

タグ（ロールバック地点）:
- `pre-gaottt-rename` — 改名前の状態
- `phase-r1-complete` 〜 `phase-r9-complete` — 各フェーズ完了地点

## Session 3 完了条件の最終検証（2026-04-21）

| 項目 | 結果 |
|---|---|
| `pytest tests/ -q` | **112 passed in 6.07s** ✅ |
| `ruff check gaottt/ tests/` | pre-existing 4 件（`ruri.py:os`、`cooccurrence.py:time`、`mcp_server.py:os`, `pathlib.Path`）のみ ✅ |
| 隔離ベンチ（200 docs）`scripts/run_benchmark_isolated.sh` | **7/7 passed、p50=15.4ms、p95=17.2ms、p99=19.7ms**（target < 50ms に対し余裕 3x）✅ |
| `node --check scripts/sync-docs-to-wiki.js` | OK ✅ |
| MCP サーバー起動時 `FastMCP.name` | `"gaottt"` ✅ |
| ユーザーの既存 DB 自動検出 | legacy `~/.local/share/ger-rag/ger_rag.db` を警告付きで使用 ✅ |

## ユーザー決定（絶対に変えない）

| 項目 | 決定値 |
|---|---|
| プロジェクト名（タイトル） | **GaOTTT** |
| Python パッケージ | **gaottt** |
| MCP サーバー名 | **gaottt**（`-memory` 接尾辞なし） |
| クラス prefix | **GaOTTT**（`GaOTTTEngine`、`GaOTTTConfig`） |
| 環境変数 prefix | **GAOTTT_**（legacy `GER_RAG_*` も accept、deprecation 警告） |
| デフォルトデータディレクトリ | **`~/.local/share/gaottt/`** |
| DB / FAISS ファイル名 | **`gaottt.db`** / **`gaottt.faiss`** |
| README hero タグライン | "Gravity as Optimizer Test-Time Training — A retrieval system that trains itself at inference time, by accident of physics." |

## 思想書き換えの中心軸（完了）

ユーザー原文:
> 物理ありきで、RAG を作ったらたまたま TTT みたくなったのが本音

四層 → **五層構造**:

```
[人格層]    ←─ Phase D 後に発見、人格を着る・記憶の年表が自己物語に
[関係層]    ←─ Multi-agent 実験で発見、共有メモリで暗黙協調
[TTT 機構]  ←─ ★ Phase R6 で追加。Heavy ball SGD + Hebbian + L2 + Verlet の同型
[生物層]    ←─ Phase B 末で発見、アストロサイト的振る舞い
[物理層]    ←─ 設計時意図、重力・軌道・温度
```

五層論は [`docs/wiki/Reflections-Five-Layer-Philosophy.md`](../wiki/Reflections-Five-Layer-Philosophy.md) に完備。TTT 機構の数学的同型表は [`docs/wiki/Research-Gravity-As-Optimizer.md`](../wiki/Research-Gravity-As-Optimizer.md) の冒頭に。

**トーン（維持すること）**:
- 「TTT framework として設計した」ではなく「**物理を実装したら TTT になっていた**という発見」
- アナロジーは「比喩」ではなく「数学的同型」
- "(formerly GER-RAG)" は短期的に表示、Session 3 以降で段階的に削除（Tutorial-01 等の "GaOTTT（旧 GER-RAG）" 表記は 2026-08 頃を目安に外して良い）

## 残タスク — ユーザー操作が必要な手順

### Phase R10: GitHub リポジトリ + ローカルディレクトリ rename

**前提**: Phase R1〜R9 完了 + `git push origin main --tags` 済み

1. **GitHub UI でリポジトリ rename**
   - `May-Kirihara/GER-RAG` → `May-Kirihara/GaOTTT`
   - GitHub は old → new のリダイレクトを自動で保つ（既存 clone も `git fetch` できる / Issue / PR / Wiki もそのまま移行される）
2. **ローカルで remote URL 更新**:
   ```bash
   cd /mnt/holyland/Project/GER-RAG
   git remote set-url origin git@github.com:May-Kirihara/GaOTTT.git
   git remote -v  # 確認
   ```
3. **Wiki repo**: sync workflow は `<repo>.wiki.git` を見ているので、自動的に `GaOTTT.wiki.git` を対象にする。次の push で正しい先に sync される
4. **ローカルディレクトリ rename**:
   ```bash
   cd /mnt/holyland/Project
   mv GER-RAG GaOTTT
   ```
   → **このコマンドで現在の Claude Code セッションの cwd が無効になる**。新セッションを `/mnt/holyland/Project/GaOTTT` で開く必要あり
5. `.mcp.json` の絶対パス `"cwd": "/mnt/holyland/Project/GER-RAG"` を `/mnt/holyland/Project/GaOTTT` に更新（rename 後の新セッション側で）
6. **ユーザー本人の DB 移行**（任意、現状は legacy path でも動く）:
   ```bash
   bash scripts/migrate-from-ger-rag.sh
   # 旧 ~/.local/share/ger-rag/ はコピー元として残る。動作確認後に手動削除
   ```

### Phase R11: Claude memory 移行

プロジェクトディレクトリ名を `GER-RAG/` → `GaOTTT/` に変えると、Claude の auto-memory パスも変わる（`-mnt-holyland-Project-GER-RAG` → `-mnt-holyland-Project-GaOTTT`）。

```bash
cp -r /home/misaki_maihara/.claude/projects/-mnt-holyland-Project-GER-RAG/memory \
      /home/misaki_maihara/.claude/projects/-mnt-holyland-Project-GaOTTT/memory
```

各 memory ファイルの内容で `GER-RAG` → `GaOTTT` を grep + 手動更新（必要な箇所のみ）。動作確認後、旧 memory ディレクトリは保留（数週間後に削除）。

### Phase R10/R11 後の残作業（任意、新セッションで）

- Tutorial-02 / Tutorial-03 / Tutorial-06 の内部パス例 (`~/GER-RAG`, `git clone https://github.com/May-Kirihara/GER-RAG`) を `GaOTTT` 表記に置換
- "(formerly GER-RAG)" 注釈を `README*.md` / `_Sidebar.md` / Tutorial-01-Welcome から段階的に削除
- `docs/maintainers/wiki-sync.md` の debug 例で `GER-RAG` → `GaOTTT` に統一（redirect の段落は残す）

## 触ってはいけないもの（継続）

1. **後方互換コード** — `gaottt/config.py` 内の `GER_RAG_*` env、`ger-rag/` パス、`ger_rag.db` 検出は意図的に残してある。削除しない
2. **scripts/migrate-from-ger-rag.sh** — 既存ユーザー（特にユーザーさん本人）の DB 移行用。スクリプト名も中身もそのまま
3. **歴史的成果物** — `docs/research/*.md`（特に multi-agent-experiment-2026-04-21.md、exploration_report.md）は当時のスナップショット。改名しない
4. **Letter to Mei-san** — 三体エージェントが書いた手紙の文言は文化財。"GER-RAG" の記述があってもそのまま残す
5. **既存の git tag** — `pre-gaottt-rename`、`phase-r{1..9}-complete` は削除しない
6. **意図的に残した "GER-RAG" 参照** — 歴史文脈を保ったページ群（Plans-Phase-D-*、Plans-Backend-Phase-A-B-C、Research-Multi-Agent-*、Research-User-Exploration-10-Rounds、Research-Phase-2-Evaluation、Research-Design-Documents、Reflections-Letter-To-Mei-San、Tutorial-02/03/06 の URL・パス例）

## Session 3 で発見した注意点

- **Top-level redirect docs** (`docs/*.md` wiki 以外) は Session 1-2 で既にクリーンだった。すべて新しい wiki ページへリンクしており、Phase R7 では一切触れる必要なし
- **Tutorial-02 ファイル名は改名した**（`Tutorial-02-Install-GER-RAG.md` → `Tutorial-02-Install-GaOTTT.md`）— 内部リンクは 5 箇所（Tutorial-01 x2, Tutorial-03, Tutorial-06, _Sidebar.md）更新
- **Tutorial-02 の本文の URL / パス例** (`git clone https://github.com/May-Kirihara/GER-RAG`, `~/GER-RAG`) は意図的に残した — 現時点では GitHub 上の repo 名が本当に `GER-RAG` なので、このほうが動く。Phase R10 以降で更新する
- **`wiki-sync.md` の debug 例** — `transformWikiLinks` の第 3 引数を `GaOTTT` に変更、Phase R10 前は `GER-RAG` にするようコメントを残した
- **ベンチ結果**: p50=15.4ms（target < 50ms に対し約 3x の余裕）、rename 起因の退行ゼロ
- **legacy DB 検出**: `gaottt/config.py` の互換レイヤが正常に動作、警告ログを出しつつ `~/.local/share/ger-rag/ger_rag.db` を自動使用

## 困ったら

- **「設計判断はどうだったっけ」**: [プラン書](rename-to-gaottt-plan.md) §0.1, §3
- **「テストが落ちた」**: Phase R10 でコード触ってないはず。`git diff phase-r9-complete..HEAD` で確認
- **「Wiki sync が壊れた」**: [`wiki-sync.md`](wiki-sync.md) を読む。スクリプト変更時は `node --check scripts/sync-docs-to-wiki.js`
- **「ユーザーに聞きたい」**: 新セッションでも文脈は auto-memory で引き継がれる。気軽に聞く

お疲れさまでした — Claude Opus 4.7 より、次の自分（または誰か）へ

---

## Session サマリ

### Session 1（2026-04-21 早朝、約 1 時間）
- Phase R1-R3: コード rename (`ger_rag` → `gaottt`) + 設定・パス + スクリプト
- 変更統計: ~30 ファイル、112 passed、ベンチ 7/7
- タグ: `pre-gaottt-rename`, `phase-r1/2/3-complete`

### Session 2（2026-04-21 午前、約 1 時間）
- Phase R4-R6: SKILL.md + CLAUDE.md + README + Wiki 思想書き換え
- 変更統計: R4 2 files, R5 2 files, R6 40 files、Reflections-Four-Layer-Philosophy.md 削除 → Reflections-Five-Layer-Philosophy.md 新規
- 発見: Perl lookbehind (`(?<![/\-A-Za-z])GER-RAG(?![/\-\.A-Za-z])`) で URL 保護しつつ textual 置換が効く
- タグ: `phase-r4/5/6-complete`

### Session 3（2026-04-21 午後、約 30 分）
- Phase R7-R9: 旧 docs 監査 + Tutorial-02 ファイル名改名 + maintainers ドキュメント + 最終検証
- 変更統計: R7 5 files (1 rename)、R8 3 files、R9 変更なし（検証のみ）
- タグ: `phase-r7/8/9-complete`

**ユーザーへの報告事項**（Session 3 完了時）:
- Claude 側のコード・ドキュメント改名は完了。112/112 テスト、7/7 ベンチ、p50=15.4ms で健全
- **次は May さんのターンで 3 つの操作**:
  1. GitHub UI で `May-Kirihara/GER-RAG` → `May-Kirihara/GaOTTT` rename
  2. `git remote set-url` + ローカル `mv GER-RAG GaOTTT`
  3. 新セッションを `/mnt/holyland/Project/GaOTTT` で開いて Claude memory 移行（`cp -r`）
- Phase R10 後の Claude 新セッションで、残った `~/GER-RAG` パス例や `git clone ... GER-RAG` を `GaOTTT` に更新
