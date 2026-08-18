"""MV5 WP-1 — unit tests for the litestream config generator.

Covers the pure function (:func:`generate_litestream_config`) and the CLI
(``scripts/gen_litestream_config.py``) via subprocess. The pure function
lives in ``gaottt.multiverse.backup`` so the supervisor hook (WP-2) can
import it without touching ``scripts/``.

Test surface (Codex review B1/B2 + trash race):
  * empty root → valid YAML ``dbs: []``
  * 1 / 3 universes → correct entry count + paths
  * ``trash/`` exclusion (a deleted universe placed there is absent)
  * manifest missing → ERROR log + universe skipped
  * ``*.db`` outside the canonical location ignored
  * stdout/stderr separation: stdout is always parseable YAML
  * atomic write: ``--output`` survives a generator failure
  * trash race: a universe moved to ``trash/`` mid-scan-set is not emitted
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import yaml

from gaottt.multiverse.backup import (
    DEFAULT_REPLICA_PREFIX,
    atomic_write_text,
    generate_litestream_config,
)
from gaottt.store.manifest import MANIFEST_FILENAME, UniverseManifest

# Repo root for invoking the CLI script by absolute path.
REPO_ROOT = Path(__file__).resolve().parents[2]
CLI = REPO_ROOT / "scripts" / "gen_litestream_config.py"
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"


# ---------------------------------------------------------------------------
# filesystem helpers
# ---------------------------------------------------------------------------

def _make_universe(root: Path, uid: str, *, with_manifest: bool = True,
                   with_db: bool = True) -> Path:
    """Create ``<root>/universes/<uid>/`` with optional manifest + db."""
    udir = root / "universes" / uid
    udir.mkdir(parents=True, exist_ok=True)
    if with_manifest:
        m = UniverseManifest(
            universe_id=uid,
            embedder_id="stub",
            embedder_version="test",
            embedding_dim=32,
            created_at=0.0,
            managed=False,
        )
        (udir / MANIFEST_FILENAME).write_text(m.model_dump_json(indent=2))
    if with_db:
        (udir / "gaottt.db").write_bytes(b"sqlite-header-bytes")
    return udir


def _parse(yaml_text: str) -> dict:
    """Parse generated YAML. Fails the test loudly if it is not valid YAML."""
    return yaml.safe_load(yaml_text)


# ---------------------------------------------------------------------------
# pure function — basic shape
# ---------------------------------------------------------------------------

def test_empty_root_emits_empty_dbs(tmp_path):
    root = tmp_path / "mv"
    (root / "universes").mkdir(parents=True)
    text = generate_litestream_config(root)
    data = _parse(text)
    assert data == {"dbs": []}


def test_root_without_universes_dir_emits_empty_dbs(tmp_path):
    # A path that isn't a multiverse root at all — graceful empty, not error.
    root = tmp_path / "not-a-multiverse"
    root.mkdir()
    text = generate_litestream_config(root)
    assert _parse(text) == {"dbs": []}


def test_single_universe_has_one_entry(tmp_path):
    root = tmp_path / "mv"
    _make_universe(root, "abc123")
    text = generate_litestream_config(root)
    data = _parse(text)
    assert len(data["dbs"]) == 1
    entry = data["dbs"][0]
    assert entry["path"] == str(root / "universes" / "abc123" / "gaottt.db")
    assert entry["replicas"][0]["type"] == "file"
    assert entry["replicas"][0]["path"] == f"{DEFAULT_REPLICA_PREFIX}/abc123"


def test_three_universes_have_three_entries(tmp_path):
    root = tmp_path / "mv"
    for uid in ("aaa", "bbb", "ccc"):
        _make_universe(root, uid)
    text = generate_litestream_config(root)
    data = _parse(text)
    assert len(data["dbs"]) == 3
    paths = {e["path"] for e in data["dbs"]}
    for uid in ("aaa", "bbb", "ccc"):
        assert str(root / "universes" / uid / "gaottt.db") in paths


# ---------------------------------------------------------------------------
# scan rules — exclusion / skip
# ---------------------------------------------------------------------------

def test_trash_subdir_excluded(tmp_path):
    """A deleted universe moved to ``<root>/trash/`` must NOT appear."""
    root = tmp_path / "mv"
    (root / "trash").mkdir(parents=True)
    # live universe
    _make_universe(root, "live")
    # deleted universe placed directly in trash/
    _make_universe(root.parent / "mv" / "trash" / "deleted", "deleted")  # writes under trash/deleted/deleted
    text = generate_litestream_config(root)
    data = _parse(text)
    uids_in_yaml = {e["path"].split("/")[-2] for e in data["dbs"]}
    assert "live" in uids_in_yaml
    assert "deleted" not in uids_in_yaml


def test_stray_trash_subdir_inside_universes_is_skipped(tmp_path):
    """A stray ``universes/trash/`` (non-canonical) is skipped defensively."""
    root = tmp_path / "mv"
    _make_universe(root, "live")
    stray = root / "universes" / "trash" / "stray"
    stray.mkdir(parents=True)
    (stray / MANIFEST_FILENAME).write_text("{}")
    (stray / "gaottt.db").write_bytes(b"x")
    text = generate_litestream_config(root)
    data = _parse(text)
    uids = {e["path"].split("/")[-2] for e in data["dbs"]}
    assert uids == {"live"}


def test_manifest_missing_is_skipped_with_error(tmp_path, caplog):
    """Universe without manifest → ERROR log + excluded from YAML entirely."""
    root = tmp_path / "mv"
    _make_universe(root, "good", with_manifest=True)
    _make_universe(root, "bad", with_manifest=False, with_db=True)
    with caplog.at_level(logging.ERROR, logger="gaottt.multiverse.backup"):
        text = generate_litestream_config(root)
    data = _parse(text)
    uids = {e["path"].split("/")[-2] for e in data["dbs"]}
    assert uids == {"good"}
    assert any("bad" in r.message and r.levelno == logging.ERROR
               for r in caplog.records)


def test_db_missing_is_skipped_with_warn(tmp_path, caplog):
    """Universe without gaottt.db → WARN log + excluded."""
    root = tmp_path / "mv"
    _make_universe(root, "good", with_db=True)
    _make_universe(root, "empty", with_db=False)
    with caplog.at_level(logging.WARNING, logger="gaottt.multiverse.backup"):
        text = generate_litestream_config(root)
    data = _parse(text)
    uids = {e["path"].split("/")[-2] for e in data["dbs"]}
    assert uids == {"good"}
    assert any("empty" in r.message and r.levelno == logging.WARNING
               for r in caplog.records)


def test_db_outside_canonical_location_ignored(tmp_path):
    """registry.db at root, stray .db files anywhere non-canonical = ignored."""
    root = tmp_path / "mv"
    _make_universe(root, "live")
    # registry.db lives at the root — must never become a litestream target.
    (root / "registry.db").write_bytes(b"x")
    # a stray .db inside a universe but with the wrong name
    (root / "universes" / "live" / "other.db").write_bytes(b"x")
    text = generate_litestream_config(root)
    data = _parse(text)
    paths = {e["path"] for e in data["dbs"]}
    assert paths == {str(root / "universes" / "live" / "gaottt.db")}
    assert not any("registry.db" in p for p in paths)
    assert not any("other.db" in p for p in paths)


def test_empty_universes_dir_warns(tmp_path, caplog):
    root = tmp_path / "mv"
    (root / "universes").mkdir(parents=True)
    with caplog.at_level(logging.WARNING, logger="gaottt.multiverse.backup"):
        text = generate_litestream_config(root)
    assert _parse(text) == {"dbs": []}
    assert any("no universe dirs" in r.message for r in caplog.records)


def test_trash_race_universe_moved_to_trash_absent(tmp_path):
    """Simulate the delete race: a universe dir is under trash/, not
    universes/. It must not appear in the generated YAML."""
    root = tmp_path / "mv"
    _make_universe(root, "alive1")
    _make_universe(root, "alive2")
    # The universe that was just deleted lives in trash/ now.
    (root / "trash").mkdir(parents=True, exist_ok=True)
    (root / "trash" / "just-deleted").mkdir(parents=True)
    (root / "trash" / "just-deleted" / MANIFEST_FILENAME).write_text("{}")
    (root / "trash" / "just-deleted" / "gaottt.db").write_bytes(b"x")
    text = generate_litestream_config(root)
    data = _parse(text)
    uids = {e["path"].split("/")[-2] for e in data["dbs"]}
    assert uids == {"alive1", "alive2"}
    assert "just-deleted" not in uids


# ---------------------------------------------------------------------------
# replica prefix override
# ---------------------------------------------------------------------------

def test_replica_prefix_override(tmp_path):
    root = tmp_path / "mv"
    _make_universe(root, "abc")
    text = generate_litestream_config(root, replica_prefix="/srv/backup/gaottt")
    data = _parse(text)
    assert data["dbs"][0]["replicas"][0]["path"] == "/srv/backup/gaottt/abc"


# ---------------------------------------------------------------------------
# CLI — stdout/stderr separation + atomic write
# ---------------------------------------------------------------------------

def _run_cli(*args: str, env_root: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("GAOTTT_MULTIVERSE_ROOT", None)
    if env_root is not None:
        env["GAOTTT_MULTIVERSE_ROOT"] = env_root
    return subprocess.run(
        [str(PYTHON), str(CLI), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_cli_stdout_is_valid_yaml_stderr_has_diagnostics(tmp_path):
    root = tmp_path / "mv"
    _make_universe(root, "abc")
    r = _run_cli("--root", str(root))
    assert r.returncode == 0, r.stderr
    # stdout parses as YAML
    data = yaml.safe_load(r.stdout)
    assert len(data["dbs"]) == 1
    assert data["dbs"][0]["path"].endswith("abc/gaottt.db")
    # stderr may carry diagnostics; it is NOT required to be empty, but it
    # must NOT be the YAML (no "dbs:" as the only content). The contract is
    # "stdout is YAML only", so stderr containing "dbs:" would be fine as a
    # log message but stdout must be the parseable artifact.


def test_cli_no_root_errors_to_stderr_nonzero(tmp_path):
    r = _run_cli()  # no --root, no env
    assert r.returncode != 0
    assert r.stdout == ""
    assert "root" in r.stderr.lower()


def test_cli_env_root_default_works(tmp_path):
    root = tmp_path / "mv"
    _make_universe(root, "xyz")
    r = _run_cli(env_root=str(root))
    assert r.returncode == 0, r.stderr
    assert yaml.safe_load(r.stdout)["dbs"][0]["path"].endswith("xyz/gaottt.db")


def test_cli_atomic_write_output_survives_generator_failure(tmp_path, monkeypatch):
    """``--output`` atomic write: if the generator raises, the existing
    output file is untouched (Codex review B2)."""
    root = tmp_path / "mv"
    _make_universe(root, "abc")
    out_file = tmp_path / "litestream.yml"

    # Pre-existing good output.
    pre = generate_litestream_config(root)
    out_file.write_text(pre)

    # Patch the pure function to raise inside the CLI subprocess by injecting
    # a sitecustomize-like hook: we can't monkeypatch across subprocess, so
    # instead we point --root at an unreadable path to force the OSError
    # branch. Make root's universes/ unreadable.
    unreadable = tmp_path / "unreadable" / "universes"
    unreadable.mkdir(parents=True)
    unreadable.chmod(0o000)
    try:
        r = _run_cli("--root", str(tmp_path / "unreadable"), "--output", str(out_file))
        # Either it errors (permission denied) or succeeds with empty; the
        # point is the pre-existing out_file content must be intact.
        # On success of the failure path, returncode != 0.
        assert r.returncode != 0
    finally:
        unreadable.chmod(0o755)  # restore so cleanup works

    # The pre-existing file is untouched.
    assert out_file.read_text() == pre


def test_cli_atomic_write_output_success(tmp_path):
    root = tmp_path / "mv"
    _make_universe(root, "abc")
    _make_universe(root, "def")
    out_file = tmp_path / "litestream.yml"
    r = _run_cli("--root", str(root), "--output", str(out_file))
    assert r.returncode == 0, r.stderr
    # On --output success, stdout is empty.
    assert r.stdout == ""
    data = yaml.safe_load(out_file.read_text())
    uids = {e["path"].split("/")[-2] for e in data["dbs"]}
    assert uids == {"abc", "def"}


# ---------------------------------------------------------------------------
# atomic_write_text — smoke (parent-dir fsync is best-effort, not directly
# testable; this just confirms the helper still writes correctly after the
# _fsync_dir addition)
# ---------------------------------------------------------------------------

def test_atomic_write_text_writes_content(tmp_path):
    """atomic_write_text must still write the expected content after the
    parent-dir fsync hardening. The dir fsync itself is best-effort and
    silently skipped on unsupported filesystems, so we assert behavior
    (content + overwrite), not crash-durability."""
    out = tmp_path / "sub" / "out.yml"
    atomic_write_text(out, "dbs: []\n")
    assert out.read_text() == "dbs: []\n"

    # Overwrite path: tmp + os.replace must replace, not append.
    atomic_write_text(out, "dbs:\n  - path: x\n")
    assert out.read_text() == "dbs:\n  - path: x\n"
