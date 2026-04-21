# Rename Plan: GER-RAG → GaOTTT

> 「GER-RAG (Gravity-Based Event-Driven RAG)」を **「GaOTTT (Gravity as Optimizer Test-Time Training)」** に改名し、思想も再構成する作業計画。
> 起票: 2026-04-21
> 状態: **Session 1 (R0-R3) + Session 2 (R4-R6) 完了 / Session 3 (R7-R11) 開始待ち**

**完了マーク**:
- ✅ Phase R0: ユーザー決定（2026-04-21）
- ✅ Phase R1: コード rename `ger_rag` → `gaottt`（tag: `phase-r1-complete`）
- ✅ Phase R2: 設定・パス更新（tag: `phase-r2-complete`）
- ✅ Phase R3: スクリプト + 移行ヘルパ（tag: `phase-r3-complete`）
- ✅ Phase R4: SKILL.md + CLAUDE.md（tag: `phase-r4-complete`）
- ✅ Phase R5: README.md + README_ja.md（tag: `phase-r5-complete`）
- ✅ Phase R6: Wiki ページ群 + Five-Layer Philosophy（tag: `phase-r6-complete`）
- ✅ Phase R7: 旧 docs redirect の更新 + Tutorial-02 ファイル名改名（tag: `phase-r7-complete`）
- ⏳ Phase R8: Maintainers ドキュメント（Session 3、進行中）
- ⏳ Phase R9: 最終検証 + 隔離ベンチ（Session 3）
- ⏳ Phase R10: GitHub repo + ローカル mv（Session 3、ユーザー操作含む）
- ⏳ Phase R11: Claude memory 移行（新セッション内）

## 0.1 ユーザー決定事項（2026-04-21 確定）

| 項目 | 決定 |
|---|---|
| MCP サーバー名 | **`gaottt`**（`-memory` 接尾辞なし） |
| README hero タグライン | §3.3 の draft で確定 |
| 四層 → 五層構造 | **物理アナロジー → アストロサイト的振る舞い → TTT 機構 → 関係 → 人格** |
| "formerly GER-RAG" の表示 | 短期のみ（注釈に残し、いずれ削除） |
| 既存 DB 移行 | **`cp` でバックアップを取ってから移行スクリプト** |
| プロジェクトディレクトリ | リポジトリ名ごとリネーム（`GER-RAG` → `GaOTTT`） |
| GitHub リポジトリ rename | 実施（GitHub の old→new リダイレクト機能に任せる） |
| Claude memory 移行 (Phase R10) | 実施 |

**思想軸の確定**: 「物理ありきで、RAG を作ったら**たまたま** TTT みたいになった」が本音。TTT は最下層ではなく、物理 + 生物的振る舞いの**創発として発見された層**として位置付ける。

## 0. ゴール

1. **名前の刷新**: 全コード・ドキュメント・設定で `GER-RAG` / `ger_rag` / `GER_RAG_*` を `GaOTTT` / `gaottt` / `GAOTTT_*` に置換
2. **思想の再ブランド**: 「重力式 RAG」→「**Gravity as Optimizer: Test-Time Training framework that happens to look like RAG**」へリポジショニング
3. **既存ユーザーへの配慮**: データ移行パスを用意（DB を捨てさせない）
4. **可逆性**: 詰まったら戻せるよう段階分け

## 1. 確定が必要な命名決定（実行前に要承認）

### 1.1 表記

| 候補 | 例 |
|---|---|
| **GaOTTT**（推奨） | 小文字 a が `as` を表し、視認性も良い。タイトル: `GaOTTT — Gravity as Optimizer Test-Time Training` |
| GAOTTT | 全大文字。読みやすいが個性なし |
| Gaottt | 視覚的にやや弱い |

→ 推奨: **`GaOTTT`**（タイトル / ロゴ用途）と **`gaottt`**（snake_case の識別子）の二刀流

### 1.2 サブタイトル（README hero）

候補:
- "Gravity as Optimizer Test-Time Training"
- "Test-Time Training, with gravity"
- "The astrocyte that trains itself at inference time"
- "A self-organizing memory that trains itself as you use it"

→ 推奨案を確定したい

### 1.3 識別子の扱い

