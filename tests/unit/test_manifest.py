"""MV0 — Universe manifest tests (test-first / RED stage).

These tests assume the ``gaottt.store.manifest`` module API defined in
``docs/maintainers/multiverse-implementation-plan.md`` §MV0-2 and the
``manifest_check_enabled`` config knob from the same section. Until WP-2
lands both, importing this module raises ``ModuleNotFoundError`` — the
expected test-first RED state. Once WP-2 implements the contract below,
every test here should turn GREEN with no further edits.

Assumed public API (the WP-2 contract this file pins):

    class UniverseManifest(BaseModel):
        schema_version: int = 1
        universe_id: str            # "default" for single-user setups
        embedder_id: str            # e.g. config.model_name
        embedder_version: str       # HF revision, or "unpinned"
        embedding_dim: int
        created_at: float
        managed: bool = False       # MV3 supervisor writes True

    def load_manifest(data_dir: Path) -> UniverseManifest | None: ...
    def write_manifest(data_dir: Path, m: UniverseManifest) -> None: ...
        # atomic: tmp file + os.replace; no stray *.tmp after a clean write
    def ensure_manifest(data_dir: Path, config: GaOTTTConfig) -> UniverseManifest: ...
        # generate-from-config when absent, return-existing as-is, mkdir data_dir
    def verify_embedder_identity(manifest, embedder, config) -> None: ...
        # FAISS dim protection (embedder.dimension vs config.embedding_dim)
        # always raises; manifest-layer mismatches honour manifest_check_enabled

The manifest file lives at ``<data_dir>/manifest.json``.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from gaottt.config import GaOTTTConfig

MANIFEST_FILENAME = "manifest.json"


class _StubEmbedderForManifest:
    """Minimal embedder stub for identity / dimension checks.

    Only the attributes ``verify_embedder_identity`` consults are
    implemented. ``encode_*`` are present to satisfy the Protocol shape
    but must never be called by the manifest module.
    """

    def __init__(
        self,
        dimension: int,
        embedder_id: str = "stub-test",
        embedder_version: str = "unpinned",
    ) -> None:
        self._dimension = dimension
        self._embedder_id = embedder_id
        self._embedder_version = embedder_version

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def embedder_id(self) -> str:
        return self._embedder_id

    @property
    def embedder_version(self) -> str:
        return self._embedder_version

    def encode_documents(self, texts: list[str]):  # pragma: no cover
        raise AssertionError("encode_documents must not be called by manifest ops")

    def encode_query(self, text: str):  # pragma: no cover
        raise AssertionError("encode_query must not be called by manifest ops")


def _make_config(data_dir: Path, **overrides) -> GaOTTTConfig:
    """Build a small-dim config pinned to ``data_dir`` (archive_ttl style)."""
    base: dict = dict(
        embedding_dim=32,
        model_name="stub-test-model",
        data_dir=str(data_dir),
        db_path=str(data_dir / "gaottt.db"),
        faiss_index_path=str(data_dir / "gaottt.faiss"),
    )
    base.update(overrides)
    return GaOTTTConfig(**base)


def _sample_manifest(**overrides):
    from gaottt.store.manifest import UniverseManifest

    fields: dict = dict(
        universe_id="default",
        embedder_id="stub-test-model",
        embedder_version="unpinned",
        embedding_dim=32,
        created_at=time.time(),
    )
    fields.update(overrides)
    return UniverseManifest(**fields)


# ---------------------------------------------------------------------------
# 1. UniverseManifest roundtrip
# ---------------------------------------------------------------------------

def test_manifest_model_roundtrip_preserves_fields():
    from gaottt.store.manifest import UniverseManifest

    # Pin the v1 contract directly: roundtrip alone would pass with
    # schema_version=2 since the field round-trips, so assert the default here.
    assert _sample_manifest().schema_version == 1

    m = _sample_manifest(embedder_id="other-model", embedding_dim=768)
    rebuilt = UniverseManifest(**m.model_dump())
    assert rebuilt == m


def test_manifest_managed_defaults_false():
    # MV3 supervisor writes managed=True; standalone ensure_manifest leaves False.
    assert _sample_manifest().managed is False


# ---------------------------------------------------------------------------
# 2. load_manifest
# ---------------------------------------------------------------------------

def test_load_manifest_returns_none_when_absent(tmp_path: Path):
    from gaottt.store.manifest import load_manifest

    assert load_manifest(tmp_path) is None


def test_load_manifest_reads_existing(tmp_path: Path):
    from gaottt.store.manifest import load_manifest, write_manifest

    m = _sample_manifest(universe_id="u-abc")
    write_manifest(tmp_path, m)
    loaded = load_manifest(tmp_path)
    assert loaded is not None
    assert loaded == m


# ---------------------------------------------------------------------------
# 3. write_manifest atomicity (tmp + os.replace)
# ---------------------------------------------------------------------------

def test_write_manifest_leaves_no_tmp_after_clean_write(tmp_path: Path):
    from gaottt.store.manifest import write_manifest

    write_manifest(tmp_path, _sample_manifest())
    assert (tmp_path / MANIFEST_FILENAME).exists()
    # Naming of the scratch file is impl-defined, so glob any *.tmp leftover.
    leftovers = [p.name for p in tmp_path.glob("*.tmp")]
    assert leftovers == [], f"stray tmp files after clean write: {leftovers}"


def test_write_manifest_replaces_existing_atomically(tmp_path: Path):
    from gaottt.store.manifest import load_manifest, write_manifest

    write_manifest(tmp_path, _sample_manifest(embedder_id="model-v1"))
    write_manifest(tmp_path, _sample_manifest(embedder_id="model-v2"))

    loaded = load_manifest(tmp_path)
    assert loaded is not None
    assert loaded.embedder_id == "model-v2"
    leftovers = [p.name for p in tmp_path.glob("*.tmp")]
    assert leftovers == []


def test_write_manifest_failed_replace_keeps_original(tmp_path: Path):
    """Simulates a crash / concurrent writer mid-replace: os.replace fails,
    the previously-written manifest at ``manifest.json`` must survive
    intact. This is the atomic-write invariant the tmp+replace pattern buys."""
    from gaottt.store.manifest import load_manifest, write_manifest

    write_manifest(tmp_path, _sample_manifest(embedder_id="model-v1"))
    original_bytes = (tmp_path / MANIFEST_FILENAME).read_bytes()

    with patch("gaottt.store.manifest.os.replace", side_effect=OSError("simulated")):
        # The natural implementation lets os.replace's OSError propagate.
        with pytest.raises(OSError, match="simulated"):
            write_manifest(tmp_path, _sample_manifest(embedder_id="model-v2"))

    assert (tmp_path / MANIFEST_FILENAME).read_bytes() == original_bytes
    assert load_manifest(tmp_path).embedder_id == "model-v1"

    # A failed replace must not leave a stray scratch file behind — the atomic
    # tmp+replace pattern is only complete when the failure path also cleans up.
    leftovers = [p.name for p in tmp_path.glob("*.tmp")]
    assert leftovers == [], f"stray tmp files after failed replace: {leftovers}"


# ---------------------------------------------------------------------------
# 4. ensure_manifest — generation path (manifest absent)
# ---------------------------------------------------------------------------

def test_ensure_manifest_generates_from_config_when_absent(tmp_path: Path):
    from gaottt.store.manifest import ensure_manifest, load_manifest

    cfg = _make_config(tmp_path)
    before = time.time()
    m = ensure_manifest(tmp_path, cfg)
    after = time.time()

    assert m.universe_id == "default"
    assert m.embedder_id == cfg.model_name
    assert m.embedding_dim == cfg.embedding_dim
    assert m.managed is False
    assert before - 1.0 <= m.created_at <= after + 1.0

    # Persisted to disk.
    reloaded = load_manifest(tmp_path)
    assert reloaded is not None
    assert reloaded.embedder_id == cfg.model_name


# ---------------------------------------------------------------------------
# 5. ensure_manifest — existing path (does not overwrite)
# ---------------------------------------------------------------------------

def test_ensure_manifest_returns_existing_without_overwrite(tmp_path: Path):
    from gaottt.store.manifest import ensure_manifest, load_manifest, write_manifest

    cfg = _make_config(tmp_path)
    existing = _sample_manifest(
        embedder_id="preset-different-model",
        embedder_version="abc123",
        embedding_dim=cfg.embedding_dim,
    )
    write_manifest(tmp_path, existing)

    returned = ensure_manifest(tmp_path, cfg)
    assert returned.embedder_id == "preset-different-model"
    assert returned.embedder_version == "abc123"

    # On-disk file unchanged.
    assert load_manifest(tmp_path).embedder_id == "preset-different-model"


# ---------------------------------------------------------------------------
# 6. ensure_manifest — creates data_dir if missing (QA NB1)
# ---------------------------------------------------------------------------

def test_ensure_manifest_makes_data_dir_when_missing(tmp_path: Path):
    from gaottt.store.manifest import ensure_manifest

    nested = tmp_path / "deep" / "nested" / "data"
    assert not nested.exists()

    cfg = _make_config(nested)
    m = ensure_manifest(nested, cfg)

    assert nested.is_dir()
    assert (nested / MANIFEST_FILENAME).exists()
    assert m.embedder_id == cfg.model_name


# ---------------------------------------------------------------------------
# 7. verify_embedder_identity — manifest.embedding_dim mismatch
# ---------------------------------------------------------------------------

def test_verify_dim_mismatch_raises_when_check_enabled(tmp_path: Path):
    from gaottt.store.manifest import verify_embedder_identity

    cfg = _make_config(tmp_path, manifest_check_enabled=True)
    manifest = _sample_manifest(embedding_dim=cfg.embedding_dim + 64)
    embedder = _StubEmbedderForManifest(dimension=cfg.embedding_dim)

    with pytest.raises(RuntimeError):
        verify_embedder_identity(manifest, embedder, cfg)


def test_verify_dim_mismatch_warns_when_check_disabled(tmp_path: Path, caplog):
    from gaottt.store.manifest import verify_embedder_identity

    cfg = _make_config(tmp_path, manifest_check_enabled=False)
    manifest = _sample_manifest(embedding_dim=cfg.embedding_dim + 64)
    embedder = _StubEmbedderForManifest(dimension=cfg.embedding_dim)

    with caplog.at_level(logging.WARNING):
        # Escape hatch downgrades the manifest-layer mismatch to a warning.
        verify_embedder_identity(manifest, embedder, cfg)

    warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("embedding_dim" in msg for msg in warning_msgs), (
        f"expected embedding_dim in warnings: {warning_msgs}"
    )


# ---------------------------------------------------------------------------
# 8. verify_embedder_identity — embedder_id mismatch
# ---------------------------------------------------------------------------

def test_verify_embedder_id_mismatch_raises_when_check_enabled(tmp_path: Path):
    from gaottt.store.manifest import verify_embedder_identity

    cfg = _make_config(tmp_path, manifest_check_enabled=True)
    manifest = _sample_manifest(embedder_id="manifest-recorded-model")
    embedder = _StubEmbedderForManifest(
        dimension=cfg.embedding_dim, embedder_id="runtime-different-model"
    )

    with pytest.raises(RuntimeError):
        verify_embedder_identity(manifest, embedder, cfg)


def test_verify_embedder_id_mismatch_warns_when_check_disabled(tmp_path: Path, caplog):
    from gaottt.store.manifest import verify_embedder_identity

    cfg = _make_config(tmp_path, manifest_check_enabled=False)
    manifest = _sample_manifest(embedder_id="manifest-recorded-model")
    embedder = _StubEmbedderForManifest(
        dimension=cfg.embedding_dim, embedder_id="runtime-different-model"
    )

    with caplog.at_level(logging.WARNING):
        verify_embedder_identity(manifest, embedder, cfg)

    warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("embedder_id" in msg for msg in warning_msgs), (
        f"expected embedder_id in warnings: {warning_msgs}"
    )


# ---------------------------------------------------------------------------
# 8b. verify_embedder_identity — embedder_version mismatch is warn-only
#     (v1 contract: id + dim are hard checks; version drift does not stop
#      startup. HF revision moves under normal operation in v1.)
# ---------------------------------------------------------------------------

def test_verify_embedder_version_mismatch_warns_only(tmp_path: Path, caplog):
    from gaottt.store.manifest import verify_embedder_identity

    cfg = _make_config(tmp_path, manifest_check_enabled=True)
    # id and dim agree — only the version differs.
    manifest = _sample_manifest(
        embedder_id="stub-test-model",
        embedder_version="manifest-recorded-rev",
        embedding_dim=cfg.embedding_dim,
    )
    embedder = _StubEmbedderForManifest(
        dimension=cfg.embedding_dim,
        embedder_id="stub-test-model",
        embedder_version="runtime-different-rev",
    )

    with caplog.at_level(logging.WARNING):
        # Even with the check enabled, version drift must NOT raise.
        verify_embedder_identity(manifest, embedder, cfg)

    warning_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("embedder_version" in msg for msg in warning_msgs), (
        f"expected embedder_version in warnings: {warning_msgs}"
    )


# ---------------------------------------------------------------------------
# 9. verify_embedder_identity — FAISS dimension protection
#    (embedder.dimension != config.embedding_dim ALWAYS raises, independent
#    of manifest_check_enabled — a dim mismatch would corrupt the index.)
# ---------------------------------------------------------------------------

def test_faiss_dim_mismatch_always_raises_even_when_check_disabled(tmp_path: Path):
    from gaottt.store.manifest import verify_embedder_identity

    cfg = _make_config(tmp_path, manifest_check_enabled=False)
    # Manifest dim agrees with config (no manifest-layer mismatch) ...
    manifest = _sample_manifest(embedding_dim=cfg.embedding_dim)
    # ... but the runtime embedder reports a different dimension.
    embedder = _StubEmbedderForManifest(dimension=cfg.embedding_dim + 128)

    with pytest.raises(RuntimeError):
        verify_embedder_identity(manifest, embedder, cfg)


def test_faiss_dim_mismatch_raises_when_check_enabled(tmp_path: Path):
    from gaottt.store.manifest import verify_embedder_identity

    cfg = _make_config(tmp_path, manifest_check_enabled=True)
    manifest = _sample_manifest(embedding_dim=cfg.embedding_dim)
    embedder = _StubEmbedderForManifest(dimension=cfg.embedding_dim + 128)

    with pytest.raises(RuntimeError):
        verify_embedder_identity(manifest, embedder, cfg)
