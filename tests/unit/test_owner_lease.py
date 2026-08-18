"""Owner lease (single-owner coordination) tests (test-first / RED stage).

These tests assume the ``gaottt.store.lease`` module API defined in the
owner-lease work package, and the config knobs that WP-2 adds to
``gaottt/config.py``: ``owner_lease_enabled``, ``lease_force_takeover``,
``lease_heartbeat_seconds``, ``lease_stale_seconds``. Until WP-2 lands both,
importing ``gaottt.store.lease`` raises ``ModuleNotFoundError`` and constructing
``GaOTTTConfig`` with the lease kwargs raises ``TypeError`` — the expected
test-first RED state. Once WP-2 implements the contract below, every test here
should turn GREEN with no further edits.

To keep pytest collection intact (WP-1 learning: a module-top-level import of an
unimplemented module aborts collection for the whole suite), the unimplemented
module is imported **inside each test function**. Module top-level imports are
limited to already-available libraries.

Pinned contract (what these tests assert):

    class LeaseHeldError(Exception):
        \"\"\"Raised when acquire() finds an active owner.\"\"\"

    class OwnerLease:
        def __init__(self, data_dir: Path, config: GaOTTTConfig): ...
        def acquire(self, force: bool = False) -> None: ...
        async def heartbeat_loop(self, stop_event: asyncio.Event) -> None: ...
        def release(self) -> None: ...
        @property
        def owner_id(self) -> str: ...      # uuid4().hex — this process's owner id
        @property
        def is_active(self) -> bool: ...     # read-back; True iff owner_id is ours

    lock file:  <data_dir>/owner.lock
        JSON {owner_id, pid, hostname, started_at, heartbeat_at, takeover_count}
    guard file: <data_dir>/owner.lock.guard
        (fcntl.flock(LOCK_EX) held across the stale/force critical section)

    acquire() atomicity:
      - new lock:      os.open(O_CREAT | O_WRONLY | O_EXCL); EEXIST -> read existing
      - stale / force: flock(guard) -> read -> judge -> atomic replace (tmp + os.replace)
    stale judgement: now - heartbeat_at > config.lease_stale_seconds
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import multiprocessing
import os
import socket
import threading
import time
import uuid
from pathlib import Path

import pytest

from gaottt.config import GaOTTTConfig

OWNER_LOCK_FILENAME = "owner.lock"
OWNER_GUARD_FILENAME = "owner.lock.guard"
_REQUIRED_LOCK_FIELDS = (
    "owner_id",
    "pid",
    "hostname",
    "started_at",
    "heartbeat_at",
    "takeover_count",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_config(data_dir: Path, **overrides) -> GaOTTTConfig:
    """Build a config pinned to ``data_dir`` with short lease timers.

    The lease knobs do not exist yet (RED): passing them raises ``TypeError``
    until WP-2 adds the fields.
    """
    base: dict = dict(
        data_dir=str(data_dir),
        owner_lease_enabled=True,
        lease_stale_seconds=60.0,
        lease_heartbeat_seconds=10.0,
        lease_force_takeover=False,
    )
    base.update(overrides)
    return GaOTTTConfig(**base)


def _read_lock(data_dir: Path) -> dict:
    return json.loads((Path(data_dir) / OWNER_LOCK_FILENAME).read_text("utf-8"))


def _write_lock(data_dir: Path, **fields) -> None:
    """Mutate the lock JSON in place for deterministic stale/owner simulation.

    Tests must never rely on wall-clock sleeping to age a heartbeat — rewriting
    ``heartbeat_at`` deterministically exercises the same code path without
    making the suite slow or flaky.
    """
    path = Path(data_dir) / OWNER_LOCK_FILENAME
    data = _read_lock(data_dir)
    data.update(fields)
    path.write_text(json.dumps(data), "utf-8")


def _parallel_acquire_worker(data_dir: str, queue=None) -> str:
    """Module-level (picklable) worker: try to acquire the lease.

    Returns one of ``"ok"`` / ``"held"`` / ``"error:<ExcName>"``. The outer
    try/except guarantees the parent always receives a result even in the RED
    state where ``gaottt.store.lease`` cannot be imported or the config fields
    do not yet exist. The large ``lease_stale_seconds`` keeps this race about
    fresh-create exclusivity (O_CREAT|O_EXCL), not about staleness.

    ``queue`` is an optional ``multiprocessing`` Queue used by the B3 cross-
    ``Process`` path (L669); ``pool.map`` (L380) calls with a single positional
    argument, so it must stay optional. The result is ``put`` to the queue and
    returned from a single point so the two callers never diverge.
    """
    try:
        from gaottt.store.lease import LeaseHeldError, OwnerLease

        cfg = GaOTTTConfig(
            data_dir=data_dir,
            owner_lease_enabled=True,
            lease_stale_seconds=600.0,
            lease_heartbeat_seconds=10.0,
            lease_force_takeover=False,
        )
        lease = OwnerLease(Path(data_dir), cfg)
        try:
            lease.acquire()
            result = "ok"
        except LeaseHeldError:
            result = "held"
    except Exception as exc:  # RED: missing module / missing config fields
        result = f"error:{type(exc).__name__}"

    if queue is not None:
        queue.put(result)
    return result


def _write_full_lock(
    data_dir: Path,
    *,
    owner_id: str,
    heartbeat_at: float,
    started_at: float | None = None,
    takeover_count: int = 0,
) -> None:
    """Write a complete owner.lock JSON from scratch (no prior acquire needed).

    Used by the cross-process guard TOCTOU fence (B2), which must stage a stale
    lock without going through ``OwnerLease.acquire`` — that path is RED until
    WP-2 and would also contend for the guard we are trying to hold ourselves.
    """
    path = Path(data_dir) / OWNER_LOCK_FILENAME
    payload = {
        "owner_id": owner_id,
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "started_at": started_at if started_at is not None else time.time(),
        "heartbeat_at": heartbeat_at,
        "takeover_count": takeover_count,
    }
    path.write_text(json.dumps(payload), "utf-8")


def _barrier_acquire_worker(data_dir: str, barrier, queue) -> None:
    """Module-level worker: synchronize on ``barrier`` then race ``acquire``.

    The barrier is reached only after the import + lease construction succeed,
    so every worker that hits ``barrier.wait()`` calls ``acquire()`` in the same
    instant. A non-atomic ``exists()->write_text()`` acquisition cannot rely on
    the scheduler serializing it past this burst — the simultaneous landing on
    the ``O_CREAT|O_EXCL`` path (or its absence) is exactly what this fences.

    Reports one of ``"ok"`` / ``"held"`` / ``"error:<ExcName>"`` via ``queue``.
    """
    try:
        from gaottt.store.lease import LeaseHeldError, OwnerLease

        cfg = GaOTTTConfig(
            data_dir=data_dir,
            owner_lease_enabled=True,
            lease_stale_seconds=600.0,  # keep this race about fresh-create exclusivity
            lease_heartbeat_seconds=10.0,
            lease_force_takeover=False,
        )
        lease = OwnerLease(Path(data_dir), cfg)
        barrier.wait(timeout=30.0)  # all workers released into acquire() at once
        try:
            lease.acquire()
            queue.put("ok")
        except LeaseHeldError:
            queue.put("held")
    except Exception as exc:  # RED: missing module / missing config fields
        try:
            queue.put(f"error:{type(exc).__name__}")
        except Exception:
            pass


def _guard_child_worker(data_dir: str, queue) -> None:
    """Module-level worker for the cross-process guard TOCTOU fence (B2).

    Tries to acquire a (pre-staged) stale lock. A correct implementation blocks
    on the guard flock while the parent holds it, then — once the parent
    releases the guard after rewriting a FRESH owner — re-reads under the guard,
    sees the fresh owner, and raises ``LeaseHeldError`` instead of clobbering
    it. The parent releases the guard in bounded time, so this worker never
    deadlocks.
    """
    try:
        from gaottt.store.lease import LeaseHeldError, OwnerLease

        cfg = GaOTTTConfig(
            data_dir=data_dir,
            owner_lease_enabled=True,
            lease_stale_seconds=60.0,
            lease_heartbeat_seconds=10.0,
            lease_force_takeover=False,
        )
        lease = OwnerLease(Path(data_dir), cfg)
        try:
            lease.acquire()  # blocks on guard flock until parent releases
            queue.put("ok")
        except LeaseHeldError:
            queue.put("held")
    except Exception as exc:  # RED: missing module / missing config fields
        try:
            queue.put(f"error:{type(exc).__name__}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 1. acquire on an empty dir creates the lock with all required fields
# ---------------------------------------------------------------------------

def test_acquire_new_creates_lock(tmp_path: Path):
    """A first acquire creates owner.lock with the full owner bookkeeping."""
    from gaottt.store.lease import OwnerLease

    lease = OwnerLease(tmp_path, _make_config(tmp_path))
    lease.acquire()

    lock_path = tmp_path / OWNER_LOCK_FILENAME
    assert lock_path.exists(), "owner.lock must be created on first acquire"

    data = _read_lock(tmp_path)
    for field in _REQUIRED_LOCK_FIELDS:
        assert field in data, f"lock JSON missing required field: {field}"

    # The persisted owner_id must agree with the lease's own id, and the
    # process bookkeeping must reflect the current process.
    assert data["owner_id"] == lease.owner_id
    assert data["pid"] == os.getpid()
    assert data["hostname"] == socket.gethostname()
    # A fresh lock is the first owner, not a takeover.
    assert data["takeover_count"] == 0


# ---------------------------------------------------------------------------
# 2. a second owner on an active (fresh) lock is rejected
# ---------------------------------------------------------------------------

def test_acquire_conflict_raises(tmp_path: Path):
    """An active fresh lock must reject a second owner with LeaseHeldError."""
    from gaottt.store.lease import LeaseHeldError, OwnerLease

    first = OwnerLease(tmp_path, _make_config(tmp_path))
    first.acquire()

    second = OwnerLease(tmp_path, _make_config(tmp_path))
    with pytest.raises(LeaseHeldError):
        second.acquire()


# ---------------------------------------------------------------------------
# 3. a stale lock is taken over, the counter increments, and it is logged
# ---------------------------------------------------------------------------

def test_stale_takeover(tmp_path: Path, caplog):
    """An owner whose heartbeat exceeds the stale threshold is superseded."""
    from gaottt.store.lease import OwnerLease

    cfg = _make_config(tmp_path, lease_stale_seconds=60.0)
    first = OwnerLease(tmp_path, cfg)
    first.acquire()
    before = _read_lock(tmp_path)

    # Deterministically age the heartbeat past the threshold (no sleeping).
    _write_lock(tmp_path, heartbeat_at=time.time() - (cfg.lease_stale_seconds + 5.0))

    second = OwnerLease(tmp_path, cfg)
    with caplog.at_level(logging.WARNING):
        second.acquire()

    after = _read_lock(tmp_path)
    assert after["takeover_count"] == before["takeover_count"] + 1
    assert after["owner_id"] == second.owner_id

    # A takeover is an administrative action; it must be visible in the logs.
    warning_msgs = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert any(
        "stale" in msg.lower() or "takeover" in msg.lower() for msg in warning_msgs
    ), f"expected stale/takeover warning: {warning_msgs}"


# ---------------------------------------------------------------------------
# 4. lease_force_takeover wins even against a fresh lock
# ---------------------------------------------------------------------------

def test_force_takeover(tmp_path: Path):
    """lease_force_takeover=True supersedes an otherwise-active owner."""
    from gaottt.store.lease import OwnerLease

    # First owner holds a FRESH lock (heartbeat not aged).
    first = OwnerLease(tmp_path, _make_config(tmp_path))
    first.acquire()

    # A second owner with force enabled wins despite the fresh heartbeat.
    forced_cfg = _make_config(tmp_path, lease_force_takeover=True)
    second = OwnerLease(tmp_path, forced_cfg)
    second.acquire()  # must not raise

    assert _read_lock(tmp_path)["owner_id"] == second.owner_id


# ---------------------------------------------------------------------------
# 5. release deletes our own lock
# ---------------------------------------------------------------------------

def test_release_deletes_own_lock(tmp_path: Path):
    """Releasing a lock we own removes owner.lock from disk."""
    from gaottt.store.lease import OwnerLease

    lease = OwnerLease(tmp_path, _make_config(tmp_path))
    lease.acquire()
    assert (tmp_path / OWNER_LOCK_FILENAME).exists()

    lease.release()
    assert not (tmp_path / OWNER_LOCK_FILENAME).exists()


# ---------------------------------------------------------------------------
# 6. release must NOT delete a lock we no longer own
# ---------------------------------------------------------------------------

def test_release_does_not_delete_other(tmp_path: Path):
    """release() never deletes a lock whose owner_id is no longer ours."""
    from gaottt.store.lease import OwnerLease

    lease = OwnerLease(tmp_path, _make_config(tmp_path))
    lease.acquire()

    # Another owner takes over the lock out from under us (owner_id rewritten).
    _write_lock(tmp_path, owner_id=uuid.uuid4().hex)
    lock_path = tmp_path / OWNER_LOCK_FILENAME

    lease.release()
    # Deleting another owner's lease would break single-owner safety.
    assert lock_path.exists(), "release must not delete another owner's lock"


# ---------------------------------------------------------------------------
# 7. parallel acquire — exactly one process wins (repeated; race is probabilistic)
# ---------------------------------------------------------------------------

def test_parallel_acquire_only_one_wins(tmp_path: Path):
    """N processes racing a fresh acquire: exactly one wins, the rest are held."""
    data_dir = str(tmp_path)
    ctx = multiprocessing.get_context("spawn")

    for iteration in range(3):  # Codex review: repeat, races are probabilistic
        # Each iteration starts from a clean directory (lock + guard + scratch).
        for stale_file in tmp_path.glob("owner.lock*"):
            stale_file.unlink()

        with ctx.Pool(processes=5) as pool:
            results = pool.map(_parallel_acquire_worker, [data_dir] * 5)

        winners = [r for r in results if r == "ok"]
        held = [r for r in results if r == "held"]
        assert len(winners) == 1, (
            f"iteration {iteration}: expected exactly 1 winner, got "
            f"{len(winners)}; results={results}"
        )
        assert len(held) == 4, (
            f"iteration {iteration}: expected 4 held, results={results}"
        )


# ---------------------------------------------------------------------------
# 8. is_active read-back detects an external owner change
# ---------------------------------------------------------------------------

def test_is_active_detects_owner_change(tmp_path: Path):
    """is_active re-reads the lock; an external owner change flips it to False."""
    from gaottt.store.lease import OwnerLease

    lease = OwnerLease(tmp_path, _make_config(tmp_path))
    lease.acquire()
    assert lease.is_active is True

    # An external takeover rewrites the owner_id out from under us.
    _write_lock(tmp_path, owner_id=uuid.uuid4().hex)

    assert lease.is_active is False


# ---------------------------------------------------------------------------
# 9. the guard-file flock serializes concurrent stale takeovers
# ---------------------------------------------------------------------------

def test_guard_file_flock_serializes_takeover(tmp_path: Path):
    """Two threads racing a stale takeover: flock serializes so only one wins."""
    from gaottt.store.lease import LeaseHeldError, OwnerLease

    cfg = _make_config(tmp_path, lease_stale_seconds=60.0)
    first = OwnerLease(tmp_path, cfg)
    first.acquire()

    # Age the heartbeat so both contenders observe a stale, takeable lock.
    _write_lock(tmp_path, heartbeat_at=time.time() - (cfg.lease_stale_seconds + 5.0))
    before = _read_lock(tmp_path)["takeover_count"]

    results: list[str] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(2)  # release both contenders into acquire() together

    def contend() -> None:
        # Each thread owns its own OwnerLease -> its own guard-fd, so flock
        # excludes the other across the read->judge->replace critical section.
        lease = OwnerLease(tmp_path, cfg)
        barrier.wait()
        try:
            lease.acquire()
            outcome = "ok"
        except LeaseHeldError:
            outcome = "held"
        with results_lock:
            results.append(outcome)

    t1 = threading.Thread(target=contend)
    t2 = threading.Thread(target=contend)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # The loser must see the now-fresh lock (the winner's replace landed under
    # the guard flock) and be rejected — never a double takeover.
    assert results.count("ok") == 1, f"expected exactly 1 takeover winner: {results}"
    assert results.count("held") == 1, f"expected 1 held: {results}"

    after = _read_lock(tmp_path)["takeover_count"]
    assert after == before + 1, (
        f"takeover_count must increment exactly once: before={before}, after={after}"
    )


# ===========================================================================
# Concurrency-regression fences (Codex review WP-1b)
#
# The original 9 cases above are kept intact as basic smoke. The tests below
# tighten the concurrency contract along four axes the review flagged:
#   B1 — parallel acquire with a true start barrier (not just Pool.map)
#   B2 — cross-process guard-file TOCTOU (stale-read vs. fresh-replace race)
#   B3 — release() never deletes another owner's lock, proven across processes
#   B4 — heartbeat_loop: updates heartbeat_at, detects owner loss, honors stop
# Plus edge cases (force arg, stale boundary, release/is_active robustness on
# missing/corrupt lock). All stay RED until WP-2 implements lease.py.
# ===========================================================================


# ---------------------------------------------------------------------------
# B1 — parallel acquire with a synchronization barrier (true simultaneous race)
# ---------------------------------------------------------------------------

@pytest.mark.timeout(60)
def test_parallel_acquire_with_barrier(tmp_path: Path):
    """N workers released simultaneously by a Barrier: exactly one wins.

    Unlike ``Pool.map`` (case 7), every worker calls ``acquire()`` in the same
    instant right after ``barrier.wait()``. A non-atomic ``exists()->write()``
    implementation cannot slip through serialized scheduling here — the burst
    lands on the ``O_CREAT|O_EXCL`` path all at once. Repeated 3x because the
    race is probabilistic; each iteration starts from a clean directory.
    """
    data_dir = str(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    n_workers = 5

    for iteration in range(3):
        for stale_file in tmp_path.glob("owner.lock*"):
            stale_file.unlink()

        barrier = ctx.Barrier(n_workers)
        queue = ctx.Queue()
        procs = [
            ctx.Process(
                target=_barrier_acquire_worker, args=(data_dir, barrier, queue)
            )
            for _ in range(n_workers)
        ]
        for p in procs:
            p.start()

        # Bounded join — a wedged worker must fail the suite, not hang it.
        deadline = time.time() + 40.0
        for p in procs:
            remaining = max(1.0, deadline - time.time())
            p.join(timeout=remaining)
        alive = [p for p in procs if p.is_alive()]
        for p in alive:  # last-resort cleanup so the suite can report
            p.terminate()

        results = []
        for _ in range(n_workers):
            try:
                results.append(queue.get(timeout=5.0))
            except Exception:
                results.append("<missing>")

        assert not alive, (
            f"iteration {iteration}: workers still alive (deadlock?): "
            f"{[p.pid for p in alive]}; results={results}"
        )
        winners = [r for r in results if r == "ok"]
        held = [r for r in results if r == "held"]
        assert len(winners) == 1, (
            f"iteration {iteration}: expected exactly 1 winner, got "
            f"{len(winners)}; results={results}"
        )
        assert len(held) == 4, (
            f"iteration {iteration}: expected 4 held, got {len(held)}; "
            f"results={results}"
        )


# ---------------------------------------------------------------------------
# B2 — cross-process guard-file TOCTOU (stale-read vs. fresh-replace race)
# ---------------------------------------------------------------------------

@pytest.mark.timeout(40)
def test_cross_process_guard_toctou(tmp_path: Path):
    """The guard flock closes the stale-read -> fresh-replace TOCTOU window.

    Parent holds ``owner.lock.guard`` under ``LOCK_EX``; a child trying to
    acquire a pre-staged STALE lock blocks on the guard. While the child is
    blocked, the parent rewrites ``owner.lock`` to a FRESH owner (simulating
    another process having just taken over), THEN releases the guard. The child,
    on obtaining the guard, must RE-READ and see the fresh owner -> raise
    ``LeaseHeldError`` and leave the lock untouched. A buggy implementation
    that commits to the stale read before the guard would clobber the fresh
    owner — this test fails on that regression.

    Timeout safety: the parent releases the guard in bounded time, so the child
    never deadlocks; ``join(timeout=...)`` plus the per-test deadline backstop
    any broken implementation.
    """
    data_dir = str(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()

    # Stage a STALE lock owned by a long-gone owner (no acquire needed, so this
    # works even in RED and avoids contending for the guard we hold next).
    stale_owner = uuid.uuid4().hex
    _write_full_lock(
        tmp_path,
        owner_id=stale_owner,
        heartbeat_at=time.time() - 65.0,  # > child's lease_stale_seconds(60)
    )

    guard_path = tmp_path / OWNER_GUARD_FILENAME
    guard_path.touch()

    # Parent takes the guard flock and holds it across the critical section.
    guard_fd = os.open(str(guard_path), os.O_RDWR)
    try:
        fcntl.flock(guard_fd, fcntl.LOCK_EX)

        child = ctx.Process(target=_guard_child_worker, args=(data_dir, queue))
        child.start()

        # Let the child reach its blocking flock on the guard.
        time.sleep(0.5)

        # Rewrite the lock to a FRESH owner — exactly what a competing takeover
        # would do mid-flight. The child must NOT observe this half-replace; it
        # is blocked on the guard we hold.
        fresh_owner = uuid.uuid4().hex
        _write_full_lock(
            tmp_path, owner_id=fresh_owner, heartbeat_at=time.time()
        )

        # Release the guard: the child may now proceed and re-read.
        fcntl.flock(guard_fd, fcntl.LOCK_UN)
    finally:
        os.close(guard_fd)

    child.join(timeout=20.0)
    if child.is_alive():  # broken impl never unblocked
        child.terminate()
        child.join(timeout=5.0)

    try:
        outcome = queue.get(timeout=5.0)
    except Exception:
        outcome = "<missing>"

    assert not child.is_alive(), (
        "child did not unblock after guard release (deadlock on guard?)"
    )

    # The child saw the FRESH owner under the guard -> rejected, never having
    # replaced the lock.
    assert outcome == "held", (
        f"expected child to see fresh owner and raise LeaseHeldError "
        f"(outcome='held'); got {outcome!r} — guard did not close the TOCTOU"
    )

    final = _read_lock(tmp_path)
    assert final["owner_id"] == fresh_owner, (
        "child must not have clobbered the fresh owner's lock"
    )


# ---------------------------------------------------------------------------
# B3 — release() TOCTOU: never deletes another owner's lock (cross-process)
# ---------------------------------------------------------------------------

@pytest.mark.timeout(40)
def test_release_toctou_does_not_delete_other(tmp_path: Path):
    """release() leaves another owner's stale lock intact and acquirable.

    Extends case 6 to the process level: after our ``release()`` declines to
    delete a lock we no longer own, a SEPARATE process can still acquire that
    stale lock (take it over). This proves release neither deleted another's
    lock nor left it in a corrupt or un-acquirable state.
    """
    from gaottt.store.lease import OwnerLease

    data_dir = str(tmp_path)
    cfg = _make_config(tmp_path)
    lease = OwnerLease(tmp_path, cfg)
    lease.acquire()
    lock_path = tmp_path / OWNER_LOCK_FILENAME
    assert lock_path.exists()

    # Another owner takes over out from under us AND goes stale. The heartbeat
    # is aged past the worker's lease_stale_seconds(600) so a fresh acquire in a
    # child process can take it over.
    other_owner = uuid.uuid4().hex
    _write_lock(
        tmp_path,
        owner_id=other_owner,
        heartbeat_at=time.time() - 605.0,
    )

    # Our release must NOT delete: the owner_id is no longer ours.
    lease.release()
    assert lock_path.exists(), "release deleted a lock we do not own"
    assert _read_lock(tmp_path)["owner_id"] == other_owner

    # A separate process can still acquire (take over) the intact stale lock.
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    child = ctx.Process(target=_parallel_acquire_worker, args=(data_dir, queue))
    child.start()
    child.join(timeout=20.0)
    if child.is_alive():
        child.terminate()
        child.join(timeout=5.0)
    try:
        outcome = queue.get(timeout=5.0)
    except Exception:
        outcome = "<missing>"

    assert outcome == "ok", (
        f"separate process could not acquire after release; outcome={outcome!r}"
    )

    # The child took over: the owner changed and the counter incremented.
    final = _read_lock(tmp_path)
    assert final["owner_id"] != other_owner, (
        "child did not take over the intact stale lock"
    )
    assert final["takeover_count"] == 1, (
        f"expected takeover_count==1 after child takeover, got "
        f"{final['takeover_count']}"
    )


# ---------------------------------------------------------------------------
# B4 — heartbeat_loop contract (async)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.timeout(20)
async def test_heartbeat_updates_heartbeat_at(tmp_path: Path):
    """heartbeat_loop advances the persisted heartbeat_at on each tick."""
    from gaottt.store.lease import OwnerLease

    cfg = _make_config(tmp_path, lease_heartbeat_seconds=0.05)
    lease = OwnerLease(tmp_path, cfg)
    lease.acquire()
    before = _read_lock(tmp_path)["heartbeat_at"]

    stop = asyncio.Event()
    task = asyncio.create_task(lease.heartbeat_loop(stop))
    # ~4 tick intervals at 0.05s — comfortably past float clock resolution.
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, timeout=5.0)  # broken loop -> TimeoutError -> fail

    after = _read_lock(tmp_path)["heartbeat_at"]
    assert after > before, (
        f"heartbeat_at not advanced by loop: before={before}, after={after}"
    )


@pytest.mark.asyncio
@pytest.mark.timeout(20)
async def test_heartbeat_detects_owner_loss(tmp_path: Path, caplog):
    """On detecting a foreign owner the loop logs ERROR and stops updating; it
    must NOT overwrite the other owner's lock."""
    from gaottt.store.lease import OwnerLease

    cfg = _make_config(tmp_path, lease_heartbeat_seconds=0.05)
    lease = OwnerLease(tmp_path, cfg)
    lease.acquire()

    # External takeover: rewrite owner_id under us.
    foreign = uuid.uuid4().hex
    _write_lock(tmp_path, owner_id=foreign)

    stop = asyncio.Event()
    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(lease.heartbeat_loop(stop))
        await asyncio.sleep(0.3)  # let the next tick read back and detect
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)

    assert lease.is_active is False, "lease should be inactive after owner loss"

    error_msgs = [
        r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR
    ]
    assert error_msgs, "expected an ERROR log when heartbeat detects owner loss"

    # The foreign owner's lock must be untouched.
    assert _read_lock(tmp_path)["owner_id"] == foreign, (
        "heartbeat_loop must not overwrite another owner's lock"
    )