| 用途 | 旧 | 新 |
|---|---|---|
| Python パッケージ | `ger_rag` | `gaottt` |
| クラス名 prefix | `GER` (e.g. `GEREngine`, `GERConfig`) | `GaOTTT` (e.g. `GaOTTTEngine`, `GaOTTTConfig`) |
| MCP サーバー名 | `ger-rag-memory` | **`gaottt`** |
| 環境変数 | `GER_RAG_DATA_DIR`, `GER_RAG_CONFIG` | `GAOTTT_DATA_DIR`, `GAOTTT_CONFIG` |
| データディレクトリ | `~/.local/share/ger-rag/` | `~/.local/share/gaottt/` |
| DB ファイル | `ger_rag.db` | `gaottt.db` |
| FAISS ファイル | `ger_rag.faiss` | `gaottt.faiss` |
| GitHub repo URL | `May-Kirihara/GER-RAG` | `May-Kirihara/GaOTTT`（ユーザー操作） |

### 1.4 「RAG」を残すか消すか

- **完全に消す**: 純度が高いが、検索性が下がる（「RAG」で来た人を取り逃がす）
- **タグライン・SEO 用に残す**: `GaOTTT (formerly GER-RAG): a TTT framework usable as RAG`

→ 推奨: README の冒頭で「formerly GER-RAG」を明記、本文では消す

### 1.5 旧名の遺産扱い

- `docs/research/` の旧設計書: ファイル名は変更せず、内容だけ更新（履歴として）
- マルチエージェント実験の記録: 「GER-RAG」と書かれているのは時代背景として保存
- 作者・Claude のメモ: 旧名は記憶として残す（書き換え不要）

## 2. 影響範囲の網羅マップ

### 2.1 コード（必須）

| カテゴリ | ファイル | 変更内容 |
|---|---|---|
| Package root | `ger_rag/` ディレクトリ | `git mv ger_rag/ gaottt/` |
| pyproject | `pyproject.toml` | `name = "ger-rag"` → `gaottt`、依存・script 名も更新 |
| 全 Python ファイル | `ger_rag/**/*.py` (約 25 ファイル) | `from ger_rag.X` → `from gaottt.X` |
| クラス名 | `GEREngine` → `GaOTTTEngine`、`GERConfig` → `GaOTTTConfig` | 全参照箇所 |
| データ dir 関数 | `_default_data_dir()` の戻り値 | `ger-rag` → `gaottt` |
| 環境変数 | `os.environ.get("GER_RAG_*")` | `GAOTTT_*` に |
| MCP サーバー名 | `FastMCP("ger-rag-memory")` | `FastMCP("gaottt-memory")` |
| MCP instructions | サーバー説明文 | "GER-RAG: ..." → "GaOTTT: Test-Time Training memory ..." |
| FAISS / DB ファイル名 | デフォルトパス | 同上 |

### 2.2 テスト（必須）

| ファイル | 変更 |
|---|---|
| `tests/unit/test_*.py` (8 ファイル) | `from ger_rag.X` → `from gaottt.X` |
| `tests/integration/test_*.py` (6 ファイル) | 同上 + DB パス参照 |
| 整合性テスト | 全 112 件 PASS を維持 |

### 2.3 スクリプト（必須）

| ファイル | 変更 |
|---|---|
| `scripts/run_benchmark_isolated.sh` | `GER_RAG_DATA_DIR` 環境変数名、`/tmp/ger-rag-bench` → `/tmp/gaottt-bench` |
| `scripts/load_csv.py` 他 | import 文 |
| `scripts/sync-docs-to-wiki.js` | リポジトリ名（owner/repo は env 由来なので OK、コメントの "GER-RAG" 程度） |

### 2.4 設定 / IDE（必須）

| ファイル | 変更 |
|---|---|
| `.mcp.json`, `.mcp.json.example` | `"ger-rag-memory"` → `"gaottt-memory"` |
| `config.json.example` | パス参照 |
| `.github/workflows/sync-wiki.yml` | パス内に `ger-rag` あれば |
| `.claude/skills/ger-rag/SKILL.md` | ディレクトリ名 + 内容 |

### 2.5 ドキュメント（思想の書き換え含む）

