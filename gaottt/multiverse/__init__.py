"""MV3 Multiverse — supervisor, registry, and shim supervisor-mode (WP-1..WP-4).

This package owns the multiverse coordination surface (a supervisor process
that runs N universe backends behind per-universe API keys). It is a pure
ops / coordination layer: it touches no physics (``gaottt/core/``) and the
feature is entirely opt-in via ``config.multiverse_root`` (empty default =
feature inert, standalone deployment bit-exact).
"""
