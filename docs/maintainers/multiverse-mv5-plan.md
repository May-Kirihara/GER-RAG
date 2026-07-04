# MV5 — Backup / DR 実装計画（詳細化作業計画）

> **位置付け**: [multiverse-implementation-plan.md](multiverse-implementation-plan.md) §MV5（要件レベルの作業手順書）を、PM が実装に着手できる作業単位・テスト戦略・設計判断レベルに分解したもの。戦略の SoT は [Plans — Multiverse Scale-Out](../wiki/Plans-Multiverse-Scale-Out.md) §Stage 4、要件は上記 implementation-plan §MV5。本書はそれらを変更せず、実装のために具体化する。
> 起票: 2026-07-04
> リスク分類: **normal** — engine/physics/observation 層は完全不変、新規ファイル中心、default 不変（backup knob は未設定 = inert）。ただし DR は商用ラインのクリティカル経路なので、正常系 + 異常系のテストと docs 整合性を厳格に扱う。

## 0. 成果物と受け入れ基準

> **★ スコープ確定（Codex review 反映）**: MV5 は **per-universe data recovery** に明示的に限定する。復旧対象は各宇宙の `gaottt.db` + `manifest.json` の 2 点セットのみ。以下は **本 stage のスコープ外** として runbook で明示し、商用運用の別作業とする:
> - `<multiverse_root>/registry.db`（supervisor local registry）の復旧 — supervisor は `reconcile()` で on-disk `universes/` から再構築する設計だが、完全復旧（API key hash の連続性・port 割当履歴）は人手作業。runbook に手順を記載
> - MV4 control plane (Postgres) の復旧 — Postgres 自体のバックアップ/復旧は標準的な商用 DB 運用。本 stage は関与しない
> - `backend.token` の復旧 — supervisor が re-spawn 時に再生成するので、復旧後の初回 route で新 token が発行される（既存 MV3 の設計どおり）。旧 token の連続性は保証しない
> - `owner.lock` の復旧 — 復旧時の初回起動で新 lease が取得される。旧 lease の owner_id 連続性は保証しない
>
> この境界は戦略計画 [Plans — Multiverse Scale-Out](../wiki/Plans-Multiverse-Scale-Out.md) §0 の「Postgres が本当に得意なこと（台帳・課金・監査の集計）だけを Postgres にやらせる」方針、および実装計画 §MV5「バックアップ対象 = SQLite + manifest.json の 2 点セット」と整合する。

| 成果物 | acceptance |
|---|---|
| `deploy/litestream.yml`（雛形） | 宇宙ごとに `gaottt.db` を replicate する最小設定。`dbs:` ブロックは `scripts/gen_litestream_config.py` が生成する想定を明記。manifest の別途バックアップ（file-level snapshot）をコメントで明記 |
| `scripts/gen_litestream_config.py` | `<multiverse_root>/universes/*/` を scan し litestream YAML を生成。**YAML のみ stdout**、**diagnostics（WARN/ERROR/INFO）は stderr のみ**（stdout を polluted にしない — Codex review B1）。`--output` 指定時は `tmp + fsync + os.replace` で **atomic write**（Codex review B2）。`trash/` 除外、空 universes は stderr へ WARN、manifest 不在は stderr へ ERROR で継続。`main()` と純粋関数 `generate_litestream_config(root: Path) -> str` の両方を公開（supervisor hook と unit test は後者を直接呼ぶ） |
| `gaottt/multiverse/supervisor.py` への hook 組み込み | `litestream_config_path` knob（default 空 = inert）。`POST /admin/universes` と `DELETE /admin/universes/{uid}` の成功経路で、knob 設定時のみ **純粋関数を direct import で呼び**（subprocess ではない — 例外隔離と testability を両立）、atomic write で設定ファイルを再生成。**例外は supervisor の応答に影響しない**（best-effort、ERROR log）。**spawn env には絶対に渡さない**（`_build_spawn_env` は allowlist のまま、Codex review #7） |
| `scripts/dr_drill.py` | tmp multiverse_root + StubEmbedder で **standalone（managed=False）宇宙** を populate → backup（生 SQLite+manifest copy、`--with-litestream` で litestream binary も検証） → 宇宙ディレクトリ破壊 → restore → `rebuild_faiss_from_db.py --apply` → `--check` → engine.startup して `run_startup_checks` が Tier A/B で ERROR なしを assert → exit 0 = 成功。**主張は「standalone 宇宙について、SQLite + manifest + 同一 embedder artifact があれば engine レベルの復旧ができる」に明示的に限定**（managed 宇宙の完全復旧は runbook の人手作業、Codex review B3） |
| `docs/wiki/Operations-Backup-Multiverse.md` | 新設。**§スコープ** で per-universe data recovery への限定を明記、backup 対象 = SQLite + manifest.json の 2 点セット、embedder artifact pinning（HF model の tar 退避 or 社内ミラー）、DR runbook（他プロセス停止 → restore → model 用意 → FAISS rebuild → 診断）、大宇宙向け optional FAISS snapshot、Litestream 雛形の使い方、dr_drill の四半期実行、**商用導入前チェックリスト**（litestream binary 実機試験 / managed 宇宙 DR drill / registry 復旧手順 / Postgres 復旧は別運用） |
| `docs/wiki/_Sidebar.md` / `docs/wiki/Home.md` / `docs/wiki/Operations-Compact-And-Backup.md` / `docs/wiki/Operations-Tuning.md` / `docs/wiki/Architecture-Overview.md` / `docs/maintainers/multiverse-implementation-plan.md` / `docs/wiki/Plans-Multiverse-Scale-Out.md` | 相互リンク、backup knob 追加、MV5 完了マーク、戦略計画 Stage 4 に「実装済み」マーク |
| 既存 suite 全緑 + 両 smoke + ruff | default 不変の回帰確認 |

