"""End-to-end smoke test for the GaOTTT REST API.

Boots uvicorn against an **isolated** DB (default ``/tmp/gaottt-rest-smoke``)
so production memory at ``~/.local/share/gaottt/`` is never touched, then
walks through six realistic user scenarios:

  1. Persona onboarding (value → intention → commitment → /persona)
  2. Knowledge curation (remember → recall with source_filter → reflect/summary)
  3. Task lifecycle (commit → start → complete → reflect/tasks_completed)
  4. Knowledge revision (relate supersedes → get_relations → reflect/relations)
  5. Maintenance (hypothesis with TTL → compact → merge near-duplicates)
  6. Forget / restore round-trip

Each scenario prints PASS/FAIL with details. Exit code is 0 iff all pass.

Usage::

    .venv/bin/python scripts/rest_smoke.py            # default port 8766, isolated /tmp dir
    .venv/bin/python scripts/rest_smoke.py --port 9000
    .venv/bin/python scripts/rest_smoke.py --keep-server   # leave running on exit (manual poke)
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx


# --- ANSI colors (no extra dep) -----------------------------------------

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
DIM = "\033[2m"
RESET = "\033[0m"


def step(label: str) -> None:
    print(f"\n{YELLOW}▸ {label}{RESET}")


def passed(label: str, detail: str = "") -> None:
    line = f"  {GREEN}PASS{RESET}  {label}"
    if detail:
        line += f"  {DIM}{detail}{RESET}"
    print(line)


def failed(label: str, detail: str) -> None:
    print(f"  {RED}FAIL{RESET}  {label}\n    {detail}")


# --- Server lifecycle ---------------------------------------------------

def start_uvicorn(port: int, data_dir: Path, log_path: Path) -> subprocess.Popen:
    project_root = Path(__file__).resolve().parent.parent
    python = project_root / ".venv" / "bin" / "python"
    if not python.exists():
        print(f"{RED}ERROR{RESET}: {python} not found. Activate the uv venv first.")
        sys.exit(2)

    env = os.environ.copy()
    env["GAOTTT_DATA_DIR"] = str(data_dir)
    env.pop("GAOTTT_CONFIG", None)
    env.pop("GER_RAG_CONFIG", None)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "w")  # noqa: SIM115 — kept open for the subprocess lifetime
    proc = subprocess.Popen(
        [str(python), "-m", "uvicorn", "gaottt.server.app:app",
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=log_fh, stderr=subprocess.STDOUT, env=env, cwd=str(project_root),
    )
    return proc


def wait_ready(url: str, proc: subprocess.Popen, timeout_s: int = 60) -> bool:
    for _ in range(timeout_s):
        if proc.poll() is not None:
            return False
        try:
            r = httpx.get(f"{url}/docs", timeout=2.0)
            if r.status_code == 200:
                return True
        except httpx.RequestError:
            pass
        time.sleep(1)
    return False


def stop_server(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


# --- Scenarios ----------------------------------------------------------

class Scenario:
    """Holds per-scenario PASS/FAIL state without raising on first failure.

    Each scenario prints its own progress; the runner aggregates the count.
    """

    def __init__(self, name: str, client: httpx.Client) -> None:
        self.name = name
        self.client = client
        self.fail_count = 0

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        if condition:
            passed(label, detail)
        else:
            failed(label, detail or "(condition False)")
            self.fail_count += 1


def scenario_persona_onboarding(client: httpx.Client) -> Scenario:
    s = Scenario("Persona onboarding (value → intention → commitment → /persona)", client)
    step(s.name)

    r = client.post("/persona/values",
                    json={"content": "build memory that respects the user's voice"})
    value_id = r.json().get("id")
    s.check("declare value", r.status_code == 200 and bool(value_id), f"id={value_id}")

    r = client.post("/persona/intentions",
                    json={"content": "ship REST parity in S5", "parent_value_id": value_id})
    intention_id = r.json().get("id")
    s.check("declare intention from value",
            r.status_code == 200 and bool(intention_id),
            f"id={intention_id} parent_value_id={r.json().get('parent_value_id')}")

    r = client.post("/persona/commitments", json={
        "content": "land /tasks/{id}/* simplification this week",
        "parent_intention_id": intention_id,
        "deadline_seconds": 604800,
    })
    commit_id = r.json().get("id")
    s.check("declare commitment with deadline",
            r.status_code == 200 and bool(commit_id) and r.json().get("expires_at") is not None,
            f"id={commit_id} expires_at={r.json().get('expires_at')}")

    r = client.get("/persona")
    persona = r.json()
    s.check("inherit_persona returns snapshot with value",
            r.status_code == 200 and any(v["id"] == value_id for v in persona["values"]))
    s.check("snapshot lists declared intention",
            any(i["id"] == intention_id for i in persona["intentions"]))
    s.check("snapshot lists declared commitment",
            any(c["id"] == commit_id for c in persona["commitments"]))
    return s


def scenario_knowledge_curation(client: httpx.Client) -> Scenario:
    s = Scenario("Knowledge curation (remember → recall + source_filter → reflect/summary)", client)
    step(s.name)

    facts = [
        {"content": "user prefers uv over pip for python tooling", "source": "user", "tags": ["pref"]},
        {"content": "agent uses gravitational recall to bias retrieval", "source": "agent"},
        {"content": "hypothesis: emotion magnitude can boost dormant recall", "source": "hypothesis"},
    ]
    ids: list[str] = []
    for body in facts:
        r = client.post("/remember", json=body)
        nid = r.json().get("id")
        s.check(f"remember source={body['source']}", r.status_code == 200 and bool(nid), f"id={nid}")
        if nid:
            ids.append(nid)

    r = client.post("/recall", json={"query": "uv pip python", "top_k": 5})
    items = r.json().get("items", [])
    s.check("recall surfaces user pref",
            r.status_code == 200
            and any("uv over pip" in it["content"] for it in items),
            f"got {len(items)} items, first source={items[0]['source'] if items else '-'}")

    r = client.post("/recall", json={
        "query": "recall", "top_k": 5, "source_filter": ["user"],
    })
    items = r.json().get("items", [])
    s.check("source_filter=['user'] returns only user-sourced items",
            all(it["source"] == "user" for it in items))

    r = client.post("/reflect/summary")
    summary = r.json()
    s.check("reflect/summary returns valid envelope",
            r.status_code == 200
            and summary["total_memories"] >= len(ids)
            and isinstance(summary["sources"], dict),
            f"total={summary['total_memories']} sources={summary['sources']}")
    return s


def scenario_task_lifecycle(client: httpx.Client) -> Scenario:
    s = Scenario("Task lifecycle (commit → start → complete → reflect/tasks_completed)", client)
    step(s.name)

    r = client.post("/tasks", json={
        "content": "smoke test the new /tasks/{id}/* endpoints",
        "deadline_seconds": 3600,
    })
    task_id = r.json().get("id")
    s.check("commit task with deadline",
            r.status_code == 200 and bool(task_id),
            f"id={task_id} expires_at={r.json().get('expires_at')}")

    r = client.post(f"/tasks/{task_id}/start")
    s.check("start task refreshes TTL + emotion",
            r.status_code == 200
            and r.json().get("found") is True
            and (r.json().get("emotion_weight") or 0) > 0,
            f"emotion={r.json().get('emotion_weight')}")

    r = client.post(f"/tasks/{task_id}/complete",
                    json={"outcome": "smoke test passes end-to-end", "emotion": 0.7})
    out = r.json()
    s.check("complete task creates outcome edge",
            r.status_code == 200 and bool(out.get("outcome_id")) and out.get("task_id") == task_id,
            f"outcome_id={out.get('outcome_id')}")

    r = client.post("/reflect/tasks_completed", params={"limit": 5})
    items = r.json().get("items", [])
    s.check("reflect/tasks_completed lists the just-completed task",
            r.status_code == 200 and any(it["task_id"] == task_id for it in items),
            f"found {len(items)} completed tasks")

    r = client.post("/tasks/unknown-task-id-0000/start")
    s.check("start unknown task returns 404", r.status_code == 404)
    return s


def scenario_knowledge_revision(client: httpx.Client) -> Scenario:
    s = Scenario("Knowledge revision (supersedes edge → get_relations → reflect/relations)", client)
    step(s.name)

    old = client.post("/remember", json={
        "content": "old judgment: keep mcp_server tools verbose for clarity",
    }).json()["id"]
    new = client.post("/remember", json={
        "content": "new judgment: thin MCP wrappers + shared services beat verbose tools",
    }).json()["id"]

    r = client.post("/relations", json={
        "src_id": new, "dst_id": old, "edge_type": "supersedes",
        "metadata": {"reason": "Phase S unification"},
    })
    edge = r.json().get("edge", {})
    s.check("create supersedes edge",
            r.status_code == 200 and edge.get("edge_type") == "supersedes",
            f"{edge.get('src','?')[:8]}.. → {edge.get('dst','?')[:8]}..")

    r = client.get(f"/relations/{new}", params={"direction": "out"})
    edges = r.json().get("edges", [])
    s.check("get_relations(out) returns the edge",
            r.status_code == 200 and any(e["dst"] == old for e in edges))

    r = client.post("/reflect/relations", params={"limit": 10})
    overview = r.json()
    s.check("reflect/relations counts supersedes",
            r.status_code == 200 and overview.get("by_type", {}).get("supersedes", 0) >= 1,
            f"by_type={overview.get('by_type')}")

    r = client.delete("/relations", params={"src_id": new, "dst_id": old})
    s.check("delete the edge",
            r.status_code == 200 and r.json().get("removed") == 1)
    return s


def scenario_maintenance(client: httpx.Client) -> Scenario:
    s = Scenario("Maintenance (hypothesis TTL → /compact → /merge near-duplicates)", client)
    step(s.name)

    client.post("/remember", json={
        "content": "ephemeral hypothesis to be reaped",
        "source": "hypothesis", "ttl_seconds": 0.05,
    })
    time.sleep(0.2)
    r = client.post("/compact", json={"expire_ttl": True, "rebuild_faiss": True})
    rep = r.json()
    s.check("compact reports TTL expiry",
            r.status_code == 200
            and "expired" in rep
            and "vectors_before" in rep
            and "vectors_after" in rep,
            f"expired={rep.get('expired')} vectors={rep.get('vectors_before')}→{rep.get('vectors_after')}")

    a = client.post("/remember", json={"content": "near-duplicate alpha for merge test"}).json()["id"]
    b = client.post("/remember", json={"content": "near-duplicate alpha for merge test extra"}).json()["id"]
    r = client.post("/merge", json={"node_ids": [a, b]})
    out = r.json()
    s.check("merge collapses near-duplicates into one survivor",
            r.status_code == 200
            and out.get("count") == 1
            and out["outcomes"][0]["absorbed_id"] in {a, b},
            f"absorbed={out['outcomes'][0]['absorbed_id'][:8]}.. survivor={out['outcomes'][0]['survivor_id'][:8]}..")
    return s


def scenario_forget_restore(client: httpx.Client) -> Scenario:
    s = Scenario("Forget / restore round-trip", client)
    step(s.name)

    nid = client.post("/remember", json={"content": "transient note for forget/restore test"}).json()["id"]

    r = client.post("/forget", json={"node_ids": [nid]})
    s.check("soft-archive via /forget",
            r.status_code == 200 and r.json() == {"affected": 1, "requested": 1, "hard": False})

    r = client.post("/recall", json={"query": "transient note", "top_k": 5, "force_refresh": True})
    s.check("archived item drops out of /recall",
            all(it["id"] != nid for it in r.json().get("items", [])))

    r = client.post("/restore", json={"node_ids": [nid]})
    s.check("/restore brings it back",
            r.status_code == 200 and r.json() == {"affected": 1, "requested": 1})

    r = client.post("/recall", json={"query": "transient note", "top_k": 5, "force_refresh": True})
    s.check("restored item is recall-able again",
            any(it["id"] == nid for it in r.json().get("items", [])))
    return s


# --- Runner -------------------------------------------------------------

def run_all_scenarios(url: str) -> int:
    with httpx.Client(base_url=url, timeout=30.0) as client:
        scenarios = [
            scenario_persona_onboarding(client),
            scenario_knowledge_curation(client),
            scenario_task_lifecycle(client),
            scenario_knowledge_revision(client),
            scenario_maintenance(client),
            scenario_forget_restore(client),
        ]
    print()
    print("=" * 60)
    total_failures = sum(s.fail_count for s in scenarios)
    for s in scenarios:
        flag = f"{GREEN}OK{RESET}" if s.fail_count == 0 else f"{RED}{s.fail_count} FAIL{RESET}"
        print(f"  {flag}  {s.name}")
    print("=" * 60)
    if total_failures == 0:
        print(f"{GREEN}All scenarios passed.{RESET}")
    else:
        print(f"{RED}{total_failures} check(s) failed across {sum(1 for s in scenarios if s.fail_count) } scenarios.{RESET}")
    return 0 if total_failures == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--port", type=int, default=int(os.environ.get("REST_SMOKE_PORT", 8766)))
    ap.add_argument("--data-dir", default=os.environ.get("REST_SMOKE_DIR", "/tmp/gaottt-rest-smoke"))
    ap.add_argument("--keep-server", action="store_true",
                    help="Leave the uvicorn process running after the smoke completes (manual poking)")
    ap.add_argument("--keep-data", action="store_true",
                    help="Do not wipe the isolated data dir after the run")
    args = ap.parse_args()

    data_dir = Path(args.data_dir).resolve()
    log_path = data_dir / "server.log"
    if data_dir.exists() and not args.keep_data:
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  GaOTTT REST smoke")
    print(f"  data dir : {data_dir}")
    print(f"  port     : {args.port}")
    print(f"  log      : {log_path}")
    print("  prod DB at ~/.local/share/gaottt/ is NOT touched.")
    print("=" * 60)

    print("\n[1/3] Booting uvicorn (model load may take 10–30s on first run)...")
    proc = start_uvicorn(args.port, data_dir, log_path)
    url = f"http://127.0.0.1:{args.port}"

    try:
        if not wait_ready(url, proc, timeout_s=90):
            print(f"{RED}ERROR{RESET}: server did not become ready. Last log lines:")
            try:
                print(log_path.read_text()[-3000:])
            except FileNotFoundError:
                pass
            return 2
        print(f"  Server ready at {url}")

        print("\n[2/3] Running scenarios...")
        rc = run_all_scenarios(url)

        print("\n[3/3] Done.")
        if args.keep_data:
            print(f"  Data dir preserved at {data_dir}")
        if args.keep_server:
            print(f"  Server still running at {url} (PID {proc.pid}). Stop it manually.")
            proc = None  # don't kill in finally
        return rc
    finally:
        if proc is not None:
            stop_server(proc)
        if not args.keep_data and data_dir.exists():
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
