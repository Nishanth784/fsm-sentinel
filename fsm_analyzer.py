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


def _mask_comments(text):
    """Return a same-length copy of `text` with the interior of //...
    line comments and /* ... */ block comments replaced by spaces
    (newlines kept, so line-based logic still works).

    Every structural token scan in this file (case/endcase, if/else/
    begin/end matching) runs on this masked version instead of the raw
    text — English prose in a comment can and does contain words like
    "case", "if", or "begin" (a real example found in this codebase's
    own test file: a comment reading "...handle single-beat case
    (burst_count == 0)..." silently broke case/endcase balance
    matching before this fix). Masking and original text are always
    the same length, so any (start, end) offset found by scanning the
    masked text is valid for slicing the original — which is what
    every caller actually extracts or writes back, so real code,
    comments, and formatting are never altered.
    """
    result = list(text)
    i = 0
    n = len(text)
    while i < n:
        two = text[i : i + 2]
        if two == "//":
            j = i
            while j < n and text[j] != "\n":
                result[j] = " "
                j += 1
            i = j
        elif two == "/*":
            end = text.find("*/", i + 2)
            end = end + 2 if end != -1 else n
            for j in range(i, end):
                if text[j] != "\n":
                    result[j] = " "
            i = end
        else:
            i += 1
    return "".join(result)


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


def extract_reset_state(module_body, states=None):
    """Find the state a module resets to. Tries, in order:

    1. An active-low bang reset: if (!<signal>) ... state <= <name>;
       (signal name generalized — not hardcoded to one module's reset
       pin — so this works across modules with different reset names.)
    2. An active-low comparison reset: if (<signal> == 0) ... state <= <name>;
    3. The state register's own declared initial value, e.g.
       "reg [3:0] state = idle;" or "reg [4:0] state = 0;" — some FSMs
       set their reset value this way instead of inside the reset
       branch. A bare numeric value is mapped back to a state name via
       `states` (the localparam dict) when one is supplied.
    """
    # Scope the bang/comparison search to the region before case(state):
    # the reset check always precedes it in module order, and searching
    # the whole module_body risks false-matching an ordinary transition
    # condition deep in the FSM body that happens to look like
    # "if (X == 0) state <= Y;" (this is not hypothetical — it happened
    # against axi4_slave, a real module in this codebase's test file).
    case_match = re.search(r"case\s*\(\s*state\s*\)", module_body)
    search_region = module_body[: case_match.start()] if case_match else module_body

    m = re.search(
        r"if\s*\(\s*!\s*\w+\s*\)\s*(?:begin)?\s*state\s*<=\s*(\w+)\s*;",
        search_region,
    )
    if m:
        return m.group(1)

    m = re.search(
        r"if\s*\(\s*\w+\s*==\s*0\s*\)\s*(?:begin)?\s*state\s*<=\s*(\w+)\s*;",
        search_region,
    )
    if m:
        return m.group(1)

    m = re.search(r"\bstate\s*(?:\[[^\]]*\])?\s*=\s*(\w+)\s*;", search_region)
    if m:
        value = m.group(1)
        if states:
            if value in states:
                return value
            if value.isdigit():
                for name, num in states.items():
                    if num == int(value):
                        return name
        elif not value.isdigit():
            return value

    return None


CASE_ENDCASE_TOKEN = re.compile(r"\bcase\b|\bendcase\b")


def find_matching_endcase(text, case_keyword_start):
    """Given text[case_keyword_start:] starting at a 'case' keyword,
    return the (start, end) span of its matching 'endcase' keyword,
    balancing any case/endcase pairs nested inside (e.g. a
    `case(some_other_signal) ... endcase` inside one state's block)."""
    depth = 0
    for m in CASE_ENDCASE_TOKEN.finditer(text, case_keyword_start):
        if m.group(0) == "case":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return m.start(), m.end()
    return None


def extract_case_block(module_body):
    masked = _mask_comments(module_body)
    m = re.search(r"case\s*\(\s*state\s*\)", masked)
    if not m:
        return None
    content_start = m.end()

    end_span = find_matching_endcase(masked, m.start())
    if end_span is None:
        return None
    endcase_start, _endcase_end = end_span

    return module_body[content_start:endcase_start]


