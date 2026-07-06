# Multiverse Importer — PM Execution Plan (v3)

> **目的**: 既存 standalone `data_dir`（単一ユーザー構成の `gaottt.db` + FAISS 群）を multiverse layout（`<multiverse_root>/universes/<uid>/`）の 1 宇宙として取り込む importer を実装する。MV3 完了後の追加機能であり、別途 MV4/MV5 と独立して進行可能。
>
> 起票: 2026-07-04 (v2: QA plan review 反映、v3: Codex plan review 反映)
> リスク分類: **high-risk**（本番 760MB DB 移行を想定したツール、data-loss surface、多層メタデータ操作）
> 前提: MV0（manifest）/ MV2（lease）/ MV3（registry + supervisor）完了済み
> SoT 関係: 本書が importer 仕様の SoT。戦略論は [Plans — Multiverse Scale-Out](../wiki/Plans-Multiverse-Scale-Out.md)、全体実装計画は [multiverse-implementation-plan.md](multiverse-implementation-plan.md)。前例形式: [multiverse-mv4-execution-plan.md](multiverse-mv4-execution-plan.md)
> 関連 GaOTTT memory: id=4d0069fb（必須 5 点チェックリスト）, id=58a87d7a（暗黙採用拒否の設計判断）

## 1. 背景・スコープ

### 1.1 背景

MV3 の `MultiverseRegistry.reconcile()` は「supervisor が作っていない宇宙を暗黙採用しない」設計（`gaottt/multiverse/registry.py:293-325`、directory present + registry missing → WARNING + skip）。`POST /admin/universes` も新規空宇宙作成専用（`uuid4().hex[:12]` 新規発行、`universe_dir.mkdir` で空ディレクトリを作るだけ）。結果として **「既存 standalone DB を multiverse に import する機能」は意図的に存在しない**。

ユーザーは現在 `~/.local/share/gaottt/`（gaottt.db = 760MB / gaottt.db-wal = 176MB / FAISS = 122MB / virtual.faiss = 122MB、本日稼働中）を multiverse の 1 宇宙として取り込みたい。この gap を埋める importer を実装する。

### 1.2 スコープ（やること）

- `scripts/import_universe.py` — CLI importer（dry-run / copy / move）
- `gaottt/multiverse/importer.py` — 純粋関数・ビジネスロジック層（テスト可能）
- `tests/unit/test_importer.py` — 純粋関数の unit test
- `tests/integration/test_import_universe.py` — source → target → recall round-trip + **supervisor spawn e2e**
- `docs/wiki/Operations-Multiverse-Import-Universe.md` — 運用 runbook + importer リファレンス
- `docs/maintainers/handover-2026-07-04-multiverse-importer.md` — handover note

### 1.3 非スコープ（やらないこと）

- **本番 DB の実際の移行実行** — 別セッションで importer 完成・検証後にメンテ窓として実施
- **supervisor admin API への `POST /admin/universes/import` 追加** — importer script 単体で完結させる（supervisor 起動中・停止中 両方で動く）。admin API 拡張は別 PR
- **control plane (MV4) への直接登録** — supervisor の次回起動時の `reconcile` + `control_client.pull` で自動反映（J5「local が一次」原則）
- **MV6 英語宇宙対応** — importer は embedder_id を source から読むので多 embedder でも動くが、EN 宇宙専用機能は追加しない
- **physics 層への接触** — `core/gravity.py` / `core/scorer.py` は 1 行も変更しない（横断ルール 1）

### 1.4 リスク分類: high-risk の理由

1. 760MB 本番 DB の data-loss surface（copy 中の crash、移行先 filesystem の空き容量等）
2. 多層メタデータ操作（manifest / port / registry / lease / token / control plane）
3. embedder identity 切り替え（standalone は in-process RuriEmbedder、multiverse は RemoteEmbedder 必須）で backend 起動失敗の可能性
4. write-behind 逆方向上書き罠（過去に本番で 2 件への激減事故発生、CLAUDE.md「マルチプロセス / 共有 DB の罠」参照）

## 2. 設計

### 2.1 ファイル構成

```
gaottt/multiverse/importer.py          # 純粋関数・ビジネスロジック
scripts/import_universe.py             # CLI entry point
tests/unit/test_importer.py            # unit test
tests/integration/test_import_universe.py  # integration test（@slow 含む）
docs/wiki/Operations-Multiverse-Import-Universe.md  # 運用 runbook
docs/maintainers/handover-2026-07-04-multiverse-importer.md  # handover
```

### 2.2 CLI 仕様

```bash
.venv/bin/python scripts/import_universe.py \
    --source ~/.local/share/gaottt \
    --owner-label "main" \
    [--universe-id <12-hex-lower>] \
    [--multiverse-root <path>] \
    [--move] \
    [--force] \
    [--yes] \
    [--dry-run] \
    [--embedder-id <id>] \
    [--embedder-version <v>]
```

