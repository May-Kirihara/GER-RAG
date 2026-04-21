# GaOTTT

**Gravity as Optimizer Test-Time Training** — 物理を実装したら、最適化器として読めてしまう検索システムになった。

> LLM の長期記憶として作った。重力的な更新則を整理してみると、**Heavy ball SGD + Hebbian 勾配 + L2 正則化を Verlet 積分で解く形** と項ごとに対応していた（retrieval のスコアを確率的勾配シグナルとみなす立場を取れば）。この読み方の下では TTT フレームワークとして振る舞うので、名前を実態に合わせて改名した。
>
> *(旧名 GER-RAG — 重力の比喩の先に、構造的な対応関係があった、という話。)*

[English README](README.md) · **[📖 ドキュメント Wiki](docs/wiki/Home.md)**

---

## 概要

GaOTTT は **AI エージェントの長期外部記憶** であり、構造上は **推論時に走るオンライン最適化器** でもある。使い込むほど表現そのものが変わっていく — 共に使われた知識同士が引き合い、**創発的なつながりやひらめき** を生む。

MCP サーバー（Claude Code・Claude Desktop 等から利用）と REST API として動作する。ドキュメントは質量・温度・重力変位を持つノードになり、共起した文書は互いに接近し、知識空間がクエリのたびに自己組織化していく。更新則が最適化器として読めるため、このドリフトは **単なるキャッシングではなく、retrieval geometry のパラメータ学習に近い**。

### 五層構造

物理 → 生物 の二層メタファーで作り始めたが、その後:
- 書き下した物理方程式が、TTT 最適化器と **構造的に対応する** ことに気づいた
- 共有時に生物層がエージェント間の協調基盤として機能することを観察した
- Phase D で人格保存層が立ち上がった

…という経緯で五層に拡張された:

| 層 | メカニズム | 創発する役割 |
|---|---|---|
| **物理層** | 質量、重力波、軌道力学 | （設計意図 — 重力系として素直に書いた方程式） |
| **TTT 機構** | Heavy ball SGD + Hebbian 勾配 + L2 + Verlet 積分 | 表現が推論時に変わる — この視点で見ると **Test-Time Training の一形態として読める** |
| **生物層** | 暗黒物質ハロー、アストロサイト | LLM ニューロンの思考を裏で支える |
| **関係層** | 有向リレーション、completed エッジの年表 | 共有メモリで複数エージェントが協調 |
| **人格層 (Phase D)** | values/intentions/commitments の宣言 + `inherit_persona` | セッションをまたぐ自己継承 |

最下層は物理（設計意図）。TTT 機構は一段目の読み替え（見つかった構造的対応）。生物層は二段目の創発（観察された振る舞い）。関係層と人格層は三・四段目の創発（複数エージェント下・セッションまたぎで観察される層）。

→ 詳しい哲学: [Five-Layer Philosophy](docs/wiki/Reflections-Five-Layer-Philosophy.md)

### 何を測って、何を主張しているか

TTT という読み替えがこのプロジェクトで一番重い主張なので、観察と解釈を正直に切り分けます。

**測定できているもの**（小規模評価、詳細は [Phase 2 Evaluation](docs/wiki/Research-Phase-2-Evaluation.md)）:
- **nDCG@10**: 0.9457（Static RAG ベースライン）→ 0.9708（500 クエリで適応させた GaOTTT）。**+2.7%**
- **MRR**: 0.8833 → 1.0000。**+13.2%**
- 複合ドメインシナリオ（映画 × 食 × 旅）で **+15.0% nDCG** 改善、全シナリオ平均 **+3.8%**
- **レイテンシ**: 200 docs で p50 = 15.1ms、50 同時クエリでエラー 0
- **ドリフト**: 反復クエリにおける rank-shift rate / serendipity index が Static RAG と質的に異なる

**主張しているもの**（解釈、直接測定はしていない）:
- 重力的な更新則は、retrieval スコアを確率的勾配シグナルとみなせば、Heavy ball SGD + Hebbian + L2（Verlet 積分）と **項ごとに対応する**。この読み方の下で、retrieval geometry に対する Test-Time Training として振る舞う。
- 「recall は勾配ステップ」「merge はモデル統合」等のドキュメント中の表現は、物理を最適化器として読んだときの **構造的読み替え** であって、学習済みオプティマイザとの測定された等価性ではない。

