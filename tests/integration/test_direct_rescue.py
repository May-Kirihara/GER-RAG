"""Phase U WP-4b — raw-top rescue (integration, engine.query).

docs/notes/phase-u/wp4-trace-findings.md の病理再現 fixture:
target T は query と近重複語彙 (StubEmbedder raw cosine で pool 内
rank 1) だが、genesis-kick 発散の代理として query と直交する方向へ
displacement を注入され virtual cosine が低下 → final_score は
qualified pool 内で最下位帯になる。Phase T Stage 3 (qualified-first
stable partition) は qualification を順位信号にしないため T は
top-K から消える。raw-top rescue は qualified ∧ raw rank ≤
``direct_rescue_raw_rank`` の natural item を先頭 tier に lift する
(sort key = rescued → qualified → score desc、rescued tier 内は
raw cosine 降順)。

displacement は決定論的に作る: 固定 seed 乱数ベクトルを query 方向に
Gram-Schmidt 直交化し、unit-norm raw embedding に対し十分大きな
scale を掛ける → virtual_pos = normalize(raw + disp) と query の
cosine ≈ scale/√(1+scale²) 程度に低下し、T は raw 軸だけで
qualified に保たれる (raw_min=0.5 で corpus size 非依存)。
"""
from __future__ import annotations

import numpy as np
import pytest

from gaottt.config import GaOTTTConfig
from gaottt.core.engine import GaOTTTEngine

from tests.integration.test_engine_direct_qualification import (
    QUERY,
    _make_engine,
)

# target: query と完全同一語彙 → StubEmbedder raw cosine は厳密に 1.0
# (pool 内 rank 1 が決定論的に保証される)。
DOC_T = QUERY
# near-duplicate candidates: query 5 token + 固有 2 token → raw cosine は
# 0.75-0.85 帯 (既存 fixture の DOC_A 測定 +0.79 と同型)。全員
# raw_min=0.5 で qualified。token vector は md5 seed で固定なので
# raw 順位も実行毎に同一 (test 内で独立再計算して pin する)。
DOC_Q1 = "quantum gravity wave general relativity experiment data"
DOC_Q2 = "quantum gravity wave general relativity theory overview"
DOC_Q3 = "quantum gravity wave general relativity black hole"
DOC_Q4 = "quantum gravity wave general relativity spacetime metric"
# unqualified filler (既存 fixture の DOC_B): query と共有 token 1 つ。
DOC_FILLER = "quantum cooking pasta recipe kitchen tomato basil olive"

DISPLACE_SCALE = 8.0   # unit raw に対し十分大 → virtual cosine ~0.12 に低下
RESCUE_CORPUS = dict(direct_raw_cosine_min=0.5)


def _displace_away_from_query(
    engine: GaOTTTEngine, node_id: str, query: str,
    scale: float = DISPLACE_SCALE, seed: int = 20260826,
) -> np.ndarray:
    """query 方向と直交する displacement を注入し、ベクトルを返す。"""
    q = engine.embedder.encode_query(query)[0].astype(np.float64)
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(q.shape[0])
    v -= (float(np.dot(v, q)) / float(np.dot(q, q))) * q
    v /= np.linalg.norm(v) + 1e-12
    disp = (v * scale).astype(np.float32)
    engine.cache.set_displacement(node_id, disp)
    return disp


def _set_mass(engine: GaOTTTEngine, node_id: str, mass: float) -> None:
    state = engine.cache.get_node(node_id)
    assert state is not None
    state.mass = mass
    engine.cache.set_node(state, dirty=True)


def _raw_order(
    engine: GaOTTTEngine, node_ids: list[str], query: str,
) -> tuple[list[str], dict[str, float]]:
    """scored set 相当の独立 raw cosine 再計算 (rank は降順・id tiebreak)。"""
    q = engine.embedder.encode_query(query)[0]
    vecs = engine.faiss_index.get_vectors(list(node_ids))
    cos = {
        nid: float(np.dot(q, v))
        / (float(np.linalg.norm(q)) * float(np.linalg.norm(v)) + 1e-12)
        for nid, v in vecs.items()
    }
    order = sorted(cos, key=lambda nid: (-cos[nid], nid))
    return order, cos


