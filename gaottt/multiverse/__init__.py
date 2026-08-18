"""MV3+MV4 Multiverse — supervisor, registry, control client, shim supervisor-mode.

This package owns the multiverse coordination surface (a supervisor process
that runs N universe backends behind per-universe API keys). It is a pure
ops / coordination layer: it touches no physics (``gaottt/core/``) and the
feature is entirely opt-in via ``config.multiverse_root`` (empty default =
feature inert, standalone deployment bit-exact).

MV4 adds ``control_client.ControlClient`` — an httpx-based pull/push client
that syncs the local registry with the Postgres-backed control plane
(``control/`` package, a separate localhost process) and pushes usage
telemetry with an idempotent local spool for degraded-mode durability.
The control client imports only httpx + stdlib + ``gaottt.config`` /
``gaottt.multiverse.registry`` — never ``asyncpg`` (J9).
"""