| 引数 | 必須 | default | 意味 |
|---|---|---|---|
| `--source` | ✅ | — | 既存 data_dir（gaottt.db 等を含む） |
| `--owner-label` | ✅ | — | 宇宙の owner label（registry に記録） |
| `--universe-id` | — | `uuid4().hex[:12]` | 宇宙 ID。**12 文字 lowercase hex** 必須（違えば exit 2）。重複時は exit 2 |
| `--multiverse-root` | — | config の `multiverse_root` | multiverse root。config + CLI 両方空なら exit 2 |
| `--move` | — | False（copy） | 指定時は source から target へ移動 |
| `--force` | — | False | source backend 停止確認を迂回（非推奨、複数行 WARNING を stderr 出力） |
| `--yes` | — | False | TTY confirm prompt を迂回。**non-TTY 環境（CI / pipe）では `--yes` 必須**（無ければ exit 2） |
| `--dry-run` | — | False | 実行せず計画表示 |
| `--embedder-id` | — | source manifest or config | embedder identity 上書き |
| `--embedder-version` | — | source manifest or "unpinned" | embedder version 上書き |

**`reset_masses.py` との dry-run default 逆転の理由**: `reset_masses.py` は `--apply` 必須（dry-run default）、importer は `--dry-run` 指定（wet default）。理由: reset_masses は全ノード破壊的変更なので dry が安全側、importer は confirm prompt（TTY）+ `--yes` 必須（non-TTY）で安全側を担保しつつ、dry-run を明示することで「本当に wet で走らせたい」意図を明確化する設計。

### 2.3 実行フロー

```
1. 引数解析 + config ロード
   ├─ uid format validation（12 lowercase hex、違えば exit 2）
   ├─ non-TTY + --yes 無し → exit 2
   └─ multiverse_root 空（config + CLI 両方）→ exit 2 + 案内
2. 健全性チェック
   ├─ source が存在し、ディレクトリである
   ├─ source に gaottt.db が存在する
   ├─ target uid 重複チェック（directory exists or registry row exists）
   └─ 起動中プロセスチェック（_running_gaottt_pids + backend port 7878 probe）
      → 検出時は --force で迂回（複数行 WARNING を stderr、追加で post-copy integrity check を必須化）、それ以外は exit 3
3. embedder identity 決定（優先度: CLI override > source manifest > config）
   ├─ 注意: config.embedding_dim は runtime expectation であって source FAISS の次元数の証明ではない。
   │   真の検証は import 後の engine.startup() が FAISS load 時に行う（verify_embedder_identity + FAISS index size）
4. embedder service 検証（config.embedder_endpoint 設定時のみ、hard check）
   ├─ endpoint 設定時 + service unreachable / 4xx/5xx → reject（exit 4）
   │   理由: import しても backend が起動しない
   ├─ endpoint 設定時 + /info が manifest identity と不一致 → reject（exit 4）
   ├─ /info は「現 embedder service が manifest identity と一致すること」を証明するのみ。
   │   過去の vector 生成 model まで保証しない（semantic drift は import 時には falsify 不可）
   └─ endpoint 未設定時 = WARNING のみ（standalone → standalone 移行を許容）
5. WAL size check（warning 閾値: 64MB、hard reject 閾値: 256MB）
   ├─ > 256MB → exit 5（先に MCP server を正常 shutdown して WAL checkpoint させる）
   └─ > 64MB → WARNING 表示（--yes で続行可）
6. file plan 構築（compute_file_plan）
   ├─ copy 対象（完全一致 7 file、ただし存在するもののみ）:
   │   gaottt.db / gaottt.db-shm / gaottt.db-wal
   │   gaottt.faiss / gaottt.faiss.ids
   │   gaottt.virtual.faiss / gaottt.virtual.faiss.ids
   ├─ -wal / -shm が無い場合は正常（checkpoint 済み）として扱う。suspicious として扱わない。
   └─ 除外（backup / 一時 / 別用途）:
        *.bak / *.before-* / *.post-* / *.broken-* / *.tmp
        manifest.json（新規生成）/ owner.lock（新規生成）
        backend.token（新規生成）/ registry.db（誤配置防止）
        memory.db（legacy）/ *.db?mode=ro（破損 legacy）
7. disk 容量チェック（target filesystem の空き >= copy 合計サイズ + 10% buffer）
   └─ 不足時は exit 6（必要容量と現状空きを表示）
8. plan 表示（copy 対象 file 一覧 + 合計サイズ + target uid + port + mode）
9. dry-run → ここで exit 0
10. TTY confirm prompt（--yes で迂回、non-TTY は step 1 で弾かれる）
11. **transactional execute（max 3 回 retry、unit = allocate → copy → manifest → registry INSERT）**:
    a. target multiverse_root 初期化（mkdir -p / chmod 0700、初回のみ）
    b. registry 初期化（MultiverseRegistry.initialize()、初回のみ）
    c. **retry loop 開始（max 3）**:
       i.   port 割当（registry.allocate_port(range_start, range_end)）
       ii.  target dir 作成（mkdir <root>/universes/<uid>）
       iii. copy / move 実行（try/except で囲む、例外時は target file を cleanup して retry 放棄 → exit 7）
       iv.  **post-copy validation**: target の gaottt.db で `PRAGMA integrity_check` を実行
            - "ok" 以外 → cleanup して exit 8（corruption detected）
            - この検証は --force 使用時に必須（writer がいた可能性を考慮）
       v.   target で manifest を managed=True で生成（UniverseManifest.write_manifest）
       vi.  registry.create_universe(uid, owner_label, port, embedder_id, embedder_version)
            - IntegrityError（partial UNIQUE INDEX `idx_universes_port_live` 違反等）→
              target dir を cleanup（manifest 含む）して retry へ
            - それ以外の exception → cleanup して exit 7
       vii. 成功 → loop 脱出
    d. retry 上限到達 → cleanup 済みの上で exit 9（"could not allocate port after 3 attempts"）
12. 結果出力: universe_id / api_key（一度だけ）/ port / target_dir / **次の手順（supervisor 起動 or `/route` 実行 / `.mcp.json` 設定）**
13. （--move の場合）source には backup 系ファイル（.bak / .before-* / manifest.json / owner.lock）が残る旨を WARNING 表示
14. exit 0
```

