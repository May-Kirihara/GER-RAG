-- schema_migrations は migrate.py の ensure_bootstrap() が作成する（chicken-and-egg 回避、Codex B7）

CREATE TABLE IF NOT EXISTS tenants (
    tenant_id  TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- default tenant を bootstrap（Codex review #2 新 blocking #2 — tenant_id="default" の FK 解決）
INSERT INTO tenants (tenant_id, name) VALUES ('default', 'Default tenant (implicit, MV4 v1)')
ON CONFLICT (tenant_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL REFERENCES tenants(tenant_id),
    email      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, email)
);

CREATE TABLE IF NOT EXISTS hosts (
    host_id     TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,   -- SHA-256 of host token (plaintext issued once)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS universes (
    universe_id      TEXT PRIMARY KEY,
    tenant_id        TEXT NOT NULL REFERENCES tenants(tenant_id),
    owner_user_id    TEXT REFERENCES users(user_id),
    host_id          TEXT REFERENCES hosts(host_id),
    embedder_id      TEXT NOT NULL,
    embedder_version TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_universes_tenant ON universes(tenant_id);
CREATE INDEX IF NOT EXISTS idx_universes_host   ON universes(host_id);

-- usage_batches を usage_events の **前に** 作成（Codex review #2 新 blocking #1 — FK 解決順序）
CREATE TABLE IF NOT EXISTS usage_batches (
    batch_id     UUID PRIMARY KEY,
    host_id      TEXT NOT NULL REFERENCES hosts(host_id),
    window_start TIMESTAMPTZ NOT NULL,
    window_end   TIMESTAMPTZ NOT NULL,
    event_count  INTEGER NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- usage_events は tenant_id を非正規化で保持（Codex B1 — subquery index は Postgres が拒否する）
CREATE TABLE IF NOT EXISTS usage_events (
    id           BIGSERIAL PRIMARY KEY,
    batch_id     UUID NOT NULL REFERENCES usage_batches(batch_id),  -- 冪等性の単位（Codex B4）
    universe_id  TEXT NOT NULL REFERENCES universes(universe_id),
    tenant_id    TEXT NOT NULL REFERENCES tenants(tenant_id),       -- 非正規化（B1）
    host_id      TEXT NOT NULL REFERENCES hosts(host_id),
    event_type   TEXT NOT NULL,          -- 'route_resolution' | 'universe_create' | 'universe_delete' | 'universe_restore' (J1=A、原形統一)
    count        INTEGER NOT NULL DEFAULT 1,
    window_start TIMESTAMPTZ NOT NULL,
    window_end   TIMESTAMPTZ NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_usage_tenant_time ON usage_events(tenant_id, window_start);
CREATE INDEX IF NOT EXISTS idx_usage_universe_time ON usage_events(universe_id, window_start);

CREATE TABLE IF NOT EXISTS audit_log (
    id        BIGSERIAL PRIMARY KEY,
    tenant_id TEXT,
    actor     TEXT NOT NULL,             -- host_id or 'admin'
    action    TEXT NOT NULL,             -- 過去形: 'universe_created' | 'universe_deleted' | 'host_registered' | ...（audit_log.action は過去形、usage_events.event_type は原形 — 使い分け明記）
    target    TEXT,
    at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    detail    JSONB
);
CREATE INDEX IF NOT EXISTS idx_audit_tenant_time ON audit_log(tenant_id, at);
CREATE INDEX IF NOT EXISTS idx_audit_actor_time ON audit_log(actor, at);