## 1. 設計判断（確定）

### D1. backup 対象は SQLite + manifest.json の 2 点セット
FAISS は [`scripts/rebuild_faiss_from_db.py`](../../scripts/rebuild_faiss_from_db.py) で **同一 embedder artifact + 同一 version + 同一 prefix/normalization** が揃えば決定論的に再構築可能（2026-05-31 incident 復旧で実証）。manifest.json は embedder identity + 次元を保持し、復旧後の `verify_embedder_identity` の整合ガードの鍵。このため **2 点セットで保つ**。FAISS をデフォルトで replicate しない理由は、WAL stream と衝突しない + RPO/RTO の対象を最小化するため。大宇宙向けには FAISS snapshot も optional で設定例を併記（RTO 短縮）。

**manifest のバックアップ経路（Codex review 反映）**: litestream は SQLite WAL のみを replicate し、manifest.json を同時に capture しない。よって manifest のバックアップは **別経路** で必須:
- `deploy/litestream.yml` に `exec` command で `<multiverse_root>/universes/*/manifest.json` を file-level に snapshot する例を併記（rsync / cp -a で別ディレクトリまたは S3 prefix へ）
- 運用としては `<multiverse_root>` 全体を filesystem snapshot（ZFS / Btrfs / LVM snapshot）するのが最も堅牢。runbook に両方の選択肢を明記
- **dr_drill は SQLite + manifest を 1 単位として backup/restore する**（両方同時に copy する）ので、この「2 点セット」制約を自動的に証明する

### D2. supervisor hook は default 不変・best-effort・import-based・直列化
`litestream_config_path` knob（default `""` = 機能不使用）。未設定時は hook は一切呼ばれず、既存の MV3-only / MV4 構成は **1 行も変わらず** 動く。設定時は `create_universe` / `delete_universe` の **成功経路の最後**（registry commit / trash move の後）で `generate_litestream_config` 純粋関数を **direct import** で呼び、atomic write で出力ファイルを更新。失敗時は ERROR log のみ（supervisor 忚答は成功のまま）。これにより「backup 設定ミスで宇宙作成が倒れる」事故を構造的に防ぐ。

**import-based（subprocess ではない）理由**: (a) 例外の隔離 — 関数呼び出しの try/except で確実、(b) testability — monkeypatch で失敗注入が容易、(c) レイテンシ — subprocess 起動の overhead 無し。**制約**: `generate_litestream_config` は disk IO（multiverse_root scan + atomic write）のみを行い、network / 外部 binary に依存しない純粋関数として実装する。