### 2.4 importer.py の純粋関数 API

```python
# gaottt/multiverse/importer.py

COPY_FILENAMES: tuple[str, ...] = (
    "gaottt.db", "gaottt.db-shm", "gaottt.db-wal",
    "gaottt.faiss", "gaottt.faiss.ids",
    "gaottt.virtual.faiss", "gaottt.virtual.faiss.ids",
)

@dataclass
class FilePlan:
    copy_files: list[str]   # source に存在する copy/move 対象（COPY_FILENAMES の部分集合）
    skipped: list[tuple[str, str]]  # (filename, reason) — 除外されたファイル（backup / manifest / owner.lock 等）
    total_bytes: long triple of int  # copy_files の合計サイズ

def compute_file_plan(source: Path) -> FilePlan:
    """source ディレクトリを scan して copy 対象を決定。純粋関数。

    COPY_FILENAMES の内 source に存在するものを copy_files に。それ以外は全て
    skipped に分類（理由付き）。total_bytes は copy_files の stat.st_size 合計。
    """

def resolve_embedder_identity(
    source_manifest: UniverseManifest | None,
    config: GaOTTTConfig,
    override_id: str | None,
    override_version: str | None,
) -> tuple[str, str, int]:
    """embedder_id / version / dim を決定。優先度: override > source manifest > config。

    返り値の dim は **config.embedding_dim を runtime expectation として返す**（source FAISS
    の実際の次元数の証明ではない）。真の検証は import 後の engine.startup() が行う
    verify_embedder_identity + FAISS index size との照合。importer はここで「config が
    期待する dim」を manifest に記録するだけ。"""

def validate_universe_id(uid: str) -> bool:
    """12 文字 lowercase hex なら True。supervisor の uuid4().hex[:12] と同じ形式。"""

@dataclass
class ImportPlan:
    universe_id: str
    source: Path
    target: Path
    owner_label: str
    port: int | None  # build 時は未決定、execute 時に registry.allocate_port で確定
    embedder_id: str
    embedder_version: str
    embedding_dim: int
    file_plan: FilePlan
    mode: str  # "copy" | "move"

def build_import_plan(
    source: Path,
    owner_label: str,
    config: GaOTTTConfig,
    *,
    universe_id: str | None = None,
    move: bool = False,
    embedder_id_override: str | None = None,
    embedder_version_override: str | None = None,
) -> ImportPlan:
    """dry-run 可能な plan を構築。副作用なし（registry への port 割当は execute 側）。"""

def _verify_target_db(target_db: Path) -> None:
    """target の gaottt.db で PRAGMA integrity_check を実行。
    "ok" 以外なら RuntimeError（caller が cleanup して exit 8）。
    別プロセスが書いていないかの事後検証にも使う（--force 時に必須）。"""

async def execute_import(plan: ImportPlan, registry: MultiverseRegistry) -> str:
    """plan を実行し、API key 平文を返す。

    **Transaction scope (B2'/B3' 対応)**: max 3 回 retry で次のシーケンスを wrap:
      (a) allocate_port
      (b) target dir 作成 + copy/move
      (c) post-copy PRAGMA integrity_check
      (d) manifest write (managed=True)
      (e) registry.create_universe

    例外時の cleanup:
      - IntegrityError（partial UNIQUE INDEX 違反）→ target dir を cleanup して retry
      - それ以外の exception → target dir を cleanup して raise（caller が exit 7）
      - retry 上限到達 → cleanup 済みの上で RuntimeError（caller が exit 9）

    これにより「target dir が未登録のまま残り、reconcile が WARNING する」状態を防ぐ。
    """
```

### 2.5 設計判断（根拠付き）

