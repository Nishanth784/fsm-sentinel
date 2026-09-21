# FSM Sentinel

Hardware FSM bug detector — finds deadlocks and unreachable states in Verilog designs before they reach silicon.

## Quick Start (Windows)

Double-click `start.bat`

OR manually:

```bash
pip install -r requirements.txt
uvicorn api:app --reload --port 8000
```

Backend runs at http://localhost:8000

## Run Tests

```bash
python test_fsm_analyzer.py
```

All 21 tests must pass.

## Demo

Upload `axi_master.v` to confirm tool works before demo.

Expected: 1 deadlock found — `wdata_last` HIGH severity.

## API Endpoints

| Endpoint | Method | Input | Output |
|---|---|---|---|
| /analyze | POST | .v file | JSON report |
| /visualize | POST | .v file | D3 graph JSON |
| /fix | POST | .v file | diff + fixed content |
| /download | POST | .v file | fixed .v file |
| /analyze/batch | POST | .zip file | consolidated report |
| /compare | POST | two .v files | comparison JSON |
| /export/pdf | POST | .v file | PDF report |
