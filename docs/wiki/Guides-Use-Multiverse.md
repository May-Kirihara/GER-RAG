# Guide — Multiverse を使う（1人1宇宙・マルチテナント）

> **このページは**: マルチバース機能を **使い始めたい・使いこなしたい** 人向けの入門と解説。コマンドを叩きながら読めます。
> **運用者向け**（商用デプロイ・監視・スケール・DR）は [Operations — Multiverse 運用プレイブック](Operations-Multiverse-Operations.md)。
> **パニック時**（異変に気づいた）は [Multiverse クイック健康チェック](Operations-Multiverse-Quick-Check.md)。

---

## Multiverse とは（1 分で分かる）

**Multiverse** = 1 人 1 宇宙。ユーザーごとに **完全に独立した GaOTTT 記憶空間**（= **宇宙**, universe）を持ち、1 台のホストで **supervisor** という管理プロセスが複数ユーザーの宇宙を自動で運用します。

- **宇宙** = あなた専用の記憶の箱。他の人の記憶は一切混ざらない（プライバシー完全分離）
- **supervisor** = 宇宙の管理人。作成・起動・休眠・削除を自動でやり、API key で「あなたはこの宇宙」とルーティングする
- **1 ホストで複数ユーザー** = GPU / モデルは 1 つを全員で共有、記憶だけ別々（コストがユーザー数ではなくホスト数に比例）

### 既存ガイドとの違い（進化の段階）

| ガイド | 記憶の分離 | 何人で | 管理方法 |
|---|---|---|---|
| [Getting Started](Getting-Started.md) | 1 つだけ | 1 人 | 手動 |
| [Multi-Agent](Guides-Multi-Agent.md) | **共有**（協調したい時） | 1 人・複数 agent | 手動・同じ `data_dir` |
| [Per-Project DBs](Guides-Per-Project-DBs.md) | プロジェクト別 | 1 人 | 手動・env var 切替 |
| **Multiverse（ここ）** | **1 人 1 宇宙** | **複数人** | **supervisor が自動** |

Multiverse は「複数人で 1 ホストを共有したい」「本格的にユーザーごとに独立させたい」時に選ぶ、最も本格的な構成です。

---

## MV0〜MV5 の各機能（何を解決したか）

マルチバースは 6 つの機能（MV0〜MV5）の組み合わせで成り立ちます。**全てを使わなくても OK**。必要な分だけ段階的に導入できます。

### MV0 — manifest：モデル切り替え事故の防止

**解決する問題**: GaOTTT の記憶は特定の AI モデル（RURI）の埋め込みベクトルで作られています。誤って別モデルに切り替えて `recall` すると、ベクトル空間が合わず**記憶が全て無意味**になります。

**何ができるか**: 各宇宙に `manifest.json` という「この宇宙はこのモデルで作った」という身分証を持ちます。起動時に照合し、モデルが違えば** hard stop** します。

**ユーザー視点**: 普段は意識しません。裏側の安全装置。「別モデルに替えたい時は `scripts/rebuild_faiss_from_db.py` で再エンベッドが必要」という案内もエラーメッセージに出ます。

> 詳細: [Operations — Tuning](Operations-Tuning.md) §Multiverse manifest (MV0)

### MV1 — embedding service 分離：GPU コストの共有化

**解決する問題**: 従来は各プロセスが個別に AI モデルをロードしていました。複数ユーザーで運用すると **GPU メモリがユーザー数分必要** になり、すぐ VRAM 枯渇します。

**何ができるか**: `embedding service`（port 7879）という**ホスト共有のサービス**にモデルを 1 つだけロードし、全宇宙がそれを使い回します。

**ユーザー視点**: GPU 1 枚で何十人ものユーザーを支えられます。CPU だけのホストでも動きます（遅いですが）。これが「コストがホスト数に比例する」の正体です。

> 詳細: [Operations — Resource Requirements](Operations-Resource-Requirements.md) §Multiverse MV1

### MV2 — owner lease：二重書き込み事故の防止

