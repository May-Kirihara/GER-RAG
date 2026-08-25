"""Phase T Stage 5 — ambient OR gate, pure parts (WP-4 unit).

``AmbientGateDiagnostics`` defaults, the ``format_ambient`` gate-triage
line contract (sentinel byte-identical, diagnostic line appended only
under ``expose_breakdown``), and the ``_bm25_gate_top`` numeric wrapper
that feeds both the gate decision and the diagnostics from one search.
"""
from __future__ import annotations

from types import SimpleNamespace

from gaottt.config import GaOTTTConfig
from gaottt.core.types import (
    AmbientGateDiagnostics,
    AmbientMemory,
    AmbientRecallResponse,
)
from gaottt.index.bm25_index import BM25Index
from gaottt.services.memory import _bm25_gate, _bm25_gate_top

SENTINEL = "(関連する記憶なし)"


def _gate_engine(**overrides):
    idx = BM25Index()  # trigram — the tests exercise gate logic, not Sudachi
    idx.add(
        ["a", "b"],
        [
            "gravitational wave propagation through the seed pool",
            "orbital mechanics and displacement decay",
        ],
    )
    return SimpleNamespace(
        config=GaOTTTConfig(**overrides), ambient_gate_index=idx,
    )


# --- AmbientGateDiagnostics model ------------------------------------------------


def test_gate_diagnostics_defaults():
    d = AmbientGateDiagnostics()
    assert d.candidates_generated == 0
    assert d.after_tag_exclusion == 0
    assert d.after_dump_filter == 0
    assert d.semantic_qualified == 0
    assert d.direct_selected == 0
    assert d.lensing_selected == 0
    assert d.bm25_top_score is None
    assert d.bm25_gate is None
    assert d.semantic_max_virtual is None
    assert d.semantic_max_raw is None
    assert d.empty_reason is None


# --- _bm25_gate_top (numeric input behind the gate) ------------------------------


def test_bm25_gate_top_returns_score_when_usable():
    engine = _gate_engine(ambient_bm25_min_score=0.01)
    top = _bm25_gate_top(engine, "gravitational wave propagation")
    assert isinstance(top, float)
    assert top > 0.0
    # same search feeds the boolean gate
    assert _bm25_gate(engine, "gravitational wave propagation") is True


def test_bm25_gate_top_zero_for_disjoint_vocabulary():
    engine = _gate_engine(ambient_bm25_min_score=0.01)
    assert _bm25_gate_top(engine, "りんごジュースの値段") == 0.0
    assert _bm25_gate(engine, "りんごジュースの値段") is False


def test_bm25_gate_top_none_when_disabled_or_absent():
    engine = _gate_engine(ambient_gate_use_bm25=False)
    assert _bm25_gate_top(engine, "gravitational wave propagation") is None
    engine = SimpleNamespace(config=GaOTTTConfig(), ambient_gate_index=None)
    assert _bm25_gate_top(engine, "gravitational wave propagation") is None


# --- format_ambient gate-triage line --------------------------------------------


def _empty_resp(**kw) -> AmbientRecallResponse:
    diag = kw.pop("diag", None)
    expose = kw.pop("expose", False)
    return AmbientRecallResponse(
        count=0, gate_diagnostics=diag, expose_breakdown=expose,
    )


def test_sentinel_byte_identical_without_expose():
    """Default (expose off): the empty return is EXACTLY the legacy sentinel —
    no diagnostic line, no trailing newline."""
    from gaottt.services.formatters import format_ambient

    for diag in (
        None,
        AmbientGateDiagnostics(empty_reason="bm25_veto", bm25_gate=False),
        AmbientGateDiagnostics(empty_reason="bm25_and_semantic_below_threshold"),
    ):
        resp = _empty_resp(diag=diag)
        assert format_ambient(resp) == SENTINEL
        assert format_ambient(resp, config=GaOTTTConfig()) == SENTINEL


def test_sentinel_then_diag_line_when_exposed():
    """expose_breakdown=True + diagnostics: sentinel line unchanged, the
    triage line is APPENDED after it (never inline)."""
    from gaottt.services.formatters import format_ambient

    resp = _empty_resp(
        diag=AmbientGateDiagnostics(
            empty_reason="bm25_and_semantic_below_threshold",
            bm25_top_score=8.3,
            bm25_gate=False,
            semantic_max_virtual=0.61,
            semantic_max_raw=0.58,
            candidates_generated=10,
        ),
        expose=True,
    )
    out = format_ambient(resp, config=GaOTTTConfig())
    lines = out.splitlines()
    assert lines[0] == SENTINEL
    assert len(lines) == 2
    assert lines[1].startswith("gate: ")
    assert "bm25_and_semantic_below_threshold" in lines[1]
    assert "bm25_top=8.3" in lines[1]
    assert "virt_max=0.610" in lines[1]
    assert "raw_max=0.580" in lines[1]
    assert "candidates=10" in lines[1]


def test_diag_line_omits_absent_axes():
    """Fields the gate could not compute (None) stay out of the line."""
    from gaottt.services.formatters import format_ambient

    resp = _empty_resp(
        diag=AmbientGateDiagnostics(
            empty_reason="bm25_veto", bm25_top_score=5.0, bm25_gate=False,
        ),
        expose=True,
    )
    line = format_ambient(resp).splitlines()[1]
    assert "bm25_top=5.0" in line
    assert "virt_max" not in line
    assert "raw_max" not in line


def test_nonempty_block_appends_gate_line_before_manifest():
    """Non-empty block: the triage line rides along only under
    expose_breakdown, and the ``<!-- ambient-ids ... -->`` manifest stays
    the last line before the closing tag."""
    from gaottt.services.formatters import format_ambient

    diag = AmbientGateDiagnostics(
        bm25_top_score=40.0, bm25_gate=True, semantic_max_virtual=0.91,
        candidates_generated=10, after_dump_filter=10, semantic_qualified=4,
        direct_selected=2,
    )
    direct = [AmbientMemory(id="n1", content="hello world", source="agent")]
    base = AmbientRecallResponse(direct=direct, count=1)

    # expose off → byte-identical to the legacy block (no gate line)
    out_off = format_ambient(base, config=GaOTTTConfig())
    assert "gate:" not in out_off

    on = base.model_copy(
        update={"gate_diagnostics": diag, "expose_breakdown": True},
    )
    out_on = format_ambient(on, config=GaOTTTConfig())
    assert "gate: passed (" in out_on
    lines = out_on.splitlines()
    assert lines[-1] == "</gaottt-ambient-recall>"
    assert lines[-2].startswith("<!-- ambient-ids ")
    assert lines[-3].startswith("gate: ")