**hook 呼び出しの直列化（Codex review round-2 B2 反映 — 必須）**: hook の scan と atomic write を **専用の `_backup_hook_lock: asyncio.Lock`** で包み、**臨界区間内で scan → atomic write を一気に行う**。これにより:
- 複数の create / delete hook が並行発火しても、各々が最新の on-disk 状態を scan → write する直列化された列になる
- 最後の writer の scan 時点での状態が最終 YAML 内容（out-of-order 完了で stale に陥らない — scan と write が分離されないため）
- create_lock / spawn_lock とは独立した lock なので、create / delete 自体の並行性は維持される（hook だけ直列化）

**なぜ last-writer-wins だけでは不効だったか**: atomic write は torn YAML を防ぐが、scan と write が別々の hook instance で行われると「scan_A → write_B → write_A」の順で最終的に A の scan 結果（古い状態）が最終 YAML になる。scan と write を同一臨界区間に閉じ込めることでこの stale write を構造的に排除する。

**spawn env への隔離（Codex review #7 反映）**: `_build_spawn_env` の allowlist に litestream / backup 関連 knob は絶対に渡さない。hook は supervisor プロセス内のみで動き、backend には漏れない。WP-2 test で assert。

### D3. dr_drill は litestream binary 非依存を default に
litestream は Go binary で配布され、CI / 全環境に入るとは限らない。dr_drill の本質は **「SQLite + manifest だけで復元できる」の証明** であり、litestream 特有の経路ではない。よって default は **生ファイルコピー** で backup/restore を検証し、`--with-litestream` オプションで litestream binary 経路も検証する。両経路で同じ acceptance（FAISS rebuild → 起動時診断 green）を課す。これで litestream 未導入環境でも四半期 drill が実行できる。litestream binary 不在時は `--with-litestream` を WARN skip し、商用導入前チェックリスト（docs）で別途試験することを明示。

### D4. dr_drill の主張は standalone 宇宙に明示的に限定（Codex review B3 反映）
dr_drill は `ensure_manifest` で生成される **standalone（managed=False）宇宙** で実行する。証明する主張は:
> 「standalone 宇宙について、`gaottt.db` + `manifest.json` + embedder artifact（model weights）が揃えば、FAISS rebuild → engine.startup → 起動時診断 green までの engine レベル復旧ができる」

**主張しないこと**（明示的に runbook で案内、自動検証対象外）:
- managed 宇宙（`manifest.managed=True`）の復旧 — lease / backend token / registry 行 / control plane 同期を含む完全復旧は人手作業
- 上記を dr_drill に含めない理由: (a) lease は復旧時の初回起動で新規取得される（旧 owner_id 連続性は保証外）、(b) backend token は re-spawn 時に再生成される、(c) registry.db は supervisor の `reconcile()` で on-disk から再構築できるが、API key 連続性は人手で再発行が必要、(d) control plane (Postgres) は別運用

managed 宇宙の完全復旧手順は `Operations-Backup-Multiverse.md` の runbook で人手手順として明示する（supervisor 停止 → 各宇宙 restore → `reconcile()` → API key 再発行 → control plane 再同期）。

### D5. MCP/REST parity 鉄則は適用外
backup は管理面（`/reset` と同じ例外クラス、実装計画書 §0 ルール 4）。新 MCP ツール 0、新 REST エンドポイント 0 が本 stage の原則。supervisor admin API の hook 拡張も parity 対象外（[Architecture — Overview](../wiki/Architecture-Overview.md) 設計判断表「Multiverse supervisor は MCP/REST parity 対象外」と整合）。

### D6. generator 出力は stdout YAML + stderr diagnostics（Codex review B1 反映）
`generate_litestream_config(root: Path) -> str`（YAML 文字列を返す純粋関数）。`main()` は YAML を stdout へ、diagnostics（WARN / ERROR / INFO）を **stderr のみ** へ出力。`--output FILE` 指定時は YAML をファイルへ atomic write し、stdout は何も出さない（成功時）または diagnostics のみ（stderr）。これにより litestream が stdout を parse する経路と人間が読む経路の両方で YAML が valid に保たれる。

## 2. Work Package 分割（直列）

依存関係: WP-1 → WP-2（hook が gen を呼ぶ）→ WP-3（dr_drill が gen + hook の存在を前提）→ WP-4（docs が全 WP の機能を記述）。並列化しない（WP 間の所有ファイルが重複しないが、機能依存が強く、並列の利益 < 統合リスク）。