def split_state_blocks(case_block, state_names):
    """Split the case(state) body into per-state text chunks.

    Case labels may group multiple states together (a common Verilog
    idiom for states that share identical behavior), e.g.:
        no_ack_wdata, no_ack_waddr: begin ... end
    Each name in such a group is treated as its own label sharing the
    same block text.
    """
    masked_case_block = _mask_comments(case_block)

    names_alt = "|".join(re.escape(s) for s in state_names)
    label_pattern = re.compile(
        r"((?:(?:" + names_alt + r")\s*,\s*)*(?:" + names_alt + r"))\s*:"
    )
    labels = list(label_pattern.finditer(masked_case_block))

    default_match = re.search(r"\bdefault\s*:", masked_case_block)
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
    """Return (transitions, warnings) for one state block: transitions
    is a list of (to_state, condition) tuples, warnings is a list of the
    same "[PARSER WARNING] ..." strings this function also prints —
    printed for CLI parity, and returned so callers (confidence scoring,
    API responses) can see them without scraping stdout."""
    transitions = []
    warnings = []

    # Scan the comment-masked version throughout: a comment containing an
    # English word like "if" or "else" must never be mistaken for real
    # Verilog control flow. Masking preserves length/position exactly,
    # and never alters real code, so every capture below is identical to
    # what scanning block_text directly would give outside of comments.
    masked_block = _mask_comments(block_text)

    keyword_matches = list(KEYWORD_TOKEN.finditer(masked_block))

    if not keyword_matches:
        # No if/else at all in this block -- expect at most one
        # unconditional assignment.
        assigns = ASSIGNMENT.findall(masked_block)
        if len(assigns) == 1:
            transitions.append((assigns[0], "unconditional"))
        elif len(assigns) == 0:
            pass
        else:
            warning = (
                f"[PARSER WARNING] Could not parse transition in state "
                f"{state_name} — skipped"
            )
            print(warning)
            warnings.append(warning)
        return transitions, warnings

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
            paren_start = masked_block.find("(", kw.end())
            next_kw_start = (
                keyword_matches[i + 1].start()
                if i + 1 < len(keyword_matches)
                else len(masked_block)
            )
            if paren_start == -1 or paren_start > next_kw_start:
                parse_ok = False
                continue
            paren_end = find_matching_paren(masked_block, paren_start)
            if paren_end is None:
                parse_ok = False
                continue
            condition = masked_block[paren_start + 1 : paren_end].strip()
            segment_start = paren_end + 1

        segment_end = (
            keyword_matches[i + 1].start()
            if i + 1 < len(keyword_matches)
            else len(masked_block)
        )
        segments.append((condition, masked_block[segment_start:segment_end]))

    if not parse_ok:
        warning = (
            f"[PARSER WARNING] Could not parse transition in state "
            f"{state_name} — skipped"
        )
        print(warning)
        warnings.append(warning)
        return transitions, warnings

    for condition, segment in segments:
        assigns = ASSIGNMENT.findall(segment)
        if len(assigns) == 1:
            transitions.append((assigns[0], condition))
        elif len(assigns) == 0:
            # Condition guards something other than a state transition;
            # not an error by itself.
            continue
        else:
            warning = (
                f"[PARSER WARNING] Could not parse transition in state "
                f"{state_name} — skipped"
            )
            print(warning)
            warnings.append(warning)

    return transitions, warnings


def build_graph(case_block, state_names):
    """Return (graph, parser_warnings)."""
    blocks = split_state_blocks(case_block, state_names)
    graph = {name: [] for name in state_names}
    parser_warnings = []

    for state_name, block_text in blocks.items():
        transitions, state_warnings = parse_transitions_for_state(state_name, block_text)
        graph[state_name].extend(transitions)
        parser_warnings.extend(state_warnings)

    return graph, parser_warnings


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