| 判断 | 採用 | 理由 |
|---|---|---|
| copy vs move の default | **copy** | 元を残す = 移行失敗時のロールバック容易。disk 2 倍消費は許容（本番 copy 対象 7 file で ~1.18GB、copy で ~2.4GB 必要） |
| dry-run の default | **False**（明示 `--dry-run`） | `reset_masses.py` と逆だが、TTY confirm prompt + non-TTY `--yes` 必須で安全側を担保。理由は上記（§2.2 備考） |
| registry method の新設 | **新設しない**（既存 `create_universe` を使う） | registry 側は universe_id を受け取る形なので、uid 指定も新規発行も同じ API。新規メソッドは重複 |
| supervisor 起動要件 | **不要**（importer 単体で動く） | supervisor は spawn 管理のみ。importer は直接 registry.db を触る。supervisor 起動中でも WAL + busy_timeout + partial UNIQUE INDEX で並行安全 |
| control plane 直接登録 | **しない** | supervisor 次回起動時の `reconcile` + `control_client.pull` で自動反映（J5「local が一次」）。直接 POST すると二重登録リスク |
| WAL checkpoint の実施 | **しない** | 他プロセス停止チェックで clean state を担保（WAL size check で担保漏れを検出）。checkpoint は他プロセスが生きてると失敗する |
| `--force` の安全側 | **source backend 停止確認のみ迂回**（registry 操作は迂回しない） | 複数行 WARNING を stderr、リスク説明を明示 |
| embedder endpoint 設定時 + service unreachable | **reject（exit 4）** | import しても backend が起動しない。standalone → standalone（endpoint 未設定）のみ WARNING で許容 |
| WAL hard reject 閾値 | **256MB** | SQLite WAL は通常数十 MB 以下。256MB 超は明らかに異常（他プロセスが生きている等） |

### 2.6 MCP/REST parity 鉄則との関係

本機能は **importer script 単体**（管理面ツール）。MCP ツール追加 0、REST endpoint 追加 0。これは `/reset`（REST 専用、`reset_masses.py`）と同じ例外クラス（破壊的管理操作を LLM に露出しない）。`Architecture-Overview.md` の設計判断表に「importer は MCP/REST parity 対象外（管理面）」を追記する。

## 3. assumption ledger（v3 改訂版）

| ID | assumption | basis | falsification condition | blast radius if wrong |
|---|---|---|---|---|
| A1 | importer と supervisor は並行実行時に一時的な port 衝突を起こし得るが、最終的には整合する | supervisor と importer が同時に `allocate_port` を呼ぶ → 同じ port を選ぶ可能性。partial UNIQUE INDEX `idx_universes_port_live` が INSERT 時に一方を IntegrityError で拒否。importer は max 3 回 retry で別 port を選び直す。supervisor 側（既存）は retry 無し → supervisor 側が失敗した場合は operator が再試行 | retry 上限到達（exit 9）。supervisor 側の INSERT 失敗は現状未 retry（既存の挙動、本 plan の scope 外） | 中。importer 側は安全。supervisor 側の失敗率は通常極低（数ミリ秒窓） |
| A2 | 既存 standalone DB の embedder identity は `cl-nagoya/ruri-v3-310m` / dim=768 と**推定**される | config default。**本番 `~/.local/share/gaottt/manifest.json` は存在しない**（実測、`ls` で確認）。importer は config fallback で embedder_id を決定。**ただし semantic drift（過去に別 model で embed された vector が残っている等）は import 時に falsify 不可**。/info は現 embedder service と manifest identity の一致を証明するのみで、過去の vector 生成 model までは保証しない | source に manifest.json が存在し、かつ別 model を示す（→ 検出可能）。または、config と異なる dim の vector が DB に含まれる（→ post-copy の engine.startup が FAISS load 時に RuntimeError で検出） | 中（semantic drift の場合は recall 精度低下）。軽減: post-copy に engine.startup を走らせて recall の round-trip を確認（acceptance #7） |
| A3 | writers 停止後の file copy で SQLite の実用上の整合性は保たれる | writers 停止後の `(db + db-wal + db-shm)` copy は SQLite 公式 backup API と等価**ではない**が、実用的な FS migration 手法。`-shm` は SQLite が起動時に再構築可能、`-wal` は writers 停止後に checkpoint 済みなら整合性保持。**主保護は post-copy の `PRAGMA integrity_check`**（acceptance #18） | (1) `-wal` に unflushed 変更がある（他プロセスが SIGKILL された等）→ post-copy integrity_check が "ok" でなくなり exit 8。(2) `-wal` が巨大（> 256MB）→ step 5 で hard reject。(3) 別 process が copy 中に source に書く → `--force` 使用時のみ発生、post-copy integrity_check で検出 | 高（corruption）。**全て post-copy integrity_check で検出可能**。--force 時は特に重要 |
| A4 | source の `.bak` / `.before-*` 等の backup 系ファイルは import 不要 | **実測**: `ls ~/.local/share/gaottt/` で `.bak` / `.before-chat-reingest` / `.before-claudecode-purge` / `.before-cpu-experiment` / `.before-vram-experiment` / `.before-faiss-rebuild` / `.post-shutdown` / `.broken-*` / `memory.db` / `gaottt.db?mode=ro` の存在を確認。全て backup / 一時 / legacy 目的 | （運用上の仮定）これらが現役ファイルになることはない | 低。allowlist 方式（COPY_FILENAMES のみ copy）で保安 |
| A5 | owner.lock を copy 対象から除外すれば PID 衝突しない | lease は PID + hostname + nonce（owner_id）で識別（`gaottt/store/lease.py`）、target では別物が新規生成 | copy してしまうと alive PID と衝突 | 中。除外で完全防止 |
| A6 | port range 7890-7989 に空きがある | **実測**: `ls ~/.local/share/gaottt-multiverse/` で No such file or directory（未存在 = 宇宙ゼロ）。`registry.allocate_port` は初期状態で 7890 を返す | 既に 100 宇宙がある（非現実的） | 低（明確なエラーで即 exit） |
| A7 | user は `--multiverse-root` を明示せず config から読むことを許容する | config の `multiverse_root` を使う設計、CLI で上書き可。**実測**: 現在の本番 config（`~/.config/gaottt/config.toml`、env）に `multiverse_root` 設定が無いので、初回実行時は user が必ず `--multiverse-root` を明示する必要がある | user が config を設定していない | 低（importer が fail して案内）。docs で強調 |

