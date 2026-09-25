## SYSTEM STATE
fsm_analyzer.py: VALIDATED (analysis + fix engine + multi-FSM parsing + severity scoring + confidence scoring + bug-pattern classification)
api.py: VALIDATED (FastAPI wrapper — /analyze, /fix, /download, /visualize, /analyze/batch, /compare, /export/pdf, all tested live via curl and requests, all multi-FSM aware)
test_fsm_analyzer.py: VALIDATED (21/21 tests passing)
axi_master.v: REAL PRODUCTION FILE. Contains 4 modules: tb_connect_m_s
  (testbench), connect_m_s (instantiation-only wrapper), axi_master (14
  states, 1 deadlock), axi4_slave (17 states, 0 deadlocks, 1 unreachable
  state). parse_fsm() correctly skips the first two (no FSM structure)
  and analyzes the latter two.
axi_master_fixed.v: GENERATED OUTPUT — produced by `python fsm_analyzer.py axi_master.v --fix`; not hand-maintained, regenerate via the tool. Also used as v2 input for TEST 16 (/compare).
test_cases/unreachable_fsm.v: BUILT — synthetic fixture with a
  deliberately unreachable state ('orphan'), used by TEST 14
Fix engine: BUILT — generate_fix / show_diff / apply_fix / fix_and_verify added; provider order Groq -> Anthropic -> template fallback; verified end-to-end via the template fallback (wdata_last deadlock -> 0 warnings); neither live provider reachable/validated from this sandbox (see FIX ENGINE section)
Batch analysis (/analyze/batch): BUILT/VALIDATED
Historical comparison (/compare): BUILT/VALIDATED
Confidence scoring (compute_confidence): BUILT/VALIDATED
PDF export (/export/pdf): BUILT/VALIDATED
Bug pattern classification (classify_bug_pattern): BUILT/VALIDATED
Webhook support (webhook_url on /analyze and /analyze/batch): BUILT/VALIDATED
frontend/index.html: BUILT/VALIDATED — single-file UI (HTML+CSS+JS, D3 v7.8.5 from cdnjs, JetBrains Mono), no build step; open directly in a browser with the backend on :8000. 5-page flow upload -> analysis (layered left-to-right state diagram + report) -> fix diff -> verification (fixed content re-sent through /analyze + /visualize) -> download summary; plus .zip batch view and v1/v2 compare view. Verified end-to-end against axi_master.v in Chrome via Playwright: wdata_last pulses red, /fix diff renders, post-fix re-analysis 0 warnings for axi_master, axi_master_fixed.v downloads, Back works on every page, 0 console errors. See frontend/README.md
Last updated by: Devin (frontend)
Last updated at: 2026-09-25T10:45:00Z

## ARCHITECTURE DECISIONS
- Parser uses Python re only — no third party Verilog libraries
- Graph is plain Python dict of lists — no networkx
- parse_fsm() now analyzes every module in a file, not just axi_master
  (see MULTI-FSM SUPPORT below). A module counts as an FSM only if it
  has both localparam states AND a case(state) block; testbenches and
  instantiation-only wrapper modules are silently skipped as "not an
  FSM", not errors.
- Deadlock detection flags states where ALL exits are handshake-dependent with no timeout (== 15) escape
- wdata_last is the known deadlock in axi_master.v — tool must catch this
- All structural regex scanning (case/endcase balance, if/else/begin/end
  keyword matching, case labels, default:) runs on a comment-masked copy
  of the text via _mask_comments(), never on raw source. This is load-
  bearing, not cosmetic: axi4_slave's real body has English comments
  containing words like "case", and one such comment was observed to
  silently corrupt case/endcase balance matching before this was fixed.
  Masking preserves exact character length/position, so every offset
  found in masked text is valid for slicing the original — actual
  extraction, and everything apply_fix() writes back to disk, always
  comes from the original unmasked text.
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
- New libraries: `reportlab` (PDF generation for /export/pdf) and
  `httpx` (async HTTP client for webhook POSTs). `zipfile` (batch
  ZIP handling) is stdlib, no install needed.
- compute_confidence() and classify_bug_pattern() are pure reporting-
  layer functions computed from check_deadlocks()'s already-decided
  output — like score_severity(), they never change which states get
  flagged, and check_deadlocks() itself remains untouched throughout
  this round of changes.