### WP-1: gen_litestream_config + 雏形
- 新規 `scripts/gen_litestream_config.py`（CLI `main()` + 純粋関数 `generate_litestream_config(root: Path) -> str`）
- 新規 `deploy/litestream.yml`（雏形、`dbs:` ブロックは gen が生成すると明記、manifest の別途 backup を `exec` 例で明記）
- 新規 `tests/unit/test_gen_litestream_config.py`
- **allowed paths**: 上記 3 ファイル + `tests/unit/`
- **forbidden**: `gaottt/`（supervisor.py 含む）、他の scripts、docs
- **acceptance**:
  - 純粋関数 `generate_litestream_config(root)` が multiverse_root を scan して YAML 文字列を返す
  - `trash/` を除外
  - 空 universes は stderr へ WARN（stdout は valid YAML を保つ）
  - manifest 不在は stderr へ ERROR で継続
  - `*.db` 以外のファイルは無視
  - YAML 構造が litestream の schema に合致（`yaml.safe_load` で parse して `dbs` 列長と `path` / `replicas` の key を assert）
  - **atomic write test**: `--output FILE` で出力時、`tmp + fsync + os.replace` で書かれ、generator が失敗したとき既存 file が生き残る（Codex review B2 / missing test）
  - **stdout/stderr 分離 test**: stdout は常に valid YAML、WARN/ERROR は stderr のみ（Codex review B1 / missing test）
  - **trash race test**: 削除中の宇宙（`trash/` へ移動済み）が生成 YAML に含まれない

### WP-2: supervisor hook 組み込み
- 変更 `gaottt/config.py`（`litestream_config_path: str = ""` 追加、generic env-override loop が拾う。**`backup_gen_timeout_seconds` は追加しない** — import-based なので timeout は不要、Knob を置くと誤解を招く、Codex review round-2 #5）
- 変更 `gaottt/multiverse/supervisor.py`（create / delete の成功経路で hook を呼ぶ、default 不変、**専用 `_backup_hook_lock` で scan+write を直列化**、**spawn env には渡さない**）
- 新規 `tests/integration/test_supervisor_backup_hook.py`
- **allowed paths**: 上記 + `tests/integration/`
- **forbidden**: 他の `gaottt/`、`scripts/`、docs、`deploy/`
- **acceptance**:
  - knob 未設定時は hook が一切呼ばれない（既存 `test_supervisor.py` が無変更で green — default 不変の回帰 fence）
  - 設定時は create と delete の成功経路で YAML が atomic write で再生成される
  - hook 例外（`generate_litestream_config` を monkeypatch で raise）でも HTTP 応答は成功、ERROR log が出る
  - **並行 create/delete 後の YAML が on-disk の最終状態と一致**（Codex review round-2 B2 / missing test）— `asyncio.gather` で 5 個の create と 2 個の delete を同時発火し、最終 YAML が (a) parse 可能、(b) `dbs` 列が on-disk の live universes と 1:1（過不足なし）。これが `_backup_hook_lock` の scan+write 直列化が stale write を防ぐことの検証
  - **spawn env 隔離**: `_build_spawn_env` の出力に `LITESTREAM` / `BACKUP` を含まないことを assert（Codex review #7 / default-invariant）

### WP-3: dr_drill
- 新規 `scripts/dr_drill.py`（本体は `run_drill(root, with_litestream=False) -> int` 関数、`main()` は argparse のみ）
- 新規 `tests/integration/test_dr_drill.py`（pytest から `run_drill()` を実行）
- **allowed paths**: 上記 + `tests/integration/`
- **forbidden**: `gaottt/`（drill は black-box）、`deploy/`、docs
- **acceptance**:
  - tmp 宇宙 + StubEmbedder で remember → recall round-trip 成功（drill 前提の data があること）
  - backup（生 copy）で SQLite + manifest を **両方** 取る（D1 の 2 点セット制約）
  - 宇宙ディレクトリ破壊 → restore（両方）
  - `rebuild_faiss_from_db.py --apply` → `--check` で整合（ベクトル数 ≒ documents 数、決定論性は仮定 2 で担保）
  - engine.startup で `run_startup_checks` の Tier A/B が ERROR 0 件
  - exit 0 = 成功
  - **`--with-litestream` は litestream binary 不在時は WARN skip**（default 経路は必ず走る、Codex review #3）
  - **manifest 一貫性失敗 test**: DB だけ restore して manifest を忘れた場合は `verify_embedder_identity` で RuntimeError（復旧手順の誤りを検出する fence、Codex review missing test）
  - **standalone 限定であることの明示**: drill の冒頭 log と docs で「managed 宇宙の復旧は別途 runbook」と明示

