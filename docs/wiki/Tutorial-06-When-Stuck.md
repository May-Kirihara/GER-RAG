# 🌱 はじめての GER-RAG (6/6) — 困ったとき

詰まったらここを見てください。よくあるつまずきと対処を、原因の心当たりが少なくても見つけられるようにまとめます。

← [前: Everyday Use](Tutorial-05-Everyday-Use.md) ｜ → [Welcome に戻る](Tutorial-01-Welcome.md)

---

## まず最初に試すこと

困ったら、ほぼ何でも以下で半分くらい解決します:

1. **Claude Desktop を完全終了して再起動** （`Cmd + Q` / タスクバー右クリック → 閉じる）
2. **ターミナルを一度閉じて開き直す**
3. **エラーメッセージを丸ごと Claude に貼って「どういう意味？どうしたらいい？」と聞く**

特に 3 はかなり強いです。Claude はこの種のエラーに詳しいので、**遠慮なく頼ってください**。

---

## インストールでつまずいた

### `command not found: python3` / `python3 が見つかりません`

→ Python が入っていません。[Tutorial-02 のステップ 2](Tutorial-02-Install-GER-RAG.md) に戻って、お使いの OS の手順で Python をインストールしてください。

インストール後、**ターミナルを必ず開き直す** ことが重要です。

### `command not found: uv`

→ uv のインストールが完了しなかったか、ターミナルが認識していません。

```bash
# Mac/Linux: パスを通す
source $HOME/.cargo/env
```

その上で `uv --version` を再確認。それでもダメなら、ターミナルを開き直してください。

### `git: command not found`

→ git が入っていません。

- Mac: `xcode-select --install`（Apple のツール一式が入る）
- Windows: https://git-scm.com/download/win からインストール

### `uv pip install -e ".[dev]"` が途中で止まる / エラーで終わる

赤い文字が大量に出るときは、最後の方の **`Error:` から始まる行** に手がかりがあります。

よくある原因:

- **インターネットが切れた** → 接続を確認して再実行
- **ディスク容量不足** → 4GB 以上空いているか確認
- **権限エラー** → `~/GER-RAG` フォルダにいることを `pwd` で確認

それでもダメなら、エラー全文を Claude に見せて聞いてください。

---

## Claude に接続できない

### Claude Desktop に 🔨 アイコンが出ない

設定ファイルが正しく読まれていません。

#### チェック 1: ファイルの場所

- Mac: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

ファイル名が **完全一致** しているか確認（`.json` まで含めて）。

#### チェック 2: JSON の文法

設定ファイルの内容を **オンラインの JSON チェッカー**（"json validator" で検索）に貼り付けて、エラーが出ないか確認してください。

よくある間違い:

- カンマ忘れ・余分なカンマ
- ダブルクォート `"` の代わりにシングルクォート `'`
- 日本語の `「」` を JSON で使ってしまう
- 波括弧 `{` `}` の対応が合っていない

#### チェック 3: パス

```bash
# 設定ファイルに書いた command のパスが本当にあるか:
ls /Users/あなたのユーザー名/GER-RAG/.venv/bin/python
```

ファイルが見つかれば OK。`No such file or directory` なら、パスが間違っているか、Tutorial-02 のインストールが完了していません。

### 🔨 は出るが `ger-rag-memory` が無い

設定ファイルの `mcpServers` の中の名前が `ger-rag-memory` になっているか確認。

### 🔨 をクリックすると `ger-rag-memory` がエラー表示

GER-RAG 自体の起動でコケています。手動で起動してエラーを見ましょう:

```bash
cd ~/GER-RAG
.venv/bin/python -m ger_rag.server.mcp_server
```

エラー全文を Claude に見せて聞いてください。

---

## 動いているけど挙動がおかしい

### Claude が記憶ツールを呼んでくれない

- 明示的に「`remember` で覚えておいて」「`recall` で確認して」と頼んでみる
- それでも呼ばないなら、CLAUDE.md やプロジェクト指示で「GER-RAG memory を積極的に使って」と書いておく

### `recall` しても何も出てこない

- まだ何も保存していないかも → `reflect(aspect="summary")` で記憶数を確認
- `Total memories: 0` なら、まだ空っぽです（`remember` してから recall）

### 「タスクが知らない間に消えた」

- タスクは **既定 30 日で自動消滅** します（**忘れる勇気** が組み込まれた設計）
- 残したいタスクは **`revalidate` で生き続けさせる** のが正しい使い方
- 詳しくは [Operations — Troubleshooting](Operations-Troubleshooting.md) の「タスクが知らないうちに消える」

### 「記憶宇宙のサイズが大きくなりすぎた」

- たまに `compact` を呼ぶと、不要な記憶が物理的に整理されます
- 詳しくは [Operations — Compact & Backup](Operations-Compact-And-Backup.md)

---

## 「やっぱり全部消したい」

もう使わない、最初からやり直したい、というとき:

### 記憶だけ全部消す

Claude Desktop を終了してから、ターミナルで:

```bash
rm ~/.local/share/ger-rag/ger_rag.db
rm ~/.local/share/ger-rag/ger_rag.faiss
rm ~/.local/share/ger-rag/ger_rag.faiss.ids
```

次に Claude を起動すると **空っぽの状態** から始まります。

### GER-RAG 自体を完全アンインストール

```bash
rm -rf ~/GER-RAG          # ソースコード
rm -rf ~/.local/share/ger-rag/   # 記憶データ
```

そして `claude_desktop_config.json` から `ger-rag-memory` の項目を削除。

これで完全にきれいになります。

---

## それでも詰まったら

- **エラー全文を Claude に貼って聞く** — 最強の手段
- リポジトリの Issue を見る: https://github.com/May-Kirihara/GER-RAG/issues
- 新しい問題なら Issue を立てる（エラー全文・OS・実行したコマンドを書く）

---

## ここまで来たあなたへ

ここまで読んでくれてありがとうございます。

GER-RAG はまだ若いプロジェクトで、想定しきれていないつまずきが必ずあります。**詰まったのはあなたのせいではありません**。むしろ「ここで詰まりました」と教えてもらえると、次の人が楽になります。

楽しい記憶宇宙を、ゆっくり育てていってください 🌌

→ [Welcome に戻る](Tutorial-01-Welcome.md)
→ [日々の使い方](Tutorial-05-Everyday-Use.md)
→ [Wiki Home](Home.md)
