# Plans — Multiverse Scale-Out（大規模化 — 1人1宇宙のスケールアウト）

> 対象: デプロイ層・インフラ層（engine / physics / observation 層は **完全不変**）
> 言語: 日本語（保守・計画ドキュメント）
> 関連: [Architecture — Concurrency](Architecture-Concurrency.md), [Operations — Server Setup](Operations-Server-Setup.md), [Operations — Resource Requirements](Operations-Resource-Requirements.md), [Guides — Per-Project DBs](Guides-Per-Project-DBs.md), [Plans — Roadmap](Plans-Roadmap.md)
> 状態: 🟢 **MV0〜MV5 実装済み（2026-07-04）** / MV6 未着手。ブレスト収束。同日 Codex CLI (`codex exec`) による独立レビューを実施し、指摘 10 件（dimension wiring / Stage 依存関係 / ownership lease / DR 前提 / リソース試算等）を反映済み。Stage 4（バックアップ/DR）は 2026-07-04 に実装済み (MV5)。
> 実装者向け作業計画: [multiverse-implementation-plan.md](../maintainers/multiverse-implementation-plan.md)（MV0–MV6、ファイル単位の変更一覧・テスト・acceptance 付き）
> 最終更新: 2026-07-04（MV5 実装済み）

## 0. 背景と確定事項

2026-07-02 のブレスト（「DB を PostgreSQL 等に移行すべきか」を起点に、大規模化・商用提供の形を検討）で以下が確定した:

| 論点 | 決定 |
|---|---|
| 提供形態 | **セルフホスト SaaS** — テナント（顧客組織）のホストで動かす |
| テナント規模 | 1 テナント **~50 ユーザー** |
| 宇宙の割り方 | **1 ユーザー = 1 宇宙**（チーム共有宇宙ではない） |
| data plane | **SQLite-per-universe + FAISS files を堅持**。Postgres への全面移行は **しない** |
| 共有するもの | **embedding service**（RURI 常駐をホストに 1 つ、engine から分離） |
| control plane | Postgres 1 つ（テナント台帳・ユーザー・課金・監査・宇宙ごとの embedder version）。**物理には触らせない** |
| 言語 | 日本語（RURI）先行 → 英語は後続。embedder は **宇宙単位の属性** |
| チーム共有知識 | **v1 では作らない**（既存ツールに任せる）。v2 で「テナント共有宇宙を 1 つ追加」する方式を roadmap に置く（§4 Stage 6） |

### なぜ「Postgres 移行」が答えではなかったか

ブレストの過程で、「PostgreSQL に移す」は 2 つの別問題を含んでいると整理された:

1. **永続化の置き換え** — SQLite+ファイル → Postgres。`StoreBase` の実装差し替えで済む、比較的安い作業
2. **「1 プロセスが宇宙全体を RAM に持つ」前提の解体** — cache 全件ロード / virtual FAISS O(N) rebuild / BM25 全件 build。**Postgres に移しただけでは 1 ミリも解決しない**

そして鍵になる観察: **GaOTTT の物理は 1 つの記憶宇宙の中で閉じている**。重力場・共起・persona は宇宙を跨がないので、クロス宇宙クエリが原理的に不要。つまり **宇宙 = 自然なシャード境界** であり、「巨大な共有 DB を水平分割する」難問を解く必要がそもそもない。1 ユーザー = 1 宇宙と決めた時点で、(2) の前提解体も不要になる — 個人宇宙は現行アーキテクチャが快適に扱えるサイズ（数千〜数万ノード）に収まるため。

さらに「SQLite が唯一の source of truth」という性質（FAISS は [`scripts/rebuild_faiss_from_db.py`](../../scripts/rebuild_faiss_from_db.py) で決定論的に再構築可能 — 2026-05-31 の FAISS reverse-overwrite incident 復旧で実証済み）が、そのままバックアップ/DR 設計になる。Postgres が本当に得意なこと（台帳・課金・監査の集計）だけを Postgres にやらせる。

## 1. 物理アナロジー — Multiverse

命名規約（新概念には物理アナロジーを必ず命名する）に従い、本計画のアーキテクチャを **Multiverse** と呼ぶ:

- **各ユーザーの記憶宇宙は独立** — 質量・重力場・軌道・persona は宇宙の内側で閉じ、宇宙間に力は働かない
- **物理法則（計量）は全宇宙で共有** — embedding model は「その宇宙の空間の計量テンソル」であり、同じ言語圏の宇宙は同じ法則で動く。embedding service の共有は「物理定数の共有」であって「物質の共有」ではない
- **1 宇宙 1 観測者（書き込みオーナー）** — Verlet 積分・mass 更新は順序依存（非可換）なので、宇宙ごとに書き込みオーナーは常に 1 プロセス。これは現行 proxy mode の「engine は常に 1 process」原則の宇宙単位への一般化

この命名は Phase M の単一規則哲学と整合する: Multiverse はデプロイの殻であり、force computation / mass update / observation layer のいずれにも触れない。physics Phase ではないので **Phase レター非消費**。

## 2. 目標アーキテクチャ

```
[中央 control plane]  Postgres ×1 (どこかに 1 つ、テナントホスト外でも可)
  ├─ テナント台帳 / ユーザー・認証
  ├─ 宇宙レジストリ (universe_id, owner, embedder_id + version, data_dir)
  └─ 課金・利用量・監査ログ

[テナントホスト ×N (セルフホスト先、~50 ユーザー)]
  ├─ embedding service ×1     … RURI 常駐 (GPU or CPU)、バッチング。将来 EN モデル同居
  ├─ universe supervisor ×1   … 宇宙 engine の spawn / 休眠 / ユーザー→宇宙ルーティング
  └─ universe engine ×(アクティブ数)
       … 現行の proxy backend そのもの。各自 SQLite + FAISS files + cache + BM25
       … アイドル時は dead-man-switch で self-shutdown、次アクセスで auto-respawn
```

現行構成との対応: **現在の単一ユーザー運用は「宇宙が 1 つだけの Multiverse」** である。stdio / proxy / streamable-http の既存起動モードは default 不変のまま残り、Multiverse は opt-in の上位層として被さる（frontend parity の文化を維持）。

## 3. 設計原則

1. **physics 完全不変** — engine / gravity / scorer / store のコードは Stage 1 の embedder 縫い目以外触らない
2. **1 宇宙 1 書き込みオーナー** — 積分の非可換性ゆえ、宇宙をマルチマスターにしない。supervisor が宇宙ごとの ownership を保証する（= 逆方向上書き罠・bidirectional cache overwrite のクラスが構造的に消える）。**ただしこの保証は「supervisor 外からの直接 engine 起動を宇宙ディレクトリの owner lease で拒否する」ことが条件** — legacy stdio/streamable-http で同じ data_dir を直接開ける限り、dream loop / write-behind / FAISS save は supervisor の管理外から走れる。lease 機構は Stage 2 の必須要件（§4 Stage 2）
3. **SQLite が唯一の source of truth** — バックアップ対象は宇宙ごとの SQLite ファイルのみ。FAISS / BM25 は再構築可能な派生物
4. **embedder は宇宙単位の属性** — 1 宇宙内で embedding は同質でなければならない（RURI は cross-lingual でない — 実機確認済み）。宇宙レジストリに `embedder_id + version` を記録
5. **default 不変** — 既存のシングルユーザー構成（claude.json / opencode.json / .codex）は一切変更なしで動き続ける

## 4. Stage 計画

### Stage 1 — Embedder 分離（RemoteEmbedder + embedding service）

**目的**: 「プロセスごとにモデルロードが必要」というデプロイ上の最重量問題を解消する。GPU コストをユーザー数ではなくホスト数に比例させる。

**縫い目の確認結果 (2026-07-02)**: engine が要求する embedder インターフェースは実質 4 メソッドの duck-typing —

| メソッド | 呼び出し元 |
|---|---|
| `encode_documents(texts) -> np.ndarray` | `engine.index_documents` / `_rebuild_*` |
| `encode_query(text) -> np.ndarray` | `engine.query` seed 段 |
| `encode_queries(texts) -> np.ndarray` | multi-source query（**optional** — `getattr` fallback あり） |
| `dimension: int` | index 初期化 |

テストの `StubEmbedder` が既にこの縫い目だけで engine を駆動している。型ヒントは `RuriEmbedder` 固定なので `Protocol`（`EmbedderProtocol`）へ緩める。

**実装スケッチ**:

- `gaottt/embedding/service.py` — FastAPI の極小サーバ。`POST /encode_documents` / `POST /encode_queries`、複数 requester のバッチング（`batch_size=32` を跨いでまとめる）、model は起動時 1 回ロード。`GET /info` で `model_name` / `dimension` / `version` を返す
- **wire protocol を明示的に固める**（レビュー指摘）: payload は binary `float32`（msgpack or raw bytes、JSON float 配列は帯域と parse で不利）、shape は `(N, dim)` / `encode_query` は `(1, dim)` の現行契約を維持、**L2 正規化と RURI prefix の適用は service 側の責務**（client は生テキストを送るだけ — prefix 実装が client 側に漏れると embedder 差し替え時に二重管理になる）。request timeout / batch queue の最大待ち時間 / backpressure（queue 上限超過で 503）も v1 で定義する
- `gaottt/embedding/remote.py` — `RemoteEmbedder`。上記 4 メソッドを HTTP で叩く薄いクライアント
- **dimension / version の整合ガード**: 現行 wiring は `build_engine` が **embedder に問い合わせる前に** `config.embedding_dim` で FAISS を構築する（`services/runtime.py:36`）。したがって check は「`/info` の dimension == `config.embedding_dim`」かつ「`/info` の model version == 宇宙 manifest の embedder version」を **build/startup 時に検証し、不一致なら起動拒否**（宇宙の計量が変わる事故の防止）。FAISS 構築を embedder 初期化後に寄せるリファクタは optional
- **宇宙 manifest（local）を Stage 1 で導入**: `<data_dir>/manifest.json` に `embedder_id / embedder_version / dimension / created_at` を記録。**Stage 3 の Postgres レジストリを待たずに version check が成立する**（レジストリは後で manifest と sync する上位層）。manifest 不在の既存 DB は「manifest なし = check skip + 初回起動時に現 config から生成」で後方互換（default 不変）
- `services/runtime.py` の factory を分岐: `config.embedder_endpoint` があれば `RemoteEmbedder`、なければ従来どおり in-process `RuriEmbedder`（**default 不変**）
- 同期呼び出しについて: 現行 `encode_query` も GPU 同期呼びで event loop をブロックしており、Remote 化で悪化はしない（ネットワーク往復 +1-3ms のみ）。`asyncio.to_thread` への wrap は独立した改善として optional

**rollback**: `embedder_endpoint` 未設定で完全に従来経路。
**所要**: 中（4〜5 日、バッチング + protocol 定義 + manifest 含む）

### Stage 2 — Universe supervisor（proxy 機構の宇宙単位への一般化）

**目的**: ユーザー→宇宙のルーティングと、宇宙 engine のライフサイクル管理（spawn / 休眠 / 再起床）。

**実装スケッチ**:

- 現行 proxy mode の実績ある機構を **概念的な下敷き** として一般化する（レビュー指摘: 「そのまま流用」は過小評価 — 現行 proxy は固定 host:port の単一 backend 前提であり、以下の多くは新規開発）:
  - **spawn**: 初回アクセスで detached backend を auto-spawn → **宇宙ごとの backend を on-demand spawn**。新規に必要: 宇宙ごとの port / unix socket 割当、**spawn 競合ロック**（同一宇宙への同時初回アクセスで backend が 2 つ立たない保証）、プロセス監視・異常終了時の再起動
  - **休眠**: cold-war dead-man-switch（ping 停止 5 分で self-shutdown）→ **アイドル宇宙が RAM から消える**。50 ユーザー中アクティブ 10-15 なら常駐はその分だけ
  - **ルーティング**: shim → backend の relay → **認証 identity → universe_id → その宇宙の backend** への relay
- **宇宙ディレクトリの owner lease（必須要件）**: `<data_dir>/owner.lock`（pid + host + heartbeat）を engine 起動時に取得し、**lease 保持者がいる宇宙への直接起動（legacy stdio / streamable-http を含む）は起動時診断で拒否**する。§3 原則 2 の「1 宇宙 1 書き込みオーナー」はこの機構で初めて保証になる。復旧作業用の escape hatch（`--force-takeover`、既存 lease の heartbeat 停止確認つき）を用意
- 宇宙ごとの分離は `data_dir`（現行 XDG `~/.local/share/gaottt` を `.../universes/<universe_id>/` に）+ port or unix socket + Stage 1 の manifest
- **env/config の明示的配達**: proxy backend が「最初に spawn した frontend の env だけ継承する」既知の罠（2026-06-01 確定）は、supervisor が宇宙 manifest（Stage 3 以降はレジストリ）から config を明示的に渡して spawn する構造で **クラスごと解消** する
- 認証の検証点を明確にする: **supervisor が入口の検証点**（API キー → universe_id 解決）。加えて **宇宙 backend 自体にも per-universe token**（spawn 時に env で注入、streamable-http middleware で検証）— localhost 上の別 OS ユーザーが port 直叩きで supervisor を迂回する穴を塞ぐ（実装計画レビューで確定）。embedding service はホスト内部資源として **localhost / unix socket に bind**（テナントホスト外に露出しない、認証は持たない）。supervisor 管理 API（宇宙の作成・削除）は別の admin キー。**v1 で宇宙に露出する経路は MCP のみ**（REST app は独自 engine を立てるため managed 宇宙では owner lease が構造的に拒否する。REST 相当が必要なら Stage 3 以降で reverse-proxy 化を検討）。Roadmap 未実装項目「認証」はここに内包
- supervisor 自体はステートレス再起動可能に: 宇宙の状態は manifest + owner.lock + control plane が持ち、supervisor は再起動後にそれらをスキャンして復元