def score_severity(state_name, graph):
    """Score a deadlocked state's severity.

    HIGH:   3+ other states have a transition into it, OR its name puts
            it on a critical write/read path ("wdata", "rdata",
            "wr_resp") — either condition alone is enough for HIGH.
    MEDIUM: 1-2 other states transition into it.
    LOW:    no other state transitions into it (only reachable from
            reset, or self-looping).

    This is purely a reporting-layer annotation computed from the
    already-detected deadlock list; it does not change which states
    check_deadlocks() flags.
    """
    critical_path_hint = any(
        token in state_name.lower() for token in ("wdata", "rdata", "wr_resp")
    )
    if critical_path_hint:
        return "HIGH"

    incoming_states = set()
    for source_state, transitions in graph.items():
        if source_state == state_name:
            continue
        if any(to_state == state_name for to_state, _cond in transitions):
            incoming_states.add(source_state)

    if len(incoming_states) >= 3:
        return "HIGH"
    if len(incoming_states) >= 1:
        return "MEDIUM"
    return "LOW"


PROTOCOL_KEYWORDS = ("axi", "uart", "spi", "i2c", "apb", "ahb")


def classify_bug_pattern(state_name, condition, graph):
    """Classify a deadlock's blocking condition into one of five named
    bug patterns, checked in this priority order (first match wins):

    1. handshake_deadlock — condition contains both 'valid' and 'ready'
    2. single_signal_deadlock — condition is a single bare reference to
       exactly one external (handshake) signal: no boolean operators
       (&&/||) and no comparison, so there's nothing else going on
    3. counter_overflow_deadlock — condition contains a counter
       comparison (==/!= N) and does NOT also reference an external
       handshake signal (if it did, this is really a handshake/
       protocol bug that happens to also check a counter, which the
       earlier/later patterns describe better)
    4. missing_default_deadlock — the state has 2+ distinct branches
       (an if/else-if chain) and none of them is unconditional; a
       single lone condition doesn't count as "missing a default" in
       the way a multi-way branch with no fallback does
    5. protocol_violation_deadlock — state name or condition mentions
       a known bus/protocol keyword

    Falls back to "unclassified_deadlock" if nothing matches. This is
    purely a reporting-layer annotation, like score_severity — it does
    not change which states check_deadlocks() flags.
    """
    condition_lower = condition.lower()
    name_lower = state_name.lower()
    transitions = graph.get(state_name, [])

    has_boolean_op = "&&" in condition or "||" in condition
    has_comparison = bool(re.search(r"==|!=", condition))
    external_signal_tokens = {
        tok
        for tok in re.findall(r"[a-zA-Z_]\w*", condition)
        if is_handshake_condition(tok)
    }

    if "valid" in condition_lower and "ready" in condition_lower:
        return {
            "pattern": "handshake_deadlock",
            "description": (
                "FSM waits for a handshake that never completes. "
                "Common in AXI protocol implementations where "
                "valid/ready pairs must both assert to proceed."
            ),
        }

    if not has_boolean_op and not has_comparison and len(external_signal_tokens) == 1:
        return {
            "pattern": "single_signal_deadlock",
            "description": (
                "FSM stalls on a single external signal with no "
                "fallback. Any failure of that signal permanently "
                "freezes the state machine."
            ),
        }

    has_counter_comparison = bool(re.search(r"==\s*\d+|!=\s*\d+", condition))
    if has_counter_comparison and not external_signal_tokens:
        return {
            "pattern": "counter_overflow_deadlock",
            "description": (
                "FSM depends on a counter reaching a specific value. "
                "If the counter never reaches that value due to "
                "upstream logic, the FSM stalls indefinitely."
            ),
        }

    if len(transitions) >= 2 and not any(cond == "unconditional" for _to, cond in transitions):
        return {
            "pattern": "missing_default_deadlock",
            "description": (
                "FSM has no default transition. Unexpected input "
                "combinations leave the state machine with no valid "
                "next state."
            ),
        }

    if any(kw in name_lower or kw in condition_lower for kw in PROTOCOL_KEYWORDS):
        return {
            "pattern": "protocol_violation_deadlock",
            "description": (
                "FSM implements a hardware protocol and deadlocks on "
                "a protocol handshake. This class of bug is "
                "particularly dangerous as it can cause system-wide "
                "bus lockup, not just local FSM failure."
            ),
        }

    return {
        "pattern": "unclassified_deadlock",
        "description": (
            "Deadlock condition does not match known patterns. Manual "
            "review recommended."
        ),
    }


