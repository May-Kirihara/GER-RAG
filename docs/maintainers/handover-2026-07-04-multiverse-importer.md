# Handover — Multiverse Importer (2026-07-04)

> 引き継ぎメモ: 既存 standalone `data_dir` を multiverse layout の 1 宇宙として取り込む importer（`scripts/import_universe.py` + `gaottt/multiverse/importer.py`）の実装完了報告。
> 関連: [multiverse-importer-execution-plan.md](multiverse-importer-execution-plan.md)（仕様 SoT、v3）、[Operations — Multiverse Import Universe](../wiki/Operations-Multiverse-Import-Universe.md)（運用 runbook）

---

## ステータス

| 項目 | 値 |
|---|---|
| 状態 | ✅ **完了**（WP-1 / WP-2 / WP-3 全て green） |
| 日付 | 2026-07-04 |
| 担当 | implementer subagent（WP-2 実装）+ docs subagent（WP-3 docs） |
| 概要 | standalone `data_dir`（例: `~/.local/share/gaottt/`）を `<multiverse_root>/universes/<uid>/` の 1 宇宙へ変換する CLI importer を実装。copy/move/dry-run の 3 mode、transaction scope で atomic 実行、post-copy `PRAGMA integrity_check` + schema gate で corruption/empty を検出 |

## 変更内容

### 新規ファイル

| ファイル | 行数 | 役割 |
|---|---|---|
| `gaottt/multiverse/importer.py` | 562 | 純粋関数（`compute_file_plan` / `resolve_embedder_identity` / `validate_universe_id` / `build_import_plan`）+ `execute_import`（transaction-scoped mutating entry point）+ `COPY_FILENAMES` 定数 |
| `scripts/import_universe.py` | 578 | CLI entry point。引数解析 / source sanity / process probe / WAL check / embedder service probe / disk capacity check / dry-run / TTY confirm / `execute_import` 呼び出し |
| `tests/unit/test_importer.py` | (WP-1) | 純粋関数の unit test |
| `tests/integration/test_import_universe.py` | (WP-1) | round-trip + supervisor spawn e2e（`@slow` 含む）|
| `docs/wiki/Operations-Multiverse-Import-Universe.md` | 523 | 運用 runbook（本 handover と同 WP-3 成果物） |
| `docs/maintainers/handover-2026-07-04-multiverse-importer.md` | (本ファイル) | handover note |

### 既存ファイル更新（WP-3 docs）

- `docs/wiki/Operations-Multiverse-Setup.md` — 「既存宇宙の import」節へのリンク追加
- `docs/wiki/Operations-Troubleshooting.md` — importer 関連 4 項目追加
- `docs/wiki/_Sidebar.md` + `docs/wiki/Home.md` — 新ページ追加に伴う更新
- `docs/wiki/Architecture-Overview.md` — 設計判断表に importer 追加
- `docs/maintainers/multiverse-implementation-plan.md` — MV3.5 follow-on として完了マーク追記

## 変更理由

MV3 の `MultiverseRegistry.reconcile()` は「supervisor が作っていない宇宙を暗黙採用しない」設計（directory present + registry missing → WARNING + skip）で、`POST /admin/universes` も新規空宇宙作成専用です。結果として **「既存 standalone DB を multiverse に import する機能」は意図的に存在しません** でした（memory id=58a87d7a「暗黙採用拒否の設計判断」）。

ユーザーが現在本番運用中の `~/.local/share/gaottt/`（gaottt.db = 760MB / FAISS = 122MB / virtual.faiss = 122MB）を multiverse の 1 宇宙として取り込む必要があり、この gap を埋める importer を実装しました（memory id=4d0069fb「import 必須 5 点」）。

## Work packages

### WP-1: test-first（unit + integration）

- **Scope**: `tests/unit/test_importer.py` / `tests/integration/test_import_universe.py`
- **Status**: ✅ 完了（66/66 + `@slow` 1/1 green）
- **Files**: 上記 2 ファイル
- **Verification**: `.venv/bin/python -m pytest tests/unit/test_importer.py tests/integration/test_import_universe.py -v`
- **Remaining risks**: なし（WP-2 実装で test contract は全て充足）

### WP-2: core 実装

- **Scope**: `gaottt/multiverse/importer.py` / `scripts/import_universe.py`
- **Status**: ✅ 完了
- **Files**: 上記 2 ファイル
- **Verification**: WP-1 のテストが全て green / `ruff check gaottt/ tests/`（pre-existing 4 件のみ）
- **Remaining risks**: 下記「既知の問題」参照

### WP-3: docs / handoff（本 WP）

- **Scope**: runbook / handover / 5 既存 docs 更新
- **Status**: ✅ 完了（本 handover）
- **Files**: 下記「触ったファイル」参照
- **Verification**: 目視確認（docs-only WP、test 実行なし）
- **Remaining risks**: runbook の手動フォールバック手順は importer の safety check を踏まないため、緊急時のみ使用することを明記済み