### WP-4: ドキュメント
- 新規 `docs/wiki/Operations-Backup-Multiverse.md`
- 変更 `docs/wiki/_Sidebar.md`（Operations 節に行追加）
- 変更 `docs/wiki/Home.md`（Operations の一覧に追加、該当節が存在する場合）
- 変更 `docs/wiki/Operations-Compact-And-Backup.md`（Multiverse バックアップへの相互リンク節）
- 変更 `docs/wiki/Operations-Tuning.md`（backup knob 1 つ、`litestream_config_path`）
- 変更 `docs/wiki/Architecture-Overview.md`（設計判断表は確認のみ、既存「Multiverse supervisor は parity 対象外」行でカバーされるはず — なければ 1 行追加）
- 変更 `docs/maintainers/multiverse-implementation-plan.md`（MV5 完了マーク）
- 変更 `docs/wiki/Plans-Multiverse-Scale-Out.md`（Stage 4 に「実装済み」マーク、最終更新日更新）
- **allowed paths**: 上記 docs のみ
- **forbidden**: コード、scripts、tests
- **acceptance**:
  - 新ページのリンク切れなし
  - Sidebar のカテゴリ構造維持（Operations 節、アイコン付き）
  - **§スコープ** で per-universe data recovery への限定を明示（D4 / Codex review B3）
  - runbook が他プロセス停止（owner lease 確認）→ model 用意 → FAISS rebuild → 診断の順を明示
  - embedder artifact pinning を必須項目化
  - **商用導入前チェックリスト** 節を設け、litestream binary 実機試験 / managed 宇宙 DR drill（人手）/ registry 復旧手順 / Postgres 復旧は別運用を明記

## 3. テスト戦略

| WP | test | 内容 |
|---|---|---|
| WP-1 | `tests/unit/test_gen_litestream_config.py` | 純粋関数（multiverse_root → YAML 文字列）の単体テスト。空 root / 宇宙 1 つ / 宇宙 3 つ / trash 除外 / manifest 不在 / `*.db` 以外のファイル無視 / YAML 構造（`yaml.safe_load` で parse して `dbs` 列長と `path`/`replicas` の key を assert）/ **stdout valid YAML + stderr diagnostics の分離**（Codex review B1）/ **atomic write**（`--output` で generator 失敗時に既存 file が生き残る、Codex review B2）/ **trash race**（削除済み宇宙が YAML に含まれない）|
| WP-2 | `tests/integration/test_supervisor_backup_hook.py` | ASGITransport で supervisor を立てて `litestream_config_path` を tmp パスに設定。create → delete → create の順で YAML が atomic write で再生成されることを assert。knob 未設定時は file が作られないことも assert（default 不変の回帰 fence）。**hook が例外を吐く設定**（`generate_litestream_config` を monkeypatch で raise）でも HTTP 応答が成功すること。**並行 create/delete**（`asyncio.gather` ×5 create + ×2 delete）の最終 YAML が (a) parse 可能、(b) `dbs` 列が on-disk の live universes と 1:1 — `_backup_hook_lock` の scan+write 直列化が stale write を防ぐことの検証（Codex review round-2 B2）。**spawn env 隔離**（`_build_spawn_env` の出力に `LITESTREAM` / `BACKUP` を含まない）|
| WP-3 | `tests/integration/test_dr_drill.py` | `run_drill()` 関数を pytest から実行。tmp multiverse_root + StubEmbedder。complete 経路の exit 0 を assert。`--with-litestream` は binary 不在検出をして skip する経路も assert。**manifest 一貫性失敗 test**（DB だけ restore すると `verify_embedder_identity` が RuntimeError で復旧手順の誤りを検出）。**standalone 限定の log 出力** を assert |
| 全体 | 既存 suite 全緑 | `pytest tests/ -q` / `ruff check gaottt/ tests/` / `rest_smoke.py` / `mcp_smoke.py`（default 不変の回帰確認）|

**Test-first 原則**: WP-1 と WP-2 はテストを先に書いてから実装。WP-3 は本体が統合テスト的性格を持つので、テストと本体を 1 PR で出す。