async def _index_corpus(
    engine: GaOTTTEngine, docs: list[str], names: list[str],
) -> dict[str, str]:
    ids = await engine.index_documents(
        [{"content": d, "metadata": {"source": "agent"}} for d in docs],
    )
    return dict(zip(names, ids))


async def _pathology_engine(tmp_path, *, displace_target=True, **overrides):
    """T (raw rank 1, displacement 発散) + Q1..Q3 (qualified, 高 final)。"""
    cfg = {**RESCUE_CORPUS, **overrides}
    engine = await _make_engine(tmp_path, **cfg)
    named = await _index_corpus(
        engine, [DOC_T, DOC_Q1, DOC_Q2, DOC_Q3], ["T", "Q1", "Q2", "Q3"],
    )
    if displace_target:
        _displace_away_from_query(engine, named["T"], QUERY)
    return engine, named


def test_config_default_pin():
    """knob の code default は 3 (Phase U WP-4b 昇格)。"""
    assert GaOTTTConfig().direct_rescue_raw_rank == 3


async def test_rescue_lifts_displaced_raw_top1_to_head(tmp_path):
    """病理本体: knob=0 では T (raw rank 1, qualified) が top-K から消え、
    default knob では rescued tier の先頭 (#1) に現れる。"""
    # --- default knob (rescue ON) ---
    sub = tmp_path / "rescue"
    sub.mkdir()
    engine, named = await _pathology_engine(sub)
    try:
        results = await engine.query(text=QUERY, top_k=4)
        # 前提: T は独立再計算で raw rank 1 かつ qualified
        order, cos = _raw_order(engine, list(named.values()), QUERY)
        assert order[0] == named["T"]
        assert cos[named["T"]] == pytest.approx(1.0)
        # 前提: displacement で T の final は qualified 内最下位
        # (rescue が無ければ top-3 に入らない)
        finals = {r.id: r.final_score for r in results}
        assert finals[named["T"]] < min(
            finals[named[n]] for n in ("Q1", "Q2", "Q3")
        )
        # rescue: T が #1 (rescued tier 先頭)
        assert results[0].id == named["T"]
        assert results[0].score_breakdown.qualified is True
        # rescued tier (raw rank 1..3) は raw cosine 降順 — T(final 最下位)
        # が #1 に来ること自体が「tier 内 = raw desc (final desc でない)」
        # の判別証拠になる。
        assert [r.id for r in results[:3]] == order[:3]
    finally:
        await engine.shutdown()

    # --- knob=0 (rescue OFF) → T は top-3 にいない ---
    sub = tmp_path / "norescue"
    sub.mkdir()
    engine, named = await _pathology_engine(sub, direct_rescue_raw_rank=0)
    try:
        results = await engine.query(text=QUERY, top_k=3)
        returned = [r.id for r in results]
        assert named["T"] not in returned
        assert set(returned) == {named["Q1"], named["Q2"], named["Q3"]}
    finally:
        await engine.shutdown()


async def test_knob_zero_matches_stage3_ordering_exactly(tmp_path):
    """knob=0 は Phase T Stage 3 契約 (qualified-first stable partition
    over final_score desc) に厳密一致 — rescue 導入で変わらない fence。"""
    engine, named = await _pathology_engine(tmp_path, direct_rescue_raw_rank=0)
    try:
        results = await engine.query(text=QUERY, top_k=4)
        # T (qualified だが final 最下位) は qualified group の末尾 = 全体 #4
        assert [r.id for r in results][-1] == named["T"]
        expected = sorted(
            results,
            key=lambda r: (
                0 if r.score_breakdown.qualified else 1, -r.final_score,
            ),
        )
        assert [r.id for r in results] == [r.id for r in expected]
    finally:
        await engine.shutdown()