## 触ったファイル

### 新規作成（WP-3）

- `docs/wiki/Operations-Multiverse-Import-Universe.md`（523 行）— 運用 runbook
- `docs/maintainers/handover-2026-07-04-multiverse-importer.md`（本ファイル）— handover note

### 既存更新（WP-3）

- `docs/wiki/Operations-Multiverse-Setup.md` — 「既存宇宙の import」節へのリンク追加
- `docs/wiki/Operations-Troubleshooting.md` — importer 関連 4 項目追加
- `docs/wiki/_Sidebar.md` — Operations 配下に Import Universe 追加
- `docs/wiki/Home.md` — Operations 表に Import Universe 行追加
- `docs/wiki/Architecture-Overview.md` — 設計判断表に importer 行追加
- `docs/maintainers/multiverse-implementation-plan.md` — MV3.5 完了マーク追記

### 触っていない（明示）

- 実装ファイル（`gaottt/multiverse/importer.py` / `scripts/import_universe.py`）— WP-2 の成果物、本 WP では不変
- テストファイル（`tests/`）— WP-1 の成果物、本 WP では不変
- `docs/maintainers/multiverse-importer-execution-plan.md`（plan 自体）— PM 管轄、本 WP では不変
- `docs/wiki/Operations-Tuning.md` — 新 config knob 無し（importer は既存 knob に依存するのみ）、更新不要
- `pyproject.toml` / lint 設定 / CI — 本 WP では不変

## テスト

### 実行したコマンド（WP-3、docs-only WP なので最小限）

- ファイル作成・編集後の目視確認（行数 / grep で WP-2 の 3 点が明記されているか等）
- `wc -l` / `grep` での構成確認

### 未実行

- `.venv/bin/python -m pytest tests/ -q` — 本 WP は docs-only なので test 実行なし。WP-2 完了時点で 66/66 + `@slow` 1/1 green を確認済み（別 WP）
- `ruff check gaottt/ tests/` — 同上（docs は ruff 対象外）
- `scripts/rest_smoke.py` / `scripts/mcp_smoke.py` — importer は MCP/REST に触らないので回帰影響なし、本 WP では未実行

### 理由

WP-3 は docs-only work package です。実装・テストファイルは一切触っていないため、test 実行による検証項目はありません。WP-2 完了時点で全 test green を確認済みであり、docs 更新が実装・テストに影響を与える経路は存在しません。

## ドキュメント

### 更新したドキュメント

- `docs/wiki/Operations-Multiverse-Import-Universe.md`（新規、523 行）— 運用 runbook。CLI リファレンス / copy 対象除外表 / 典型的利用フロー / `--move` 時の注意 / `--force` 使用時の注意 / ロールバック手順 / トラブルシューティング表 / 既知の制約と今後の改善点 / 手動フォールバック手順 を網羅
- `docs/maintainers/handover-2026-07-04-multiverse-importer.md`（新規、本ファイル）
- `docs/wiki/Operations-Multiverse-Setup.md` — 「既存宇宙の import」節へのリンク追加
- `docs/wiki/Operations-Troubleshooting.md` — importer 関連 4 項目追加
- `docs/wiki/_Sidebar.md` + `docs/wiki/Home.md` — 新ページ追加
- `docs/wiki/Architecture-Overview.md` — 設計判断表に importer 行追加
- `docs/maintainers/multiverse-implementation-plan.md` — MV3.5 完了マーク追記

### 未更新

- `docs/wiki/Operations-Tuning.md` — 新 config knob 無し。importer の挙動は既存 knob（`multiverse_root` / `embedder_endpoint` 等）に依存するのみ。`WAL_HARD_REJECT_BYTES` / `WAL_WARN_BYTES` / `DISK_CAPACITY_BUFFER` / `DEFAULT_LEASE_STALE_SECONDS` は `scripts/import_universe.py` の module-level 定数で config knob 化していないため、Tuning ページには載せない（plan §1.3「新 config knob 無し」および D2 設計判断に準拠）
- `SKILL.md` / `.claude/skills/gaottt/SKILL.md` — importer は MCP ツールではないため更新不要
- `docs/wiki/MCP-Reference-*.md` / `docs/wiki/REST-API-Reference.md` — importer は MCP/REST parity 対象外（管理面）なので更新不要

## 手動確認

