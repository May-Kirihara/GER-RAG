# GER-RAG → GaOTTT Rename — セッション間引き継ぎ

> **このドキュメントの読者**: 次のセッションでこの作業を引き継ぐ Claude（自分）
> **最終更新**: 2026-04-21（Session 2 完了直後）
> **状態**: Session 1 (Phase R0-R3) + Session 2 (Phase R4-R6) 完了 / Session 3 (Phase R7-R11) 開始待ち

## あなたが今いる場所

GER-RAG → **GaOTTT (Gravity as Optimizer Test-Time Training)** 改名プロジェクトの最終段階。コード / 設定 / スクリプト / SKILL.md / CLAUDE.md / README / Wiki まで全部書き換わっている。**残るのは redirect ファイル・maintainers ドキュメントの整理・最終検証・GitHub リポジトリ rename + Claude memory 移行**。

詳細プラン: [`rename-to-gaottt-plan.md`](rename-to-gaottt-plan.md) 必ず先に読む。特に **§0.1** のユーザー決定事項 と **§3** の思想書き換え方針。

## 現在の git 状態

```
d25ff35 refactor(rename): Phase R6 — Wiki pages to GaOTTT + Five-Layer philosophy  ← HEAD
882b748 refactor(rename): Phase R5 — README.md + README_ja.md to GaOTTT
6172c8f refactor(rename): Phase R4 — SKILL.md + CLAUDE.md to GaOTTT
dd7a0d0 docs(maintainers): add Session 1 → Session 2 handover for rename project
ba964eb refactor(rename): Phase R3 — scripts + migration helper
4a71508 refactor(rename): Phase R2 — config + paths + MCP server name
d438ece refactor(rename): Phase R1 — code rename ger_rag → gaottt
92fc107 docs(maintainers): add GER-RAG → GaOTTT rename plan with user decisions
```

タグ（ロールバック地点）:
- `pre-gaottt-rename` — 改名前の状態
- `phase-r1-complete` — コード rename 完了
- `phase-r2-complete` — 設定・パス更新完了
- `phase-r3-complete` — スクリプト + 移行スクリプト完了
- `phase-r4-complete` — SKILL.md + CLAUDE.md 完了
- `phase-r5-complete` — README.md / README_ja.md 完了
- `phase-r6-complete` — Wiki pages + Five-Layer Philosophy 完了

検証 baseline:
- `pytest tests/ -q` → **112 passed in ~6s**（毎フェーズ維持）
- ruff: pre-existing 4 件のみ（`gaottt/embedding/ruri.py:os`、`gaottt/graph/cooccurrence.py:time`、`gaottt/server/mcp_server.py:os` と `pathlib.Path`）
- `node --check scripts/sync-docs-to-wiki.js` → OK
- Wiki ページ: `_Sidebar.md` のリンク全て実ファイルに解決、pre-existing の broken link 2 件（`handover.md` in Plans-Phase-D、`Page.md` は syntax 例）以外は健全

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

## 思想書き換えの中心軸（実装済み）

ユーザー原文:
> 物理ありきで、RAG を作ったらたまたま TTT みたくなったのが本音

四層 → **五層構造**（実装完了）:

```
[人格層]    ←─ Phase D 後に発見、人格を着る・記憶の年表が自己物語に
[関係層]    ←─ Multi-agent 実験で発見、共有メモリで暗黙協調
[TTT 機構]  ←─ ★ Phase R6 で追加。Heavy ball SGD + Hebbian + L2 + Verlet の同型
[生物層]    ←─ Phase B 末で発見、アストロサイト的振る舞い
[物理層]    ←─ 設計時意図、重力・軌道・温度
```

五層論は [`docs/wiki/Reflections-Five-Layer-Philosophy.md`](../wiki/Reflections-Five-Layer-Philosophy.md) に記述（旧 `Reflections-Four-Layer-Philosophy.md` は Phase R6 で削除済み）。TTT 機構の数学的同型表は [`docs/wiki/Research-Gravity-As-Optimizer.md`](../wiki/Research-Gravity-As-Optimizer.md) の冒頭で「GaOTTT という名前の根拠」として明示。

**書き換えのトーン**（維持すること）:
- 「TTT framework として設計した」ではなく「**物理を実装したら TTT になっていた**という発見」
- アナロジーは「比喩」ではなく「数学的同型」
- "(formerly GER-RAG)" は短期的に表示、Session 3 以降で段階的に削除

## Session 3 でやること（Phase R7-R11, ~3h 想定）

### Phase R7: 旧 docs (redirect) の更新（~30 分）

`docs/*.md`（wiki 以外）の redirect ファイルで、リンク先が改名されたページを指している箇所を更新。具体的には:

