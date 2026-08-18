# Operations — Multiverse 運用プレイブック

> **総合入口** — Multiverse デプロイ (MV3 + MV4 + MV5) を運用するための、コンポーネント横断的なワークフロー・評価方法・チェックリストを一冊にまとめた運用書。
> 本ページは **入口 + ワークフロー + 評価フレームワーク** であり、各コンポーネントの詳細手順・パラメータ表・runbook は個別ページ (下記関連ドキュメント) に委ねる。重複を避け、本ページでは概要 + リンク渡しで構成する。
> 対象読者: テナントホストの運用者・SRE・商用導入を検討している担当者。Linux / systemd / Postgres の基礎知識を前提とする。
> Status: 2026-07-04 / **MV5 完了時点** (MV0〜MV5 実装済み、MV6 英語宇宙は未実装)。

## 関連ドキュメント (SoT)

本ページは以下の個別ページを束ねる **インデックス + 運用フロー** である。深い内容は各ページを参照されたい。

- [Operations — Multiverse Setup (MV3)](Operations-Multiverse-Setup.md) — universe supervisor (port 7880) 本体・multiverse layout・セキュリティモデル
- [Operations — Control Plane (MV4)](Operations-Control-Plane.md) — Postgres-backed control plane (port 7881)・台帳・監査・usage telemetry
- [Operations — Backup & DR (MV5)](Operations-Backup-Multiverse.md) — per-universe の継続バックアップ (Litestream) と災害復旧
- [Operations — Resource Requirements](Operations-Resource-Requirements.md) — VRAM/RAM/Disk 見積もり (MV1 embedding service 分離を含む)
- [Operations — Performance Testing (7-tier)](Operations-Performance-Testing.md) — 7 階層テストスイート (Tier 1/3/6 が本ページと最も関連)
- [Operations — Tuning](Operations-Tuning.md) — 全ハイパラ表 (Multiverse 関連は MV0/MV1/MV2/MV3/MV4/MV5 節)
- [Operations — Troubleshooting](Operations-Troubleshooting.md) — 一般的な障害対応 (`LeaseHeldError`・FAISS 不整合等)
- [Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md) — 戦略計画・設計判断の SoT

---

## §1. 全体アーキテクチャ（コンポーネント役割まとめ）

Multiverse 構成は、従来の単一 engine 構成を **テナントごとの独立宇宙** に切り分け、ホスト共有の embedding model と台帳・バックアップ層を周囲に置いたものである。下図は MV3 の基本構成 ([Multiverse Setup](Operations-Multiverse-Setup.md) §アーキテクチャ) を、control plane (MV4) と litestream replica (MV5) の経路まで拡張したもの。

```
agent (Claude Code / opencode / Codex / ...)
  ↓ stdio
mcp_proxy shim (--supervisor-url http://127.0.0.1:7880)
  ↓ POST /route {api_key}        ← supervisor が key → universe 解決 + backend ensure
  ↓ 返る {url, token}
universe backend (http://127.0.0.1:<7890-7989>/mcp, GAOTTT_BACKEND_TOKEN 必須)
  ↓ HTTP
embedding service (port 7879, MV1)         ← RURI model をホスト共有、全宇宙が参照
                                           
supervisor (port 7880) ── HTTP ──> control plane (port 7881, MV4)
                                   ↓ asyncpg
                                   Postgres 16 (台帳 / 監査 / usage_events)
                                           
litestream daemon (独立 systemd unit) ── replica ──> object storage / file replica
  ↑ 各宇宙の gaottt.db を WAL レプリケーションで継続バックアップ
```

### コンポーネント表

| コンポーネント | port | 役割 | 依存先 | 障害時の影響範囲 | 詳細 |
|---|---|---|---|---|---|
| embedding service (MV1) | 7879 | RURI model をホスト共有し、全宇宙が `RemoteEmbedder` で参照 | なし (model load のみ) | 全宇宙の `remember` / `recall` が停止 | [Server Setup](Operations-Server-Setup.md)「embedding service を分離する」節 |
| universe supervisor (MV3) | 7880 | 宇宙の作成 / クローン / 一覧 / 削除 + `/route` で API key → 宇宙解決 + backend spawn/respawn | embedding service (7879)、multiverse_root、(任意) control plane | 新規 spawn 不可。既存 backend は自走継続 | [Multiverse Setup](Operations-Multiverse-Setup.md) |
| 宇宙 backend (per-universe) | 7890–7989 (動的) | 各宇宙 = 1 つの `mcp_server --transport streamable-http`。`GAOTTT_BACKEND_TOKEN` で保護 | embedding service、(任意) control plane (via supervisor) | 対象テナントの機能停止、supervisor が次 route で respawn | [Multiverse Setup](Operations-Multiverse-Setup.md) |
| control plane (MV4) | 7881 | 台帳・監査・usage telemetry 収集点。`control/` 独立パッケージ、engine コード非接触 | Postgres 16 | degraded mode で supervisor は local 自走、usage は spool 蓄積 | [Control Plane](Operations-Control-Plane.md) |
| Postgres 16 | (control plane 内部) | 7 domain テーブル + `audit_log` を保持 | なし | control plane 機能停止、supervisor は local 自走 | [Control Plane](Operations-Control-Plane.md) §setup |
| litestream daemon | (独立プロセス) | 各宇宙の `gaottt.db` を WAL レプリケーションで継続バックアップ | object storage / file replica | バックアップ停止、data plane は無影響 | [Backup & DR](Operations-Backup-Multiverse.md) |

> 全コンポーネントとも **localhost bind のみ** (信頼境界)。外部公開する場合は前段に認証付き reverse proxy (Caddy / nginx / Cloudflare Access 等) + TLS、または VPN / SSH tunnel を必ず挟むこと ([Multiverse Setup](Operations-Multiverse-Setup.md) §セキュリティモデル)。

---

## §2. 前進依存とライフサイクル（いつ何が要るか）

Multiverse は段階的に導入できる。「どこまで進めるか」は、運用する構成と必要な能力で決まる。

| 構成 | 必要なコンポーネント | 必要なドキュメント |
|---|---|---|
| **single-user** (従来構成) | 単一 engine (proxy / stdio / streamable-http) | [Server Setup](Operations-Server-Setup.md) のみ |
| **single-tenant local** (1 ホスト複数宇宙、台帳不要) | embedding service (MV1) + supervisor (MV3) | [Server Setup](Operations-Server-Setup.md) embedding service 節 + [Multiverse Setup](Operations-Multiverse-Setup.md) |
| **single-tenant + control plane** (台帳・監査・usage 収集が要る) | 上記 + control plane (MV4) + Postgres | + [Control Plane](Operations-Control-Plane.md) |
| **commercial with backup** (商用、DR 必須) | 上記 + litestream daemon (MV5) + 定期再生成 cron | + [Backup & DR](Operations-Backup-Multiverse.md) |

依存関係は単調 (左 → 右へ進むほど成分が増える) で、**default は全て inert** である。各 knob が空文字列 / 未設定のとき、該当機能は 1 行も変わらず動かない ([Tuning](Operations-Tuning.md) 各 Multiverse 節の「default 不変」記述)。つまり single-user 構成から commercial 構成へ、既存設定を壊さず段階的に移行できる。

