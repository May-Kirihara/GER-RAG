# Operations — Multiverse Import Universe

> 既存の standalone `data_dir`（単一ユーザー構成の `gaottt.db` + FAISS 群）を multiverse layout の 1 宇宙として取り込む importer の運用 runbook。
> 起票: 2026-07-04（MV3 follow-on、importer 完成）
> 関連: [Operations — Multiverse Setup](Operations-Multiverse-Setup.md)（MV3）、[Operations — Backup & DR](Operations-Backup-Multiverse.md)（MV5）、[Operations — Troubleshooting](Operations-Troubleshooting.md)、[Plans — Multiverse Scale-Out](Plans-Multiverse-Scale-Out.md)、[multiverse-importer-execution-plan.md](../maintainers/multiverse-importer-execution-plan.md)（仕様 SoT）、[handover-2026-07-04-multiverse-importer.md](../maintainers/handover-2026-07-04-multiverse-importer.md)

## 概要

GaOTTT を単一ユーザーで運用している `data_dir`（例: `~/.local/share/gaottt/`）を、[multiverse layout](Operations-Multiverse-Setup.md)（`<multiverse_root>/universes/<uid>/`）の 1 宇宙へ変換する CLI ツールです。次の 4 つを **best-effort で順次実行** します（途中で失敗した場合は target dir を cleanup し、registry への INSERT が完了していない状態を残しません）:

1. `data_dir` 内の 7 file を `<multiverse_root>/universes/<uid>/` へ copy（または move）
2. target の `gaottt.db` で `PRAGMA integrity_check` + schema gate を実行（corruption / empty を拒否）
3. `manifest.json` を `managed=True` で新規生成（[MV2 lease](Operations-Multiverse-Setup.md) が強制される）
4. `registry.db` に universe row を INSERT（API key 平文を一度だけ返す）

importer は local の `registry.db` のみを更新します。control plane への反映は supervisor の次回起動時に `control_client.pull`（`control_sync_interval_seconds` 周期）で自動的に行われます（J5「local が一次」原則）。supervisor が起動しているかどうかに依存しません（importer 単体で動作します）。supervisor が起動中の場合は並行安全です（partial UNIQUE INDEX `idx_universes_port_live` + WAL + busy_timeout で最終的に整合します）。

> **parity 例外**: importer は [MCP/REST parity 鉄則](Architecture-Overview.md) の対象外です（`/reset` と同じ例外クラス、破壊的管理操作を LLM に露出しない）。管理面の CLI ツールであり、engine の能力追加ではありません。

## 前提

- MV0（manifest）/ MV2（owner lease）/ MV3（registry + supervisor）が完了していること
- import 元の `data_dir` に `gaottt.db` が存在すること（FAISS 群は任意、欠損時は [rebuild_faiss_from_db.py](../../scripts/rebuild_faiss_from_db.py) で再構築可能）
- multiverse_root が指定されていること（CLI `--multiverse-root` または config の `multiverse_root`）
- source を所有する GaOTTT server process が停止していること（`pkill -f gaottt.server.mcp_server` / [Server Setup](Operations-Server-Setup.md) 参照）。`--force` で迂回可能ですが **非推奨**

## CLI リファレンス

```bash
.venv/bin/python scripts/import_universe.py \
    --source <source_data_dir> \
    --owner-label <owner_label> \
    [--universe-id <12-hex-lower>] \
    [--multiverse-root <path>] \
    [--move] \
    [--force] \
    [--yes] \
    [--dry-run] \
    [--embedder-id <id>] \
    [--embedder-version <v>]
```

### 引数