**解決する問題**: 同じ宇宙を 2 つのプロセスが同時に開くと、**後から書いた方が前の書き込みを上書き**して記憶を壊します（write-behind 逆方向上書き事故。実運用で 2 度発生）。

**何ができるか**: 各宇宙に「今の正当な持ち主は誰」の lease（貸し借り）を持たせ、1 宇宙 1 書き込みオーナーを**機構で強制**します。2 つ目が開こうとすると `LeaseHeldError` で即停止。

**ユーザー視点**: 普段は意識しません。supervisor 管理下の宇宙は自動的にこの保護が効きます。誤って 2 つの agent を同じ宇宙に向けた時、**安全に止まってくれる**のが恩恵です。

> 詳細: [Operations — Tuning](Operations-Tuning.md) §Multiverse owner lease (MV2)

### MV3 — universe supervisor：複数宇宙の自動管理 ★マルチバース本体

**解決する問題**: 複数ユーザーの宇宙を手動で起動・停止・port 割当するのは破綻します。

**何ができるか**: **supervisor**（port 7880）が宇宙を自動管理します:
- 宇宙の作成（`POST /admin/universes`）→ port 割当 + API key 発行 + ディレクトリ作成
- 宇宙の起動（最初の `/route` リクエストで backend を spawn）
- 宇宙の休眠（idle で順次停止、cold-war dead-man-switch で完全 shutdown）
- 宇宙の削除（`DELETE` → trash 移動 → 物理削除）
- **API key によるルーティング**（`POST /route` に API key を送ると `{url, token}` を返す）

**ユーザー視点**: これがマルチバースの本体。「宇宙を作って、API key をユーザーに渡す」だけで、あとは supervisor が全部面倒を見ます。

> 詳細: [Operations — Multiverse Setup (MV3)](Operations-Multiverse-Setup.md)

### MV4 — control plane：usage 集計・商用 SaaS 向け（optional）

**解決する問題**: 誰がどれくらい使ったか、課金・監査に必要なデータを集計したい。

**何ができるか**: **control plane**（port 7881、Postgres）が usage telemetry を集計します。supervisor が定期的に同期し、control plane が止まっても supervisor は local 自走（degraded mode）。

**ユーザー視点**: **個人・小チームでは不要**。商用 SaaS として複数ホストを運用する時だけ必要。3 つの env（URL + host_id + token）が全て揃った時だけ有効になり、1 つでも欠ければ完全に無効化されます（default 不変）。

> 詳細: [Operations — Control Plane (MV4)](Operations-Control-Plane.md)

### MV5 — backup / DR：記憶のバックアップと災害復旧

**解決する問題**: ハードウェア故障・誤操作で宇宙が消えた時、記憶を復元したい。

**何ができるか**:
- **Litestream**（外部ツール）で各宇宙の `gaottt.db` を継続的に object storage にレプリケート
- supervisor が宇宙の作成 / 削除のたびに Litestream 設定を自動再生成
- **DR 演習スクリプト**（`scripts/dr_drill.py`）で四半期ごとに復元手順をテスト

**ユーザー視点**: 個人運用でも安心のために知っておくべき。**FAISS は決定論的に再構築できる**ので、バックアップ対象は SQLite と manifest の 2 点だけ。`scripts/dr_drill.py` を 1 回走らせれば「自分の宇宙が復元できること」を証明できます。

> 詳細: [Operations — Backup & DR (MV5)](Operations-Backup-Multiverse.md)

---

## 必要なもの（環境・前提）

### ハードウェア（目安）

| 構成 | 同時アクティブ宇宙 | RAM | GPU | Disk |
|---|---|---|---|---|
| 個人（1〜3 人） | 1〜3 | 8 GB+ | 不要（CPU 可） | 数 GB / 人 |
| 小チーム（〜10 人） | 3〜10 | 16 GB+ | 推奨（1 枚） | 数十 GB |
| 商用 SaaS（〜100 人/ホスト） | 10〜100 | 32 GB+ | ほぼ必須 | SSD 推奨 |

> 実測値は [Operations — Resource Requirements](Operations-Resource-Requirements.md) 参照。GPU 1 枚でモデル ~1.5GB、1 宇宙あたりモデル抜き ~0.5GB。

