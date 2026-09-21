#!/usr/bin/env python3
"""Automated checks for fsm_analyzer.py against axi_master.v."""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time

import requests

from fsm_analyzer import (
    TARGET_MODULE,
    check_deadlocks,
    check_reachability,
    fix_and_verify,
    get_fsm,
    parse_fsm,
    score_severity,
)

FILENAME = "axi_master.v"
UNREACHABLE_FILENAME = "test_cases/unreachable_fsm.v"
EXPECTED_STATE_COUNT = 14
API_BASE = "http://localhost:8000"


def load():
    """Load axi_master's FSM data from axi_master.v via parse_fsm
    (which now returns a list of every FSM module in the file)."""
    fsms = parse_fsm(FILENAME)
    entry = get_fsm(fsms, TARGET_MODULE)
    return entry["states"], entry["reset_state"], entry["graph"]


def test_fix_engine():
    """Run the fix engine end-to-end (writes axi_master_fixed.v) and
    confirm the fixed file reports 0 warnings. Output from the fix
    engine itself is suppressed to keep the test log readable; the
    assertion is on its return value, not its printed report."""
    fsms = parse_fsm(FILENAME)
    entry = get_fsm(fsms, TARGET_MODULE)
    if entry is None:
        return False
    states, reset_state, graph = entry["states"], entry["reset_state"], entry["graph"]
    deadlocks = check_deadlocks(graph, reset_state)
    if not deadlocks:
        return False

    with contextlib.redirect_stdout(io.StringIO()):
        result = fix_and_verify(
            FILENAME, deadlocks, graph, states, reset_state, module_name=TARGET_MODULE
        )

    if result is None:
        return False
    _fixed_filepath, fixed_deadlocks, _fixes_applied = result
    return fixed_deadlocks == []


def _api_server_running():
    try:
        requests.get(f"{API_BASE}/docs", timeout=1)
        return True
    except requests.exceptions.ConnectionError:
        return False


def _ensure_api_server():
    """Return a subprocess handle if this function had to start
    `uvicorn api:app`, or None if a server was already running on
    API_BASE (in which case it's left alone)."""
    if _api_server_running():
        return None

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api:app", "--port", "8000"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(30):
        if _api_server_running():
            return proc
        time.sleep(0.5)

    proc.terminate()
    raise RuntimeError("API server did not start within 15s")


def _find_result(results, module_name):
    for r in results:
        if r.get("module_name") == module_name:
            return r
    return None


def test_api_analyze():
    """[TEST 8] /analyze returns correct JSON with wdata_last deadlock"""
    with open(FILENAME, "rb") as f:
        resp = requests.post(f"{API_BASE}/analyze", files={"file": (FILENAME, f, "text/plain")})
    if resp.status_code != 200:
        return False
    data = resp.json()
    axi_master_result = _find_result(data.get("results", []), TARGET_MODULE)
    if axi_master_result is None:
        return False
    deadlock_states = {d["state"] for d in axi_master_result.get("deadlocks", [])}
    return (
        "wdata_last" in deadlock_states
        and axi_master_result.get("summary", {}).get("total_warnings") == 1
        and axi_master_result.get("states_found") == EXPECTED_STATE_COUNT
    )


def test_api_fix():
    """[TEST 9] /fix returns warnings_after: 0 for axi_master's deadlock"""
    with open(FILENAME, "rb") as f:
        resp = requests.post(f"{API_BASE}/fix", files={"file": (FILENAME, f, "text/plain")})
    if resp.status_code != 200:
        return False
    data = resp.json()
    axi_master_result = _find_result(data.get("results", []), TARGET_MODULE)
    if axi_master_result is None:
        return False
    verification = axi_master_result.get("verification", {})
    return verification.get("warnings_after") == 0 and bool(data.get("fixed_file_content"))


def test_api_download():
    """[TEST 10] /download returns a valid fixed Verilog file"""
    with open(FILENAME, "rb") as f:
        resp = requests.post(f"{API_BASE}/download", files={"file": (FILENAME, f, "text/plain")})
    if resp.status_code != 200:
        return False
    if "attachment" not in resp.headers.get("content-disposition", ""):
        return False

    content = resp.content.decode("utf-8")
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".v", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        fsms = parse_fsm(tmp_path)
        entry = get_fsm(fsms, TARGET_MODULE)
        if entry is None:
            return False
        states, reset_state, graph = entry["states"], entry["reset_state"], entry["graph"]
        deadlocks = check_deadlocks(graph, reset_state)
        unreachable = check_reachability(graph, reset_state, list(states.keys()))
        return len(states) == EXPECTED_STATE_COUNT and not deadlocks and not unreachable
    finally:
        if tmp_path:
            os.remove(tmp_path)


def test_api_visualize():
    """[TEST 11] /visualize returns correct node types — wdata_last is
    deadlock, idle is reset, 14 nodes total (for axi_master specifically)."""
    with open(FILENAME, "rb") as f:
        resp = requests.post(f"{API_BASE}/visualize", files={"file": (FILENAME, f, "text/plain")})
    if resp.status_code != 200:
        return False
    data = resp.json()
    modules = data.get("modules", [])
    axi_master_module = next((m for m in modules if m.get("module_name") == TARGET_MODULE), None)
    if axi_master_module is None:
        return False

    nodes = axi_master_module.get("nodes", [])
    if len(nodes) != EXPECTED_STATE_COUNT:
        return False

    node_types = {n["id"]: n["type"] for n in nodes}
    if node_types.get("wdata_last") != "deadlock":
        return False
    if node_types.get("idle") != "reset":
        return False

    edges = axi_master_module.get("edges", [])
    # Every transition in the analyzer's graph must appear as an edge;
    # cross-check against the CLI-facing graph directly rather than
    # trusting the endpoint's own edge count.
    fsms = parse_fsm(FILENAME)
    entry = get_fsm(fsms, TARGET_MODULE)
    expected_edge_count = sum(len(transitions) for transitions in entry["graph"].values())
    return len(edges) == expected_edge_count