def compute_confidence(module_name, states, graph, parser_warnings, reset_state=None):
    """Score 0-100: how completely the parser extracted this FSM.

    Deductions from a starting score of 100:
    - 5 points per parser warning (a skipped/unparseable transition)
    - 3 points per state with zero outgoing transitions
    - 20 points if the reset state couldn't be determined
    - 15 points if fewer than 3 states were found (likely incomplete parse)
    - 10 points if more than 30% of states have empty transition lists
    Floored at 0.

    `reset_state` isn't part of the signature the spec for this
    function gave (compute_confidence(module_name, states, graph,
    parser_warnings)) — it's added as a keyword arg at the end, kept
    optional so any caller using that exact positional signature still
    works. It's needed because "reset state could not be determined"
    is one of the stated deductions and there's no way to detect that
    from states/graph alone.

    `module_name` isn't used in the scoring itself (no rule references
    it) — kept in the signature to match the spec exactly.
    """
    _ = module_name  # unused, kept for signature compatibility
    score = 100

    score -= 5 * len(parser_warnings)

    zero_exit_states = [name for name, transitions in graph.items() if not transitions]
    score -= 3 * len(zero_exit_states)

    if reset_state is None:
        score -= 20

    if len(states) < 3:
        score -= 15

    if states and (len(zero_exit_states) / len(states)) > 0.3:
        score -= 10

    return max(0, score)


def print_report(
    filename, states, reset_state, unreachable, deadlocks, graph, module_name=None, confidence=None
):
    print("=== FSM Analysis Report ===")
    print(f"File: {filename}")
    if module_name is not None:
        print(f"Module: {module_name}")
    if confidence is not None:
        print(f"Parse confidence: {confidence}%")
    print(f"States found: {len(states)}")
    print(f"Reset state: {reset_state}")
    print()

    print("--- Reachability ---")
    if not unreachable:
        print(f"[PASS] All {len(states)} states reachable from reset.")
    else:
        for state_name in unreachable:
            print(f"[WARN] Unreachable state: {state_name}")
            print(f"       No path from '{reset_state}' to '{state_name}' exists.")
    print()

    print("--- Deadlock Detection ---")
    if not deadlocks:
        print("[PASS] No deadlocks detected.")
    else:
        for state_name, condition in deadlocks:
            severity = score_severity(state_name, graph)
            pattern_info = classify_bug_pattern(state_name, condition, graph)
            print(f"[WARN] Potential deadlock: state '{state_name}'  [SEVERITY: {severity}]")
            print(f"       Bug pattern: {pattern_info['pattern']}")
            print(f"       Pattern description: {pattern_info['description']}")
            print(f"       Blocking condition: {condition}")
            print(
                f"       Failure scenario: If {condition} never asserts while"
            )
            print(f"       the FSM is in '{state_name}', there is no timeout or")
            print("       unconditional exit. The FSM stalls indefinitely.")
    print()

    total_warnings = len(unreachable) + len(deadlocks)
    print(f"=== Summary: {total_warnings} warning(s) found ===")


def find_module_names(text):
    """Return every `module <name>` declaration in a Verilog source
    file, in order of appearance."""
    return re.findall(r"\bmodule\s+(\w+)\b", text)


def parse_fsm(filepath):
    """Read a Verilog file and extract every FSM it contains.

    A module counts as an FSM only if it has both localparam state
    definitions AND a case(state) block; anything else (testbenches,
    wrapper/instantiation-only modules, modules with unrelated case
    statements) is silently skipped — it's not an FSM to analyze.

    Returns a list of dicts, one per FSM module found:
        [{"module_name": ..., "states": {...}, "reset_state": ...,
          "graph": {...}, "parser_warnings": [...]}, ...]
    (possibly empty, if the file has no FSM modules at all).
    parser_warnings is the list of "[PARSER WARNING] ..." strings (if
    any) produced while parsing that module's transitions — the same
    ones printed to stdout, also returned so callers (confidence
    scoring, API responses) don't have to scrape stdout for them.
    """
    with open(filepath, "r") as f:
        text = f.read()

    results = []
    for module_name in find_module_names(text):
        module_body = extract_module_body(text, module_name)
        if module_body is None:
            continue

        states = extract_states(module_body)
        if not states:
            continue

        case_block = extract_case_block(module_body)
        if case_block is None:
            continue

        reset_state = extract_reset_state(module_body, states)
        graph, parser_warnings = build_graph(case_block, list(states.keys()))

        results.append(
            {
                "module_name": module_name,
                "states": states,
                "reset_state": reset_state,
                "graph": graph,
                "parser_warnings": parser_warnings,
            }
        )

    return results


