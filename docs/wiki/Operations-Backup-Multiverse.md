# Operations — Backup & DR (Multiverse MV5)

> multiverse 構成（MV3〜MV5）における **per-universe の継続バックアップと災害復旧 (DR)** の運用ページ。standalone（単一ユーザー）構成のバックアップは [Operations — Compact & Backup](Operations-Compact-And-Backup.md) を参照。
> 起票: 2026-07-04（MV5 完遂）
> 関連: [Operations — Multiverse Setup](Operations-Multiverse-Setup.md)（MV3）、[Operations — Control Plane](Operations-Control-Plane.md)（MV4）、[Operations — Compact & Backup](Operations-Compact-And-Backup.md)、[Operations — Tuning](Operations-Tuning.md)、[Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md) §Stage 4、[multiverse-implementation-plan.md](../maintainers/multiverse-implementation-plan.md) §MV5、[multiverse-mv5-plan.md](../maintainers/multiverse-mv5-plan.md)

## 概要

MV5 は各宇宙（universe）の `gaottt.db` を [Litestream](https://litestream.io/)（SQLite WAL レプリケーション）で継続バックアップし、災害時に **SQLite + manifest の 2 点セット** から engine レベルの復旧をできるようにする仕組みと手順を提供する。FAISS ファイルは [`scripts/rebuild_faiss_from_db.py`](../../scripts/rebuild_faiss_from_db.py) で **同一 embedder artifact + 同一 version + 同一 prefix/normalization** が揃えば決定論的に再構築可能（2026-05-31 の FAISS reverse-overwrite incident 復旧で実証済み）なので、バックアップ対象は意図的に最小化されている。コード成果物は [Litestream 設定ファイル生成スクリプト](#litestream-雛形の使い方)・[supervisor hook](#supervisor-hook)・[DR drill](#dr-drill-四半期実行) の 3 点で、運用手順（本ページ）が成果物の本体。

## スコープ（per-universe data recovery への明示的限定）

> ★ **境界確定（[multiverse-mv5-plan.md](../maintainers/multiverse-mv5-plan.md) §0 / §1 D4 反映）**: MV5 が扱うのは **per-universe data recovery** のみ。各宇宙の `gaottt.db` + `manifest.json` の 2 点セットだけが復旧対象。

**本 stage のスコープ外**（商用運用の別作業として runbook の人手手順で案内）:

| 対象 | 復旧方法 | 理由 |
|---|---|---|
| `<multiverse_root>/registry.db`（supervisor local registry） | supervisor の `reconcile()` が on-disk `universes/` から再構築。ただし **API key hash の連続性・port 割当履歴は保証されない** → API key 再発行が必須 | registry は index であり source-of-truth ではない（[Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md) §0）。directory が正、registry は突き合わせ |
| MV4 control plane (Postgres) | Postgres 自体の標準バックアップ/復旧運用（別途） | 本 stage は関与しない（設計判断 J5: control plane は aggregator/audit 収集点、運用 deprovisioning 権威なし） |
| `backend.token` | 復旧後の初回 route で supervisor が re-spawn 時に再生成 | 設計どおり（MV3）。旧 token の連続性は保証しない |
| `owner.lock` | 復旧時の初回起動で新 lease が取得される | 旧 lease の owner_id 連続性は保証しない（[MV2](Operations-Multiverse-Setup.md)） |

## バックアップ対象（2 点セット）

| ファイル | 役割 | バックアップ経路 |
|---|---|---|
| `gaottt.db` | 全ノードの content / metadata / mass / displacement / velocity / 共起 edge。**唯一の source-of-truth** | Litestream（SQLite WAL レプリケーション）で継続 replica |
| `manifest.json` | embedder identity（`embedder_id` / `embedder_version`）+ `embedding_dim` + `managed` flag。復旧後の `verify_embedder_identity` 整合ガードの鍵 | **Litestream は対象外**（WAL のみ replicate）。file-level snapshot or `exec` command で別経路必須（下記「Litestream 雛形の使い方」） |

**FAISS ファイル（`gaottt.faiss` / `gaottt.virtual.faiss`）はデフォルトでバックアップ対象外**。理由: (a) SQLite + embedder artifact から決定論再構築可能、(b) WAL stream との衝突回避、(c) RPO/RTO 対象の最小化。大宇宙向けには FAISS snapshot も optional で設定可能（[下記](#大宇宙向け-optional-faiss-snapshot)）。

> ★ **「SQLite だけで復元できる」は前提条件つき**: FAISS 再構築の決定論は **同一 embedder artifact（model weights）+ 同一 version + 同一 prefix/normalization 実装** が揃って初めて成立する。manifest の `embedder_id` / `embedder_version` はその照合キー。モデルが HF から消える・依存ライブラリ更新で encode 数値が変わるリスクに備え、embedder artifact の pin / ミラーは **商用運用の必須要件**（[下記](#embedder-artifact-pinning必須)）。

## Litestream 雛形の使い方

[`deploy/litestream.yml`](../../deploy/litestream.yml) がテンプレート。`dbs:` ブロックは [`scripts/gen_litestream_config.py`](../../scripts/gen_litestream_config.py) が `<multiverse_root>/universes/*/` を scan して生成する（宇宙の増減で再生成）。

```bash
# 手動で設定ファイルを生成（stdout に YAML、diagnostics は stderr）
.venv/bin/python scripts/gen_litestream_config.py --root /var/lib/gaottt-multiverse

# ファイルへ atomic write（supervisor hook が設定時はこれを自動実行）
.venv/bin/python scripts/gen_litestream_config.py \
    --root /var/lib/gaottt-multiverse \
    --output /etc/litestream/gaottt.yml
```

生成される YAML（litestream v0.3 schema）:

```yaml
dbs:
  - path: /var/lib/gaottt-multiverse/universes/<uid>/gaottt.db
    replicas:
      - type: file
        path: /var/lib/litestream/gaottt-multiverse/<uid>
```

**manifest のバックアップ**は litestream と別経路で必須。3 つの選択肢から環境に合うものを選び、runbook に明記すること:

- **(A) filesystem snapshot 推奨** — `<multiverse_root>` 全体を ZFS / Btrfs / LVM snapshot。SQLite と manifest をほぼ同時点に capture でき最も堅牢。embedder identity は不変（宇宙の生存期間中に変わらない）ので、SQLite と manifest の snapshot が数秒ズレても復旧後の照合は成功する。
- **(B) litestream `exec` command** — `deploy/litestream.yml` のコメント例を参照。各 WAL sync 後に `cp -a universes/<uid>/manifest.json` を平行ディレクトリへ mirror。
- **(C) cron rsync** — `universes/*/manifest.json` を定期的に object storage へ sync。

## supervisor hook

supervisor は `litestream_config_path` knob（[Operations — Tuning](Operations-Tuning.md) §MV5）が設定されているとき、`POST /admin/universes`（作成）と `DELETE /admin/universes/{uid}`（削除）の **成功経路の最後** で `generate_litestream_config` 純粋関数を direct import で呼び、atomic write（`tmp + fsync + os.replace`）で設定ファイルを再生成する。

```bash
# supervisor 起動時に litestream 設定ファイルのパスを指定
GAOTTT_LITESTREAM_CONFIG_PATH=/etc/litestream/gaottt.yml \
GAOTTT_MULTIVERSE_ROOT=/var/lib/gaottt-multiverse \
GAOTTT_SUPERVISOR_ADMIN_KEY=... \
.venv/bin/python -m gaottt.multiverse.supervisor
```

設計特性（[multiverse-mv5-plan.md](../maintainers/multiverse-mv5-plan.md) §1 D2）:

- **default 不変**: knob 未設定（空文字列）なら hook は一切呼ばれず、MV3/MV4 構成は 1 行も変わらず動く。
- **best-effort**: hook 内の例外は ERROR log のみ。バックアップ設定ミスで宇宙作成/削除が倒れる事故を構造的に防ぐ。
- **scan + write 直列化**: 専用の `_backup_hook_lock`（`asyncio.Lock`）で scan → atomic write を **1 つの臨界区間** で行う。並行 create/delete hook が out-of-order に完了しても、最後の writer の scan 時点の on-disk 状態が最終 YAML になる（stale write の構造的排除、Codex review round-2 B2）。
- **import-based（subprocess ではない）**: 例外隔離・testability・レイテンシの全てで有利。純粋関数は disk IO のみ。
- **spawn env 隔離**: `_build_spawn_env` は `GAOTTT_*` を strip して allowlist だけ再注入する設計。litestream / backup 関連 knob は backend に **絶対に漏れない**（hook は supervisor プロセス内のみで動く）。

### ⚠ 新規宇宙が litestream config に現れるまでのラグ

> ★ **既知の制約（[multiverse-mv5-plan.md](../maintainers/multiverse-mv5-plan.md) §5 R2/R4）**: create/delete の hook は設定ファイルを **次の操作または定期再生成で反映** するが、新規宇宙の `gaottt.db` が litestream config に現れるまでには構造的なラグがある。

**ラグの原因**: `POST /admin/universes`（create）は宇宙ディレクトリ + `manifest.json` + registry 行を作った **直後** に backup hook を発火する。しかしこの時点ではまだ backend が起動しておらず、`gaottt.db` がディスク上に存在しない。`generate_litestream_config` は db 無しの宇宙を WARN skip する（[WP-1 scan rule](#litestream-雛形の使い方)）ので、新規宇宙はこの時点の YAML に含まれない。`gaottt.db` が作られるのは **最初の `/route` で backend が spawn され、engine が `SqliteStore` を初期化したとき** である。

**v1 の構造的緩和**: backend が readiness を確認した直後の `_ensure_locked` 成功経路でも hook を発火するよう実装した（[supervisor.py](../../gaottt/multiverse/supervisor.py) `_ensure_locked`）。これにより、新規宇宙の **最初の route** で db 作成 → 即座に rescan → YAML に現れる、という経路が閉じる。ただし「最初の route が来る前」の窓（create 直後〜最初の route）は残る。

**定期再生成 cron による決定的な閉包（推奨）**: 上記の窓を確実に埋めるため、**litestream config の定期再生成 cron** を併用すること。create/delete の hook と `_ensure_locked` の hook に加え、定期的に全宇宙を rescan すれば、どの経路でも取りこぼされた宇宙が遅くとも次の cron 周期で config に現れる:

```cron
# 毎時 0 分に litestream config を再生成（周期は環境に合わせて調整）
0 * * * * /opt/gaottt/.venv/bin/python /opt/gaottt/scripts/gen_litestream_config.py \
    --root /var/lib/gaottt-multiverse \
    --output /etc/litestream/gaottt.yml
```

> cron と supervisor hook は同じ `_backup_hook_lock`（同一プロセス内）または atomic write（プロセス間）で直列化されるため、並行実行しても YAML が torn になることはない。

### ⚠ litestream / object-store credential の分離

> ★ **既知の制約**: supervisor の `_build_spawn_env` は `GAOTTT_*` 変数のみを strip し、それ以外の環境変数（`AWS_SECRET_ACCESS_KEY` / `LITESTREAM_*` / object-store 認証 等）は spawned backend に **そのまま継承** される設計。allowlist 化（structural hardening）は別 stage の課題。

supervisor プロセスの環境に litestream / object-store の credential が含まれていると、各宇宙の backend プロセスにもそれらが漏れ渡る。**credential は supervisor とは別の、litestream daemon 専用の環境から供給すること**:

- litestream daemon は **独立した systemd unit**（`litestream.service`）で起動し、`/etc/litestream/env` 等の専用 env file からのみ credential を読み込む（[`deploy/litestream.yml`](../../deploy/litestream.yml) の冒頭コメントに最小 unit 例を記載）。
- supervisor の systemd unit（または起動 shell）には litestream / object-store 認証を **置かない**。
- これにより「supervisor と litestream が同一ホストで動く構成」でも、backend が object-store への書込権限を持つ事故を防ぐ。

## DR runbook（復旧手順）

災害発生時（ディスク障害・誤削除・宇宙ディレクトリ破損 等）の標準復旧手順。**各宇宙単位で独立に適用** する。

### standalone 宇宙（managed=False）の復旧

```bash
# 0. 復旧対象の宇宙 uid を特定。multiverse_root を環境変数で。
ROOT=/var/lib/gaottt-multiverse
UID=abc123

# 1. 全プロセス停止（supervisor + 全 backend） — write-behind 上書き罠の防御
systemctl stop gaottt-supervisor
ps -ef | grep 'gaottt.server.mcp_server' | grep -v grep   # 残りがいないか確認
# owner lease が release されたことも確認（lease 保持中の強制移動は NG）
cat $ROOT/universes/$UID/owner.lock 2>/dev/null || echo "no lease file (OK)"

# 2. SQLite + manifest を restore（2 点セット）
mkdir -p $ROOT/universes/$UID
# Litestream から SQLite を restore（litestream binary 必須）
litestream restore -o $ROOT/universes/$UID/gaottt.db \
    -replica /var/lib/litestream/gaottt-multiverse/$UID
# manifest は別経路（filesystem snapshot / exec mirror / rsync）から restore
cp -a /backup/gaottt-multiverse-manifests/$UID/manifest.json $ROOT/universes/$UID/

# 3. embedder artifact が利用可能ことを確認（manifest.embedder_id + version に対応する model）
#    → 詳細は下記「embedder artifact pinning」
.venv/bin/python -c "from gaottt.embedding.ruri import RuriEmbedder; RuriEmbedder('cl-nagoya/ruri-v3-310m').dimension"

# 4. FAISS 再構築（SQLite から決定論的に）
GAOTTT_DATA_DIR=$ROOT/universes/$UID .venv/bin/python scripts/rebuild_faiss_from_db.py --apply
GAOTTT_DATA_DIR=$ROOT/universes/$UID .venv/bin/python scripts/rebuild_faiss_from_db.py --check

# 5. 起動時診断（Tier A FAISS integrity / Tier B size consistency）が ERROR 0 件であることを確認
#    → supervisor 再起動時の lifespan で自動実行。ERROR があれば手順 1〜4 を見直す。

# 6. supervisor 再起動
systemctl start gaottt-supervisor
# supervisor の reconcile() が registry を再構築。API で universe 一覧を確認。
```

### managed 宇宙（managed=True）の復旧 — 人手作業（standalone の手順に加えて）

managed 宇宙は `manifest.managed=true` で [owner lease](Operations-Multiverse-Setup.md) が強制される。復旧時の初回起動で新 lease が取得されるが、以下は人手で対応する:

1. **API key 再発行**: registry.db は `reconcile()` で再構築できるが、API key hash の連続性は保証されない（[仮定 6](../maintainers/multiverse-mv5-plan.md)）。旧 key は無効 → ユーザーへ新 key を再発行（`POST /admin/universes` ではなく、registry への直接 key 再発行手順、または該当宇宙の削除→再作成）。
2. **backend token 再生成**: 復旧後の初回 route で supervisor が再 spawn し、新 token が `backend.token` に書かれる。旧 token を使っていた shim は再 route で新 token を取得する。
3. **control plane 再同期**（MV4 使用時）: supervisor の定期 sync が復旧後の universe 状態を control plane へ反映する。手動で即時 sync したい場合は supervisor 再起動。API key 再発行の事実は audit log に記録する。

## embedder artifact pinning（必須）

> ★ 商用運用の **必須要件**。「SQLite だけで復元できる」は同一 embedder artifact が入手できる前提で成り立つ。

manifest の `embedder_id`（例 `cl-nagoya/ruri-v3-310m`）+ `embedder_version`（HF snapshot commit hash）に対応する model weights を、復旧時に確実に入手できる状態にしておくこと。HF から model が消える・依存ライブラリ（`transformers` / `sentence-transformers` / `torch`）の更新で encode 数値が微妙に変わるリスクに備える。

推奨手法（環境に応じて選択）:

- **HF cache の tar 退避**: 運用中の HF cache（`~/.cache/huggingface/hub/models--cl-nagoya--ruri-v3-310m/snapshots/<commit>/`）を丸ごと tar でバックアップディレクトリへ退避。復旧時に展開するだけで決定論が保証される。
- **社内ミラー**: 社内の model registry / S3 ミラーへ snapshot を保持。HF Hub が落ちていても復旧可能。
- **version pin**: `requirements.txt` / `uv.lock` で `transformers==X.Y.Z` / `torch==A.B.C` を pin。`embedder_version` は HF revision を記録するが、依存ライブラリの version も encode 数値に影響しうるので、`embedder_version` + 依存 lock file の両方を保存する。

`scripts/dr_drill.py` の drill は stub embedder でこの前提を検証する（[下記](#dr-drill-四半期実行)）。商用投入前は **本番の embedder artifact で同様の pin 手順** を別途試験すること（[商用導入前チェックリスト](#商用導入前チェックリスト)）。

## 大宇宙向け optional FAISS snapshot

FAISS はデフォルトでバックアップ対象外だが、**大宇宙（数万ノード以上）では FAISS 再構築の RTO が無視できなくなる**（再 embed の時間）。RTO 短縮のため、FAISS ファイル自体も replicate 対象にする設定例:

- filesystem snapshot（ZFS / Btrfs / LVM）なら FAISS も同時に capture されるので、手順 (A) を採用していれば自動対応。
- litestream 経由ではないので、`gaottt.faiss` / `gaottt.virtual.faiss` は別途 rsync / object storage sync で定期的に退避。復旧時は SQLite restore → FAISS restore → `rebuild_faiss_from_db.py --check` で整合確認（`--apply` は不要、FAISS が揃っていれば skip される）。

## DR drill（四半期実行）

[`scripts/dr_drill.py`](../../scripts/dr_drill.py) は standalone（`managed=False`）宇宙で **バックアップ → 破壊 → 復元 → FAISS rebuild → 起動時診断 green → top-1 determinism 保持** の完全な DR 経路を自動検証する。litestream binary は不要（default は生ファイルコピー、`--with-litestream` で binary 経路も検証）。

```bash
# 四半期に 1 回、本番とは別の tmp root で実行（exit 0 = 成功）
.venv/bin/python scripts/dr_drill.py
.venv/bin/python scripts/dr_drill.py --with-litestream   # litestream binary がある環境で
```

**何を証明するか**: 「standalone 宇宙について、`gaottt.db` + `manifest.json` + embedder artifact が揃えば、FAISS rebuild → engine.startup → 起動時診断 green → 固定 query の top-1 が復元前後で一致する engine レベル復旧ができる」（[仮定 2](../maintainers/multiverse-mv5-plan.md): StubEmbedder の決定論性）。

**何を証明しないか**（runbook の人手作業）:
- managed 宇宙の完全復旧（lease / backend token / registry / control plane 再同期）— [上記](#managed-宇宙-managedtrue-の復旧--人手作業standalone-の手順に加えて)の人手手順
- litestream binary を実際に立ち上げての WAL restore e2e — 外部依存、[商用導入前チェックリスト](#商用導入前チェックリスト)で別途試験
- MV4 control plane (Postgres) の復旧 — 本 stage のスコープ外（Postgres は別運用）

## 商用導入前チェックリスト

商用環境へ MV5 を導入する前に、以下を **本番環境と同じ構成で** 別途試験すること（dr_drill では検証できない外部依存を含む）:

- [ ] **litestream binary 実機試験**: 実際の litestream binary で WAL replica → `litestream restore` の e2e を、本番サイズの dummy 宇宙で実施。雛形 `deploy/litestream.yml` の `dbs:` / `replicas:` schema が実機で受理されるか確認。
- [ ] **litestream 厳格検証（手動）**: `dr_drill.py --with-litestream` は best-effort（binary 不在・subprocess 失敗を ERROR log するが drill は落とさない）。本番投入前は **実際の litestream binary で snapshot → `litestream restore` の完全な WAL e2e** を手動で 1 回実施し、replica から復元した SQLite で `rebuild_faiss_from_db.py --check` が通ることを確認すること。
- [ ] **litestream config の新規宇宙取りこぼし確認**: 新規宇宙を作成し、定期再生成 cron 1 周期（または最初の route）待った後に生成された `/etc/litestream/gaottt.yml` にその宇宙の `gaottt.db` が含まれることを確認（[上記](#⚠-新規宇宙が-litestream-config-に現れるまでのラグ)のラグ対策の確認）。取りこぼしがあれば cron 周期を短くするか、定期再生成 cron の導入を見直す。
- [ ] **litestream / object-store credential 分離の確認**: supervisor プロセス環境に `AWS_SECRET_ACCESS_KEY` / `LITESTREAM_*` 等の認証が含まれていないこと、litestream daemon が独立 systemd unit + 専用 env file から起動していることを確認（[上記](#⚠-litestream--object-store-credential-の分離)）。
- [ ] **manifest バックアップ経路の選定と試験**: filesystem snapshot / `exec` mirror / rsync のいずれかを選び、復旧時に manifest が揃うことを実機確認（manifest 欠落は `verify_embedder_identity` で検出されるが、商用では未然に防ぐ）。
- [ ] **managed 宇宙 DR drill（人手）**: standalone の `dr_drill.py` に加え、managed 宇宙で「supervisor 停止 → 各宇宙 restore → `reconcile()` → API key 再発行 → control plane 再同期」の人手手順を 1 回リハーサル。
- [ ] **registry 復旧手順の確認**: `registry.db` 破損時に `reconcile()` が on-disk `universes/` から再構築すること、API key は再発行が必要なことを関係者へ周知。
- [ ] **embedder artifact pin の実機確認**: 本番 embedder で HF cache tar 退避 or 社内ミラーが機能し、復旧時に同一数値の encode が得られることを確認（dr_drill は stub embedder で検証するため、本番 embedder では別途）。
- [ ] **Postgres 復旧（MV4 使用時、別運用）**: control plane の Postgres バックアップ/復旧は標準的な商用 DB 運手順で別途設定。本 stage は関与しない。
