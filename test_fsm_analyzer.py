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
    build_graph,
    check_deadlocks,
    check_reachability,
    extract_case_block,
    extract_module_body,
    extract_reset_state,
    extract_states,
    fix_and_verify,
    parse_fsm,
)

FILENAME = "axi_master.v"
EXPECTED_STATE_COUNT = 14
API_BASE = "http://localhost:8000"


def load():
    with open(FILENAME, "r") as f:
        text = f.read()
    module_body = extract_module_body(text, TARGET_MODULE)
    states = extract_states(module_body)
    reset_state = extract_reset_state(module_body)
    case_block = extract_case_block(module_body)
    graph = build_graph(case_block, list(states.keys()))
    return states, reset_state, graph


def test_fix_engine():
    """Run the fix engine end-to-end (writes axi_master_fixed.v) and
    confirm the fixed file reports 0 warnings. Output from the fix
    engine itself is suppressed to keep the test log readable; the
    assertion is on its return value, not its printed report."""
    states, reset_state, graph = parse_fsm(FILENAME)
    if states is None:
        return False
    deadlocks = check_deadlocks(graph, reset_state)
    if not deadlocks:
        return False

    with contextlib.redirect_stdout(io.StringIO()):
        result = fix_and_verify(FILENAME, deadlocks, graph, states, reset_state)

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


def test_api_analyze():
    """[TEST 8] /analyze returns correct JSON with wdata_last deadlock"""
    with open(FILENAME, "rb") as f:
        resp = requests.post(f"{API_BASE}/analyze", files={"file": (FILENAME, f, "text/plain")})
    if resp.status_code != 200:
        return False
    data = resp.json()
    deadlock_states = {d["state"] for d in data.get("deadlocks", [])}
    return (
        "wdata_last" in deadlock_states
        and data.get("summary", {}).get("total_warnings") == 1
        and data.get("states_found") == EXPECTED_STATE_COUNT
    )


def test_api_fix():
    """[TEST 9] /fix returns warnings_after: 0"""
    with open(FILENAME, "rb") as f:
        resp = requests.post(f"{API_BASE}/fix", files={"file": (FILENAME, f, "text/plain")})
    if resp.status_code != 200:
        return False
    data = resp.json()
    verification = data.get("verification", {})
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

        states, reset_state, graph = parse_fsm(tmp_path)
        if states is None:
            return False
        deadlocks = check_deadlocks(graph, reset_state)
        unreachable = check_reachability(graph, reset_state, list(states.keys()))
        return len(states) == EXPECTED_STATE_COUNT and not deadlocks and not unreachable
    finally:
        if tmp_path:
            os.remove(tmp_path)


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
    except RuntimeError as exc:
        results.append((f"[TEST 8] /analyze returns correct JSON with wdata_last deadlock ({exc})", False))
        results.append((f"[TEST 9] /fix returns warnings_after: 0 ({exc})", False))
        results.append((f"[TEST 10] /download returns a valid fixed Verilog file ({exc})", False))
    finally:
        if api_proc is not None:
            api_proc.terminate()
            api_proc.wait(timeout=5)

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