def get_fsm(fsm_list, module_name):
    """Return the {"module_name", "states", "reset_state", "graph"}
    dict for one module out of parse_fsm()'s result list, or None."""
    for entry in fsm_list:
        if entry["module_name"] == module_name:
            return entry
    return None


def analyze_and_report(filepath, states, reset_state, graph, module_name=None, parser_warnings=None):
    """Run reachability + deadlock checks, print the report, and return
    the list of (state_name, condition) deadlocks found.

    `module_name` and `parser_warnings` are optional (and kept at the
    end, defaulting to None/[]) so fix_and_verify's existing internal
    call — analyze_and_report(output_filepath, states, reset_state,
    graph), unchanged, since fix_and_verify itself is not to be
    touched — keeps working exactly as before; it just won't show a
    Module:/confidence line during the [VERIFY] re-report. main()
    passes both explicitly to get the full report.
    """
    unreachable = check_reachability(graph, reset_state, list(states.keys()))
    deadlocks = check_deadlocks(graph, reset_state)
    confidence = None
    if module_name is not None:
        confidence = compute_confidence(
            module_name, states, graph, parser_warnings or [], reset_state=reset_state
        )
    print_report(
        filepath, states, reset_state, unreachable, deadlocks, graph,
        module_name=module_name, confidence=confidence,
    )
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


def locate_state_block_span(source, state_name, state_names, module_name=TARGET_MODULE):
    """Find the (start, end) character span of a state's full case-label
    block ("state_name: begin ... end") within `source`, scoped to
    `module_name`'s case(state) block specifically.

    Scoping to one module matters as soon as a file has more than one
    FSM: without it, this would always find the *first* case(state) in
    the whole file, silently editing the wrong module whenever the
    target state belongs to a later one.

    Returns None if the module or the state isn't found. Returns the
    string "grouped" if the state shares a comma-joined case label with
    other states (e.g. "no_ack_wdata, no_ack_waddr:") — callers should
    refuse to surgically edit such a block, since doing so would also
    touch the other state(s) sharing it.
    """
    masked_source = _mask_comments(source)

    module_match = re.search(
        r"\bmodule\s+" + re.escape(module_name) + r"\b(.*?)\bendmodule\b",
        masked_source,
        re.DOTALL,
    )
    if not module_match:
        return None
    module_start = module_match.start(1)
    masked_module_text = masked_source[module_start : module_match.end(1)]

    case_open_match = re.search(r"case\s*\(\s*state\s*\)", masked_module_text)
    if not case_open_match:
        return None
    case_content_start = case_open_match.end()
    end_span = find_matching_endcase(masked_module_text, case_open_match.start())
    if end_span is None:
        return None
    case_endcase_start, _case_endcase_end = end_span

    case_start = module_start + case_content_start
    case_end = module_start + case_endcase_start

    names_alt = "|".join(re.escape(s) for s in state_names)
    label_pattern = re.compile(
        r"((?:(?:" + names_alt + r")\s*,\s*)*(?:" + names_alt + r"))\s*:"
    )
    labels = list(label_pattern.finditer(masked_source, case_start, case_end))

    default_match = re.search(r"\bdefault\s*:", masked_source[case_start:case_end])
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


