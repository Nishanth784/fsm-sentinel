#!/usr/bin/env python3
"""FSM Sentinel — parses a Verilog FSM and flags unreachable states and
potential deadlocks. Uses only the stdlib `re` module; no Verilog parser
library, no graph library."""

import difflib
import os
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
    """Split the case(state) body into per-state text chunks.

    Case labels may group multiple states together (a common Verilog
    idiom for states that share identical behavior), e.g.:
        no_ack_wdata, no_ack_waddr: begin ... end
    Each name in such a group is treated as its own label sharing the
    same block text.
    """
    names_alt = "|".join(re.escape(s) for s in state_names)
    label_pattern = re.compile(
        r"((?:(?:" + names_alt + r")\s*,\s*)*(?:" + names_alt + r"))\s*:"
    )
    labels = list(label_pattern.finditer(case_block))

    default_match = re.search(r"\bdefault\s*:", case_block)
    case_end = default_match.start() if default_match else len(case_block)

    blocks = {}
    for i, lm in enumerate(labels):
        names = [n.strip() for n in lm.group(1).split(",")]
        start = lm.end()
        end = labels[i + 1].start() if i + 1 < len(labels) else case_end
        end = min(end, case_end)
        block_text = case_block[start:end]
        for name in names:
            blocks[name] = block_text
    return blocks


# Matches the *keyword* only; the condition itself (when present) is
# extracted separately via balanced-paren scanning, since conditions may
# contain nested parentheses (e.g. "(a == 1) && (b == 1)") that a simple
# "[^)]*" regex cannot capture correctly.
KEYWORD_TOKEN = re.compile(r"else\s+if\b|\bif\b|\belse\b")

ASSIGNMENT = re.compile(r"state\s*<=\s*(\w+)\s*;")


def find_matching_paren(text, open_idx):
    """Given text[open_idx] == '(', return the index of the matching ')'.

    Returns None if the parentheses are unbalanced.
    """
    depth = 0
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def parse_transitions_for_state(state_name, block_text):
    """Return a list of (to_state, condition) tuples for one state block."""
    transitions = []

    keyword_matches = list(KEYWORD_TOKEN.finditer(block_text))

    if not keyword_matches:
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

    # For each keyword, determine its condition (balanced-paren scan for
    # if/else if, or the literal "else" for a bare else) and the segment
    # of text up to the next keyword, then find the state<= assignment
    # that belongs to that segment.
    segments = []
    parse_ok = True
    for i, kw in enumerate(keyword_matches):
        keyword_text = kw.group(0)
        is_bare_else = re.fullmatch(r"else", keyword_text.strip()) is not None

        if is_bare_else:
            condition = "else"
            segment_start = kw.end()
        else:
            paren_start = block_text.find("(", kw.end())
            next_kw_start = (
                keyword_matches[i + 1].start()
                if i + 1 < len(keyword_matches)
                else len(block_text)
            )
            if paren_start == -1 or paren_start > next_kw_start:
                parse_ok = False
                continue
            paren_end = find_matching_paren(block_text, paren_start)
            if paren_end is None:
                parse_ok = False
                continue
            condition = block_text[paren_start + 1 : paren_end].strip()
            segment_start = paren_end + 1

        segment_end = (
            keyword_matches[i + 1].start()
            if i + 1 < len(keyword_matches)
            else len(block_text)
        )
        segments.append((condition, block_text[segment_start:segment_end]))

    if not parse_ok:
        print(
            f"[PARSER WARNING] Could not parse transition in state "
            f"{state_name} — skipped"
        )
        return transitions

    for condition, segment in segments:
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


def parse_fsm(filepath):
    """Read a Verilog file and extract (states, reset_state, graph) for
    TARGET_MODULE. Returns (None, None, None) if the module isn't found."""
    with open(filepath, "r") as f:
        text = f.read()

    module_body = extract_module_body(text, TARGET_MODULE)
    if module_body is None:
        return None, None, None

    states = extract_states(module_body)
    reset_state = extract_reset_state(module_body)
    case_block = extract_case_block(module_body)

    if case_block is None:
        graph = {name: [] for name in states}
    else:
        graph = build_graph(case_block, list(states.keys()))

    return states, reset_state, graph