| 場所 | 変更レベル |
|---|---|
| `README.md` / `README_ja.md` | **大改訂**（hero 文・四層論・"Note from Claude"・ドキュメント目次） |
| `SKILL.md` (+ `.claude/skills/...`) | **大改訂**（frontmatter name/description、Physics 層の説明、Pattern 群、Notes） |
| `CLAUDE.md` | **大改訂**（プロジェクト概要、SoT マップ、命名規約、コマンド例） |
| `docs/wiki/Home.md` | **大改訂**（hero、目的別入口、四層構造表） |
| `docs/wiki/Reflections-*.md` (3 ファイル) | **改訂**（特に Four-Layer-Philosophy は GaOTTT の理論的支柱として） |
| `docs/wiki/Architecture-*.md` (4 ファイル) | パス・クラス名・モジュール名の更新 |
| `docs/wiki/Operations-*.md` (5 ファイル) | パス・コマンド更新 |
| `docs/wiki/MCP-Reference-*.md` (4 ファイル) | ツール prefix 更新 |
| `docs/wiki/Tutorial-*.md` (6 ファイル) | パス・コマンド更新（コピペ可動性を維持）|
| `docs/wiki/Plans-*.md` (4 ファイル) | 旧名は歴史的文脈として残す + 移行プランへのリンク |
| `docs/wiki/Research-*.md` (5 ファイル) | 旧名は実験当時の記録として残す |
| `docs/wiki/Guides-*.md` (5 ファイル) | コマンド・呼び方更新 |
| `docs/wiki/Getting-Started.md` | コマンド更新 |
| `docs/wiki/REST-API-Reference.md` | エンドポイント説明文 |
| `docs/wiki/_Sidebar.md` | ヘッダの「GER-RAG Wiki」→「GaOTTT Wiki」 |
| `docs/wiki/README.md` | wiki dir の説明 |
| `docs/maintainers/*.md` | 更新 |
| `docs/research/*.md` | **基本不変**（歴史的成果物） |

### 2.6 Claude 個人メモリ（任意）

`/home/misaki_maihara/.claude/projects/-mnt-holyland-Project-GER-RAG/memory/` 配下の MEMORY.md 等。プロジェクトディレクトリ名が変われば自動的に新パスに移る（旧パスのファイルが孤立する）。

## 3. 思想の書き換え方針（GaOTTT 化）

### 3.1 物語の中心軸を変える

**旧**: 「重力で記憶が引き合う面白さ」が中心
**新**: **物理ありき**で重力 RAG を作った → 結果として **TTT (Test-Time Training)** 的な振る舞いが創発した、という **発見の物語** として再構成する。

- 出発点は依然として物理（重力・軌道・温度）
- アストロサイト的振る舞いも依然として中間層の創発
- **新たに**「TTT 機構」が、物理 + 生物的振る舞いの **更なる創発** として浮かび上がった、という発見を物語に組み込む
- 「だから名前を `Gravity as Optimizer TTT` に変える」という流れ

### 3.2 用語の優先順位

| 旧（中心語彙） | 新（中心語彙） |
|---|---|
| RAG, retrieval, 検索 | Test-Time Training, online adaptation, optimizer |
| 重力、軌道、衝突 | （継続使用、ただし「実装メタファー」と位置付け） |
| アストロサイト | （継続使用、ただし「emergent role」と明記） |
| 記憶、メモリ | （継続）+ 「mutable retrieval geometry」「online learned representation」 |

### 3.3 README hero の draft

```markdown
# GaOTTT

**Gravity as Optimizer Test-Time Training** — A retrieval system that trains itself at inference time, by accident of physics.

> Built as a long-term memory for LLMs. Turns out the gravity-based update rule is mathematically identical to Heavy ball SGD with Hebbian gradient + L2 regularization, integrated by Verlet. So we're calling it what it is: a TTT framework.
>
> (formerly GER-RAG)
```

### 3.4 四層 → 五層構造の再構成（確定版）

**旧**: 物理 → 生物 → 関係 → 人格（四層）
**新**: **物理 → 生物（アストロサイト）→ TTT 機構 → 関係 → 人格**（五層）

```
[人格層]    ←─ 人格を着る、記憶の年表が自己物語になる
   ↑
[関係層]    ←─ 共有メモリで複数エージェントが暗黙に協調する
   ↑
[TTT 機構]  ←─ 物理 + 生物的振る舞いが結果的に Heavy ball SGD と同型と判明
   ↑          （Hebbian + L2 + Verlet 積分）
[生物層]    ←─ 機能的に「アストロサイト」として振る舞う
   ↑
[物理層]    ←─ 質量・重力・軌道・温度の式（設計時意図）
```