def _template_fix(state_name, condition, fsm_source, module_name=TARGET_MODULE):
    """Deterministic fallback fix: reuse this FSM's existing '== 15'
    timeout-counter pattern to add a timeout escape to a state whose
    only exit is a handshake condition. Returns None if the state's
    block doesn't have the simple "if (...) begin ... end" shape this
    template can safely extend.
    """
    module_body = extract_module_body(fsm_source, module_name)
    if module_body is None:
        return None
    states = extract_states(module_body)
    reset_state = extract_reset_state(module_body, states) or "idle"

    span = locate_state_block_span(
        fsm_source, state_name, list(states.keys()), module_name=module_name
    )
    if not isinstance(span, tuple):
        return None
    original_block = fsm_source[span[0] : span[1]]
    masked_block = _mask_comments(original_block)

    if_match = re.search(r"\bif\s*\(", masked_block)
    if not if_match:
        return None
    paren_start = masked_block.find("(", if_match.start())
    paren_end = find_matching_paren(masked_block, paren_start)
    if paren_end is None:
        return None
    begin_idx = masked_block.find("begin", paren_end)
    if begin_idx == -1:
        return None
    end_span = find_matching_end(masked_block, begin_idx)
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


def generate_fix(state_name, condition, fsm_source, module_name=TARGET_MODULE):
    """Generate a targeted fix for one deadlocked state block.

    Provider order: Groq (GROQ_API_KEY) first, then Anthropic
    (ANTHROPIC_API_KEY), then a deterministic template fix that reuses
    this FSM's own existing '== 15' timeout pattern. Each step falls
    through to the next on a missing key, a missing package, or a
    failed call — a live demo shouldn't go down over a network blip or
    a missing key. Whichever path is used is always announced on
    stdout, so the fallback is never mistaken for a live LLM result.

    `module_name` only matters for the template fallback, which needs
    to know which module's case(state) block to locate the state in
    when a file has more than one FSM.

    Returns (fixed_block, provider) where provider is one of "groq",
    "anthropic", or "template".
    """
    prompt = _build_fix_prompt(state_name, condition, fsm_source)

    groq_key = os.environ.get("GROQ_API_KEY")
    if groq_key:
        try:
            return _call_groq(prompt, groq_key), "groq"
        except Exception as exc:
            print(f"[FIX ENGINE] Groq call failed ({exc}) — trying next provider.")

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if anthropic_key:
        try:
            return _call_anthropic(prompt, anthropic_key), "anthropic"
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

    fixed_block = _template_fix(state_name, condition, fsm_source, module_name=module_name)
    if fixed_block is None:
        raise RuntimeError(
            f"Could not generate a fix for state '{state_name}' "
            f"(neither the LLM nor the template fallback produced one)"
        )
    return fixed_block, "template"


def build_diff_text(original_block, fixed_block, state_name):
    """Return a unified diff string comparing the original and fixed
    versions of one state block. Shared by show_diff (CLI) and api.py
    (JSON responses) so the diff is built in exactly one place."""
    diff_lines = difflib.unified_diff(
        original_block.splitlines(),
        fixed_block.splitlines(),
        fromfile=f"{state_name} (original)",
        tofile=f"{state_name} (fixed)",
        lineterm="",
    )
    return "\n".join(diff_lines)


def show_diff(original_block, fixed_block, state_name):
    print(f"\n--- DIFF for state '{state_name}' ---")
    print(build_diff_text(original_block, fixed_block, state_name))
    print("--- END DIFF ---\n")


def _fix_description(provider):
    if provider == "template":
        return "Added timeout escape using == 15 counter pattern (deterministic template fallback)"
    return f"LLM-generated timeout escape fix (via {provider})"


def apply_fix(filepath, state_name, fixed_block, output_filepath=None, module_name=TARGET_MODULE):
    """Surgically replace only `state_name`'s case-label block in the
    file at `filepath` with `fixed_block`, and write the result to
    `output_filepath` (default: <filepath without .v>_fixed.v).

    Before writing, re-parses the edited source and refuses to save if
    the module's state set changed — a cheap sanity check that the
    surgical edit didn't corrupt the FSM structure.
    """
    with open(filepath, "r") as f:
        source = f.read()

    module_body = extract_module_body(source, module_name)
    if module_body is None:
        print(f"[ERROR] Module '{module_name}' not found in {filepath}")
        return None
    states = extract_states(module_body)

    span = locate_state_block_span(
        source, state_name, list(states.keys()), module_name=module_name
    )
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

    fixed_module_body = extract_module_body(fixed_source, module_name)
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


