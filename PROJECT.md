## SYSTEM STATE
fsm_analyzer.py: VALIDATED (analysis + fix engine)
test_fsm_analyzer.py: VALIDATED (7/7 tests passing)
axi_master.v: REAL PRODUCTION FILE (replaces the synthetic fixture Claude Code originally wrote; now contains the real testbench, axi_master, and axi4_slave modules)
axi_master_fixed.v: GENERATED OUTPUT — produced by `python fsm_analyzer.py axi_master.v --fix`; not hand-maintained, regenerate via the tool
Fix engine: BUILT — generate_fix / show_diff / apply_fix / fix_and_verify added; verified end-to-end (wdata_last deadlock -> 0 warnings)
Last updated by: Claude Code
Last updated at: 2026-09-21T10:57:49Z

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

## FIX ENGINE (added on top of the analyzer)
- generate_fix(state_name, condition, fsm_source) — tries the live
  Anthropic API (model "claude-sonnet-5") first when ANTHROPIC_API_KEY
  is set in the environment. If the key is missing, the 'anthropic'
  package isn't installed, or the API call itself raises, it falls
  back to a deterministic template fix instead of failing the whole
  workflow — a live demo shouldn't go down over a network blip or a
  missing key. The fallback is always announced on stdout
  ("[FIX ENGINE] ... using deterministic template fix") so it's never
  mistaken for a live LLM result. NOTE: this was built and validated
  entirely on the fallback path — no ANTHROPIC_API_KEY was available
  in the build environment, so the live-API branch is implemented per
  spec but has not itself been exercised end-to-end. Wire in a real
  key and re-run `python fsm_analyzer.py axi_master.v --fix` to
  validate that branch before the demo.
- The template fallback reuses this FSM's own existing "== 15" timeout
  counter pattern (picks wr_count vs rd_count by matching read/write
  hints in the state name) and inserts a matching else-if/else clause,
  indentation-matched to the surrounding block.
- show_diff(original_block, fixed_block, state_name) — unified diff via
  difflib, printed between "--- DIFF ---" / "--- END DIFF ---" markers.
- apply_fix(filepath, state_name, fixed_block, output_filepath=None) —
  locates the exact state block via locate_state_block_span (handles
  comma-grouped case labels; refuses to edit a state that shares a
  label with others, to avoid corrupting them), replaces only that
  span, then re-parses the result and refuses to write the file if the
  module's state set changed. Output defaults to
  <name>_fixed.v.
- fix_and_verify(filepath, deadlocks, graph, states, reset_state) —
  runs generate_fix + show_diff + apply_fix per deadlock, then calls
  parse_fsm + analyze_and_report on the fixed file to verify 0
  deadlocks remain. Returns (fixed_filepath, fixed_deadlocks).
- main() now accepts an optional --fix flag:
  `python fsm_analyzer.py axi_master.v --fix`.
- Verified: axi_master_fixed.v differs from axi_master.v only inside
  the wdata_last block (a 5-line else-if/else insertion); re-running
  the analyzer on the fixed file reports
  "=== Summary: 0 warning(s) found ===".

## TASK QUEUE
[x] Build fsm_analyzer.py — Claude Code
[x] Validate against axi_master.v — Claude Code
[x] Build test_fsm_analyzer.py — Claude Code
[x] Add LLM fix engine (generate_fix/show_diff/apply_fix/fix_and_verify) — Claude Code
[x] Validate fix engine against axi_master.v (fallback path; live-API path unverified, no key in build env) — Claude Code
[ ] Build web UI — Devin
[ ] Build state diagram visualizer — Devin
[ ] Diff viewer (UI) — Devin
[ ] Download button — Devin
[ ] Polish output formatting — Devin

## DO NOT TOUCH
fsm_analyzer.py core parser logic — owned by Claude Code, validated against axi_master.v
Any function that builds the graph dict
The deadlock detection logic in Check B
The fix engine's surgical-replacement and re-verification logic (locate_state_block_span, apply_fix's state-set safety check, fix_and_verify's re-parse) — owned by Claude Code