**Codex review で指摘されたが本 stage では対象外の test**（runbook / 商用導入前チェックリストで扱う）:
- managed 宇宙の完全復旧（lease / backend token / registry / control plane 再同期）— D4 で standalone に限定
- litestream binary を実際に立ち上げての WAL restore e2e — 外部依存、商用導入前チェックリスト
- MV4 control plane (Postgres) の復旧 — 本 stage のスコープ外（Postgres は別運用）

これらは docs の「商用導入前チェックリスト」節に明示し、dr_drill や unit test では扱わない。

## 4. 仮定台帳（assumption / basis / falsification / blast radius）

1. **仮定**: litestream バイナリはデプロイ時に別途インストールされ、Python パッケージではない。かつ litestream binary の発見可能性は運用環境の責務。
   - **根拠**: litestream は Go binary で GitHub releases から配布。実装計画書 §0 ルール 3 も `pyproject.toml` の optional extra 対象外を示唆。
   - **反証条件**: (a) `pyproject.toml` に litestream 依存を足す必要が出た場合、設計をやり直す。(b) `shutil.which("litestream")` が None を返す環境で `--with-litestream` を実行した場合、WARN skip すること（WP-3 test で検証）。
   - **blast radius**: 中。dr_drill の default は生 copy で動くので litestream 未導入でも drill は実行可能、商用の継続バックアップのみ影響を受ける。

2. **仮定**: StubEmbedder の encode は **入力に対して決定論的**（同一入力で同一ベクトル）。ベクトル数の一致だけでなく、固定入力での top-K 結果が stable。
   - **根拠**: `tests/integration/test_engine_archive_ttl.py:StubEmbedder` はトークンベースの決定論的埋め込みを使う（CLAUDE.md の「よくある罠」）。
   - **反証条件**: WP-3 で `rebuild_faiss_from_db.py --apply` 後のベクトル数と DB documents 数が一致しない、または固定 query で top-K が drill 前後で変わる。
   - **blast radius**: 中。dr_drill が FAISS rebuild 段階で失敗するが、設計上の問題ではなく StubEmbedder の前提崩壊を意味する。WP-3 test で固定 query の top-K 一致を assert して早期検出。

3. **仮定**: supervisor の create / delete 後に同期関数呼び出し（`generate_litestream_config` direct import）をしても API レイテンシに実害はない。
   - **根拠**: 純粋関数は disk IO のみ（数十宇宙で数十 ms）、subprocess 起動の overhead 無し。
   - **反証条件**: 100 宇宙の multiverse_root で create API の p95 が著しく悪化する（>1s）。WP-2 test で timing を log し、閾値超過で WARNING。
   - **blast radius**: 低。hook は best-effort で、例外は ERROR log のみ。

4. **仮定**: manifest の別途バックアップ（litestream は SQLite WAL のみを replicate するので manifest は対象外）は file-level で確実に行える。file-level backup の atomicity は「SQLite のある時点の WAL snapshot と manifest の snapshot が完全に同期しなくても、復旧後の embedder identity 照合が成功する」で十分。
   - **根拠**: manifest は作成時に 1 回書かれ、embedder identity は不変（宇宙の生存期間中に変わらない）。SQLite の WAL と manifest の snapshot が数秒ズレても、embedder identity は同一なので `verify_embedder_identity` は成功する。
   - **反証条件**: 運用中に embedder の version が変わり manifest が update されるケース（現状 v1 では warning-only なので起きない）で、manifest の snapshot が古いと復旧後の version 照合で warning が出る。
   - **blast radius**: 低。warning-only なので起動は block しない。WP-3 test で manifest の別途 backup が取られることを assert して確認。

5. **仮定**: WP-3 の dr_drill は standalone（`managed=False`）の tmp 宇宙で実行し、owner lease は発動しない。managed 宇宙の復旧は別途人手作業。
   - **根拠**: lease 強制の発動条件は `owner_lease_enabled OR manifest.managed`（MV2）。dr_drill は `ensure_manifest` で生成される standalone manifest を使うので `managed=False`。
   - **反証条件**: dr_drill が engine を 2 つ同時に開いて `LeaseHeldError` が出る。
   - **blast radius**: 低。dr_drill は逐次実行で engine を 1 つずつ開く。実運用（managed 宇宙）の DR runbook は別途 docs で supervisor 停止 / `--force-takeover` を明示する。