**スコープ外 (v1 では扱わない)**:
- **MV6 英語宇宙** — embedder per universe による多言語対応は未実装 ([Plans](Plans-Multiverse-Scale-Out.md) §Stage 5)。現状の RURI v3 は日本語特化・cross-lingual ではない ([Troubleshooting](Operations-Troubleshooting.md)「英語クエリで日本語の記憶がヒットしない」)。
- **テナント共有宇宙 (v2)** — 個人宇宙に加えて共有宇宙を 1 つ追加する方式は [Plans](Plans-Multiverse-Scale-Out.md) §Stage 6 に方式のみ確定 (実装は v2)。

---

## §3. 初回セットアップ（順序付き）

セットアップには **依存順序がある**。下記の番号順に実施すること。各ステップの詳細手順・パラメータは個別ページを参照。

1. **ハードウェア見積もり** — GPU/CPU/RAM/Disk を見積もる。[Resource Requirements](Operations-Resource-Requirements.md) の実測表を参照。テナントホストは RAM 16GB 推奨 (最低 12GB)・GPU 1 枚推奨・disk 数十 GB (§6.4 参照)。同時アクティブ宇宙数を見積もり、`supervisor_spawn_concurrency` (default 3) との兼ね合いを確認。
2. **Postgres を用意する** (商用の場合) — 外部 Postgres 16+ インスタンスを用意し、DSN を `CONTROL_DATABASE_URL` に設定。開発用には `control/compose.yml` の disposable Postgres (port 55432、volume なし) が使える。詳細: [Control Plane](Operations-Control-Plane.md) §setup。
3. **control plane 起動 + host 登録** — `CONTROL_ADMIN_KEY` (空 = fail-fast) を設定して `python -m control` を起動。`POST /admin/hosts` で host を登録し、**平文 token を一度だけ受け取る** (DB には SHA-256 hash のみ保存)。手順: [Control Plane](Operations-Control-Plane.md) §setup。
4. **embedding service 起動** — `python -m gaottt.embedding.service --host 127.0.0.1 --port 7879`。**非 localhost bind は拒否** (認証を持たない service)。systemd 雛形は `deploy/gaottt-embedder.service`。詳細: [Server Setup](Operations-Server-Setup.md) embedding service 節。⚠ ハマりどころ: service が起動していないと宇宙作成 (`GET /info` 検証) が 400 で失敗する ([Multiverse Setup](Operations-Multiverse-Setup.md) §トラブルシューティング)。
5. **supervisor 起動** (3-point gate + admin key) — `GAOTTT_MULTIVERSE_ROOT`・`GAOTTT_SUPERVISOR_ADMIN_KEY` (空 = fail-fast)・`GAOTTT_EMBEDDER_ENDPOINT` を設定。MV4 連携する場合は 3-point gate (`GAOTTT_CONTROL_PLANE_URL` + `GAOTTT_CONTROL_HOST_ID` + `GAOTTT_CONTROL_HOST_TOKEN`) を追加。手順: [Multiverse Setup](Operations-Multiverse-Setup.md) §セットアップ手順。
6. **multiverse root の permission 確認** — supervisor の lifespan が `multiverse_root` を 0700 に chmod する。`manifest.json` / `owner.lock` / `backend.token` は 0600。事前に親ディレクトリの permission は呼び出し側の責任。詳細: [Multiverse Setup](Operations-Multiverse-Setup.md) §セキュリティモデル。
7. **宇宙作成** — `POST /admin/universes` で新規作成。応答の `api_key` (平文、発行時に一度だけ) を記録。registry には SHA-256 hash で保存。手順: [Multiverse Setup](Operations-Multiverse-Setup.md) §3。⚠ 紛失時は再発行 API または宇宙再作成。
8. **agent 側 shim 設定** — `.mcp.json` (Claude Code) / opencode / Codex で `--supervisor-url http://127.0.0.1:7880` と `GAOTTT_API_KEY` を設定。手順: [Multiverse Setup](Operations-Multiverse-Setup.md) §4 + [Server Setup](Operations-Server-Setup.md) 各クライアント節。
9. **動作確認** — supervisor 稼働確認 (`GET /admin/universes`)・route 解決 (`POST /route`)・token なし直叩きで 401 を確認。他テナントの宇宙への干渉がないこと (宇宙 A の `remember` が宇宙 B の `recall` に出ない)。手順: [Multiverse Setup](Operations-Multiverse-Setup.md) §5。
10. **バックアップ設定** (商用の場合) — `GAOTTT_LITESTREAM_CONFIG_PATH` を設定して supervisor hook を有効化。litestream daemon を独立 systemd unit で起動 (credential は supervisor env と分離)。定期再生成 cron を併用し、新規宇宙の config 取りこぼしを防ぐ。手順: [Backup & DR](Operations-Backup-Multiverse.md) §supervisor hook。

> **順序依存**: Postgres → control plane → supervisor → 宇宙、の順が必須。control plane が未起動でも supervisor 自体は起動するが (degraded)、3-point gate が揃わないと台帳同期が始まらない。各ステップで詰まった場合は対応する詳細ページのトラブルシューティング節へ ([Control Plane](Operations-Control-Plane.md) §トラブルシューティング / [Multiverse Setup](Operations-Multiverse-Setup.md) §トラブルシューティング / [Troubleshooting](Operations-Troubleshooting.md))。

---

## §4. 日常運用タスク（カレンダー）

| 頻度 | タスク | 確認方法 / コマンド | 詳細 |
|---|---|---|---|
| **都度** | usage / auth 状態の確認 | `curl http://127.0.0.1:7880/admin/status -H "X-Admin-Key: $GAOTTT_SUPERVISOR_ADMIN_KEY"` で `auth_failed` / `spool_pending` を確認 | [Control Plane](Operations-Control-Plane.md) §監視 |
| **日次** | backend 自然休眠の確認・disk 使用量の確認 | `du -sh <multiverse_root>`。idle watchdog (default 5 分) で休眠した backend は再 route で respawn | [Multiverse Setup](Operations-Multiverse-Setup.md) |
| **週次** | `compact()` の実行・FAISS size drift の確認 | `compact()` を MCP 経由で実行。起動時診断 Tier B `[diagnostics:tier_b_*]` ログで 5% drift を超える WARN がないか | [Compact & Backup](Operations-Compact-And-Backup.md) / [Troubleshooting](Operations-Troubleshooting.md) §起動時異常 |
| **月次** | litestream replica の健全性・control plane audit log 抽出保管 | `litestream generations` で snapshot が最新か確認。audit_log を外部保管先へ抽出 | [Backup & DR](Operations-Backup-Multiverse.md) / [Control Plane](Operations-Control-Plane.md) |
| **四半期** | **DR drill 実行**・(商用では) litestream 厳格検証 | `.venv/bin/python scripts/dr_drill.py` で exit 0 を確認。商用では手動 WAL restore e2e を 1 回 | [Backup & DR](Operations-Backup-Multiverse.md) §DR drill / §商用導入前チェックリスト |

