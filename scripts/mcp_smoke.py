"""End-to-end smoke test for the GaOTTT MCP server.

Spawns ``python -m gaottt.server.mcp_server`` over stdio (same transport
Claude Code / Desktop use) and drives the full JSON-RPC handshake + typical
workflows via the official MCP client SDK:

  1. Handshake + tools/list (verify 25 tools discoverable)
  2. Memory round-trip (remember → recall → forget → restore)
  3. Task lifecycle (commit → start → complete → reflect tasks_completed)
  4. Persona chain (declare_value → declare_intention → declare_commitment → inherit_persona)
  5. Relations (relate supersedes → get_relations → unrelate)
  6. Resources (memory://stats, memory://hot)

Uses an **isolated** DB (default ``/tmp/gaottt-mcp-smoke``) so production
memory is never touched. Exit code 0 iff every check passes.

Usage::

    .venv/bin/python scripts/mcp_smoke.py
    .venv/bin/python scripts/mcp_smoke.py --data-dir /tmp/mcp-custom
    .venv/bin/python scripts/mcp_smoke.py --keep-data
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
DIM = "\033[2m"
RESET = "\033[0m"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"


def step(label: str) -> None:
    print(f"\n{YELLOW}▸ {label}{RESET}")


def passed(label: str, detail: str = "") -> None:
    line = f"  {GREEN}PASS{RESET}  {label}"
    if detail:
        line += f"  {DIM}{detail}{RESET}"
    print(line)


def failed(label: str, detail: str) -> None:
    print(f"  {RED}FAIL{RESET}  {label}\n    {detail}")


class Scenario:
    def __init__(self, name: str) -> None:
        self.name = name
        self.fail_count = 0

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        if condition:
            passed(label, detail)
        else:
            failed(label, detail or "(condition False)")
            self.fail_count += 1


# --- MCP helpers --------------------------------------------------------

def _first_text(result) -> str:
    """Join all TextContent blocks in an MCP tool result."""
    parts: list[str] = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


async def _call(session: ClientSession, name: str, args: dict) -> str:
    result = await session.call_tool(name, args)
    return _first_text(result)


def _extract_id(text: str, prefix: str = "ID: ") -> str | None:
    """Pull the first UUID that follows ``prefix`` in a tool output string."""
    if prefix not in text:
        return None
    tail = text.split(prefix, 1)[1]
    # Tokens end at whitespace or punctuation we care about.
    token = tail.split()[0].rstrip(".,)")
    return token or None


# --- Scenarios ----------------------------------------------------------

async def scenario_handshake_and_list(session: ClientSession) -> Scenario:
    s = Scenario("Handshake + tools/list")
    step(s.name)

    tools = await session.list_tools()
    names = {t.name for t in tools.tools}
    expected = {
        "remember", "recall", "explore", "forget", "restore", "revalidate",
        "prefetch", "prefetch_status", "reflect", "relate", "unrelate",
        "get_relations", "merge", "compact", "auto_remember", "ingest",
        "commit", "start", "complete", "abandon", "depend",
        "declare_value", "declare_intention", "declare_commitment",
        "inherit_persona",
    }
    missing = expected - names
    s.check("25 expected tools are discoverable",
            not missing,
            f"tools={len(names)} missing={sorted(missing) or '-'}")

    # Spot-check descriptions are populated (MCP clients rely on these).
    described = sum(1 for t in tools.tools if t.description)
    s.check("every tool has a description",
            described == len(tools.tools),
            f"{described}/{len(tools.tools)} have descriptions")

    resources = await session.list_resources()
    r_names = {str(r.uri) for r in resources.resources}
    s.check("memory:// resources exposed",
            "memory://stats" in r_names and "memory://hot" in r_names,
            f"resources={sorted(r_names)}")

    prompts = await session.list_prompts()
    p_names = {p.name for p in prompts.prompts}
    s.check("prompts registered",
            {"context_recall", "save_context", "explore_connections"} <= p_names,
            f"prompts={sorted(p_names)}")

    return s


async def scenario_memory_roundtrip(session: ClientSession) -> Scenario:
    s = Scenario("Memory round-trip (remember → recall → forget → restore)")
    step(s.name)

    text = await _call(session, "remember", {
        "content": "MCP smoke: user prefers uv over pip (preference)",
        "source": "user",
        "tags": ["smoke", "pref"],
    })
    node_id = _extract_id(text)
    s.check("remember returns ID",
            "Remembered" in text and node_id is not None,
            f"id={node_id}")

    recall = await _call(session, "recall", {"query": "uv pip preference", "top_k": 5})
    s.check("recall finds the remembered content",
            "uv over pip" in recall)

    forget = await _call(session, "forget", {"node_ids": [node_id]})
    s.check("forget soft-archives the node",
            "Archived 1" in forget)

    recall = await _call(session, "recall", {
        "query": "uv pip preference", "top_k": 5, "force_refresh": True,
    })
    s.check("recall no longer surfaces archived content",
            "uv over pip" not in recall)

    restore = await _call(session, "restore", {"node_ids": [node_id]})
    s.check("restore re-activates", "Restored 1" in restore)

    recall = await _call(session, "recall", {
        "query": "uv pip preference", "top_k": 5, "force_refresh": True,
    })
    s.check("recall surfaces again after restore",
            "uv over pip" in recall)

    return s


async def scenario_task_lifecycle(session: ClientSession) -> Scenario:
    s = Scenario("Task lifecycle (commit → start → complete → reflect tasks_completed)")
    step(s.name)

    text = await _call(session, "commit", {
        "content": "MCP smoke: verify stdio transport end-to-end",
        "deadline_seconds": 3600,
    })
    task_id = _extract_id(text)
    s.check("commit creates task",
            "Task committed" in text and task_id is not None,
            f"task_id={task_id}")

    text = await _call(session, "start", {"task_id": task_id})
    s.check("start bumps emotion and refreshes TTL",
            "Started" in text and "emotion=+0.40" in text)

    text = await _call(session, "complete", {
        "task_id": task_id,
        "outcome": "MCP smoke test passes via stdio",
        "emotion": 0.7,
    })
    s.check("complete records outcome edge",
            "Completed. outcome=" in text and task_id[:8] in text)

    text = await _call(session, "reflect", {"aspect": "tasks_completed", "limit": 5})
    s.check("reflect(tasks_completed) surfaces the just-completed task",
            "Completed tasks" in text and task_id[:8] in text)

    text = await _call(session, "start", {"task_id": "00000000-0000-0000-0000-000000000000"})
    s.check("start on unknown task returns friendly not-found",
            "not found" in text or "archived" in text)

    return s


async def scenario_persona_chain(session: ClientSession) -> Scenario:
    s = Scenario("Persona chain (declare_value → declare_intention → declare_commitment → inherit_persona)")
    step(s.name)

    text = await _call(session, "declare_value", {
        "content": "MCP smoke value: keep the protocol layer honest",
    })
    value_id = _extract_id(text)
    s.check("declare_value", "Value declared" in text and value_id is not None, f"id={value_id}")

    text = await _call(session, "declare_intention", {
        "content": "MCP smoke intention: ensure LLM-facing tools stay byte-identical",
        "parent_value_id": value_id,
    })
    intention_id = _extract_id(text)
    s.check("declare_intention from value",
            "Intention declared" in text
            and intention_id is not None
            and "derived_from" in text,
            f"id={intention_id}")

    text = await _call(session, "declare_commitment", {
        "content": "MCP smoke commitment: run this script before every release",
        "parent_intention_id": intention_id,
        "deadline_seconds": 604800,
    })
    commit_id = _extract_id(text)
    s.check("declare_commitment with parent_intention",
            "Commitment declared" in text
            and commit_id is not None
            and "fulfills" in text,
            f"id={commit_id}")

    persona = await _call(session, "inherit_persona", {})
    s.check("inherit_persona renders markdown with all three declared items",
            "# Persona inheritance" in persona
            and "MCP smoke value" in persona
            and "MCP smoke intention" in persona
            and "MCP smoke commitment" in persona)

    return s


async def scenario_relations(session: ClientSession) -> Scenario:
    s = Scenario("Relations (relate → get_relations → reflect relations → unrelate)")
    step(s.name)

    a = _extract_id(await _call(session, "remember", {"content": "MCP smoke: old API design judgment"}))
    b = _extract_id(await _call(session, "remember", {"content": "MCP smoke: new API design judgment"}))

    text = await _call(session, "relate", {
        "src_id": b, "dst_id": a, "edge_type": "supersedes",
        "metadata": {"reason": "Phase S unification"},
    })
    s.check("relate supersedes",
            "supersedes" in text and b[:8] in text and a[:8] in text)

    listing = await _call(session, "get_relations", {"node_id": b, "direction": "out"})
    s.check("get_relations lists the edge",
            "supersedes" in listing and a[:8] in listing)

    overview = await _call(session, "reflect", {"aspect": "relations", "limit": 10})
    s.check("reflect(relations) counts supersedes",
            "supersedes:" in overview)

    removed = await _call(session, "unrelate", {"src_id": b, "dst_id": a})
    s.check("unrelate removes the edge", "Removed" in removed)

    listing = await _call(session, "get_relations", {"node_id": b, "direction": "out"})
    s.check("get_relations reports no edges after unrelate",
            listing.startswith("No directed relations"))
    return s


async def scenario_resources(session: ClientSession) -> Scenario:
    s = Scenario("Resources (memory://stats, memory://hot)")
    step(s.name)

    try:
        r = await session.read_resource("memory://stats")
    except Exception as e:  # noqa: BLE001 — surface as FAIL, don't abort suite
        s.check("read memory://stats", False, f"raised: {e}")
        return s

    text = None
    for block in r.contents:
        text = getattr(block, "text", None)
        if text:
            break
    try:
        stats = json.loads(text or "{}")
    except json.JSONDecodeError as e:
        s.check("memory://stats returns JSON", False, f"raised: {e}")
        return s

    s.check("memory://stats returns JSON with expected keys",
            {"total_memories", "active_memories", "faiss_vectors"} <= set(stats.keys()),
            f"keys={sorted(stats.keys())}")

    r = await session.read_resource("memory://hot")
    hot_text = None
    for block in r.contents:
        hot_text = getattr(block, "text", None)
        if hot_text:
            break
    try:
        hot = json.loads(hot_text or "[]")
    except json.JSONDecodeError as e:
        s.check("memory://hot returns JSON array", False, f"raised: {e}")
        return s
    s.check("memory://hot returns a list of items",
            isinstance(hot, list),
            f"len={len(hot) if isinstance(hot, list) else '-'}")
    return s


# --- Runner -------------------------------------------------------------

async def run_all(data_dir: Path) -> int:
    env = os.environ.copy()
    env["GAOTTT_DATA_DIR"] = str(data_dir)
    env.pop("GAOTTT_CONFIG", None)
    env.pop("GER_RAG_CONFIG", None)

    params = StdioServerParameters(
        command=str(PYTHON),
        args=["-m", "gaottt.server.mcp_server"],
        env=env,
        cwd=str(PROJECT_ROOT),
    )

    print("[1/3] Spawning MCP stdio server (model load may take 10–30s on first run)...")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("  Connected, protocol initialized.")

            print("\n[2/3] Running scenarios...")
            scenarios = [
                await scenario_handshake_and_list(session),
                await scenario_memory_roundtrip(session),
                await scenario_task_lifecycle(session),
                await scenario_persona_chain(session),
                await scenario_relations(session),
                await scenario_resources(session),
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
        fail_scenarios = sum(1 for s in scenarios if s.fail_count)
        print(f"{RED}{total_failures} check(s) failed across {fail_scenarios} scenarios.{RESET}")
    print("\n[3/3] Done.")
    return 0 if total_failures == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--data-dir", default=os.environ.get("MCP_SMOKE_DIR", "/tmp/gaottt-mcp-smoke"))
    ap.add_argument("--keep-data", action="store_true",
                    help="Do not wipe the isolated data dir after the run")
    args = ap.parse_args()

    if not PYTHON.exists():
        print(f"{RED}ERROR{RESET}: {PYTHON} not found. Set up the uv venv first.")
        return 2

    data_dir = Path(args.data_dir).resolve()
    if data_dir.exists() and not args.keep_data:
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("  GaOTTT MCP smoke (stdio transport)")
    print(f"  data dir : {data_dir}")
    print("  prod DB at ~/.local/share/gaottt/ is NOT touched.")
    print("=" * 60)

    try:
        rc = asyncio.run(run_all(data_dir))
    finally:
        if not args.keep_data and data_dir.exists():
            shutil.rmtree(data_dir, ignore_errors=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