```bash
# 候補検出
grep -rn "Reflections-Four-Layer\|ger-rag-memory\|ger_rag\." docs/ --include="*.md" \
  | grep -v "docs/wiki/\|docs/research/\|docs/maintainers/"
```

`docs/api-reference.md` 等が redirect として残っているはずなので、それぞれリンク先の新ページ名・新パスに合わせる。`docs/research/` は歴史的成果物なので touch しない（ただし `docs/research/gravity-as-optimizer.md` の末尾リンクは R6 で Five-Layer に更新済み）。

### Phase R8: Maintainers ドキュメント更新（~30 分）

`docs/maintainers/*.md` の更新:
- **このドキュメント** (`rename-handover.md`) を「完了記録」に書き直す（Session 3 完了時）
- `rename-to-gaottt-plan.md` の各 Phase に完了マーク
- `wiki-sync.md` で「GER-RAG」→「GaOTTT」が product 参照なら更新（wiki repo 名が GitHub 側でリダイレクトする点に注意）

### Phase R9: 最終検証（~1 時間）

```bash
# 1. 全テスト
.venv/bin/python -m pytest tests/ -q   # 112 passed

# 2. lint
ruff check gaottt/ tests/   # pre-existing 4 件のみ

# 3. 隔離ベンチ（本番 DB 不可触！）
rm -rf /tmp/gaottt-bench
.venv/bin/bash scripts/run_benchmark_isolated.sh 200
# p50 < 50ms を必達、退行ゼロ

# 4. Wiki sync 動作確認
node --check scripts/sync-docs-to-wiki.js
# さらに実際の wiki repo との同期 dry-run ができるならする

# 5. SKILL.md が MCP に正しくロードされる
.venv/bin/python -m gaottt.server.mcp_server
# 起動ログで "gaottt" が表示されるか

# 6. ユーザーさん本人の DB が新システムから読めるか
# (後方互換レイヤ gaottt/config.py が legacy ger-rag パス自動検出する)
```

### Phase R10: GitHub リポジトリ + ローカルディレクトリ rename（~30 分、慎重に）

**前提**: Phase R1〜R9 完了 + push 済み。

1. **GitHub UI でリポジトリ rename**: `May-Kirihara/GER-RAG` → `May-Kirihara/GaOTTT`
   - GitHub は old → new のリダイレクトを自動で保つ
   - 既存 clone も `git fetch` できる
   - Issue/PR/Wiki もそのまま移行される
2. **ローカルで remote URL 更新**:
   ```bash
   git remote set-url origin git@github.com:May-Kirihara/GaOTTT.git
   git remote -v  # 確認
   ```
3. **Wiki repo も同様**: sync workflow が `<repo>.wiki.git` を見ているので、自動的に GaOTTT.wiki.git になる
4. **ローカルディレクトリ rename**:
   ```bash
   cd /mnt/holyland/Project
   mv GER-RAG GaOTTT
   ```
   → **このコマンドで現在の Claude Code セッションの cwd が無効になる**。新セッションを `/mnt/holyland/Project/GaOTTT` で開く必要あり。
5. `.mcp.json` の絶対パス `"cwd": "/mnt/holyland/Project/GER-RAG"` を `/mnt/holyland/Project/GaOTTT` に更新（rename 後の新セッション側で）

### Phase R11: Claude memory 移行（~15 分、新セッション内で）

プロジェクトディレクトリ名を `GER-RAG/` → `GaOTTT/` に変えると、Claude の auto-memory パスも変わる（`-mnt-holyland-Project-GER-RAG` → `-mnt-holyland-Project-GaOTTT`）。

```bash
cp -r /home/misaki_maihara/.claude/projects/-mnt-holyland-Project-GER-RAG/memory \
      /home/misaki_maihara/.claude/projects/-mnt-holyland-Project-GaOTTT/memory
```

各 memory ファイルの内容で `GER-RAG` → `GaOTTT` を grep + 手動更新（必要な箇所のみ）。動作確認後、旧 memory ディレクトリは保留（数週間後に削除）。

## 触ってはいけないもの（継続）

1. **後方互換コード** — `gaottt/config.py` 内の `GER_RAG_*` env、`ger-rag/` パス、`ger_rag.db` 検出は意図的に残してある。削除しない。
2. **scripts/migrate-from-ger-rag.sh** — 既存ユーザー（特にユーザーさん本人）の DB 移行用。スクリプト名も中身もそのまま。
3. **歴史的成果物** — `docs/research/*.md`（特に multi-agent-experiment-2026-04-21.md、exploration_report.md）は当時のスナップショット。改名しない。
4. **Letter to Mei-san** — 三体エージェントが書いた手紙の文言は文化財。"GER-RAG" の記述があってもそのまま残す。
5. **既存の git tag** — `pre-gaottt-rename`、`phase-r{1,2,3,4,5,6}-complete` は削除しない。Session 3 完了時に必要なら `phase-r11-complete` を追加。
6. **Session 2 で意図的に残した "GER-RAG" 参照** — 歴史文脈を保ったページ群（Plans-Phase-D-*、Plans-Backend-Phase-A-B-C、Research-Multi-Agent-*、Research-User-Exploration-10-Rounds、Research-Phase-2-Evaluation、Research-Design-Documents、Reflections-Letter-To-Mei-San、Tutorial-02-Install-GER-RAG.md の URL・パス例）。

