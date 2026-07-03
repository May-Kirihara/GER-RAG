"""Pydantic validation tests for control plane models (MV4 WP-1, docker-free).

Covers required-field enforcement, type coercion, and defaults for every
domain model and request body in ``control.models``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from control.models import (
    AuditLog,
    CreateTenantBody,
    RegisterHostBody,
    RegisterUniverseBody,
    SyncBody,
    Tenant,
    Universe,
    UniverseSyncEntry,
    UsageBatch,
    UsageBatchBody,
    UsageEvent,
    UsageEventInput,
    User,
    Host,
)


def _uuid() -> UUID:
    return uuid4()


def _ts() -> datetime:
    return datetime(2026, 7, 3, 10, 0, 0, tzinfo=timezone.utc)


# --- domain models ----------------------------------------------------------


class TestTenant:
    def test_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            Tenant()  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            Tenant(tenant_id="t1")  # name missing
        with pytest.raises(ValidationError):
            Tenant(name="n")  # tenant_id missing

    def test_defaults_and_types(self) -> None:
        t = Tenant(tenant_id="t1", name="Acme")
        assert t.created_at is None  # DB supplies now()
        assert t.tenant_id == "t1"

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            Tenant(tenant_id="t1", name="n", bogus=1)  # type: ignore[call-arg]


class TestUser:
    def test_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            User(user_id="u1")  # tenant_id + email missing

    def test_ok(self) -> None:
        u = User(user_id="u1", tenant_id="t1", email="a@b.c")
        assert u.created_at is None


class TestHost:
    def test_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            Host(host_id="h1", label="lab")  # token_hash missing

    def test_revoked_default_none(self) -> None:
        h = Host(host_id="h1", label="lab", token_hash="deadbeef")
        assert h.revoked_at is None
        assert h.created_at is None


class TestUniverse:
    def test_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            Universe(universe_id="u1")  # embedder fields missing

    def test_status_default_and_optionals(self) -> None:
        u = Universe(
            universe_id="u1",
            tenant_id="t1",
            embedder_id="cl-nagoya/ruri-v3-310m",
            embedder_version="abc",
        )
        assert u.status == "active"
        assert u.owner_user_id is None
        assert u.host_id is None
        assert u.created_at is None

    def test_status_override(self) -> None:
        u = Universe(
            universe_id="u1",
            tenant_id="t1",
            embedder_id="x",
            embedder_version="y",
            status="deleted",
        )
        assert u.status == "deleted"


class TestUsageBatch:
    def test_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            UsageBatch(batch_id=_uuid(), host_id="h1")  # window_*/event_count missing

    def test_uuid_coercion_from_string(self) -> None:
        s = "550e8400-e29b-41d4-a716-446655440000"
        b = UsageBatch(
            batch_id=s,  # type: ignore[arg-type]
            host_id="h1",
            window_start=_ts(),
            window_end=_ts(),
            event_count="3",  # type: ignore[arg-type]  # coerced to int
        )
        assert b.batch_id == UUID(s)
        assert b.event_count == 3
        assert b.received_at is None


class TestUsageEvent:
    def test_count_default(self) -> None:
        ev = UsageEvent(
            batch_id=_uuid(),
            universe_id="u1",
            tenant_id="t1",
            host_id="h1",
            event_type="route_resolution",
            window_start=_ts(),
            window_end=_ts(),
        )
        assert ev.count == 1  # SQL default mirrored
        assert ev.id is None

    def test_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            UsageEvent(batch_id=_uuid())  # type: ignore[call-arg]


class TestAuditLog:
    def test_required_actor_action(self) -> None:
        with pytest.raises(ValidationError):
            AuditLog()  # type: ignore[call-arg]

    def test_detail_default_none(self) -> None:
        a = AuditLog(actor="admin", action="universe_created")
        assert a.detail is None
        assert a.tenant_id is None
        assert a.target is None
        assert a.id is None

    def test_detail_accepts_dict(self) -> None:
        a = AuditLog(actor="admin", action="x", detail={"k": 1})
        assert a.detail == {"k": 1}


# --- request bodies ---------------------------------------------------------


class TestCreateTenantBody:
    def test_name_required(self) -> None:
        with pytest.raises(ValidationError):
            CreateTenantBody()  # type: ignore[call-arg]

    def test_tenant_id_optional(self) -> None:
        b = CreateTenantBody(name="Acme")
        assert b.tenant_id is None


class TestRegisterHostBody:
    def test_label_required(self) -> None:
        with pytest.raises(ValidationError):
            RegisterHostBody()  # type: ignore[call-arg]

    def test_host_id_optional(self) -> None:
        b = RegisterHostBody(label="host-1")
        assert b.host_id is None


class TestRegisterUniverseBody:
    def test_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            RegisterUniverseBody(tenant_id="t1")  # missing many

    def test_owner_optional_status_default(self) -> None:
        b = RegisterUniverseBody(
            tenant_id="t1",
            universe_id="u1",
            host_id="h1",
            embedder_id="e",
            embedder_version="v",
        )
        assert b.owner_user_id is None
        assert b.status == "active"


class TestUsageEventInput:
    def test_count_default_and_floor(self) -> None:
        ev = UsageEventInput(
            universe_id="u1",
            tenant_id="t1",
            event_type="route_resolution",
        )
        assert ev.count == 1

    def test_count_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            UsageEventInput(
                universe_id="u1",
                tenant_id="t1",
                event_type="route_resolution",
                count=-1,
            )


class TestUsageBatchBody:
    def test_batch_id_required(self) -> None:
        with pytest.raises(ValidationError):
            UsageBatchBody(window_start=_ts(), window_end=_ts())  # type: ignore[call-arg]

    def test_events_default_empty(self) -> None:
        b = UsageBatchBody(
            batch_id=_uuid(), window_start=_ts(), window_end=_ts()
        )
        assert b.events == []

    def test_events_nested_validation(self) -> None:
        b = UsageBatchBody(
            batch_id=_uuid(),
            window_start=_ts(),
            window_end=_ts(),
            events=[
                {
                    "universe_id": "u1",
                    "tenant_id": "t1",
                    "event_type": "route_resolution",
                    "count": 5,
                }
            ],
        )
        assert len(b.events) == 1
        assert b.events[0].count == 5


class TestSyncBody:
    def test_defaults(self) -> None:
        b = SyncBody()
        assert b.local_universes == []
        assert b.deleted_universes == []

    def test_round_trip(self) -> None:
        b = SyncBody(
            local_universes=[
                UniverseSyncEntry(
                    universe_id="u1",
                    tenant_id="default",
                    host_id="h1",
                    embedder_id="cl-nagoya/ruri-v3-310m",
                    embedder_version="abc",
                )
            ],
            deleted_universes=["u2"],
        )
        assert b.local_universes[0].universe_id == "u1"
        assert b.deleted_universes == ["u2"]