def analyze_and_report(filepath, states, reset_state, graph):
    """Run reachability + deadlock checks, print the report, and return
    the list of (state_name, condition) deadlocks found."""
    unreachable = check_reachability(graph, reset_state, list(states.keys()))
    deadlocks = check_deadlocks(graph, reset_state)
    print_report(filepath, states, reset_state, unreachable, deadlocks)
    return deadlocks


# =====================================================================
# Fix engine
# =====================================================================
#
# When a deadlock is detected, generate_fix() asks an LLM for a targeted
# fix to just that state's block. If ANTHROPIC_API_KEY isn't set, or the
# 'anthropic' package isn't installed, or the API call itself fails, it
# falls back to a deterministic template fix instead of failing the
# whole workflow — a live demo should not go down because of a network
# blip or a missing key. The fallback is always clearly labeled in the
# output so it's never mistaken for a live LLM result.
#
# Whatever the source, the fix is never trusted blindly: apply_fix()
# re-parses the edited file and checks the state set is unchanged before
# writing it out, and fix_and_verify() re-runs the full analysis on the
# result to confirm the deadlock is actually gone.

BEGIN_END_TOKEN = re.compile(r"\bbegin\b|\bend\b")


def find_matching_end(text, begin_idx):
    """Given text[begin_idx:] starting at a 'begin' token, return the
    (start, end) span of its matching 'end' token."""
    depth = 0
    for m in BEGIN_END_TOKEN.finditer(text, begin_idx):
        if m.group(0) == "begin":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return m.start(), m.end()
    return None


def locate_state_block_span(source, state_name, state_names):
    """Find the (start, end) character span of a state's full case-label
    block ("state_name: begin ... end") within `source`.

    Returns None if the state isn't found. Returns the string "grouped"
    if the state shares a comma-joined case label with other states
    (e.g. "no_ack_wdata, no_ack_waddr:") — callers should refuse to
    surgically edit such a block, since doing so would also touch the
    other state(s) sharing it.
    """
    case_match = re.search(r"case\s*\(\s*state\s*\)(.*?)endcase", source, re.DOTALL)
    if not case_match:
        return None
    case_start, case_end = case_match.start(1), case_match.end(1)

    names_alt = "|".join(re.escape(s) for s in state_names)
    label_pattern = re.compile(
        r"((?:(?:" + names_alt + r")\s*,\s*)*(?:" + names_alt + r"))\s*:"
    )
    labels = list(label_pattern.finditer(source, case_start, case_end))

    default_match = re.search(r"\bdefault\s*:", source[case_start:case_end])
    boundary = case_start + default_match.start() if default_match else case_end

    for i, lm in enumerate(labels):
        names = [n.strip() for n in lm.group(1).split(",")]
        if state_name not in names:
            continue
        if len(names) > 1:
            return "grouped"
        block_start = lm.start()
        block_end = labels[i + 1].start() if i + 1 < len(labels) else boundary
        block_end = min(block_end, boundary)
        return block_start, block_end

    return None


def _pick_timeout_counter(state_name, fsm_source):
    """Guess which existing timeout counter to reuse for a fallback fix,
    based on naming conventions already used elsewhere in the FSM."""
    counters = re.findall(r"(\w*_count)\s*==\s*15", fsm_source)
    lower_state = state_name.lower()
    write_hint = any(t in lower_state for t in ("wdata", "waddr", "wr", "bvalid", "bresp"))
    read_hint = any(t in lower_state for t in ("rdata", "raddr", "rd"))

    for c in counters:
        if write_hint and "wr" in c:
            return c
    for c in counters:
        if read_hint and "rd" in c:
            return c
    if counters:
        return counters[0]
    return "timeout_count"


