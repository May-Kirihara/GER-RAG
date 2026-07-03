"""Control-plane configuration (MV4 WP-1, design J6 + Codex non-blocking #1).

All knobs are read from the environment via :meth:`ControlConfig.from_env`.
An empty ``admin_key`` is permitted at construction; the fail-fast check
(raise on empty admin key) lives in WP-2's ``create_app`` so that this module
stays free of side-effects and trivially unit-testable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ControlConfig"]

# Module-level default for schema_dir: the schema/ folder shipped next to this
# file. Resolved at import time so the path is stable regardless of CWD.
_DEFAULT_SCHEMA_DIR = Path(__file__).resolve().parent / "schema"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass
class ControlConfig:
    """Runtime configuration for the control plane.

    Environment variable names follow ``CONTROL_*`` (see field comments). The
    ``schema_dir`` defaults to the bundled ``schema/`` directory and is
    overridable for tests.
    """

    database_url: str  # CONTROL_DATABASE_URL (empty allowed here; WP-2 fail-fast)
    admin_key: str  # CONTROL_ADMIN_KEY (empty allowed here; WP-2 fail-fast)
    listen_host: str = "127.0.0.1"  # CONTROL_LISTEN_HOST (non-localhost rejected in WP-2)
    listen_port: int = 7881  # CONTROL_LISTEN_PORT (J2)
    db_pool_min_size: int = 2  # CONTROL_DB_POOL_MIN_SIZE
    db_pool_max_size: int = 10  # CONTROL_DB_POOL_MAX_SIZE
    schema_dir: Path = _DEFAULT_SCHEMA_DIR

    @classmethod
    def from_env(cls) -> "ControlConfig":
        """Build a config purely from the process environment.

        Does not validate admin_key / database_url — construction never raises
        for empty values. The fail-fast checks are WP-2's responsibility.
        """
        schema_dir_env = _env("CONTROL_SCHEMA_DIR", "")
        schema_dir = Path(schema_dir_env) if schema_dir_env else _DEFAULT_SCHEMA_DIR
        return cls(
            database_url=_env("CONTROL_DATABASE_URL", ""),
            admin_key=_env("CONTROL_ADMIN_KEY", ""),
            listen_host=_env("CONTROL_LISTEN_HOST", "127.0.0.1"),
            listen_port=_env_int("CONTROL_LISTEN_PORT", 7881),
            db_pool_min_size=_env_int("CONTROL_DB_POOL_MIN_SIZE", 2),
            db_pool_max_size=_env_int("CONTROL_DB_POOL_MAX_SIZE", 10),
            schema_dir=schema_dir,
        )