@pytest.mark.asyncio
@pytest.mark.timeout(20)
async def test_heartbeat_stop_event(tmp_path: Path):
    """stop_event.set() terminates heartbeat_loop promptly (no hang)."""
    from gaottt.store.lease import OwnerLease

    cfg = _make_config(tmp_path, lease_heartbeat_seconds=0.05)
    lease = OwnerLease(tmp_path, cfg)
    lease.acquire()

    stop = asyncio.Event()
    task = asyncio.create_task(lease.heartbeat_loop(stop))
    stop.set()
    # If the loop ignores stop_event, wait_for raises TimeoutError -> test fails.
    await asyncio.wait_for(task, timeout=5.0)
    assert task.done()


# ---------------------------------------------------------------------------
# Edge cases (Codex missing-tests, non-blocking)
# ---------------------------------------------------------------------------

def test_acquire_force_arg_contract(tmp_path: Path):
    """acquire(force=True) supersedes a fresh lock independent of the config knob.

    Case 4 covers ``lease_force_takeover=True`` (config default). This pins the
    per-call ``force`` argument: with the config knob OFF, the arg still wins.
    """
    from gaottt.store.lease import OwnerLease

    first = OwnerLease(tmp_path, _make_config(tmp_path, lease_force_takeover=False))
    first.acquire()

    second = OwnerLease(tmp_path, _make_config(tmp_path, lease_force_takeover=False))
    second.acquire(force=True)  # must not raise despite config=False

    assert _read_lock(tmp_path)["owner_id"] == second.owner_id


