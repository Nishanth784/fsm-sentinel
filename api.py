#!/usr/bin/env python3
"""FastAPI wrapper around fsm_analyzer.py.

Exposes /analyze, /fix, /download, /visualize, /analyze/batch,
/compare, and /export/pdf for the web UI. All FSM parsing, deadlock
detection, severity/confidence scoring, and fix logic lives in
fsm_analyzer.py — this file only adapts its functions to HTTP: saving
uploads to a temp file, shaping the JSON responses, and handling
errors gracefully. No parser or fix-engine logic is duplicated here.

A single uploaded .v file can contain more than one FSM (as
axi_master.v itself does: axi_master and axi4_slave). Every endpoint
here reports on ALL FSM modules found in the file, not just one.
"""

import datetime
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Optional

import fsm_analyzer as fsm
import httpx
from fastapi import BackgroundTasks, FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

BATCH_ZIP_MAX_BYTES = 10 * 1024 * 1024  # 10MB

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


async def _save_upload_to_tempdir(file: UploadFile, tmpdir: str, basename: str = "upload") -> str:
    """Save an uploaded file into a temp directory that the caller owns
    (and will clean up via `with tempfile.TemporaryDirectory()`), and
    return its path. Never writes outside that temp directory, so
    nothing from an upload persists on disk after the request.

    `basename` lets a caller save more than one upload into the same
    tempdir without collisions (e.g. /compare's file_v1 and file_v2,
    which would otherwise both land on "upload.v")."""
    contents = await file.read()
    if not contents:
        raise FSMError("Empty file uploaded", "The uploaded file contained no data.")

    suffix = Path(file.filename or "upload.v").suffix or ".v"
    tmp_path = str(Path(tmpdir) / f"{basename}{suffix}")
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
    result = []
    for state_name, condition in deadlocks:
        pattern_info = fsm.classify_bug_pattern(state_name, condition, graph)
        result.append(
            {
                "state": state_name,
                "severity": fsm.score_severity(state_name, graph),
                "bug_pattern": pattern_info["pattern"],
                "pattern_description": pattern_info["description"],
                "condition": condition,
                "scenario": (
                    f"If {condition} never asserts while the FSM is in "
                    f"'{state_name}', there is no timeout or unconditional "
                    f"exit. The FSM stalls indefinitely."
                ),
            }
        )
    return result


def _analyze_one(entry):
    states = entry["states"]
    reset_state = entry["reset_state"]
    graph = entry["graph"]
    parser_warnings = entry.get("parser_warnings", [])

    unreachable = fsm.check_reachability(graph, reset_state, list(states.keys()))
    deadlocks = fsm.check_deadlocks(graph, reset_state)
    confidence = fsm.compute_confidence(
        entry["module_name"], states, graph, parser_warnings, reset_state=reset_state
    )

    return {
        "module_name": entry["module_name"],
        "parse_confidence": confidence,
        "states_found": len(states),
        "reset_state": reset_state,
        "reachability": {
            "status": "pass" if not unreachable else "fail",
            "unreachable": unreachable,
        },
        "deadlocks": _deadlocks_to_json(deadlocks, graph),
        "summary": {"total_warnings": len(unreachable) + len(deadlocks)},
    }