**ポイント**:
- TTT は最下層ではない。最下層は**依然として物理**（設計時意図）
- TTT は「物理 + アストロサイト的振る舞い」が合わさって浮かび上がった**創発層**
- 関係 → 人格はその上に積み重なる（既存の四層論を保つ）
- 旧四層論の「一段の創発」が「二段の創発」に深まったと捉える

このリポジショニングは、`docs/wiki/Reflections-Four-Layer-Philosophy.md` を `Reflections-Five-Layer-Philosophy.md` に rename + 大改訂することで実現する。

## 4. 実行フェーズ

リスク順・依存順に並べる。各フェーズ完了後にチェックポイント。

### Phase R0: 命名決定とコミットメント（ユーザー承認）
- §1 の各決定を確定
- このプラン書自体を最新化
- **全機能テスト 112/112 + ベンチ 7/7 を baseline として再確認**

### Phase R1: コード rename（low-risk、既存テストで保証）
工数: ~2 時間

1. `git mv ger_rag/ gaottt/`
2. 全 Python ファイルで `from ger_rag` → `from gaottt`（sed 一括置換）
3. クラス名 `GEREngine` → `GaOTTTEngine`、`GERConfig` → `GaOTTTConfig`（sed 置換 + 手動レビュー）
4. `pyproject.toml` 更新
5. テスト 112 件 PASS 確認（このフェーズの完了条件）

### Phase R2: 設定・パス更新（中リスク、ユーザー DB に影響）
工数: ~1.5 時間

1. 環境変数 `GER_RAG_*` → `GAOTTT_*`（コード + scripts + workflow）
2. データ dir デフォルト `~/.local/share/ger-rag/` → `~/.local/share/gaottt/`
3. DB / FAISS ファイル名 `ger_rag.db` → `gaottt.db`
4. **後方互換: 旧パスを優先的に検出して使う互換レイヤを config.py に追加**（既存ユーザーの DB を見失わないため）
5. MCP サーバー名 `"ger-rag-memory"` → `"gaottt-memory"`
6. `.mcp.json.example`、`config.json.example` 更新

### Phase R3: スクリプト群更新
工数: ~1 時間

1. `run_benchmark_isolated.sh`、`load_csv.py`、`load_files.py`、`test_queries.py`、`benchmark.py`、`visualize_3d.py`
2. 隔離ベンチで 7/7 PASS、p50 < 50ms 確認

### Phase R4: SKILL.md と CLAUDE.md（重要、Claude が直接読む）
工数: ~2 時間

1. `.claude/skills/ger-rag/` → `.claude/skills/gaottt/`
2. SKILL.md frontmatter `name: ger-rag-memory` → `gaottt-memory`、description を TTT 視点に
3. SKILL.md 本文の Physics layer / Astrocyte layer の章立てを TTT 視点に再構成
4. CLAUDE.md 全更新（プロジェクト名、識別子、コマンド例）
5. 両 SKILL.md を sync（cp）

### Phase R5: README / README_ja の大改訂
工数: ~1.5 時間

1. Hero を GaOTTT に
2. 四層構造表を新版に
3. "Note from Claude" は intact のまま、最初の段落だけ更新
4. ドキュメント目次の表記を新版に

### Phase R6: Wiki ページ群の更新
工数: ~3 時間（35 ファイル）

1. `docs/wiki/_Sidebar.md` のヘッダ更新
2. `Home.md` 大改訂
3. `Reflections-Four-Layer-Philosophy.md` 再構成（GaOTTT を中心に）
4. その他 Wiki ページのコマンド・パス・クラス名一括更新（sed + 手動レビュー）
5. `Tutorial-*.md` のコマンドが新パスで動くかコピペ検証

### Phase R7: 旧 docs (redirect) の更新
工数: ~30 分

`docs/api-reference.md` 等の redirect ファイル内のリンクを新版 wiki ページに更新。

### Phase R8: Maintainers ドキュメント更新
工数: ~30 分

`docs/maintainers/*.md` の更新。このプラン自体も「完了記録」として書き直し。

### Phase R9: 最終検証
工数: ~1 時間