### ソフトウェア

- Python 3.11+（推奨 3.12）
- `uv`（パッケージ管理、pip 禁止）
- GaOTTT 本体（[Getting Started](Getting-Started.md) と同じ手順でインストール済みであること）
- **MV4 を使う場合のみ**: PostgreSQL 13+

### 構成パターンの選び方

| やりたいこと | 必要な MV |
|---|---|
| 自分専用の宇宙を 1 つ作る（それだけ） | MV0 + MV1 + MV3 |
| チームで複数人使う | MV0 + MV1 + MV2 + MV3 |
| + バックアップも欲しい | + MV5 |
| + usage 集計・課金・監査 | + MV4 |

**まずは MV0 + MV1 + MV3 から始める**のが現実的。MV2 は supervisor 管理下なら自動で効く、MV4 / MV5 は必要になってから足せば OK。

---

## セットアップ（初回）

既に GaOTTT 本体はインストール済みとします（[Getting Started](Getting-Started.md)）。

### 1. multiverse root の作成

```bash
mkdir -p ~/.local/share/gaottt-multiverse
chmod 700 ~/.local/share/gaottt-multiverse   # 他ユーザーから見えないように
```

### 2. embedding service の起動（MV1）

モデルをホストに 1 つだけロードする共有サービス:

```bash
# systemd で常駐させる場合（推奨）
# deploy/gaottt-embedder.service を参考に unit file 作成
.venv/bin/python -m gaottt.embedding.service --host 127.0.0.1 --port 7879
```

localhost bind 必須（認証を持たない設計なので、外部露出厳禁）。

### 3. supervisor の起動（MV3）

```bash
export GAOTTT_MULTIVERSE_ROOT=~/.local/share/gaottt-multiverse
export GAOTTT_SUPERVISOR_ADMIN_KEY=$(openssl rand -hex 32)   # 強力な乱数で
echo "ADMIN KEY: $GAOTTT_SUPERVISOR_ADMIN_KEY"                # 必ず記録

.venv/bin/python -m gaottt.server.supervisor
```

`GAOTTT_SUPERVISOR_ADMIN_KEY` が空だと **起動時に fail-fast** します（認証無しの管理 API を絶対に露出しない設計）。

### 4. 最初の宇宙の作成

```bash
curl -X POST http://127.0.0.1:7880/admin/universes \
  -H "Authorization: Bearer $GAOTTT_SUPERVISOR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"owner_label": "me", "embedder_id": "cl-nagoya/ruri-v3-310m"}'
```

応答例:

```json
{"universe_id": "abc123def456", "api_key": "<長い文字列>", "port": 7890}
```

**⚠ `api_key` はこの瞬間に 1 度だけ表示されます。必ず記録。** 紛失した場合は再発行が必要（旧 key は失効）。

### 5. agent 側の設定（Claude Code の例）

```json
{
  "mcpServers": {
    "gaottt": {
      "command": ".venv/bin/python",
      "args": ["-m", "gaottt.server.mcp_server", "--supervisor-url", "http://127.0.0.1:7880"],
      "env": {"GAOTTT_API_KEY": "<ステップ4で受け取った api_key>"}
    }
  }
}
```

**opencode / Codex** の設定は [Tutorial — 3. Connect Your Client](Tutorial-03-Connect-Your-Client.md) 参照。

### 6. 動作確認

```bash
# route 解決（API key → backend URL + token）
curl -X POST http://127.0.0.1:7880/route \
  -H "Content-Type: application/json" \
  -d '{"api_key": "<api_key>"}'
# → {"url": "http://127.0.0.1:7890/mcp", "token": "..."}

# agent から remember → recall を試す
# Claude Code 等で "test memory" を remember し、recall で出ることを確認
```

> 詳細・全クライアント設定: [Operations — Multiverse Setup](Operations-Multiverse-Setup.md)

---

## 日常の使い方

### 基本はいつも通り

マルチバース下でも、`remember` / `recall` / `explore` / `reflect` 等、**MCP ツールの使い方は変わりません**。agent が裏で `/route` を叩いて自分の宇宙に繋いでくれるので、利用者は意識しません。