## 4. テスト戦略（改訂版）

### 4.1 unit test (`tests/unit/test_importer.py`)

- `compute_file_plan()`:
  - 対象 7 file 全て存在 → copy_files に 7 file / total_bytes 正確 / skipped 空
  - `.bak` / `.before-*` / `.post-*` / `.broken-*` / `.tmp` / `manifest.json` / `owner.lock` / `backend.token` / `registry.db` / `memory.db` / `*.db?mode=ro` 存在 → skipped に理由付きで分類
  - 空ディレクトリ / 存在しないディレクトリ
  - source の manifest.json が他 model を示していても plan 構築は影響を受けない（execute 時に検証）
- `resolve_embedder_identity()`:
  - override > source manifest > config の優先度
  - source manifest 無し → config fallback
  - 返り値の dim は常に config.embedding_dim
- `validate_universe_id()`:
  - 12 文字 lowercase hex → True
  - 11 文字 / 13 文字 / 大文字含む / 非 hex（`g` 含む）→ False
- `build_import_plan()`:
  - uuid4 発行（指定無し）
  - 指定 uid 重複（target dir exists）→ raise
  - mode="copy" / mode="move"
  - target dir が既存 → raise

### 4.2 integration test (`tests/integration/test_import_universe.py`)

Fixture: StubEmbedder で source data_dir を populate（`remember("hello")` × 数件、FAISS write + flush）。

**基本ラウンドトリップ**:
- source を import → target で engine を直接開いて `recall("hello")` が元のノードを返す
- copy 後のファイル構成: target に `gaottt.db` / `*.faiss` / `manifest.json`（`managed=True`）/ `owner.lock` 無し / `backend.token` 無し / `.bak` 系無し
- registry 反映: `registry.list_universes()` に含まれる / API key で `verify_api_key()` が通る
- **node count / FAISS size 整合性**: source と target で active node 数が一致、FAISS size が一致

**copy / move**:
- `--move`: source の 7 file が無くなる、target に移動されている。**backup 系ファイル（.bak 等）は source に残る**
- `--dry-run`: 何も変更しない（source も target も不変、registry に INSERT されない、target dir が作成されない）

**エラーハンドリング**:
- uid 重複: 既存 uid で `build_import_plan` を呼ぶと raise
- `--force` なしの起動中プロセス検知: mock `_running_gaottt_pids` で PID を返す → exit 3
- **uid format validation**: `--universe-id abc` / `--universe-id gggggggggggg` が exit 2
- **copy 中の例外で target が cleanup**: `monkeypatch.setattr(shutil, "copy2", ...)` で 3 file 目で `OSError` を raise → target dir が cleanup される（registry INSERT も未実行）
- **disk full**: `monkeypatch` で `shutil.copy2` に `OSError(ENOSPC)` → user-friendly error message
- **cross-filesystem move**: mock で `os.rename` → `OSError(EXDEV)` → copy+unlink fallback が走る
- **embedder service down 時の reject**: endpoint 設定 + service unreachable → exit 4
- **embedder identity mismatch**: source manifest の embedder_id と `/info` が違う → exit 4
- **WAL > 256MB で hard reject**: mock で WAL size を偽装 → exit 5
- **WAL > 64MB で WARNING**: `--yes` で続行 / 無しで exit 5
- **disk 容量不足**: mock で `shutil.disk_usage` → exit 6 + 必要容量表示
- **non-TTY + `--yes` 無し**: stdin を mock で non-TTY → exit 2
- **★ post-copy PRAGMA integrity_check**: 故意に壊した db file で `integrity_check` が "ok" 以外 → exit 8 + target cleanup
- **★ create_universe IntegrityError retry**: mock で `registry.create_universe` が 1 回目 IntegrityError、2 回目成功 → retry で救済、stray target 無し
- **★ create_universe の retry 上限**: mock で常に IntegrityError → max 3 回で cleanup + exit 9
- **★ copy 成功後の create_universe 失敗で cleanup**: copy 完了 → manifest 書込み → create_universe で generic exception → target dir が cleanup される（reconcile が WARNING する未登録 dir を残さない）
- **★ importer/supervisor 同時 create race**: 別プロセスで supervisor が同じ port を先取り → importer 側は IntegrityError → retry で別 port → 成功（実プロセスではなく `MultiverseRegistry` を直接叩く形で模擬）
- **★ source の active owner.lock**: 別プロセスが source を所有 → `--force` 無しで exit 3 / `--force` で WARNING 付き続行 + post-copy integrity_check 必須
- **★ source の stale owner.lock**: lease heartbeat が古い（> lease_stale_seconds）→ WARNING のみ（takeover 可能状態）
- **★ corrupted FAISS**: source の gaottt.faiss を破損 → target で engine.startup 時に検出される（importer 自体は copy するだけ、検証は engine 側）
- **★ FAISS .ids と .faiss の size 不整合**: engine.startup の既存診断（Tier B severe undersize）が検出する。importer はそのまま copy、検知は engine 側
- **★ empty source DB**: gaottt.db が空（schema 無し）→ integrity_check か engine.startup が検出
- **★ missing -wal / -shm**: 正常（checkpoint 済み）として扱い copy 無しで進行

