# FSM Sentinel — Frontend

Single-file UI (`index.html`: HTML + CSS + JS). No npm, no build step.

## Run

1. Start the backend from the repo root:

   ```bash
   pip install -r requirements.txt
   uvicorn api:app --port 8000
   ```

2. Open `frontend/index.html` directly in a browser (double-click, or `file:///.../frontend/index.html`).

   Needs internet access for JetBrains Mono (Google Fonts) and D3 v7.8.5 (cdnjs).

   Backend defaults to `http://localhost:8000`; override with `index.html?api=http://host:port`.

## Flow

1. **Upload** — drop/browse a `.v`, `.sv` (single analysis) or `.zip` (batch). `[ Compare Versions ]` switches to a two-file v1/v2 upload.
2. **Analysis** — left: left-to-right state diagram from `POST /visualize` (red pulsing = deadlock, blue = reset, orange = unreachable, grey = normal; hover edges for conditions, click a node to focus it). Right: report from `POST /analyze`. Files with multiple FSMs get a module selector. `[ Export PDF ]` calls `POST /export/pdf`.
3. **Fix** — `[ Fix This ]` calls `POST /fix` and renders the state-block inline diff.
4. **Verify** — the fixed content is re-sent through `/analyze` + `/visualize`; the fixed node transitions red → grey.
5. **Done** — `[ Download Fixed File ]` calls `POST /download` and shows a before/after summary.

Batch (`.zip`) uses `POST /analyze/batch`; compare uses `POST /compare`.

## Demo check

Upload `axi_master.v` → `axi_master` shows `wdata_last` pulsing red, 1 deadlock, HIGH severity → Fix This → Apply Fix → `[PASS] all checks passed. 0 warnings remaining.`