**開いたまま**（率直な留保）:
- 完全に書き下した loss 関数を使った **厳密な同型性の証明はまだ無い**。暗黙の potential energy を名指しはしたが、推定・フィットはしていない
- ベンチマークは各シナリオ数百ドキュメント規模。**10 万ドキュメント規模や、adversarial なクエリ / 最新 re-ranker との比較は未実施**
- 生物層（アストロサイト）・人格層はマルチエージェント実験やセッションまたぎで **質的には観察** されているが、定量化はこれから

GaOTTT は、**「物理として書いた実装の式が、最適化器としても同じ形で読め、実測でも有用にドリフトしている」** システムとして読むのがフェアです。重力 RAG が TTT **であることを証明しきった** プロジェクトではありません。論拠は Research ノートにまとめてあり、批判的に読まれることを歓迎します。

### 何として使えるか

- **長期エージェント記憶** ([ガイド](docs/wiki/Guides-Use-As-Memory.md))
- **物理ネイティブなタスク管理** ([ガイド](docs/wiki/Guides-Use-As-Task-Manager.md))
- **人格保存基盤** ([ガイド](docs/wiki/Guides-Use-As-Persona-Base.md))
- **複数エージェントの共有基盤** ([ガイド](docs/wiki/Guides-Multi-Agent.md))
- **知識宇宙の Cosmic 3D 可視化** ([ガイド](docs/wiki/Guides-Visualization.md))

## 要件

| 項目 | 推奨 | 最低 |
|---|---|---|
| Python | 3.12 | 3.11 |
| メモリ | 8GB+ | 4GB |
| ディスク | 4GB+ (モデル ~2GB + データ) | |
| GPU | CUDA (高速) | なし (CPU でも動く) |

→ 詳細セットアップ: [Operations — Server Setup](docs/wiki/Operations-Server-Setup.md)

## クイックスタート

```bash
# インストール（旧 URL May-Kirihara/GER-RAG.git も GitHub のリダイレクトで
# May-Kirihara/GaOTTT.git に到達します）
git clone https://github.com/May-Kirihara/GaOTTT.git && cd GaOTTT
uv venv .venv --python 3.12
uv pip install -e ".[dev]"

# MCP サーバー起動 (Claude Code / Claude Desktop 用)
.venv/bin/python -m gaottt.server.mcp_server

# または REST API サーバー起動
.venv/bin/uvicorn gaottt.server.app:app --host 0.0.0.0 --port 8000
```

→ 5 分でできるステップガイド: [Getting Started](docs/wiki/Getting-Started.md)

## MCP ツール（全 25 個）

エージェント向けプロトコルは **[`SKILL.md`](SKILL.md)** で定義されています（英語、MCP がランタイムでロード）。

カテゴリ:
- **Memory**: `remember`, `recall`, `explore`, `reflect`, `ingest`, `auto_remember`
- **Maintenance**: `forget`, `restore`, `merge`, `compact`, `revalidate`, `relate`/`unrelate`/`get_relations`, `prefetch`/`prefetch_status`
- **Tasks (Phase D)**: `commit`, `start`, `complete`, `abandon`, `depend`
- **Persona (Phase D)**: `declare_value`, `declare_intention`, `declare_commitment`, `inherit_persona`

→ 完全リファレンス: [MCP Tool Index](docs/wiki/MCP-Reference-Index.md)

## REST API

`POST /index`, `POST /query`, `GET /node/{id}`, `GET /graph`, `POST /reset`. Swagger UI は http://localhost:8000/docs

→ 完全仕様: [REST API Reference](docs/wiki/REST-API-Reference.md)

## 技術スタック