| 引数 | 必須 | default | 意味 |
|---|---|---|---|
| `--source` | ✅ | — | 既存 `data_dir`（`gaottt.db` 等を含むディレクトリ） |
| `--owner-label` | ✅ | — | 宇宙の owner label（registry に記録） |
| `--universe-id` | — | `uuid4().hex[:12]` | 宇宙 ID。**12 文字 lowercase hex** 必須（違えば exit 2）。重複時は exit 2 |
| `--multiverse-root` | — | config の `multiverse_root` | multiverse root。config + CLI 両方空なら exit 2 |
| `--move` | — | `False`（copy） | 指定時は source から target へ移動（source は 7 file を失う） |
| `--force` | — | `False` | source backend 停止確認を迂回（非推奨、複数行 WARNING を stderr 出力）。post-copy の `PRAGMA integrity_check` は `--force` 有無に関わらず常に実行 |
| `--yes` | — | `False` | TTY confirm prompt を迂回。**non-TTY 環境（CI / pipe）では `--yes` 必須**（無ければ exit 2） |
| `--dry-run` | — | `False` | 実行せず計画表示して exit 0（source / target / registry は一切不変） |
| `--embedder-id` | — | source manifest or config | embedder identity 上書き |
| `--embedder-version` | — | source manifest or `"unpinned"` | embedder version 上書き |

> **`reset_masses.py` との dry-run default 逆転の理由**: `reset_masses.py` は `--apply` 必須（dry-run default）。importer は `--dry-run` 指定（wet default）。理由: `reset_masses` は全ノード破壊的変更なので dry を安全側とする一方、importer は confirm prompt（TTY）+ `--yes` 必須（non-TTY）で安全側を担保しつつ、dry-run を明示することで「本当に wet で走らせたい」意図を明確にする設計です。

### Exit codes

| code | 意味 | 主な対処 |
|---|---|---|
| `0` | success | — |
| `2` | argument error（bad uid format / non-TTY without `--yes` / empty `multiverse_root` / target dir 重複） | 引数を修正して再実行 |
| `3` | source backend が起動中（`--force` で迂回可） | source の MCP/REST プロセスを停止して再実行 |
| `4` | embedder service unreachable または identity mismatch | embedding service の起動を確認、または `--embedder-id` で override |
| `5` | WAL が大きすぎる（`> 256MB` hard reject、または `> 64MB` で `--yes` 無し） | source backend を正常 shutdown して WAL checkpoint を実行 |
| `6` | target filesystem の disk 容量不足 | 不要ファイルを削除、または別 filesystem を指定 |
| `7` | copy / move 失敗（generic exception） | disk full / permission / cross-filesystem EXDEV 等を確認 |
| `8` | post-copy `PRAGMA integrity_check` 失敗 | source DB が破損している可能性。別途修復してから再実行 |
| `9` | retry 上限到達（persistent port race、max 3 回） | supervisor の port 割当状況を確認、時間を置いて再実行 |

## copy 対象 / 除外

allowlist 方式（`COPY_FILENAMES` のみ copy）。それ以外は全て `skipped` に分類され、dry-run レポートと `--move` 時の WARNING に現れます。

### copy 対象（7 file、存在するもののみ）

| ファイル | 役割 |
|---|---|
| `gaottt.db` | SQLite 本体（全ノードの content / metadata / mass / displacement / velocity / 共起 edge） |
| `gaottt.db-shm` | SQLite shared memory（WAL の index）。無くても正常（checkpoint 済み） |
| `gaottt.db-wal` | SQLite WAL（未 checkpoint の変更）。無くても正常 |
| `gaottt.faiss` | raw FAISS index（原始 embedding） |
| `gaottt.faiss.ids` | raw FAISS の id map |
| `gaottt.virtual.faiss` | virtual FAISS index（`raw + displacement`） |
| `gaottt.virtual.faiss.ids` | virtual FAISS の id map |

> **`-wal` / `-shm` が無い場合**: 正常（checkpoint 済み）として扱い、suspicious として扱いません。WAL size が `> 64MB` のみ WARNING、`> 256MB` で hard reject します。

### 除外（skipped、理由付きで分類）

| 対象 | 理由 |
|---|---|
| `*.bak` / `*.before-*` / `*.post-*` / `*.broken-*` / `*.tmp` | backup / 一時 / quarantine 系 |
| `manifest.json` | target で `managed=True` として新規生成（source のものは使わない） |
| `owner.lock` / `owner.lock.guard` | lease bookkeeping、target backend が新規取得 |
| `backend.token` | supervisor spawn 時に新規生成 |
| `registry.db` | multiverse registry（誤配置防止、universe data file ではない） |
| `memory.db` | legacy memory db |
| `*.db?mode=ro` | legacy read-only shim |
| 上記以外の file | `unrecognized file (not in copy allowlist)` |
| subdirectory | `directory (not a file)` |