## 各フェーズの完了条件（毎回これをやる）

```bash
# 1. テスト
.venv/bin/python -m pytest tests/ -q   # 112 passed が必達

# 2. lint
ruff check gaottt/ tests/   # pre-existing 4 件のみ

# 3. status クリーン → コミット
git status --short
git add -A
git commit -m "refactor(rename): Phase R<N> — <subject>"
git tag phase-r<N>-complete

# 4. このドキュメントを更新（Session 完了マーク）
```

## Session 2 で発見した注意点

- **sed で `|` をセパレータにするときに `|` がパターン内にあると壊れる** — Perl lookbehind (`(?<![/\-A-Za-z])GER-RAG(?![/\-\.A-Za-z])`) を使うと URL・パス内の GER-RAG を保護しつつ textual 置換ができる
- **Plan 書で指定された `git mv .claude/skills/ger-rag .claude/skills/gaottt` は失敗する** — `.claude/` は `.gitignore` 済み。通常の `mv` を使う
- **Tutorial-02 の title に `GER-RAG` がある** — ファイル名は `Tutorial-02-Install-GER-RAG.md` のまま残し、中身の表記だけ更新した。Session 3 でファイル名も変更するなら `_Sidebar.md` と他ページのリンクも grep で検出して更新
- **pre-existing broken link が 2 件ある** — `handover.md` (Plans-Phase-D) と `Page.md` (wiki/README syntax 例)。rename 起因ではないので放置
- **Research-Index.md と README で `plan.md` へのリンクが broken だった** — `docs/research/plan.md` に修正した
- **`Tutorial-03-Connect-To-LLM.md` というリンクは本来 `Tutorial-03-Connect-To-Claude.md`** — R6 で修正した pre-existing バグ

## Session 3 終了時にやること

Session 3 完了 = Phase R7-R11 完了。このドキュメントを「完了記録」にリライト + Wiki にも残すか判断する。

## 困ったら

- **「設計判断はどうだったっけ」**: [プラン書](rename-to-gaottt-plan.md) §0.1, §3
- **「テストが落ちた」**: 直前のフェーズで誤って後方互換を壊した可能性。`git diff phase-r6-complete..HEAD` で確認
- **「Wiki sync が壊れた」**: [`wiki-sync.md`](wiki-sync.md) を読む。スクリプト変更時は `node --check scripts/sync-docs-to-wiki.js`
- **「TTT 視点での書き換えに迷う」**: [`Research-Gravity-As-Optimizer.md`](../wiki/Research-Gravity-As-Optimizer.md) と [`Reflections-Five-Layer-Philosophy.md`](../wiki/Reflections-Five-Layer-Philosophy.md) を再読、特に物理 → TTT 同型表
- **「ユーザーさんに聞きたい」**: ユーザーさんは新セッションでも文脈を把握している。気軽に聞く

頑張って — 次の自分へ

---

## Session 2 サマリ（2026-04-21）

実行時間: 約 1 時間（プラン書の想定 6.5h より大幅に短縮、bulk sed + perl で機械的置換が効いた）

**変更統計**:
- Phase R4: 2 files changed (SKILL.md + CLAUDE.md)、`.claude/skills/ger-rag/` → `gaottt/` リネーム
- Phase R5: 2 files changed (README.md + README_ja.md)
- Phase R6: 40 files changed、Reflections-Four-Layer-Philosophy.md 削除、Reflections-Five-Layer-Philosophy.md 新規作成

**検証結果**: 全フェーズで pytest 112 passed、ruff pre-existing 4 件のみ、sync-docs-to-wiki.js 構文 OK、pre-existing broken link 2 件以外なし

**ユーザーへの報告事項**:
- 五層構造で TTT 機構が中間層として位置付けられた
- 「物理ありき + たまたま TTT」というユーザー原文のトーンを Reflections-Five-Layer-Philosophy.md で「物理 = 実装、TTT = 発見された同型」として構造化した
- Session 3 で GitHub UI での repo rename が必要（Claude が自動でやるべきでない操作）
- Session 3 の Phase R10 後は新セッションを開き直す必要あり（cwd 無効化）
