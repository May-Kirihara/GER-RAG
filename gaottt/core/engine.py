from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import pickle
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from gaottt.config import GaOTTTConfig
from gaottt.core.clustering import Cluster, cluster_by_similarity, find_merge_candidates
from gaottt.core.collision import MergeOutcome, merge_pair, pick_survivor
from gaottt.core.diversity import (
    apply_relevance_floor,
    cluster_key_from_cache,
    mmr_select,
    normalize_relevance,
)
from gaottt.core.gravity import (
    SEED_PARENT_ID,
    compute_gravity_kick,
    compute_virtual_position,
    evaporate_mass,
    is_self_force_by_id,
    propagate_gravity_wave,
    update_orbital_state,
)
from gaottt.core.persona_gravity import (
    collect_active_persona_ids,
    compute_persona_proximities,
)
from gaottt.core.prefetch import PrefetchCache, PrefetchPool
from gaottt.core.segmentation import segment_query
from gaottt.core.scorer import (
    compute_certainty_boost,
    compute_decay,
    compute_emotion_boost,
    compute_lexical_strength,
    compute_mass_boost,
    compute_semantic_factor,
    is_direct_qualified,
    qualification_confidence,
)
from gaottt.core.types import (
    CooccurrenceEdge,
    DirectedEdge,
    NodeState,
    QueryResultItem,
    ScoreBreakdown,
)
from gaottt.embedding.base import EmbedderProtocol
from gaottt.graph.cooccurrence import CooccurrenceGraph
from gaottt.index.bm25_index import BM25Index
from gaottt.index.faiss_index import FaissIndex
from gaottt.store.cache import CacheLayer
from gaottt.store.lease import LeaseLostError, OwnerLease
from gaottt.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)

# WP-6c (Phase U / R5) — background BM25 build の fill を thread 投入する
# chunk 幅。tokenization は CPU-bound なので asyncio.to_thread で chunk ごとに
# 投入し、chunk 境界で cancellation が届くようにする (1 chunk = 数百 doc で
# cancel 待ちが数秒に収まる)。sync 経路 (rollback flag) は従来どおり
# 一括で event loop 内で add する。
_BM25_BG_FILL_CHUNK = 512

# WP-6d (Phase U / R5) — BM25 snapshot 永続化の file format 定数。
# layout: 65 byte header (sha256(body) の hex 64 字 + b"\n") + pickle body。
# checksum を header に置くことで、body を unpickle する **より前に**
# 破損・改竄を検出できる。format_version は module-level 定数として
# guard する (不一致は「ファイルが無い」と同じ扱い = build に fallback)。
_BM25_SNAPSHOT_FILENAME = "bm25.snapshot"
_BM25_SNAPSHOT_FORMAT_VERSION = 1
_BM25_SNAPSHOT_HEADER_LEN = 65


def _bm25_index_state(index: BM25Index) -> dict:
    """WP-6d — BM25Index の内部状態を plain-data dict に抽出する。

    BM25Index は tokenizer として lambda/closure を保持するため object
    ごと pickle できない (かつ本 WP では index/bm25_index.py が scope 外)。
    抽出結果は全要素が builtin 型なので payload 丸ごと安全に pickle できる。
    copy を取ってから to_thread の write に渡す — publish 中の mutation で
    辞書が動かないようにするため。
    """
    return {
        "k1": index.k1,
        "b": index.b,
        "doc_ids": list(index._doc_ids),
        "doc_lens": list(index._doc_lens),
        "id_to_idx": dict(index._id_to_idx),
        "inverted": {t: list(p) for t, p in index._inverted.items()},
        "removed": set(index._removed),
        "active_count": index._active_count,
        "active_total_dl": index._active_total_dl,
    }


def _bm25_index_from_state(state: dict, tokenizer: str) -> BM25Index:
    """WP-6d — state dict から BM25Index を再構成する。

    tokenizer は名前から再生成する (identity 検証済みなので config の名前
    は snapshot 保存時と一致する)。内部配列をそのまま戻すので、検索結果
    (tie-break の insertion order 含む) は build 時と bit-for-bit 同一。
    """
    index = BM25Index(k1=state["k1"], b=state["b"], tokenizer=tokenizer)
    index._doc_ids = state["doc_ids"]
    index._doc_lens = state["doc_lens"]
    index._id_to_idx = state["id_to_idx"]
    index._inverted = state["inverted"]
    index._removed = state["removed"]
    index._active_count = state["active_count"]
    index._active_total_dl = state["active_total_dl"]
    return index


def _bm25_snapshot_trusted(path: Path) -> tuple[bool, str]:
    """WP-8 — snapshot file の trusted-file policy 検査 (unpickle の前提)。

    checksum は攻撃者が再計算できる (= 偶然の破損検出のみ) ので、pickle を
    unpickle してよいかの真正性は file の所有権・権限で担保する。信頼境界は
    ``data_dir`` (FAISS file と同じ domain)。全条件を満たす場合のみ
    ``(True, "")``:

    - 通常 file であること (symlink は lstat で拒否 — link 先の所有権が
      検査を迂回するのを防ぐ)
    - 所有 uid が process の euid と一致すること (他人が植えた file は
      checksum が正当でも拒否)
    - group/other write bit が無いこと (``mode & 0o022 == 0``)
    - 親 directory (data_dir) も group/world-writable でないこと

    違反時は ``(False, reason)`` — 呼び出し側は snapshot を存在しない扱い
    (通常 build への fallback) にする。決して unpickle しない。
    """
    try:
        st = os.lstat(path)
    except OSError as exc:
        return False, f"stat failed: {exc}"
    if stat.S_ISLNK(st.st_mode):
        return False, "snapshot is a symlink"
    if not stat.S_ISREG(st.st_mode):
        return False, "snapshot is not a regular file"
    if st.st_uid != os.geteuid():
        return False, (
            f"snapshot owner uid {st.st_uid} != process euid {os.geteuid()}"
        )
    if st.st_mode & 0o022:
        return False, "snapshot is group/world-writable"
    try:
        dst = os.stat(path.parent)
    except OSError as exc:
        return False, f"data_dir stat failed: {exc}"
    if dst.st_mode & 0o022:
        return False, "data_dir is group/world-writable"
    return True, ""


def _bm25_snapshot_read(path: Path) -> dict | None:
    """WP-6d — snapshot file を trust policy + checksum 検証してから
    unpickle する。

    checksum (header sha256) は **偶然の破損** の検出のみを担う — 攻撃者は
    checksum を再計算できるため、真正性は unpickle 前に
    :func:`_bm25_snapshot_trusted` の所有権・権限 policy で検査する
    (信頼境界 = data_dir)。以降は header sha256 と body 再計算が一致した
    場合のみ unpickle する。format_version 不一致・任意の読み込み例外は
    「ファイルが無い」のと同じ扱い (None) — 呼び出し側は通常の build に
    fallback する。
    """
    trusted, reason = _bm25_snapshot_trusted(Path(path))
    if not trusted:
        logger.error(
            "BM25 snapshot untrusted (%s) — treating as absent, "
            "falling back to build: %s",
            reason, path,
        )
        return None
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if len(data) <= _BM25_SNAPSHOT_HEADER_LEN:
        return None
    try:
        header = data[:_BM25_SNAPSHOT_HEADER_LEN]
        if header[64:65] != b"\n":
            return None
        expected = header[:64].decode("ascii")
        int(expected, 16)  # hex 以外は ValueError で落とす
    except (ValueError, UnicodeDecodeError):
        return None
    body = data[_BM25_SNAPSHOT_HEADER_LEN:]
    if hashlib.sha256(body).hexdigest() != expected:
        return None
    try:
        payload = pickle.loads(body)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("format_version") != _BM25_SNAPSHOT_FORMAT_VERSION:
        return None
    return payload


def _bm25_snapshot_write(path: Path, payload: dict) -> bool:
    """WP-6d — snapshot の atomic publish (tmp write → fsync → os.replace
    → directory fsync → read-back checksum 検証)。

    manifest.py の atomic-write 規約に読み戻し検証を加えたもの。失敗時は
    例外を伝播させず False を返す — snapshot 欠落は次 boot の再 build で
    自動回復するので、build 完了 / shutdown を落とさない方が重要。
    """
    path = Path(path)
    try:
        body = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
        digest = hashlib.sha256(body).hexdigest()
        blob = digest.encode("ascii") + b"\n" + body
        path.parent.mkdir(parents=True, exist_ok=True)
        # tmp file は明示 0o600 で作る — umask が 0o002 等だと default 作成
        # が 0o664 (group-writable) になり、読み込み側の trust policy が
        # 自分の書いた snapshot を拒否してしまうため (policy との一貫性)。
        tmp = path.parent / (
            f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        )
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            # os.open の mode 引数は umask で削られる (0o077 級の hostile
            # umask で owner bit が落ちると読み取れない file になる) ので、
            # open 済み fd に対して明示 enforce する (round-2 review)。
            os.fchmod(f.fileno(), 0o600)
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise
        # directory fsync — rename で入れ替わった dirent 自体は file fsync
        # の管轄外なので、crash 直後に rename が失われる filesystem を防ぐ
        # (final review non-blocking 指摘)。
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        # read-back 検証: replace 後の file の checksum が書いた内容と一致
        written = path.read_bytes()
        if (
            len(written) <= _BM25_SNAPSHOT_HEADER_LEN
            or written[64:65] != b"\n"
            or hashlib.sha256(
                written[_BM25_SNAPSHOT_HEADER_LEN:],
            ).hexdigest() != written[:64].decode("ascii", errors="replace")
        ):
            logger.error("BM25 snapshot read-back verification failed: %s", path)
            return False
        return True
    except OSError as exc:
        logger.error("BM25 snapshot write failed (%s): %s", path, exc)
        return False


@dataclass
class _BM25JournalEntry:
    """WP-6c — background build 窓内の BM25-affecting mutation の記録。

    sync 経路 (現行の mutation コード) が各 index に適用する操作と
    **まったく同じ対象・同じ順**で replay するための最小情報:
      * ``add`` — index_documents。sync 経路は hybrid + gate の両方に
        add するので ``gate_too=True``。
      * ``remove`` — archive / forget(hard) / merge / compact expiry。
        sync 経路は hybrid のみに remove する (gate index は次の
        compact rebuild まで removed doc を保持する現行挙動) ので
        ``gate_too=False``。
      * ``restore`` — restore。sync 経路は hybrid のみ。snapshot に無い
        doc への restore は postings が無いので soft-remove 復帰できず、
        content からの add に fallback する (そのため texts を持つ)。
    """

    op: str                                   # "add" | "remove" | "restore"
    ids: list[str]
    texts: list[str] | None = None            # add / restore 用
    gate_too: bool = field(default=False)     # add のみ True (sync 経路と同じ対象)


def _rrf_forced_key(
    nid: str,
    cosine_rank: dict[str, int],
    bm25_rank: dict[str, int],
    rrf_k: int,
) -> float:
    """RRF-combined rank score for forced ordering (Phase L Stage 1).

    ``cosine_rank`` / ``bm25_rank`` map node id → 1-based rank (precomputed).
    Absent ids contribute 0 for that metric.
    """
    score = 0.0
    cr = cosine_rank.get(nid)
    if cr is not None:
        score += 1.0 / (rrf_k + cr)
    br = bm25_rank.get(nid)
    if br is not None:
        score += 1.0 / (rrf_k + br)
    return score