def test_multi_fsm_analyze():
    """[TEST 12] /analyze on axi_master.v finds 2 FSMs — axi_master and axi4_slave"""
    with open(FILENAME, "rb") as f:
        resp = requests.post(f"{API_BASE}/analyze", files={"file": (FILENAME, f, "text/plain")})
    if resp.status_code != 200:
        return False
    data = resp.json()
    if data.get("modules_found") != 2:
        return False
    results = data.get("results", [])
    axi_master_result = _find_result(results, "axi_master")
    axi4_slave_result = _find_result(results, "axi4_slave")
    if axi_master_result is None or axi4_slave_result is None:
        return False
    return (
        len(axi_master_result.get("deadlocks", [])) == 1
        and len(axi4_slave_result.get("deadlocks", [])) == 0
    )


def test_severity_wdata_last():
    """[TEST 13] wdata_last scores HIGH severity"""
    fsms = parse_fsm(FILENAME)
    entry = get_fsm(fsms, TARGET_MODULE)
    if entry is None:
        return False
    return score_severity("wdata_last", entry["graph"]) == "HIGH"


def test_unreachable_fsm_file():
    """[TEST 14] unreachable_fsm.v — 'orphan' state flagged as
    unreachable, no deadlocks detected"""
    fsms = parse_fsm(UNREACHABLE_FILENAME)
    if len(fsms) != 1:
        return False
    entry = fsms[0]
    states, reset_state, graph = entry["states"], entry["reset_state"], entry["graph"]
    unreachable = check_reachability(graph, reset_state, list(states.keys()))
    deadlocks = check_deadlocks(graph, reset_state)
    return (
        entry["module_name"] == "unreachable_fsm"
        and reset_state == "idle"
        and unreachable == ["orphan"]
        and deadlocks == []
    )


def main():
    states, reset_state, graph = load()
    unreachable = check_reachability(graph, reset_state, list(states.keys()))
    deadlocks = check_deadlocks(graph, reset_state)
    deadlock_states = {name for name, _cond in deadlocks}

    results = []

    results.append((
        "[TEST 1] 14 states extracted correctly",
        len(states) == EXPECTED_STATE_COUNT,
    ))

    results.append((
        "[TEST 2] Reset state is 'idle'",
        reset_state == "idle",
    ))

    results.append((
        "[TEST 3] All 14 states reachable from reset",
        len(unreachable) == 0,
    ))

    results.append((
        "[TEST 4] 'wdata_last' flagged as deadlock",
        "wdata_last" in deadlock_states,
    ))

    results.append((
        "[TEST 5] 'idle' NOT flagged as deadlock",
        "idle" not in deadlock_states,
    ))

    results.append((
        "[TEST 6] 'send_wdata' NOT flagged as deadlock (it has timeout "
        "escape at wr_count == 15)",
        "send_wdata" not in deadlock_states,
    ))

    results.append((
        "[TEST 7] Fix engine — axi_master_fixed.v produces 0 warnings",
        test_fix_engine(),
    ))

    api_proc = None
    try:
        api_proc = _ensure_api_server()

        results.append((
            "[TEST 8] /analyze returns correct JSON with wdata_last deadlock",
            test_api_analyze(),
        ))

        results.append((
            "[TEST 9] /fix returns warnings_after: 0",
            test_api_fix(),
        ))

        results.append((
            "[TEST 10] /download returns a valid fixed Verilog file",
            test_api_download(),
        ))

        results.append((
            "[TEST 11] /visualize returns correct node types — wdata_last is "
            "deadlock, idle is reset, 14 nodes total",
            test_api_visualize(),
        ))

        results.append((
            "[TEST 12] /analyze on axi_master.v finds 2 FSMs — axi_master "
            "(1 deadlock) and axi4_slave (0 deadlocks)",
            test_multi_fsm_analyze(),
        ))
    except RuntimeError as exc:
        for n, label in (
            (8, "/analyze returns correct JSON with wdata_last deadlock"),
            (9, "/fix returns warnings_after: 0"),
            (10, "/download returns a valid fixed Verilog file"),
            (11, "/visualize returns correct node types"),
            (12, "/analyze on axi_master.v finds 2 FSMs"),
        ):
            results.append((f"[TEST {n}] {label} ({exc})", False))
    finally:
        if api_proc is not None:
            api_proc.terminate()
            api_proc.wait(timeout=5)

    results.append((
        "[TEST 13] wdata_last scores HIGH severity",
        test_severity_wdata_last(),
    ))

    results.append((
        "[TEST 14] unreachable_fsm.v — 'orphan' state flagged as "
        "unreachable, no deadlocks detected",
        test_unreachable_fsm_file(),
    ))

    all_passed = True
    for label, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"{label}: {status}")
        if not passed:
            all_passed = False

    if not all_passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
