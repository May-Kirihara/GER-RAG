"""GaOTTT control plane package (MV4).

Independent Postgres-backed aggregator / audit / billing collection point.
Does NOT depend on the ``gaottt`` engine package (J9). Communication between
the supervisor and this control plane is HTTP-only.
"""

__all__ = ["__version__", "create_app"]

__version__ = "0.1.0"


def __getattr__(name: str):  # PEP 562 — lazy re-export to avoid import cost.
    if name == "create_app":
        from .api import create_app as _create_app

        return _create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