### 複数 agent で同じ宇宙を共有したい時

> ⚠ **重要**: 同じ宇宙を 2 つ以上のプロセスで同時に開くと `LeaseHeldError` が出ます（MV2 の保護）。
> 同じ宇宙を複数 agent で共有したいなら、**両方とも同じ supervisor + 同じ API key** を使います（proxy mode が 1 つの backend に relay してくれる）。

```json
// agent A の .mcp.json
{"GAOTTT_API_KEY": "<api_key>"}

// agent B の .mcp.json（同じ api_key を使う）
{"GAOTTT_API_KEY": "<api_key>"}
```

> 注意: supervisor の proxy mode では **agent ごとに軽量 shim が立ち上がり、supervisor が detached な HTTP backend を auto-spawn します**。shim に `--spawn-supervisor` を付ければ、local supervisor が停止中の場合は supervisor 自体も detached 起動します（`GAOTTT_MULTIVERSE_ROOT` / `GAOTTT_SUPERVISOR_ADMIN_KEY` 必須、local HTTP URL 限定）。N agents 起動しても engine（cache / FAISS / dream loop）は常に 1 プロセスだけ（[Operations — Server Setup](Operations-Server-Setup.md) §起動モード）。

### 別プロジェクトで別宇宙が欲しい時

**プロジェクトごとに別宇宙を作ります**（Per-Project DBs のマルチバース版）:

```bash
# プロジェクト A 用の宇宙
curl -X POST http://127.0.0.1:7880/admin/universes \
  -H "Authorization: Bearer $GAOTTT_SUPERVISOR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"owner_label": "project-a", "embedder_id": "cl-nagoya/ruri-v3-310m"}'
# → {"api_key": "<key-A>", ...}

# プロジェクト B 用の宇宙
curl -X POST http://127.0.0.1:7880/admin/universes \
  -H "Authorization: Bearer $GAOTTT_SUPERVISOR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"owner_label": "project-b", "embedder_id": "cl-nagoya/ruri-v3-310m"}'
# → {"api_key": "<key-B>", ...}
```

各 agent の `.mcp.json` で対応する api_key を使えば、プロジェクト A の記憶と B の記憶は**完全に独立**します。

### 宇宙の削除（退会・データ削除）

```bash
curl -X DELETE http://127.0.0.1:7880/admin/universes/<universe_id> \
  -H "Authorization: Bearer $GAOTTT_SUPERVISOR_ADMIN_KEY"
```

- 即時物理削除ではなく `trash/` へ移動（猶予期間あり）
- API key は即時失効
- バックアップ（MV5）からも完全削除したい場合は別途手順（[Operations — Backup & DR](Operations-Backup-Multiverse.md) §退出・データエクスポート）

---

## バックアップの確認（MV5）

個人運用でも 1 回はやっておきたい「**自分の宇宙が復元できること**」の確認:

```bash
# DR 演習スクリプトを 1 回走らせる（exit 0 なら OK）
.venv/bin/python scripts/dr_drill.py --root /tmp/gaottt-drill-$(date +%s)
```

期待結果:

```
DR DRILL PASSED
```

これが通れば「SQLite + manifest の 2 点があれば、同一 embedder で engine ごと復元できる」ことが証明されます。

**本格的な継続バックアップ**（Litestream + 定期再生成 cron）は [Operations — Backup & DR (MV5)](Operations-Backup-Multiverse.md) を参照。商用運用前には checklist を全て確認することを推奨。

---

## よくある質問・つまずきポイント

### Q. API key をなくしました

**A.** 再発行しかありません。旧 key は失効します:

```bash
# 古い宇宙を削除して作り直す（記憶は消える）
curl -X DELETE http://127.0.0.1:7880/admin/universes/<old_uid> \
  -H "Authorization: Bearer $GAOTTT_SUPERVISOR_ADMIN_KEY"
# 新しい宇宙を作る
curl -X POST http://127.0.0.1:7880/admin/universes ...
```

**教訓**: api_key は password manager 等に必ず保存。

### Q. `LeaseHeldError` が出る