WP-3 は docs-only なので手動確認項目は目視確認のみです。本番適用時の手動確認項目は [runbook の典型的利用フロー](../wiki/Operations-Multiverse-Import-Universe.md#典型的利用フロー) を参照してください（6 step: dry-run → プロセス停止 → wet run → target で recall 確認 → supervisor 経由で recall 確認 → source cleanup）。

## 既知の問題

実装上の既知の制約で、runbook の「既知の制約と今後の改善点」節に明記済みのもの:

### 1. port range が importer 側で 7890–7989 hardcoded

`execute_import` が `registry.allocate_port(7890, 7989)` を呼びます（`_DEFAULT_PORT_RANGE_START` / `_DEFAULT_PORT_RANGE_END` モジュール定数）。これは `ImportPlan` dataclass の shape が WP-1 の test contract で固定されているため、window を動的に渡せない制約です。supervisor の `universe_port_range` config と不一致でも機能しますが、後から port を変えたい場合は registry row の `port` 列を直接編集する必要があります。

### 2. move mode は cleanup しない（data loss 防止）

`execute_import` は move mode では例外時に target を cleanup しません（copy 中に一部 file が移動済みの場合、cleanup すると data loss になるため）。crash 時は手動で target → source の逆方向 copy で復元します。copy mode を推奨します。

### 3. `_verify_target_db` の schema gate

post-copy の整合性検証は **3 段階** です（file 存在 / `PRAGMA integrity_check == "ok"` / `sqlite_master` の user table count ≥ 1）。

> **plan v3 からの差分（重要）**: plan v3 の acceptance criteria #18 は「post-copy `PRAGMA integrity_check` が "ok" を返す」のみを要求していました。WP-2 の実装ではこれを超えて **(c) `sqlite_master` の user table count チェック** を追加し、0-byte file / schemaless file を reject するよう強化しています。WP-2 implementer の handover context に「schema gate 強化の余地（0-byte file 対策は今後の課題）」とありましたが、**実装を見ると既に対処済み** です（`importer.py:297-362` の `_verify_target_db`）。repository を truth として扱い、runbook には「0-byte file 対策は実装済み」と正確に記載しました。

**残存 gap**: non-GaOTTT schema（無関係なテーブルだけを持つ DB）は (c) を通過してしまいます。完全な GaOTTT schema check（`nodes` table の存在等）は `engine.startup` に委譲しています（`importer.py` を `store/` の schema 定義に耦合させないため）。

### 4. importer + supervisor 同時起動の port race

importer と supervisor が同時に `allocate_port` を呼ぶと同じ port を選ぶ可能性があります。partial UNIQUE INDEX `idx_universes_port_live` が INSERT 時に一方を `IntegrityError` で拒否し、importer 側は max 3 回 retry で別 port を選び直します。supervisor 側は retry 無し（既存挙動）なので、衝突した場合は supervisor 側の create が失敗する可能性があります（確率は極低）。

### 5. control plane (MV4) への反映

importer は local の `registry.db` のみを触ります。control plane への反映は supervisor の次回起動時の `reconcile` + `control_client.pull` で自動反映されます（J5「local が一次」設計）。直接 control plane に POST しないでください（二重登録リスク）。

### 6. `_verify_target_db` の semantic drift 検出不可

過去に別 model で embed された vector は、post-copy の `engine.startup` の `verify_embedder_identity` で次元チェックはできますが、意味的な drift は検出できません。実質的な検証は runbook の典型的利用フロー step 4（`recall` round-trip）で行ってください。

## 残 TODO

本 WP-3（docs / handoff）の残 TODO はありません。importer 実装全体としては以下が別セッションで対応します:

- **本番 DB の実際の移行実行** — plan §1.3「非スコープ」にて明記済み。importer 完成・検証後にメンテ窓として実施（runbook の典型的利用フローに沿って）
- **supervisor admin API への `POST /admin/universes/import` 追加** — plan §1.3「非スコープ」。importer script 単体で完結する現構成を維持する場合、追加不要
- **schema gate の強化**（`nodes` table 存在チェック）— 現状の `_verify_target_db` は `sqlite_master` の user table count のみ。完全な GaOTTT schema check を入れる場合は `importer.py` を `store/` の schema 定義に coupling する trade-off を評価する必要あり

## リスク

### 高リスク

- **本番 DB の data-loss surface**: 760MB DB の copy 中の crash / disk full / permission 等で中途半端な状態になる可能性。copy mode なら source 無傷、`execute_import` の retry unit 例外時 cleanup で target を掃除するので安全側。`--move` は非推奨（crash 時の手動復旧が必要）
- **write-behind 逆方向上書き罠**: source を触るプロセスが生きていると copy 元が変更され、結果が不定になる。importer は `_running_gaottt_pids` + source の `owner.lock` active check で検知して exit 3 するが、`--force` で迂回した場合は post-copy `PRAGMA integrity_check` が必須（実装が自動実行）

### 中リスク

- **embedder identity 切り替え**: standalone は in-process RuriEmbedder、multiverse は RemoteEmbedder 必須。backend 起動失敗の可能性。`/info` で現 embedder service と manifest identity の一致を確認するが、過去の vector 生成 model までは保証しない（semantic drift は falsify 不可）
- **多層メタデータ操作**: manifest / port / registry / lease / token / control plane の整合性。transaction scope で atomic 実行し、例外時 cleanup で stray target を残さない設計

### 低リスク

- **port race**: importer + supervisor 同時起動時に同じ port を選ぶ可能性。partial UNIQUE INDEX + max 3 回 retry で最終的に整合する。確率は極低
- **WAL size 異常**: unflushed 変更がある場合。64MB 超で WARNING、256MB 超で hard reject する

## ロールバックメモ

importer で作成した宇宙を取り消す手順です。詳細は [runbook のロールバック手順](../wiki/Operations-Multiverse-Import-Universe.md#ロールバック手順) を参照。

### copy mode（推奨）の場合

source は無傷。**2 step** で完全 rollback:

1. registry row を `status=deleted` へ（`registry.delete_universe(uid)`）
2. target dir を `trash/` へ移動または削除

**重要**: supervisor 起動中は `DELETE /admin/universes/{uid}` を使う（`.spawn.lock` を尊重）。直接 registry / filesystem を触るのは supervisor 停止中のみ。

### `--move` の場合

source は空（backup 系以外）。rollback には逆方向 copy（target → source）が必要:

1. target から source へ 7 file を copy（`shutil.copy2`）
2. その後、copy mode と同じ手順で target を削除

`--move` は非推奨です。

### WP-3 自身の rollback

WP-3 は docs-only なので、差分を revert すれば完了です（実装・テスト・config には影響しません）:

```bash
git revert <commit>   # docs の差分を取り消すだけ
```

## 次の担当者・エージェントへのメモ

### 本番適用を担当する場合

1. **必ず runbook の典型的利用フローを上から下まで実行してください**（[Operations — Multiverse Import Universe](../wiki/Operations-Multiverse-Import-Universe.md#典型的利用フロー)）。dry-run → プロセス停止 → wet run → target で recall 確認 → supervisor 経由で recall 確認 → source cleanup の 6 step です
2. **copy mode を使ってください**（default）。`--move` は失敗時の復旧が手作業になります
3. **`--force` は使わないでください**。事前にプロセスを停止するのが安全側です
4. **api_key 平文は一度しか表示されません**。即座に安全な場所に保存してください
5. **import 後も同じクエリに対して同じ recall 結果を返すのが正常です**。importer は `gaottt.virtual.faiss` も copy 対象とし、target の `engine.startup` は disk から load します（`virtual.faiss` が空 = size==0 の時のみ rebuild）。したがって virtual 位置も含めて mass / temperature / displacement が preserve されます。**もし結果が変わった場合は semantic drift や corruption の可能性がある** ので、`scripts/diag_recall.py snapshot` で source / target を比較してください。詳細は [Troubleshooting](../wiki/Operations-Troubleshooting.md) の該当項目を参照

### importer を拡張する場合

1. **`ImportPlan` dataclass の shape は WP-1 の test contract で固定** されています。field 追加は test の更新が必要
2. **`execute_import(plan, registry) -> str` の signature も固定**。port range を動的に渡したい場合は module 定数 (`_DEFAULT_PORT_RANGE_START` / `_DEFAULT_PORT_RANGE_END`) を変更するか、`registry.allocate_port` 側を拡張してください
3. **`_verify_target_db` の schema gate を強化する場合**（`nodes` table 存在チェック等）、`importer.py` を `gaottt/store/` の schema 定義に coupling する trade-off を評価してください。現状は `engine.startup` に委譲することで coupling を避けています
4. **新 config knob は作らない設計** です（plan §1.3 / §2.5）。`WAL_HARD_REJECT_BYTES` 等の閾値は module-level 定数。config knob 化が必要な場合は [Operations — Tuning](../wiki/Operations-Tuning.md) への追記もセットで

### docs を更新する場合

- **runbook は `docs/wiki/Operations-Multiverse-Import-Universe.md`** が SoT です。実装の挙動が変わった場合は runbook を先に更新してください
- **plan (`docs/maintainers/multiverse-importer-execution-plan.md`)** は PM 管轄です。実装が plan から逸脱した場合は plan の「反映ログ」節に追記し、runbook には実装を正として記載してください（本 handover の「既知の問題 #3」が具体例です）
- **`SKILL.md` / `MCP-Reference-*.md` / `REST-API-Reference.md` は更新不要** です（importer は MCP/REST parity 対象外、管理面の CLI ツール）

### GaOTTT memory 関連

- memory id=4d0069fb「import 必須 5 点」— 本 importer が対応する 5 点（copy / manifest / registry / API key / verify）は全て実装済み
- memory id=58a87d7a「暗黙採用拒否の設計判断」— reconcile が暗黙採用しない設計を importer で明示的に埋めた形