**dry-run 厳密検証**: dry-run 後に `tmp_path` 配下に directory / file / registry row が 1 つも作られないことを assert

### 4.3 supervisor spawn e2e test (`@slow` marker) ★ B1 対応

`tests/integration/test_import_universe.py::test_imported_universe_supervisor_spawn` — `tests/integration/test_supervisor.py::test_mutual_isolation` の infrastructure（`StubServiceEmbedder` + `make_supervisor` + `route_universe` + `mcp_call`）を再利用:

1. StubEmbedder で source data_dir を populate（`remember("hello-world")` など）
2. importer で multiverse に import（`StubServiceEmbedder` と同じ embedder_id を manifest に記録）
3. supervisor を起動（`make_supervisor` helper）
4. `POST /route {api_key}` → supervisor が imported universe の backend を spawn
5. spawn した backend で MCP `recall("hello-world")` → 元のノードが返る
6. backend の `verify_embedder_identity` が manifest と一致して通ることを間接検証

これが **「本番 760MB DB で初めて spawn 経路が試される」事態を防ぐ最重要 test**。

### 4.4 acceptance

- `.venv/bin/python -m pytest tests/ -q` 全緑
- `ruff check gaottt/ tests/`（pre-existing 4 件のみ）
- `scripts/rest_smoke.py && scripts/mcp_smoke.py` green（importer は触らないが回帰確認）
- importer script を dry-run で叩き、plan が表示されることを目視確認

## 5. work package plan

3 WP に分ける。serial 実行（WP-1 → WP-2 → WP-3）。

### WP-1: test-first（unit + integration）

- `tests/unit/test_importer.py` — 純粋関数
- `tests/integration/test_import_universe.py` — round-trip + supervisor spawn e2e（`@slow`）
- 実装前なので import error で fail することを確認（test-first の本体）
- allowed paths: 上記 2 ファイルのみ
- forbidden: `gaottt/multiverse/importer.py` を作らない（WP-2 で作る）

### WP-2: core 実装

- `gaottt/multiverse/importer.py` — 純粋関数（`compute_file_plan` / `resolve_embedder_identity` / `validate_universe_id` / `build_import_plan` / `execute_import`）+ `COPY_FILENAMES` 定数
- `scripts/import_universe.py` — CLI entry point（`reset_masses.py` の `_running_gaottt_pids` / `_backend_port_reachable` を参考に抽出）
- WP-1 のテストが全て green になること
- allowed paths: 上記 2 ファイル + `tests/` 既存ファイルの修正（fixture 共有のため、最小限）
- forbidden: `gaottt/multiverse/registry.py` / `gaottt/store/manifest.py` / `gaottt/core/*` の編集

### WP-3: docs/handoff

- `docs/wiki/Operations-Multiverse-Import-Universe.md` — 運用 runbook（importer の使い方 + 手動手順のフォールバック + トラブルシューティング + copy 対象/除外の明示的表 + `--move` 時の旧 `.mcp.json` 更新手順）
- `docs/wiki/Operations-Multiverse-Setup.md` — 「既存宇宙の import」節へのリンクを追加
- `docs/wiki/Operations-Troubleshooting.md` — importer 関連の項を追加（`database is locked` / backend 起動失敗 / WAL size 大 / recall 結果変化の正当性）
- `docs/wiki/_Sidebar.md` + `Home.md` — 新ページ追加に伴う更新
- `docs/wiki/Architecture-Overview.md` — 設計判断表に importer 追加
- `docs/maintainers/multiverse-implementation-plan.md` — MV3 follow-on として importer 完了マークを追記
- `docs/maintainers/handover-2026-07-04-multiverse-importer.md` — handover note（日本語）
- **Tuning は更新しない**（新 config knob 無し。importer の挙動は既存 knob に依存するのみ）

## 6. acceptance criteria（done の定義）

1. `compute_file_plan` が対象 7 file を正しく抽出し、backup 系を除外する
2. `resolve_embedder_identity` が override > source manifest > config の優先度で決定する
3. `validate_universe_id` が 12 文字 lowercase hex のみを受け入れる
4. `build_import_plan` が dry-run 可能で、uid 重複を reject する
5. `execute_import` が copy または move を実行し、target に `managed=True` の manifest を生成する
6. registry に INSERT され、API key 平文が一度だけ返る
7. import 後の target で engine が開け、`recall` が元のノードを返す
8. **imported universe で supervisor が backend を spawn し、API key 認証経由で MCP recall が元のノードを返す**（QA B1 対応、最重要）
9. `--dry-run` が副作用なしで plan を表示する
10. `--force` なしで起動中プロセスを検知して exit 3 する
11. embedder endpoint 設定時 + service unreachable で exit 4 する
12. embedder identity mismatch を exit 4 で reject する
13. copy 中の例外で target file が cleanup される
14. WAL > 256MB で exit 5、WAL > 64MB で WARNING（`--yes` で続行可）
15. disk 容量不足で exit 6 + 必要容量表示
16. non-TTY + `--yes` 無しで exit 2
17. 既存 suite 全緑 + smoke green（default 不変）
18. **★ post-copy `PRAGMA integrity_check` が "ok" を返す（Codex B1' 対応）**
19. **★ copy 成功後の `create_universe` 失敗で target dir が cleanup される（Codex B2' 対応）**
20. **★ importer/supervisor port race で IntegrityError → retry で別 port で成功、stray target 無し（Codex B3' 対応）**
21. **★ retry 上限（3 回）到達で cleanup 済みの上で exit 9**