6. **仮定（Codex review で指摘、追加）**: `<multiverse_root>/registry.db` の復旧は supervisor の `reconcile()` で on-disk `universes/` から再構築できるが、**API key の hash 連続性は保証されない**。復旧後は API key 再発行が必要。
   - **根拠**: `MultiverseRegistry.reconcile()` は on-disk `universes/` と registry を突き合わせるが、directory-only の宇宙は WARNING + skip（`registry.py:319-325`）。API key は平文を保存しない設計（hash only）なので、復旧時には再発行しかない。
   - **反証条件**: 復旧後に旧 API key で `/route` が通ることを期待する test が通ってしまう（= hash が復元できている）。現状は通らないはず。
   - **blast radius**: 中。商用運用で「退会 → 再開」時に API key 再発行が必要になることをユーザーに事前告知する必要がある。runbook に明記。

7. **仮定（Codex review round-2 で強化）**: WP-2 の hook は supervisor プロセス内の asyncio task として実行され、**専用 `_backup_hook_lock` で scan と atomic write を直列化する**ことで並行 create / delete に対して stale write を防ぐ。
   - **根拠**: hook は「scan on-disk multiverse_root → 純粋変換 → atomic write」を 1 つの asyncio 臨界区間で行う。create / delete の成功経路が hook を発火する時点で、当該操作の on-disk 副作用（dir 作成 / trash move）は完了済み。臨界区間内での scan はその時点の最新状態を取り、write は同じ臨界区間内で行われるので、別の hook の scan 結果で上書きされる余地がない。
   - **反証条件**: 並行 create/delete 後の YAML が parse 不能、または `dbs` 列が on-disk の live universes と 1:1 でない。WP-2 test で検証（5 create + 2 delete を `asyncio.gather` で同時発火）。
   - **blast radius**: 中。stale YAML は litestream が universe を取りこぼす原因になるが、`_backup_hook_lock` で構造的に防がれる。verified by WP-2 並行 test。

## 5. 反証的に検証すべきリスク

- **R1. litestream 雛形が実機で動くか**: 雛形は docs と deploy/ に置くが、本 stage では litestream binary を実際に立ち上げての e2e は含めない（外部依存）。dr_drill の `--with-litestream` は binary があれば通すが、無ければ skip する。商用運用前の別作業として litestream 実機試験が必要 — これを docs の「商用導入前チェックリスト」に明記する。
- **R2. supervisor hook の並行性（§1 D2 / §4 仮定 7 と整合）**: 2 つの同時 create / delete が同時に hook を発火する可能性がある。本 stage の設計では `generate_litestream_config` の **scan + atomic write を専用 `_backup_hook_lock`（asyncio.Lock）の臨界区間内で行う** ことで、並行 hook が直列化され、最後の writer の scan 時点での on-disk 状態が最終 YAML になる（out-of-order 完了で stale に陥らない — 詳細は §1 D2 と §4 仮定 7）。**「last-writer-wins で安全」という主張は採用しない**（round-2 review で否定済み — atomic write 単独では stale write を防げない）。検証は WP-2 integration test で `asyncio.gather` による 5 create + 2 delete を同時発火し、最終 YAML の `dbs` 列が on-disk の live universes と 1:1 であることを assert する。
- **R3. embedder artifact の実際の pin 手法**: runbook に「HF cache の tar 退避 or 社内ミラー」と書くが、具体的なコマンドは環境依存。Phase 5（MV6, EN embedder）で本格化するまで v1 は「必須項目として明記」にとどめ、具体的手順の自動化は将来 stage。
- **R4. 新規宇宙が litestream config に現れるまでのラグ（Codex FINAL review B1）**: `create_universe` の hook は宇宙ディレクトリ + manifest 作成直後に発火するが、この時点では backend が未起動で `gaottt.db` が存在しないため、`generate_litestream_config` の db-required scan rule が新規宇宙を WARN skip する。結果、新規宇宙は「最初の `/route` で backend が db を作る + その後の hook 再発火」が起きるまで config に現れない。
  - **v1 の構造的緩和（採用）**: `_ensure_locked` の fresh-spawn readiness 成功経路（backend が db を作成した直後）でも `_run_backup_hook()` を発火するよう実装した。これで新規宇宙の **最初の route** で config に現れる経路が閉じる（knob 未設定時は no-op で default 不変）。
  - **決定的な閉包（併用推奨）**: 「最初の route が来る前」の窓を埋めるため、litestream config の **定期再生成 cron**（例: 毎時 `gen_litestream_config.py --output`）を runbook で推奨。これにより取りこぼされた宇宙が遅くとも次の cron 周期で config に現れる。
  - **将来 stage（見送り）**: route/ensure machinery に手を入れる完全な構造的 fix（hook on backend readiness event の完全統合等）は regression リスクから本 stage のスコープ外。許容根拠: backup は best-effort・default inert であり、定期再生成 cron で運用上の取りこぼしは閉じる。

