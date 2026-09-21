#!/usr/bin/env python3
"""FSM Sentinel — parses a Verilog FSM and flags unreachable states and
potential deadlocks. Uses only the stdlib `re` module; no Verilog parser
library, no graph library."""

import re
import sys
from collections import deque

TARGET_MODULE = "axi_master"
HANDSHAKE_KEYWORDS = ("m_axi_", "valid", "ready")


def extract_module_body(text, module_name):
    pattern = re.compile(
        r"\bmodule\s+" + re.escape(module_name) + r"\b(.*?)\bendmodule\b",
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1)


def extract_states(module_body):
    states = {}
    localparam_match = re.search(
        r"\blocalparam\b(.*?);", module_body, re.DOTALL
    )
    if not localparam_match:
        return states

    body = localparam_match.group(1)
    for entry in body.split(","):
        entry = entry.strip()
        if not entry:
            continue
        m = re.match(r"(\w+)\s*=\s*(\d+)", entry)
        if m:
            states[m.group(1)] = int(m.group(2))
    return states


def extract_reset_state(module_body):
    m = re.search(
        r"if\s*\(\s*!\s*m_axi_aresetn\s*\)\s*(?:begin)?\s*"
        r"state\s*<=\s*(\w+)\s*;",
        module_body,
    )
    if m:
        return m.group(1)
    return None


def extract_case_block(module_body):
    m = re.search(r"case\s*\(\s*state\s*\)(.*?)endcase", module_body, re.DOTALL)
    if not m:
        return None
    return m.group(1)


def split_state_blocks(case_block, state_names):
    """Split the case(state) body into per-state text chunks."""
    label_pattern = re.compile(
        r"(" + "|".join(re.escape(s) for s in state_names) + r")\s*:"
    )
    labels = list(label_pattern.finditer(case_block))

    default_match = re.search(r"\bdefault\s*:", case_block)
    case_end = default_match.start() if default_match else len(case_block)

    blocks = {}
    for i, lm in enumerate(labels):
        name = lm.group(1)
        start = lm.end()
        end = labels[i + 1].start() if i + 1 < len(labels) else case_end
        end = min(end, case_end)
        blocks[name] = case_block[start:end]
    return blocks


# Matches, in order of precedence: "else if (COND)", "if (COND)", "else"
CONDITION_TOKEN = re.compile(
    r"else\s+if\s*\(([^)]*)\)|if\s*\(([^)]*)\)|(else)\b"
)

ASSIGNMENT = re.compile(r"state\s*<=\s*(\w+)\s*;")


def parse_transitions_for_state(state_name, block_text):
    """Return a list of (to_state, condition) tuples for one state block."""
    transitions = []

    tokens = list(CONDITION_TOKEN.finditer(block_text))

    if not tokens:
        # No if/else at all in this block -- expect at most one
        # unconditional assignment.
        assigns = ASSIGNMENT.findall(block_text)
        if len(assigns) == 1:
            transitions.append((assigns[0], "unconditional"))
        elif len(assigns) == 0:
            pass
        else:
            print(
                f"[PARSER WARNING] Could not parse transition in state "
                f"{state_name} — skipped"
            )
        return transitions

    # Walk each condition token, and find the state<= assignment that
    # belongs to it (the first assignment after the token and before the
    # next token).
    for i, tok in enumerate(tokens):
        if tok.group(1) is not None:
            condition = tok.group(1).strip()
        elif tok.group(2) is not None:
            condition = tok.group(2).strip()
        else:
            condition = "else"

        segment_start = tok.end()
        segment_end = tokens[i + 1].start() if i + 1 < len(tokens) else len(block_text)
        segment = block_text[segment_start:segment_end]

        assigns = ASSIGNMENT.findall(segment)
        if len(assigns) == 1:
            transitions.append((assigns[0], condition))
        elif len(assigns) == 0:
            # Condition guards something other than a state transition;
            # not an error by itself.
            continue
        else:
            print(
                f"[PARSER WARNING] Could not parse transition in state "
                f"{state_name} — skipped"
            )

    return transitions


def build_graph(case_block, state_names):
    blocks = split_state_blocks(case_block, state_names)
    graph = {name: [] for name in state_names}

    for state_name, block_text in blocks.items():
        transitions = parse_transitions_for_state(state_name, block_text)
        graph[state_name].extend(transitions)

    return graph


def check_reachability(graph, reset_state, all_states):
    reachable = set()
    if reset_state in graph:
        queue = deque([reset_state])
        reachable.add(reset_state)
        while queue:
            current = queue.popleft()
            for to_state, _cond in graph.get(current, []):
                if to_state in graph and to_state not in reachable:
                    reachable.add(to_state)
                    queue.append(to_state)

    unreachable = [s for s in all_states if s not in reachable]
    return unreachable


def is_handshake_condition(condition):
    return any(keyword in condition for keyword in HANDSHAKE_KEYWORDS)


def is_safe_exit(condition):
    return condition == "unconditional" or "== 15" in condition


def check_deadlocks(graph, reset_state):
    deadlocks = []
    for state_name, transitions in graph.items():
        if state_name == reset_state:
            continue
        if not transitions:
            continue

        has_safe_exit = any(is_safe_exit(cond) for _to, cond in transitions)
        if has_safe_exit:
            continue

        handshake_conditions = [
            cond for _to, cond in transitions if is_handshake_condition(cond)
        ]
        if handshake_conditions and len(handshake_conditions) == len(transitions):
            deadlocks.append((state_name, handshake_conditions[0]))

    return deadlocks


def print_report(filename, states, reset_state, unreachable, deadlocks):
    print("=== FSM Analysis Report ===")
    print(f"File: {filename}")
    print(f"States found: {len(states)}")
    print(f"Reset state: {reset_state}")
    print()

    print("--- Reachability ---")
    if not unreachable:
        print(f"[PASS] All {len(states)} states reachable from reset.")
    else:
        for state_name in unreachable:
            print(f"[WARN] Unreachable state: {state_name}")
            print(f"       No path from 'idle' to '{state_name}' exists.")
    print()

    print("--- Deadlock Detection ---")
    if not deadlocks:
        print("[PASS] No deadlocks detected.")
    else:
        for state_name, condition in deadlocks:
            print(f"[WARN] Potential deadlock: state '{state_name}'")
            print(f"       Blocking condition: {condition}")
            print(
                f"       Failure scenario: If {condition} never asserts while"
            )
            print(f"       the FSM is in '{state_name}', there is no timeout or")
            print("       unconditional exit. The FSM stalls indefinitely.")
    print()

    total_warnings = len(unreachable) + len(deadlocks)
    print(f"=== Summary: {total_warnings} warning(s) found ===")


def main():
    if len(sys.argv) != 2:
        print("Usage: python fsm_analyzer.py <verilog_file>")
        sys.exit(1)

    filename = sys.argv[1]
    with open(filename, "r") as f:
        text = f.read()

    module_body = extract_module_body(text, TARGET_MODULE)
    if module_body is None:
        print(f"[ERROR] Module '{TARGET_MODULE}' not found in {filename}")
        sys.exit(1)

    states = extract_states(module_body)
    reset_state = extract_reset_state(module_body)
    case_block = extract_case_block(module_body)

    if case_block is None:
        graph = {name: [] for name in states}
    else:
        graph = build_graph(case_block, list(states.keys()))

    unreachable = check_reachability(graph, reset_state, list(states.keys()))
    deadlocks = check_deadlocks(graph, reset_state)

    print_report(filename, states, reset_state, unreachable, deadlocks)


if __name__ == "__main__":
    main()