> 宇宙あたりの disk quota は v1 では組み込み機能が無い。必要なら OS レベル (XFS project quota 等) で設定する。

---

## §5. 監視すべきメトリクス

| メトリクス | 正常範囲 | 異常時の原因 | 確認方法 / コマンド | 詳細 |
|---|---|---|---|---|
| recall p50 / p95 / p99 | p50<60ms / p95<120ms / p99<250ms (real RURI、200 doc 観測値は p50 ~35ms / p95 ~56ms / p99 ~85ms) | hot path の O(N²)・config default 変更・FAISS drift | `tests/perf/` Tier 6 / `scripts/perf_baseline.py` | [Performance Testing](Operations-Performance-Testing.md) §観測される real RURI 数値 |
| 宇宙あたりの RAM (model 抜き) | ~数百 MB (FAISS ×2 + BM25 + cache + SQLite + uvicorn)。大規模 DB では +2GB 程度 (100k node) | DB 肥大化・BM25/FAISS の RAM 圧迫 | `ps -o rss= -p <engine_pid>` (MV1 分離構成での計測手順) | [Resource Requirements](Operations-Resource-Requirements.md) §Multiverse MV1 |
| 同時アクティブ宇宙数 vs spawn concurrency 上限 | 同時 spawn は `supervisor_spawn_concurrency` (default 3) 以下 | 朝の集中起床で readiness timeout | supervisor log の spawn semaphore・`supervisor_readiness_timeout` (default 90s) | [Tuning](Operations-Tuning.md) §Multiverse supervisor (MV3) |
| control plane usage spool pending | `spool_pending` が増え続けない (復旧で drain する) | control plane 長期間不可・permanent auth failure (401) | `GET /admin/status` の `control.spool_pending` | [Control Plane](Operations-Control-Plane.md) §監視 |
| litestream replica の世代 | `litestream generations` で最新 snapshot が存在・古すぎない | litestream daemon 停止・object store 到達不能 | `litestream generations` / `litestream databases` | [Backup & DR](Operations-Backup-Multiverse.md) |
| FAISS size vs SQLite active | drift < 5% | write-behind 逆方向上書き・`compact(rebuild_faiss=True)` 未実行 | 起動時診断 Tier B `[diagnostics:tier_b_*]` ログ・`scripts/verify_faiss_recovery.py` | [Troubleshooting](Operations-Troubleshooting.md) §FAISS と SQLite のカウントが合わない |
| disk 残量 | 宇宙 disk + replica disk + control plane Postgres に十分な空き | node 蓄積・usage spool 肥大化・WAL bloat | `df -h`・`du -sh <multiverse_root>`・spool dir のサイズ | [Resource Requirements](Operations-Resource-Requirements.md) §DB サイズの増え方 |

> **監視の容易性**: supervisor / control plane は localhost のみ bind するため、外部 monitor 向けの metric endpoint を expose していない (将来課題)。現状は **cron + log watch** が基本。Prometheus 等の外部監視を導入する場合は、metric を抽出する轻量 exporter を別途立てるか、supervisor の `/admin/status` を scrape する sidecar を検討すること。

---

## §6. 評価方法（商用投入前・定期評価）

Multiverse を商用投入する前、および定期評価で、性能・信頼性・セキュリティ・コストの 4 軸で検証する。各軸の検証方法を以下にまとめる。

### §6.1 性能評価

[Performance Testing](Operations-Performance-Testing.md) の **Tier 1 (smoke)** / **Tier 3 (quality)** / **Tier 6 (latency)** を、新機能・hot path 変更時に手動実行する。real RURI で **p50<60ms / p95<120ms / p99<250ms / ingest>500 docs/sec** を目安とする (観測値は p50 ~35ms / p95 ~56ms / p99 ~85ms / ingest ~1200 docs/sec、[Performance Testing](Operations-Performance-Testing.md) §観測される real RURI 数値 より引用)。

```bash
.venv/bin/python -m pytest tests/perf/ -q                 # 全 38 tests / real RURI / ~15s
.venv/bin/python scripts/perf_baseline.py --label before
# ... 変更 ...
.venv/bin/python scripts/perf_baseline.py --label after
.venv/bin/python scripts/perf_diff.py                     # >25% regression で exit 1
```

**Multiverse 固有の観点**: 宇宙間干渉が無いこと (宇宙 A の `remember` が宇宙 B の `recall` に出ない) を、supervisor integration test 相当で検証する。独立した宇宙の `data_dir` / FAISS / cache を持つ構造的分離 ([Multiverse Setup](Operations-Multiverse-Setup.md) §概要) が性能評価でも維持されていることを確認する。

### §6.2 信頼性評価

- **DR drill (四半期実行)** — `.venv/bin/python scripts/dr_drill.py` を本番とは別の tmp root で実行し、exit 0 を確認する。「standalone 宇宙で `gaottt.db` + `manifest.json` + embedder artifact が揃えば、FAISS rebuild → engine.startup → 起動時診断 green → 固定 query の top-1 が復元前後で一致する engine レベル復旧ができる」ことを証明する。litestream binary がある環境では `--with-litestream` も検証。詳細: [Backup & DR](Operations-Backup-Multiverse.md) §DR drill。
- **degraded mode 試験** — control plane を意図的に止めて、supervisor が local で自走するか・usage spool が蓄積し復旧後に再送されるかを確認する。network error / 5xx は WARNING + spool 蓄積 + 次回再送、permanent auth failure (401) は `_auth_failed` flag で POST 試行停止・spool 書き込み継続、の区別を検証。詳細: [Control Plane](Operations-Control-Plane.md) §degraded mode / §permanent auth failure。
- **owner lease 試験 (MV2 回帰フェンス)** — 同一宇宙を 2 プロセスで開いて `LeaseHeldError` が出ることを確認する。lease 喪失時 (`LeaseLostError`) は read-only 遷移し、read 系 (`recall(passive=True)` / `get_node` / `reflect`) が継続すること。詳細: [Troubleshooting](Operations-Troubleshooting.md) §LeaseHeldError / LeaseLostError。

### §6.3 セキュリティ評価

- **permission 監査** — `<multiverse_root>` が 0700・`manifest.json` / `owner.lock` / `backend.token` が 0600 であることを、`trash/` 配下も含めて確認する。supervisor の lifespan が chmod するが、親ディレクトリは呼び出し側の責任。詳細: [Multiverse Setup](Operations-Multiverse-Setup.md) §セキュリティモデル。
- **認証境界** — token なし直叩きで 401・admin key 空で起動 fail-fast (`RuntimeError`)・API key が SHA-256 hash 化され平文は発行時のみ返ることを確認する。全認証比較は `secrets.compare_digest` (timing attack 防御)。詳細: [Multiverse Setup](Operations-Multiverse-Setup.md) §セキュリティモデル / [Control Plane](Operations-Control-Plane.md) §認証。
- **credential 分離** — supervisor の env に litestream / object-store の credential (`AWS_*` / `LITESTREAM_*`) が含まれていないことを確認する。litestream daemon は独立 systemd unit + 専用 env file から起動すること。詳細: [Backup & DR](Operations-Backup-Multiverse.md) §credential 分離。
- **信頼境界の明示** — 同一 OS ユーザー内の manifest 改変は v1 では防がない ([Multiverse Setup](Operations-Multiverse-Setup.md) §制限事項)。`managed=false` への書き換え (lease 解除) は runbook の復旧手順としてのみ案内する。root / 同一ユーザーを敵とするモデルは v1 信頼境界外。