## 7. リスクと対策

| リスク | 対策 |
|---|---|
| copy 中の crash で中途半端な状態になる | `execute_import` の retry unit 内で try/except で囲み、例外時に target dir を cleanup（registry INSERT 前なので安全、acceptance #19） |
| 本番 DB の disk 容量不足（copy 対象 7 file で ~1.18GB、copy mode で ~2.4GB 必要） | dry-run で事前確認 + `execute_import` 冒頭で disk 容量チェック（不足で exit 6） |
| embedder service が起動していない | endpoint 設定時 + unreachable → exit 4（hard reject）。endpoint 未設定時 = WARNING のみ（standalone → standalone 許容） |
| 他プロセスが source に write している | `_running_gaottt_pids` + backend port 7878 probe で検知 + exit 3。`--force` で迂回可（複数行 WARNING + post-copy integrity_check 必須） |
| target multiverse_root の権限 | `chmod 0700` を実行（supervisor の lifespan と同じ） |
| control plane が後から同期される時の矛盾 | importer は local のみ触る。supervisor 次回起動時の `reconcile` は **local registry ↔ local dirs 整合**のみ（未登録 dir は採用しない）。control plane への反映は supervisor の `control_client.pull`（J5 設計通り） |
| WAL size 異常（unflushed 変更） | 64MB 超で WARNING、256MB 超で hard reject（exit 5） |
| importer + supervisor allocate_port race | partial UNIQUE INDEX `idx_universes_port_live` で最終救済、importer 側は max 3 回 retry（acceptance #20）。supervisor 側は retry 無し（既存挙動、衝突確率は極低） |
| `--move` で backup 系が source に残る → user が `rm -rf` で失う | WARNING で明示、runbook に退避手順を記載 |
| `--move` 後の rollback（target → source へ逆方向 copy） | runbook に手順記載、copy mode を推奨（default） |
| copy 後の SQLite corruption（`--force` 使用時や SIGKILL 残滓） | post-copy `PRAGMA integrity_check`（acceptance #18）。失敗時は cleanup + exit 8 |
| copy loop の例外で registry まで書かれずに target dir が残る | retry unit 内で例外時 cleanup（acceptance #19） |
| stale source manifest（過去 model swap の残滓） | `/info` で現 service と一致を確認。semantic drift は falsify 不可なので post-copy の engine.startup + recall round-trip（acceptance #7）で実質検証 |

## 8. ロールバック（v3 改訂版）

### 8.1 copy mode の場合（推奨、default）

source は無傷。**2 step** で完全 rollback:

1. **registry row を `status=deleted` へ**（`registry.delete_universe(uid)`）
2. **target dir を `trash/` へ移動または削除**

**重要**: 上記 2 step を実行する前に **supervisor の `.spawn.lock` を尊重**すること（Codex B4' 対応）。

#### supervisor 起動中の場合（推奨経路）

`DELETE /admin/universes/{uid}` を使う。supervisor が `.spawn.lock` + 2-layer lock で安全に backend を停止し、target dir を `trash/` へ移動、registry row を `status=deleted` にする（`supervisor.py:741-801` の実装）。

```bash
curl -X DELETE http://127.0.0.1:7880/admin/universes/<uid> \
    -H "X-Admin-Key: $GAOTTT_SUPERVISOR_ADMIN_KEY"
```

#### supervisor 停止中の場合（直接 registry を触る）

**前提条件**: supervisor が停止していることを必ず確認（`ps -ef | grep gaottt.multiverse.supervisor`）。もし supervisor が生きてると `/route` が mid-spawn で race する。

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

**注意**: target dir を消すだけだと registry row が `status=active` で残り、次回 supervisor 起動の `reconcile` で `orphan` 扱いになる（`registry.py:328-337`）。**必ず registry row の delete と target dir 移動をセットで行う**。

### 8.2 `--move` の場合

source は空（backup 系以外）。rollback には **逆方向 copy**（target → source）が必要:

1. 上記 8.1 と同じ手順で target を削除する **前に**
2. target から source へ 7 file を copy（`shutil.copy2`）
3. その後 registry row delete + target dir 移動

runbook に具体手順を記載する。**`--move` は非推奨、copy を強く推奨**。

## 9. 検証コマンド早見表

