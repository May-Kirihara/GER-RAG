# Wiki Sync Workflow

`docs/wiki/` を main repo の SoT として編集 → GitHub Action が自動で `<repo>.wiki.git` に push する運用。**保守者向け**（ユーザーには関係ない）。

## 構成

```
docs/wiki/*.md             ← 編集対象（PR レビュー可）
   │
   │ push to main
   ▼
.github/workflows/sync-wiki.yml
   │
   │ runs
   ▼
scripts/sync-docs-to-wiki.js
   │
   │ clone <repo>.wiki.git → transform links → write → commit → push
   ▼
GitHub Wiki（読み手が見る）
```

## スクリプトの動作

[`scripts/sync-docs-to-wiki.js`](../../scripts/sync-docs-to-wiki.js) の主処理:

1. `docs/wiki/*.md` を再帰的にスキャン
2. 各ファイルの内容を **`transformWikiLinks` で Wiki 仕様に変換**:
   - `[X](Foo.md)` → `[X](Foo)` （同階層 `.md` → 拡張子なし Wiki ページ名）
   - `[X](Foo.md#section)` → `[X](Foo#section)` （アンカー保持）
   - `[X](../research/foo.md)` → `[X](https://github.com/<owner>/<repo>/blob/<branch>/docs/research/foo.md)`
   - `[X](../../ger_rag/config.py)` → 絶対 GitHub URL
   - 外部 URL / mailto / 純アンカー / インラインコード / コードフェンス内 → そのまま
3. Wiki repo (`<repo>.wiki.git`) にクローン → 変換後の content を書き込み
4. **`docs/wiki/_Sidebar.md` がソースにあれば尊重**（カテゴリ分け・絵文字を保持）。無ければフラットな自動生成リストにフォールバック
5. `docs/wiki/` に存在しなくなったページを Wiki から削除（保護ページ除く）
6. 差分があれば commit + push

## メンテ作業レシピ

### ✏️ 既存ページを編集する

`docs/wiki/<Page>.md` を直接編集 → コミット → push。Action が走って Wiki に反映される。

### 📄 新しいページを追加する

1. `docs/wiki/Section-NewPage.md` を作る（命名規約: `<Section>-<PageName>.md`、ハイフン区切り）
2. **`docs/wiki/_Sidebar.md` の該当セクションに 1 行追加** ─ 例: `- [New Page](Section-NewPage.md)`
3. （任意）`Home.md` の該当セクションにもリンク追加
4. push

### 🗑️ ページを削除する

1. `docs/wiki/<Page>.md` を `git rm`
2. `docs/wiki/_Sidebar.md` から該当行を削除
3. push → Action が Wiki 側からも削除する

### 🔄 既存ページをリネームする

1. `git mv docs/wiki/Old.md docs/wiki/New.md`
2. `_Sidebar.md`、`Home.md`、他ページ内のリンクを更新（`grep -rl 'Old.md' docs/wiki/` で発見）
3. push

### 🔒 Wiki にだけ存在させたいページを作る（main repo に置きたくない）

- ページ名を `[private]<PageName>` で始める → スクリプトが削除対象から除外する
- または `scripts/sync-docs-to-wiki.js` の `PROTECTED_WIKI_PAGES` 配列に明示的に追加
- 用途例: ドラフト、内部 only メモ、Wiki UI から直接書きたいもの

## ローカルでリンク変換結果を確認したいとき

```bash
node -e "
const { transformWikiLinks } = require('./scripts/sync-docs-to-wiki.js');
const fs = require('fs');
const src = fs.readFileSync('docs/wiki/Home.md', 'utf-8');
console.log(transformWikiLinks(src, 'May-Kirihara', 'GER-RAG', 'main'));
" | less
```

スクリプトを編集したら必ず `node --check scripts/sync-docs-to-wiki.js` で構文チェック。

## サイドバーのカスタマイズ

`docs/wiki/_Sidebar.md` をそのまま編集すれば、カテゴリ・絵文字・順序すべて反映される。スクリプトは上書きしない。

ただし新規ページを `docs/wiki/` に追加した場合、**自動でサイドバーには載らない** ── 手動で `_Sidebar.md` に追加する必要がある（明示的キュレーション方式）。

## Action が動かないとき

1. `.github/workflows/sync-wiki.yml` のトリガー条件を確認（`paths: docs/wiki/**` 等）
2. Wiki repo が初期化されているか確認 ─ GitHub の Settings → Features → Wikis を有効化、最低 1 ページ作る
3. Action のログを確認 ─ `❌ GITHUB_TOKEN ...` 等のエラーが出ていないか
4. 権限 ─ workflow に `contents: write` permissions があるか

## 注意点

- **Wiki UI から直接編集しない** ─ 次の sync で main repo の内容に上書きされる
- 編集は **必ず main repo の `docs/wiki/*.md`** で行う
- 新ページを追加したら `_Sidebar.md` を必ず更新（忘れると Wiki ナビゲーションから辿れない）
- `_Sidebar.md` の編集も transformWikiLinks を通るので、ソースは普通の `[Text](Page.md)` 形式で書いて OK
- スクリプトを変更したら `node --check scripts/sync-docs-to-wiki.js` + ローカル変換テスト
