## SYSTEM STATE
fsm_analyzer.py: VALIDATED (analysis + fix engine)
test_fsm_analyzer.py: VALIDATED (7/7 tests passing)
axi_master.v: REAL PRODUCTION FILE (replaces the synthetic fixture Claude Code originally wrote; now contains the real testbench, axi_master, and axi4_slave modules)
axi_master_fixed.v: GENERATED OUTPUT — produced by `python fsm_analyzer.py axi_master.v --fix`; not hand-maintained, regenerate via the tool
Fix engine: BUILT — generate_fix / show_diff / apply_fix / fix_and_verify added; provider order Groq -> Anthropic -> template fallback; verified end-to-end via the template fallback (wdata_last deadlock -> 0 warnings); neither live provider reachable/validated from this sandbox (see FIX ENGINE section)
Last updated by: Claude Code
Last updated at: 2026-09-21T11:10:46Z

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
- generate_fix(state_name, condition, fsm_source) — tries providers in
  order: Groq first (GROQ_API_KEY, OpenAI-compatible endpoint at
  api.groq.com, model from GROQ_MODEL env var, default
  "llama-3.3-70b-versatile"), then Anthropic (ANTHROPIC_API_KEY, model
  "claude-sonnet-5"), then a deterministic template fix. Each step
  falls through to the next on a missing key, a missing package, or a
  failed call — a live demo shouldn't go down over a network blip or a
  missing key. Whichever path runs is always announced on stdout
  ("[FIX ENGINE] ... using deterministic template fix" /
  "Groq call failed (...) — trying next provider") so the fallback is
  never mistaken for a live LLM result.
  NOTE: **neither live provider has been validated end-to-end from the
  build environment.** ANTHROPIC_API_KEY was never available here.
  A GROQ_API_KEY was supplied later and wired in, but this sandbox's
  network policy blocks outbound HTTPS to api.groq.com (proxy returns
  403 connect_rejected — an org-level policy denial, not a bug in the
  code) so the Groq branch immediately falls through to the template
  fix. Both API branches are implemented per spec but only exercised
  in this environment via the fallback path. Before the demo, run
  `python fsm_analyzer.py axi_master.v --fix` with GROQ_API_KEY (and/or
  ANTHROPIC_API_KEY) set from a machine that can actually reach the
  provider, and confirm the diff quality and output.
  Also: the Groq key that was used to smoke-test the fallthrough
  behavior was pasted in plaintext in chat — treat it as compromised
  and rotate it on Groq's console before relying on it for the demo.
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