### §6.4 コスト評価

[Resource Requirements](Operations-Resource-Requirements.md) の GPU/CPU 別試算表を引用する。テナントホストは **RAM 16GB 推奨 (最低 12GB)・GPU 1 枚推奨・disk 数十 GB** を目安とする。MV1 embedding service 分離により、model 分をユーザー数ではなくホスト数で割れるため、N ユーザー運用では `~5-6GB + N × 数百MB` に削減される (従来 `N × 6.9GB`、[Resource Requirements](Operations-Resource-Requirements.md) §Multiverse MV1 より)。

**セルフホスト SaaS 1 テナント ~50 ユーザー前提での月次コスト見積もり (雛形)**: GPU 1 枚搭載の 1 テナントホスト (RAM 16GB・disk 100GB) + 1 Postgres インスタンス (control plane 用) + object storage (litestream replica 用)。embedding model はホスト共有 1 プロセス。宇宙あたりの disk は ~1GB 程度 (10k node 規模、[Resource Requirements](Operations-Resource-Requirements.md) §DB サイズの増え方) を想定すると 50 宇宙で ~50GB。**この雛形は実測で上書きすること** — GPU 種別・クラウド料金・テナントの平均 node 数・backup retention によって実コストは大きく変動する。

---

## §7. スケールアクション（宇宙追加・ホスト追加）

### 宇宙追加

1. `POST /admin/universes` (admin key) で新規作成 → `universe_id` / `api_key` (平文、一度だけ) / `port` が返る。
2. `api_key` をユーザーに交付 (安全な経路で)。
3. ユーザーの shim 設定 (`.mcp.json` / opencode / Codex) に `--supervisor-url` + `GAOTTT_API_KEY` を設定。

詳細: [Multiverse Setup](Operations-Multiverse-Setup.md) §3 + §4。**注意**: port range 7890–7989 (100 本) が 1 ホストあたりの宇宙数上限 (v1 制約)。枯渇すると宇宙作成が 503 で失敗する。不要宇宙は `DELETE /admin/universes/{id}` で `trash/` へ移動 (即時物理削除しない)。

### 宇宙クローン

既存宇宙をテンプレートや検証用 sandbox として複製する場合は、`POST /admin/universes/{source_universe_id}/clone` (admin key) を使う。新しい `universe_id` / `api_key` / `port` が発行され、作成時点までの SQLite と FAISS を継承する。その後のデータと認証情報は完全に独立する。整合した snapshot を得るため元 backend は一度正常停止され、次の `/route` で再起動する。操作例と failure code は [Multiverse Setup](Operations-Multiverse-Setup.md) §3.1 を参照。

### ホスト追加

同じ control plane に向けて別ホストで supervisor を起動 → host 登録 → `host_id` と `host_token` (平文、一度だけ) を発行。**宇宙はホストを跨がない** — `data_dir` がホストローカルだからである。将来のホスト間宇宙移動は SQLite ファイル + レジストリ更新で原理的には可能だが、v1 では未サポート ([Plans](Plans-Multiverse-Scale-Out.md) §8 スコープ外)。

> **★ 同一 host_id で token を rotate すること**: 新ホスト登録ではなく既存ホストの token 差し替えは `POST /admin/hosts/{hid}/rotate-token` を使う (revoke 解除 + 新 token hash を 1 txn で)。新規 host 登録すると別 host_id が発行され、`universes.host_id` 行が orphan する ([Control Plane](Operations-Control-Plane.md) §復旧手順)。

### 注意: cold respawn spike

同時 spawn 上限 (`supervisor_spawn_concurrency` default 3) を超える朝の集中起床で、`supervisor_readiness_timeout` (default 90s) が出ないかを想定する。宇宙数が多い場合は同時 spawn 上限を上げる調整を検討する ([Tuning](Operations-Tuning.md) §Multiverse supervisor (MV3))。

---

## §8. 縮退運用（コンポーネント障害時）

各コンポーネントが落ちた時の挙動と影響、復旧の方向性を表で示す。詳細な復旧手順は対応する詳細ページを参照。

| 障害コンポーネント | 挙動・影響 | 復旧の方向性 | 詳細 |
|---|---|---|---|
| **embedding service** (7879) | 全宇宙の `remember` / `recall` 停止。engine は明示的エラーで 1 ターン失敗 | systemd `Restart=always` で再起動。VRAM 不足時は `batch_size` 縮小または CPU fallback。**開発機（systemd 無し）構成では supervisor が lazy spawn する** — embedder が落ちている状態で次回 `/route` が来ると supervisor が `/healthz` で検知して再 spawn する（`supervisor_spawn_embedder=True`、default 有効） | [Resource Requirements](Operations-Resource-Requirements.md) §VRAM/RAM 不足時 / [Server Setup](Operations-Server-Setup.md) / [Tuning](Operations-Tuning.md) §MV3 follow-on |
| **supervisor** (7880) | 新規 spawn 不可。既存 backend は自走継続、idle で順次休眠 | systemd 常駐推奨。supervisor 自体はステートレス (registry は disk) なので再起動可、`reconcile()` が on-disk `universes/` から再構築 | [Multiverse Setup](Operations-Multiverse-Setup.md) / [Backup & DR](Operations-Backup-Multiverse.md) §スコープ |
| **control plane** (7881) | degraded mode で supervisor は local 自走、usage は spool 蓄積。復旧で再送。permanent auth failure (401) は spool 書き込み継続・POST 停止 | network error は自然復旧。401 は同一 host_id で `rotate-token` → env 更新 → supervisor restart | [Control Plane](Operations-Control-Plane.md) §degraded mode / §permanent auth failure |
| **Postgres** | control plane が機能停止。supervisor は control client が none 扱いで local 自走 (3-point gate 未満と同等) | Postgres 復旧後、control plane が再起動し台帳同期が再開。Postgres 自体のバックアップ/復旧は別運用 | [Control Plane](Operations-Control-Plane.md) §制限事項 |
| **litestream daemon** | バックアップ停止。data plane は無影響 | systemd で再起動。replica の健全性を `litestream generations` で確認 | [Backup & DR](Operations-Backup-Multiverse.md) |
| **単一宇宙 backend** クラッシュ | 対象テナントの機能停止。データは保持 | 次 route で supervisor が respawn (データは SQLite + FAISS に保持) | [Multiverse Setup](Operations-Multiverse-Setup.md) §トラブルシューティング |

---

## §9. 退出・データエクスポート（テナント退出・忘れられる権利）

### 宇宙削除

`DELETE /admin/universes/<universe_id>` (admin key) で、supervisor が backend 停止を確認 (port probe timeout 付き) してから `<multiverse_dir>` を `trash/` へ move する。**即時物理削除しない** (猶予付き、[Multiverse Setup](Operations-Multiverse-Setup.md) §6)。猶予期間後に物理削除する。