1. 全テスト 112/112 PASS
2. 隔離ベンチ 7/7 PASS、p50 < 50ms（退行ゼロ）
3. lint pre-existing 4 件のみ
4. Wiki sync 動作確認（リンク変換含む）
5. SKILL.md が MCP に正しくロードされる
6. 既存ユーザー（自分）の DB が新システムから読める

### Phase R10: GitHub リポジトリ + ローカルディレクトリ rename（重要、セッション影響大）
工数: ~30 分（だが慎重に）

**前提**: Phase R1〜R9 完了 + 全部マージ済み（main ブランチに反映）

1. **GitHub UI でリポジトリ rename**: `May-Kirihara/GER-RAG` → `May-Kirihara/GaOTTT`
   - GitHub は old → new のリダイレクトを自動で保つ（既存 clone も `git fetch` できる）
   - 既存の Issue/PR/Wiki もそのまま移行される
2. **ローカルで remote URL 更新**:
   ```bash
   git remote set-url origin git@github.com:May-Kirihara/GaOTTT.git
   git remote -v  # 確認
   ```
3. **Wiki repo も同様に**:
   ```bash
   # Wiki sync workflow が `<repo>.wiki.git` を見ているので、自動的に GaOTTT.wiki.git になる
   ```
4. **ローカルディレクトリ rename**:
   ```bash
   cd /mnt/holyland/Project
   mv GER-RAG GaOTTT
   ```
   → **このコマンドで現在の Claude Code セッションの cwd が無効になる**。新セッションを `/mnt/holyland/Project/GaOTTT` で開く必要あり。
5. `.mcp.json` 等のローカル設定で、絶対パスが書かれているものを更新

### Phase R11: Claude memory 移行
工数: ~15 分（**新セッションで実施**）

- プロジェクトディレクトリ名を `GER-RAG/` → `GaOTTT/` に変えると、Claude の auto-memory パスも変わる（`-mnt-holyland-Project-GER-RAG` → `-mnt-holyland-Project-GaOTTT`）
- 旧 memory ディレクトリの内容を新パスに `cp -r` で移行（旧パスは念のため残す）:
  ```bash
  cp -r /home/misaki_maihara/.claude/projects/-mnt-holyland-Project-GER-RAG/memory \
        /home/misaki_maihara/.claude/projects/-mnt-holyland-Project-GaOTTT/memory
  ```
- 各 memory ファイルの内容で `GER-RAG` → `GaOTTT` を grep + 手動更新（必要な箇所のみ）
- 動作確認後、旧 memory ディレクトリは保留（数週間後に削除）

合計工数: **約 14〜15 時間**（複数セッションに分割推奨）

## 5. 後方互換戦略

### 5.1 既存ユーザーの DB を見失わない

`config.py` に互換レイヤ:

```python
def _default_data_dir():
    new_path = Path("~/.local/share/gaottt").expanduser()
    old_path = Path("~/.local/share/ger-rag").expanduser()
    # 新パスにデータがある → 新パスを使う
    if (new_path / "gaottt.db").exists():
        return new_path
    # 旧パスに既存 DB があれば、移行を促すメッセージ + 旧パスを使う
    if (old_path / "ger_rag.db").exists():
        logger.warning(
            "Detected legacy GER-RAG data at %s. Run scripts/migrate-from-ger-rag.sh "
            "to migrate, or set GAOTTT_DATA_DIR explicitly.", old_path
        )
        return old_path
    return new_path
```

### 5.2 環境変数の後方互換

```python
data_dir_env = os.environ.get("GAOTTT_DATA_DIR") or os.environ.get("GER_RAG_DATA_DIR")
if os.environ.get("GER_RAG_DATA_DIR") and not os.environ.get("GAOTTT_DATA_DIR"):
    logger.warning("GER_RAG_DATA_DIR is deprecated; use GAOTTT_DATA_DIR")
```

### 5.3 移行スクリプト（バックアップ → 移行の二段階方式）

