#!/usr/bin/env python3
"""FastAPI wrapper around fsm_analyzer.py.

Exposes /analyze, /fix, and /download for the web UI. All FSM parsing,
deadlock detection, and fix logic lives in fsm_analyzer.py — this file
only adapts its functions to HTTP: saving uploads to a temp file,
shaping the JSON responses, and handling errors gracefully. No parser
or fix-engine logic is duplicated here.
"""

import tempfile
from pathlib import Path

import fsm_analyzer as fsm
from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

app = FastAPI(title="FSM Sentinel API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class FSMError(Exception):
    """Raised for any expected failure (bad upload, no FSM found, fix
    engine failure) so a single handler can turn it into a clean JSON
    error response."""

    def __init__(self, error, detail):
        super().__init__(error)
        self.error = error
        self.detail = detail


def _error_response(error, detail, status_code=400):
    return JSONResponse(status_code=status_code, content={"error": error, "detail": detail})


async def _save_upload_to_tempdir(file: UploadFile, tmpdir: str) -> str:
    """Save an uploaded file into a temp directory that the caller owns
    (and will clean up via `with tempfile.TemporaryDirectory()`), and
    return its path. Never writes outside that temp directory, so
    nothing from an upload persists on disk after the request."""
    contents = await file.read()
    if not contents:
        raise FSMError("Empty file uploaded", "The uploaded file contained no data.")

    suffix = Path(file.filename or "upload.v").suffix or ".v"
    tmp_path = str(Path(tmpdir) / f"upload{suffix}")
    with open(tmp_path, "wb") as f:
        f.write(contents)
    return tmp_path


def _parse_or_raise(filepath):
    """Call fsm_analyzer.parse_fsm and turn its failure modes into a
    clean FSMError instead of a raw exception or a silent None."""
    try:
        states, reset_state, graph = fsm.parse_fsm(filepath)
    except UnicodeDecodeError:
        raise FSMError(
            "Invalid file",
            "Uploaded file is not valid UTF-8 text — expected a Verilog (.v) source file",
        )

    if states is None:
        raise FSMError(
            "No FSM found in uploaded file",
            f"Could not locate module '{fsm.TARGET_MODULE}' in the uploaded file",
        )
    if not states:
        raise FSMError(
            "No FSM found in uploaded file",
            "Could not locate localparam state definitions or case(state) block",
        )
    return states, reset_state, graph


def _deadlocks_to_json(deadlocks):
    return [
        {
            "state": state_name,
            "condition": condition,
            "scenario": (
                f"If {condition} never asserts while the FSM is in "
                f"'{state_name}', there is no timeout or unconditional "
                f"exit. The FSM stalls indefinitely."
            ),
        }
        for state_name, condition in deadlocks
    ]


@app.post("/analyze")
async def analyze(file: UploadFile = File(...)):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = await _save_upload_to_tempdir(file, tmpdir)
            states, reset_state, graph = _parse_or_raise(tmp_path)

            unreachable = fsm.check_reachability(graph, reset_state, list(states.keys()))
            deadlocks = fsm.check_deadlocks(graph, reset_state)

            return {
                "filename": file.filename,
                "states_found": len(states),
                "reset_state": reset_state,
                "reachability": {
                    "status": "pass" if not unreachable else "fail",
                    "unreachable": unreachable,
                },
                "deadlocks": _deadlocks_to_json(deadlocks),
                "graph": graph,
                "summary": {"total_warnings": len(unreachable) + len(deadlocks)},
            }
    except FSMError as exc:
        return _error_response(exc.error, exc.detail)
    except Exception as exc:
        return _error_response("Internal error while analyzing file", str(exc), status_code=500)


def _run_fix_engine(tmp_path, file_label):
    """Shared by /fix and /download: parse, detect deadlocks, and (if
    any) run fix_and_verify. Returns
    (fixed_filepath, warnings_before, warnings_after, fixes_applied).
    fixed_filepath is tmp_path unchanged when there were no deadlocks
    to fix.
    """
    states, reset_state, graph = _parse_or_raise(tmp_path)

    unreachable = fsm.check_reachability(graph, reset_state, list(states.keys()))
    deadlocks = fsm.check_deadlocks(graph, reset_state)
    warnings_before = len(unreachable) + len(deadlocks)

    if not deadlocks:
        return tmp_path, warnings_before, warnings_before, []

    result = fsm.fix_and_verify(tmp_path, deadlocks, graph, states, reset_state)
    if result is None:
        raise FSMError(
            "Fix engine failed",
            f"Could not generate or apply a fix for {file_label}",
        )

    fixed_filepath, fixed_deadlocks, fixes_applied = result

    fixed_states, fixed_reset, fixed_graph = fsm.parse_fsm(fixed_filepath)
    if fixed_states is None:
        raise FSMError(
            "Fix engine failed",
            "Fixed file could not be re-parsed for verification",
        )
    fixed_unreachable = fsm.check_reachability(
        fixed_graph, fixed_reset, list(fixed_states.keys())
    )
    warnings_after = len(fixed_unreachable) + len(fixed_deadlocks)

    return fixed_filepath, warnings_before, warnings_after, fixes_applied


@app.post("/fix")
async def fix(file: UploadFile = File(...)):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = await _save_upload_to_tempdir(file, tmpdir)
            fixed_filepath, warnings_before, warnings_after, fixes_applied = _run_fix_engine(
                tmp_path, file.filename or "uploaded file"
            )

            with open(fixed_filepath, "r") as f:
                fixed_content = f.read()

            return {
                "filename": file.filename,
                "fixes_applied": fixes_applied,
                "fixed_file_content": fixed_content,
                "verification": {
                    "status": "pass" if warnings_after == 0 else "fail",
                    "warnings_before": warnings_before,
                    "warnings_after": warnings_after,
                },
            }
    except FSMError as exc:
        return _error_response(exc.error, exc.detail)
    except Exception as exc:
        return _error_response("Internal error while generating fix", str(exc), status_code=500)


@app.post("/download")
async def download(file: UploadFile = File(...)):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = await _save_upload_to_tempdir(file, tmpdir)
            fixed_filepath, _warnings_before, _warnings_after, _fixes_applied = _run_fix_engine(
                tmp_path, file.filename or "uploaded file"
            )

            with open(fixed_filepath, "r") as f:
                content = f.read()

            original_stem = Path(file.filename or "axi_master.v").stem
            download_name = f"{original_stem}_fixed.v"

            return Response(
                content=content,
                media_type="text/plain",
                headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
            )
    except FSMError as exc:
        return _error_response(exc.error, exc.detail)
    except Exception as exc:
        return _error_response(
            "Internal error while generating download", str(exc), status_code=500
        )