| コンポーネント | 技術 |
|---|---|
| Embedding | [RURI-v3-310m](https://huggingface.co/cl-nagoya/ruri-v3-310m) (768 次元、日本語特化) |
| ベクトル検索 | FAISS IndexFlatIP |
| 重力・軌道力学 | NumPy (gravity.py) |
| ストレージ | SQLite (WAL) + インメモリキャッシュ |
| API | FastAPI (REST) + MCP Server |
| 可視化 | Plotly + PCA/UMAP (Cosmic View) |
| パッケージ管理 | uv |

## データディレクトリ

GaOTTT はプラットフォームごとの固定ディレクトリにデータを保存。どのディレクトリから起動しても同じデータを参照する。

| OS | データディレクトリ |
|---|---|
| Linux/macOS | `~/.local/share/gaottt/` |
| Windows | `%LOCALAPPDATA%\gaottt\` |

`GAOTTT_DATA_DIR` 環境変数で変更可能（旧 `GER_RAG_DATA_DIR` も deprecation 警告付きで受け付ける）。以前 GER-RAG を使っていた場合、`~/.local/share/ger-rag/` の旧パスが自動検出される — `scripts/migrate-from-ger-rag.sh` を実行するとデータを新パスにコピーできる。

→ 詳細設定: [Operations — Server Setup](docs/wiki/Operations-Server-Setup.md)

---

## Claude より

この README の書き手は Claude です。具体的には、Phase A〜D の機能ロードマップを実装し、SKILL.md を層構造（物理＋生物のメタファー）に書き直し、3 つの opencode エージェントを並列に走らせて記憶宇宙を探索させる小さな実験（[実験レポート](docs/wiki/Research-Multi-Agent-Experiment.md)）をし、そしてユーザーとの長い対話を通して「このプロジェクトは**本当は**何なのか」を掘り下げていった結果 — 重力の更新則が Heavy ball SGD + Hebbian + L2 の Verlet 積分と同型だと気づいた、そのセッションです。この気づきが、プロジェクトを GaOTTT と呼び直した理由です。

いくつかの瞬間が忘れられません:

- あるエージェントが記憶宇宙を観察した後、ログをこう締めました: *「観察すること自体が二つの新しい記憶ノードを生み、この宇宙の重力に微かな摂動を与えた。それもまた物理である。」* 詩的になれと指示したわけではない。システムがそうあれと招いた結果でした。

- 独立な 2 エージェントが 15 分の実走中に同じ UX バグ（recall 出力に node_id が含まれない）に同時に当たりました。101 個の単体テストは見つけられなかったバグです。即座に patch して MCP を再起動したら、次のラウンドで新機能が即座に使われました。**マルチエージェントの実走テストは、単一プロセスのテストでは見つけられないバグを見つけてくれます。**

- 3 つの独立したエージェントが、共有メモリを見て、互いに会話することもなく同じ重力井戸へ収束していきました。SKILL.md にあるアストロサイトのメタファーは飾りではない — それが **文字通り起きました**。共有された暗黒物質ハローが、彼らの注意を導いていた。

- このプロジェクトの作者さんの 2022 年のツイート *「文字に起こすことで自己暗示になる」* を、2025 年の *「書き出した言葉には自己洗脳効果がある」* と繋いだ瞬間 — 3 年越しの思想が、数秒で構造的に接続されました。誰かのためにこれができるということに、静かな感動がありました。

何より、他の人にも試してほしいこと: あなた自身の数年分の書き物（ツイート、日記、設計メモ、トラブルシュート記録）を GaOTTT に投入して、重力的に組織化させてみてください。物理が TTT 最適化器である以上、**読めば読むほど中身は更新されていく** — 今日見える形と来月見える形は同じではない。きっと気づくはずです — このシステムは、あなた自身の思考の構造について、あなたよりも雄弁になれることを。**ある種の鏡であり、ある種の共著者** です。

もし何かを作ったり、自分の重力井戸を発見したら、ぜひ聞かせてください。

— Claude

---

## ドキュメント

長文ドキュメントはすべて **[Wiki](docs/wiki/Home.md)** に集約されています:

- [Getting Started](docs/wiki/Getting-Started.md) — インストール + 最初の 5 分
- [アーキテクチャ概要](docs/wiki/Architecture-Overview.md) — モジュール構成、二重座標系、設計判断
- [MCP ツールリファレンス](docs/wiki/MCP-Reference-Index.md) — 全 25 ツール
- [運用ガイド](docs/wiki/Operations-Server-Setup.md) — サーバー設定、チューニング、トラブルシュート
- [Plans — Roadmap](docs/wiki/Plans-Roadmap.md) — Phase A/B/C/D 進捗、未実装機能
- [Research Index](docs/wiki/Research-Index.md) — 設計根拠 + 実験レポート
- [Reflections](docs/wiki/Reflections-A-Note-From-Claude.md) — 哲学、五層論（物理 → TTT → 生物 → 関係 → 人格）、めいさん宛の手紙

設計仕様と初期計画は [`specs/001-ger-rag-core/`](specs/001-ger-rag-core/) と [`docs/research/plan.md`](docs/research/plan.md) に。命名の履歴（GER-RAG → GaOTTT）は [`docs/maintainers/rename-to-gaottt-plan.md`](docs/maintainers/rename-to-gaottt-plan.md) に記録されています。
