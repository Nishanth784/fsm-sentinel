## SYSTEM STATE
fsm_analyzer.py: VALIDATED
test_fsm_analyzer.py: VALIDATED
Last updated by: Claude Code
Last updated at: 2026-09-21T10:33:11Z

## ARCHITECTURE DECISIONS
- Parser uses Python re only — no third party Verilog libraries
- Graph is plain Python dict of lists — no networkx
- Only axi_master module is parsed — testbench and axi4_slave are ignored
- Deadlock detection flags states where ALL exits are handshake-dependent with no timeout (== 15) escape
- wdata_last is the known deadlock in axi_master.v — tool must catch this

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