**A.** 同じ宇宙を 2 つのプロセスで開いています（MV2 の保護）。

- どちらかを止める
- または supervisor が管理する宇宙なら、supervisor を再起動すれば lease が解放される

### Q. recall がずっと空

**A.** 3 つの可能性:

1. **宇宙が空**（まだ `remember` していない）→ 普通に `remember` してみる
2. **FAISS が壊れた**（レア）→ supervisor を止めて `scripts/rebuild_faiss_from_db.py --apply`（[Troubleshooting](Operations-Troubleshooting.md)）
3. **モデルが違う**（manifest 照合で止まるはずだが、warning-only なら通る）→ manifest を確認

### Q. supervisor を再起動したら宇宙が `orphan` になった

**A.** ディスク上の宇宙ディレクトリとレジストリが整合しなかった状態。supervisor は `reconcile()` を起動時に走らせるので、通常は再起動で直ります。それでも直らない場合は [Operations — Multiverse Setup](Operations-Multiverse-Setup.md) §制限事項 を参照。

### Q. 個人で使うだけなら control plane (MV4) は要る？

**A.** **要りません。** MV4 は複数ホストを跨ぐ商用 SaaS 運用向け。3 つの env（URL + host_id + token）を全て設定しない限り完全に無効化されます（default 不変）。

### Q. GPU が無いホストで動く？

**A.** 動きますが、遅いです（recall が数秒かかることも）。本格利用には GPU 1 枚を推奨。CPU fallback の挙動は [Operations — Resource Requirements](Operations-Resource-Requirements.md) §VRAM/RAM 不足時の挙動 参照。

### Q. 複数ホストで分散運用したい

**A.** v1 では **1 ホスト = 1 supervisor**。複数ホストで動かす場合は各ホストで別 supervisor を立ち上げ、control plane (MV4) に向けて接続します。ただし **宇宙はホストを跨がない**（data_dir がホストローカル）。将来のホスト間宇宙移動は [Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md) §スコープ外 に記載。

---

## 次のステップ

- **全部入り運用ガイド**（評価方法・スケール・商用導入）→ [Operations — Multiverse 運用プレイブック](Operations-Multiverse-Operations.md)
- **異変に気づいた時の 1 分チェック** → [Multiverse クイック健康チェック](Operations-Multiverse-Quick-Check.md)
- **ハイパラ調整** → [Operations — Tuning](Operations-Tuning.md)
- **設計思想**（なぜこうなったか）→ [Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md)
- **詰まった時** → [Operations — Troubleshooting](Operations-Troubleshooting.md)

---

## 関連ドキュメント

### 入門・チュートリアル
- [Getting Started](Getting-Started.md) — GaOTTT 自体の最初のインストール（5 分）
- [Tutorial — 3. Connect Your Client](Tutorial-03-Connect-Your-Client.md) — Claude Code / opencode / Codex の登録手順

### 関連 Guides（進化の段階）
- [Multi-Agent Setup](Guides-Multi-Agent.md) — 複数 agent で同じ記憶を共有（単一プロセス）
- [Per-Project DBs](Guides-Per-Project-DBs.md) — プロジェクト別に DB を分ける（手動・env var）
- [As Long-Term Memory](Guides-Use-As-Memory.md) — GaOTTT を長期記憶として使う基本

### 運用・詳細
- [Operations — Multiverse 運用プレイブック](Operations-Multiverse-Operations.md) — 統合運用ガイド
- [Operations — Multiverse Setup (MV3)](Operations-Multiverse-Setup.md) — supervisor 詳細
- [Operations — Control Plane (MV4)](Operations-Control-Plane.md) — Postgres 台帳
- [Operations — Backup & DR (MV5)](Operations-Backup-Multiverse.md) — バックアップ・復旧
- [Operations — Resource Requirements](Operations-Resource-Requirements.md) — VRAM/RAM/Disk 見積もり
- [Operations — Tuning](Operations-Tuning.md) — ハイパラ

### 設計・戦略
- [Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md) — 戦略計画（SoT）
- [Architecture — Overview](Architecture-Overview.md) — 全体アーキテクチャ