- analyze_and_report() and print_report() both gained new *optional*
  keyword-only-in-practice parameters (module_name, confidence for
  print_report; module_name, parser_warnings for analyze_and_report)
  specifically so fix_and_verify's existing internal call —
  analyze_and_report(output_filepath, states, reset_state, graph),
  unchanged — keeps working exactly as-is without fix_and_verify's
  body being touched at all, per the hard constraint not to touch it.
  The tradeoff: fix_and_verify's own [VERIFY] re-report doesn't show a
  Module:/confidence line, since it doesn't pass those new args.
  main() does pass them, so `python fsm_analyzer.py axi_master.v`
  shows the full report with confidence/bug-pattern.
- compute_confidence's signature deviates slightly from the one given
  in the spec (`compute_confidence(module_name, states, graph,
  parser_warnings)`): it adds `reset_state=None` as a trailing keyword
  argument, since one of the spec's own stated deductions ("if reset
  state could not be determined: -20 points") is impossible to compute
  without knowing the reset state, and there's no way to infer that
  from states/graph/parser_warnings alone. Kept optional and at the
  end so any caller using the exact spec'd positional signature still
  works; module_name itself isn't used in the scoring (no rule
  references it), kept only for signature fidelity.

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
All four endpoints are multi-FSM aware (see MULTI-FSM SUPPORT below) —
every response covers every FSM module found in the uploaded file, not
just axi_master.
- POST /analyze — returns `{filename, modules_found, results: [...],
  total_warnings}`. Each entry in `results` is one module:
  `{module_name, states_found, reset_state, reachability, deadlocks
  (with severity + scenario text per deadlock), summary.total_warnings}`.
  `graph` was dropped from /analyze's response in favor of /visualize,
  which is the dedicated endpoint for graph data.
- POST /fix — returns `{filename, modules_found, results: [...],
  fixed_file_content, verification}`. Each `results` entry is
  `{module_name, fixes_applied, verification}` for that module alone.
  Only modules that actually have deadlocks get fixes applied — a
  module with just an unreachable-state warning (axi4_slave, in the
  real file) is reported as-is, since the fix engine's timeout-escape
  strategy doesn't address unreachability. Fixes for multiple modules
  in the same file are layered onto one accumulated output file
  (fix_and_verify chained across modules), since they all live in the
  same .v file; `fixed_file_content` is that combined result, and the
  top-level `verification` sums warnings across all modules.
- POST /download — same multi-module fix run as /fix, returns the
  combined fixed file as a `Response` with
  `Content-Disposition: attachment; filename="<name>_fixed.v"` and
  `Content-Type: text/plain`, not JSON.
- POST /visualize (new) — returns `{modules: [...]}`, one
  `{module_name, nodes, edges, metadata}` per FSM, shaped for a D3
  force-directed graph. `nodes[].type` is "reset" / "deadlock" /
  "unreachable" / "normal" (deadlock nodes also carry `severity`);
  `edges[]` is `{source, target, condition}` for every transition in
  the graph; `metadata` has `reset_state`, `deadlocked_states`,
  `unreachable_states`, `total_states`.
- CORS: `CORSMiddleware(allow_origins=["*"], allow_methods=["*"],
  allow_headers=["*"])`.
- Errors: a shared `FSMError` exception + handler on all four
  endpoints returns `{"error": ..., "detail": ...}` with a 4xx/5xx
  status for empty uploads, files with no FSM at all, non-UTF-8
  uploads, and fix-engine failures, instead of a raw traceback.
- Temp files: every request's upload (and every intermediate
  `..._fixed.v` produced while chaining fixes across modules) lives
  inside one `tempfile.TemporaryDirectory()` per request and nothing
  survives past the request — verified by checking /tmp before/after a
  full round of curl calls to all four endpoints.
- Live-tested: `uvicorn api:app --port 8000`, then curl against all
  four endpoints with the real axi_master.v. Confirmed: /analyze finds
  2 modules with the correct per-module numbers; /fix and /download
  fix axi_master's deadlock (warnings_after: 0 for that module) while
  correctly leaving axi4_slave's unreachable-state warning in place and
  reporting it as such, with the downloaded file diffing from the
  original by only the same 5-line wdata_last insertion; /visualize
  returns 14/17 nodes with the right types including axi4_slave's
  `comp_rd_tx` correctly marked "unreachable".
