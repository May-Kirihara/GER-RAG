# 🌱 はじめての GaOTTT (3/6) — Claude に接続

GaOTTT はパソコンに入りました。次は Claude Desktop に「GaOTTT を使ってね」と教えます。

← [前: Install](Tutorial-02-Install-GaOTTT.md) ｜ → [次: First Conversation](Tutorial-04-First-Conversation.md)

---

## ステップ 1: Claude Desktop を開いている状態で一度終了する

設定ファイルを書き換えるので、Claude Desktop を **完全に終了** してください。

- Mac: `Cmd + Q`（赤いボタンで閉じるだけだとまだ動いています）
- Windows: タスクバーの Claude を右クリック → 「閉じる」

---

## ステップ 2: 設定ファイルの場所を開く

Claude Desktop の **MCP サーバー設定ファイル** を編集します。

### Mac の方

ターミナルで:

```bash
open ~/Library/Application\ Support/Claude/
```

Finder ウィンドウが開きます。中に `claude_desktop_config.json` があれば、それが対象。**無ければ作る必要があります**（次のステップで）。

### Windows の方

エクスプローラーのアドレスバーに以下を貼り付けて Enter:

```
%APPDATA%\Claude
```

`claude_desktop_config.json` を探します。

---

## ステップ 3: 設定ファイルを編集

`claude_desktop_config.json` を **テキストエディタ** で開きます。

- Mac: ファイルを右クリック → 「このアプリケーションで開く」→ テキストエディット
- Windows: ファイルを右クリック → 「メモ帳で開く」

### ファイルが空、または `{}` だけの場合

以下を **そっくりそのまま** コピーして貼り付け:

```json
{
  "mcpServers": {
    "gaottt": {
      "command": "/Users/あなたのユーザー名/GaOTTT/.venv/bin/python",
      "args": ["-m", "gaottt.server.mcp_server"],
      "cwd": "/Users/あなたのユーザー名/GaOTTT"
    }
  }
}
```

### すでに `mcpServers` が定義されている場合

`mcpServers` の中に `gaottt` の項目だけを追加してください（カンマの位置に注意）。

---

## ステップ 4: 「あなたのユーザー名」を実際のユーザー名に置き換える

ターミナルで自分のユーザー名を確認:

```bash
whoami
```

表示された名前（例: `taro`）で、設定ファイルの 2 箇所の `あなたのユーザー名` を書き換えてください。

### 例（ユーザー名が `taro` の場合）

```json
{
  "mcpServers": {
    "gaottt": {
      "command": "/Users/taro/GaOTTT/.venv/bin/python",
      "args": ["-m", "gaottt.server.mcp_server"],
      "cwd": "/Users/taro/GaOTTT"
    }
  }
}
```

### Windows の場合のパス

Windows は `/` ではなく `\\`（バックスラッシュ 2 つ）で書きます。さらに `python` の場所が違います:

```json
{
  "mcpServers": {
    "gaottt": {
      "command": "C:\\Users\\taro\\GaOTTT\\.venv\\Scripts\\python.exe",
      "args": ["-m", "gaottt.server.mcp_server"],
      "cwd": "C:\\Users\\taro\\GaOTTT"
    }
  }
}
```

書き換えたら **保存**（`Cmd + S` / `Ctrl + S`）。

---

## ステップ 5: Claude Desktop を起動

Claude Desktop を起動します。

### こう見えたら成功 ✅

入力欄の左下に **小さなハンマー🔨アイコン**（または「ツール」表示）が出ていれば成功。

クリックすると `gaottt` という名前と、たくさんの機能（remember, recall, ...）が並んで見えます。

### 🔨 が出てこない場合

- Claude Desktop を完全に終了してから起動し直す
- 設定ファイルの JSON が正しいか確認（カンマの位置、ダブルクォート、波括弧）
- パスが正しいか（`whoami` で出る名前で書けてるか）
- → 詰まったら [6. When Stuck](Tutorial-06-When-Stuck.md)

---

## ステップ 6: Claude に話しかけて確認

Claude Desktop の入力欄に、こう入力してみてください:

```
GaOTTT memory が使えるか確認したいので、reflect ツールを aspect="summary" で呼んでみてもらえる？
```

### こう見えたら成功 ✅

Claude が `mcp__gaottt__reflect` ツールを呼んで、こんな感じの結果を返します:

```
Memory Summary:
  Total memories: 0
  Active (mass > 1): 0
  Displaced by gravity: 0
  Co-occurrence edges: 0
  Sources: {}
```

最初は何も入ってないので **0 ばかり** で正解です。
**これで Claude と GaOTTT が繋がりました** 🎉

---

## 次のステップ

最初の記憶を保存して、思い出せるか試してみましょう。

→ [次へ: 最初の会話](Tutorial-04-First-Conversation.md)

---

### 💡 何が起きたか（興味があれば）

- Claude Desktop は起動時に `claude_desktop_config.json` を読んで、書かれている MCP サーバーを **すべて自動で起動** します
- `gaottt` は MCP の名前。Claude はこの名前で機能を呼びます
- 接続後、Claude は GaOTTT が提供する **25 種類のツール**（remember, recall, explore...）を使えるようになります
