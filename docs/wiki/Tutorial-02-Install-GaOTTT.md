# 🌱 はじめての GaOTTT (2/6) — インストール

このページでは、パソコンに GaOTTT（旧 GER-RAG）を入れます。

> **改名お知らせ**: 旧名 GER-RAG (Gravity-Based Event-Driven RAG) は 2026-04 に GaOTTT (Gravity as Optimizer Test-Time Training) に改名されました。GitHub リポジトリ URL の改名は作業中（`GER-RAG.git` はリダイレクトで引き続き利用可）。このページの手順もそのまま動きます。

← [前: Welcome](Tutorial-01-Welcome.md) ｜ → [次: LLM に接続](Tutorial-03-Connect-To-Claude.md)

---

## ステップ 1: ターミナルを開く

「コマンドを打つ画面」のことです。

### Mac の方

1. `Cmd + Space` キーで Spotlight 検索を開く
2. 「ターミナル」と打って Enter
3. 黒っぽい（または白い）ウィンドウが開きます

### Windows の方

1. スタートメニューを開く
2. 「PowerShell」と打って Enter
3. 青っぽい（または黒い）ウィンドウが開きます

このウィンドウに、これから出てくる命令文（コマンド）を **1 行ずつコピペして Enter** で実行していきます。

> 💡 **コピペのコツ**: コードブロック（灰色の枠）の右上にコピーボタンがあります。それを押して、ターミナルで **右クリック → ペースト** で貼り付けられます。

---

## ステップ 2: Python が入っているか確認

ターミナルに次の行をコピペして Enter:

```bash
python3 --version
```

### こう見えたら成功 ✅

```
Python 3.11.5
```

（数字は違っていても OK。**3.11 以上** なら次へ進んでください。3.10 以下なら次のステップへ）

### `command not found` と出たら、または 3.10 以下のとき

Python が入っていないか、古いバージョンです。インストールしましょう。

#### Mac の方

```bash
# Homebrew が無ければ先に入れる
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Python 3.12 を入れる
brew install python@3.12
```

#### Windows の方

公式サイトから Python 3.12 をインストール: https://www.python.org/downloads/

> ⚠️ **インストール時に「Add Python to PATH」のチェックを必ず入れる** ことが重要です。これを忘れるとターミナルから python が見つかりません。

インストール後、**ターミナルを一度閉じて開き直し**、もう一度 `python3 --version` で確認してください。

---

## ステップ 3: uv をインストール

`uv` は Python のパッケージを高速にインストールしてくれるツールです。

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Windows PowerShell の方:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### こう見えたら成功 ✅

最後の方に `installed: ...` のようなメッセージが出れば OK。

> ⚠️ **重要**: ここで一度 **ターミナルを閉じて開き直してください**。新しくインストールした uv をターミナルが認識するために必要です。

開き直したら、確認:

```bash
uv --version
```

`uv 0.x.x` のように表示されれば成功です。

---

## ステップ 4: GaOTTT をダウンロード

GaOTTT のソースコードを、自分のパソコンに持ってきます。

```bash
cd ~
git clone https://github.com/May-Kirihara/GER-RAG
cd GER-RAG
```

> 💡 `git` も入ってない場合: Mac は `xcode-select --install`、Windows は https://git-scm.com/download/win から入れてください。
>
> 📝 上記 URL は改名前のものですが、GitHub が自動でリダイレクトしてくれるので問題ありません。

### こう見えたら成功 ✅

```
$ cd GER-RAG
GER-RAG $
```

ターミナルの先頭が `GER-RAG` っぽい表示になれば、フォルダの中に入れています（フォルダ名は近日中に GaOTTT に変わる予定）。

---

## ステップ 5: 仮想環境を作る + 依存パッケージをインストール

ここが一番時間がかかります（5〜15 分くらい）。

```bash
uv venv .venv --python 3.12
uv pip install -e ".[dev]"
```

途中でたくさん文字が流れますが、**怒られているわけではありません**。「これをダウンロードしてます」「あれを設定してます」というお知らせです。

### こう見えたら成功 ✅

最後に **エラーメッセージなしで** プロンプト（`$` や `>` の入力待ち状態）に戻れば成功です。

赤い文字や `Error:` から始まる行が出たら、[6. When Stuck](Tutorial-06-When-Stuck.md) を参照してください。

---

## ステップ 6: 動作確認

GaOTTT が動くかちょっとだけ試します:

```bash
.venv/bin/python -c "from gaottt.config import GaOTTTConfig; print('GaOTTT is ready!')"
```

### こう見えたら成功 ✅

```
GaOTTT is ready!
```

このメッセージが出れば、**インストール完了です** 🎉

---

## 次のステップ

これで GaOTTT はパソコンに入りました。
次は LLM Desktop に GaOTTT を繋ぎます。

→ [次へ: LLM に接続](Tutorial-03-Connect-To-Claude.md)

---

### 💡 何が起きたか（興味があれば）

- `~/GER-RAG` フォルダに、GaOTTT 一式が入りました（フォルダ名は `GaOTTT` に変わる予定）
- `.venv` フォルダの中に、GaOTTT だけが使う Python 環境ができました（あなたのパソコン全体に影響しません）
- 初回のインストールで、約 1〜2GB のファイルがダウンロードされています

不要になったら `~/GER-RAG` フォルダを丸ごと削除すれば、すべて綺麗に消えます。