**rollback**: supervisor を使わなければ従来の単一 backend proxy のまま。
**所要**: 大（**1.5〜2 週**）— registry / lease / spawn 競合 / 監視を含む新規の制御面として見積もる（レビューで 5〜7 日は楽観的と指摘）

### Stage 3 — Control plane（Postgres）

**目的**: テナント・ユーザー・宇宙の台帳と、課金・監査に必要な集計。**GaOTTT 本体とは独立した新コンポーネント**であり、engine のコードには触れない。

**実装スケッチ**:

- スキーマ: `tenants` / `users` / `universes (universe_id, owner_user, embedder_id, embedder_version, data_dir, status)` / `usage_events` / `audit_log`
- **source of truth の向き**: 宇宙のローカル manifest（Stage 1）が一次、Postgres レジストリはその集約 + 管理層。supervisor は control plane から読む（ローカル manifest cache + 定期 sync、control plane 落ちでもホストは自走できる degraded mode）。Stage 1-2 が manifest だけで成立する順序になっているのはこのため（レビュー指摘の Stage 依存関係の解消）
- 課金メータリング: supervisor が recall/remember 回数等を `usage_events` に非同期 push
- **MCP/REST parity 鉄則との関係**: control plane API は engine の能力ではなく管理面なので parity 鉄則の対象外（`/reset` と同様の例外扱い）。engine 側に新 API を足す場合は従来どおり MCP + REST 同時

**所要**: 大（1〜2 週）— 新規開発の本体

### Stage 4 — バックアップ / DR（Litestream）✅ 実装済み (MV5, 2026-07-04)

**目的**: 宇宙ごとの継続バックアップと、退会・エクスポートの運用。実装詳細は [Operations — Backup & DR](Operations-Backup-Multiverse.md) と [multiverse-implementation-plan.md](../maintainers/multiverse-implementation-plan.md) §MV5 を参照。

**実装スケッチ**:

- 宇宙ごとの SQLite に Litestream（or 同等の WAL ストリーミング）を張り、オブジェクトストレージへ replicate。**FAISS は原則バックアップ対象外**（`rebuild_faiss_from_db.py` で決定論的に再構築、`--check` で検証）
- **「SQLite だけで復元できる」の前提条件を DR に明記する**（レビュー指摘）: FAISS 再構築の決定論は **同一 embedder artifact（model weights）+ 同一 version + 同一 prefix/normalization 実装** が揃って初めて成立する。バックアップ対象は SQLite + **宇宙 manifest** の 2 点セットとし、embedder model の取得手段（HF cache の pin / 社内ミラー）を runbook に含める。モデルが将来 HF から消える・依存更新で数値が微妙に変わるリスクに備え、**大きい宇宙には FAISS snapshot の任意バックアップ**（RTO 短縮を兼ねる）をオプションで用意
- 復旧手順 = 他プロセス停止（owner lease 確認）→ SQLite restore → manifest の embedder version で model を用意 → FAISS rebuild → 起動時診断（Tier A/B）で整合確認。既存の incident 復旧手順がそのまま runbook になる
- 退会 = 宇宙ディレクトリ削除 + レジストリ更新。エクスポート = SQLite ファイルコピー（データポータビリティ・忘れられる権利にそのまま効く）
- Operations doc 化: `Operations-Backup-Multiverse.md`（実装時に新設、[Operations — Compact & Backup](Operations-Compact-And-Backup.md) から相互リンク）

**所要**: 小〜中（2〜3 日、ほぼ設定と runbook）