def fix_and_verify(filepath, deadlocks, graph, states, reset_state, module_name=TARGET_MODULE):
    """Generate, show, and apply a fix for every detected deadlock, then
    re-run the full analysis on the fixed file to verify it worked.

    Returns (output_filepath, fixed_deadlocks, fixes_applied), where
    fixes_applied is a list of {"state", "diff", "fix_description"}
    dicts — one per deadlock a fix was successfully applied for — or
    None if there were no deadlocks to fix.
    """
    if not deadlocks:
        print("[INFO] No deadlocks to fix.")
        return None

    output_filepath = filepath.replace(".v", "_fixed.v")
    current_read_path = filepath
    fixes_applied = []

    for state_name, condition in deadlocks:
        print(f"\n[FIX ENGINE] Generating fix for '{state_name}'...")

        with open(current_read_path, "r") as f:
            fsm_source = f.read()

        fixed_block, provider = generate_fix(
            state_name, condition, fsm_source, module_name=module_name
        )

        module_body = extract_module_body(fsm_source, module_name)
        cur_states = extract_states(module_body) if module_body else {}
        span = locate_state_block_span(
            fsm_source, state_name, list(cur_states.keys()), module_name=module_name
        )
        if isinstance(span, tuple):
            original_block = fsm_source[span[0] : span[1]]
        else:
            original_block = "<could not locate original block>"

        diff_text = build_diff_text(original_block, fixed_block, state_name)
        print(f"\n--- DIFF for state '{state_name}' ---")
        print(diff_text)
        print("--- END DIFF ---\n")

        result_path = apply_fix(
            current_read_path,
            state_name,
            fixed_block,
            output_filepath=output_filepath,
            module_name=module_name,
        )
        if not result_path:
            print(f"[ERROR] Fix application failed for '{state_name}'")
            continue

        current_read_path = result_path
        fixes_applied.append(
            {
                "state": state_name,
                "diff": diff_text,
                "fix_description": _fix_description(provider),
            }
        )

    print("\n[VERIFY] Re-running analysis on fixed file...")
    print("=" * 50)
    fixed_fsms = parse_fsm(output_filepath)
    fixed_entry = get_fsm(fixed_fsms, module_name)
    if fixed_entry is None:
        print(f"[ERROR] Could not parse fixed file {output_filepath}")
        return None
    fixed_deadlocks = analyze_and_report(
        output_filepath, fixed_entry["states"], fixed_entry["reset_state"], fixed_entry["graph"]
    )
    return output_filepath, fixed_deadlocks, fixes_applied


def main():
    if len(sys.argv) < 2:
        print("Usage: python fsm_analyzer.py <verilog_file> [--fix]")
        sys.exit(1)

    filepath = sys.argv[1]
    auto_fix = "--fix" in sys.argv

    fsms = parse_fsm(filepath)
    if not fsms:
        print(f"[ERROR] No FSM found in {filepath}")
        sys.exit(1)

    # Prefer TARGET_MODULE when present, so behavior on axi_master.v is
    # exactly what it always was. Otherwise this file has no module by
    # that name — fall back to the one FSM found (or, if several, the
    # first one, noting the others) so the CLI works on any Verilog
    # file, not only axi_master.v.
    target = get_fsm(fsms, TARGET_MODULE)
    if target is None:
        if len(fsms) > 1:
            other_names = ", ".join(f["module_name"] for f in fsms)
            print(
                f"[INFO] Module '{TARGET_MODULE}' not found; multiple FSMs "
                f"present ({other_names}). Analyzing '{fsms[0]['module_name']}'."
            )
        target = fsms[0]

    module_name = target["module_name"]
    states, reset_state, graph = target["states"], target["reset_state"], target["graph"]
    parser_warnings = target.get("parser_warnings", [])
    deadlocks = analyze_and_report(
        filepath, states, reset_state, graph,
        module_name=module_name, parser_warnings=parser_warnings,
    )

    if auto_fix and deadlocks:
        fix_and_verify(filepath, deadlocks, graph, states, reset_state, module_name=module_name)
    elif deadlocks:
        print("\n[INFO] Run with --fix flag to automatically generate and apply fixes.")


if __name__ == "__main__":
    main()
