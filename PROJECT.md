## SYSTEM STATE
fsm_analyzer.py: VALIDATED (analysis + fix engine)
api.py: VALIDATED (FastAPI wrapper — /analyze, /fix, /download all tested live via curl and requests)
test_fsm_analyzer.py: VALIDATED (10/10 tests passing)
axi_master.v: REAL PRODUCTION FILE (replaces the synthetic fixture Claude Code originally wrote; now contains the real testbench, axi_master, and axi4_slave modules)
axi_master_fixed.v: GENERATED OUTPUT — produced by `python fsm_analyzer.py axi_master.v --fix`; not hand-maintained, regenerate via the tool
Fix engine: BUILT — generate_fix / show_diff / apply_fix / fix_and_verify added; provider order Groq -> Anthropic -> template fallback; verified end-to-end via the template fallback (wdata_last deadlock -> 0 warnings); neither live provider reachable/validated from this sandbox (see FIX ENGINE section)
Last updated by: Claude Code
Last updated at: 2026-09-21T11:28:46Z

## ARCHITECTURE DECISIONS
- Parser uses Python re only — no third party Verilog libraries
- Graph is plain Python dict of lists — no networkx
- Only axi_master module is parsed — testbench and axi4_slave are ignored
- Deadlock detection flags states where ALL exits are handshake-dependent with no timeout (== 15) escape
- wdata_last is the known deadlock in axi_master.v — tool must catch this
- Web API is FastAPI + uvicorn + python-multipart (file uploads). api.py
  is a thin HTTP adapter over fsm_analyzer.py — it imports and calls
  fsm_analyzer's functions directly (parse_fsm, check_reachability,
  check_deadlocks, fix_and_verify) and duplicates none of their logic;
  it only shapes JSON responses and maps failures to clean JSON errors
- CORS is wide open (allow_origins=["*"]) since this is a hackathon demo
  API with no auth; tighten before any real deployment
- Uploaded files are handled entirely inside
  `tempfile.TemporaryDirectory()` per request — nothing from an upload
  is ever written outside that directory, and it's deleted when the
  request finishes, so no upload persists on disk

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
  runs generate_fix + apply_fix per deadlock (building each diff via
  the shared build_diff_text helper), then calls parse_fsm +
  analyze_and_report on the fixed file to verify 0 deadlocks remain.
  Returns (fixed_filepath, fixed_deadlocks, fixes_applied), where
  fixes_applied is a list of {"state", "diff", "fix_description"}
  dicts — this is what api.py's /fix and /download endpoints consume
  directly, with no duplicated diff/fix logic.
- generate_fix now returns (fixed_block, provider) instead of just the
  block text, so callers (currently only fix_and_verify) know which
  path produced it ("groq" / "anthropic" / "template") and can build a
  human-readable fix_description.
- build_diff_text(original_block, fixed_block, state_name) factored out
  of show_diff so the same diff-building code is reusable by api.py.
  Also fixed a formatting bug while doing this: show_diff previously
  printed a blank line after every diff line (splitlines(keepends=True)
  content already ended in "\n", then print() added another), which
  produced a double-spaced, hard-to-read diff in the CLI and would have
  leaked into the API's JSON diff string too.
- main() now accepts an optional --fix flag:
  `python fsm_analyzer.py axi_master.v --fix`.
- Verified: axi_master_fixed.v differs from axi_master.v only inside
  the wdata_last block (a 5-line else-if/else insertion); re-running
  the analyzer on the fixed file reports
  "=== Summary: 0 warning(s) found ===".

## WEB API (api.py — FastAPI wrapper over fsm_analyzer.py)
- POST /analyze — parses the uploaded .v file, returns states_found,
  reset_state, reachability, deadlocks (with per-state scenario text),
  the full graph (JSON-serializable as-is: tuples become arrays), and
  summary.total_warnings.
- POST /fix — runs the full fix engine (fsm.fix_and_verify) and returns
  fixes_applied (state/diff/fix_description per deadlock fixed),
  fixed_file_content (the whole fixed file as a string), and
  verification.{status,warnings_before,warnings_after}.
- POST /download — same fix engine run as /fix, but returns the fixed
  file itself as a `Response` with
  `Content-Disposition: attachment; filename="<name>_fixed.v"` and
  `Content-Type: text/plain`, not JSON.
- CORS: `CORSMiddleware(allow_origins=["*"], allow_methods=["*"],
  allow_headers=["*"])`.
- Errors: a shared `FSMError` exception + handler on all three
  endpoints returns `{"error": ..., "detail": ...}` with a 4xx/5xx
  status for empty uploads, files with no axi_master FSM, non-UTF-8
  uploads, and fix-engine failures, instead of a raw traceback.
- Temp files: every request's upload is written inside
  `tempfile.TemporaryDirectory()` and nothing survives past the
  request — verified by checking /tmp before/after a full round of
  curl calls to all three endpoints.
- Live-tested: `uvicorn api:app --port 8000`, then curl against
  /analyze, /fix, and /download with axi_master.v — all three matched
  the expected shapes exactly (total_warnings: 1 on /analyze,
  warnings_after: 0 and non-empty fixed_file_content on /fix, a
  downloaded file that diffs from the original by only the same 5-line
  wdata_last insertion and re-parses to 0 warnings on /download).
- Not yet tested: file uploads other than a clean UTF-8 .v (e.g. a
  .txt with a .v extension, extremely large files, concurrent
  requests). The empty-file and non-UTF-8 and no-FSM-found error paths
  were exercised; edge cases beyond that weren't.

## TASK QUEUE
[x] Build fsm_analyzer.py — Claude Code
[x] Validate against axi_master.v — Claude Code
[x] Build test_fsm_analyzer.py — Claude Code
[x] Add LLM fix engine (generate_fix/show_diff/apply_fix/fix_and_verify) — Claude Code
[x] Validate fix engine against axi_master.v (fallback path; live-API path unverified, no key in build env) — Claude Code
[x] Build api.py (FastAPI: /analyze, /fix, /download) — Claude Code
[x] Add CORS middleware — Claude Code
[x] Live-test all 3 endpoints with curl against axi_master.v — Claude Code
[x] Add TEST 8/9/10 (live API tests via requests) to test_fsm_analyzer.py — Claude Code
[ ] Build web UI — Devin
[ ] Build state diagram visualizer (consume /analyze's `graph` field) — Devin
[ ] Diff viewer (UI) — consume /fix's `fixes_applied[].diff` — Devin
[ ] Download button — wire to POST /download — Devin
[ ] Polish output formatting — Devin
[ ] Wire a real GROQ_API_KEY or ANTHROPIC_API_KEY in the deployed
    environment and confirm the live-LLM fix path (not just the
    template fallback) — whoever owns the demo environment, since
    neither key has been validated from this sandbox

## DO NOT TOUCH
fsm_analyzer.py core parser logic — owned by Claude Code, validated against axi_master.v
Any function that builds the graph dict
The deadlock detection logic in Check B
The fix engine's surgical-replacement and re-verification logic (locate_state_block_span, apply_fix's state-set safety check, fix_and_verify's re-parse) — owned by Claude Code
api.py's error handling (FSMError + the try/except in each endpoint) and its temp-file handling (tempfile.TemporaryDirectory per request) — owned by Claude Code