- Not yet tested: file uploads other than a clean UTF-8 .v (e.g. a
  .txt with a .v extension, extremely large files, concurrent
  requests). The empty-file, non-UTF-8, and no-FSM-found error paths
  were exercised; edge cases beyond that weren't.

## MULTI-FSM SUPPORT (Feature 2)
- find_module_names(text) finds every `module <name>` declaration.
  parse_fsm(filepath) now returns a **list** of dicts, one per module
  that has both localparam states and a case(state) block — this is a
  breaking signature change from the old (states, reset_state, graph)
  3-tuple. get_fsm(fsm_list, module_name) looks one up by name.
- extract_reset_state(module_body, states=None) was generalized to
  handle reset idioms beyond axi_master's own
  `if (!m_axi_aresetn) state <= idle;`: an `if (X == 0) state <= Y;`
  comparison form, and a fallback to the state register's own declared
  initial value (`reg [4:0] state = 0;`, mapped back to a state name
  via the localparam dict when it's a bare number) — axi4_slave uses
  exactly this last pattern (`if (s_axi_aresetn == 0) begin <zeroes
  memory, never touches state> end`, with `state`'s reset value coming
  from its declaration instead). Both the `!X` and `X == 0` searches
  are scoped to the module text *before* case(state) starts, because
  searching the whole module body once caused a real false match: an
  ordinary transition condition deep in axi4_slave's body happened to
  match the `X == 0` pattern and produced a garbage reset state
  ("send_ack") before this fix.
- extract_case_block and locate_state_block_span (the fix engine's
  block locator) both used to find the case(state) block with a
  non-greedy `case\s*\(\s*state\s*\)(.*?)endcase` regex, which stops at
  the *first* endcase — including a nested `case(awburst) ... endcase`
  inside one state's own block. axi4_slave's real body has exactly this
  (a nested case inside `accept_rd`/`rcheck_br_len`'s block), and it
  silently truncated the parsed case(state) block right after that
  nested endcase, leaving every state from that point on with an empty,
  unexplained transition list (no parser warning). Fixed with a real
  balanced case/endcase scanner (find_matching_endcase, token-depth
  counting like the existing find_matching_paren/find_matching_end).
- apply_fix, _template_fix, generate_fix, locate_state_block_span, and
  fix_and_verify all gained a `module_name=TARGET_MODULE` parameter so
  the fix engine can target any module, not just axi_master — this
  matters as soon as a file has more than one FSM with deadlocks, or a
  deadlock in a module other than axi_master; without it, the fix
  engine would always silently edit the first case(state) block in the
  file regardless of which module's state was actually being fixed.
- main() (CLI) prefers TARGET_MODULE ("axi_master") when present, so
  behavior on axi_master.v is byte-for-byte what it always was — this
  is what makes "existing CLI behavior unchanged" true. When
  TARGET_MODULE isn't in the file (e.g. test_cases/unreachable_fsm.v),
  it falls back to the one FSM found, so the CLI works on any Verilog
  file, which is the actual point of this feature.
- **Real numbers vs. the task's illustrative example**: the prompt's
  example JSON showed axi4_slave as "2 states, 0 deadlocks, reset
  s_idle". The real axi4_slave in this file has none of that — 17
  declared states, reset state "idle", 0 deadlocks, and one genuinely
  unreachable state (see below). That example was illustrative, not
  drawn from this file; the numbers below are what the tool actually
  found running against the real source, verified against the source
  lines by hand, not adjusted to match the prompt's example.

## SEVERITY SCORING (Feature 3)
- score_severity(state_name, graph) in fsm_analyzer.py: HIGH if the
  state name matches a critical-path hint ("wdata", "rdata",
  "wr_resp") OR 3+ distinct other states transition into it; MEDIUM
  for 1-2; LOW for none. It's purely a reporting-layer annotation
  computed from check_deadlocks()'s already-decided output — it does
  not change which states get flagged as deadlocks, and check_deadlocks
  itself is untouched.
- Wired into: the CLI report (`[WARN] ... [SEVERITY: HIGH]`), /analyze
  and /fix's JSON (`"severity"` per deadlock), and /visualize's deadlock
  nodes (`"severity"` field).
- wdata_last scores HIGH (via the "wdata" name hint; send_wdata is also
  its only incoming state, which alone would only be MEDIUM — the name
  hint is what pushes it to HIGH, matching the task's stated reasoning).

## UNREACHABLE-STATE TEST FIXTURE (Feature 4)
- test_cases/unreachable_fsm.v: a small synthetic FSM (idle/running/
  complete/orphan) with `orphan` declared but never targeted by any
  transition. `python fsm_analyzer.py test_cases/unreachable_fsm.v`
  reports `[WARN] Unreachable state: orphan` and
  `=== Summary: 1 warning(s) found ===`, exactly as specified.
- Also worth noting: axi4_slave in the REAL axi_master.v independently
  turned out to have its own genuinely unreachable state — `comp_rd_tx`
  is declared in its localparam list (value 16) but is never used as a
  case label anywhere in its case(state) block. This was found by the
  tool, not planted; test_cases/unreachable_fsm.v exists as a clean,
  deliberate, single-purpose example for TEST 14 specifically.

## ADVANCED BACKEND FEATURES (batch, compare, confidence, PDF, bug patterns, webhooks)

### Batch analysis — POST /analyze/batch
- Accepts a .zip (10MB limit, checked before extraction), extracts to
  a tempdir, walks it recursively for *.v/*.sv files, runs parse_fsm()
  on each. A file that can't even be read/parsed is reported as
  `{"filename", "error": "Parse failed", "skipped": true}`; a file
  that parses fine but has no FSM in it just contributes an empty
  `modules: []` (not an error — plenty of real .v files aren't FSMs).
- Errors: no .v/.sv files found, malformed ZIP, and the size limit
  each return a distinct `{"error": ...}` before any extraction/parsing
  is attempted for the size and malformed-ZIP cases.
- Real result analyzing axi_master.v via batch: 1 file, 2 FSMs found
  (axi_master + axi4_slave, same as the non-batch /analyze), 1
  deadlock total — matches TEST 15 exactly.

### Historical comparison — POST /compare
- Takes file_v1 + file_v2, parses both with parse_fsm(), and diffs
  per module (matched by module_name, since a module can exist in one
  version and not the other): deadlocks present in v1 but not v2 are
  "fixed", present in v2 but not v1 are "introduced", present in both
  are "unchanged"; states are diffed the same way into added/removed.
  Matching a deadlock across versions is done by state name, not exact
  condition string, since the condition can legitimately change
  slightly between versions for what's conceptually the same bug.
- Verdict: MIXED if both fixed>0 and introduced>0 (net changes in
  both directions beats a pure count comparison), else IMPROVED if
  only fixed>0, REGRESSED if only introduced>0, else UNCHANGED.
- `_save_upload_to_tempdir` gained an optional `basename` param
  specifically for this endpoint — v1 and v2 share one tempdir, and
  without distinct basenames ("v1"/"v2" instead of the default
  "upload") the second upload would silently overwrite the first.
- Verified with the real axi_master.v (v1) vs axi_master_fixed.v (v2):
  wdata_last shows as fixed in the axi_master module, verdict
  IMPROVED, and axi4_slave (unchanged by the fix, same file content in
  both versions) correctly shows no changes at all — matches TEST 16.

### Confidence scoring — compute_confidence()
- See ARCHITECTURE DECISIONS for the signature deviation
  (reset_state added as a trailing optional kwarg).
- axi_master scores 100/100 (no parser warnings, no zero-exit states,
  reset state determined, 14 states). axi4_slave scores 94/100 — the
  only deduction is 2 zero-exit states, i.e. states with no outgoing
  transitions at all (this includes `comp_rd_tx`, its genuinely unused
  state, and is a real, meaningful signal about that module, not a
  parser gap). Wired into the CLI report ("Parse confidence: NN%"),
  every module result in /analyze, /fix (via the shared _analyze_one
  building block used across endpoints), /analyze/batch, and
  /visualize's metadata.

### PDF export — POST /export/pdf
- Built with reportlab's SimpleDocTemplate, entirely in memory
  (io.BytesIO) — no temp file needed for the PDF itself, only the
  uploaded .v file goes through tempfile as usual. Cover page (title,
  subtitle, filename, timestamp, FSM/deadlock/unreachable counts) +
  one section per module (states, reset state, confidence,
  reachability, one block per deadlock with a color-coded severity
  label — red/orange/yellow-ish for HIGH/MEDIUM/LOW — bug pattern name
  + description, blocking condition and scenario in a monospace style,
  and a suggested fix) + a summary table (total warnings, severity
  breakdown).
- The "suggested fix" is generated by calling fsm._template_fix()
  directly (not the public generate_fix(), which may call a live LLM)
  — the PDF spec explicitly asks for "the same template fix the engine
  generates", and triggering a live API call just to render a report
  preview would be slow, nondeterministic, and could fail the whole
  export over a network issue for something that's meant to be a
  quick illustrative suggestion.
- Verified: response Content-Type is application/pdf, Content-
  Disposition names it fsm_sentinel_report.pdf, and the bytes start
  with the literal `%PDF-` header (checked directly, not just trusted
  from the Content-Type) — matches TEST 18. Real output against
  axi_master.v is 3 pages (cover + axi_master + axi4_slave).

### Bug pattern classification — classify_bug_pattern()
- Five named patterns, checked in a specific priority order (first
  match wins) chosen to make the patterns mutually exclusive rather
  than overlapping in the obvious-but-wrong way a literal reading of
  each rule in isolation would produce. Concretely:
  - counter_overflow_deadlock additionally requires the condition
    NOT also reference an external handshake signal — otherwise
    wdata_last's condition ("m_axi_wready && burst_count == 0", which
    does contain a "== 0" counter comparison) would match this pattern
    before ever reaching protocol_violation_deadlock, which is not
    what the task's own worked example wants.
  - missing_default_deadlock additionally requires 2+ distinct
    branches (an if/else-if chain) with none unconditional — every
    single-branch deadlock (i.e. most of them, definitionally, since
    check_deadlocks only flags states with no unconditional/timeout
    exit at all) would otherwise trivially match this pattern first
    and the other four named patterns would be unreachable in
    practice.
  These two refinements were necessary to make wdata_last actually
  land on protocol_violation_deadlock or handshake_deadlock as the
  task's own example says it should — implemented literally without
  them, the given rule text does not produce that result for the real
  wdata_last condition.
- wdata_last classifies as protocol_violation_deadlock (its condition
  contains "axi", as a substring of "m_axi_") — matches TEST 19, which
  accepts either of the two patterns the task names as correct.
- Wired into: the CLI report ("Bug pattern: ..." / "Pattern
  description: ..."), every deadlock in /analyze, /fix, and
  /analyze/batch's JSON (`bug_pattern` + `pattern_description`),
  /visualize's deadlock nodes (`bug_pattern` only, per the spec'd node
  shape), and /export/pdf's per-deadlock sections.

### Webhook support — webhook_url on /analyze and /analyze/batch
- Optional `webhook_url` query param + FastAPI `BackgroundTasks`: the
  main response is returned to the caller first, then (if a URL was
  given) the same JSON is POSTed to it in the background via httpx,
  with header `X-FSM-Sentinel: true`. A failed webhook POST is logged
  (`[WEBHOOK] Failed to POST to {url}: {exc}`) and never affects the
  caller's response, which has already been sent by the time the
  background task even runs.
- Verified with a real HTTP round-trip: TEST 21 spins up a throwaway
  `http.server.HTTPServer` in a background thread as the mock webhook
  receiver, calls /analyze with webhook_url pointing at it, and checks
  the receiver actually got a POST carrying the X-FSM-Sentinel header
  and a payload matching the main response — not a mocked/stubbed
  check, an actual second HTTP request observed landing.

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
[x] Build POST /visualize (D3-ready nodes/edges/metadata) — Claude Code
[x] Refactor parse_fsm() to multi-FSM (list of modules) — Claude Code
[x] Fix reset-state extraction, case/endcase balancing, and comment-
    masking bugs found while validating multi-FSM support against the
    real axi4_slave module — Claude Code
[x] Make /analyze, /fix, /download multi-FSM aware — Claude Code
[x] Add score_severity() + wire severity into CLI/API/visualize — Claude Code
[x] Build test_cases/unreachable_fsm.v + TEST 14 — Claude Code
[x] Add TEST 11/12/13/14, all 14 tests passing — Claude Code
[x] Build POST /analyze/batch (ZIP upload, multi-file consolidated report) — Claude Code
[x] Build POST /compare (v1 vs v2 diff: fixed/introduced/unchanged bugs, added/removed states, verdict) — Claude Code
[x] Add compute_confidence() + wire parse_confidence into CLI/API/visualize — Claude Code
[x] Build POST /export/pdf (reportlab, cover + per-module sections + severity table) — Claude Code
[x] Add classify_bug_pattern() + wire bug_pattern into CLI/API/visualize/PDF — Claude Code
[x] Add webhook_url support (BackgroundTasks + httpx) to /analyze and /analyze/batch — Claude Code
[x] Add TEST 15-21, all 21 tests passing — Claude Code
[x] Re-verify CLI commands (axi_master.v, axi_master.v --fix, unreachable_fsm.v) unchanged in substance after all of the above — Claude Code
[x] Build web UI — Devin
[x] Build state diagram visualizer — consume POST /visualize's
    `modules[].{nodes,edges,metadata}` (D3, layered left-to-right layout, not force-directed; node
    `type` drives color, deadlock nodes carry `severity`) — Devin
[x] Diff viewer (UI) — consume /fix's `results[].fixes_applied[].diff` — Devin
[x] Download button — wire to POST /download — Devin
[x] Polish output formatting — Devin
[ ] Wire a real GROQ_API_KEY or ANTHROPIC_API_KEY in the deployed
    environment and confirm the live-LLM fix path (not just the
    template fallback) — whoever owns the demo environment, since
    neither key has been validated from this sandbox
[ ] Decide whether axi4_slave's unreachable comp_rd_tx state is worth
    fixing by hand in the source (the fix engine intentionally does not
    auto-fix unreachability, only deadlocks) — whoever owns the demo
    script, since it'll show up as a real, correctly-reported warning
    if axi4_slave is included in the demo
[x] Batch upload UI — wire to POST /analyze/batch (multi-file drag/drop,
    consolidated results table) — Devin
[x] Version-diff UI — wire to POST /compare (two-file upload, verdict
    badge, fixed/introduced/unchanged lists, added/removed states) — Devin
[x] "Download PDF report" button — wire to POST /export/pdf — Devin
[x] Show parse_confidence % somewhere in the module header/card in the UI — Devin
[x] Show bug_pattern + pattern_description on deadlock cards/nodes — Devin
[ ] CI/CD integration docs — document webhook_url usage for pipeline
    integration (POST /analyze?webhook_url=... or /analyze/batch) —
    whoever writes user-facing docs

## DO NOT TOUCH
fsm_analyzer.py core parser logic — owned by Claude Code, validated against axi_master.v
Any function that builds the graph dict
check_deadlocks(), is_safe_exit(), is_handshake_condition() — the core
  deadlock detection logic. score_severity(), classify_bug_pattern(),
  and compute_confidence() are all separate, additive reporting-layer
  functions computed from check_deadlocks()'s already-decided output;
  none of them touch it or change which states get flagged.
apply_fix() and fix_and_verify() — owned by Claude Code, untouched
  through this round of changes (analyze_and_report/print_report grew
  new *optional* trailing params instead, specifically so
  fix_and_verify's existing internal call didn't need to change)
The fix engine's surgical-replacement and re-verification logic (locate_state_block_span, apply_fix's state-set safety check, fix_and_verify's re-parse) — owned by Claude Code
api.py's error handling (FSMError + the try/except in each endpoint) and its temp-file handling (tempfile.TemporaryDirectory per request) — owned by Claude Code
score_severity(), classify_bug_pattern(), compute_confidence() — validated reporting-layer functions, owned by Claude Code
_mask_comments() and everywhere it's wired in (extract_case_block,
  locate_state_block_span, split_state_blocks, parse_transitions_for_state,
  _template_fix) — owned by Claude Code; this closes a real class of bugs
  (a comment containing "case"/"if"/"begin" corrupting structural parsing)
  and removing it will silently reopen that class on any file with
  English comments in its FSM body