### Stage 5 — 英語宇宙（embedder per universe）

**目的**: 日本語（RURI）の次に英語圏へ。

**実装スケッチ**:

- EN embedder 選定（候補: multilingual-e5, BGE-M3 — [Plans — Embedder Comparison](Plans-Embedder-Comparison.md) の評価手法を再利用）
- embedding service に EN モデルを同居ホスト、宇宙レジストリの `embedder_id` で選択
- **必須の再チューニング 1 回**: 物理ハイパラは RURI の狭い cosine 帯（Phase Q rollout finding）前提でチューニングされている。EN embedder は cosine 分布が違うため、golden corpus 英語版 + Tier 3/7 相当の acceptance + 主要ハイパラ（reach floor / gate 類）の EN プロファイル確定が要る。config に per-embedder プロファイルの置き場を作る
- 既存宇宙の embedder 乗り換えは versioned migration（M00x）+ 再 embed（`rebuild_faiss_from_db.py` の経路流用）で可能だが、v1 では新規宇宙のみ EN とする

**所要**: 中〜大（選定・チューニング込みで 1〜2 週）

### Stage 6 — テナント共有宇宙（v2、スコープ外だが方式は確定）

v1 では作らない。方式だけ確定しておく:

- 個人宇宙 ×50 に加えて **テナント共有宇宙を 1 つ** 追加する
- **読みは 2 宇宙合成**: ambient recall / recall が自宇宙と共有宇宙の両方に問い合わせて結果を合成（[Guides — Multi-Agent](Guides-Multi-Agent.md) の延長）。共有宇宙への自動系アクセスは `passive=true` に寄せて場の摂動を能動的操作に絞る
- **書きは明示的**: 共有宇宙への `remember` は「チームに共有する」という能動的行為。書き込みオーナー 1 原則は共有宇宙でも維持（supervisor 経由で直列化）
- 共有宇宙では Heavy Persona Dominance の組織版（声の大きい 1 人が場を支配）が予想される — Phase N (evaporation) / Phase P (pressure) がここで効く。persona 層はユーザー毎の名前空間化（source/tag）が要る。これらの設計は v2 着手時に別 Plans として起草

## 5. リソース試算

前提: [Operations — Resource Requirements](Operations-Resource-Requirements.md) の実測（41k ノード）からの按分。

レビュー指摘を受けて **GPU 構成 / CPU 構成 / モデル抜き engine の 3 ケースに分解** する。実測（41k ノード、warm backend system RAM ~6.9GB = engine + model 込みの値）からの按分であり、モデル抜き engine 単体の実測はまだない点に注意:

| 項目 | 試算 |
|---|---|
| 個人宇宙 1 つ（5k〜20k ノード想定） | engine RAM ~数百 MB（**モデル抜き** — FAISS ×2 + BM25 + cache。実測 6.9GB からモデル+torch 分を引いた推定値、Stage 1 実装後に実測で確定） |
| embedding service（GPU 構成） | VRAM ~7GB（warm、PyTorch allocator は reserved を返さない）+ system RAM **~5-6GB**（model + torch runtime） |
| embedding service（CPU 構成） | system RAM **~5-6GB**。conversational remember ~250-500ms/件・**recall のクエリ encode を含めた体感は 1-3 秒**で実用範囲、bulk ingest だけ GPU が欲しい非対称運用 |
| テナントホスト RAM | 同時アクティブ 10-15 宇宙 × ~0.5GB + embedding service ~6GB + OS ≒ **16GB 推奨（最低 12GB）** |
| スループット | recall p50 <60ms/宇宙は **in-process GPU warm の実測値**。remote embedder で +1-3ms、CPU embedding では encode がボトルネックになり秒オーダー。宇宙間は完全並列（プロセス独立）なので 50 人で干渉しない |
| cold respawn spike | 同時に多数の宇宙が起床すると startup（FAISS load + BM25 build）が並ぶ。supervisor 側で **同時 spawn 数の上限（例: 3）** を設けて stagger する（朝の始業時刻に 15 宇宙が一斉起床するケースを想定） |
| ディスク | 宇宙 ~25MB@1k 〜 ~240MB@10k ノード。50 宇宙でも数 GB〜十数 GB |

セルフホスト先への推奨スペックは「GPU 1 枚（推奨）+ RAM 16GB + SSD 数十 GB」程度の 1 台。**実装後に Tier 6 相当の実測でこの表を丸ごと差し替える**（[Operations — Resource Requirements](Operations-Resource-Requirements.md) と同じ実測ベース表記に揃える）。