## 典型的利用フロー

### 1. dry-run で plan を確認

`--dry-run` は副作用ゼロ（source / target / registry を一切触らない）です。copy 対象 file 一覧 / 合計サイズ / target uid / port / mode を確認します:

```bash
.venv/bin/python scripts/import_universe.py \
    --source ~/.local/share/gaottt \
    --owner-label "main" \
    --multiverse-root ~/.local/share/gaottt-multiverse \
    --dry-run --yes
```

### 2. 本番適用前に全 MCP/REST プロセスを停止

write-behind の逆方向上書き罠（[マルチプロセス / 共有 DB の罠](../../CLAUDE.md)、[Troubleshooting §問題5.5](Operations-Troubleshooting.md)）を防ぐため、source を触る全プロセスを停止します:

```bash
# MCP server (stdio / proxy backend 両方)
pkill -f 'gaottt.server.mcp_server'
pkill -f 'gaottt.server.app'        # REST server も念のため

# proxy mode の detached HTTP backend (port 7878) が残っていないか確認
ps -ef | grep "gaottt.server.mcp_server.*streamable-http" | grep -v grep
# 起動時刻が古ければ kill する（code deploy 時の backend 再起動ルールと同じ）

# source に binding されたプロセスが残っていないか念のため確認
ss -ltnp 2>/dev/null | grep -E ':(7878|7880|789[0-9])' || true
```

importer 自体が `_running_gaottt_pids` + source の `owner.lock` active check で検知して exit 3 しますが、事前停止を推奨します（`--force` を使わずに済む）。

### 3. wet run（copy を推奨）

```bash
.venv/bin/python scripts/import_universe.py \
    --source ~/.local/share/gaottt \
    --owner-label "main" \
    --multiverse-root ~/.local/share/gaottt-multiverse \
    --yes
```

成功時の出力（api_key 平文は **一度だけ** 表示される）:

```
universe_id : <12-hex>
port        : 7890
target_dir  : ~/.local/share/gaottt-multiverse/universes/<uid>
api_key     : <plaintext-api-key>  (shown once — store it now)

Next steps:
  - Ensure the supervisor is running on multiverse_root=...
    (or start it: python -m gaottt.multiverse.supervisor)
  - Point a shim at the supervisor and /route with the api_key
  - Confirm with a recall() through the spawned backend
```

