## SYSTEM STATE
fsm_analyzer.py: VALIDATED
test_fsm_analyzer.py: VALIDATED
axi_master.v: REAL PRODUCTION FILE (replaces the synthetic fixture Claude Code originally wrote; now contains the real testbench, axi_master, and axi4_slave modules)
Last updated by: Claude Code
Last updated at: 2026-09-21T10:46:45Z

## ARCHITECTURE DECISIONS
- Parser uses Python re only — no third party Verilog libraries
- Graph is plain Python dict of lists — no networkx
- Only axi_master module is parsed — testbench and axi4_slave are ignored
- Deadlock detection flags states where ALL exits are handshake-dependent with no timeout (== 15) escape
- wdata_last is the known deadlock in axi_master.v — tool must catch this

## PARSER CHANGES (validating against the real axi_master.v)
- Case labels can group multiple states on one line (e.g.
  `no_ack_wdata, no_ack_waddr: begin ... end`). split_state_blocks now
  matches comma-joined label groups and assigns the same block text to
  every name in the group, instead of only the last name directly
  preceding the colon. Previously this silently dropped the first
  name's transitions (empty exit list, no parser warning) whenever a
  label was combined this way.
- If/else-if conditions in the real file contain nested parentheses
  (e.g. `if ((m_axi_rvalid == 1'b1) && (m_axi_rlast != 1))`). The old
  condition regex (`\(([^)]*)\)`) stopped at the first `)` and produced
  truncated, unbalanced-paren condition strings. Condition extraction
  now does a manual balanced-paren scan (find_matching_paren) instead
  of a single non-nesting regex.
- Both fixes were verified by dumping the raw graph dict and per-state
  exit list for the real file; output was cross-checked against the
  source lines by hand before re-running the full report and test
  suite.

## TASK QUEUE
[x] Build fsm_analyzer.py — Claude Code
[x] Validate against axi_master.v — Claude Code
[x] Build test_fsm_analyzer.py — Claude Code
[ ] Build web UI — Devin
[ ] Build state diagram visualizer — Devin
[ ] Add --suggest-fix flag — Devin
[ ] Polish output formatting — Devin

## DO NOT TOUCH
fsm_analyzer.py core parser logic — owned by Claude Code, validated against axi_master.v
Any function that builds the graph dict
The deadlock detection logic in Check B