### データエクスポート

各宇宙の `gaottt.db` (SQLite ファイル) を直接 copy すれば、データポータビリティとしてのエクスポートになる。FAISS は `scripts/rebuild_faiss_from_db.py` で同一 embedder artifact から決定論再構築可能 ([Backup & DR](Operations-Backup-Multiverse.md) §概要)。

### バックアップからの完全削除

litestream replica と manifest backup からも削除する必要がある。具体的には: (a) 該当宇宙の litestream replica ディレクトリを削除、(b) manifest backup 経路 (filesystem snapshot / `exec` mirror / rsync のいずれか) から該当宇宙の manifest を削除。**replica からの完全削除は retention 設定に依存する** — litestream の世代管理が古い世代をいつまで保持するかで、物理的に完全に消えるまでのラグが変わる。商用では法務要件 (GDPR 等の「忘れられる権利」) とすり合わせ、retention 設定と削除手順を文書化すること。

control plane 側の `audit_log` は法令遵守のために保持するか別途判断する。control 側 `DELETE /admin/universes/{uid}` は台帳 row の論理削除 (`status='deleted'`) のみで物理削除ではない (J5、[Control Plane](Operations-Control-Plane.md) §権限モデル)。

---

## §10. 既知の制約と将来ロードマップ

- **100 宇宙 / ホスト上限** — port range 7890–7989 (100 本)。枯渇すると宇宙作成が 503 ([Multiverse Setup](Operations-Multiverse-Setup.md) §制限事項)。
- **REST 経路の宇宙提供なし** — managed 宇宙は owner lease で二重 engine を構造的に拒否するため、宇宙に露出する経路は MCP のみ。テナント管理者の操作は supervisor admin API で提供 ([Multiverse Setup](Operations-Multiverse-Setup.md) §制限事項)。
- **NFS / CIFS 非サポート** — lease 機構 (`O_EXCL` / `fcntl.flock` / `os.replace`) が POSIX semantics に依存するため、local FS のみ ([Multiverse Setup](Operations-Multiverse-Setup.md) §制限事項 / [Troubleshooting](Operations-Troubleshooting.md) §LeaseHeldError)。
- **同一 OS ユーザー内の manifest 改変は信頼境界外** — root / 同一ユーザーを敵とするモデルは v1 で守らない ([Multiverse Setup](Operations-Multiverse-Setup.md) §制限事項)。
- **usage は `/route` 解決回数 telemetry** (operation count ではない) — billing-grade の正確な operation count は MV4.1 で導入予定 (J1=A、[Control Plane](Operations-Control-Plane.md) §usage telemetry の意味)。
- **MV6 英語宇宙** (未実装) — embedder per universe による多言語対応 ([Plans](Plans-Multiverse-Scale-Out.md) §Stage 5)。現状 RURI v3 は cross-lingual ではない。
- **v2: テナント共有宇宙** — 個人宇宙に加えて共有宇宙を 1 つ追加する方式は [Plans](Plans-Multiverse-Scale-Out.md) §Stage 6 に方式のみ確定。

---

## §11. 商用導入前チェックリスト（統合版）

商用環境へ Multiverse を導入する前の総合チェックリスト。MV5 固有のバックアップ確認項目 (litestream 厳格検証・新規宇宙取りこぼし確認・credential 分離等) は **[Backup & DR](Operations-Backup-Multiverse.md) §商用導入前チェックリスト を参照** し、ここでは重複を避け Multiverse 全体としての項目を並べる。

- [ ] **ハードウェア見積もり確認** — GPU/CPU/RAM/Disk が [Resource Requirements](Operations-Resource-Requirements.md) の試算を満たす (テナントホスト RAM 16GB 推奨・GPU 1 枚推奨・disk 数十 GB)
- [ ] **Postgres のバックアップ体制** — control plane 用 Postgres のバックアップ/復旧を標準的な商用 DB 運用で別途設定 ([Control Plane](Operations-Control-Plane.md) §制限事項)
- [ ] **multiverse root の permission 監査** — `<multiverse_root>` 0700 / `manifest.json` / `owner.lock` / `backend.token` 0600、`trash/` 配下も含めて ([Multiverse Setup](Operations-Multiverse-Setup.md) §セキュリティモデル)
- [ ] **admin key / host token / API key の secret 管理** — config file に書かず環境変数で渡す。平文 token は発行時のみ記録 ([Multiverse Setup](Operations-Multiverse-Setup.md) §セキュリティモデル / [Control Plane](Operations-Control-Plane.md) §認証)
- [ ] **embedding service の systemd `Restart=always`** — 障害時の自動再起動 ([Server Setup](Operations-Server-Setup.md) embedding service 節)
- [ ] **supervisor の systemd 常駐** — supervisor 自体はステートレスだが常駐推奨 ([Multiverse Setup](Operations-Multiverse-Setup.md) §セットアップ手順)
- [ ] **litestream daemon の独立 systemd unit + credential 分離** — supervisor env に `AWS_*` / `LITESTREAM_*` を含めない ([Backup & DR](Operations-Backup-Multiverse.md) §credential 分離)
- [ ] **定期再生成 cron の設定** — 新規宇宙の litestream config 取りこぼしを防ぐ ([Backup & DR](Operations-Backup-Multiverse.md) §新規宇宙が litestream config に現れるまでのラグ)
- [ ] **宇宙あたりの disk quota** (任意) — v1 は組み込み機能が無いので OS レベル (XFS project quota 等) で設定
- [ ] **monitoring / alerting の設定** — 現状は cron + log watch。`GET /admin/status` の `auth_failed` / `spool_pending` を定期監視 ([Control Plane](Operations-Control-Plane.md) §監視)
- [ ] **DR drill を 1 回実施して EXIT=0 を確認** — `scripts/dr_drill.py` ([Backup & DR](Operations-Backup-Multiverse.md) §DR drill)
- [ ] **degraded mode 試験を 1 回実施** — control plane 停止 → local 自走 → usage spool 蓄積 → 復旧で再送 ([Control Plane](Operations-Control-Plane.md) §degraded mode)
- [ ] **商用前の performance baseline 取得** — `scripts/perf_baseline.py` で p50/p95/p99 / ingest throughput を記録 ([Performance Testing](Operations-Performance-Testing.md) §Tier 6 baseline)
- [ ] **audit log の外部保管先確保** — control plane の `audit_log` を定期抽出・保管 ([Control Plane](Operations-Control-Plane.md) §概要)
- [ ] **退出手順の dry-run** — 削除 → `trash/` → 物理削除 → replica からの削除の経路を確認 (§9)
- [ ] **embedder artifact pin の実機確認** — 本番 embedder で HF cache tar 退避 or 社内ミラーが機能し、復旧時に同一数値の encode が得られること ([Backup & DR](Operations-Backup-Multiverse.md) §embedder artifact pinning)
- [ ] **litestream 厳格検証 (手動)** — 実際の litestream binary で WAL replica → `litestream restore` の完全 e2e を本番サイズの dummy 宇宙で実施 ([Backup & DR](Operations-Backup-Multiverse.md) §商用導入前チェックリスト)