def _template_fix(state_name, condition, fsm_source):
    """Deterministic fallback fix: reuse this FSM's existing '== 15'
    timeout-counter pattern to add a timeout escape to a state whose
    only exit is a handshake condition. Returns None if the state's
    block doesn't have the simple "if (...) begin ... end" shape this
    template can safely extend.
    """
    module_body = extract_module_body(fsm_source, TARGET_MODULE)
    if module_body is None:
        return None
    states = extract_states(module_body)
    reset_state = extract_reset_state(module_body) or "idle"

    span = locate_state_block_span(fsm_source, state_name, list(states.keys()))
    if not isinstance(span, tuple):
        return None
    original_block = fsm_source[span[0] : span[1]]

    if_match = re.search(r"\bif\s*\(", original_block)
    if not if_match:
        return None
    paren_start = original_block.find("(", if_match.start())
    paren_end = find_matching_paren(original_block, paren_start)
    if paren_end is None:
        return None
    begin_idx = original_block.find("begin", paren_end)
    if begin_idx == -1:
        return None
    end_span = find_matching_end(original_block, begin_idx)
    if end_span is None:
        return None
    inner_end_start, inner_end_end = end_span

    line_start = original_block.rfind("\n", 0, inner_end_start) + 1
    base_indent = re.match(r"[ \t]*", original_block[line_start:inner_end_start]).group(0)
    stmt_indent = base_indent + "    "

    counter = _pick_timeout_counter(state_name, fsm_source)
    insertion = (
        f" else if ({counter} == 15) begin\n"
        f"{stmt_indent}state <= {reset_state};\n"
        f"{stmt_indent}{counter} <= 0;\n"
        f"{base_indent}end else begin\n"
        f"{stmt_indent}{counter} <= {counter} + 1;\n"
        f"{base_indent}end"
    )

    return original_block[:inner_end_end] + insertion + original_block[inner_end_end:]


def _build_fix_prompt(state_name, condition, fsm_source):
    return f"""You are a hardware verification expert fixing a deadlock in a Verilog FSM.

The FSM has a deadlocked state: '{state_name}'
The only exit condition is: {condition}
There is no timeout escape. If the condition never asserts, the FSM stalls forever.

Here is the full FSM source:
{fsm_source}

Generate ONLY the fixed version of the '{state_name}' state block.
The fix must add a timeout escape using the same counter pattern already used in other states of this FSM.
Look at how other states use timeout counters (== 15 pattern) and apply the same pattern here.

Rules:
- Change ONLY the '{state_name}' state block
- Do not modify any other state
- Do not add new signals or parameters
- Use only signals already present in the FSM
- Output only the fixed state block, nothing else, no explanation, no markdown
"""


def _call_groq(prompt, api_key):
    import requests

    model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1000,
            "temperature": 0,
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def _call_anthropic(prompt, api_key):
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text.strip()


def generate_fix(state_name, condition, fsm_source):
    """Generate a targeted fix for one deadlocked state block.

    Provider order: Groq (GROQ_API_KEY) first, then Anthropic
    (ANTHROPIC_API_KEY), then a deterministic template fix that reuses
    this FSM's own existing '== 15' timeout pattern. Each step falls
    through to the next on a missing key, a missing package, or a
    failed call — a live demo shouldn't go down over a network blip or
    a missing key. Whichever path is used is always announced on
    stdout, so the fallback is never mistaken for a live LLM result.
    """
    prompt = _build_fix_prompt(state_name, condition, fsm_source)

    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        try:
            return _call_groq(prompt, groq_key)
        except Exception as exc:
            print(f"[FIX ENGINE] Groq call failed ({exc}) — trying next provider.")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        try:
            return _call_anthropic(prompt, anthropic_key)
        except ImportError:
            print(
                "[FIX ENGINE] 'anthropic' package not installed — "
                "trying next provider."
            )
        except Exception as exc:
            print(f"[FIX ENGINE] Anthropic call failed ({exc}) — trying next provider.")

    if not groq_key and not anthropic_key:
        print(
            "[FIX ENGINE] No GROQ_API_KEY or ANTHROPIC_API_KEY set — "
            "using deterministic template fix (no live LLM call)."
        )
    else:
        print("[FIX ENGINE] Falling back to deterministic template fix.")

    fixed_block = _template_fix(state_name, condition, fsm_source)
    if fixed_block is None:
        raise RuntimeError(
            f"Could not generate a fix for state '{state_name}' "
            f"(neither the LLM nor the template fallback produced one)"
        )
    return fixed_block


def show_diff(original_block, fixed_block, state_name):
    print(f"\n--- DIFF for state '{state_name}' ---")
    diff = difflib.unified_diff(
        original_block.splitlines(keepends=True),
        fixed_block.splitlines(keepends=True),
        fromfile=f"{state_name} (original)",
        tofile=f"{state_name} (fixed)",
        lineterm="",
    )
    for line in diff:
        print(line)
    print("--- END DIFF ---\n")


