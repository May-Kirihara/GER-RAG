"""GaOTTT service layer — shared between MCP and REST servers.

Each module exposes async functions that take an engine (or use the runtime
singleton) and return Pydantic models. Transport-specific wrappers live in
``gaottt/server/`` and are thin: MCP pipes the result through
``services.formatters``; REST returns the Pydantic model as JSON.
"""