---

## §13. 動作確認・回帰確認チェックリスト（技術 smoke + 回帰）

本チェックリストは **技術的な smoke 確認と回帰検出** に特化する — コピペ可能なコマンド・期待結果・失敗時の原因とトラブルシューティングリンクを並べ、MV5 実装（および本 doc 作業）が既存挙動を壊していないかを機械的に検証する。§11（商用導入前チェックリスト = ハードウェア・permission・secret 管理等の運用準備性）と補完関係にあり、内容は重複しない。以下の場面で実施する想定: (a) 初回セットアップ直後、(b) MV3/MV4/MV5 関連 code 変更後、(c) 四半期サインオフ前。グループ内で実行順序が意味を持つ場合があるため、その都度注記する。

### §13.1 前提

repo root に稼働中の `.venv` があること、multiverse root が環境変数 `MV_ROOT` で指定されていること、supervisor admin key が `SUP_KEY` に入っていること、少なくとも 1 つのテスト宇宙が作成済みであること。litestream 関連の確認 (§13.3) では追加で `LITESTREAM_CONFIG_PATH` が設定済みであること。route 解決の確認では agent 用 API key が `API_KEY` に入っていること。全コマンドは特に断りのない限り repo root から実行する。

```bash
export MV_ROOT=${GAOTTT_MULTIVERSE_ROOT:-$HOME/.local/share/gaottt-multiverse}
export SUP_KEY=$GAOTTT_SUPERVISOR_ADMIN_KEY
export API_KEY=<宇宙作成時に一度だけ受け取った平文 api_key>
export LITESTREAM_CONFIG_PATH=$GAOTTT_LITESTREAM_CONFIG_PATH  # knob 設定時のみ
```

### §13.2 A. 基本動作確認（smoke — 初回セットアップ後・変更直後）

multiverse の基本経路が通っているかの smoke である。実行順序は 1→8 を推奨 (前者が後者の前提になるため)。

1. **パッケージ整合** — multiverse 関連モジュールが ImportError なく import できること。
   ```bash
   .venv/bin/python -c "import gaottt; import gaottt.multiverse.backup; import gaottt.multiverse.supervisor"
   ```
   - 期待結果: 何も出力せず exit 0。
   - 失敗時: `uv sync` 忘れ ([Server Setup](Operations-Server-Setup.md) §インストール)。`gaottt.multiverse` が見つからない場合は該当 phase 未実装。