## 6. リスクと留意点

- **embedding service が SPOF になる** — 全宇宙の remember/recall が止まる。ヘルスチェック + 自動再起動（systemd）+ engine 側の明示的エラー（現行の OOM 時と同様、その 1 ターン失敗で留める）。in-process fallback は model load が重いので自動では行わない
- **embedder version mismatch は宇宙を壊す** — 宇宙の計量が変わると全ベクトルが無意味化する。Stage 1 の起動時 version check（manifest + `config.embedding_dim` の二重照合）を必須ガードにする（persist guard と同じ「起動時診断で block」パターン）
- **embedder model artifact の供給が DR の隠れた依存** — 「SQLite だけで復元可能」は同一モデルが入手できる前提。HF から model が消える・依存ライブラリ更新で encode 数値が変わる事態に備え、モデル artifact の pin / ミラーを商用運用の要件にする（§4 Stage 4）
- **supervisor 自体の可用性** — supervisor 落ち = 新規 spawn 不可（既存 backend は自走継続 → dead-man-switch で順次休眠）。systemd 常駐 + 単純さ優先で状態を control plane / ローカルレジストリに持たせ、supervisor はステートレス再起動可能にする
- **同期 encode の loop ブロック** — 現行と等価だが、Remote 化を機に `to_thread` wrap を検討（optional、perf Tier 6 で before/after を取る）
- **NodeState へのユーザー次元追加は不採用** — Roadmap 未実装項目「マルチユーザー状態分離」は本計画で **不要と確定**（宇宙 = DB で分離するため）。engine 内部にユーザー ID を持ち込むと単一規則・物理の対称性に異物が入る
- **acceptance は隔離宇宙で** — 本番 acceptance workflow（secondopinion-MCP 経由）はそのまま。Multiverse 化で「テスト用宇宙を 1 つ spawn して使い捨てる」ことが構造的に容易になる副次効果あり

## 7. 検証計画

1. 既存 suite 全緑維持（`tests/` + `tests/perf/`、default 経路が不変であることの確認）
2. Stage 1: `RemoteEmbedder` の unit（StubEmbedder を service に載せた round-trip）+ real RURI での in-process vs remote の **数値等価テスト** — service 側バッチングは複数 requester をまとめて batch shape が変わるため bit-exact は保証できない（レビュー指摘）。`np.allclose`（atol 明示）+ cosine 差分閾値 + **golden query での top-K 一致** の 3 段で検証する + Tier 6 で latency before/after
3. Stage 2: supervisor の integration — 2 宇宙 spawn → 相互不可視性（宇宙 A の remember が宇宙 B の recall に出ない）→ 休眠 → 再起床でデータ保持
4. Stage 4: backup → 破壊 → restore → FAISS rebuild → 起動時診断 green の DR drill を scripted に
5. Stage 5: golden corpus EN 版で Tier 3/7 相当

## 8. スコープ外（明示）

- physics / observation 層の変更 — ゼロ
- Postgres への data plane 移行 — 不採用（§0）
- チーム共有宇宙 — v2（§4 Stage 6 に方式のみ確定）
- 宇宙間の力・クロス宇宙 retrieval — Multiverse の定義により存在しない
- マルチリージョン / 宇宙のホスト間移動 — 将来（SQLite ファイル + レジストリ更新で原理的には可能）

## 9. 関連ドキュメント

- [Architecture — Concurrency](Architecture-Concurrency.md) — 1 宇宙 1 プロセス原則の根拠（逆方向上書き罠・proxy mode）
- [Operations — Server Setup](Operations-Server-Setup.md) — proxy mode / dead-man-switch（Stage 2 の下敷き）
- [Operations — Resource Requirements](Operations-Resource-Requirements.md) — リソース試算の実測根拠
- [Operations — Migration](Operations-Migration.md) — versioned migration（Stage 5 の embedder 乗り換えで使用）
- [Guides — Per-Project DBs](Guides-Per-Project-DBs.md) — DB 分離運用の現行形（Multiverse の原型）
- [Guides — Multi-Agent](Guides-Multi-Agent.md) — 複数 frontend → 1 engine（Stage 6 の読み合成の下敷き）
- [Plans — Embedder Comparison](Plans-Embedder-Comparison.md) — EN embedder 選定の評価手法
- [Plans — Roadmap](Plans-Roadmap.md)