def test_stale_boundary(tmp_path: Path):
    """Boundary: now - heartbeat_at == stale_seconds is NOT stale; only > is.

    The margin inside the not-stale side comfortably exceeds any acquire()-side
    wall-clock drift, so a correct strict ``>`` judgement holds the lock while
    an off-by-one ``>=`` would wrongly take it over.
    """
    from gaottt.store.lease import LeaseHeldError, OwnerLease

    cfg = _make_config(tmp_path, lease_stale_seconds=2.0)
    first = OwnerLease(tmp_path, cfg)
    first.acquire()

    # 1.0s inside the not-stale side of the 2.0s boundary.
    _write_lock(tmp_path, heartbeat_at=time.time() - (cfg.lease_stale_seconds - 1.0))

    second = OwnerLease(tmp_path, cfg)
    with pytest.raises(LeaseHeldError):
        second.acquire()


def test_release_before_acquire(tmp_path: Path):
    """release() before any acquire is a safe no-op."""
    from gaottt.store.lease import OwnerLease

    lease = OwnerLease(tmp_path, _make_config(tmp_path))
    lease.release()  # must not raise
    assert not (tmp_path / OWNER_LOCK_FILENAME).exists()


def test_release_missing_lock(tmp_path: Path):
    """release() tolerates the lock having vanished externally."""
    from gaottt.store.lease import OwnerLease

    lease = OwnerLease(tmp_path, _make_config(tmp_path))
    lease.acquire()
    (tmp_path / OWNER_LOCK_FILENAME).unlink()  # external removal
    lease.release()  # must not raise


def test_is_active_missing_lock(tmp_path: Path):
    """is_active is False when owner.lock is absent."""
    from gaottt.store.lease import OwnerLease

    lease = OwnerLease(tmp_path, _make_config(tmp_path))
    assert lease.is_active is False


def test_is_active_corrupt_json(tmp_path: Path):
    """is_active is False when owner.lock is corrupt (cannot be our owner)."""
    from gaottt.store.lease import OwnerLease

    lease = OwnerLease(tmp_path, _make_config(tmp_path))
    lease.acquire()
    (tmp_path / OWNER_LOCK_FILENAME).write_text("{not valid json", "utf-8")
    assert lease.is_active is False