2. **supervisor プロセス起動** — admin API が 200 + JSON で応答すること (supervisor は `X-Admin-Key` と `Authorization: Bearer` 両方を受け付ける)。
   ```bash
   curl -fsS -H "Authorization: Bearer $SUP_KEY" http://127.0.0.1:7880/admin/status | jq .
   ```
   - 期待結果: `control` / `universes` 等の key を持つ JSON が表示され、exit 0。
   - 失敗時: admin key 空 / systemd unit 未起動 / port bind 失敗 ([Multiverse Setup](Operations-Multiverse-Setup.md#トラブルシューティング))。

3. **宇宙一覧が取れる** — active 宇宙が一覧で取れること。
   ```bash
   curl -fsS -H "Authorization: Bearer $SUP_KEY" http://127.0.0.1:7880/admin/universes | jq '.[] | {universe_id, status, port}'
   ```
   - 期待結果: テスト宇宙が `status: "active"` で 1 行以上表示される。
   - 失敗時: on-disk 宇宙が registry から消え `orphan` 化 ([Multiverse Setup](Operations-Multiverse-Setup.md#制限事項-v1))。

4. **route 解決（token 取得）** — API key → `{url, token}` 解決が通ること。`/route` は POST + JSON body (`{"api_key": ...}`) で API key を受け取る (Authorization header ではない)。
   ```bash
   curl -fsS -X POST http://127.0.0.1:7880/route \
       -H "Content-Type: application/json" \
       -d "{\"api_key\": \"$API_KEY\"}" | jq '{url, token_present: (.token != null)}'
   ```
   - 期待結果: `{"url": "http://127.0.0.1:<port>/mcp", "token_present": true}`。
   - 失敗時: API key revoke / backend spawn 失敗 ([Control Plane](Operations-Control-Plane.md#permanent-auth-failure-401))。

5. **宇宙 backend が recall 応答** — backend が MCP recall にエラーなく応答すること。経路 a (agent 経由) が難しければ経路 b (backend 直叩き) で確認する。
   ```bash
   # 経路 a: agent 経由 (shim 設定済みの場合)
   #   recall(query="smoke", top_k=1) が空でもエラーなく返ることを確認
   # 経路 b: route で得た url + token で backend の MCP endpoint を叩く
   curl -fsS -H "Authorization: Bearer $BACKEND_TOKEN" "$BACKEND_URL" \
       -X POST -H "Content-Type: application/json" \
       -d '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"recall","arguments":{"query":"smoke","top_k":1}},"id":1}'
   ```
   - 期待結果: 200 + JSON-RPC response (結果空でも可)。結果が空でない場合は item が 1 件。
   - 失敗時: engine startup 失敗 / lease 競合 ([Troubleshooting](Operations-Troubleshooting.md) §LeaseHeldError)。

6. **宇宙 disk の基本構造** — 各宇宙の必須ファイルが揃い permission が正しいこと。
   ```bash
   ls -la $MV_ROOT/universes/<uid>/
   ```
   - 期待結果: `gaottt.db` / `manifest.json` / `owner.lock` / `backend.token` が揃い、`manifest.json` / `owner.lock` / `backend.token` が `0600`、ディレクトリが `0700`。
   - 失敗時: permission 設定漏れ ([Multiverse Setup](Operations-Multiverse-Setup.md#セキュリティモデル))。

7. **multiverse root の permission** — root が `0700` であること。
   ```bash
   stat -c '%a' $MV_ROOT
   ```
   - 期待結果: `700`。
   - 失敗時: 他 OS ユーザーから読まれるリスク ([Multiverse Setup](Operations-Multiverse-Setup.md#セキュリティモデル))。

8. **litestream 設定の存在（knob 設定時のみ）** — knob が設定されていれば config ファイルが存在し YAML 先頭が読めること。
   ```bash
   test -n "$LITESTREAM_CONFIG_PATH" && test -f "$LITESTREAM_CONFIG_PATH" && head -5 "$LITESTREAM_CONFIG_PATH"
   ```
   - 期待結果: `dbs:` で始まる YAML が 5 行表示される (knob 未設定なら何も出ず exit 0 でスキップ扱い)。
   - 失敗時: supervisor hook が未発火 / cron 未設定 ([Backup & DR](Operations-Backup-Multiverse.md#⚠-新規宇宙が-litestream-config-に現れるまでのラグ))。

### §13.3 B. MV5 backup hook の健全性（knob 設定時）

前提: `LITESTREAM_CONFIG_PATH` が空でない (litestream 連携が有効)。未設定環境では項目 1 のみ実施し、default 不変フェンスとして機能させる。

1. **knob 未設定時は何も起きない（default 不変の回帰フェンス）** — knob 空 + 宇宙作成で YAML が作成されないこと。
   ```bash
   .venv/bin/python -m pytest tests/integration/test_supervisor_backup_hook.py::test_knob_unset_creates_no_yaml -v
   ```
   - 期待結果: `PASSED`。
   - 失敗時: default 不変崩壊 (高優先)。MV5 実装の `litestream_config_path` 空文字列判定を見直す ([Tuning](Operations-Tuning.md) §Multiverse MV5)。

2. **create → YAML に含まれる** — 宇宙作成 + 初回 route 後、定期 cron 周期 (推奨 1 時間、[Backup & DR](Operations-Backup-Multiverse.md#supervisor-hook)) 経過後に生成 YAML にその宇宙の `gaottt.db` path が含まれること。
   ```bash
   grep -c "<uid>/gaottt.db" "$LITESTREAM_CONFIG_PATH"
   ```
   - 期待結果: `1` 以上。
   - 失敗時: cron 未設定 / supervisor hook の `_ensure_locked` 未発火の窓 ([Backup & DR](Operations-Backup-Multiverse.md#⚠-新規宇宙が-litestream-config-に現れるまでのラグ))。

3. **delete → YAML から消える** — 宇宙削除後、次の cron 実行後に YAML から当該宇宙行が消えること。
   ```bash
   grep -c "<deleted-uid>/gaottt.db" "$LITESTREAM_CONFIG_PATH"
   ```
   - 期待結果: `0`。
   - 失敗時: trash race / cron 未実行 ([Backup & DR](Operations-Backup-Multiverse.md#supervisor-hook))。

4. **並行 create/delete で YAML が破損しない** — 同時 create/delete で on-disk YAML が in-memory state と一致すること。
   ```bash
   .venv/bin/python -m pytest tests/integration/test_supervisor_backup_hook.py::test_concurrent_create_delete_yaml_matches_on_disk -v
   ```
   - 期待結果: `PASSED`。
   - 失敗時: `_backup_hook_lock` 崩壊 (高優先)。MV5 の `asyncio.Lock` 保護を見直す。

5. **spawn env に backup knob が漏れない** — supervisor が backend spawn 時に litestream / object-store credential を渡さないこと。
   ```bash
   .venv/bin/python -m pytest tests/integration/test_supervisor_backup_hook.py::test_spawn_env_does_not_leak_backup_knob -v
   ```
   - 期待結果: `PASSED`。
   - 失敗時: backend へ credential 漏洩の疑い ([Backup & DR](Operations-Backup-Multiverse.md#⚠-litestream--object-store-credential-の分離))。

6. **atomic write が失敗に強い** — config 生成スクリプトが異常終了しても既存 config が破損しないこと。
   ```bash
   .venv/bin/python -m pytest tests/unit/test_gen_litestream_config.py::test_cli_atomic_write_output_survives_generator_failure -v
   ```
   - 期待結果: `PASSED`。
   - 失敗時: 既存 config が半壊するリスク。`tmp + os.replace` 経路を見直す ([Backup & DR](Operations-Backup-Multiverse.md#supervisor-hook))。

### §13.4 C. 回帰確認（全テストスイート）

MV5 実装と本 doc 作業が既存の挙動を壊していないかの門。新規失敗が 0 件であることが合格基準。

1. **新規 MV5 関連テスト全部 green** — litestream config 生成・supervisor backup hook・DR drill の 3 ファイル。
   ```bash
   .venv/bin/python -m pytest tests/unit/test_gen_litestream_config.py tests/integration/test_supervisor_backup_hook.py tests/integration/test_dr_drill.py -v
   ```
   - 期待結果: `27 passed`。
   - 失敗時: MV5 実装の破綻。該当 test を `-x` で最初の失敗から調査。

2. **既存 supervisor テストが default 不変で green** — MV3 由来の supervisor 本体テスト。
   ```bash
   .venv/bin/python -m pytest tests/integration/test_supervisor.py tests/unit/test_supervisor.py -q
   ```
   - 期待結果: `49 passed`。
   - 失敗時: MV3 由来の asyncio teardown race の可能性 (単独実行で green なら環境問題)。
   - 注記: `test_probe_initialize_success_returns_ok` は full-suite で稀に flaky。単独実行で確認:
     ```bash
     .venv/bin/python -m pytest tests/unit/test_supervisor.py::test_probe_initialize_success_returns_ok -v
     ```
     これが通れば pre-existing flaky で MV5 起因ではない ([Troubleshooting](Operations-Troubleshooting.md) §異常終了後の起動)。

3. **full suite** — 全テストを通す。
   ```bash
   .venv/bin/python -m pytest tests/ -q --timeout=180
   ```
   - 期待結果: `1134 passed / 14 skipped / 1 pre-existing flaky`。新規失敗が 0 件であること。
   - 失敗時: 差分を `git diff` で確認、MV5 changed file に起因するかを切り分け。

4. **ruff が新規エラーを出さない** — MV5 関連 file に lint error が無いこと。
   ```bash
   ruff check gaottt/ tests/ scripts/gen_litestream_config.py scripts/dr_drill.py gaottt/multiverse/backup.py
   ```
   - 期待結果: pre-existing 4 件のみ (CLAUDE.md 記載)。MV5 changed file は 0 errors。
   - 失敗時: lint error は直す (unused import / 未使用変数等)。

5. **両 smoke が通る** — REST + MCP の end-to-end smoke (各 7 scenario)。
   ```bash
   .venv/bin/python scripts/rest_smoke.py && .venv/bin/python scripts/mcp_smoke.py
   ```
   - 期待結果: 各 `All scenarios passed.`。
   - 失敗時: server 側の回帰。scenario 名で失敗箇所を特定 ([Server Setup](Operations-Server-Setup.md) §smoke)。

### §13.5 D. DR drill の実行（四半期 or 商用導入前）

DR drill は「standalone 宇宙で SQLite + manifest + embedder artifact が揃えば、FAISS rebuild → engine startup → 固定 query の top-1 が復元前後で一致する」ことを証明する ([Backup & DR](Operations-Backup-Multiverse.md#dr-drill四半期実行))。

1. **基本 drill が EXIT=0** — raw-copy 経路で drill を通す。
   ```bash
   .venv/bin/python scripts/dr_drill.py --root /tmp/gaottt-drill-$(date +%s)
   ```
   - 期待結果: `DR DRILL PASSED`、exit 0、diagnostics `5 checks (0 error, 0 warn, 5 info)`、top-1 が pre/post で一致。
   - 失敗時: embedder artifact 不整合 / FAISS rebuild 失敗 / StubEmbedder の決定論性破綻 ([Backup & DR](Operations-Backup-Multiverse.md#dr-drill四半期実行))。

2. **manifest 漏れ検知 fence** — DB だけ restore して manifest を忘れた場合に RuntimeError で止まること。
   ```bash
   .venv/bin/python -m pytest tests/integration/test_dr_drill.py::test_manifest_missing_on_restore_is_caught -v
   ```
   - 期待結果: `PASSED`。
   - 意味: `verify_embedder_identity` が manifest 無しを検知して RuntimeError で停止する fence の証明。manifest 単独復元漏れが検出される。

3. **litestream 非依存（CI 環境配慮）** — `--with-litestream` 指定でも litestream binary が無ければ raw-copy 経路で drill が通ること。
   ```bash
   .venv/bin/python scripts/dr_drill.py --root /tmp/gaottt-drill-litestream-$(date +%s) --with-litestream
   ```
   - 期待結果: litestream binary 無し環境では ERROR log が出るが drill 自体は EXIT=0 (default raw-copy 経路が本体)。binary 有り環境では snapshot 経路も試行される。
   - 失敗時: litestream binary 実機試験が商用導入前に必要 ([Backup & DR](Operations-Backup-Multiverse.md#商用導入前チェックリスト))。

### §13.6 E. 典型的エンバグ症状と検出方法（早見表）

| 症状 | 検出方法 / コマンド | 原因 | 対処 | 詳細 |
|---|---|---|---|---|
| supervisor が admin API で 401 を返す | `curl -H "Authorization: Bearer $SUP_KEY" http://127.0.0.1:7880/admin/status` が 401 | admin key 誤り / unit file の env 未設定 | env `GAOTTT_SUPERVISOR_ADMIN_KEY` を確認し unit 再読み込み | [Multiverse Setup](Operations-Multiverse-Setup.md#トラブルシューティング) |
| 宇宙一覧に `orphan` が並ぶ | `GET /admin/universes` の `status` が `orphan` | on-disk 宇宙が registry から消えた | supervisor 再起動で `reconcile()` が再構築 | [Multiverse Setup](Operations-Multiverse-Setup.md) |
| backend が spawn しない (route が 503) | `POST /route` が 503 / readiness timeout | embedding service 落下 / port bind / readiness timeout | embedder (7879) と port 空きを確認 | [Multiverse Setup](Operations-Multiverse-Setup.md) |
| recall が空 | `recall(query="...", top_k=3)` が常に空 | FAISS size 0 / 起動時診断 Tier B ERROR | `scripts/verify_faiss_recovery.py` で size 確認 → `rebuild_faiss_from_db.py` | [Troubleshooting](Operations-Troubleshooting.md) |
| `LeaseHeldError` が出る | recall / remember が RuntimeError | 同一宇宙を 2 プロセスで開いた | 片方を停止 / `--force-takeover` で lease 取得 | [Troubleshooting](Operations-Troubleshooting.md) |
| litestream 設定が更新されない | `head $LITESTREAM_CONFIG_PATH` が古い | knob 未設定 / cron 未稼働 | `GAOTTT_LITESTREAM_CONFIG_PATH` と cron を確認 | [Backup & DR](Operations-Backup-Multiverse.md#supervisor-hook) |
| 新規宇宙が litestream 設定に乗らない | 新規宇宙の uid が YAML に無い | `_ensure_locked` hook 未発火の窓 | 定期 cron で次周期に取り込まれる | [Backup & DR](Operations-Backup-Multiverse.md#⚠-新規宇宙が-litestream-config-に現れるまでのラグ) |
| spawn env に AWS_* が漏れる | backend プロセスの `/proc/<pid>/environ` に AWS_* | supervisor unit file に credential | litestream daemon を独立 unit + 専用 env file に分離 | [Backup & DR](Operations-Backup-Multiverse.md#⚠-litestream--object-store-credential-の分離) |
| FAISS size vs SQLite active が 5% 以上ズレる | 起動時診断 Tier B `[diagnostics:tier_b_*]` が WARN/ERROR | write-behind 逆方向上書き | 全プロセス停止 → `rebuild_faiss_from_db.py --apply` | [Troubleshooting](Operations-Troubleshooting.md) |
| full-suite で `test_probe_initialize_success_returns_ok` が 1 件 fail | `pytest tests/` で該当 test のみ fail | asyncio teardown race (MV3 由来) | 単独実行で確認、MV5 起因でなければ別 issue 化 | [Troubleshooting](Operations-Troubleshooting.md) |

### §13.7 終了基準（exit criteria）

以下の 5 項目をすべて満たせば、Multiverse は正常に稼働し MV5 由来の回帰も無いと判断できる。

- **(1) §13.2 全項目 green** — 基本 smoke 8 項目がすべて期待結果通り。
- **(2) §13.3 健全性** — knob 設定環境では全 6 項目 green / 未設定環境では項目 1 (default 不変フェンス) が green。
- **(3) §13.4 回帰確認** — full suite で新規失敗が 0 件 (pre-existing flaky 1 件は許容)。
- **(4) §13.5 DR drill** — 基本 drill が EXIT=0、manifest 漏れ fence が PASS。
- **(5) §13.6 症状表** — 該当する症状が 1 件も無いこと。

これらを満たせば、Multiverse は正常に稼働し MV5 由来の回帰も無いと判断できる。次回の四半期 drill まで安定稼働を想定する。

---

## §12. 関連ドキュメント

### コンポーネント別 SoT (Operations)

- [Operations — Server Setup](Operations-Server-Setup.md) — 単一プロセス server setup・embedding service 分離・各クライアント (Claude Code / opencode / Codex) 登録
- [Operations — Multiverse Setup (MV3)](Operations-Multiverse-Setup.md) — universe supervisor 本体・multiverse layout・セキュリティモデル・制限事項
- [Operations — Control Plane (MV4)](Operations-Control-Plane.md) — Postgres-backed control plane・台帳・監査・usage telemetry・degraded mode・permanent auth failure 復旧
- [Operations — Backup & DR (MV5)](Operations-Backup-Multiverse.md) — per-universe の継続バックアップ (Litestream)・DR runbook・DR drill・商用導入前チェックリスト
- [Operations — Resource Requirements](Operations-Resource-Requirements.md) — VRAM/RAM/Disk 見積もり・MV1 embedding service 分離時の試算
- [Operations — Performance Testing (7-tier)](Operations-Performance-Testing.md) — Tier 1/3/6 latency・quality テスト・baseline 回帰検出
- [Operations — Tuning](Operations-Tuning.md) — 全ハイパラ表 (Multiverse 関連は MV0/MV1/MV2/MV3/MV4/MV5 節)
- [Operations — Troubleshooting](Operations-Troubleshooting.md) — `LeaseHeldError` / FAISS 不整合 / FAISS 逆方向上書き等の障害対応
- [Operations — Compact & Backup](Operations-Compact-And-Backup.md) — `compact()` の定期実行 (standalone 構成向けバックアップ)

### 戦略と計画

- [Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md) — 戦略計画・設計判断の SoT・Stage 1–6・スコープ外 (§8)
- [multiverse-implementation-plan.md](../maintainers/multiverse-implementation-plan.md) — 実装者向け作業計画 (MV0–MV6、ファイル単位の変更一覧・テスト・acceptance)

### 設計判断

- [Architecture — Overview](Architecture-Overview.md) — 全体アーキテクチャ・設計判断の記録表 (「Multiverse supervisor は MCP/REST parity 対象外」等)
- [Architecture — Concurrency](Architecture-Concurrency.md) — マルチプロセス並走の安全性・owner lease の構造的解・write-behind の罠
