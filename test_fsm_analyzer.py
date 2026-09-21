#!/usr/bin/env python3
"""Automated checks for fsm_analyzer.py against axi_master.v."""

import sys

from fsm_analyzer import (
    TARGET_MODULE,
    build_graph,
    check_deadlocks,
    check_reachability,
    extract_case_block,
    extract_module_body,
    extract_reset_state,
    extract_states,
)

FILENAME = "axi_master.v"
EXPECTED_STATE_COUNT = 14


def load():
    with open(FILENAME, "r") as f:
        text = f.read()
    module_body = extract_module_body(text, TARGET_MODULE)
    states = extract_states(module_body)
    reset_state = extract_reset_state(module_body)
    case_block = extract_case_block(module_body)
    graph = build_graph(case_block, list(states.keys()))
    return states, reset_state, graph


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
