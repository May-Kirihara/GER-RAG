"""Pydantic v2 models for the control plane (MV4 WP-1).

Domain models mirror the SQL schema in ``schema/001_initial.sql`` field-for-
field. Request bodies are the wire shapes the WP-2 API will accept. Field
names match the SQL columns exactly so that an ``asyncpg.Record`` can be
dict-cast and fed straight into the matching model.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    # domain models
    "Tenant",
    "User",
    "Host",
    "Universe",
    "UsageBatch",
    "UsageEvent",
    "AuditLog",
    # request bodies
    "CreateTenantBody",
    "CreateUserBody",
    "RegisterHostBody",
    "RegisterUniverseBody",
    "UsageEventInput",
    "UsageBatchBody",
    "UniverseSyncEntry",
    "SyncBody",
]


# --- domain models (mirror SQL rows) ----------------------------------------


class Tenant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    name: str
    created_at: datetime | None = None  # schema default now()


class User(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    tenant_id: str
    email: str
    created_at: datetime | None = None


class Host(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_id: str
    label: str
    token_hash: str
    created_at: datetime | None = None
    revoked_at: datetime | None = None


class Universe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    universe_id: str
    tenant_id: str
    owner_user_id: str | None = None
    host_id: str | None = None
    embedder_id: str
    embedder_version: str
    status: str = "active"
    created_at: datetime | None = None


class UsageBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: UUID
    host_id: str
    window_start: datetime
    window_end: datetime
    event_count: int
    received_at: datetime | None = None


class UsageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None  # BIGSERIAL
    batch_id: UUID
    universe_id: str
    tenant_id: str
    host_id: str
    event_type: str
    count: int = 1
    window_start: datetime
    window_end: datetime
    received_at: datetime | None = None


class AuditLog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int | None = None  # BIGSERIAL
    tenant_id: str | None = None
    actor: str
    action: str
    target: str | None = None
    at: datetime | None = None
    detail: dict | None = None  # JSONB


# --- request bodies (WP-2 wire shapes) --------------------------------------


class CreateTenantBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    tenant_id: str | None = None  # server-generated when omitted


class CreateUserBody(BaseModel):
    """Body for ``POST /admin/tenants/{tid}/users`` (WP-2)."""

    model_config = ConfigDict(extra="forbid")

    email: str
    user_id: str | None = None  # server-generated when omitted


class RegisterHostBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    host_id: str | None = None  # server-generated when omitted


class RegisterUniverseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    universe_id: str
    host_id: str
    embedder_id: str
    embedder_version: str
    owner_user_id: str | None = None
    status: str = "active"


class UsageEventInput(BaseModel):
    """One event inside a usage batch push (WP-2 ``POST /hosts/{hid}/usage``)."""

    model_config = ConfigDict(extra="forbid")

    universe_id: str
    tenant_id: str
    event_type: str
    count: int = Field(default=1, ge=0)


class UsageBatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: UUID  # idempotency key (Codex B4)
    window_start: datetime
    window_end: datetime
    events: list[UsageEventInput] = Field(default_factory=list)


class UniverseSyncEntry(BaseModel):
    """One row of supervisor-side local state reported via ``POST /hosts/{hid}/sync``."""

    model_config = ConfigDict(extra="forbid")

    universe_id: str
    tenant_id: str
    host_id: str | None = None
    embedder_id: str
    embedder_version: str
    status: str = "active"


class SyncBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_universes: list[UniverseSyncEntry] = Field(default_factory=list)
    deleted_universes: list[str] = Field(default_factory=list)