async def post_webhook(url: str, payload: dict):
    """Best-effort POST of an analysis result to a caller-supplied
    webhook URL. Runs as a FastAPI background task, after the main
    response has already been sent — a slow or unreachable webhook
    never delays or affects the response the caller gets back."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                url,
                json=payload,
                headers={"X-FSM-Sentinel": "true"},
                timeout=10.0,
            )
    except Exception as exc:
        print(f"[WEBHOOK] Failed to POST to {url}: {exc}")


@app.post("/analyze")
async def analyze(
    file: UploadFile = File(...),
    webhook_url: Optional[str] = None,
    background_tasks: BackgroundTasks = None,
):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = await _save_upload_to_tempdir(file, tmpdir)
            fsms = _parse_or_raise(tmp_path)

            results = [_analyze_one(entry) for entry in fsms]
            total_warnings = sum(r["summary"]["total_warnings"] for r in results)

            response = {
                "filename": file.filename,
                "modules_found": len(results),
                "results": results,
                "total_warnings": total_warnings,
            }

            if webhook_url and background_tasks is not None:
                background_tasks.add_task(post_webhook, webhook_url, response)

            return response
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
    parser_warnings = entry.get("parser_warnings", [])

    unreachable = set(fsm.check_reachability(graph, reset_state, list(states.keys())))
    deadlocks = fsm.check_deadlocks(graph, reset_state)
    deadlock_conditions = {state_name: condition for state_name, condition in deadlocks}
    confidence = fsm.compute_confidence(
        entry["module_name"], states, graph, parser_warnings, reset_state=reset_state
    )

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
            condition = deadlock_conditions[state_name]
            node["severity"] = fsm.score_severity(state_name, graph)
            node["bug_pattern"] = fsm.classify_bug_pattern(state_name, condition, graph)["pattern"]
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
            "parse_confidence": confidence,
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


# =====================================================================
# Batch analysis (Feature 1)
# =====================================================================


def _analyze_file_for_batch(filepath, display_name):
    """Return (modules, error) for one file extracted from a batch ZIP.
    `error` is only set for a genuine parse failure (a file that
    couldn't even be read as text) — a file that reads fine but simply
    has no FSM in it just yields an empty modules list, not an error."""
    try:
        fsms = fsm.parse_fsm(filepath)
    except Exception:
        return None, {"filename": display_name, "error": "Parse failed", "skipped": True}

    modules = []
    for entry in fsms:
        analyzed = _analyze_one(entry)
        modules.append(
            {
                "module_name": analyzed["module_name"],
                "parse_confidence": analyzed["parse_confidence"],
                "states_found": analyzed["states_found"],
                "reset_state": analyzed["reset_state"],
                "deadlocks": analyzed["deadlocks"],
                "unreachable_states": analyzed["reachability"]["unreachable"],
                "summary": analyzed["summary"],
            }
        )
    return modules, None


@app.post("/analyze/batch")
async def analyze_batch(
    file: UploadFile = File(...),
    webhook_url: Optional[str] = None,
    background_tasks: BackgroundTasks = None,
):
    try:
        contents = await file.read()
        if not contents:
            raise FSMError("Empty file uploaded", "The uploaded ZIP contained no data.")
        if len(contents) > BATCH_ZIP_MAX_BYTES:
            raise FSMError(
                "ZIP file exceeds 10MB limit",
                f"Uploaded file is {len(contents)} bytes; the limit is "
                f"{BATCH_ZIP_MAX_BYTES} bytes.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = str(Path(tmpdir) / "upload.zip")
            with open(zip_path, "wb") as f:
                f.write(contents)

            extract_dir = str(Path(tmpdir) / "extracted")
            os.makedirs(extract_dir, exist_ok=True)
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    zf.extractall(extract_dir)
            except zipfile.BadZipFile:
                raise FSMError("Invalid ZIP file", "The uploaded file is not a valid ZIP archive")

            verilog_files = []
            for root, _dirs, filenames in os.walk(extract_dir):
                for fname in filenames:
                    if fname.lower().endswith((".v", ".sv")):
                        verilog_files.append((fname, os.path.join(root, fname)))
            verilog_files.sort(key=lambda pair: pair[0])

            if not verilog_files:
                raise FSMError(
                    "No Verilog files found in ZIP", "The ZIP contained no .v or .sv files"
                )

            results = []
            files_with_issues = []
            total_fsms_found = 0
            total_deadlocks = 0
            total_unreachable_states = 0

            for display_name, filepath in verilog_files:
                modules, error = _analyze_file_for_batch(filepath, display_name)
                if error is not None:
                    results.append(error)
                    continue

                file_deadlocks = sum(len(m["deadlocks"]) for m in modules)
                file_unreachable = sum(len(m["unreachable_states"]) for m in modules)
                total_fsms_found += len(modules)
                total_deadlocks += file_deadlocks
                total_unreachable_states += file_unreachable
                if file_deadlocks or file_unreachable:
                    files_with_issues.append(display_name)

                results.append({"filename": display_name, "modules": modules})

            response = {
                "batch_summary": {
                    "files_analyzed": len(verilog_files),
                    "total_fsms_found": total_fsms_found,
                    "total_deadlocks": total_deadlocks,
                    "total_unreachable_states": total_unreachable_states,
                    "files_with_issues": files_with_issues,
                },
                "results": results,
            }

            if webhook_url and background_tasks is not None:
                background_tasks.add_task(post_webhook, webhook_url, response)

            return response
    except FSMError as exc:
        return _error_response(exc.error, exc.detail)
    except Exception as exc:
        return _error_response(
            "Internal error while running batch analysis", str(exc), status_code=500
        )


# =====================================================================
# Historical comparison (Feature 2)
# =====================================================================


@app.post("/compare")
async def compare(file_v1: UploadFile = File(...), file_v2: UploadFile = File(...)):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            v1_path = await _save_upload_to_tempdir(file_v1, tmpdir, basename="v1")
            v2_path = await _save_upload_to_tempdir(file_v2, tmpdir, basename="v2")

            v1_fsms = _parse_or_raise(v1_path)
            v2_fsms = _parse_or_raise(v2_path)

            v1_by_name = {e["module_name"]: e for e in v1_fsms}
            v2_by_name = {e["module_name"]: e for e in v2_fsms}
            all_module_names = sorted(set(v1_by_name) | set(v2_by_name))

            modules_out = []
            total_fixed = 0
            total_introduced = 0
            total_unchanged = 0

            for module_name in all_module_names:
                v1_entry = v1_by_name.get(module_name)
                v2_entry = v2_by_name.get(module_name)

                if v1_entry is not None:
                    v1_states = set(v1_entry["states"].keys())
                    v1_deadlocks = dict(
                        fsm.check_deadlocks(v1_entry["graph"], v1_entry["reset_state"])
                    )
                    v1_unreachable = fsm.check_reachability(
                        v1_entry["graph"], v1_entry["reset_state"], list(v1_states)
                    )
                    v1_warnings = len(v1_deadlocks) + len(v1_unreachable)
                else:
                    v1_states, v1_deadlocks, v1_warnings = set(), {}, 0

                if v2_entry is not None:
                    v2_states = set(v2_entry["states"].keys())
                    v2_deadlocks = dict(
                        fsm.check_deadlocks(v2_entry["graph"], v2_entry["reset_state"])
                    )
                    v2_unreachable = fsm.check_reachability(
                        v2_entry["graph"], v2_entry["reset_state"], list(v2_states)
                    )
                    v2_warnings = len(v2_deadlocks) + len(v2_unreachable)
                else:
                    v2_states, v2_deadlocks, v2_warnings = set(), {}, 0

                fixed = [
                    {
                        "state": s,
                        "condition": v1_deadlocks[s],
                        "fix_description": "Deadlock resolved between versions",
                    }
                    for s in v1_deadlocks
                    if s not in v2_deadlocks
                ]
                introduced = [
                    {
                        "state": s,
                        "condition": v2_deadlocks[s],
                        "fix_description": "New deadlock introduced between versions",
                    }
                    for s in v2_deadlocks
                    if s not in v1_deadlocks
                ]
                unchanged = [
                    {"state": s, "condition": v2_deadlocks[s]}
                    for s in v2_deadlocks
                    if s in v1_deadlocks
                ]

                total_fixed += len(fixed)
                total_introduced += len(introduced)
                total_unchanged += len(unchanged)

                modules_out.append(
                    {
                        "module_name": module_name,
                        "v1_warnings": v1_warnings,
                        "v2_warnings": v2_warnings,
                        "changes": {
                            "fixed": fixed,
                            "introduced": introduced,
                            "unchanged": unchanged,
                            "added_states": sorted(v2_states - v1_states),
                            "removed_states": sorted(v1_states - v2_states),
                        },
                    }
                )

            if total_fixed and total_introduced:
                verdict = "MIXED"
            elif total_fixed:
                verdict = "IMPROVED"
            elif total_introduced:
                verdict = "REGRESSED"
            else:
                verdict = "UNCHANGED"

            return {
                "comparison_summary": {
                    "bugs_fixed": total_fixed,
                    "bugs_introduced": total_introduced,
                    "bugs_unchanged": total_unchanged,
                    "verdict": verdict,
                },
                "modules": modules_out,
                "verdict": verdict,
            }
    except FSMError as exc:
        return _error_response(exc.error, exc.detail)
    except Exception as exc:
        return _error_response("Internal error while comparing files", str(exc), status_code=500)


# =====================================================================
# PDF export (Feature 4)
# =====================================================================


def _build_pdf_report(filename, module_results):
    """Build the full analysis report as a PDF and return its raw
    bytes. Built entirely in memory (io.BytesIO) — no temp file needed
    for the PDF itself; the source .v file was already handled via
    tempfile by the caller."""
    import io

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    mono_style = ParagraphStyle("Mono", parent=styles["Normal"], fontName="Courier", fontSize=9)
    severity_colors = {"HIGH": colors.red, "MEDIUM": colors.orange, "LOW": colors.Color(0.6, 0.6, 0)}

    total_fsms = len(module_results)
    total_deadlocks = sum(len(m["deadlocks_raw"]) for m in module_results)
    total_unreachable = sum(len(m["unreachable"]) for m in module_results)

    story = [
        Spacer(1, 1.5 * inch),
        Paragraph("FSM Sentinel Analysis Report", styles["Title"]),
        Paragraph("Hardware State Machine Security Analysis", styles["Heading2"]),
        Spacer(1, 0.4 * inch),
        Paragraph(f"File analyzed: {filename}", styles["Normal"]),
        Paragraph(
            f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", styles["Normal"]
        ),
        Spacer(1, 0.3 * inch),
        Paragraph(
            f"{total_fsms} FSM(s) analyzed, {total_deadlocks} deadlock(s) found, "
            f"{total_unreachable} unreachable state(s) found",
            styles["Normal"],
        ),
        PageBreak(),
    ]

    for m in module_results:
        story.append(Paragraph(f"Module: {m['module_name']}", styles["Heading1"]))
        story.append(Paragraph(f"States found: {m['states_found']}", styles["Normal"]))
        story.append(Paragraph(f"Reset state: {m['reset_state']}", styles["Normal"]))
        story.append(Paragraph(f"Parse confidence: {m['parse_confidence']}%", styles["Normal"]))
        story.append(Spacer(1, 0.2 * inch))

        story.append(Paragraph("Reachability", styles["Heading2"]))
        if not m["unreachable"]:
            story.append(Paragraph("PASS — all states reachable from reset.", styles["Normal"]))
        else:
            for state_name in m["unreachable"]:
                story.append(Paragraph(f"Unreachable: {state_name}", mono_style))
        story.append(Spacer(1, 0.2 * inch))

        story.append(Paragraph("Deadlocks", styles["Heading2"]))
        severity_counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        if not m["deadlocks_raw"]:
            story.append(Paragraph("PASS — no deadlocks detected.", styles["Normal"]))
        else:
            for d in m["deadlocks_raw"]:
                severity_counts[d["severity"]] = severity_counts.get(d["severity"], 0) + 1
                sev_style = ParagraphStyle(
                    f"Sev_{d['state']}",
                    parent=styles["Normal"],
                    textColor=severity_colors.get(d["severity"], colors.black),
                    fontName="Helvetica-Bold",
                )
                story.append(Paragraph(f"State: {d['state']}  [{d['severity']}]", sev_style))
                story.append(Paragraph(f"Bug pattern: {d['bug_pattern']}", styles["Normal"]))
                story.append(Paragraph(d["pattern_description"], styles["Normal"]))
                story.append(Paragraph(f"Blocking condition: {d['condition']}", mono_style))
                story.append(Paragraph(f"Failure scenario: {d['scenario']}", styles["Normal"]))
                if d.get("suggested_fix"):
                    story.append(Paragraph("Suggested fix:", styles["Normal"]))
                    fix_text = d["suggested_fix"].replace("\n", "<br/>").replace(" ", "&nbsp;")
                    story.append(Paragraph(fix_text, mono_style))
                story.append(Spacer(1, 0.15 * inch))

        table_data = [
            ["Total warnings", str(m["summary"]["total_warnings"])],
            ["HIGH severity", str(severity_counts.get("HIGH", 0))],
            ["MEDIUM severity", str(severity_counts.get("MEDIUM", 0))],
            ["LOW severity", str(severity_counts.get("LOW", 0))],
        ]
        table = Table(table_data, colWidths=[2.5 * inch, 1.5 * inch])
        table.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ]
            )
        )
        story.append(table)
        story.append(PageBreak())

    doc.build(story)
    return buffer.getvalue()


@app.post("/export/pdf")
async def export_pdf(file: UploadFile = File(...)):
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = await _save_upload_to_tempdir(file, tmpdir)
            fsms = _parse_or_raise(tmp_path)

            with open(tmp_path, "r") as f:
                fsm_source = f.read()

            module_results = []
            for entry in fsms:
                analyzed = _analyze_one(entry)
                deadlocks_raw = []
                for d in analyzed["deadlocks"]:
                    # The "same template fix the engine generates" per the
                    # PDF spec: the deterministic template specifically
                    # (not generate_fix, which may call a live LLM — not
                    # appropriate to trigger just for a report preview).
                    try:
                        suggested_fix = fsm._template_fix(
                            d["state"], d["condition"], fsm_source, module_name=entry["module_name"]
                        )
                    except Exception:
                        suggested_fix = None
                    deadlocks_raw.append({**d, "suggested_fix": suggested_fix})

                module_results.append(
                    {
                        **analyzed,
                        "unreachable": analyzed["reachability"]["unreachable"],
                        "deadlocks_raw": deadlocks_raw,
                    }
                )

            pdf_bytes = _build_pdf_report(file.filename or "uploaded file", module_results)

            return Response(
                content=pdf_bytes,
                media_type="application/pdf",
                headers={"Content-Disposition": 'attachment; filename="fsm_sentinel_report.pdf"'},
            )
    except FSMError as exc:
        return _error_response(exc.error, exc.detail)
    except Exception as exc:
        return _error_response("Internal error while generating PDF", str(exc), status_code=500)


# =====================================================================
# Chat (ask questions about an /analyze result)
# =====================================================================


class ChatRequest(BaseModel):
    analysis: dict
    message: str


def _build_analysis_context(analysis: dict) -> str:
    """Turn an /analyze response into a concise plain-English summary
    for the chat system prompt — not raw JSON."""
    lines = []
    for result in analysis.get("results", []):
        module_name = result.get("module_name", "unknown module")
        states_found = result.get("states_found", "unknown")
        reset_state = result.get("reset_state", "unknown")
        confidence = result.get("parse_confidence", "unknown")
        unreachable = result.get("reachability", {}).get("unreachable", [])
        deadlocks = result.get("deadlocks", [])

        lines.append(
            f"Module '{module_name}': {states_found} states, reset state "
            f"'{reset_state}', parse confidence {confidence}%."
        )

        if unreachable:
            lines.append(f"  Unreachable states: {', '.join(unreachable)}.")
        else:
            lines.append("  All states reachable from reset.")

        if deadlocks:
            for d in deadlocks:
                lines.append(
                    f"  Deadlock in state '{d.get('state')}' "
                    f"(severity {d.get('severity')}, bug pattern "
                    f"{d.get('bug_pattern')}): blocking condition "
                    f"`{d.get('condition')}`. {d.get('scenario')}"
                )
        else:
            lines.append("  No deadlocks detected.")

    return "\n".join(lines)


def _fallback_deadlock_summary(analysis: dict) -> str:
    """Plain-text deadlock listing read directly from the analysis
    dict, used when the LLM call isn't available."""
    parts = []
    for result in analysis.get("results", []):
        module_name = result.get("module_name", "unknown module")
        for d in result.get("deadlocks", []):
            parts.append(
                f"{module_name}.{d.get('state')} "
                f"({d.get('severity')} severity, {d.get('bug_pattern')})"
            )
    if not parts:
        return "no deadlocks were found in this analysis."
    return "; ".join(parts)


@app.post("/chat")
async def chat(request: ChatRequest):
    context = _build_analysis_context(request.analysis)
    system_prompt = (
        "You are FSM Sentinel, an expert hardware design analysis assistant. "
        "You have just analyzed a Verilog file and found the following: "
        f"{context}. Answer the user's questions about this analysis concisely "
        "and technically accurately. If asked about things outside this "
        "analysis, say you can only speak to the uploaded file."
    )

    try:
        api_key = os.environ.get("GROQ_API_KEY")
        if not api_key:
            raise RuntimeError("GROQ_API_KEY not set")

        from groq import Groq

        client = Groq(api_key=api_key)
        completion = client.chat.completions.create(
            model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request.message},
            ],
            max_tokens=1024,
            temperature=0.3,
        )
        reply = completion.choices[0].message.content
        return {"reply": reply}
    except Exception:
        return {
            "reply": (
                "I can see the analysis results but the AI assistant is "
                "temporarily unavailable. The key finding: "
                f"{_fallback_deadlock_summary(request.analysis)}"
            )
        }