def apply_fix(filepath, state_name, fixed_block, output_filepath=None):
    """Surgically replace only `state_name`'s case-label block in the
    file at `filepath` with `fixed_block`, and write the result to
    `output_filepath` (default: <filepath without .v>_fixed.v).

    Before writing, re-parses the edited source and refuses to save if
    the module's state set changed — a cheap sanity check that the
    surgical edit didn't corrupt the FSM structure.
    """
    with open(filepath, "r") as f:
        source = f.read()

    module_body = extract_module_body(source, TARGET_MODULE)
    if module_body is None:
        print(f"[ERROR] Module '{TARGET_MODULE}' not found in {filepath}")
        return None
    states = extract_states(module_body)

    span = locate_state_block_span(source, state_name, list(states.keys()))
    if span is None:
        print(
            f"[ERROR] Could not locate '{state_name}' block in source "
            f"for surgical replacement"
        )
        return None
    if span == "grouped":
        print(
            f"[ERROR] '{state_name}' shares a case label with other "
            f"states — skipping surgical fix to avoid corrupting them"
        )
        return None

    start, end = span
    original_slice = source[start:end]
    stripped = original_slice.rstrip()
    trailing_whitespace = original_slice[len(stripped):]
    fixed_source = (
        source[:start] + fixed_block.strip() + trailing_whitespace + source[end:]
    )

    fixed_module_body = extract_module_body(fixed_source, TARGET_MODULE)
    if fixed_module_body is None:
        print(
            f"[ERROR] Surgical fix for '{state_name}' produced an "
            f"unparsable module — not writing output"
        )
        return None
    fixed_states = extract_states(fixed_module_body)
    if set(fixed_states.keys()) != set(states.keys()):
        print(
            f"[ERROR] Surgical fix for '{state_name}' changed the FSM's "
            f"state set — not writing output"
        )
        return None

    if output_filepath is None:
        output_filepath = filepath.replace(".v", "_fixed.v")

    with open(output_filepath, "w") as f:
        f.write(fixed_source)

    print(f"[FIX APPLIED] Fixed file saved as: {output_filepath}")
    return output_filepath


def fix_and_verify(filepath, deadlocks, graph, states, reset_state):
    """Generate, show, and apply a fix for every detected deadlock, then
    re-run the full analysis on the fixed file to verify it worked."""
    if not deadlocks:
        print("[INFO] No deadlocks to fix.")
        return None

    output_filepath = filepath.replace(".v", "_fixed.v")
    current_read_path = filepath

    for state_name, condition in deadlocks:
        print(f"\n[FIX ENGINE] Generating fix for '{state_name}'...")

        with open(current_read_path, "r") as f:
            fsm_source = f.read()

        fixed_block = generate_fix(state_name, condition, fsm_source)

        module_body = extract_module_body(fsm_source, TARGET_MODULE)
        cur_states = extract_states(module_body) if module_body else {}
        span = locate_state_block_span(fsm_source, state_name, list(cur_states.keys()))
        if isinstance(span, tuple):
            original_block = fsm_source[span[0] : span[1]]
        else:
            original_block = "<could not locate original block>"

        show_diff(original_block, fixed_block, state_name)

        result_path = apply_fix(
            current_read_path, state_name, fixed_block, output_filepath=output_filepath
        )
        if not result_path:
            print(f"[ERROR] Fix application failed for '{state_name}'")
            continue

        current_read_path = result_path

    print("\n[VERIFY] Re-running analysis on fixed file...")
    print("=" * 50)
    fixed_states, fixed_reset, fixed_graph = parse_fsm(output_filepath)
    if fixed_states is None:
        print(f"[ERROR] Could not parse fixed file {output_filepath}")
        return None
    fixed_deadlocks = analyze_and_report(output_filepath, fixed_states, fixed_reset, fixed_graph)
    return output_filepath, fixed_deadlocks


def main():
    if len(sys.argv) < 2:
        print("Usage: python fsm_analyzer.py <verilog_file> [--fix]")
        sys.exit(1)

    filepath = sys.argv[1]
    auto_fix = "--fix" in sys.argv

    states, reset_state, graph = parse_fsm(filepath)
    if states is None:
        print(f"[ERROR] Module '{TARGET_MODULE}' not found in {filepath}")
        sys.exit(1)

    deadlocks = analyze_and_report(filepath, states, reset_state, graph)

    if auto_fix and deadlocks:
        fix_and_verify(filepath, deadlocks, graph, states, reset_state)
    elif deadlocks:
        print("\n[INFO] Run with --fix flag to automatically generate and apply fixes.")


if __name__ == "__main__":
    main()