```bash
# unit + integration（@slow 以外）
.venv/bin/python -m pytest tests/unit/test_importer.py tests/integration/test_import_universe.py -v

# @slow も含む（supervisor spawn e2e、real subprocess）
.venv/bin/python -m pytest tests/integration/test_import_universe.py -v -m slow

# 全 suite
.venv/bin/python -m pytest tests/ -q

# lint
ruff check gaottt/ tests/

# smoke（回帰確認）
.venv/bin/python scripts/rest_smoke.py
.venv/bin/python scripts/mcp_smoke.py

# importer dry-run（隔離環境）
GAOTTT_MULTIVERSE_ROOT=/tmp/gaottt-import-test \
.venv/bin/python scripts/import_universe.py \
    --source /tmp/gaottt-source-fixture \
    --owner-label "test" \
    --dry-run --yes
```

## 10. 次のステップ（実行後）

- Codex CLI plan review（本 v2 に対して）→ 反映
- WP-1 → WP-2 → WP-3 serial で delegation
- 各 WP 検証後に次へ
- 最終 gates: PM diff inspection → Codex final → QA final
- GaOTTT writeback（設計判断 / 教訓 / self-postmortem）
- **本番適用は別セッション**: importer 検証完了後、メンテ窓として本番 DB を停止 → copy import → 接続確認 → 旧ディレクトリ保持（念のため）という手順で実施

## 11. plan review 反映ログ

### v1 → v2: QA plan review 反映

| QA 指摘 | 対応 | 反映箇所 |
|---|---|---|
| B1: supervisor spawn 経路 test 無し | acceptance #8 追加 + §4.3 supervisor spawn e2e test 追加 | §4.3, §6 #8 |
| B2: embedder endpoint + service down 挙動矛盾 | hard reject（exit 4）に統一 | §2.3 step 4, §2.5, §7 |
| N1: 容量見積もり修正 | ~1.18GB（copy 対象 7 file）, copy mode で ~2.4GB | §2.5, §7 |
| N2: `--yes` flag 追加 | §2.2 table に追加 | §2.2 |
| N3: `reset_masses.py` との逆転理由 | §2.2 備考に明記 | §2.2 |
| N4: uid format validation | §2.2 + `validate_universe_id()` 追加 | §2.2, §2.4, §4.1 |
| N5: `--move` 時の source 残存物 | §2.3 step 13 で WARNING 表示 | §2.3 |
| N6: copy 中 crash の自動 cleanup | §2.3 step 11 + §7 + §4.2 で test | §2.3, §4.2, §7 |
| N7: WAL threshold | 64MB WARNING / 256MB hard reject | §2.3 step 5, §2.5, §7 |
| M1-M8: missing tests | §4.2 に全て追加 | §4.2 |
| A1-A7: falsification 精密化 | §3 全体を改訂 | §3 |
| D1: Troubleshooting 追加 | WP-3 に追加 | §5 WP-3 |
| D2: Tuning 判断基準 | WP-3 で「更新しない」明記 | §5 WP-3 |
| D3: `--move` 時の旧設定ファイル更新 | runbook に記載（WP-3） | §5 WP-3 |
| D4: copy 対象 / 除外の明示的表 | runbook に記載（WP-3） | §5 WP-3 |
| rollback 不備（registry row 残存） | §8 を完全改訂、2 step 必須 + 具体手順 | §8 |

### v2 → v3: Codex plan review 反映

| Codex 指摘 | 対応 | 反映箇所 |
|---|---|---|
| B1': 「3 file copy = SQLite 公式 backup 等価」は誤り | A3 を修正（実用 FS migration と言い換え）、post-copy `PRAGMA integrity_check` 追加 | §3 A3, §2.3 step 11.c.iv, §6 #18 |
| B2': create_universe 失敗で target dir 未登録残り | retry unit `(allocate → copy → manifest → create_universe)` で例外時 cleanup | §2.3 step 11, §2.4 execute_import, §6 #19 |
| B3': port race の retry 境界が未定義 | max 3 回 retry で `(allocate → copy → manifest → create_universe)` を wrap、IntegrityError で cleanup + retry | §2.3 step 11, §2.4, §6 #20, #21 |
| B4': rollback が `.spawn.lock` を bypass | §8 を改訂、supervisor 起動中は `DELETE /admin/universes/{uid}` 必須、停止中の直接 path に注意書き | §8 |
| N1': `-wal` / `-shm` 無き時の正常扱い | §2.3 step 6 に明記 | §2.3 step 6 |
| N2': resolve_embedder_identity の docstring 修正 | config.embedding_dim は runtime expectation、真の検証は engine 側 | §2.4 docstring, §2.3 step 3 |
| N3': 古い source manifest の semantic drift | A2 を修正（falsify 不可を明記）、post-copy engine.startup で実質検証 | §3 A2, §7 |
| N4': assumption ledger の falsifiability 強化 | A1-A4 を実測ベースに書き直し、explicit check 化 | §3 |
| M1'-M6': missing tests 追加 | §4.2 に integrity_check / retry / race / owner.lock / corrupted FAISS / empty DB / missing wal-shm 追加 | §4.2 |
| Incorrect claim: 「reconcile で control plane 反映」 | §7 で「reconcile は local registry ↔ local dirs のみ、control plane 反映は control_client.pull」と修正 | §7 |
| Incorrect claim: 「WAL + busy_timeout + UNIQUE INDEX で並行安全」 | A1 を「一時的な port 衝突を起こし得るが最終的に整合」に修正 | §3 A1 |
