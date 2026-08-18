"""``python -m control`` entrypoint — run the control plane via uvicorn.

Reads :class:`~control.config.ControlConfig` from the environment, builds the
FastAPI app (which fails fast on an empty admin key or a non-localhost bind),
and serves it. The non-localhost :class:`SystemExit` fires inside
:func:`create_app` *before* uvicorn attempts to bind.
"""

from __future__ import annotations


def main() -> None:
    import uvicorn

    from .api import create_app
    from .config import ControlConfig

    config = ControlConfig.from_env()
    app = create_app(config)
    uvicorn.run(app, host=config.listen_host, port=config.listen_port)


if __name__ == "__main__":  # pragma: no cover - manual / systemd launch
    main()
