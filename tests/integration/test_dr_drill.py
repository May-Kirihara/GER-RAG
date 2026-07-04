"""MV5 WP-3 — DR drill integration tests.

Drives ``scripts/dr_drill.run_drill`` in-process against a tmp root and
asserts the three acceptance criteria from the plan:

1. ``run_drill(tmp_path)`` returns 0 (the complete happy path).
2. ``run_drill(tmp_path, with_litestream=True)`` returns 0 even when the
   litestream binary is absent (the WARN-skip path — default path always
   runs, Codex review #3).
3. **Manifest-consistency fence**: a restore that copies the DB back but
   forgets the manifest is caught by ``verify_embedder_identity`` (the
   startup gate raises), proving the fence detects operator error.

The drill starts its own stub embedding service internally (via
``run_drill``), so these tests need no embedder fixture of their own.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

# scripts/ is not a package; import the drill by file path so the test does
# not depend on scripts becoming importable.
REPO_ROOT = Path(__file__).resolve().parents[2]
DRILL = REPO_ROOT / "scripts" / "dr_drill.py"

import importlib.util  # noqa: E402 — must follow REPO_ROOT/DRILL definitions above

_spec = importlib.util.spec_from_file_location("dr_drill", DRILL)
assert _spec is not None and _spec.loader is not None
_drill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_drill)
run_drill = _drill.run_drill


# ---------------------------------------------------------------------------
# 1. complete happy path
# ---------------------------------------------------------------------------

def test_run_drill_returns_zero(tmp_path):
    # Sync test: run_drill() owns its own event loop (asyncio.run), so it
    # cannot be called from inside pytest-asyncio's already-running loop.
    rc = run_drill(tmp_path / "drill-root")
    assert rc == 0, "drill should complete the full backup→destroy→restore→rebuild→diagnose path"


# ---------------------------------------------------------------------------
# 2. --with-litestream WARN-skips when binary absent (Codex review #3)
# ---------------------------------------------------------------------------

def test_run_drill_with_litestream_skips_when_binary_absent(tmp_path, monkeypatch):
    # Sync test (see above). Force the binary lookup to miss, regardless of
    # the host environment.
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    rc = run_drill(tmp_path / "drill-root-litestream", with_litestream=True)
    assert rc == 0, "drill must complete even when litestream binary is absent (WARN skip)"


# ---------------------------------------------------------------------------
# 3. manifest-consistency fence — DB restored, manifest forgotten -> gate raises
# ---------------------------------------------------------------------------

def test_manifest_missing_on_restore_is_caught(tmp_path):
    """A restore that copies gaottt.db back but forgets manifest.json must be
    caught: ``ensure_manifest`` regenerates a manifest from config defaults
    (embedder_id = config.model_name), which then mismatches the runtime
    stub embedder's identity, so ``verify_embedder_identity`` raises.

    This is the fence that proves the 2-point backup set is *necessary* —
    SQLite alone is not sufficient for a correct restore."""
    from gaottt.config import GaOTTTConfig
    from gaottt.store.manifest import (
        UniverseManifest,
        ensure_manifest,
        load_manifest,  # noqa: F401 — kept to mirror the manifest module surface
        verify_embedder_identity,
        write_manifest,
    )

    udir = tmp_path / "universe"
    udir.mkdir(parents=True)

    # Write a manifest that records the stub embedder identity (what a real
    # backup would have captured at universe-creation time).
    write_manifest(
        udir,
        UniverseManifest(
            universe_id="fence",
            embedder_id="stub",
            embedder_version="test",
            embedding_dim=32,
            created_at=0.0,
            managed=False,
        ),
    )
    # A minimal SQLite file so ensure_manifest's data_dir exists; the gate
    # runs before any store initialize, so the db content is irrelevant —
    # the gate only inspects manifest vs embedder identity.
    (udir / "gaottt.db").write_bytes(b"x")

    config = GaOTTTConfig(
        data_dir=str(udir),
        db_path=str(udir / "gaottt.db"),
        embedding_dim=32,
        # Simulate "operator forgot the manifest": ensure_manifest will
        # REGENERATE one from config defaults. config.model_name defaults to
        # "cl-nagoya/ruri-v3-310m", which must mismatch the runtime stub.
        embedder_endpoint="http://127.0.0.1:0",  # unreachable is fine; gate raises before any HTTP
    )

    # Pretend the manifest was NOT restored: delete it so ensure_manifest
    # regenerates from config (the simulated operator error).
    (udir / "manifest.json").unlink()

    # Build a stub embedder with a DIFFERENT identity than config.model_name.
    class _StubWithIdentity:
        dimension = 32
        embedder_id = "stub"
        embedder_version = "test"

        def encode_documents(self, texts):
            raise NotImplementedError

        def encode_query(self, text):
            raise NotImplementedError

    # ensure_manifest regenerates a manifest whose embedder_id =
    # config.model_name (because the operator forgot the real one).
    regenerated = ensure_manifest(udir, config)
    assert regenerated.embedder_id == config.model_name, (
        "test precondition: regenerated manifest must carry config.model_name"
    )

    # The gate now sees: manifest embedder_id = "cl-nagoya/ruri-v3-310m",
    # runtime embedder.embedder_id = "stub" -> mismatch -> RuntimeError.
    # (manifest_check_enabled defaults True; this is the fence.)
    with pytest.raises(RuntimeError, match="embedder_id"):
        verify_embedder_identity(regenerated, _StubWithIdentity(), config)