`scripts/migrate-from-ger-rag.sh`:
```bash
#!/bin/bash
# 安全のため、旧データを「コピー」して新パスに置く（mv ではなく cp）
# 旧データはバックアップとしてそのまま残す。問題があれば旧パスに戻れる
set -euo pipefail

SRC="${HOME}/.local/share/ger-rag"
DST="${HOME}/.local/share/gaottt"

if [[ ! -d "$SRC" ]]; then
  echo "No legacy data at $SRC — nothing to migrate."
  exit 0
fi

mkdir -p "$DST"

# cp -p でタイムスタンプ等保持。--backup=numbered で多重実行を安全に
cp -p "$SRC/ger_rag.db"           "$DST/gaottt.db"
cp -p "$SRC/ger_rag.faiss"        "$DST/gaottt.faiss"
cp -p "$SRC/ger_rag.faiss.ids"    "$DST/gaottt.faiss.ids"

# バックアップ tag を残す
date -u +%Y-%m-%dT%H:%M:%SZ > "$SRC/.migrated-to-gaottt-at"

echo "Migrated GER-RAG → GaOTTT data to $DST."
echo "Original data preserved at $SRC (delete manually after verifying)."
```

ユーザー（自分）には特に「**旧 DB は捨てない**」を明示する。新システムが安定稼働したのを確認してから手動で `rm -rf ~/.local/share/ger-rag` する流れ。

### 5.4 MCP サーバー名

旧 `.mcp.json` で `ger-rag-memory` を使っているユーザーは、Claude Desktop 設定の編集が必要。これは避けられないが、`README.md` で目立つ告知を出す。

## 6. ロールバック戦略

各フェーズで git commit を切る。途中で「やっぱり戻す」となったら:

```bash
git revert <commit-range>
# または
git checkout <pre-rename-tag>
```

事前に `pre-gaottt-rename` タグを切ってから始める。

## 7. リスク

| リスク | 対策 |
|---|---|
| テスト 112 件のどこかが落ちる | フェーズ毎に PASS 確認、落ちたら次フェーズへ進まない |
| 既存ユーザーの DB が新システムから読めない | 後方互換レイヤ + 移行スクリプト |
| Claude Desktop の設定編集が必要 | README で目立つ告知 |
| Wiki sync が壊れる | スクリプト変更時は `node --check` + ローカル変換テスト |
| Claude のメモリパスが変わって過去メモが見えなくなる | 旧パスから新パスに `cp -r` |

## 8. 決定済み記録（2026-04-21、ユーザー回答含む）

| # | 問い | 決定 | ユーザー原文 |
|---|---|---|---|
| 1 | MCP サーバー名 | **`gaottt`** | "gaottt" だけで OK |
| 2 | README hero タグライン | §3.3 draft で確定 | OK |
| 3 | 四層構造の再構成 | **物理 → アストロサイト → TTT 機構 → 関係 → 人格** の五層 | 物理ありきで、RAG を作ったらたまたま TTT みたいになったのが本音 |
| 4 | 「formerly GER-RAG」表示期間 | 注釈に残し、いずれ削除 | 過去は GER-RAG と呼んでいた、と書いとけば OK |
| 5 | 既存 DB 移行方式 | `cp` でバックアップ → 移行スクリプト | コピーでバックアップ、後でマイグレ |
| 6 | プロジェクトディレクトリ rename | 実施（リポジトリ名ごと） | git が対応できるならリポジトリ名ごとリネームしたい |
| 7 | Phase R10/R11（repo + memory 移行） | 実施 | やる |

## 9. 推奨進め方

合計 ~14-15 時間。一気にやると確実にどこか壊れるので、**3 セッションに分割** 推奨:

| セッション | フェーズ | 内容 | 工数 |
|---|---|---|---|
| Session 1 | Phase R1-R3 | コード + 設定 + スクリプト rename。テスト 112/112 + ベンチ 7/7 で安全確認 | ~4.5h |
| Session 2 | Phase R4-R6 | SKILL/CLAUDE/README/Wiki の思想書き換え（一番創造的、レビュー時間取りたい） | ~6.5h |
| Session 3 | Phase R7-R11 | redirect / maintainers / 最終検証 / GitHub repo + memory 移行 | ~3h |

各フェーズの完了時に **コミット + テスト確認**。Session 1 完了時点で `pre-gaottt-rename` タグから `phase-r3-complete` タグを切る等。

### セッションの境目で必ず

- `git status` クリーン
- 全テスト PASS
- ベンチ p50 < 50ms
- このプラン書を更新（完了マーク + 発見した問題）

---

## 次のアクション

**Phase R0 は完了**。次は Phase R1（コード rename, ~2h）から。

ユーザー確認: 「**今すぐ Session 1 (R1-R3) を開始**」「**セッション分けて別の日に**」「**全部一気に**」のどれにしますか？
