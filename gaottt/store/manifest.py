"""Universe manifest (MV0).

The manifest is a small JSON sidecar at ``<data_dir>/manifest.json`` that
records the embedder identity the universe was created with. Its job is to
stop a silent embedder swap — switching to a different model (or even a
different dimension) without re-embedding would make every stored vector
semantically meaningless and corrupt the FAISS index. Two layers guard
against that:

1. **engine.startup()** calls ``ensure_manifest`` (auto-generates from
   config when absent, so existing DBs are covered) then checks the
   ``embedding_dim`` against ``config.embedding_dim`` — the manifest-layer
   dimension guard. ``manifest_check_enabled`` downgrades this to a warning.
2. **build_engine()** additionally calls ``verify_embedder_identity`` with
   the concrete embedder instance: ``embedder_id`` is a hard check (gated
   by ``manifest_check_enabled``), ``embedder.dimension != config.embedding_dim``
   is *always* a hard error (FAISS index corruption would otherwise result),
   and ``embedder_version`` drift is warning-only (HF revisions move under
   normal operation in v1).

API surface is pinned by ``tests/unit/test_manifest.py`` (WP-1 contract).
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

from pydantic import BaseModel

from gaottt.config import GaOTTTConfig

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "manifest.json"


class UniverseManifest(BaseModel):
    schema_version: int = 1
    universe_id: str
    embedder_id: str
    embedder_version: str
    embedding_dim: int
    created_at: float
    managed: bool = False


def load_manifest(data_dir: Path) -> UniverseManifest | None:
    path = Path(data_dir) / MANIFEST_FILENAME
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return UniverseManifest(**data)


def write_manifest(data_dir: Path, m: UniverseManifest) -> None:
    """Atomically write ``m`` to ``<data_dir>/manifest.json``.

    Writes to a scratch file then ``os.replace``-renames it onto the final
    path. ``os.replace`` is atomic on POSIX, so a reader never sees a torn
    file. The scratch name is unique per writer (pid + uuid) so concurrent
    writers cannot clobber each other's tmp, and it always ends in ``.tmp``
    so the ``*.tmp`` cleanup invariant stays observable. On replace failure
    the scratch file is removed so no ``*.tmp`` leftover survives — the
    atomic-write invariant is only complete when the failure path also
    cleans up.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = m.model_dump_json(indent=2)
    final = data_dir / MANIFEST_FILENAME
    tmp_name = f"{MANIFEST_FILENAME}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    tmp = data_dir / tmp_name
    tmp.write_text(payload, encoding="utf-8")
    try:
        os.replace(tmp, final)
    except Exception:
        # Best-effort cleanup; ignore a concurrent unlink so the original
        # exception (the actionable signal) propagates unmasked.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def ensure_manifest(data_dir: Path, config: GaOTTTConfig) -> UniverseManifest:
    """Return the manifest for ``data_dir``, generating it from ``config`` if absent.

    ``data_dir`` is created (parents, exist-ok) so a brand-new DB whose
    directory does not yet exist (the gate runs before ``store.initialize``)
    gets both the directory and the manifest in one step. Existing DBs hit
    the no-op mkdir and the return-existing path.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    existing = load_manifest(data_dir)
    if existing is not None:
        return existing
    m = UniverseManifest(
        universe_id="default",
        embedder_id=config.model_name,
        embedder_version="unpinned",
        embedding_dim=config.embedding_dim,
        created_at=time.time(),
        managed=False,
    )
    write_manifest(data_dir, m)
    return m


def verify_embedder_identity(
    manifest: UniverseManifest,
    embedder,
    config: GaOTTTConfig,
) -> None:
    """Verify the runtime embedder matches the universe manifest.

    Three independent checks, in order of severity:

    * **FAISS dimension protection** (``embedder.dimension !=
      config.embedding_dim``): always ``RuntimeError``. A dim mismatch
      would corrupt the FAISS index; this is independent of the manifest
      and not escapable.
    * **manifest ``embedding_dim``** and **``embedder_id``**: ``RuntimeError``
      when ``manifest_check_enabled``, warning-and-continue otherwise.
    * **``embedder_version``**: warning-only. HF revisions move under
      normal operation in v1; DR artifact pinning is a later-stage concern.
    """
    # FAISS index corruption guard — manifest-independent, never escapable.
    if embedder.dimension != config.embedding_dim:
        raise RuntimeError(
            f"FAISS dimension mismatch: embedder.dimension="
            f"{embedder.dimension}, config.embedding_dim="
            f"{config.embedding_dim}. The FAISS index was built for "
            f"{config.embedding_dim} dims; a different-dimension embedder "
            f"would corrupt it."
        )

    if manifest.embedding_dim != config.embedding_dim:
        msg = (
            f"Manifest embedding_dim mismatch: manifest="
            f"{manifest.embedding_dim}, config={config.embedding_dim}. "
            f"Switching embedder requires re-embedding via "
            f"scripts/rebuild_faiss_from_db.py and a manifest update. "
            f"escape: GAOTTT_MANIFEST_CHECK_ENABLED=false"
        )
        if config.manifest_check_enabled:
            raise RuntimeError(msg)
        logger.warning(msg)

    if manifest.embedder_id != embedder.embedder_id:
        msg = (
            f"Manifest embedder_id mismatch: manifest="
            f"{manifest.embedder_id!r}, runtime={embedder.embedder_id!r}. "
            f"Switching embedder requires re-embedding via "
            f"scripts/rebuild_faiss_from_db.py and a manifest update. "
            f"escape: GAOTTT_MANIFEST_CHECK_ENABLED=false"
        )
        if config.manifest_check_enabled:
            raise RuntimeError(msg)
        logger.warning(msg)

    # Version drift is warning-only in v1 (HF revision moves under normal
    # operation); never blocks startup.
    if manifest.embedder_version != embedder.embedder_version:
        logger.warning(
            "Manifest embedder_version mismatch: manifest=%r, runtime=%r "
            "(v1: warning only — HF revision drift does not block startup).",
            manifest.embedder_version, embedder.embedder_version,
        )