async def test_rescue_rank_boundary(tmp_path):
    """raw rank ≤ 3 だけが rescued (rank 4 は rescued にならない)。
    rank-4 doc に mass pump して final を高めにし、誤 rescue が
    順位を変える構造を作る。"""
    engine = await _make_engine(tmp_path, **RESCUE_CORPUS)
    try:
        named = await _index_corpus(
            engine, [DOC_T, DOC_Q1, DOC_Q2, DOC_Q3, DOC_Q4],
            ["T", "Q1", "Q2", "Q3", "Q4"],
        )
        _displace_away_from_query(engine, named["T"], QUERY)
        order, _ = _raw_order(engine, list(named.values()), QUERY)
        rank4 = order[3]
        rank5 = order[4]
        _set_mass(engine, rank4, 50.0)  # unrescued tier で final 最高に

        results = await engine.query(text=QUERY, top_k=5)
        returned = [r.id for r in results]
        # head = rescued tier (rank 1-3, raw desc) / tail = qualified tier
        # (final desc: rank4 (mass) → rank5)。rank4 が誤 rescue されたら
        # head 側に現れてこの順序は壊れる。
        assert returned[:3] == order[:3]
        assert returned[3:] == [rank4, rank5]
    finally:
        await engine.shutdown()


async def test_unqualified_high_raw_not_rescued(tmp_path):
    """raw rank 1 でも unqualified なら rescue されない (rescue は
    qualified item のみに定義される)。knob=0 と同一順序の fence。"""
    async def run(sub, **overrides):
        engine = await _make_engine(
            sub,
            direct_raw_cosine_min=0.99,
            direct_virtual_cosine_min=0.99,
            direct_bm25_absolute_min=1e9,   # lexical axis off
            **overrides,
        )
        try:
            # T は近重複語彙 (raw ~0.82 = corpus 内 rank 1) だが全軸の
            # 閾値 (0.99) を下回る → unqualified。raw cosine 1.0 の完全
            # 一致文本は使わない (raw 軸で qualified になってしまう)。
            named = await _index_corpus(
                engine, [DOC_Q1, DOC_FILLER], ["T", "FILLER"],
            )
            _displace_away_from_query(engine, named["T"], QUERY)
            _set_mass(engine, named["FILLER"], 48.0)
            results = await engine.query(text=QUERY, top_k=2)
            return named, [r.id for r in results], {
                r.id: r.score_breakdown.qualified for r in results
            }
        finally:
            await engine.shutdown()

    sub = tmp_path / "rescue-on"
    sub.mkdir()
    named_on, order_on, qual_on = await run(sub)
    assert qual_on[named_on["T"]] is False          # high raw でも unqualified
    assert order_on[-1] == named_on["T"]            # rescue されず最下位

    sub = tmp_path / "rescue-off"
    sub.mkdir()
    named_off, order_off, _ = await run(sub, direct_rescue_raw_rank=0)
    # rescue の影響ゼロ (id は engine 毎に異なるため語彙名で比較)
    inv_on = {v: k for k, v in named_on.items()}
    inv_off = {v: k for k, v in named_off.items()}
    assert [inv_on[i] for i in order_on] == [inv_off[i] for i in order_off]


async def test_qualification_off_no_rescue(tmp_path):
    """direct_qualification_enabled=False なら rescue も無効
    (rescue は qualified item 上に定義)。legacy final_score 順。"""
    engine, named = await _pathology_engine(
        tmp_path,
        direct_qualification_enabled=False,
        ttt_qualification_enabled=False,
    )
    try:
        results = await engine.query(text=QUERY, top_k=1)
        # T の final は displacement で最下位 → raw rank 1 でも top-1 は
        # Q 側 (legacy 挙動)
        assert results[0].id != named["T"]
        assert results[0].score_breakdown.qualified is None
    finally:
        await engine.shutdown()