## 6. ロールバック

- WP-1 / WP-3 は新規ファイルのみ。削除すれば完全撤去。
- WP-2 は `litestream_config_path` 未設定で完全 inert（既存の MV3/MV4 構成は無影響）。knob を残したまま hook を無効化する escape hatch は作らない（設定しなければ効かないのが escape の本体）。
- WP-4 は docs のみ。差し戻しは git revert。

## 7. Codex plan review 履歴

- **第 1 巡（2026-07-04）**: blocking 3 件（generator stdout/stdout 分離 / atomic write / DR 境界の曖昧さ）+ non-blocking + missing tests + 仮定台帳の弱さ。本書の §0 スコープ確定 / §1 D2/D4/D6 / §2 acceptance / §3 test 戦略 / §4 仮定 6/7 追加 に反映。
- **第 2 巡（2026-07-04）**: B1/B3 closed。B2 partial（atomic write は入ったが、並行性の過剰主張「last-writer-wins で安全」が残り、stale write を防げない）。新 blocking「並行安全性の過剰主張」。`backup_gen_timeout_seconds` knob 削除推奨。本書の §1 D2（`_backup_hook_lock` で scan+write 直列化）/ §2 WP-2 acceptance（並行 test が on-disk 状態と 1:1 を assert）/ §3 test 表 / §4 仮定 7 強化 / §2 WP-2 knob 削除 に反映。第 3 巡 review にて再確認予定。
- **FINAL diff review（2026-07-04）**: 4 blocking を PM decision で解決。B1（新規宇宙の config ラグ）→ §5 R4 に記録。構造的 fix の一部（`_ensure_locked` fresh-spawn 成功経路での hook 発火）は low-risk として採用、残る窓は定期再生成 cron で運用閉包。B2（並行 test の settle rescan）→ docstring で意味を明示、test logic 不変。B3（spawn env の credential leak）→ docs/deploy template で credential 分離を明示、`_build_spawn_env` は構造的 hardening を見送り（別 stage）。B4（`--with-litestream` の silent ignore）→ ERROR log 化 + help/docstring 更新、drill の exit code は不変。non-blocking 2 件（parent-dir fsync / uv.lock）も適用。

## 7. 完了条件（normal gate matrix 適用）

- [ ] WP-1 unit test green
- [ ] WP-2 integration test green + 既存 `test_supervisor.py` 全緑（default 不変）
- [ ] WP-3 integration test green + dr_drill.py の手動実行で exit 0
- [ ] WP-4 docs 相互リンク切れなし / Sidebar 構造維持
- [ ] `pytest tests/ -q` 全緑
- [ ] `ruff check gaottt/ tests/`（pre-existing 4 件のみ）
- [ ] `scripts/rest_smoke.py` && `scripts/mcp_smoke.py` green
- [ ] 実装計画書 §MV5 の完了マーク更新
- [ ] Codex CLI 最終 diff review（blocking なし）
- [ ] GaOTTT writeback（durable な決定・教訓）

## 8. 関連

- [multiverse-implementation-plan.md](multiverse-implementation-plan.md) §MV5 — 要件レベルの作業手順書（本書の上位）
- [Plans — Multiverse Scale-Out](../wiki/Plans-Multiverse-Scale-Out.md) §Stage 4 — 戦略計画（SoT）
- [Operations — Multiverse Setup](../wiki/Operations-Multiverse-Setup.md) — MV3 運用（本 stage の hook が載る土台）
- [Operations — Compact & Backup](../wiki/Operations-Compact-And-Backup.md) — standalone 構成のバックアップ（相互リンク）
- [Operations — Control Plane](../wiki/Operations-Control-Plane.md) — MV4（telemetry は backup と直交）
