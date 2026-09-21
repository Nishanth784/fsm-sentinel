#!/usr/bin/env python3
"""FastAPI wrapper around fsm_analyzer.py.

Exposes /analyze, /fix, /download, and /visualize for the web UI. All
FSM parsing, deadlock detection, severity scoring, and fix logic lives
in fsm_analyzer.py — this file only adapts its functions to HTTP:
saving uploads to a temp file, shaping the JSON responses, and
handling errors gracefully. No parser or fix-engine logic is
duplicated here.

A single uploaded .v file can contain more than one FSM (as
axi_master.v itself does: axi_master and axi4_slave). Every endpoint
here reports on ALL FSM modules found in the file, not just one.
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
    """Call fsm_analyzer.parse_fsm and turn "no FSM at all" into a
    clean FSMError instead of a silent empty response."""
    try:
        fsms = fsm.parse_fsm(filepath)
    except UnicodeDecodeError:
        raise FSMError(
            "Invalid file",
            "Uploaded file is not valid UTF-8 text — expected a Verilog (.v) source file",
        )

    if not fsms:
        raise FSMError(
            "No FSM found in uploaded file",
            "Could not locate any module with both localparam state "
            "definitions and a case(state) block",
        )
    return fsms


def _deadlocks_to_json(deadlocks, graph):
    return [
        {
            "state": state_name,
            "severity": fsm.score_severity(state_name, graph),
            "condition": condition,
            "scenario": (
                f"If {condition} never asserts while the FSM is in "
                f"'{state_name}', there is no timeout or unconditional "
                f"exit. The FSM stalls indefinitely."
            ),
        }
        for state_name, condition in deadlocks
    ]


def _analyze_one(entry):
    states = entry["states"]
    reset_state = entry["reset_state"]
    graph = entry["graph"]

    unreachable = fsm.check_reachability(graph, reset_state, list(states.keys()))
    deadlocks = fsm.check_deadlocks(graph, reset_state)

    return {
        "module_name": entry["module_name"],
        "states_found": len(states),
        "reset_state": reset_state,
        "reachability": {
            "status": "pass" if not unreachable else "fail",
            "unreachable": unreachable,
        },
        "deadlocks": _deadlocks_to_json(deadlocks, graph),
        "summary": {"total_warnings": len(unreachable) + len(deadlocks)},
    }


@app.post("/analyze")
async def analyze(file: UploadFile = File(...)):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = await _save_upload_to_tempdir(file, tmpdir)
            fsms = _parse_or_raise(tmp_path)

            results = [_analyze_one(entry) for entry in fsms]
            total_warnings = sum(r["summary"]["total_warnings"] for r in results)

            return {
                "filename": file.filename,
                "modules_found": len(results),
                "results": results,
                "total_warnings": total_warnings,
            }
    except FSMError as exc:
        return _error_response(exc.error, exc.detail)
    except Exception as exc:
        return _error_response("Internal error while analyzing file", str(exc), status_code=500)


def _run_multi_fix(tmp_path):
    """Run the fix engine across every FSM module found in the file,
    fixing only modules that actually have deadlocks (a module with,
    say, only an unreachable-state warning is reported as-is — the fix
    engine adds timeout escapes for deadlocks, it doesn't restructure a
    graph to make an unreachable state reachable). Fixes for different
    modules in the same file are applied on top of each other into one
    accumulated output file, since they all live in the same .v file.

    Returns (final_filepath, per_module_results, total_before, total_after).
    """
    fsms = _parse_or_raise(tmp_path)

    current_path = tmp_path
    results = []
    total_before = 0
    total_after = 0

    for entry in fsms:
        module_name = entry["module_name"]
        states, reset_state, graph = entry["states"], entry["reset_state"], entry["graph"]

        unreachable = fsm.check_reachability(graph, reset_state, list(states.keys()))
        deadlocks = fsm.check_deadlocks(graph, reset_state)
        warnings_before = len(unreachable) + len(deadlocks)
        total_before += warnings_before

        if not deadlocks:
            results.append(
                {
                    "module_name": module_name,
                    "fixes_applied": [],
                    "verification": {
                        "status": "pass" if warnings_before == 0 else "fail",
                        "warnings_before": warnings_before,
                        "warnings_after": warnings_before,
                    },
                }
            )
            total_after += warnings_before
            continue

        fix_result = fsm.fix_and_verify(
            current_path, deadlocks, graph, states, reset_state, module_name=module_name
        )
        if fix_result is None:
            raise FSMError(
                "Fix engine failed",
                f"Could not generate or apply a fix for module '{module_name}'",
            )
        fixed_path, fixed_deadlocks, fixes_applied = fix_result
        current_path = fixed_path

        post_fsms = fsm.parse_fsm(current_path)
        post_entry = fsm.get_fsm(post_fsms, module_name)
        if post_entry is None:
            raise FSMError(
                "Fix engine failed",
                f"Module '{module_name}' could not be re-parsed for verification",
            )
        post_unreachable = fsm.check_reachability(
            post_entry["graph"], post_entry["reset_state"], list(post_entry["states"].keys())
        )
        warnings_after = len(fixed_deadlocks) + len(post_unreachable)
        total_after += warnings_after

        results.append(
            {
                "module_name": module_name,
                "fixes_applied": fixes_applied,
                "verification": {
                    "status": "pass" if warnings_after == 0 else "fail",
                    "warnings_before": warnings_before,
                    "warnings_after": warnings_after,
                },
            }
        )

    return current_path, results, total_before, total_after


@app.post("/fix")
async def fix(file: UploadFile = File(...)):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = await _save_upload_to_tempdir(file, tmpdir)
            final_path, results, total_before, total_after = _run_multi_fix(tmp_path)

            with open(final_path, "r") as f:
                fixed_content = f.read()

            return {
                "filename": file.filename,
                "modules_found": len(results),
                "results": results,
                "fixed_file_content": fixed_content,
                "verification": {
                    "status": "pass" if total_after == 0 else "fail",
                    "warnings_before": total_before,
                    "warnings_after": total_after,
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
            final_path, _results, _total_before, _total_after = _run_multi_fix(tmp_path)

            with open(final_path, "r") as f:
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


def _visualize_one(entry):
    states = entry["states"]
    reset_state = entry["reset_state"]
    graph = entry["graph"]

    unreachable = set(fsm.check_reachability(graph, reset_state, list(states.keys())))
    deadlocks = fsm.check_deadlocks(graph, reset_state)
    deadlock_conditions = {state_name: condition for state_name, condition in deadlocks}

    nodes = []
    for state_name in states:
        if state_name == reset_state:
            node_type = "reset"
        elif state_name in deadlock_conditions:
            node_type = "deadlock"
        elif state_name in unreachable:
            node_type = "unreachable"
        else:
            node_type = "normal"

        node = {"id": state_name, "label": state_name, "type": node_type}
        if node_type == "deadlock":
            node["severity"] = fsm.score_severity(state_name, graph)
        nodes.append(node)

    edges = [
        {"source": source_state, "target": to_state, "condition": condition}
        for source_state, transitions in graph.items()
        for to_state, condition in transitions
    ]

    return {
        "module_name": entry["module_name"],
        "nodes": nodes,
        "edges": edges,
        "metadata": {
            "reset_state": reset_state,
            "deadlocked_states": list(deadlock_conditions.keys()),
            "unreachable_states": sorted(unreachable),
            "total_states": len(states),
        },
    }


@app.post("/visualize")
async def visualize(file: UploadFile = File(...)):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = await _save_upload_to_tempdir(file, tmpdir)
            fsms = _parse_or_raise(tmp_path)

            return {"modules": [_visualize_one(entry) for entry in fsms]}
    except FSMError as exc:
        return _error_response(exc.error, exc.detail)
    except Exception as exc:
        return _error_response(
            "Internal error while building visualization", str(exc), status_code=500
        )
