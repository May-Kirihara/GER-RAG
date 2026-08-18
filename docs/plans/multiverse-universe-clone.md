# Multiverse Universe Clone — Implementation Plan

> Date: 2026-08-18
> Scope: clone one managed local universe into a new, independently routed
> universe on the same supervisor host.

## 1. Contract

Add an admin-only endpoint:

```http
POST /admin/universes/{source_universe_id}/clone
X-Admin-Key: <admin-key>
Content-Type: application/json

{"owner_label": "experiment-a", "tenant_id": "optional-control-tenant"}
```

Success returns `201` with a newly generated `universe_id`, plaintext
`api_key` (shown once), allocated `port`, and `source_universe_id`. The source
and clone share no mutable storage after the operation. Each has its own
SQLite, FAISS files, manifest, backend token, owner lease, API key, and backend
process.

`owner_label` is optional; omission derives `<source owner_label>-clone`.

## 2. Consistency boundary

Cloning is a brief source maintenance operation, not a live fuzzy copy:

1. acquire the supervisor create lock (new ID / port serialization);
2. acquire the source universe in-process spawn lock;
3. acquire `<source>/.spawn.lock` with `flock`;
4. re-read the source registry row and require `status=active`;
5. stop the source backend and wait for process exit so write-behind SQLite
   and FAISS state is flushed;
6. remove the confirmed-dead owner's `owner.lock*` bookkeeping so source can
   restart immediately after the clone;
7. run `PRAGMA wal_checkpoint(TRUNCATE)` on the stopped source so schema and
   latest writes are folded into the main SQLite file;
8. copy only the canonical seven data files;
9. run SQLite `PRAGMA integrity_check` when a DB exists;
10. write a fresh managed manifest with the new universe ID;
11. create the registry row and issue a fresh API key;
12. release locks. The next source `/route` lazily restarts its backend.

This deliberately reuses the same lock and stop semantics as delete. If a
live source backend has an unknown PID (for example after supervisor restart),
the endpoint returns `409`; it never copies from underneath an untracked
writer.

## 3. File policy

Copy only:

- `gaottt.db`, `gaottt.db-shm`, `gaottt.db-wal`
- `gaottt.faiss`, `gaottt.faiss.ids`
- `gaottt.virtual.faiss`, `gaottt.virtual.faiss.ids`

Regenerate `manifest.json`. Never copy `.spawn.lock`, `owner.lock*`,
`backend.token`, logs, backups, or temporary files. A never-started empty
universe may be cloned without a DB; the clone is initialized on first route.

## 4. Failure and rollback

- `404`: source is absent.
- `409`: source is inactive, missing on disk, lacks a valid managed manifest,
  or has an untracked live backend that cannot safely be stopped.
- `507`: insufficient target filesystem capacity.
- `500`: copy/integrity/registry failure.

Before registry commit, every failure recursively removes the partial target.
The source receives only a clean backend shutdown and SQLite WAL checkpoint;
its logical contents are unchanged. API keys, backend tokens, and lease files
are never reused.

## 5. Concurrency and observability

- The source spawn lock serializes clone against route and delete.
- The global create lock serializes ID/port allocation against create/clone.
- The registry unique indexes remain the final port/ID race backstop.
- Successful clones trigger the existing Litestream config regeneration hook.
- Control-plane accounting records a normal `universe_create` event for the
  new universe.

## 6. Verification

- unit: auth, missing/inactive source, file allowlist, new manifest/key,
  cleanup on copy failure, insufficient space, untracked backend conflict;
- concurrency: route waits behind clone's source lock;
- integration: remember in source, clone, recall from clone, then write
  different memories to source and clone and verify divergence.