> **copy を推奨する理由**: source が無傷なので移行失敗時の rollback が 2 step（registry row delete + target dir trash move）で完了します。`--move` は disk 容量節約になりますが失敗時の復旧が手作業になります（[ロールバック手順](#ロールバック手順) 参照）。

### 4. target で engine を直接起動して `recall` の round-trip を確認（推奨）

supervisor 経由ではなく、target の `data_dir` を直接指定して engine を起動し、`recall` が元のノードを返すことを確認します。これが **semantic drift 検出の実質的な検証** になります（importer は次元チェックまでしかできず、過去に別 model で embed された vector の意味的 drift は falsify 不可）:

```bash
GAOTTT_DATA_DIR=~/.local/share/gaottt-multiverse/universes/<uid> \
GAOTTT_EMBEDDER_ENDPOINT=http://127.0.0.1:7879 \
.venv/bin/python -c "
import asyncio
from gaottt.services.runtime import build_engine
from gaottt.config import GaOTTTConfig

async def check():
    cfg = GaOTTTConfig.from_config_file()
    eng = build_engine(cfg)
    await eng.startup()
    try:
        result = await eng.query('<既知の query>', top_k=3)
        print('recall returned', len(result.items), 'items')
        for item in result.items:
            print(' -', item.node_id, item.content[:60])
    finally:
        await eng.shutdown()

asyncio.run(check())
"
```

> **注意**: この直接起動は **target の `manifest.managed=True` により owner lease (MV2) が強制されます**。他に target を開いているプロセスがあれば `LeaseHeldError` になります（supervisor が spawn している場合等）。その場合は次 step 5 の supervisor 経由で確認してください。

### 5. supervisor 起動 → `POST /route` で spawned backend 経由で recall 確認

```bash
# supervisor を起動（未起動の場合）
GAOTTT_MULTIVERSE_ROOT=~/.local/share/gaottt-multiverse \
GAOTTT_SUPERVISOR_ADMIN_KEY="<admin-key>" \
GAOTTT_EMBEDDER_ENDPOINT=http://127.0.0.1:7879 \
.venv/bin/python -m gaottt.multiverse.supervisor &

# route で spawned backend の url + token を取得
curl -sf -X POST http://127.0.0.1:7880/route \
    -H "Content-Type: application/json" \
    -d '{"api_key": "<api-key>"}'
# → {"url": "http://127.0.0.1:7890/mcp", "token": "..."}
```

supervisor の `reconcile()` が importer が作成した宇宙を採用し、初回 route で backend を spawn します。backend の `verify_embedder_identity`（`engine.startup`）が manifest と一致して通ることを間接検証します。

### 6. 満足したら旧 source を cleanup（copy mode のみ）

copy mode では source は無傷です。target で問題なく動作することを確認したら、source を退避（推奨）または削除します:

```bash
# 退避（念のため一定期間保持）
mv ~/.local/share/gaottt ~/.local/share/gaottt.pre-import-$(date +%Y%m%d)

# または完全削除（十分に検証した後のみ）
# rm -rf ~/.local/share/gaottt
```

## `--move` 時の注意

`--move` は copy 対象 7 file を source から target へ移動します。以下の点に注意してください:

### backup 系ファイルは source に残る

`--move` が移動するのは `COPY_FILENAMES` の 7 file のみです。それ以外（`.bak` / `.before-*` / `manifest.json` / `owner.lock` / `backend.token` 等）は **source に残ります**。成功時の WARNING で一覧表示されます:

```
WARNING: --move left non-copy files in source (backups, manifests, tokens).
  Move or remove them manually:
  - /path/to/source/manifest.json
  - /path/to/source/owner.lock
  - ...
```

これは data loss を防ぐ設計です（誤って削除しないよう、明示的に残します）。

### 旧 `.mcp.json` を新 multiverse 構成に切り替える

source を standalone で使っていた `.mcp.json` / opencode config / Codex hooks.json は、supervisor 経由に切り替える必要があります:

**Claude Code (`.mcp.json`)**:

```json
{
  "mcpServers": {
    "gaottt": {
      "command": "/path/to/GaOTTT/.venv/bin/python",
      "args": [
        "-m", "gaottt.server.mcp_server",
        "--transport", "proxy",
        "--supervisor-url", "http://127.0.0.1:7880"
      ],
      "env": {
        "GAOTTT_API_KEY": "<api-key>"
      },
      "cwd": "/path/to/GaOTTT"
    }
  }
}
```

**opencode / Codex CLI** も同じ `--supervisor-url` + `GAOTTT_API_KEY` で動きます（[Server Setup](Operations-Server-Setup.md) の各クライアント節を参照、args に `--supervisor-url` を追加するだけ）。

### `--move` で失敗した場合の復旧

`execute_import` は **move mode では target を cleanup しません**（data loss を防ぐため）。copy 中の例外 / `create_universe` の IntegrityError / generic exception のいずれでも、移動済みの file は target に残ります。事業者が手動で target → source へ逆方向 copy で復元してください（[ロールバック手順 §`--move` の場合](#move-の場合) 参照）。

## `--force` 使用時の注意

`--force` は **source backend 停止確認のみ** を迂回します（registry 操作は迂回しません）。複数行の WARNING を stderr 出力します:

```
WARNING: --force in effect with source writers running: pid=12345 (MCP server)
  This is unsafe: a concurrent writer can produce a corrupt copy.
  A post-copy PRAGMA integrity_check WILL be run; if it fails the import is aborted (exit 8).
```

- `--force` は source の `owner.lock` active check も迂回します（`owner.lock` が active な場合も WARNING 付きで継続）
- copy / move 自体、integrity_check、manifest 生成、registry INSERT は全て通常通り走ります
- **post-copy の `PRAGMA integrity_check` は `--force` の有無にかかわらず常に実行されます**（`_verify_target_db` は無条件呼び出し）。`--force` 時の WARNING は「`--force` で迂回してもこの検証だけは効く」と利用者に明示するためのものです
- integrity_check が "ok" でなければ target は cleanup されて exit 8 します（corruption が検出された場合）
- **本番運用では `--force` を避け、事前にプロセスを停止することを推奨します**

## ロールバック手順

import した宇宙を取り消す手順です。**copy mode を推奨** します（source 無傷、2 step で完了）。

> **重要**: 以下の手順を実行する前に **supervisor の `.spawn.lock` を尊重** してください。supervisor 起動中は `DELETE /admin/universes/{uid}` を使います（`.spawn.lock` で backend の安全な停止を保証）。直接 registry / filesystem を触るのは supervisor 停止中のみです。

### copy mode の場合（推奨、default）

source は無傷。**2 step** で完全 rollback できます:

1. **registry row を `status=deleted` へ**
2. **target dir を `trash/` へ移動または削除**

#### supervisor 起動中の場合（推奨経路）

`DELETE /admin/universes/{uid}` を使います。supervisor が `.spawn.lock` + 2-layer lock で安全に backend を停止し、target dir を `trash/` へ移動し、registry row を `status=deleted` にします（[supervisor.py](../../gaottt/multiverse/supervisor.py) の delete handler 実装）:

```bash
curl -X DELETE http://127.0.0.1:7880/admin/universes/<uid> \
    -H "X-Admin-Key: $GAOTTT_SUPERVISOR_ADMIN_KEY"
```

#### supervisor 停止中の場合（直接 registry を触る）

**前提条件**: supervisor が停止していることを確認してください（`ps -ef | grep gaottt.multiverse.supervisor`）。supervisor が生きていると `/route` が mid-spawn で race します。

```bash
.venv/bin/python -c "
import asyncio
from pathlib import Path
from gaottt.multiverse.registry import MultiverseRegistry, TRASH_SUBDIR
import shutil

async def cleanup(uid, root):
    root = Path(root)
    reg = MultiverseRegistry(root)
    await reg.initialize()
    await reg.delete_universe(uid)
    await reg.close()
    # target dir を trash/ へ移動（supervisor の delete handler と同じ）
    target = root / 'universes' / uid
    trash = root / TRASH_SUBDIR / uid
    trash.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.move(str(target), str(trash))
        print(f'moved {target} -> {trash}')
    else:
        print(f'no target dir at {target}')

asyncio.run(cleanup('<uid>', '<multiverse_root>'))
"
```

> **注意**: target dir を消すだけだと registry row が `status=active` で残り、次回 supervisor 起動の `reconcile` で `orphan` 扱いになります（WARNING + skip）。**必ず registry row の delete と target dir 移動をセットで行ってください**。

### `--move` の場合

source は空（backup 系以外）。rollback には **逆方向 copy**（target → source）が必要です:

1. 上記 copy mode と同じ手順で target を削除する **前に**
2. target から source へ 7 file を copy（`shutil.copy2`）
3. その後 registry row delete + target dir 移動

```bash
# 1. target → source へ逆方向 copy（7 file）
SOURCE=~/.local/share/gaottt
TARGET=~/.local/share/gaottt-multiverse/universes/<uid>
for f in gaottt.db gaottt.db-shm gaottt.db-wal \
         gaottt.faiss gaottt.faiss.ids \
         gaottt.virtual.faiss gaottt.virtual.faiss.ids; do
    [ -f "$TARGET/$f" ] && cp -a "$TARGET/$f" "$SOURCE/$f"
done

# 2. その後、copy mode と同じ手順で target を削除（supervisor DELETE または直接 registry）
```

**`--move` は非推奨です**。copy を強く推奨します。

## トラブルシューティング

| 症状 | 原因 / 対処 |
|---|---|
| `exit 2` / `--universe-id must be 12 lowercase hex chars` | `--universe-id` の形式誤り。12 文字 lowercase hex（`0-9a-f`）で指定 |
| `exit 2` / `non-TTY environment requires --yes` | CI / pipe 環境で `--yes` 無し。`--yes` を付けるか `--dry-run` を使う |
| `exit 2` / `multiverse_root is not set` | config と CLI 両方で未指定。`--multiverse-root` を明示するか config の `multiverse_root` を設定 |
| `exit 2` / `target directory already exists` | 同じ uid で再実行した。別 uid を使うか、既存宇宙を [rollback](#ロールバック手順) してから再実行 |
| `exit 3` / `GaOTTT server processes are bound to source` | source を所有する MCP/REST プロセスが生きている。停止して再実行、または `--force`（非推奨） |
| `exit 3` / `source owner.lock is held by an active owner` | source の lease が active。プロセスを停止して heartbeat を止める、または `--force` |
| `exit 4` / `embedder service unreachable` | `config.embedder_endpoint` 設定時に embedding service が応答しない。service 起動を確認 |
| `exit 4` / `embedder identity mismatch` | source manifest の `embedder_id` と `/info` の `model_name` が違う。source を再 embed するか `--embedder-id` で override |
| `exit 5` / `source WAL is N bytes (> 268435456)` | source backend が正常に shutdown していない。停止して SQLite checkpoint を待って再実行 |
| `exit 5` / `source WAL is N bytes (> 67108864)` | WAL が大きめ。`--yes` で続行するか、source backend を正常 shutdown して checkpoint させる |
| `exit 6` / `insufficient disk capacity` | target filesystem の空き不足。copy 合計サイズ + 10% buffer が必要。不要ファイル削除 or 別 filesystem |
| `exit 7` / `import failed: <exception>` | copy / move の generic 例外。disk full / permission / cross-filesystem EXDEV 等を確認 |
| `exit 8` / `integrity_check failed` / `target db has no user tables` | source DB が破損 / empty / schemaless。別途修復（`scripts/rebuild_faiss_from_db.py` / SQLite recovery 等）してから再実行 |
| `exit 9` / `could not allocate port after 3 attempts` | port race が持続。supervisor の port 割当状況を確認、時間を置いて再実行 |
| import 後に backend が起動しない | embedder identity mismatch の可能性。`--embedder-id` で override、または `GAOTTT_MANIFEST_CHECK_ENABLED=false` で escape（[Tuning](Operations-Tuning.md) 参照） |
| import 後に recall 結果が変わる | **同じ結果を返すのが正常** です。importer は `gaottt.db`（`nodes` テーブルの `displacement` / `velocity` BLOB 列を含む）と `gaottt.virtual.faiss` を共に copy し、target の `engine.startup` は disk から load するため、mass / temperature / displacement / virtual 位置が全て preserve されます。**もし結果が変わった場合は semantic drift や corruption の可能性がある** ため、`scripts/diag_recall.py snapshot` で source / target を比較してください。詳細は [Troubleshooting](Operations-Troubleshooting.md) 参照 |
| `database is locked` | supervisor 起動中に実行した。`busy_timeout=30000` で 30 秒待つが、頻発するなら supervisor 停止を推奨。WAL + partial UNIQUE INDEX で最終的に整合するが、importer 側は max 3 回 retry |

より詳細は [Operations — Troubleshooting](Operations-Troubleshooting.md) の importer 関連項目も参照してください。

## 既知の制約と今後の改善点

### 1. port range は config の `universe_port_range_{start,end}` に従う

`execute_import` は `port_range` keyword-only 引数を取り、CLI (`scripts/import_universe.py`) は `(config.universe_port_range_start, config.universe_port_range_end)` を渡します。`port_range=None` の場合は `(7890, 7989)` が default（後方互換）。

- supervisor の `universe_port_range` config と一致することを保証（どちらも同じ config 値を読むため）
- supervisor 側で後から port を変えたい場合は registry row の `port` 列を直接編集する必要があります（supervisor は再起動時に registry の port を再利用）
- programmatic に `execute_import` を呼ぶ場合は `port_range` を明示的に渡すことを推奨します（default は歴史的互換性のため 7890-7989）

### 2. move mode は cleanup しない（data loss 防止）

`execute_import` は **move mode では例外時に target を cleanup しません**。copy 中に一部 file が移動済みの場合、cleanup すると data loss になるためです。

- crash 時は手動で target → source の逆方向 copy で復元します（[ロールバック手順 §`--move` の場合](#move-の場合) 参照）
- copy mode を推奨します（source 無傷、retry が安全）

### 3. `_verify_target_db` の schema gate

post-copy の整合性検証は **3 段階** です:

1. target の `gaottt.db` が存在すること（missing を検出）
2. `PRAGMA integrity_check` が `"ok"` を返すこと（torn write / corruption を検出）
3. `sqlite_master` に user table が 1 つ以上あること（0-byte file / schemaless file を検出）

> **0-byte file 対策は実装済み** です。SQLite は 0-byte file を valid な空 DB として扱うため、(2) の `integrity_check` 単独では `"ok"` が返ってしまいます。(3) の user table count チェックで空 DB を reject します。

**残存 gap**: non-GaOTTT schema（無関係なテーブルだけを持つ DB）は (3) を通過してしまいます。完全な GaOTTT schema check（`nodes` table の存在等）は `engine.startup` に委譲しています（`importer.py` を `store/` の schema 定義に耦合させないため）。実質的な検証は上記 [典型的利用フロー step 4](#4-target-で-engine-を直接起動して-recall-の-round-trip-を確認推奨) の `recall` round-trip で行ってください。

### 4. importer + supervisor 同時起動の port race

importer と supervisor が同時に `allocate_port` を呼ぶと同じ port を選ぶ可能性があります。partial UNIQUE INDEX `idx_universes_port_live` が INSERT 時に一方を `IntegrityError` で拒否し、importer 側は max 3 回 retry で別 port を選び直します。

- **supervisor 側は retry 無し**（既存挙動）なので、衝突した場合は supervisor 側の create が失敗する可能性があります（確率は極低、数ミリ秒窓）
- 衝突した場合は supervisor 側の操作を再試行してください

### 5. control plane (MV4) への反映

importer は local の `registry.db` のみを触ります。control plane への反映は supervisor の次回起動時に `control_client.pull`（`control_sync_interval_seconds` 周期）で自動的に行われます（J5「local が一次」原則）。supervisor の `reconcile` は local registry ↔ local dirs の整合のみを行い、control plane には触れません。

- 直接 control plane に POST しないでください（二重登録リスク）
- 即時反映したい場合は supervisor を（再）起動してください

### 6. `_verify_target_db` の semantic drift 検出不可

過去に別 model で embed された vector は、post-copy の `engine.startup` の `verify_embedder_identity` で次元チェックはできますが、**意味的な drift は検出できません**。

- `/info` は現 embedder service と manifest identity の一致を証明するのみで、過去の vector 生成 model までは保証しません
- 実質的な検証は [典型的利用フロー step 4](#4-target-で-engine-を直接起動して-recall-の-round-trip-を確認推奨) の `recall` round-trip で行ってください
- 詳細は [Operations — Backup & DR §embedder artifact pinning](Operations-Backup-Multiverse.md#embedder-artifact-pinning必須) を参照

## 手動フォールバック手順

importer が動かない場合（bug / 互換性問題 / 緊急対応）の ad-hoc 手順です。既存の `POST /admin/universes` + `cp` + `registry.db` 直接編集で同等のことができます:

> **注意**: この手順は importer の safety check（integrity_check / WAL size / disk capacity / process probe 等）を **一切踏みません**。相当のリスクを理解した上で、緊急時のみ使用してください。

```bash
# 0. supervisor を起動（未起動の場合）
GAOTTT_MULTIVERSE_ROOT=~/.local/share/gaottt-multiverse \
GAOTTT_SUPERVISOR_ADMIN_KEY="<admin-key>" \
GAOTTT_EMBEDDER_ENDPOINT=http://127.0.0.1:7879 \
.venv/bin/python -m gaottt.multiverse.supervisor &

# 1. 空宇宙を作成（manifest.managed=True で生成される）
RESP=$(curl -sf -X POST http://127.0.0.1:7880/admin/universes \
    -H "X-Admin-Key: $GAOTTT_SUPERVISOR_ADMIN_KEY" \
    -H "Content-Type: application/json" \
    -d '{"owner_label": "main"}')
UID=$(echo "$RESP" | jq -r .universe_id)
API_KEY=$(echo "$RESP" | jq -r .api_key)
echo "universe_id=$UID api_key=$API_KEY"

# 2. 全プロセスを停止（write-behind 逆方向上書き罠の防御）
pkill -f 'gaottt.server.mcp_server'
pkill -f 'gaottt.multiverse.supervisor'

# 3. source 7 file を target へ copy
SOURCE=~/.local/share/gaottt
TARGET=~/.local/share/gaottt-multiverse/universes/$UID
for f in gaottt.db gaottt.db-shm gaottt.db-wal \
         gaottt.faiss gaottt.faiss.ids \
         gaottt.virtual.faiss gaottt.virtual.faiss.ids; do
    [ -f "$SOURCE/$f" ] && cp -a "$SOURCE/$f" "$TARGET/$f"
done

# 4. manifest を上書き（embedder identity は source のものを使う）
.venv/bin/python -c "
import json, time
from pathlib import Path
target = Path('$TARGET')
# source の manifest を読む（無ければ config fallback）
src_manifest = None
src_m_path = Path('$SOURCE/manifest.json')
if src_m_path.exists():
    src_manifest = json.loads(src_m_path.read_text())
manifest = {
    'schema_version': 1,
    'universe_id': '$UID',
    'embedder_id': src_manifest.get('embedder_id', 'cl-nagoya/ruri-v3-310m') if src_manifest else 'cl-nagoya/ruri-v3-310m',
    'embedder_version': src_manifest.get('embedder_version', 'unpinned') if src_manifest else 'unpinned',
    'embedding_dim': 768,
    'created_at': time.time(),
    'managed': True,
}
(target / 'manifest.json').write_text(json.dumps(manifest, indent=2))
print('manifest written:', manifest)
"

# 5. post-copy の integrity_check（importer と同じ検証）
.venv/bin/python -c "
import sqlite3
conn = sqlite3.connect('file:$TARGET/gaottt.db?immutable=1', uri=True)
print('integrity:', conn.execute('PRAGMA integrity_check').fetchone())
print('tables:', conn.execute(\"SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'\").fetchone())
conn.close()
"

# 6. supervisor を再起動して reconcile で宇宙を認識させる
#    → POST /admin/universes で作った registry row は既に存在するので、
#      reconcile は target dir と registry row を突き合わせて active 扱いにする
```

> **`POST /admin/universes` が空の宇宙を作る理由**: この API は `uuid4().hex[:12]` を新規発行して空ディレクトリを作るだけなので、step 3 で 7 file を copy する前は空宇宙です。importer との違いは safety check の有無と API key 発行経路（admin API 経由 vs importer 経由）です。registry row の shape は同じなので、reconcile はどちらの経路で作られた宇宙も同じように扱います。

## 関連

- [Operations — Multiverse Setup](Operations-Multiverse-Setup.md) — MV3 supervisor / 宇宙作成 / 削除
- [Operations — Backup & DR](Operations-Backup-Multiverse.md) — MV5 per-universe backup / DR
- [Operations — Control Plane](Operations-Control-Plane.md) — MV4 台帳同期
- [Operations — Troubleshooting](Operations-Troubleshooting.md) — importer 関連の項目
- [multiverse-importer-execution-plan.md](../maintainers/multiverse-importer-execution-plan.md) — 仕様 SoT（PM 管轄）
- [handover-2026-07-04-multiverse-importer.md](../maintainers/handover-2026-07-04-multiverse-importer.md) — 実装 handover note