async def test_forced_ordering_unchanged_with_rescue_active(tmp_path):
    """forced/injected 経路は Phase J 規則 (forced 内 raw cosine 降順) の
    まま — rescue は natural item にのみ適用される。"""
    engine = await _make_engine(
        tmp_path,
        direct_raw_cosine_min=0.5,
    )
    try:
        ids = await engine.index_documents([
            {"content": DOC_T, "metadata": {"source": "agent"}},
            # B: forced 側。mass pump で final を C より高くする
            # (誤って final 順になったら [B, C] に反転する構造)。
            {"content": DOC_FILLER,
             "metadata": {"source": "agent", "tags": ["forced-set"]}},
            # C: forced 側。raw 0.62 > B 0.04 だが displacement で
            # final は B 未満。
            {"content": "quantum gravity wave simulation update notes",
             "metadata": {"source": "agent", "tags": ["forced-set"]}},
        ])
        idt, idb, idc = ids
        _displace_away_from_query(engine, idc, QUERY)
        _set_mass(engine, idb, 10.0)

        results = await engine.query(
            text=QUERY, top_k=3, tag_filter=["forced-set"],
        )
        returned = [r.id for r in results]
        # forced block が先頭、forced 内は raw cosine 降順 [C, B]
        # (final 降順なら [B, C] に反転する)。natural T (qualified,
        # raw rank 1 → rescued) が残り slot を埋める。
        assert returned == [idc, idb, idt]
        for r in results[:2]:
            assert r.score_breakdown.forced_inclusion is True
    finally:
        await engine.shutdown()


async def test_passive_recall_same_ordering_no_writes(tmp_path):
    """passive recall も同一の rescue 順序 (presentation-only) で、
    physics (mass / return_count / displacement) に書き込まない。"""
    async def run(sub, *, passive):
        engine = await _make_engine(sub, **RESCUE_CORPUS)
        try:
            named = await _index_corpus(
                engine, [DOC_T, DOC_Q1, DOC_FILLER], ["T", "Q1", "FILLER"],
            )
            disp = _displace_away_from_query(engine, named["T"], QUERY)
            results = await engine.query(
                text=QUERY, top_k=3, passive=passive,
            )
            return engine, named, disp, [r.id for r in results]
        except Exception:
            await engine.shutdown()
            raise

    sub = tmp_path / "passive"
    sub.mkdir()
    eng_p, named_p, disp_p, order_p = await run(sub, passive=True)
    try:
        assert order_p[0] == named_p["T"]   # rescue 適用 (presentation-only)
        # no-writes: mass / return_count / displacement 不変
        st = eng_p.cache.get_node(named_p["T"])
        assert st.mass == pytest.approx(1.0)
        assert st.return_count == pytest.approx(0.0)
        assert np.allclose(
            eng_p.cache.get_displacement(named_p["T"]), disp_p,
        )
    finally:
        await eng_p.shutdown()

    sub = tmp_path / "active"
    sub.mkdir()
    eng_a, named_a, _, order_a = await run(sub, passive=False)
    await eng_a.shutdown()
    # twin engine (同一 deterministic fixture) で active と同順序
    # (node id は engine 毎に異なるため語彙名で比較)
    inv_a = {v: k for k, v in named_a.items()}
    inv_p = {v: k for k, v in named_p.items()}
    assert [inv_a[i] for i in order_a] == [inv_p[i] for i in order_p]


async def test_diversity_path_consumes_rescued_candidates(tmp_path):
    """diversity (MMR) は rescue 適用後の pool から選択する — rescued
    item は MMR candidate として入り、経路が壊れない (豁免しない)。"""
    engine, named = await _pathology_engine(tmp_path, **RESCUE_CORPUS)
    try:
        plain = await engine.query(text=QUERY, top_k=4, passive=True)
        # 前提: rescue で T が candidate window (top_k × multiplier) の
        # 先頭にいる
        assert plain[0].id == named["T"]

        diverse = await engine.query(
            text=QUERY, top_k=4, diversity=0.5, passive=True,
        )
        assert len(diverse) == 4            # MMR が top_k 分を選択
        assert {r.id for r in diverse} == set(named.values())
    finally:
        await engine.shutdown()