class GaOTTTEngine:
    def __init__(
        self,
        config: GaOTTTConfig,
        embedder: EmbedderProtocol,
        faiss_index: FaissIndex,
        cache: CacheLayer,
        store: SqliteStore,
        virtual_faiss_index: FaissIndex | None = None,
        bm25_index: BM25Index | None = None,
        ambient_gate_index: BM25Index | None = None,
    ):
        self.config = config
        self.embedder = embedder
        self.faiss_index = faiss_index
        self.virtual_faiss_index = virtual_faiss_index
        # Phase L Stage 1: optional BM25 lexical index. When ``None``, the
        # engine behaves exactly as before Phase L (raw + virtual FAISS only).
        # Production should wire this up in build_engine; tests get the
        # legacy behaviour for free.
        self.bm25_index = bm25_index
        # Ambient Recall Enrichment: dedicated word-level BM25 index for the
        # relevance gate (see services.memory._bm25_gate). Separate from
        # ``bm25_index`` so the gate's tokenizer is independent of Phase L.
        self.ambient_gate_index = ambient_gate_index
        self.cache = cache
        self.store = store
        self.graph = CooccurrenceGraph(config, cache)
        self.prefetch_cache = PrefetchCache(
            max_size=config.prefetch_cache_size,
            ttl_seconds=config.prefetch_ttl_seconds,
        )
        self.prefetch_pool = PrefetchPool(
            max_concurrent=config.prefetch_max_concurrent,
        )
        # FAISS write-behind state. New vectors enter only the in-memory
        # FAISS index; without periodic save, other processes' startup()
        # would load a stale index and never see them until this process
        # called shutdown(). The background loop below saves the index on
        # a fixed cadence whenever `_faiss_dirty` is set.
        self._faiss_dirty: bool = False
        self._faiss_save_task: asyncio.Task | None = None
        self._faiss_save_stop: asyncio.Event | None = None
        # Reverse-overwrite guard. Latched True by startup diagnostics when
        # this process loaded a FAISS index severely undersized vs the SQLite
        # active-node count: the index is untrustworthy, so this process must
        # never persist it back to disk (it would clobber a good index written
        # by a healthy sibling). Stays latched for the life of the process —
        # recover by running scripts/rebuild_faiss_from_db.py with all other
        # gaottt processes stopped, then restart.
        self._faiss_persist_blocked: bool = False
        # One-shot latch so the persist-skip path logs at most once per
        # process instead of every save tick.
        self._faiss_persist_guard_warned: bool = False
        # Virtual FAISS write-behind. Same multi-process visibility
        # problem as raw FAISS but driven by cache.displacement edits
        # (Phase I/J query attraction, genesis kicks, dream loop). The
        # dirty signal is `cache.virtual_faiss_dirty`; the loop reads it,
        # rebuilds the full virtual index, saves, and clears.
        self._virtual_faiss_save_task: asyncio.Task | None = None
        self._virtual_faiss_save_stop: asyncio.Event | None = None
        # Phase G — Dream loop: revisits quiet nodes on a slow cadence with
        # synthetic recalls so co-occurrence and gravity state build up even
        # without user query (hippocampal-replay analog). Disabled when
        # dream_enabled=False or dream_interval_seconds<=0.
        self._dream_task: asyncio.Task | None = None
        self._dream_stop: asyncio.Event | None = None
        # MV2 — engine-wide persist block. Latched True when the lease
        # heartbeat detects the owner changed (another process took over).
        # Gates cache flush + FAISS save + mutating method entry (read-only
        # transition). Mirrored onto ``cache.persist_blocked`` so the
        # write-behind / virtual-FAISS loops pick it up through their
        # existing flush / safe-to-persist paths. Default OFF: when
        # ``owner_lease_enabled`` and ``manifest.managed`` are both False
        # (the standalone default) no lease is ever acquired and this latch
        # stays False forever.
        self._persist_blocked: bool = False
        self._lease: OwnerLease | None = None
        self._lease_task: asyncio.Task | None = None
        self._lease_stop: asyncio.Event | None = None
        self._lease_lost_warned: bool = False
        # WP-6a (Phase U / R5): startup() のフェーズ別所要時間 (秒、
        # monotonic perf_counter 差分)。startup() 呼び出し前にも属性参照され
        # 得る契約 (WP-6b readiness) のため空 dict として常在させる。キーは
        # startup() 内の計装コメントに列挙した安定名。
        self.startup_timings: dict[str, float] = {}
        # WP-6c (Phase U / R5) — BM25 background build 状態。WP-6b readiness
        # (SEMANTIC_READY / HYBRID_READY 区分) が参照する canonical 属性:
        #   "idle"     — build 未開始 (index 未接続 / shutdown cancel 後)
        #   "building" — background task 実行中 (または sync build 実行中)
        #   "ready"    — build 完了 (swap 済み or 同期 build 済み)
        #   "failed"   — retry まで尽して give up (index は空のまま運用継続)
        self.bm25_build_state: str = "idle"
        # background 経路の試行回数 (single-retry 契約の観測用; sync 経路は 0)
        self.bm25_build_attempts: int = 0
        self._bm25_build_task: asyncio.Task | None = None
        # non-None の間だけ mutation 経路が journal に append する
        # (= background build が in flight)。journal への append と
        # replay+swap は同じ asyncio.Lock で直列化する。
        self._bm25_journal: list[_BM25JournalEntry] | None = None
        self._bm25_journal_lock: asyncio.Lock | None = None
        # BM25-affecting mutation すべてで増える世代カウンタ (journal 有無
        # にかかわらず観測用)。build 開始/完了 log で参照する。
        self._bm25_mutation_generation: int = 0
        # compact(rebuild_faiss=True) が build 窓内で現行 index を再構築し
        # た場合に立つフラグ — background build の新 object は古い snapshot
        # 基底なので swap せず破棄する (現行 index の方が新しい)。
        self._bm25_bg_invalidated: bool = False
        # WP-6d (Phase U / R5) — BM25 snapshot (data_dir/bm25.snapshot) 状態。
        # dirty: 現行 index に mutation が適用され、on-disk snapshot が現状
        #   を映さなくなった (再保存は build 完了時か graceful shutdown 時
        #   のみ — mutation ごとには書き直さない)。
        # gate_diverged: remove 系 mutation が hybrid のみに適用された
        #   (sync 経路の契約) 結果、gate index が store-active 構成から
        #   乖離した。compact の full rebuild で再収束するまで snapshot に
        #   保存しない — 「load した index は fresh build と同一結果を返す」
        #   という不変条件のため。
        self._bm25_snapshot_dirty: bool = False
        self._bm25_snapshot_gate_diverged: bool = False
        self._bm25_snapshot_block_warned: bool = False
        # startup で manifest から設定 (snapshot の universe_id 検証用)
        self._universe_id: str = ""

    async def startup(self) -> None:
        # WP-6a (Phase U / R5) — startup フェーズ計装。各フェーズの所要時間を
        # startup_timings に記録する。純観測であり、呼び出し順・例外伝播・
        # 挙動は計装なしと完全同一 (フェーズ途中で例外が出た場合はそこまで
        # の記録のみ残り、例外はそのまま伝播する)。キーは安定名:
        # manifest / lease / store_init / ttl_scan / cache_load / faiss_load /
        # virtual_faiss_load / bm25_build / background_loops / diagnostics
        # (+ startup_total / node_count / index_size)。スキップされたフェーズ
        # (index 未接続等) もキー自体は常に存在し値 ≈0.0 となる。
        # WP-6c 例外 (documented exception): bm25_background_build_enabled=True
        # のとき bm25_build は startup 時点では同期区間 ≈0.0 を記録し、
        # background build の完了時に実際の所要時間で **上書き** される
        # (post-completion update)。失敗の canonical 状態は timing 値ではなく
        # engine.bm25_build_state == "failed" (WP-6b readiness が参照)。
        # WP-6d 例外 (追加): bm25_snapshot_enabled=True で snapshot が
        # fingerprint 一致により load された場合、bm25_build は build の
        # 代わりに「fingerprint pass + load」の所要時間を記録する。
        # WP-6c/6d 着手の decision gate と WP-6b readiness が参照する。
        self.startup_timings = {}
        t_total = time.perf_counter()
        # Phase T Stage 2 — legacy semantic decay contract selected. ``delta``
        # is a per-SECOND rate (deprecated contract): exp(-0.01*600) ≈ 0.0025,
        # so the semantic score term goes numerically extinct within minutes.
        # Kept only as the bit-for-bit rollback path. Logged once per startup.
        if not self.config.semantic_halflife_enabled:
            logger.warning(
                "semantic_halflife_enabled=False: using legacy "
                "compute_decay with config.delta=%s — delta is a per-SECOND "
                "rate (deprecated contract) that zeroes the semantic score "
                "term within minutes. See "
                "docs/wiki/Plans-Phase-T-Semantic-Requalification.md §3.",
                self.config.delta,
            )
        # MV0 — universe manifest hard gate. Runs before any store / FAISS
        # touch: ``ensure_manifest`` auto-generates from config for existing
        # DBs (backward-compat), then the manifest ``embedding_dim`` is
        # checked against ``config.embedding_dim``. The diagnostics block
        # below swallows exceptions, so this gate must sit above it. The
        # runtime embedder identity (embedder_id / version) is verified in
        # ``build_engine``; here we only guard the manifest-vs-config dim.
        t_phase = time.perf_counter()
        from pathlib import Path
        from gaottt.store.manifest import ensure_manifest

        manifest = ensure_manifest(Path(self.config.data_dir), self.config)
        if manifest.embedding_dim != self.config.embedding_dim:
            msg = (
                f"Manifest embedding_dim mismatch: manifest="
                f"{manifest.embedding_dim}, config="
                f"{self.config.embedding_dim}. Switching embedder "
                f"requires re-embedding via scripts/rebuild_faiss_from_db.py "
                f"and a manifest update. "
                f"escape: GAOTTT_MANIFEST_CHECK_ENABLED=false"
            )
            if self.config.manifest_check_enabled:
                raise RuntimeError(msg)
            logger.warning(
                "Manifest embedding_dim mismatch (check disabled, "
                "continuing): manifest=%s, config=%s",
                manifest.embedding_dim,
                self.config.embedding_dim,
            )
        self.startup_timings["manifest"] = time.perf_counter() - t_phase
        # WP-6d — snapshot の cross-universe 検証に使う (以降の phase で
        # manifest object を持ち回さなくてよい)。
        self._universe_id = manifest.universe_id
        # MV2 — owner lease. Fires when owner_lease_enabled OR manifest.managed.
        # Both False (standalone default) → skip entirely: no owner.lock, no
        # heartbeat task, no release on shutdown. This is the "default 不変"
        # gate — existing deployments that never set the flag are bit-exact.
        # LeaseHeldError from acquire() propagates unmasked (the caller's
        # startup fails loudly, signalling "another process owns this dir").
        t_phase = time.perf_counter()
        if self.config.owner_lease_enabled or manifest.managed:
            self._lease = OwnerLease(
                Path(self.config.data_dir), self.config,
            )
            self._lease.acquire(force=self.config.lease_force_takeover)
        self.startup_timings["lease"] = time.perf_counter() - t_phase
        t_phase = time.perf_counter()
        await self.store.initialize()
        self.startup_timings["store_init"] = time.perf_counter() - t_phase
        t_phase = time.perf_counter()
        expired = await self.store.expire_due_nodes(time.time())
        if expired:
            logger.info("Auto-expired %d nodes past their TTL", expired)
        self.startup_timings["ttl_scan"] = time.perf_counter() - t_phase
        t_phase = time.perf_counter()
        await self.cache.load_from_store(self.store)
        self.startup_timings["cache_load"] = time.perf_counter() - t_phase
        t_phase = time.perf_counter()
        self.faiss_index.load(self.config.faiss_index_path)
        self.startup_timings["faiss_load"] = time.perf_counter() - t_phase
        t_phase = time.perf_counter()
        if self.virtual_faiss_index is not None:
            virtual_path = self.config.virtual_faiss_index_path
            self.virtual_faiss_index.load(virtual_path)
            if self.virtual_faiss_index.size == 0 and self.faiss_index.size > 0:
                # No persisted virtual index yet — rebuild from raw + cache.
                logger.info(
                    "Virtual FAISS missing; building from raw + displacement"
                )
                await self._rebuild_virtual_faiss_index()
        self.startup_timings["virtual_faiss_load"] = (
            time.perf_counter() - t_phase
        )
        # Phase L Stage 1: build BM25 index from active document content.
        # Also builds the ambient gate index (word-level BM25) when wired.
        # WP-6c (Phase U / R5): bm25_background_build_enabled=True なら同期
        # build を skip し background task に委譲する — startup は
        # SEMANTIC_READY (~6s) で返り、build 完了時に journal replay +
        # atomic swap で HYBRID_READY に遷移する (実測では同期 build が
        # startup の 96% を占めていた)。rollback (False) は同期 build に
        # bit-for-bit 復帰。
        # WP-6d (Phase U / R5): bm25_snapshot_enabled=True なら最初に
        # snapshot の load を試みる — fingerprint (content digest) が一致
        # すれば build ごと skip して両 index を load する (cold start から
        # 147s の build を除去)。不一致・破損・cross-universe・params 変更は
        # 通常の WP-6c 経路に fallback し、build 成功時に snapshot を保存。
        t_phase = time.perf_counter()
        self.bm25_build_state = "idle"
        # snapshot 状態は boot ごとに初期化 (load/build 完了時に更新される)
        self._bm25_snapshot_dirty = False
        self._bm25_snapshot_gate_diverged = False
        if self.bm25_index is not None or self.ambient_gate_index is not None:
            self.bm25_build_state = "building"
            try:
                loaded = False
                if self.config.bm25_snapshot_enabled:
                    loaded = await self._try_load_bm25_snapshot()
                if loaded:
                    self.bm25_build_state = "ready"
                elif self.config.bm25_background_build_enabled:
                    self._start_bm25_background_build()
                else:
                    # sync build に journal replay の消費先は無い — load 試行
                    # で開いた journal を閉じてから現行 (WP-6c 以前) どおり
                    # 同期 build する。成功時 (mutation が無ければ) snapshot
                    # を保存する。
                    await self._close_bm25_journal()
                    fp = await self._build_bm25_from_store()
                    self.bm25_build_state = "ready"
                    await self._save_bm25_snapshot_if_clean(fp)
            except Exception:
                # sync 経路の例外は現行どおり伝播 (startup 失敗)。WP-6b 用に
                # state だけ失敗を記録してから re-raise。
                self.bm25_build_state = "failed"
                raise
        self.startup_timings["bm25_build"] = time.perf_counter() - t_phase
        t_phase = time.perf_counter()
        self.cache.start_write_behind(self.store)
        if self.config.faiss_save_interval_seconds > 0:
            self._faiss_save_stop = asyncio.Event()
            self._faiss_save_task = asyncio.create_task(self._faiss_save_loop())
        if (
            self.virtual_faiss_index is not None
            and self.config.virtual_faiss_save_interval_seconds > 0
        ):
            self._virtual_faiss_save_stop = asyncio.Event()
            self._virtual_faiss_save_task = asyncio.create_task(
                self._virtual_faiss_save_loop()
            )
        # The dream loop hosts both the synthetic-recall replay (dream_enabled)
        # and the Phase Q Stage 2 continuous orbital tick (orbital_tick_enabled).
        # Start it if either feature is on; each is independently gated inside.
        if (
            (self.config.dream_enabled or self.config.orbital_tick_enabled)
            and self.config.dream_interval_seconds > 0
        ):
            self._dream_stop = asyncio.Event()
            self._dream_task = asyncio.create_task(self._dream_loop())
        # MV2 — lease heartbeat: refresh owner.lock on a fixed cadence so a
        # foreign takeover (stale/force) is detected and the engine latches
        # read-only. Only started when a lease was acquired and the cadence
        # is positive.
        if self._lease is not None and self.config.lease_heartbeat_seconds > 0:
            self._lease_stop = asyncio.Event()
            self._lease_task = asyncio.create_task(self._lease_heartbeat_loop())
        self.startup_timings["background_loops"] = (
            time.perf_counter() - t_phase
        )
        logger.info(
            "Engine started: %d nodes cached, %d vectors indexed, %d displacements",
            len(self.cache.node_cache),
            self.faiss_index.size,
            len(self.cache.displacement_cache),
        )

        # Stage 1 startup self-diagnostics (commitment id=aaa6e7cc).
        # Imported lazily so test fixtures that construct engines without
        # the diagnostics module on the path don't break. Failures of
        # individual checks are captured in the report, not raised.
        t_phase = time.perf_counter()
        try:
            from gaottt.diagnostics import run_startup_checks
            await run_startup_checks(self, self.config)
        except Exception as e:
            logger.warning(
                "Startup diagnostics raised — engine remains operational: %s: %s",
                type(e).__name__, e,
            )
        self.startup_timings["diagnostics"] = time.perf_counter() - t_phase

        # Phase N candidate β Stage 1 — cold-start mass evaporation sweep.
        # If the engine was offline for longer than τ_grace, no recall path
        # has touched these nodes since shutdown, so the lazy hook never
        # fires for them. Apply ``evaporate_mass`` once to every active
        # node here so the field starts from a fully-settled state. Idempotent:
        # uses ``state.last_access`` as the only time reference, so re-running
        # this on the same shutdown→startup gap produces the same result.
        # No-op when ``mass_evaporation_enabled=False`` (per-call guard inside
        # ``evaporate_mass``), so the loop cost is only paid post-rollout.
        # WP-6a: この sweep は計装キー対象外 — コストは startup_total のみに現れる。
        if self.config.mass_evaporation_enabled:
            now_sweep = time.time()
            swept = 0
            for state in self.cache.get_all_nodes():
                if state.is_archived:
                    continue
                new_mass = evaporate_mass(
                    state.mass, state.last_access, now_sweep, self.config,
                )
                if new_mass != state.mass:
                    state.mass = new_mass
                    self.cache.set_node(state, dirty=True)
                    swept += 1
            if swept:
                logger.info(
                    "Phase N β cold-start sweep: %d nodes settled mass debt",
                    swept,
                )
        self.startup_timings["startup_total"] = time.perf_counter() - t_total
        # informational な規模値 (int) — gate 分析と WP-6b readiness 表示用。
        self.startup_timings["node_count"] = len(self.cache.node_cache)
        self.startup_timings["index_size"] = self.faiss_index.size
        logger.info(
            "Engine startup timings: %s",
            " ".join(
                f"{k}={v}" if isinstance(v, int) else f"{k}={v:.3f}s"
                for k, v in self.startup_timings.items()
            ),
        )

    async def shutdown(self) -> None:
        # WP-6c — background BM25 build を最初に止める。fill は worker
        # thread で chunk 実行されているので、cancel は次の chunk 境界
        # (または現在 chunk の完了) で届く。engine は build 完了を待たず
        # shutdown できる。swap 前に止まった場合、新 object は破棄され
        # 現行 index は空のまま (中途半端な swap は起こさない)。
        if self._bm25_build_task is not None and not self._bm25_build_task.done():
            self._bm25_build_task.cancel()
            try:
                await asyncio.wait_for(self._bm25_build_task, timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        # task handle は他の background loop (_faiss_save_task 等) と同じく
        # 残す — done 状態を観測可能にしておく (null 化しない)。
        if self._bm25_journal is not None:
            # cancel が thread 完了待ちで timeout した場合の防備: 以降の
            # mutation が journal に溜り続けないように閉じる。
            self._bm25_journal = None
        await self.prefetch_pool.drain(timeout=5.0)
        if self._dream_stop is not None:
            self._dream_stop.set()
        if self._dream_task is not None:
            try:
                await asyncio.wait_for(self._dream_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._dream_task.cancel()
        # MV2 — stop the lease heartbeat before touching FAISS: the order is
        # dream → lease heartbeat → FAISS save. Signalling stop lets the
        # loop exit cleanly; the task then awaits like the others.
        if self._lease_stop is not None:
            self._lease_stop.set()
        if self._lease_task is not None:
            try:
                await asyncio.wait_for(self._lease_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._lease_task.cancel()
        if self._faiss_save_stop is not None:
            self._faiss_save_stop.set()
        if self._faiss_save_task is not None:
            try:
                await asyncio.wait_for(self._faiss_save_task, timeout=10.0)
            except asyncio.TimeoutError:
                self._faiss_save_task.cancel()
        if self._virtual_faiss_save_stop is not None:
            self._virtual_faiss_save_stop.set()
        if self._virtual_faiss_save_task is not None:
            try:
                await asyncio.wait_for(
                    self._virtual_faiss_save_task, timeout=10.0,
                )
            except asyncio.TimeoutError:
                self._virtual_faiss_save_task.cancel()
        await self.cache.stop_write_behind()
        # MV2 — revalidate ownership AFTER write-behind is fully stopped, so
        # no background flush can race with the latch. The heartbeat loop
        # stopped above, but write-behind kept running until the line just
        # above; with it now cancelled+joined, a final read-back of
        # ``is_active`` is the single remaining place a takeover (between
        # heartbeat-stop and here) can be detected. ``is_active`` re-reads
        # owner.lock without bumping heartbeat_at, so it is safe
        # mid-shutdown. If the owner changed, ``_on_lease_lost`` latches
        # ``_persist_blocked`` (and the cache's flag), making the manual
        # final flush below and the FAISS save further down both no-ops.
        if self._lease is not None and not self._lease.is_active:
            self._on_lease_lost()
        await self.cache.flush_to_store(self.store)
        # WP-6d — dirty な BM25 snapshot の再保存。flush **後** に実行する
        # ので fingerprint pass は pending 書き込みを含む store の最終状態
        # を読む (index 内容 = built content + 適用済み mutation と一致)。
        await self._save_bm25_snapshot_on_shutdown()
        # Final synchronous save guarantees durability even if the loop
        # was disabled or skipped a final tick — but still honour the
        # reverse-overwrite guard so a broken-index process doesn't clobber a
        # good on-disk index on its way out.
        ok, reason = self._faiss_safe_to_persist()
        if ok:
            # Bound the final saves so shutdown cannot hang indefinitely. The
            # per-task awaits above use wait_for(10s); these to_thread saves
            # previously had no timeout, so a wedged save (executor saturation,
            # or — with the FaissIndex lock — a cancelled-but-still-running
            # periodic save holding the lock while this one waits for it) would
            # block shutdown forever. On timeout we log and move on: the
            # on-disk index may be stale, but the startup diagnostic rebuilds
            # from the store, so durability is not lost. (wait_for cancels the
            # await but cannot interrupt the worker thread; this unblocks
            # shutdown, not a genuinely deadlocked write_index — the deeper fix
            # there is to not hold the lock across the whole write.)
            timeout = self.config.faiss_final_save_timeout_seconds
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self.faiss_index.save, self.config.faiss_index_path,
                    ),
                    timeout=timeout,
                )
                if self.virtual_faiss_index is not None:
                    await asyncio.wait_for(
                        asyncio.to_thread(
                            self.virtual_faiss_index.save,
                            self.config.virtual_faiss_index_path,
                        ),
                        timeout=timeout,
                    )
            except asyncio.TimeoutError:
                logger.warning(
                    "Final FAISS save exceeded %.0fs during shutdown — "
                    "skipping to avoid a hang. On-disk index may be stale; "
                    "the startup diagnostic will rebuild from the store.",
                    timeout,
                )
        else:
            self._log_persist_skip(reason)
        # MV2 — release the owner lease after the final flush + final FAISS
        # save so the very last writes land before ownership is relinquished.
        # ``release()`` is owner_id-guarded: if the lease was lost (persist
        # blocked), the final flush/save were no-ops and release() leaves the
        # foreign owner's lock intact. A standalone engine (no lease) skips.
        if self._lease is not None:
            self._lease.release()
        self._faiss_dirty = False
        await self.store.close()
        logger.info("Engine shut down, state persisted")

    def _faiss_safe_to_persist(self) -> tuple[bool, str]:
        """Reverse-overwrite guard: may this process write FAISS to disk?

        Returns ``(ok, reason)``. ``ok=False`` means the in-memory index is
        untrustworthy and persisting it would risk clobbering a good on-disk
        index written by a healthy sibling process.

        Two gates:
          * ``_faiss_persist_blocked`` — a hard latch set by startup
            diagnostics when the loaded index was severely undersized.
          * dynamic ratio — ``faiss.size`` has fallen far below the SQLite
            active-node count *right now*. Inert below ``faiss_persist_floor``
            (small/reset DBs) and when the guard is disabled by config.

        Legitimate bulk shrink (forget/compact) evicts from cache too, so the
        active count falls in lockstep and ``size/active`` stays healthy — the
        guard does not misfire on intentional deletion.
        """
        if self._persist_blocked:
            return False, "persist blocked (lease lost — engine is read-only)"
        if self._faiss_persist_blocked:
            return False, "persist blocked (startup severe-undersize latch)"
        if not self.config.faiss_persist_guard_enabled:
            return True, ""
        active = sum(
            1 for s in self.cache.get_all_nodes() if not s.is_archived
        )
        if active < self.config.faiss_persist_floor:
            return True, ""
        size = self.faiss_index.size
        if size < active * self.config.faiss_persist_min_ratio:
            return (
                False,
                f"faiss.size={size} << SQLite active={active} "
                f"(ratio<{self.config.faiss_persist_min_ratio}); refusing to "
                f"overwrite a healthy on-disk index",
            )
        return True, ""

    def _log_persist_skip(self, reason: str) -> None:
        """Log a guard-triggered persist skip at most once per process."""
        if not self._faiss_persist_guard_warned:
            self._faiss_persist_guard_warned = True
            logger.error(
                "FAISS persist BLOCKED — %s. This process will not write FAISS "
                "to disk. Recover: stop all gaottt processes, run "
                "`scripts/rebuild_faiss_from_db.py --apply`, then restart.",
                reason,
            )

    async def _faiss_save_loop(self) -> None:
        """Background FAISS save: persists in-memory FAISS additions on a
        fixed cadence. Crucial for multi-process visibility.
        """
        assert self._faiss_save_stop is not None
        interval = self.config.faiss_save_interval_seconds
        path = self.config.faiss_index_path
        while not self._faiss_save_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._faiss_save_stop.wait(), timeout=interval,
                )
                break  # stop signalled
            except asyncio.TimeoutError:
                pass  # interval elapsed, try a save tick
            if self._faiss_dirty:
                ok, reason = self._faiss_safe_to_persist()
                if not ok:
                    # Keep dirty=True so a later healthy state still flushes,
                    # but never overwrite a good on-disk index from here.
                    self._log_persist_skip(reason)
                    continue
                # Claim before save so any add() during the save itself
                # leaves dirty=True for the next tick to handle.
                self._faiss_dirty = False
                try:
                    await asyncio.to_thread(self.faiss_index.save, path)
                except Exception:  # noqa: BLE001
                    self._faiss_dirty = True
                    logger.exception("Periodic FAISS save failed; will retry")

    async def _virtual_faiss_save_loop(self) -> None:
        """Background virtual FAISS rebuild + save: refreshes the
        displacement-aware seed index on a fixed cadence whenever the
        cache marks itself dirty. Without this, displacement edits from
        recall (Phase I/J query attraction), genesis kicks, and the dream
        loop never reach the seed pool of subsequent recalls — virtual
        FAISS would only refresh at compact(rebuild_faiss=True).

        Rebuild is O(N) over active nodes. The default 60s cadence keeps
        the work amortized; tune via virtual_faiss_save_interval_seconds.
        """
        assert self._virtual_faiss_save_stop is not None
        assert self.virtual_faiss_index is not None
        interval = self.config.virtual_faiss_save_interval_seconds
        path = self.config.virtual_faiss_index_path
        while not self._virtual_faiss_save_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._virtual_faiss_save_stop.wait(), timeout=interval,
                )
                break  # stop signalled
            except asyncio.TimeoutError:
                pass  # interval elapsed, try a rebuild tick
            if self.cache.virtual_faiss_dirty:
                # The virtual index is derived from the raw index; if the raw
                # index is untrustworthy (reverse-overwrite guard tripped),
                # the virtual one is too — do not persist either.
                ok, reason = self._faiss_safe_to_persist()
                if not ok:
                    self._log_persist_skip(reason)
                    continue
                # Claim before rebuild so any set_displacement during the
                # rebuild itself leaves dirty=True for the next tick.
                self.cache.virtual_faiss_dirty = False
                try:
                    await self._rebuild_virtual_faiss_index()
                    await asyncio.to_thread(
                        self.virtual_faiss_index.save, path,
                    )
                except Exception:  # noqa: BLE001
                    self.cache.virtual_faiss_dirty = True
                    logger.exception(
                        "Periodic virtual FAISS rebuild failed; will retry"
                    )

    def _pick_dream_candidates(self, limit: int) -> list[str]:
        """Quiet nodes worth revisiting in a dream tick.

        Picks non-archived nodes whose mass is still below
        ``dream_mass_ceiling`` and whose ``last_access`` is older than
        ``dream_min_idle_seconds``. Sorted by oldest-access-first so the
        coldest memories get revived earliest.
        """
        now = time.time()
        ceiling = self.config.dream_mass_ceiling
        min_idle = self.config.dream_min_idle_seconds
        quiet = [
            s for s in self.cache.get_all_nodes()
            if not s.is_archived
            and s.mass < ceiling
            and (now - s.last_access) > min_idle
        ]
        quiet.sort(key=lambda s: s.last_access)
        return [s.id for s in quiet[:limit]]

    async def _dream_loop(self) -> None:
        """Hippocampal-replay analog. While the user is silent, revisit
        quiet nodes via synthetic recall so they accumulate co-occurrence
        and gravity field updates without ever being shown to the LLM.
        """
        assert self._dream_stop is not None
        interval = self.config.dream_interval_seconds
        while not self._dream_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._dream_stop.wait(), timeout=interval,
                )
                break  # stop signalled
            except asyncio.TimeoutError:
                pass

            try:
                # Phase Q Stage 2: advance free orbital motion for the lively
                # set (no recall, no mass/temp update). Runs first so the
                # cosmos keeps moving even when synthetic replay is disabled.
                if self.config.orbital_tick_enabled:
                    self._orbital_tick()
                    await asyncio.sleep(0)

                # Hippocampal replay: synthetic recalls of quiet nodes.
                if self.config.dream_enabled:
                    candidates = self._pick_dream_candidates(
                        limit=self.config.dream_batch_size,
                    )
                    for nid in candidates:
                        if self._dream_stop.is_set():
                            break
                        doc = await self.store.get_document(nid)
                        if not doc:
                            continue
                        await self._query_internal(
                            text=doc["content"],
                            top_k=self.config.dream_top_k,
                            wave_depth=None,
                            wave_k=None,
                            _is_synthetic=True,
                        )
                        # Phase M follow-up (2026-05-13): yield to the event
                        # loop between candidates so foreground MCP / REST
                        # recalls aren't starved during a dream tick.
                        # ``_query_internal`` is dominated by numpy / FAISS work
                        # that doesn't release the GIL, so without this explicit
                        # yield a batch of N candidates runs as a single
                        # contiguous CPU burst and makes interactive recalls
                        # time out.
                        await asyncio.sleep(0)
            except Exception:  # noqa: BLE001
                # A bad tick should not kill the loop. Log and try again.
                logger.exception("Dream tick failed; will retry next cycle")

    def _orbital_tick(self) -> None:
        """Phase Q Stage 2 — one continuous orbital integration step.

        Advances the orbital state (displacement + velocity) of the *lively*
        nodes — those whose cached velocity exceeds
        ``orbital_lively_v_min`` — by reusing ``update_orbital_state`` with the
        lively set as the active body. The dominant force is each node's own
        Hooke anchor (``F = -k·d`` toward its raw embedding), which by
        Bertrand's theorem is a closed-orbit central force; mutual gravity
        among the lively set perturbs the ellipses into rosettes. The node
        orbits its *own* anchor → zero anchor migration.

        Unlike a recall, this touches **only** displacement and velocity:
        mass, temperature, last_access, and co-occurrence are left untouched
        (recall = energy injection; tick = free evolution). Age-based friction
        is suppressed for the tick — it keys on ``last_access``, which is stale
        for an orbiting-but-unrecalled node and would otherwise damp the orbit
        to zero within a few ticks; only the small constant friction applies,
        giving the slow thermodynamic decay back into the well.

        Cost is O(L²) over the lively set L (mutual gravity in
        ``update_orbital_state``). ``L`` is self-limiting because constant
        friction returns kicked nodes to "cold" ~100 ticks after their last
        recall; ``orbital_tick_max_nodes`` is a hard backstop and logs when it
        truncates so a coverage cap is never silent.
        """
        if not self.config.orbital_tick_enabled:
            return
        if self._persist_blocked:
            return  # read-only: lease lost, skip mass/displacement writes

        v_min = self.config.orbital_lively_v_min
        lively: list[tuple[str, float]] = []
        for state in self.cache.get_all_nodes():
            if state.is_archived:
                continue
            vel = self.cache.get_velocity(state.id)
            if vel is None:
                continue
            speed = float(np.linalg.norm(vel))
            if speed > v_min:
                lively.append((state.id, speed))

        if len(lively) < 2:
            # update_orbital_state needs >= 2 bodies; a lone lively node has
            # no mutual gravity to integrate against here (its anchor-only
            # motion is picked up on the next recall path). Skip cleanly.
            return

        cap = self.config.orbital_tick_max_nodes
        if len(lively) > cap:
            # Process the fastest movers first; defer the rest to later ticks.
            lively.sort(key=lambda t: t[1], reverse=True)
            logger.info(
                "orbital_tick: %d lively nodes > cap %d — integrating top %d "
                "by speed, deferring %d to the next tick",
                len(lively), cap, cap, len(lively) - cap,
            )
            lively = lively[:cap]

        ids = [nid for nid, _ in lively]
        original_embs = self.faiss_index.get_vectors(ids)
        active = [nid for nid in ids if nid in original_embs]
        if len(active) < 2:
            return

        dim = self.config.embedding_dim
        displacements: dict[str, np.ndarray] = {}
        velocities: dict[str, np.ndarray] = {}
        masses: dict[str, float] = {}
        last_accesses: dict[str, float] = {}
        now = time.time()
        for nid in active:
            d = self.cache.get_displacement(nid)
            displacements[nid] = d if d is not None else np.zeros(dim, dtype=np.float32)
            v = self.cache.get_velocity(nid)
            velocities[nid] = v if v is not None else np.zeros(dim, dtype=np.float32)
            st = self.cache.get_node(nid)
            masses[nid] = st.mass if st is not None else 1.0
            last_accesses[nid] = st.last_access if st is not None else now

        # Suppress age friction for the free-evolution tick (constant friction
        # only — see docstring). dataclasses.replace keeps every other knob,
        # including orbital_integrator (Verlet) and the mass-dependent anchor β.
        #
        # Phase Q rollout finding (2026-05-30): the lively set handed to
        # update_orbital_state is a scattered set of the fastest movers, not a
        # true local neighbourhood. Treating it as mutual N-body neighbours
        # makes neighbour gravity sum *coherently* in RURI's narrow high-cosine
        # space (measured net |a|~10–640 vs the anchor's ~0.005), dominating the
        # Hooke restoring force and slamming displacement onto the
        # max_displacement_norm clamp instead of perturbing the orbit. So by
        # default the tick zeroes gravity_G (→ neighbour gravity AND the
        # G-scaled mass-BH term vanish), leaving a pure self-anchor Hooke orbit:
        # each node orbits its own embedding x₀ — exactly Phase Q's
        # zero-anchor-migration core, measured bounded + self-limiting.
        # orbital_tick_neighbor_gravity_enabled=True restores the coupled
        # behaviour for experimentation. The recall path is unaffected.
        from dataclasses import replace
        tick_overrides = {"orbital_friction_age_factor": 0.0}
        if not self.config.orbital_tick_neighbor_gravity_enabled:
            tick_overrides["gravity_G"] = 0.0
        tick_config = replace(self.config, **tick_overrides)

        new_disps, new_vels = update_orbital_state(
            active, original_embs,
            displacements, velocities,
            masses, last_accesses, now, tick_config,
            cache=self.cache,
            query_anchor=None,   # no query-attraction kick during free evolution
            query_scores=None,
        )

        for nid in new_disps:
            self.cache.set_displacement(nid, new_disps[nid])
            self.cache.set_velocity(nid, new_vels[nid])

    async def _lease_heartbeat_loop(self) -> None:
        """MV2 — refresh owner.lock heartbeat until stop or owner loss.

        On each interval the lease re-reads its lock under the guard and
        bumps ``heartbeat_at`` iff it still owns it. A foreign owner read
        back (another process took over via stale/force) flips the engine
        to read-only via ``_on_lease_lost`` and ends the loop.
        """
        assert self._lease_stop is not None
        assert self._lease is not None
        interval = self.config.lease_heartbeat_seconds
        while not self._lease_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._lease_stop.wait(), timeout=interval,
                )
                break  # stop signalled
            except asyncio.TimeoutError:
                pass  # interval elapsed, try a heartbeat tick
            if not self._lease._refresh_heartbeat():
                # Owner_id mismatch read back under the guard — we were
                # displaced. Transition to read-only and stop refreshing
                # (further writes would clobber the new owner).
                self._on_lease_lost()
                break

    def _on_lease_lost(self) -> None:
        """Latch the read-only transition after the heartbeat detects loss.

        Sets both the engine-wide ``_persist_blocked`` and the cache's
        ``persist_blocked`` so every persistence route (write-behind flush,
        FAISS save loop, virtual-FAISS save loop, shutdown final save /
        flush, mutating method entry) gates on the same signal. The ERROR
        log fires once per process (``_lease_lost_warned`` latch).
        """
        self._persist_blocked = True
        self.cache.persist_blocked = True
        if not self._lease_lost_warned:
            self._lease_lost_warned = True
            logger.error(
                "Owner lease lost — heartbeat detected owner_id mismatch. "
                "Engine transitioning to read-only. Mutating operations "
                "will raise LeaseLostError. Reads still work."
            )

    # --- US1: Document Indexing ---

    async def index_documents(
        self, documents: list[dict],
    ) -> list[str]:
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        hashes = [
            hashlib.sha256(d["content"].encode("utf-8")).hexdigest()
            for d in documents
        ]
        existing = await self.store.find_existing_hashes(hashes)
        filtered = [
            (d, h) for d, h in zip(documents, hashes) if h not in existing
        ]
        if not filtered:
            logger.info("All %d documents already exist, skipping", len(documents))
            return []

        docs_to_index = [d for d, _ in filtered]
        skipped = len(documents) - len(docs_to_index)
        if skipped:
            logger.info("Skipping %d duplicate documents", skipped)

        contents = [d["content"] for d in docs_to_index]
        metadatas = [d.get("metadata") for d in docs_to_index]
        ids = [str(uuid.uuid4()) for _ in docs_to_index]

        # Phase M Stage 1 — stamp structural identifiers on metadata before
        # we hand it to the store, so the self-force filter has something
        # to inspect on every node.
        #   * ``original_id``: defaults to the node's own id (single
        #     remember acts as its own "document"). If the caller already
        #     supplied one — or provided ``file_path`` from a chunking
        #     ingest path — we honour that, so all chunks of the same
        #     file share the same original_id and stop inflating each
        #     other's mass.
        #   * ``cohort_id``: assigned only when this batch is going to
        #     trigger a Phase K supernova (cohort size ≥ threshold). All
        #     nodes in the cohort share the same id; singleton remembers
        #     stay absent so they never self-cancel by accident.
        cohort_id: str | None = None
        if (
            self.config.supernova_enabled
            and len(ids) >= self.config.supernova_min_cohort_size
        ):
            cohort_id = uuid.uuid4().hex[:12]

        for i, doc_id in enumerate(ids):
            meta = metadatas[i] or {}
            if "original_id" not in meta:
                # H8: only group by file_path when it is an UNAMBIGUOUS
                # absolute path. A bare basename / relative path (e.g.
                # "README.md") is not a global identity — two unrelated
                # ingests that happen to share it would be treated as the
                # same document and have their genuine external-referral
                # mass suppressed as "internal trade" (false self-force,
                # corrupting Mass Conservation). Falling back to the node's
                # own id is the safe direction: a node only ever
                # self-matches itself, so a *missed* grouping merely costs
                # a little mass conservation for that ingest, whereas a
                # *false* grouping actively corrupts the gravity field.
                # Loaders that want chunk-grouping must pass an absolute
                # file_path (scripts/load_files.py does) or set
                # original_id explicitly.
                fp = meta.get("file_path")
                if isinstance(fp, str) and os.path.isabs(fp):
                    meta["original_id"] = fp
                else:
                    meta["original_id"] = doc_id
            if cohort_id is not None:
                meta["cohort_id"] = cohort_id
            metadatas[i] = meta

        vectors = self.embedder.encode_documents(contents)
        self.faiss_index.add(vectors, ids)
        if self.virtual_faiss_index is not None:
            # Fresh nodes have displacement=0, so virtual_pos == raw.
            # The genesis kick below mutates cache.displacement; for now,
            # raw and virtual stay aligned, and a later compact (or the
            # next kick step) will pull virtual_pos away from raw if
            # needed.
            self.virtual_faiss_index.add(vectors, ids)
        # Phase L Stage 1: feed BM25 with the document text so lexical
        # matches on the new docs are findable immediately, without waiting
        # for the next compact/startup rebuild.
        # WP-6c: journal append は現行 index への適用の **前** に行う。
        # append 完了 → (await なし) add の間に swap が入る余地がなく、
        # 「旧 object に適用されたが journal には無い」隙間が生じない。
        # journal が閉じている (= build 完了後) 場合、直後の add は
        # swap 済みの新 object に当たる。
        await self._journal_bm25_mutation("add", ids, contents, gate_too=True)
        if self.bm25_index is not None:
            self.bm25_index.add(ids, contents)
        if self.ambient_gate_index is not None:
            self.ambient_gate_index.add(ids, contents)

        now = time.time()
        docs_for_store = []
        for i, doc_id in enumerate(ids):
            doc_in = docs_to_index[i]
            state = NodeState(
                id=doc_id,
                last_access=now,
                expires_at=doc_in.get("expires_at"),
                emotion_weight=float(doc_in.get("emotion", 0.0)),
                certainty=float(doc_in.get("certainty", 1.0)),
                last_verified_at=now if "certainty" in doc_in else None,
            )
            self.cache.set_node(state, dirty=True)
            meta = metadatas[i] or {}
            src = meta.get("source")
            if src:
                self.cache.set_source(doc_id, src)
            # Phase J Stage 2: mirror tags so tag_filter injection sees
            # them without waiting for a cache reload.
            tags = meta.get("tags")
            if isinstance(tags, list):
                self.cache.set_tags(doc_id, [t for t in tags if isinstance(t, str)])
            # Phase M Stage 1: mirror structural identifiers — the
            # self-force filter in the wave-driven mass update consults
            # the cache, not the store, so we have to populate it now
            # instead of waiting for the next restart.
            original_id = meta.get("original_id")
            if isinstance(original_id, str) and original_id:
                self.cache.set_original(doc_id, original_id)
            cohort_meta = meta.get("cohort_id")
            if isinstance(cohort_meta, str) and cohort_meta:
                self.cache.set_cohort(doc_id, cohort_meta)
            docs_for_store.append({
                "id": doc_id,
                "content": contents[i],
                "metadata": metadatas[i],
            })

        await self.store.save_documents(docs_for_store)

        # Phase G — Genesis kick: apply one-step Newtonian gravity so the
        # new nodes enter the field with non-zero orbital state instead of
        # competing against established clusters from a "naked" mass=1,
        # displacement=0 starting point. See Plans-Phase-G-Memory-Genesis.md.
        if self.config.genesis_kick_enabled:
            self._apply_genesis_kick(ids, vectors)

        # Phase K — Stellar supernova cohort: when the batch is large enough,
        # form mutual co-occurrence edges + outward initial velocity so the
        # newly-born cohort has internal gravity from birth. See
        # Plans-Phase-K-Stellar-Supernova-Cohort.md. Applied after Phase G so
        # cohort-internal coupling stacks on top of existing-system binding.
        if self.config.supernova_enabled:
            self._apply_supernova_cohort(ids, vectors)

        await self.cache.flush_to_store(self.store)
        self._faiss_dirty = True

        logger.info("Indexed %d documents", len(ids))
        return ids

    def _top_k_heavy_neighbors(
        self,
        vec: np.ndarray,
        k: int,
        pool_size: int = 50,
    ) -> list[tuple[np.ndarray, float]]:
        """Pull a wide FAISS top-N pool, rerank by cached mass, return the
        top-k as (embedding, mass) pairs. Used by the genesis kick to find
        the heavy bodies whose gravity will bend the new node's orbit."""
        pool = self.faiss_index.search(vec.reshape(1, -1), pool_size)
        if not pool:
            return []
        candidates: list[tuple[str, float]] = []
        for nid, _cos in pool:
            state = self.cache.get_node(nid)
            if state is None or state.is_archived:
                continue
            candidates.append((nid, state.mass))
        if not candidates:
            return []
        candidates.sort(key=lambda t: t[1], reverse=True)
        candidates = candidates[:k]
        ids_only = [nid for nid, _ in candidates]
        vec_map = self.faiss_index.get_vectors(ids_only)
        out: list[tuple[np.ndarray, float]] = []
        for nid, mass in candidates:
            v = vec_map.get(nid)
            if v is not None:
                out.append((v, mass))
        return out

    def _apply_genesis_kick(
        self, new_ids: list[str], new_vecs: np.ndarray,
    ) -> None:
        """Run one Verlet step of neighbor gravity on each freshly-indexed
        node, seeding cache displacement/velocity and bumping mass.
        Skips nodes with no qualifying neighbors (an empty DB or a region
        with no heavy bodies)."""
        for i, new_id in enumerate(new_ids):
            new_vec = new_vecs[i]
            neighbors = self._top_k_heavy_neighbors(
                new_vec,
                k=self.config.genesis_kick_neighbor_k,
                pool_size=self.config.genesis_kick_pool_size,
            )
            if not neighbors:
                continue
            disp, vel, m_boost = compute_gravity_kick(
                new_vec, neighbors, self.config,
            )
            disp_norm = float(np.linalg.norm(disp))
            if disp_norm <= 1e-9 and m_boost <= 0.0:
                continue
            self.cache.set_displacement(new_id, disp)
            self.cache.set_velocity(new_id, vel)
            state = self.cache.get_node(new_id)
            if state is not None and m_boost > 0.0:
                state.mass = max(state.mass, 1.0 + m_boost)
                self.cache.set_node(state, dirty=True)

    def _apply_supernova_cohort(
        self, new_ids: list[str], new_vecs: np.ndarray,
    ) -> None:
        """Form mutual co-occurrence edges + outward initial velocity for
        the supernova cohort.

        Velocity is *added* to whatever Phase G genesis kick already put
        in cache (typically Phase G writes a velocity toward existing
        heavy bodies; Phase K adds an outward push from the batch
        centroid; the two compose). Edges are written via
        ``cache.set_edge`` which mirrors both directions of the
        undirected graph and marks them dirty for write-behind flush.
        """
        from gaottt.core.supernova import (
            compute_supernova_velocities,
            form_supernova_edges,
        )

        # Mutual co-occurrence edges
        edges = form_supernova_edges(new_ids, self.config)
        for src, dst, weight in edges:
            self.cache.set_edge(src, dst, weight, dirty=True)

        # Outward initial velocity (added to any Phase G velocity)
        velocities = compute_supernova_velocities(new_ids, new_vecs, self.config)
        for nid, v_supernova in velocities.items():
            existing = self.cache.get_velocity(nid)
            if existing is not None:
                combined = existing + v_supernova
            else:
                combined = v_supernova
            from gaottt.core.gravity import clamp_vector
            combined = clamp_vector(combined, self.config.orbital_max_velocity)
            self.cache.set_velocity(nid, combined.astype(np.float32))

    # --- US2: Query (Gravity Wave Propagation) ---

    async def query(
        self,
        text: str,
        top_k: int | None = None,
        wave_depth: int | None = None,
        wave_k: int | None = None,
        use_cache: bool = False,
        source_filter: list[str] | None = None,
        persona_context: list[str] | None = None,
        tag_filter: list[str] | None = None,
        out_training_delta: dict | None = None,
        gamma_override: float | None = None,
        passive: bool = False,
        multi_source: bool | None = None,
        diversity: float | None = None,
        out_wave_stats: dict | None = None,
    ) -> list[QueryResultItem]:
        """Run a recall query.

        ``use_cache=True`` consults the prefetch cache first; on hit the
        cached results are returned without re-running embedding/wave/scoring
        (and crucially, without re-applying simulation updates — the prefetch
        already paid that cost). Cache hits are bounded by
        ``config.prefetch_ttl_seconds``.

        ``source_filter`` (Phase H Stage 2) lets the seed step trim the
        FAISS pool to nodes whose ``metadata.source`` matches. Source
        filtering is not part of the prefetch cache key, so any call with
        ``source_filter`` set bypasses the cache.

        ``persona_context`` (Phase J Stage 2) — explicit list of declared
        value/intention/commitment IDs overriding the Stage 1 auto-detect,
        plus additive seed injection of those IDs.

        ``tag_filter`` (Phase J Stage 2) — substring list (OR match) for
        additive seed injection of every node whose ``metadata.tags`` list
        contains any substring. Bypasses ``source_filter``.

        ``gamma_override`` (Hardening Stage 1 / C3) — per-call temperature
        scale used instead of ``config.gamma`` for this recall only. Lets
        ``explore`` widen the thermal noise without monkey-patching the
        shared config across an await (which corrupted concurrent recalls).
        A non-default gamma must never read or write the shared (text, k)
        prefetch cache, so it also bypasses the cache.

        ``passive`` (Ambient Recall) — read-only recall. The search runs in
        full, but the gravity field is not perturbed afterward: no mass
        update, no query-attraction displacement, no co-occurrence edges.
        A passive recall still *reads* the prefetch cache (a cache hit is
        side-effect-free anyway), but it never *writes* it — a passive
        result must not poison a later active recall into skipping its TTT
        update. Used by automatic / background recall (Claude Code hook).

        ``diversity`` (Phase T Stage 6) — diversified presentation for
        ``explore``. ``None`` or ``<= 0.0`` (or
        ``explore_diversified_presentation_enabled=False``) keeps the
        legacy path bit-for-bit. A positive value draws the natural
        candidate pool as the top ``top_k ×
        explore_diversity_pool_multiplier`` slice of the already
        wave-reached, already-scored results list — no additional search
        runs and seed/wave reachability is untouched (plan non-goal) —
        then applies the ``explore_min_semantic`` raw-cosine floor, and
        selects the natural slots via canonical MMR (forced/injected
        items keep the legacy ordering rules and are exempt from MMR).
        A diversified
        recall also bypasses the prefetch cache: the cache key does not
        carry ``diversity``, and a cached diversified (or plain) entry
        must not leak into the other selection mode.

        ``out_wave_stats`` (Phase U WP-5) — optional observation side
        channel: when a dict is passed, the engine records the effective
        wave depth (``"depth"``) and the wave reach (``"reached"``) into
        it, unconditionally of ``passive`` (pure observation). Never
        populated on a prefetch-cache hit — callers that need the stats
        must bypass the cache (explore always does via gamma_override).

        Either explicit argument bypasses the prefetch cache.
        """
        # MV2 — read-only transition: when the lease was lost, a query still
        # returns results but must NOT perturb the gravity field. Forcing
        # ``passive=True`` routes through the existing passive gates (mass /
        # displacement / co-occurrence / return_count updates all skip on a
        # passive recall), so the field is observed, never mutated.
        if self._persist_blocked:
            passive = True
        k = top_k or self.config.top_k
        diversity_active = (
            diversity is not None
            and diversity > 0.0
            and self.config.explore_diversified_presentation_enabled
        )
        if (
            source_filter or persona_context or tag_filter or gamma_override is not None
            or diversity_active
        ):
            use_cache = False
        if use_cache:
            cached = self.prefetch_cache.get(text, k, wave_depth, wave_k)
            if cached is not None:
                if out_training_delta is not None:
                    # Phase O Stage 2 — cache hit means no simulation ran.
                    # Signal that explicitly so the caller can distinguish
                    # "TTT update was suppressed" from "no nodes were touched".
                    out_training_delta["cache_hit"] = True
                return cached
        results = await self._query_internal(
            text=text, top_k=k, wave_depth=wave_depth, wave_k=wave_k,
            source_filter=source_filter,
            persona_context=persona_context,
            tag_filter=tag_filter,
            out_training_delta=out_training_delta,
            gamma_override=gamma_override,
            passive=passive,
            multi_source=multi_source,
            diversity=diversity if diversity_active else None,
            out_wave_stats=out_wave_stats,
        )
        # A passive recall never writes the shared prefetch cache: a cached
        # passive result would let a subsequent active recall hit the cache
        # and silently skip its simulation update.
        if use_cache and not passive:
            self.prefetch_cache.put(text, k, results, wave_depth, wave_k)
        return results

    async def _query_internal(
        self,
        text: str,
        top_k: int,
        wave_depth: int | None,
        wave_k: int | None,
        _is_synthetic: bool = False,
        source_filter: list[str] | None = None,
        persona_context: list[str] | None = None,
        tag_filter: list[str] | None = None,
        out_training_delta: dict | None = None,
        gamma_override: float | None = None,
        passive: bool = False,
        multi_source: bool | None = None,
        diversity: float | None = None,
        out_wave_stats: dict | None = None,
    ) -> list[QueryResultItem]:
        # MV2 — when the lease was lost, force passive so mass / displacement
        # / co-occurrence / return_count updates are all skipped. Putting this
        # guard here (not just in query()) covers ALL callers: query(),
        # prefetch(), and the dream loop. The query()-level guard above is
        # now belt-and-suspenders and kept for readability.
        if self._persist_blocked:
            passive = True
        k = top_k
        # Phase T Stage 6 — ``query()`` normalized a disabled flag or
        # non-positive diversity to None, so any value here is active.
        # Direct ``_query_internal`` callers (prefetch, dream loop) never
        # pass diversity and keep the legacy path.
        diversity_active = diversity is not None
        query_vec = self.embedder.encode_query(text)

        # Multi-Source Query — when enabled, segment the prompt into clauses
        # and batch-embed each as a separate point mass. The wave then seeds
        # from the superposed per-segment pools instead of the pooled
        # centroid; ``query_vec`` (the whole-prompt embedding) stays the
        # scoring / TTT anchor. ``multi_source`` overrides the config flag
        # (the ambient path passes ``multi_source_ambient_enabled``); None
        # falls back to ``config.multi_source_enabled``. See
        # docs/wiki/Plans-Query-Mass-Distribution.md.
        segment_vecs: np.ndarray | None = None
        n_intent_centers = 1
        ms_on = (
            self.config.multi_source_enabled if multi_source is None
            else multi_source
        )
        if ms_on:
            segments = segment_query(text, self.config)
            if len(segments) > 1:
                # ``encode_queries`` is the batched fast path (RuriEmbedder);
                # fall back to per-segment ``encode_query`` for embedders that
                # only implement the single-query method (e.g. test stubs).
                encode_many = getattr(self.embedder, "encode_queries", None)
                if encode_many is not None:
                    segment_vecs = encode_many(segments)
                else:
                    segment_vecs = np.vstack(
                        [self.embedder.encode_query(s) for s in segments]
                    )
                n_intent_centers = len(segments)

        # Phase J Stage 1 / Stage 2: compute persona proximities once per
        # recall. Stage 2 explicit `persona_context` takes precedence over
        # the Stage 1 auto-detected active set.
        persona_proximities: dict[str, float] | None = None
        if self.config.persona_boost_enabled and self.config.persona_boost_alpha > 0.0:
            if persona_context:
                persona_ids: set[str] = set(persona_context)
            else:
                persona_ids = collect_active_persona_ids(
                    self.cache, self.config, time.time(),
                )
            if persona_ids:
                persona_proximities = compute_persona_proximities(
                    persona_ids, self.cache, self.config,
                )

        # Phase J Stage 2: build the additive injection set — explicit
        # persona_context ids plus every node matching the tag_filter
        # substring(s).
        injected_ids: set[str] | None = None
        if persona_context or tag_filter:
            injected_ids = set()
            if persona_context:
                injected_ids |= set(persona_context)
            if tag_filter:
                injected_ids |= self.cache.find_ids_by_tag_filter(tag_filter)
            if not injected_ids:
                injected_ids = None

        # Step 1: Gravity wave propagation — recursive neighbor expansion.
        # Phase M Stage 1: capture per-parent force attribution so the
        # mass-update path can filter same-document / same-cohort
        # "internal trade" contributions (Mass Conservation rule).
        wave_attribution: dict[str, dict[str, float]] = {}
        reached = propagate_gravity_wave(
            query_vec, self.faiss_index, self.cache, self.config,
            wave_k=wave_k, wave_depth=wave_depth,
            source_filter=source_filter,
            virtual_faiss_index=self.virtual_faiss_index,
            persona_proximities=persona_proximities,
            injected_ids=injected_ids,
            query_text=text,
            bm25_index=self.bm25_index,
            out_attribution=wave_attribution,
            segment_vectors=segment_vecs,
        )

        # Phase U WP-5 — wave propagation observability side channel.
        # Populated before any early exit so the caller always sees the
        # depth actually used (``wave_depth`` override or config default —
        # same expression the training-delta block reports) and the raw
        # wave reach, passive or not.
        if out_wave_stats is not None:
            out_wave_stats["depth"] = (
                wave_depth if wave_depth is not None
                else self.config.wave_max_depth
            )
            out_wave_stats["reached"] = len(reached)

        if not reached:
            return []

        # Step 2: Get original embeddings for all reached nodes
        reached_ids = list(reached.keys())
        original_embs = self.faiss_index.get_vectors(reached_ids)

        # Step 3: Score all reached nodes with virtual coordinates + wave boost
        now = time.time()
        query_vec_flat = query_vec[0] if query_vec.ndim == 2 else query_vec
        q_norm = float(np.linalg.norm(query_vec_flat)) + 1e-12
        results: list[QueryResultItem] = []
        # Coordinate naming:
        #   gravity_sim  = query_raw · virtual_pos  (stored as QueryResultItem.raw_score,
        #                  labelled "virtual_score" in MCP output).  Carries displacement
        #                  and temperature noise — reflects how far the node has drifted
        #                  toward frequently co-recalled queries.
        #   pure_raw_cosines = query_raw · node_raw  (no displacement).  Used only for
        #                  Phase J Stage 3 forced-set ordering where "closest to this
        #                  query's vocabulary" must win over "most-touched memo".
        #   QueryResultItem.raw_score keeps the field name for REST backward compat;
        #   formatters.format_recall labels it "virtual_score" in MCP output (2026-05-12).
        pure_raw_cosines: dict[str, float] = {}

        # Phase O Stage 1 — informational: precompute which reached nodes the
        # BM25 index hit for this query. Used only for the breakdown flag
        # (bm25_contributed) since BM25's actual additive contribution is
        # already folded into wave_score via _seed_boost RRF fusion.
        #
        # Phase T Stage 3 — the same single search now also feeds the direct
        # relevance qualification (lexical axis). Qualification must not
        # depend on the observability flag (Codex blocking #2), so the pool
        # runs when either qualification flag is on even with
        # ``expose_score_breakdown=False``; the expose-only legacy path keeps
        # its wider ``max(len(reached_ids), 50)`` pool so ``bm25_contributed``
        # stays bit-identical when qualification is off.
        qualification_active = (
            self.config.direct_qualification_enabled
            or self.config.ttt_qualification_enabled
        )
        bm25_hit_ids: set[str] = set()
        bm25_pool_scores: dict[str, float] = {}
        bm25_pool_top = 0.0
        if (
            (qualification_active or self.config.expose_score_breakdown)
            and self.config.hybrid_bm25_enabled
            and self.bm25_index is not None
            and self.bm25_index.size > 0
            and text
        ):
            try:
                pool_n = (
                    self.config.direct_bm25_pool_size
                    if qualification_active
                    else max(len(reached_ids), 50)
                )
                bm25_hits = self.bm25_index.search(text, pool_n)
                bm25_hit_ids = {nid for nid, _ in bm25_hits}
                bm25_pool_scores = dict(bm25_hits)
                if bm25_pool_scores:
                    bm25_pool_top = max(bm25_pool_scores.values())
            except Exception:
                bm25_hit_ids = set()
                bm25_pool_scores = {}
                bm25_pool_top = 0.0

        # Phase T Stage 3 — per-node qualification verdicts and learning
        # confidences, computed inside the scoring loop below. Empty (and
        # never consulted) when both qualification flags are OFF — the
        # legacy path stays bit-for-bit identical.
        qualification_map: dict[str, bool] = {}
        learn_confidences: dict[str, float] = {}

        # Phase U WP-5 — selection-trace provenance (observability only;
        # never a scoring / ordering input). The raw-vs-virtual seed
        # origin is not recorded inside the wave (gravity._union_pool
        # merges the pools id-wise and discards per-pool membership), so
        # classify at the engine: re-draw the seed pool at the SAME size
        # the wave used (mirror of propagate_gravity_wave's pool sizing)
        # and test membership. Classification:
        #   injected → "forced"; raw-pool member → "raw"; anything else
        #   entered through the displacement-aware virtual index (virtual
        #   seed pool, or Phase H Stage 5 virtual neighbor expansion) →
        #   "virtual"; with no virtual index the raw index drove every
        #   expansion → "raw". Multi-source queries (segment
        #   superposition) approximate with the centroid query — the
        #   trace is informational, and this path never gates anything.
        prov_raw_ids: set[str] = set()
        prov_virt_ids: set[str] = set()
        prov_virtual_neighbors = (
            self.config.wave_neighbor_use_virtual
            and self.virtual_faiss_index is not None
            and self.virtual_faiss_index.size > 0
        )
        if self.config.expose_score_breakdown:
            prov_initial_k = (
                wave_k if wave_k is not None else self.config.wave_initial_k
            )
            prov_has_boost = (
                self.config.wave_seed_mass_alpha > 0.0
                or (
                    persona_proximities is not None
                    and self.config.persona_boost_alpha > 0.0
                )
            )
            if source_filter:
                prov_pool_n = max(
                    prov_initial_k, self.config.wave_k_with_filter,
                )
            elif prov_has_boost or injected_ids:
                prov_pool_n = max(
                    prov_initial_k, self.config.wave_seed_pool_size,
                )
            else:
                prov_pool_n = prov_initial_k
            prov_raw_ids = {
                nid for nid, _ in self.faiss_index.search(query_vec, prov_pool_n)
            }
            if (
                self.virtual_faiss_index is not None
                and self.virtual_faiss_index.size > 0
            ):
                prov_virt_ids = {
                    nid for nid, _ in
                    self.virtual_faiss_index.search(query_vec, prov_pool_n)
                }

            def _provenance_of(nid: str) -> str:
                if injected_ids and nid in injected_ids:
                    return "forced"
                if nid in prov_raw_ids:
                    return "raw"
                if nid in prov_virt_ids or prov_virtual_neighbors:
                    return "virtual"
                return "raw"

        for node_id in reached_ids:
            state = self.cache.get_node(node_id)
            if state is None:
                states = await self.store.get_node_states([node_id])
                state = states.get(node_id)
            if state is None or state.is_archived:
                continue
            if state.expires_at is not None and state.expires_at <= now:
                continue

            original_emb = original_embs.get(node_id)
            if original_emb is None:
                continue

            displacement = self.cache.get_displacement(node_id)
            virtual_pos = compute_virtual_position(
                original_emb, displacement, state.temperature
            )

            gravity_sim = float(np.dot(query_vec_flat, virtual_pos))
            # Pure raw cosine — no displacement, no temperature noise.
            emb_norm = float(np.linalg.norm(original_emb)) + 1e-12
            pure_raw_cosines[node_id] = (
                float(np.dot(query_vec_flat, original_emb)) / (q_norm * emb_norm)
            )

            # Phase T Stage 3 — normalized virtual cosine (NEW computation;
            # ``gravity_sim`` above keeps its non-normalized dot contract
            # unchanged) and the per-node relevance qualification. Skipped
            # entirely when both qualification flags are OFF.
            virtual_cos_norm: float | None = None
            node_qualified: bool | None = None
            if qualification_active:
                vp_norm = float(np.linalg.norm(virtual_pos))
                virtual_cos_norm = (
                    float(np.dot(query_vec_flat, virtual_pos))
                    / (q_norm * vp_norm)
                    if vp_norm > 0.0
                    else 0.0
                )
                bm25_sc = bm25_pool_scores.get(node_id, 0.0)
                lexical = compute_lexical_strength(bm25_sc, bm25_pool_top)
                node_qualified = is_direct_qualified(
                    pure_raw_cosines[node_id], virtual_cos_norm, bm25_sc, lexical,
                    self.config.direct_raw_cosine_min,
                    self.config.direct_virtual_cosine_min,
                    self.config.direct_bm25_absolute_min,
                    self.config.direct_bm25_relative_min,
                )
                qualification_map[node_id] = node_qualified
                learn_confidences[node_id] = qualification_confidence(
                    pure_raw_cosines[node_id], virtual_cos_norm, bm25_sc, lexical,
                    self.config.direct_raw_cosine_min,
                    self.config.direct_virtual_cosine_min,
                    self.config.direct_bm25_absolute_min,
                    self.config.direct_bm25_relative_min,
                )

            mass_boost = compute_mass_boost(state.mass, self.config.alpha)
            # Phase T Stage 2 — semantic decay contract: half-life + floor
            # (default) or the legacy per-second ``delta`` rate (flag off =
            # bit-for-bit legacy path, unclamped future timestamps included).
            if self.config.semantic_halflife_enabled:
                decay = compute_semantic_factor(
                    state.last_access, now,
                    self.config.semantic_half_life_seconds,
                    self.config.semantic_floor,
                )
            else:
                decay = compute_decay(state.last_access, now, self.config.delta)
            wave_boost = self.config.wave_boost_weight * reached[node_id]
            emotion_boost = compute_emotion_boost(
                state.emotion_weight, self.config.emotion_alpha,
            )
            certainty_boost = compute_certainty_boost(
                state.certainty, state.last_verified_at, now,
                self.config.certainty_alpha, self.config.certainty_half_life_seconds,
            )

            # Presentation saturation: frequently returned nodes get lower scores
            saturation = 1.0 / (1.0 + state.return_count * self.config.saturation_rate)

            final = (
                gravity_sim * decay + mass_boost + wave_boost
                + emotion_boost + certainty_boost
            ) * saturation

            if final <= 0.0:
                continue

            doc = await self.store.get_document(node_id)
            if doc is None:
                continue

            breakdown: ScoreBreakdown | None = None
            if self.config.expose_score_breakdown:
                persona_prox = 0.0
                if persona_proximities is not None:
                    persona_prox = float(persona_proximities.get(node_id, 0.0))
                breakdown = ScoreBreakdown(
                    raw_cosine=pure_raw_cosines[node_id],
                    virtual_cosine=gravity_sim,
                    decay_factor=decay,
                    wave_score=wave_boost,
                    mass_boost=mass_boost,
                    emotion_term=emotion_boost,
                    certainty_term=certainty_boost,
                    saturation=saturation,
                    persona_proximity=persona_prox,
                    bm25_contributed=node_id in bm25_hit_ids,
                    forced_inclusion=bool(injected_ids and node_id in injected_ids),
                    # Phase T Stage 3 — informational qualification fields
                    # (never enter final_score / expected_sum). ``lensing_gap``
                    # doubles as the query-path gap signal
                    # ``virtual_cos_norm - raw_cos`` when qualification ran;
                    # the ambient lensing slot overwrites it with its own gap.
                    qualified=node_qualified,
                    direct_score=(
                        virtual_cos_norm * decay
                        if virtual_cos_norm is not None
                        else None
                    ),
                    field_score=(
                        wave_boost + mass_boost + emotion_boost + certainty_boost
                        if virtual_cos_norm is not None
                        else None
                    ),
                    lensing_gap=(
                        virtual_cos_norm - pure_raw_cosines[node_id]
                        if virtual_cos_norm is not None
                        else 0.0
                    ),
                    # Phase U WP-5 — selection trace (informational only;
                    # cohort uses the Stage 7.1 structural cluster key so
                    # the trace and the anti-hub/MMR penalties speak the
                    # same cluster identity).
                    cohort=cluster_key_from_cache(self.cache, node_id),
                    provenance=_provenance_of(node_id),
                )

            results.append(
                QueryResultItem(
                    id=node_id,
                    content=doc["content"],
                    metadata=doc.get("metadata"),
                    raw_score=gravity_sim,
                    final_score=final,
                    score_breakdown=breakdown,
                )
            )

        if not results and reached_ids and self.cache.node_cache:
            reached_in_store = sum(
                1 for node_id in reached_ids
                if self.cache.get_node(node_id) is not None
            )
            if reached_in_store == 0:
                raise RuntimeError(
                    "Semantic retrieval index/store mismatch: FAISS returned "
                    f"{len(reached_ids)} node IDs, but none exist in the active "
                    "SQLite cache. The FAISS files likely belong to a different "
                    "database snapshot. Stop all GaOTTT processes, run "
                    "`scripts/rebuild_faiss_from_db.py --apply`, then restart."
                )

        # Phase U WP-4b — raw-top rescue の準備 (presentation 専用)。
        # scored results 全体 (sort 前) で raw cosine 降順の 1-based rank
        # を作り、qualified ∧ rank ≤ direct_rescue_raw_rank の natural
        # item を sort step で先頭 tier に lift する。rank は観測の土台
        # (raw cosine) 上の順位 — genesis kick 等で displacement が
        # 発散し virtual cosine が沈んだ near-exact match が final_score
        # 最下位帯で top-K から消える病理 (docs/notes/phase-u/
        # wp4-trace-findings.md, target de1b528f) への防腐剤。
        # knob=0 または qualification OFF は構築すらスキップし、legacy
        # 経路は bit-for-bit 不変 (rollback 契約)。
        rescue_active = (
            self.config.direct_qualification_enabled
            and self.config.direct_rescue_raw_rank > 0
        )
        rescued_ids: set[str] = set()
        if rescue_active:
            raw_rank_map: dict[str, int] = {
                r.id: rank
                for rank, r in enumerate(
                    sorted(
                        results,
                        key=lambda r: (
                            -pure_raw_cosines.get(r.id, 0.0), r.id,
                        ),
                    ),
                    start=1,
                )
            }
            _rescue_cap = self.config.direct_rescue_raw_rank
            rescued_ids = {
                r.id
                for r in results
                if qualification_map.get(r.id, False)
                and raw_rank_map.get(r.id, _rescue_cap + 1) <= _rescue_cap
            }

        def _natural_rescue_key(r: QueryResultItem) -> tuple[int, int, float]:
            # 3-tier: rescued → qualified → fallback。rescued tier の
            # 内部順のみ raw cosine 降順 (観測信号。rescued は構成上
            # ≤ cap 件)、他 tier は Stage 3 契約どおり final_score
            # 降順。
            if r.id in rescued_ids:
                return (0, 0, -pure_raw_cosines.get(r.id, 0.0))
            return (
                1,
                0 if qualification_map.get(r.id) else 1,
                -r.final_score,
            )

        # Step 4: Sort and take top-K for presentation to LLM.
        # Phase J Stage 2: when explicit injection is requested, force the
        # injected ids into the top-K result. Seed-pool injection alone
        # isn't enough — once the target is a seed, its own wave neighbours
        # can outrank it by sheer cluster mass. The caller's explicit ask
        # has to survive the final cut, not just the entry gate.
        #
        # Critical: when ``len(injected_ids) > k`` (e.g. ``tag_filter``
        # matching 112 nodes with ``top_k=5``), we still respect the
        # caller's ``top_k`` budget — pick the top-K *of the injected
        # set itself* — but rank the forced set by ``raw_score`` rather
        # than ``final_score`` (Phase J Stage 3). Final score is dominated
        # by mass / wave / emotion / certainty, which makes "frequently
        # touched memos win" regardless of query semantic. Inside a
        # caller-injected set, the right ordering is "which of these
        # tagged memos is closest to the query" — i.e. raw cosine.
        # Non-injected results still rank by final_score.
        #
        # Phase L Stage 1 supplement: when BM25 is active, compute a
        # per-node lexical score and combine it with pure raw cosine
        # via RRF so that surface-form matches ("Eleventy Pipeline" →
        # .eleventy.js) can outrank pure-embedding similarity.
        bm25_forced_scores: dict[str, float] = {}
        if (
            injected_ids
            and self.bm25_index is not None
            and self.bm25_index.size > 0
            and text
        ):
            bm25_pool = self.bm25_index.search(
                text, max(len(injected_ids), 50),
            )
            bm25_forced_scores = {nid: sc for nid, sc in bm25_pool}

        if injected_ids:
            forced = [r for r in results if r.id in injected_ids]
            others = [r for r in results if r.id not in injected_ids]

            if bm25_forced_scores:
                forced_cosine_rank: dict[str, int] = {
                    r.id: rank
                    for rank, r in enumerate(
                        sorted(
                            forced,
                            key=lambda r: pure_raw_cosines.get(r.id, 0.0),
                            reverse=True,
                        ),
                        start=1,
                    )
                }
                forced_bm25_rank: dict[str, int] = {}
                bm25_sorted = sorted(
                    bm25_forced_scores.items(),
                    key=lambda t: t[1],
                    reverse=True,
                )
                for rank, (nid, _) in enumerate(bm25_sorted, start=1):
                    if nid in injected_ids:
                        forced_bm25_rank[nid] = rank
                forced.sort(
                    key=lambda r: _rrf_forced_key(
                        r.id, forced_cosine_rank, forced_bm25_rank,
                        self.config.rrf_k,
                    ),
                    reverse=True,
                )
            else:
                forced.sort(
                    key=lambda r: pure_raw_cosines.get(r.id, 0.0),
                    reverse=True,
                )
            others.sort(key=lambda r: r.final_score, reverse=True)
            if rescue_active:
                # Phase U WP-4b — natural item を 3-tier (rescued →
                # qualified → fallback) で並べ替え。forced set は上の
                # Phase J 規則のまま (rescue は forced に触れない)。
                others.sort(key=_natural_rescue_key)
            elif self.config.direct_qualification_enabled:
                # Phase T Stage 3 — qualified-first partition on the natural
                # items only; the forced set keeps the Phase J rule above.
                others.sort(
                    key=lambda r: 0 if qualification_map.get(r.id) else 1,
                )
            if len(forced) >= k:
                results = forced[:k]
            else:
                n_rest = k - len(forced)
                if diversity_active:
                    # Phase T Stage 6 — forced slots are decided; the natural
                    # slots go through the diversified MMR selection.
                    results = forced + self._select_diverse_natural(
                        others, pure_raw_cosines, original_embs,
                        top_k=k, n_select=n_rest, diversity=diversity,
                        preselected_ids=[r.id for r in forced],
                    )
                else:
                    results = forced + others[:n_rest]
        else:
            results.sort(key=lambda r: r.final_score, reverse=True)
            if rescue_active:
                # Phase U WP-4b — 3-tier rescue 並べ替え (上の injected
                # branch と同一 key)。diversity (MMR) はこの並べ替え後
                # の pool から選択する — rescued item は候補のまま。
                results.sort(key=_natural_rescue_key)
            elif self.config.direct_qualification_enabled:
                # Phase T Stage 3 — stable secondary sort: qualified natural
                # items (final_score desc, from the sort above) precede
                # fallback picks (final_score desc). Result count is
                # unchanged — fallback items still fill the top_k slots.
                results.sort(
                    key=lambda r: 0 if qualification_map.get(r.id) else 1,
                )
            if diversity_active:
                results = self._select_diverse_natural(
                    results, pure_raw_cosines, original_embs,
                    top_k=k, n_select=k, diversity=diversity,
                )
            else:
                results = results[:k]

        # Step 5: Update return_count for presented nodes + habituation recovery for all.
        # Synthetic recalls (Phase G dream loop) skip return_count so that
        # background revisits don't trip presentation saturation — the user
        # never saw these results, so habituation must not punish them.
        # Lateral Association Stage 1 sub-step 0 (2026-05-25): passive recall
        # is also gated. ``passive=True`` means the ambient hook is observing
        # the field without perturbing it; saturation is field state that
        # drives next-call ranking, so silently mutating it via every ambient
        # turn breaks the "no perturbation" contract (the same way mass /
        # displacement / co-occurrence updates are already gated below). See
        # Plans-Ambient-Recall-Lateral-Association.md Stage 1.
        result_ids = [r.id for r in results]
        all_reached_ids = list(reached.keys())
        if not _is_synthetic and not passive:
            for node_id in result_ids:
                state = self.cache.get_node(node_id)
                if state:
                    state.return_count += 1.0
                    self.cache.set_node(state, dirty=True)

        # Habituation recovery: all reached nodes slowly recover freshness.
        # Synthetic dream-loop recalls still recover (background heal). Passive
        # ambient recalls do NOT — recovery is also field perturbation.
        if not passive:
            for node_id in all_reached_ids:
                state = self.cache.get_node(node_id)
                if state and state.return_count > 0:
                    state.return_count *= (1.0 - self.config.habituation_recovery_rate)
                    self.cache.set_node(state, dirty=True)

        # Phase O Stage 2 — snapshot displacement / mass for delta computation.
        # ``topk_only=True`` (default) limits coverage to top-K returned nodes
        # for context economy; ``False`` covers every reached node (debug).
        pre_disp_norms: dict[str, float] = {}
        pre_masses: dict[str, float] = {}
        delta_active = (
            out_training_delta is not None
            and self.config.training_delta_enabled
        )
        if delta_active:
            topk_only = self.config.training_delta_topk_only
            delta_target_ids = result_ids if topk_only else all_reached_ids
            for nid in delta_target_ids:
                disp = self.cache.get_displacement(nid)
                pre_disp_norms[nid] = float(np.linalg.norm(disp)) if disp is not None else 0.0
                state = self.cache.get_node(nid)
                pre_masses[nid] = float(state.mass) if state is not None else 0.0

        # Step 6: Simulation update — ALL reached nodes.
        # Phase I Stage 2: pass the query vector + wave scores so the orbital
        # step can apply the query-attraction term to reached nodes.
        # Phase M Stage 1: pass per-parent attribution so the mass update can
        # apply the self-force (Mass Conservation) filter.
        # Ambient Recall: a passive recall observes the field without
        # perturbing it — skip mass update, query-attraction displacement and
        # co-occurrence so automatic / background queries never become an
        # uncontrolled TTT signal. The delta block below then reports zeros,
        # which is the honest answer (nothing moved).
        #
        # Phase T Stage 4 — the query-conditioned updates inside the
        # simulation (mass growth, query kick) apply only to the qualified
        # learn set; maintenance (last_access / evaporation / sim_history /
        # temperature / orbital N-body) stays all-reached. learn_ids is None
        # — legacy all-reached learning — when the gate is off, the recall
        # is passive, or the recall is synthetic: dream-loop rehearsal is
        # self-directed maintenance, not a user-query gradient path, so it
        # is exempt from the qualification gate (plan §3 Stage 4).
        learn_ids: list[str] | None = None
        learn_conf_map: dict[str, float] | None = None
        if (
            not passive
            and self.config.ttt_qualification_enabled
            and not _is_synthetic
        ):
            learn_ids = [
                nid for nid in all_reached_ids
                if qualification_map.get(nid, False)
            ]
            learn_conf_map = {
                nid: learn_confidences.get(nid, 0.0) for nid in learn_ids
            }

        # Phase U WP-5 — learn-set membership trace on the presented items.
        # Only written where the Stage 4 gate actually restricted learning
        # (active, non-synthetic, ttt ON): elsewhere the learn set is
        # all-reached and the field stays None (WP-5 contract — a True
        # there would carry no information).
        if learn_ids is not None:
            learn_trace = set(learn_ids)
            for r in results:
                if r.score_breakdown is not None:
                    r.score_breakdown.in_learn_set = r.id in learn_trace

        if not passive:
            self._update_simulation(
                all_reached_ids, reached, original_embs, now,
                query_anchor=query_vec_flat,
                wave_attribution=wave_attribution,
                gamma_override=gamma_override,
                learn_ids=learn_ids,
                learn_confidences=learn_conf_map,
            )
            if learn_ids is not None:
                # cooccurrence: presented ∩ learn set (presentation contract
                # + relevance)
                learn_set = set(learn_ids)
                self._update_cooccurrence(
                    [nid for nid in result_ids if nid in learn_set]
                )
            else:
                self._update_cooccurrence(result_ids)

        if delta_active:
            disp_changes: dict[str, float] = {}
            mass_changes: dict[str, float] = {}
            for nid in delta_target_ids:
                disp = self.cache.get_displacement(nid)
                post_d = float(np.linalg.norm(disp)) if disp is not None else 0.0
                disp_changes[nid] = post_d - pre_disp_norms.get(nid, 0.0)
                state = self.cache.get_node(nid)
                post_m = float(state.mass) if state is not None else 0.0
                mass_changes[nid] = post_m - pre_masses.get(nid, 0.0)
            persona_hops = 0
            if persona_proximities:
                for nid in all_reached_ids:
                    if persona_proximities.get(nid, 0.0) > 0.0:
                        persona_hops += 1
            out_training_delta["displacement_changes"] = disp_changes
            out_training_delta["mass_changes"] = mass_changes
            out_training_delta["wave_reached_count"] = len(reached)
            out_training_delta["wave_max_depth"] = (
                wave_depth if wave_depth is not None else self.config.wave_max_depth
            )
            out_training_delta["persona_hop_reached"] = persona_hops
            out_training_delta["supernova_triggered"] = False  # recall path
            out_training_delta["topk_only"] = self.config.training_delta_topk_only
            out_training_delta["intent_centers"] = n_intent_centers

        return results

    def _select_diverse_natural(
        self,
        ordered: list[QueryResultItem],
        pure_raw_cosines: dict[str, float],
        original_embs: dict[str, np.ndarray],
        *,
        top_k: int,
        n_select: int,
        diversity: float,
        preselected_ids: list[str] | None = None,
    ) -> list[QueryResultItem]:
        """Phase T Stage 6 — canonical MMR over the widened natural pool.

        ``ordered`` arrives in legacy presentation order (final_score
        desc; Stage 3 qualified-first already applied when enabled). The
        "widened pool" is not a second search: it is the top ``top_k ×
        explore_diversity_pool_multiplier`` entries of ``ordered`` —
        the same wave-reached, fully scored results list the legacy
        path ranks — so seed/wave reachability is bit-identical between
        the two modes; only the presentation cut differs. The cut is
        further filtered by the ``explore_min_semantic`` raw-cosine
        floor — lateral exploration still owes the query minimum
        relevance.
        Forced ids never pass through here as candidates: they occupy
        their slots via the legacy rules and only enter MMR as
        ``preselected_ids`` (redundancy + cluster-penalty reference, see
        ``diversity.mmr_select``).

        Presentation-derived updates (return_count / co-occurrence /
        training delta topk coverage) consume the returned selection —
        by construction only the MMR-presented ids.
        """
        cfg = self.config
        pool_k = top_k * cfg.explore_diversity_pool_multiplier
        pool_items = ordered[:pool_k]
        floored_ids = apply_relevance_floor(
            [r.id for r in pool_items],
            pure_raw_cosines,
            cfg.explore_min_semantic,
        )
        if not floored_ids:
            return []
        floored_set = set(floored_ids)
        relevance = normalize_relevance(
            {r.id: r.final_score for r in pool_items if r.id in floored_set},
        )
        preselected = list(preselected_ids or ())
        # ``original_embs`` covers every scored (hence every pool and
        # forced) node — Step 2 fetched vectors for the whole wave reach.
        embeddings = {
            nid: original_embs[nid]
            for nid in [*floored_ids, *preselected]
            if nid in original_embs
        }
        picked = mmr_select(
            floored_ids,
            relevance,
            embeddings,
            diversity=diversity,
            cohort_penalty=cfg.explore_cohort_penalty,
            cluster_key_of=lambda nid: cluster_key_from_cache(self.cache, nid),
            n_select=n_select,
            preselected=preselected,
        )
        by_id = {r.id: r for r in pool_items}
        return [by_id[nid] for nid in picked if nid in by_id]

    def _update_simulation(
        self,
        all_reached_ids: list[str],
        reached: dict[str, float],
        original_embs: dict[str, np.ndarray],
        now: float,
        query_anchor: np.ndarray | None = None,
        wave_attribution: dict[str, dict[str, float]] | None = None,
        gamma_override: float | None = None,
        learn_ids: list[str] | None = None,
        learn_confidences: dict[str, float] | None = None,
    ) -> None:
        """Update gravity simulation for ALL wave-reached nodes.

        This is the simulation layer: every node the wave touched gets
        mass/temperature updates and orbital mechanics (acceleration → velocity → position).
        Like dark matter, these invisible updates reshape the gravitational field.

        Phase T Stage 4 — update-category split. ``learn_ids=None`` keeps
        the legacy contract (every reached node learns, confidence 1.0).
        With a learn set:
          - all reached: evaporation, sim_history, temperature,
            last_access, orbital N-body participation
          - learn set only: mass growth (scaled by the passing-axis
            margin ``learn_confidences``) and the query kick (gated
            per-node via ``query_scores`` — absent key ⇒ no kick)
        """
        dim = self.config.embedding_dim
        masses = {}
        last_accesses = {}
        learn_set = set(learn_ids) if learn_ids is not None else None

        for node_id in all_reached_ids:
            state = self.cache.get_node(node_id)
            if state is None:
                continue

            force = reached.get(node_id, 0.0)

            # Phase M Stage 1 — Mass conservation filter. Sum only the
            # parent contributions that came from *outside* this node's
            # source document and supernova cohort. Same-original /
            # same-cohort co-occurrence is "internal trade" — Articulation
            # as Carrier (id=9a954c62) requires an external referrer to
            # generate mass. ``SEED_PARENT_ID`` (the query itself) and
            # absent attribution (legacy callers, no wave_attribution
            # passed) fall back to full ``force``.
            if (
                self.config.mass_conservation_enabled
                and wave_attribution is not None
            ):
                contributions = wave_attribution.get(node_id, {})
                if contributions:
                    mass_force = 0.0
                    for parent_id, contrib in contributions.items():
                        if parent_id == SEED_PARENT_ID:
                            mass_force += contrib
                        elif not is_self_force_by_id(self.cache, node_id, parent_id):
                            mass_force += contrib
                else:
                    mass_force = force
            else:
                mass_force = force

            # Phase N candidate β Stage 1 — lazy mass evaporation.
            # Apply the t_idle-accumulated decay *before* this recall's
            # Hebbian growth, so a heavily-touched-then-idle node first
            # repays its evaporation debt and then receives new mass on
            # top. No-op when disabled / below floor / inside grace window.
            state.mass = evaporate_mass(
                state.mass, state.last_access, now, self.config,
            )

            # Phase T Stage 4 — mass growth is query-conditioned learning:
            # only the qualified learn set grows, scaled by the passing-axis
            # margin confidence (1.0-equivalent when the gate is off, so the
            # legacy formula is reproduced exactly). Maintenance around this
            # line (evaporation above, sim_history / temperature /
            # last_access below) stays all-reached.
            if learn_set is None:
                state.mass += self.config.eta * mass_force * (
                    1.0 - state.mass / self.config.m_max
                )
            elif node_id in learn_set:
                confidence = (learn_confidences or {}).get(node_id, 0.0)
                state.mass += self.config.eta * mass_force * confidence * (
                    1.0 - state.mass / self.config.m_max
                )

            # Sim history ring buffer
            state.sim_history.append(force)
            if len(state.sim_history) > self.config.sim_buffer_size:
                state.sim_history = state.sim_history[-self.config.sim_buffer_size:]

            # Temperature
            if len(state.sim_history) >= 2:
                arr = np.array(state.sim_history)
                gamma = gamma_override if gamma_override is not None else self.config.gamma
                state.temperature = gamma * float(np.var(arr))
            else:
                state.temperature = 0.0

            last_accesses[node_id] = state.last_access
            state.last_access = now
            self.cache.set_node(state, dirty=True)
            masses[node_id] = state.mass

        # Orbital mechanics: acceleration → velocity → displacement
        active_ids = [nid for nid in all_reached_ids if nid in original_embs]
        if len(active_ids) >= 2:
            current_displacements = {}
            current_velocities = {}
            for nid in active_ids:
                cached_d = self.cache.get_displacement(nid)
                current_displacements[nid] = cached_d if cached_d is not None else np.zeros(dim, dtype=np.float32)
                cached_v = self.cache.get_velocity(nid)
                current_velocities[nid] = cached_v if cached_v is not None else np.zeros(dim, dtype=np.float32)

            new_disps, new_vels = update_orbital_state(
                active_ids, original_embs,
                current_displacements, current_velocities,
                masses, last_accesses, now, self.config,
                cache=self.cache,
                query_anchor=query_anchor,
                # Phase T Stage 4 — query kick (query-conditioned learning)
                # applies only to the learn set; absent keys gate the kick
                # per node (gravity.update_orbital_state: query_scores.get
                # → None → no kick). N-body participants stay all-reached.
                query_scores=(
                    (
                        {nid: reached[nid] for nid in learn_ids if nid in reached}
                        if learn_ids is not None
                        else reached
                    )
                    if query_anchor is not None
                    else None
                ),
            )

            for nid in new_disps:
                self.cache.set_displacement(nid, new_disps[nid])
                self.cache.set_velocity(nid, new_vels[nid])

    def _update_cooccurrence(self, result_ids: list[str]) -> None:
        """Update co-occurrence graph for LLM-returned results only.

        Co-occurrence is based on what the user/LLM actually "sees" together,
        not the full simulation reach.
        """
        if result_ids:
            self.graph.update_cooccurrence(result_ids)

    # --- US3: Node State Inspection ---

    async def get_node_state(self, node_id: str) -> NodeState | None:
        state = self.cache.get_node(node_id)
        if state is not None:
            return state
        states = await self.store.get_node_states([node_id])
        return states.get(node_id)

    def get_displacement_norm(self, node_id: str) -> float:
        disp = self.cache.get_displacement(node_id)
        if disp is None:
            return 0.0
        return float(np.linalg.norm(disp))

    # --- US4: Graph Inspection ---

    def get_graph(
        self,
        min_weight: float = 0.0,
        node_id: str | None = None,
    ) -> list[CooccurrenceEdge]:
        all_edges = self.cache.get_all_edges()
        filtered = []
        for edge in all_edges:
            if edge.weight < min_weight:
                continue
            if node_id is not None and node_id not in (edge.src, edge.dst):
                continue
            filtered.append(edge)
        return filtered

    # --- F5: Forget / Archive ---

    async def archive(self, node_ids: list[str]) -> int:
        """Soft-delete: mark nodes as archived. They are evicted from cache
        and excluded from recall/explore/reflect, but remain in the store
        and can be restored.
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        if not node_ids:
            return 0
        await self.cache.flush_to_store(self.store)
        affected = await self.store.set_archived(node_ids, archived=True)
        for nid in node_ids:
            self.cache.evict_node(nid)
        # Phase L Stage 1: drop archived ids from BM25 so search excludes
        # them immediately (the postings remain until compact/rebuild).
        # WP-6c: sync 経路と同じ条件・同じ対象 (hybrid のみ) で journal に
        # 記録してから現行 index に適用する。
        if self.bm25_index is not None and affected:
            await self._journal_bm25_mutation("remove", node_ids)
            self.bm25_index.remove(node_ids)
        if affected:
            self.prefetch_cache.invalidate()
        logger.info("Archived %d nodes", affected)
        return affected

    async def restore(self, node_ids: list[str]) -> int:
        """Un-archive nodes and reload them into the cache."""
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        if not node_ids:
            return 0
        affected = await self.store.set_archived(node_ids, archived=False)
        if affected:
            states = await self.store.get_node_states(node_ids)
            for state in states.values():
                state.is_archived = False
                self.cache.set_node(state, dirty=False)
            disps = await self.store.load_displacements(ids=node_ids)
            vels = await self.store.load_velocities(ids=node_ids)
            for nid, disp in disps.items():
                self.cache.set_displacement(nid, disp)
            for nid, vel in vels.items():
                self.cache.set_velocity(nid, vel)
            # Phase L Stage 1: BM25 also surfaces the restored docs again.
            # Calling restore is cheap (just flips the soft-remove flag);
            # if the postings were already compacted away, this is a no-op
            # and the next startup rebuild picks them up.
            # WP-6c: snapshot に無い doc (build 窓内で restore された
            # archived doc) は新 index に postings が無いので、journal が
            # content を取得して replay 時に add へ fallback する。
            if self.bm25_index is not None and affected:
                await self._journal_bm25_restore(node_ids)
                self.bm25_index.restore(node_ids)
            self.prefetch_cache.invalidate()
        logger.info("Restored %d nodes", affected)
        return affected

    async def forget(self, node_ids: list[str], hard: bool = False) -> int:
        """Forget nodes. hard=False archives them (reversible); hard=True
        physically removes them from the store. Vectors in the FAISS index
        are not removed (rebuild on next reset), but archived nodes are
        filtered out at query time.
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        if not node_ids:
            return 0
        if not hard:
            return await self.archive(node_ids)
        await self.cache.flush_to_store(self.store)
        for nid in node_ids:
            self.cache.evict_node(nid)
        deleted = await self.store.hard_delete_nodes(node_ids)
        # Phase L Stage 1: BM25 postings are reclaimed on the next compact;
        # for now just drop them from active statistics so search excludes
        # them.
        if self.bm25_index is not None and deleted:
            await self._journal_bm25_mutation("remove", node_ids)
            self.bm25_index.remove(node_ids)
        if deleted:
            self.prefetch_cache.invalidate()
        logger.info("Hard-deleted %d nodes", deleted)
        return deleted

    # --- F6: Background prefetch ---

    def prefetch(
        self,
        text: str,
        top_k: int | None = None,
        wave_depth: int | None = None,
        wave_k: int | None = None,
        persona_context: list[str] | None = None,
        tag_filter: list[str] | None = None,
    ) -> object:
        """Schedule a background recall and cache its result.

        Returns the asyncio.Task handle (mostly opaque to callers; tests can
        ``await`` it for determinism). The next ``query(text, top_k,
        use_cache=True)`` within ``prefetch_ttl_seconds`` will be served from
        the cache without re-running the simulation.

        Phase J Stage 3: `persona_context` / `tag_filter` are forwarded so
        the prefetched result matches what an explicit `recall(...)` with
        the same arguments would return. H6: the cache key is
        `(text, top_k, wave_depth, wave_k)`, so a prefetch only serves a
        recall issued with the *same* wave reach — a shallow prefetch can
        no longer poison a deep recall (or vice versa). `persona_context` /
        `tag_filter` still bypass the cache entirely on the read side, so
        they need not be in the key.
        """
        k = top_k or self.config.top_k

        async def _run() -> list[QueryResultItem]:
            results = await self._query_internal(
                text=text, top_k=k, wave_depth=wave_depth, wave_k=wave_k,
                persona_context=persona_context, tag_filter=tag_filter,
            )
            self.prefetch_cache.put(text, k, results, wave_depth, wave_k)
            return results

        return self.prefetch_pool.schedule(_run)

    def prefetch_status(self) -> dict:
        return {
            "cache": self.prefetch_cache.stats(),
            "pool": self.prefetch_pool.stats(),
        }

    # --- F3: Directed (typed) relations ---

    async def relate(
        self,
        src_id: str,
        dst_id: str,
        edge_type: str,
        weight: float = 1.0,
        metadata: dict | None = None,
    ) -> DirectedEdge:
        """Create (or replace) a directed typed edge from src to dst.

        Reserved edge types are documented in ``KNOWN_EDGE_TYPES``; the API
        does not enforce them so callers can experiment with new relations.
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        if src_id == dst_id:
            raise ValueError("Self-relations are not allowed")
        edge = DirectedEdge(
            src=src_id, dst=dst_id, edge_type=edge_type,
            weight=weight, created_at=time.time(), metadata=metadata,
        )
        await self.store.upsert_directed_edge(edge)
        # Phase J Stage 1: mirror into the in-memory cache so persona
        # traversal in the next recall sees the new edge without waiting
        # for a cache reload.
        self.cache.set_directed_edge(src_id, dst_id, edge_type)
        return edge

    async def unrelate(
        self, src_id: str, dst_id: str, edge_type: str | None = None,
    ) -> int:
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        deleted = await self.store.delete_directed_edge(src_id, dst_id, edge_type)
        if deleted > 0:
            self.cache.remove_directed_edge(src_id, dst_id, edge_type)
        return deleted

    async def get_relations(
        self,
        node_id: str,
        edge_type: str | None = None,
        direction: str = "out",
    ) -> list[DirectedEdge]:
        return await self.store.get_directed_edges(
            node_id=node_id, edge_type=edge_type, direction=direction,
        )

    # --- F7: Emotional weight & certainty ---

    async def revalidate(
        self,
        node_id: str,
        certainty: float | None = None,
        emotion: float | None = None,
    ) -> NodeState | None:
        """Stamp a node with fresh certainty/emotion (re-verification ritual).

        ``certainty`` updates last_verified_at; pass without value to just
        refresh the timestamp at the existing certainty level.
        Returns the updated state or None if the node doesn't exist.
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        state = self.cache.get_node(node_id)
        if state is None:
            states = await self.store.get_node_states([node_id])
            state = states.get(node_id)
        if state is None or state.is_archived:
            return None
        if certainty is not None:
            state.certainty = max(0.0, min(1.0, certainty))
        if emotion is not None:
            state.emotion_weight = max(-1.0, min(1.0, emotion))
        state.last_verified_at = time.time()
        self.cache.set_node(state, dirty=True)
        return state

    # --- F2 / F2.1: Clustering, Collision, Compaction ---

    def _virtual_position_for(self, node_id: str) -> np.ndarray | None:
        """Best-effort virtual position (original embedding + displacement)."""
        embs = self.faiss_index.get_vectors([node_id])
        original = embs.get(node_id)
        if original is None:
            return None
        state = self.cache.get_node(node_id)
        temperature = state.temperature if state else 0.0
        displacement = self.cache.get_displacement(node_id)
        return compute_virtual_position(original, displacement, temperature)

    def _active_virtual_positions(
        self, *, top_n_by_mass: int | None = None,
    ) -> dict[str, np.ndarray]:
        """Collect virtual positions for non-archived nodes.

        ``top_n_by_mass`` restricts to the heaviest N nodes (cheaper for large
        memories; matches the "hot topic neighborhood" heuristic in the plan).
        """
        nodes = [n for n in self.cache.get_all_nodes() if not n.is_archived]
        if top_n_by_mass is not None and len(nodes) > top_n_by_mass:
            nodes = sorted(nodes, key=lambda s: s.mass, reverse=True)[:top_n_by_mass]
        out: dict[str, np.ndarray] = {}
        for state in nodes:
            pos = self._virtual_position_for(state.id)
            if pos is not None:
                out[state.id] = pos
        return out

    def find_duplicates(
        self, *, threshold: float = 0.95, top_n_by_mass: int | None = 500,
    ) -> list[Cluster]:
        """Detect near-duplicate clusters among active memories."""
        positions = self._active_virtual_positions(top_n_by_mass=top_n_by_mass)
        return cluster_by_similarity(positions, threshold=threshold)

    async def merge(self, node_ids: list[str], keep: str | None = None) -> list[MergeOutcome]:
        """Manual collision: collapse the given IDs into one survivor.

        If ``keep`` is given, that ID survives. Otherwise the heaviest among
        ``node_ids`` is chosen (ties broken by recency).
        Returns one ``MergeOutcome`` per absorbed node.
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        unique_ids = list(dict.fromkeys(node_ids))
        if len(unique_ids) < 2:
            return []

        states_by_id: dict[str, NodeState] = {}
        for nid in unique_ids:
            state = self.cache.get_node(nid)
            if state is None:
                states = await self.store.get_node_states([nid])
                state = states.get(nid)
            if state is None or state.is_archived:
                continue
            states_by_id[nid] = state

        if len(states_by_id) < 2:
            return []

        if keep is not None and keep in states_by_id:
            survivor = states_by_id.pop(keep)
            keep_explicit = True
        else:
            ordered = sorted(
                states_by_id.values(),
                key=lambda s: (s.mass, s.last_access),
                reverse=True,
            )
            survivor = ordered[0]
            states_by_id.pop(survivor.id)
            keep_explicit = False

        outcomes: list[MergeOutcome] = []
        now = time.time()
        for absorbed in states_by_id.values():
            if not keep_explicit:
                # Auto-pick the heavier body so mass conservation feels right.
                # When the caller explicitly passed keep=, that intent wins —
                # otherwise Phase-G mass perturbations could silently override
                # the user's choice.
                survivor, _ = pick_survivor(survivor, absorbed)
                if survivor.id == absorbed.id:
                    survivor, absorbed = absorbed, survivor
            outcome = merge_pair(survivor, absorbed, self.cache, self.config, now=now)
            outcomes.append(outcome)
            # Evict absorbed from cache after marking dirty so flush persists state
            await self.cache.flush_to_store(self.store)
            self.cache.evict_node(absorbed.id)
            # Phase L Stage 1: drop absorbed from BM25 so the survivor wins
            # all lexical searches (the absorbed content is now redundant).
            if self.bm25_index is not None:
                await self._journal_bm25_mutation("remove", [absorbed.id])
                self.bm25_index.remove([absorbed.id])
        if outcomes:
            self.prefetch_cache.invalidate()
        return outcomes

    async def compact(
        self,
        *,
        expire_ttl: bool = True,
        rebuild_faiss: bool = True,
        auto_merge: bool = False,
        merge_threshold: float = 0.95,
        merge_top_n: int = 500,
    ) -> dict:
        """Periodic maintenance: TTL expiry, FAISS rebuild, optional auto-merge.

        Returns a dict report of what changed:
            {
              "expired": int,
              "merged_pairs": int,
              "faiss_rebuilt": bool,
              "vectors_before": int,
              "vectors_after": int,
            }
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        report = {
            "expired": 0,
            "merged_pairs": 0,
            "faiss_rebuilt": False,
            "vectors_before": self.faiss_index.size,
            "vectors_after": self.faiss_index.size,
        }

        if expire_ttl:
            now = time.time()
            n = await self.store.expire_due_nodes(now)
            expired_ids: list[str] = []
            for state in list(self.cache.get_all_nodes()):
                if state.expires_at is not None and state.expires_at <= now:
                    expired_ids.append(state.id)
                    self.cache.evict_node(state.id)
            # Phase L Stage 1: drop expired ids from BM25 active stats.
            if self.bm25_index is not None and expired_ids:
                await self._journal_bm25_mutation("remove", expired_ids)
                self.bm25_index.remove(expired_ids)
            report["expired"] = n

        if auto_merge:
            positions = self._active_virtual_positions(top_n_by_mass=merge_top_n)
            candidates = find_merge_candidates(positions, threshold=merge_threshold)
            done_pairs = 0
            absorbed_ids: set[str] = set()
            for a, b, _sim in candidates:
                if a in absorbed_ids or b in absorbed_ids:
                    continue
                outcomes = await self.merge([a, b])
                if outcomes:
                    done_pairs += len(outcomes)
                    for o in outcomes:
                        absorbed_ids.add(o.absorbed_id)
            report["merged_pairs"] = done_pairs

        if rebuild_faiss:
            await self._rebuild_faiss_index()
            report["faiss_rebuilt"] = True
            # Phase L Stage 1: rebuild BM25 together with FAISS so the
            # lexical index also reclaims postings from forget/merge/expire
            # and re-syncs with the SQLite content (covers any drift from
            # other processes that wrote to the DB while this one was idle).
            if self.bm25_index is not None:
                await self._rebuild_bm25_from_store()
        report["vectors_after"] = self.faiss_index.size

        # Drop orphan directed edges (endpoints hard-deleted by the user)
        all_relations = await self.store.get_directed_edges()
        valid_ids = {state.id for state in self.cache.get_all_nodes()}
        orphan_count = 0
        for edge in all_relations:
            if edge.src not in valid_ids and edge.dst not in valid_ids:
                await self.store.delete_directed_edge(edge.src, edge.dst, edge.edge_type)
                orphan_count += 1
        report["orphan_relations_removed"] = orphan_count

        await self.cache.flush_to_store(self.store)
        # Compaction may have changed scoring — invalidate prefetch cache
        self.prefetch_cache.invalidate()
        return report

    async def _rebuild_faiss_index(self) -> None:
        """Rebuild FAISS from all active cache nodes.

        Nodes already in FAISS have their vectors extracted directly.
        Nodes in cache but absent from FAISS (write-behind gap from a previous
        session that ended before the flush fired) are re-embedded from store
        so they are no longer invisible to recall.
        """
        active_ids = [
            state.id for state in self.cache.get_all_nodes() if not state.is_archived
        ]
        if not active_ids:
            self.faiss_index.reset()
            self._faiss_dirty = True
            return
        vecs = self.faiss_index.get_vectors(active_ids)
        present = [(nid, vecs[nid]) for nid in active_ids if nid in vecs]

        # Re-embed nodes that exist in cache/store but are absent from FAISS.
        missing_ids = [nid for nid in active_ids if nid not in vecs]
        recovered: list[tuple[str, np.ndarray]] = []
        if missing_ids:
            logger.info(
                "_rebuild_faiss_index: re-embedding %d nodes missing from FAISS",
                len(missing_ids),
            )
            contents: list[str] = []
            valid_ids: list[str] = []
            for nid in missing_ids:
                doc = await self.store.get_document(nid)
                if doc is not None:
                    contents.append(doc["content"])
                    valid_ids.append(nid)
            if contents:
                re_vecs = self.embedder.encode_documents(contents)
                for nid, vec in zip(valid_ids, re_vecs):
                    recovered.append((nid, vec))
            logger.info(
                "_rebuild_faiss_index: recovered %d/%d missing nodes",
                len(recovered),
                len(missing_ids),
            )

        all_pairs = present + recovered
        if not all_pairs:
            return
        matrix = np.stack([v for _, v in all_pairs]).astype(np.float32)
        # H1: build a fresh index, then swap the reference in a single
        # atomic assignment. The previous reset()+add() left a window where
        # self.faiss_index.ntotal == 0; a concurrent recall landing in that
        # window got an empty seed pool — a silent degraded result during a
        # routine compact. A lone attribute store is atomic under the GIL,
        # so no searcher observes a partial index: in-flight searches that
        # already captured the old reference finish against the old,
        # fully-valid index, and subsequent ones see the complete new one.
        new_index = FaissIndex(
            dimension=self.config.embedding_dim,
            lock_enabled=self.config.faiss_index_lock_enabled,
        )
        new_index.add(matrix, [nid for nid, _ in all_pairs])
        self.faiss_index = new_index
        self._faiss_dirty = True
        if self.virtual_faiss_index is not None:
            await self._rebuild_virtual_faiss_index()

    async def _rebuild_virtual_faiss_index(self) -> None:
        """Build the virtual FAISS index from raw embeddings + cached
        displacement (Phase H Stage 4).

        Uses ``compute_virtual_position`` for each active node so that
        Phase G priming (which moves displacement on every active node)
        becomes seedable. Without this index, raw FAISS top-K never sees
        priming-induced cluster shifts."""
        if self.virtual_faiss_index is None:
            return
        active_ids = [
            s.id for s in self.cache.get_all_nodes() if not s.is_archived
        ]
        if not active_ids:
            self.virtual_faiss_index.reset()
            return
        raw_vecs = self.faiss_index.get_vectors(active_ids)
        virtual_vectors: list[np.ndarray] = []
        virtual_ids: list[str] = []
        for nid in active_ids:
            original = raw_vecs.get(nid)
            if original is None:
                continue
            displacement = self.cache.get_displacement(nid)
            state = self.cache.get_node(nid)
            temperature = state.temperature if state is not None else 0.0
            virtual_pos = compute_virtual_position(
                original, displacement, temperature,
            )
            virtual_vectors.append(virtual_pos)
            virtual_ids.append(nid)
        if not virtual_vectors:
            self.virtual_faiss_index.reset()
            return
        matrix = np.stack(virtual_vectors).astype(np.float32)
        # H1: atomic swap, same rationale as _rebuild_faiss_index — no
        # concurrent seed step ever sees an empty virtual index mid-compact.
        new_virtual = FaissIndex(
            dimension=self.config.embedding_dim,
            lock_enabled=self.config.faiss_index_lock_enabled,
        )
        new_virtual.add(matrix, virtual_ids)
        self.virtual_faiss_index = new_virtual
        logger.info(
            "Virtual FAISS rebuilt: %d active vectors", len(virtual_ids),
        )

    async def _bm25_active_snapshot(
        self,
    ) -> tuple[list[str], list[str], int, str, int]:
        """store + cache から現時点の active document 一覧を 1 回で読む。

        ``_build_bm25_from_store`` と同一規則 (cache.node_cache に載って
        いる = archived/expired は除外、空 content は除外)。WP-6c の
        background build は task 冒頭でこれを 1 回だけ呼び、以降の構築は
        この stable snapshot に対して行う (構築中の cache 変化を拾わない
        — その役割は journal)。3 番目の戻り値は全 contents 件数
        (sync 経路の log 用)。

        WP-6d: 4/5 番目の戻り値は corpus fingerprint (digest, active_count)。
        digest = sorted な (id, sha256(content)) 列を順に連結したものの
        sha256 — content そのものの digest なので、timestamp 系の proxy
        (count + max(updated_at) 等) と違い in-place content 変更を
        取りこぼさない (WP-3 と同じ Codex review 仕様)。fingerprint は
        この pass と同じ 1 回の store scan で計算する (追加 scan なし)。
        id 順に iterate するので SQLite の row order に依存せず、build も
        この順で add する — loaded index と freshly-built index の検索結果
        (tie-break の insertion order 含む) が一致する要件の一部。
        """
        contents = await self.store.get_all_contents()
        active_ids: list[str] = []
        active_texts: list[str] = []
        fp = hashlib.sha256()
        for nid, text in sorted(contents.items()):
            if nid in self.cache.node_cache and text:
                active_ids.append(nid)
                active_texts.append(text)
                fp.update(nid.encode("utf-8"))
                fp.update(b"\x00")
                fp.update(hashlib.sha256(text.encode("utf-8")).digest())
                fp.update(b"\x00")
        return (
            active_ids, active_texts, len(contents), fp.hexdigest(),
            len(active_ids),
        )

    def _fill_bm25_indexes(
        self,
        bm25_index: BM25Index | None,
        ambient_gate_index: BM25Index | None,
        active_ids: list[str],
        active_texts: list[str],
    ) -> int:
        """BM25 index への doc 追加 — sync / background 両経路の共有 helper。

        sync 経路 (rollback flag) は startup から直接呼ぶ。background 経路
        (WP-6c) は asyncio.to_thread から chunk 単位で呼ぶ — tokenization は
        CPU-bound なので event loop を block しない。呼び出し側の lock 管理
        (engine 側) に対し、index object 自体は常に単一 owner でのみ触られる
        (build 中は新 object が build task 専有、swap 後は mutation 経路専有)。
        """
        if bm25_index is not None:
            bm25_index.add(active_ids, active_texts)
        # Ambient Recall Enrichment: the word-level gate index is built from
        # the same content scan. Sudachi tokenisation is slower than the
        # char-trigram default, so this adds to startup time on a large corpus.
        if ambient_gate_index is not None:
            ambient_gate_index.add(active_ids, active_texts)
        return len(active_ids)

    async def _build_bm25_from_store(self) -> tuple[str, int] | None:
        """Phase L Stage 1: Initial BM25 build at startup (sync path).

        Loads every document content from SQLite and adds the active ones
        (those present in ``cache.node_cache`` — archived/expired ids are
        skipped) to the in-memory BM25 index. Decision D2 dictated that
        Stage 1 has no disk persistence; WP-6d supersedes it with the
        fingerprint-guarded snapshot (callers decide whether to persist).

        WP-6d: build した内容の corpus fingerprint (digest, active_count)
        を返す — 呼び出し側が「build 成功時に snapshot を保存」する際の
        内容保証に使う。index が未接続・active doc が 0 件の場合は None
        (snapshot に意味が無い / 次 boot の build は自明に速い)。
        """
        if self.bm25_index is None and self.ambient_gate_index is None:
            return None
        active_ids, active_texts, total, digest, active_count = (
            await self._bm25_active_snapshot()
        )
        if not active_ids:
            return None
        self._fill_bm25_indexes(
            self.bm25_index, self.ambient_gate_index, active_ids, active_texts,
        )
        logger.info(
            "BM25 index built: %d active docs (skipped %d archived/missing)",
            len(active_ids), total - len(active_ids),
        )
        return digest, active_count

    # --- WP-6c (Phase U / R5): background BM25 build -----------------------
    # 「新規 index object への snapshot build + build 窓内 mutation の
    # journal replay + engine lock 下での atomic swap」。現行 index object
    # への in-place 書き込みは行わない (BM25Index は lock を持たない
    # single-owner 設計のため、並行 writer を作らない)。

    async def _journal_bm25_mutation(
        self,
        op: str,
        ids: list[str],
        texts: list[str] | None = None,
        *,
        gate_too: bool = False,
    ) -> None:
        """BM25-affecting mutation を journal に記録する (build 中のみ)。

        契約: mutation 経路は「本メソッドの await」→「現行 index への適用」
        の順で呼び、両者の間に await を入れない。これで journal 追加と
        現行 index への適用が同じ世代に対して行われ、swap をまたいだ
        mutation の取りこぼしが構造的に起きない。
        """
        # WP-6d: journal の有無にかかわらず、mutation は現行 index にも
        # 適用される = on-disk snapshot はもう現状を映していない。build
        # 完了時の条件付き保存は dirty で skip され、graceful shutdown 時
        # に fingerprint を取り直して再保存される (mutation ごとの再保存
        # は write amplification なので行わない)。remove は sync 経路の
        # 契約どおり hybrid のみに適用されるため、gate は store-active
        # 構成から乖離する (diverged — compact の full rebuild で再収束)。
        self._bm25_snapshot_dirty = True
        if op == "remove":
            self._bm25_snapshot_gate_diverged = True
        if self._bm25_journal is None or self._bm25_journal_lock is None:
            return
        async with self._bm25_journal_lock:
            # lock 取得待ちの間に swap が journal を閉じ得る — 閉じていたら
            # 記録不要 (直後の現行 index への適用が swap 済みの新 object に
            # 当たるため、mutation は失われない)
            if self._bm25_journal is None:
                return
            self._bm25_journal.append(
                _BM25JournalEntry(
                    op=op, ids=list(ids), texts=texts, gate_too=gate_too,
                )
            )
            self._bm25_mutation_generation += 1

    async def _journal_bm25_restore(self, node_ids: list[str]) -> None:
        """restore 用の journal 記録 (content 取得付き)。

        snapshot に無い doc への restore は新 index に postings が無い
        ため、replay 時に add へ fallback できるよう content を journal
        時点で取得しておく。build 中のみ store 読み込みが発生する。
        """
        if self._bm25_journal is None or self._bm25_journal_lock is None:
            return
        ids: list[str] = []
        texts: list[str] = []
        for nid in node_ids:
            doc = await self.store.get_document(nid)
            if doc is not None and doc.get("content"):
                ids.append(nid)
                texts.append(doc["content"])
        await self._journal_bm25_mutation("restore", ids, texts)

    def _wire_fresh_bm25_indexes(
        self,
    ) -> tuple[BM25Index | None, BM25Index | None]:
        """background build 用の新規 index 対。

        runtime.build_engine が wiring に使うのと同一 param で生成する
        (hybrid: k1/b/tokenizer、gate: gate tokenizer + default k1/b)。
        既存 index と同一 param なので、swap 後の検索挙動は同期 build と
        同一になる。
        """
        new_hybrid = (
            BM25Index(
                k1=self.config.bm25_k1,
                b=self.config.bm25_b,
                tokenizer=self.config.bm25_tokenizer,
            )
            if self.bm25_index is not None
            else None
        )
        new_gate = (
            BM25Index(tokenizer=self.config.ambient_gate_tokenizer)
            if self.ambient_gate_index is not None
            else None
        )
        return new_hybrid, new_gate

    @staticmethod
    def _replay_bm25_journal_entry(
        entry: _BM25JournalEntry,
        new_hybrid: BM25Index | None,
        new_gate: BM25Index | None,
        present: set[str],
    ) -> None:
        """journal entry を新 index に 1 件 replay (sync、lock 内で呼ぶ)。

        ``present`` は新 index に postings が存在する id 集 (snapshot +
        add 済み id)。restore の add-fallback 判定に使う (BM25Index は
        contains 持ちの API を持たないため engine 側で追跡)。
        add/remove は BM25Index 側で冪等 (dup add skip / 未知 id の
        remove・restore は no-op)。
        """
        if entry.op == "add":
            if entry.texts is not None:
                if new_hybrid is not None:
                    new_hybrid.add(entry.ids, entry.texts)
                if entry.gate_too and new_gate is not None:
                    new_gate.add(entry.ids, entry.texts)
            present.update(entry.ids)
        elif entry.op == "remove":
            # sync 経路と同じく hybrid のみ (gate は compact rebuild まで
            # removed doc を保持する現行挙動を維持)
            if new_hybrid is not None:
                new_hybrid.remove(entry.ids)
        elif entry.op == "restore":
            if new_hybrid is not None and entry.texts is not None:
                have: list[str] = []
                miss_ids: list[str] = []
                miss_texts: list[str] = []
                for nid, text in zip(entry.ids, entry.texts):
                    if nid in present:
                        have.append(nid)
                    elif text:
                        miss_ids.append(nid)
                        miss_texts.append(text)
                if have:
                    new_hybrid.restore(have)
                if miss_ids:
                    # snapshot に無かった doc — postings が無いので content
                    # から add する (sync 経路では起動 build が必ず含めて
                    # いた状態に相当)
                    new_hybrid.add(miss_ids, miss_texts)
                    present.update(miss_ids)

    def _start_bm25_background_build(self) -> None:
        """background build task を起動する (startup から呼ぶ)。

        journal を **先に** 開いてから snapshot を取る — snapshot 読み込み
        中の mutation も journal に入り、replay の冪等性 (dup add skip /
        remove 冪等) が二重適用を吸収する。WP-6d: snapshot load 試行で
        既に journal が開いている場合はそのまま再利用する (load 試行中の
        mutation 記録を失わない — entries は build の replay が消費する)。
        """
        if self._bm25_journal is None or self._bm25_journal_lock is None:
            self._bm25_journal_lock = asyncio.Lock()
            self._bm25_journal = []
        self._bm25_bg_invalidated = False
        self.bm25_build_attempts = 0
        self.bm25_build_state = "building"
        self._bm25_build_task = asyncio.create_task(
            self._bm25_background_build(), name="bm25-background-build",
        )

    async def _bm25_background_build(self) -> None:
        """WP-6c background build 本体。

        1 attempt = snapshot 取得 → thread での chunk fill → journal
        replay + atomic swap (1 つの lock 区間、await なしの sync block)。
        例外は single automatic retry (計 2 attempt)、それでも失敗したら
        state="failed" で give up — engine は落とさない。cancel
        (shutdown) は現行 index を空のまま残して即座に終了する。
        """
        t0 = time.perf_counter()
        max_attempts = 2  # 初回 + single retry (WP-6c retry 契約)
        try:
            for attempt in range(1, max_attempts + 1):
                self.bm25_build_attempts = attempt
                if self._bm25_bg_invalidated:
                    # compact が現行 index を再構築済み — 新 object は
                    # 古い snapshot 基底なので swap しない
                    break
                try:
                    new_hybrid, new_gate = self._wire_fresh_bm25_indexes()
                    active_ids, active_texts, _total, fp_digest, fp_count = (
                        await self._bm25_active_snapshot()
                    )
                    for i in range(0, len(active_ids), _BM25_BG_FILL_CHUNK):
                        await asyncio.to_thread(
                            self._fill_bm25_indexes,
                            new_hybrid,
                            new_gate,
                            active_ids[i:i + _BM25_BG_FILL_CHUNK],
                            active_texts[i:i + _BM25_BG_FILL_CHUNK],
                        )
                    # --- journal replay + atomic swap ---
                    # lock 区間は await なしの sync block: mutation の
                    # journal append と完全に直列化され、「replay 済み
                    # journal に append されたが swap 前の index に適用
                    # された」という取りこぼしが構造的に起きない。
                    async with self._bm25_journal_lock:
                        if self._bm25_bg_invalidated:
                            break
                        present = set(active_ids)
                        for entry in list(self._bm25_journal):
                            self._replay_bm25_journal_entry(
                                entry, new_hybrid, new_gate, present,
                            )
                        # search 経路はすべて ``self.bm25_index`` を
                        # search ごとに読むため、参照の差し替えだけで
                        # 新検索から新 object が見える (実行中の search は
                        # 旧 object 上で完結する)。2 index は同時に swap。
                        if new_hybrid is not None:
                            self.bm25_index = new_hybrid
                        if new_gate is not None:
                            self.ambient_gate_index = new_gate
                        journal_len = len(self._bm25_journal)
                        self._bm25_journal = None
                    self.bm25_build_state = "ready"
                    # WP-6a 計装契約の documented exception: 完了時に実際の
                    # 所要時間で上書きする (startup 時点では ≈0 を記録済み)
                    self.startup_timings["bm25_build"] = time.perf_counter() - t0
                    logger.info(
                        "BM25 background build ready: hybrid=%s gate=%s "
                        "(%d journal entries replayed, mutation_generation=%d, "
                        "%.2fs)",
                        new_hybrid.size if new_hybrid is not None else None,
                        new_gate.size if new_gate is not None else None,
                        journal_len,
                        self._bm25_mutation_generation,
                        self.startup_timings["bm25_build"],
                    )
                    # WP-6d: build 成功時に snapshot を保存。fp はこの
                    # attempt の snapshot pass 由来 — 新 index が表現する
                    # 内容と正確に対応する。窓内 mutation があった
                    # (dirty / gate diverged) 場合は保存を skip し、
                    # graceful shutdown 時の再保存 or 次 boot の再 build に
                    # 任せる (fp が内容を正確に記述している保証を優先)。
                    await self._save_bm25_snapshot_if_clean(
                        (fp_digest, fp_count),
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if attempt < max_attempts:
                        logger.error(
                            "BM25 background build failed (attempt %d/%d) — "
                            "retrying: %s: %s",
                            attempt, max_attempts, type(exc).__name__, exc,
                        )
                        continue
                    logger.error(
                        "BM25 background build failed (attempt %d/%d) — giving "
                        "up; BM25 indexes stay empty. Hybrid retrieval falls "
                        "back to raw+virtual; the ambient gate falls back to "
                        "the semantic path. Recover: restart the backend or "
                        "run compact(rebuild_faiss=True).",
                        attempt, max_attempts,
                    )
                    self.bm25_build_state = "failed"
                    self.startup_timings["bm25_build"] = time.perf_counter() - t0
                    async with self._bm25_journal_lock:
                        self._bm25_journal = None
                    return
            # invalidate 経由の到達 — compact が現行 index を再構築済み
            # なので現行のままで ready とする
            self.bm25_build_state = "ready"
            self.startup_timings["bm25_build"] = time.perf_counter() - t0
            async with self._bm25_journal_lock:
                self._bm25_journal = None
            logger.info(
                "BM25 background build discarded (invalidated by compact "
                "rebuild) — compact-built indexes remain active",
            )
        except asyncio.CancelledError:
            # shutdown 中の cancel: swap せずに終了する。state は "idle" に
            # 戻す ("failed" と違い、WP-6b が build 未達と誤読しない)。
            self.bm25_build_state = "idle"
            async with self._bm25_journal_lock:
                self._bm25_journal = None
            raise

    async def _rebuild_bm25_from_store(self) -> None:
        """Phase L Stage 1: Full BM25 rebuild during compact.

        Drops in-memory state and rebuilds from SQLite — reclaims postings
        from forget/merge/expire and re-syncs with content written by other
        processes (the multi-process visibility caveat in CLAUDE.md).
        """
        if self.bm25_index is None and self.ambient_gate_index is None:
            return
        # WP-6c: build 窓内で compact が走った場合、この再構築 (store の
        # 現在内容からの full rebuild) の方が background build の snapshot
        # より新しい。in-flight build を invalidation して swap を止める
        # (他プロセス書き込み含め compact 時点の store が勝つ)。
        if self._bm25_journal is not None:
            self._bm25_bg_invalidated = True
        # WP-6d: full rebuild で両 index は store-active 構成に再収束する
        # (remove 系 mutation で生じていた gate の divergence を解消)。
        # 内容が前回保存した snapshot と同じとは限らないので dirty を
        # 立て、graceful shutdown 時に fingerprint を取り直して再保存する。
        self._bm25_snapshot_dirty = True
        self._bm25_snapshot_gate_diverged = False
        if self.bm25_index is not None:
            self.bm25_index.reset()
        if self.ambient_gate_index is not None:
            self.ambient_gate_index.reset()
        await self._build_bm25_from_store()

    # --- WP-6d (Phase U / R5): BM25 snapshot persistence ---------------------
    # 「build 済み index の data_dir/bm25.snapshot への永続化 + 次回 startup
    # での fingerprint 検証付き load」。保存は build 完了時と graceful
    # shutdown 時 (dirty) のみ。load は WP-6c の swap 機構と同じ journal
    # replay + 参照差し替えで行う (= load も「build 完了」の一種)。

    def _bm25_snapshot_identity(self) -> dict:
        """WP-6d — snapshot の tokenizer identity (検証対象)。

        k1 / b / tokenizer 名は BM25Index の内容と scoring を完全に定義
        する parameter なので、この組 (と wiring 有無 = None) が一致すれば
        load した index は現在の config で build したものと同一になる。
        hybrid は config 値、gate は BM25Index default を wiring に使う
        現行構成に合わせ、実際に wired された index object から読む。
        """
        return {
            "hybrid": (
                {
                    "tokenizer": self.config.bm25_tokenizer,
                    "k1": self.bm25_index.k1,
                    "b": self.bm25_index.b,
                }
                if self.bm25_index is not None
                else None
            ),
            "gate": (
                {
                    "tokenizer": self.config.ambient_gate_tokenizer,
                    "k1": self.ambient_gate_index.k1,
                    "b": self.ambient_gate_index.b,
                }
                if self.ambient_gate_index is not None
                else None
            ),
        }

    async def _close_bm25_journal(self) -> None:
        """journal を閉じる (replay 消費先が無い経路での leak 防止)。

        WP-6d の load 試行後に sync build へ fallthrough する場合など、
        journal が開いたまま consumer を持たない経路で mutation 記録が
        無限に溜るのを防ぐ。以降の mutation は journal 無し (= sync 経路
        と同じ、現行 index への直接適用) で動く。
        """
        if self._bm25_journal_lock is None:
            return
        async with self._bm25_journal_lock:
            self._bm25_journal = None

    async def _try_load_bm25_snapshot(self) -> bool:
        """WP-6d — snapshot が新鮮なら build を skip して両 index を load。

        WP-6c と同じ ordering 契約 (journal を開いてから fingerprint pass)
        に従う: pass 中の mutation は journal に記録され、load 成功時に
        background build と同じ要領で replay + swap される (mid-startup
        mutation の取りこぼし構造的に無い)。検証は checksum (file 読み
        込み時) → format_version → universe_id → tokenizer identity →
        corpus fingerprint の順。いずれかが失敗したら False を返し、
        呼び出し側は通常の build 経路に fallback する。journal は開いた
        まま残す (background build が再利用 / sync 経路は呼び出し側で
        閉じる)。
        """
        if self._bm25_journal is None or self._bm25_journal_lock is None:
            self._bm25_journal_lock = asyncio.Lock()
            self._bm25_journal = []
        _ids, _texts, _total, digest, active_count = (
            await self._bm25_active_snapshot()
        )
        path = Path(self.config.data_dir) / _BM25_SNAPSHOT_FILENAME
        payload = _bm25_snapshot_read(path)
        if payload is None:
            logger.info(
                "BM25 snapshot absent/corrupt/format-mismatch — rebuilding (%s)",
                path,
            )
            return False
        if payload.get("universe_id") != self._universe_id:
            logger.info(
                "BM25 snapshot universe mismatch (snapshot=%r, engine=%r) — "
                "rebuilding",
                payload.get("universe_id"), self._universe_id,
            )
            return False
        identity = self._bm25_snapshot_identity()
        if payload.get("tokenizer_identity") != identity:
            logger.info(
                "BM25 snapshot tokenizer/params mismatch — rebuilding "
                "(snapshot=%r, current=%r)",
                payload.get("tokenizer_identity"), identity,
            )
            return False
        fp = payload.get("corpus_fingerprint")
        if (
            not isinstance(fp, dict)
            or fp.get("digest") != digest
            or fp.get("active_count") != active_count
        ):
            logger.info(
                "BM25 snapshot corpus fingerprint mismatch (content changed) "
                "— rebuilding"
            )
            return False
        hybrid_state = payload.get("hybrid_state")
        gate_state = payload.get("gate_state")
        # identity 検証済みなので state の有無 (= wiring 有無) は現在の
        # engine と一致している。tokenizer は identity の名前から再生成。
        new_hybrid = (
            _bm25_index_from_state(
                hybrid_state, identity["hybrid"]["tokenizer"],
            )
            if hybrid_state is not None
            else None
        )
        new_gate = (
            _bm25_index_from_state(gate_state, identity["gate"]["tokenizer"])
            if gate_state is not None
            else None
        )
        # --- journal replay + atomic swap (build 完了時と同一構造) ---
        async with self._bm25_journal_lock:
            entries = list(self._bm25_journal)
            had_entries = bool(entries)
            present: set[str] = (
                set(hybrid_state["doc_ids"]) if hybrid_state is not None else set()
            )
            for entry in entries:
                self._replay_bm25_journal_entry(
                    entry, new_hybrid, new_gate, present,
                )
            if new_hybrid is not None:
                self.bm25_index = new_hybrid
            if new_gate is not None:
                self.ambient_gate_index = new_gate
            self._bm25_journal = None
        # journal が空だった場合、load した index は on-disk snapshot の内容
        # そのもの — dirty / diverged を初期化する。窓内に mutation があった
        # (entries が replay された) 場合は on-disk 側にその内容が無いので
        # dirty は立てたままにし、graceful shutdown 時の再保存に任せる。
        if not had_entries:
            self._bm25_snapshot_dirty = False
            self._bm25_snapshot_gate_diverged = False
        logger.info(
            "BM25 snapshot loaded: hybrid=%s gate=%s (fingerprint=%s…, %d docs)",
            new_hybrid.size if new_hybrid is not None else None,
            new_gate.size if new_gate is not None else None,
            digest[:12], active_count,
        )
        return True

    async def _save_bm25_snapshot_if_clean(
        self, fp: tuple[str, int] | None,
    ) -> None:
        """WP-6d — build 完了時の条件付き snapshot 保存。

        保存してよいのは「現行 index が fp の内容を正確に表現している」
        場合のみ: build 窓内に mutation があった (dirty) / remove 系 mutation
        で gate が store-active 構成から乖離している (diverged) 場合は
        skip し、graceful shutdown 時の再保存 or 次 boot の再 build に任せ
        る。skip しても snapshot が stale/absent になるだけで、次 boot の
        fingerprint 検証が必ず再 build に落とすので安全側に倒れている。
        """
        if not self.config.bm25_snapshot_enabled or fp is None:
            return
        if self._bm25_snapshot_dirty or self._bm25_snapshot_gate_diverged:
            return
        await self._publish_bm25_snapshot(fp[0], fp[1])

    async def _publish_bm25_snapshot(
        self, digest: str, active_count: int,
    ) -> bool:
        """checksum 付き atomic publish (共通下請け)。

        ``_persist_blocked`` (owner-lease loss) 下では書かない (INFO 1 回
        のみ)。size 整合検証: hybrid / gate とも現行 index の active 件数が
        fingerprint の active_count と一致すること — restore で postings が
        無く index に載らなかった doc (index ⊊ store) 等、内容を正確に
        表現していない状態では保存しない。
        """
        if self._persist_blocked:
            if not self._bm25_snapshot_block_warned:
                self._bm25_snapshot_block_warned = True
                logger.info(
                    "BM25 snapshot save skipped — persist blocked (lease "
                    "lost); snapshot will be rebuilt on next boot",
                )
            return False
        if self.bm25_index is not None and self.bm25_index.size != active_count:
            logger.info(
                "BM25 snapshot save skipped — hybrid size %d != fingerprint "
                "active_count %d",
                self.bm25_index.size, active_count,
            )
            return False
        if (
            self.ambient_gate_index is not None
            and self.ambient_gate_index.size != active_count
        ):
            logger.info(
                "BM25 snapshot save skipped — gate size %d != fingerprint "
                "active_count %d",
                self.ambient_gate_index.size, active_count,
            )
            return False
        # payload は現 index から同期的に copy してから thread に渡す
        # (write 中の mutation が payload を動かさないようにするため)
        payload = {
            "format_version": _BM25_SNAPSHOT_FORMAT_VERSION,
            "universe_id": self._universe_id,
            "tokenizer_identity": self._bm25_snapshot_identity(),
            "corpus_fingerprint": {
                "digest": digest,
                "active_count": active_count,
            },
            "created_at": time.time(),
            "hybrid_state": (
                _bm25_index_state(self.bm25_index)
                if self.bm25_index is not None
                else None
            ),
            "gate_state": (
                _bm25_index_state(self.ambient_gate_index)
                if self.ambient_gate_index is not None
                else None
            ),
        }
        path = Path(self.config.data_dir) / _BM25_SNAPSHOT_FILENAME
        ok = await asyncio.to_thread(_bm25_snapshot_write, path, payload)
        if ok:
            logger.info(
                "BM25 snapshot saved: %d docs → %s", active_count, path,
            )
        return ok

    async def _save_bm25_snapshot_on_shutdown(self) -> None:
        """WP-6d — graceful shutdown 時の dirty snapshot 再保存。

        ``cache.flush_to_store`` の後で呼ぶこと (fingerprint pass が store
        の最終状態を読む)。build が in flight (cancel timeout で worker
        thread が残る例外的経路) の場合や state が ready でない場合は
        現行 index の内容を信用しない — skip しても stale snapshot は
        次 boot で再 build されるだけ。書き込み中に mutation が重なる
        可能性が残るため dirty はクリアしない (プロセスはここで終了)。
        """
        if not self.config.bm25_snapshot_enabled or not self._bm25_snapshot_dirty:
            return
        if self._bm25_build_task is not None and not self._bm25_build_task.done():
            logger.info(
                "BM25 snapshot save skipped on shutdown — build still in "
                "flight; snapshot will be rebuilt on next boot",
            )
            return
        if self.bm25_build_state != "ready":
            return
        if self._bm25_snapshot_gate_diverged:
            logger.info(
                "BM25 snapshot save skipped on shutdown — ambient gate "
                "diverged from store-active docs (remove mutations); "
                "rebuilding on next boot",
            )
            return
        _ids, _texts, _total, digest, active_count = (
            await self._bm25_active_snapshot()
        )
        await self._publish_bm25_snapshot(digest, active_count)

    # --- Phase M Stage 1: orbital-state reset (legacy BH residue cleanup) ---

    async def reset_orbital_state(self) -> int:
        """Clear displacement + velocity for every node — wipes the
        runtime residue of the legacy co-occurrence BH (which pulled
        nodes toward neighbor centroids before Phase M replaced it with
        the mass-threshold BH). Mass is **not** touched (see
        ``reset_masses`` for that).

        Destructive: also loses Phase G genesis kicks and Phase I/J
        query-attraction accumulation. The maintainer migration path for
        rolling Phase M out on a DB that ran under the old physics; not
        a runtime operation.

        Flushes any pending cache writes first, performs the SQL update,
        then clears the in-memory caches and invalidates the prefetch
        cache (displacement change invalidates every cached recall) plus
        marks virtual FAISS dirty so the next save loop rebuilds it from
        the now-zero displacement state.
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        await self.cache.flush_to_store(self.store)
        affected = await self.store.reset_orbital_state()
        self.cache.displacement_cache.clear()
        self.cache.velocity_cache.clear()
        self.cache.dirty_displacements.clear()
        self.cache.dirty_velocities.clear()
        self.cache.virtual_faiss_dirty = True
        self.prefetch_cache.invalidate()
        logger.info(
            "Orbital state reset: %d nodes cleared (displacement + velocity)",
            affected,
        )
        return affected

    # --- Phase Q2: velocity-only cooldown ---

    async def reset_velocities(self) -> int:
        """Clear every node's velocity, keeping displacement intact.

        Phase Q2 one-time cooldown of the degenerate, clamp-saturated momentum
        field (the pre-Q2 over-scaled neighbour gravity pushed ~99% of nodes'
        velocity to the clamp during recalls; that momentum lost its EMA
        meaning). Displacement — the learned positions / query-attraction
        integral — is preserved, unlike ``reset_orbital_state`` which wipes
        both. Velocity is a regenerable derivative of the field, so the
        (rescaled) gravity re-derives a correct, small velocity the next time
        a node participates in dynamics.

        Does NOT invalidate the prefetch cache or mark virtual FAISS dirty:
        velocity affects only future mutation, never the current ranking
        (which reads displacement) or the virtual-FAISS positions.

        Flushes pending cache writes first, performs the SQL update, then
        clears the in-memory velocity cache + dirty set.
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        await self.cache.flush_to_store(self.store)
        affected = await self.store.reset_velocities()
        self.cache.velocity_cache.clear()
        self.cache.dirty_velocities.clear()
        logger.info("Velocity cooldown: %d nodes cleared (velocity only)", affected)
        return affected

    # --- Phase M Stage 1: Mass-only reset ---

    async def reset_masses(self, value: float = 1.0) -> int:
        """Reset every node's mass to ``value`` (default 1.0), keeping
        displacement / velocity / edges / cohort_id / source intact.

        This is the maintainer hook for rolling out Phase M Stage 1 on a
        live database that accumulated mass under the old "internal trade"
        rule: switch the flag on, kill other connected processes, run
        ``reset_masses()``, restart. The new rule then accretes mass from
        a clean baseline.

        Flushes any pending cache writes first so they don't clobber the
        reset, performs the SQL update, then mirrors the new value into the
        in-memory cache and invalidates the prefetch cache (mass change
        invalidates every cached recall ranking).
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        await self.cache.flush_to_store(self.store)
        affected = await self.store.reset_masses(value)
        for state in self.cache.node_cache.values():
            state.mass = value
        self.prefetch_cache.invalidate()
        logger.info("Mass reset: %d nodes set to mass=%s", affected, value)
        return affected

    # --- Phase M follow-up: warm displacement from velocity ---

    async def warm_displacement(
        self, overwrite: bool = False,
    ) -> dict[str, int]:
        """Seed ``displacement = velocity`` (one orbital timestep) on every
        active node that has velocity but no meaningful displacement.

        Why this exists: M004 (corpus-scale cosmic-bang) writes velocity to
        every active node but leaves displacement NULL by design — the
        dream loop and natural recall events were supposed to fill it in
        over time. In practice that takes ~20 hours of continuous uptime
        for a 24k-node corpus and stalls across server restarts, so most
        nodes sit at ``velocity ≠ 0`` / ``displacement = NULL`` for days
        and visualisations show "velocity arrows but the position never
        moves". This one-shot pass takes the same step the dream loop
        would have taken on its first visit (``new_disp = old_disp +
        new_vel`` with ``old_disp = 0``) and applies it everywhere at
        once.

        Default (``overwrite=False``) leaves nodes that already have a
        non-zero displacement alone, so naturally-accumulated history
        (Phase G genesis kicks, Phase I/J query attraction, dream loop
        ticks since M004) is preserved. ``overwrite=True`` forces
        ``displacement = velocity`` on every active node with velocity
        — useful immediately after a fresh M002/M004 cycle.

        Returns a dict ``{seeded, skipped_no_velocity, skipped_already_displaced,
        active_total}`` so callers can verify how the corpus shifted.
        """
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        tol = 1e-6
        seeded = 0
        skipped_no_velocity = 0
        skipped_already_displaced = 0
        active_total = 0
        for state in self.cache.get_all_nodes():
            if state.is_archived:
                continue
            active_total += 1
            v = self.cache.get_velocity(state.id)
            if v is None or float(np.linalg.norm(v)) < tol:
                skipped_no_velocity += 1
                continue
            if not overwrite:
                d = self.cache.get_displacement(state.id)
                if d is not None and float(np.linalg.norm(d)) >= tol:
                    skipped_already_displaced += 1
                    continue
            self.cache.set_displacement(state.id, v.astype(np.float32).copy())
            seeded += 1

        if seeded > 0:
            await self.cache.flush_to_store(self.store)
            self.prefetch_cache.invalidate()
            logger.info(
                "Warm displacement: seeded %d / %d active nodes "
                "(skipped %d no-velocity, %d already-displaced)",
                seeded, active_total,
                skipped_no_velocity, skipped_already_displaced,
            )
        return {
            "seeded": seeded,
            "skipped_no_velocity": skipped_no_velocity,
            "skipped_already_displaced": skipped_already_displaced,
            "active_total": active_total,
        }

    # --- US5: State Reset ---

    async def reset(self) -> tuple[int, int]:
        if self._persist_blocked:
            raise LeaseLostError(
                "Engine is read-only: the write lease was lost to another "
                "process. Reconnect via the current owner to resume writes."
            )
        nodes_count = len(self.cache.node_cache)
        edges_count = len(self.cache.get_all_edges())

        self.cache.reset()
        self.graph.reset()
        nodes_reset, edges_removed = await self.store.reset_dynamic_state()

        # Hardening Stage 1 / C4 — every other destructive op invalidates
        # the prefetch cache (forget/merge/compact/reset_masses/
        # warm_displacement); reset() was the sole omission. Without this a
        # `recall` matching a cached (text, k) key keeps returning the
        # pre-reset ranked list for up to prefetch_ttl_seconds, so the wipe
        # silently appears not to have taken effect. Also mark the virtual
        # FAISS dirty for parity with reset_orbital_state — the displacement
        # field that fed it is now gone.
        self.prefetch_cache.invalidate()
        self.cache.virtual_faiss_dirty = True

        logger.info("Reset: %d nodes, %d edges removed", nodes_reset, edges_removed)
        return nodes_count, edges_count
