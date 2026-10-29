"""FastAPI Server for Census Comparison Agent with Interactive UI.

Run from project root:
    uvicorn backend.api:app --host 127.0.0.1 --port 8000 --reload
Or from backend/:
    uvicorn api:app --host 127.0.0.1 --port 8000 --reload
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Response, UploadFile, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# Ensure backend directory is in path
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.append(str(BACKEND_DIR))

import census_config as C
from main import run_pipeline

# Ensure unbuffered standard streams so terminal logs appear instantly
os.environ["PYTHONUNBUFFERED"] = "1"
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass

# Ensure all processing logs stream directly to the terminal stdout at INFO level
_log_format = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
_date_format = "%H:%M:%S"
_console_formatter = logging.Formatter(fmt=_log_format, datefmt=_date_format)

_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setLevel(logging.INFO)
_console_handler.setFormatter(_console_formatter)

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
if not any(isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) == sys.stdout for h in root_logger.handlers):
    root_logger.addHandler(_console_handler)

# Configure census namespace logger directly so all submodules (readers, builder, prospect, router, etc.) stream to terminal
census_logger = logging.getLogger("census")
census_logger.setLevel(logging.INFO)
if not any(isinstance(h, logging.StreamHandler) for h in census_logger.handlers):
    census_logger.addHandler(_console_handler)
census_logger.propagate = False  # Avoid double logging since handler is explicitly attached

logging.getLogger("uvicorn").setLevel(logging.INFO)

log = logging.getLogger("census.api")

# OpenAPI Tags Metadata for Swagger UI
tags_metadata = [
    {
        "name": "Pipeline Processing",
        "description": "Endpoints to upload documents and run automated census consolidation, invoice reconciliation, and deliverable generation.",
    },
    {
        "name": "Testing & Samples",
        "description": "Endpoints to test the pipeline on server disk folders or default sample data.",
    },
    {
        "name": "Deliverables & Downloads",
        "description": "Download generated Excel workbooks, exception reports, and email deliverables.",
    },
    {
        "name": "Extracted Text & OCR",
        "description": "Inspect and download raw structured text extracted from PDFs, Excel, CSV, and DOCX files.",
    },
    {
        "name": "System Health",
        "description": "Health check and system dependency status (Anthropic Claude API, Poppler OCR).",
    },
]

app = FastAPI(
    title="Census Comparison & Invoice Reconciliation Agent API",
    description="""
### Automated Census Creation and Multi-Document Reconciliation Engine

This API automates health insurance underwriting workflows:
- **Intake & OCR**: Ingests PDFs (scanned & digital via PaddleOCR), Excel, CSV, and Word documents.
- **Classification**: Distinguishes Census, Invoices, Renewals, GHQs, and RFPs.
- **Carrier Routing**: Assigns carrier (Florida Blue vs Cigna) and master template (RAPT, Marsh, SBU).
- **Prospect Extraction**: Employs Claude (`claude-opus-5-5`) for precise prospect metadata extraction.
- **Reconciliation**: Validates tiers, enrollment status (Active, Waived, Cobra), flags discrepancies, and aligns renewal rates.
- **Deliverables**: Outputs standardized protected Excel workbooks, audit exception summaries, and ready-to-send intake emails.

#### Interactive Swagger UI
- Explore and test endpoints directly below using the **Try it out** button on any endpoint.
- OpenAPI Schema: [/openapi.json](/openapi.json)
- ReDoc alternative: [/redoc](/redoc)
- Direct Swagger route: [/swagger](/swagger) or [/docs](/docs)
""",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    openapi_tags=tags_metadata,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = BACKEND_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Pydantic Request / Response Models for Swagger Schema Documentation
class FolderTestRequest(BaseModel):
    folder_path: str = Field(..., description="Absolute or relative path to folder containing input documents", example="input")


class HealthResponse(BaseModel):
    status: str = Field("online", description="Service status")
    provider: str = Field("anthropic", description="LLM provider")
    model: str = Field(..., description="Active Anthropic Claude model")
    anthropic_model: str = Field(..., description="Active Anthropic Claude model name")
    effort: str = Field("high", description="Active output_config effort level for claude-opus-5-5")
    temperature: Optional[float] = Field(None, description="Active temperature (omitted for claude-opus-5-5 deterministic effort mode)")
    anthropic_configured: bool = Field(..., description="Whether ANTHROPIC_API_KEY is configured")
    workspace_configured: bool = Field(..., description="Whether ANTHROPIC_WORKSPACE_ID is configured")
    poppler_configured: bool = Field(..., description="Whether Poppler binary path exists for PDF OCR rendering")
    output_dir: str = Field(..., description="Path to server output deliverables directory")
    instructions_doc: Optional[str] = Field(None, description="Active official instruction document")


class PipelineResponse(BaseModel):
    success: bool = Field(True, description="Whether pipeline completed successfully")
    output_filename: Optional[str] = Field(None, description="Primary Excel deliverable filename")
    txt_filename: Optional[str] = Field(None, description="Exceptions audit text report filename")
    download_xlsx_url: Optional[str] = Field(None, description="URL to download primary Excel workbook")
    download_txt_url: Optional[str] = Field(None, description="URL to download exception audit report")
    sbu_filename: Optional[str] = Field(None, description="SBU Census Template deliverable filename (if Cigna)")
    download_sbu_url: Optional[str] = Field(None, description="URL to download SBU Excel workbook")
    email_filename: Optional[str] = Field(None, description="Missing Information Email deliverable filename")
    download_email_url: Optional[str] = Field(None, description="URL to download email text file")
    email_text: str = Field("", description="Pre-composed email text ready to send to broker/client")
    carrier: str = Field("", description="Determined Carrier Routing (Florida Blue or Cigna)")
    state: str = Field("", description="Determined Business State")
    routing: dict = Field(default_factory=dict, description="Routing decisions, primary template, and notes")
    summary: str = Field("", description="Brief Exception Summary")
    audit: dict = Field(default_factory=dict, description="Employee & Dependent count audit metrics")
    counters: dict = Field(default_factory=dict, description="Discrepancy counters")
    notes: list[str] = Field(default_factory=list, description="Audit notes and rules triggered")
    extracted_text_dir: str = Field(..., description="Directory where extracted text files are saved")
    source_folder: Optional[str] = Field(None, description="Source folder path if run from test-folder")
    company: Optional[str] = Field("", description="Company name")
    prospect_name: Optional[str] = Field("", description="Prospect name")
    proposed_effective_date: Optional[str] = Field("", description="Proposed Effective Date from intake/prospect")
    sic_naics_code: Optional[str] = Field("", description="SIC / NAICS Code from intake/prospect")
    company_name: Optional[str] = Field("", description="Company Name from intake/prospect")
    address: Optional[str] = Field("", description="Address from intake/prospect")
    city_state_zip: Optional[str] = Field("", description="City, State, Zip from intake/prospect")
    prospect: Optional[dict] = Field(default_factory=dict, description="Full prospect metadata block")
    instructions_doc: Optional[str] = Field(None, description="Active official instruction document")
    learning_engine: Optional[dict] = Field(default_factory=dict, description="Learning Engine validation report and audit status")


class ExtractedFileItem(BaseModel):
    filename: str = Field(..., description="Extracted text filename")
    size_bytes: int = Field(..., description="File size in bytes")
    download_url: str = Field(..., description="Download URL")


class ExtractedFileListResponse(BaseModel):
    files: list[ExtractedFileItem] = Field(default_factory=list, description="List of extracted text files")
    directory: str = Field(..., description="Directory path on server")


class ExtractedFileContentResponse(BaseModel):
    success: bool = Field(True, description="Extraction status")
    filename: str = Field(..., description="Filename")
    path: str = Field(..., description="Full path on disk")
    content: str = Field(..., description="Full extracted text content")


@app.get("/", include_in_schema=False)
async def index():
    """Serve the interactive web UI."""
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return JSONResponse({"message": "UI not initialized yet."})
    return FileResponse(index_file)


@app.get("/swagger", include_in_schema=False)
@app.get("/docs/swagger", include_in_schema=False)
async def swagger_ui_redirect():
    """Direct alias redirecting /swagger to the interactive Swagger UI at /docs."""
    return RedirectResponse(url="/docs")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Handle browser favicon request cleanly without 404 logs."""
    return Response(status_code=204)


@app.get(
    "/api/health",
    response_model=HealthResponse,
    tags=["System Health"],
    summary="Check API and System Status",
    description="Check the online health of the server, LLM integration, Poppler OCR status, and directories.",
)
@app.get("/health", response_model=HealthResponse, include_in_schema=False)
async def health():
    """Health check endpoint."""
    from llm_client import get_instruction_doc_name
    from device_manager import get_device_manager
    dm_status = get_device_manager().get_status()
    return {
        "status": "online",
        "provider": "anthropic",
        "model": C.ANTHROPIC_MODEL,
        "anthropic_model": C.ANTHROPIC_MODEL,
        "effort": getattr(C, "ANTHROPIC_EFFORT", "high"),
        "temperature": C.ANTHROPIC_TEMPERATURE,
        "anthropic_configured": bool(C.ANTHROPIC_API_KEY),
        "workspace_configured": bool(C.ANTHROPIC_WORKSPACE_ID),
        "poppler_configured": bool(C.POPPLER_PATH and Path(C.POPPLER_PATH).exists()),
        "output_dir": str(C.OUTPUT_DIR),
        "instructions_doc": get_instruction_doc_name(),
        "device": {
            "mode": dm_status["active_mode"],
            "engine": dm_status["active_engine"],
            "name": dm_status["gpu_name"],
            "fallback": dm_status["fallback_occurred"],
        },
    }


@app.get(
    "/api/device-info",
    tags=["System Health"],
    summary="Compute Device & Hardware Acceleration Status",
    description="Query active compute device (GPU vs CPU fallback), CUDA driver versions, and VRAM telemetry.",
)
async def device_info():
    """Retrieve detailed hardware compute device status."""
    try:
        from device_manager import get_device_manager
        return get_device_manager().get_status()
    except Exception as exc:
        return {"status": "error", "message": str(exc), "active_mode": "CPU"}


@app.get(
    "/api/cache-stats",
    tags=["System Health"],
    summary="Prompt Caching Statistics and Telemetry",
    description="Check local prompt cache item count, hit rates, reuse counts, and token savings.",
)
async def cache_stats():
    """Retrieve prompt caching statistics and reuse metrics."""
    try:
        from caching import get_cache_metrics, get_local_cache
        local_stats = get_local_cache().get_stats()
        telemetry = get_cache_metrics()
        return {
            "status": "active",
            "local_cache": local_stats,
            "telemetry": telemetry,
        }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


@app.post(
    "/api/clear-cache",
    tags=["System Health"],
    summary="Clear Local Prompt Cache",
    description="Clear all cached prompt-response pairs from the local SQLite database.",
)
async def clear_cache():
    """Clear all entries in the local prompt cache."""
    try:
        from caching import get_local_cache
        get_local_cache().clear()
        return {"status": "success", "message": "Local prompt cache cleared successfully."}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def _pipeline_response(res: dict) -> dict:
    from llm_client import get_instruction_doc_name
    out_file = Path(res["output_file"]) if res.get("output_file") else None
    txt_file = Path(res["txt_file"]) if res.get("txt_file") else None
    sbu_file = Path(res["sbu_output_file"]) if res.get("sbu_output_file") else None
    email_file = Path(res["email_file"]) if res.get("email_file") else None

    prospect_block = res.get("prospect") or {}
    eff_date = (
        res.get("proposed_effective_date")
        or prospect_block.get("proposed_effective_date")
        or prospect_block.get("effective_date")
        or ""
    )
    sic = (
        res.get("sic_naics_code")
        or prospect_block.get("sic_naics_code")
        or prospect_block.get("sic")
        or ""
    )
    comp = (
        res.get("company_name")
        or res.get("company")
        or res.get("prospect_name")
        or prospect_block.get("company_name")
        or prospect_block.get("company")
        or ""
    )
    addr = (
        res.get("address")
        or prospect_block.get("address")
        or ""
    )
    csz = (
        res.get("city_state_zip")
        or prospect_block.get("city_state_zip")
        or ""
    )

    return {
        "success": True,
        "output_filename": out_file.name if out_file else None,
        "txt_filename": txt_file.name if txt_file else None,
        "download_xlsx_url": f"/api/download/{out_file.name}" if out_file else None,
        "download_txt_url": f"/api/download/{txt_file.name}" if txt_file else None,
        "sbu_filename": sbu_file.name if sbu_file else None,
        "download_sbu_url": f"/api/download/{sbu_file.name}" if sbu_file else None,
        "email_filename": email_file.name if email_file else None,
        "download_email_url": f"/api/download/{email_file.name}" if email_file else None,
        "email_text": res.get("email_text", ""),
        "carrier": res.get("carrier", ""),
        "state": res.get("state", ""),
        "learning_engine": res.get("learning_engine") or {},
        "company": comp,
        "prospect_name": comp,
        "company_name": comp,
        "proposed_effective_date": eff_date,
        "sic_naics_code": sic,
        "address": addr,
        "city_state_zip": csz,
        "prospect": {
            "proposed_effective_date": eff_date,
            "sic_naics_code": sic,
            "company_name": comp,
            "address": addr,
            "city_state_zip": csz,
            "effective_date": eff_date,
            "sic": sic,
            "company": comp,
            "referral": prospect_block.get("referral", ""),
            "sales_person": prospect_block.get("sales_person", ""),
        },
        "routing": res.get("routing", {}),
        "summary": res.get("summary", ""),
        "audit": res.get("audit", {}),
        "counters": getattr(res.get("findings"), "counters", {}),
        "notes": getattr(res.get("findings"), "notes", []),
        "classified_docs": res.get("classified_docs", []),
        "extracted_text_dir": res.get("extracted_text_dir", str(C.EXTRACTED_TEXT_DIR)),
        "instructions_doc": res.get("instructions_doc") or get_instruction_doc_name(),
    }


def _prepare_pipeline_env():
    """Ensure the census agent backend directory is in sys.path without hijacking global config."""
    backend_str = str(BACKEND_DIR)
    if backend_str not in sys.path:
        sys.path.append(backend_str)


@app.post(
    "/api/process",
    response_model=PipelineResponse,
    tags=["Pipeline Processing"],
    summary="Process Uploaded Documents",
    description="Upload multiple documents (PDFs, Excel, CSV, DOCX). Automatically classifies, parses, routes carrier, compares census to invoice, and generates deliverables.",
)
@app.post("/process", response_model=PipelineResponse, include_in_schema=False)
async def process_files(
    files: list[UploadFile] = File(..., description="Multiple document files to process together"),
    user_email: Optional[str] = Query(None, description="User email for audit tracking"),
    request: Request = None,
):
    """Upload files and run the census & invoice comparison pipeline."""
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    _prepare_pipeline_env()
    from main import run_pipeline
    import census_config as C_local

    processed_by = (
        user_email
        or (request.headers.get("x-user-email") if request else None)
        or (request.query_params.get("user_email") if request else None)
        or "SYSTEM"
    )

    log.info("=" * 70)
    log.info(">>> [API] Received upload request from %s: %d file(s) uploaded", processed_by, len(files))

    work = Path(tempfile.mkdtemp(prefix="census_run_"))
    in_dir = work / "in"
    in_dir.mkdir(parents=True, exist_ok=True)
    primary_name = "census_files"

    try:
        saved_paths = []
        for f in files:
            safe_name = Path(f.filename or "file").name
            dest = in_dir / safe_name
            with open(dest, "wb") as out:
                shutil.copyfileobj(f.file, out)
            saved_paths.append(dest)
            log.info("    * Uploaded: %-40s (%d bytes)", safe_name, dest.stat().st_size)

        primary_name = saved_paths[0].name if saved_paths else "census_files"
        try:
            from database.poc_db import log_universal
            log_universal("BENEFITS_UW", "process", primary_name, "STARTED", f"Ingesting {len(saved_paths)} submission file(s)", processed_by)
        except Exception:
            pass

        log.info(">>> [API] Starting Census Comparison Pipeline on %d files...", len(saved_paths))
        res = run_pipeline(saved_paths, C_local.OUTPUT_DIR)
        out_file = Path(res["output_file"])
        txt_file = Path(res["txt_file"])

        log.info(">>> [API] Pipeline completed successfully!")
        log.info("    * Carrier Routing: %s (State: %s)", res.get("carrier"), res.get("state"))
        log.info("    * Primary Workbook: %s", out_file.name)
        if res.get("sbu_output_file"):
            log.info("    * SBU Workbook    : %s", Path(res["sbu_output_file"]).name)
        log.info("    * Exceptions Audit: %s", txt_file.name)
        if res.get("email_file"):
            log.info("    * Missing Info Email: %s", Path(res["email_file"]).name)
        log.info("=" * 70)

        try:
            from database.poc_db import log_universal
            audit_info = res.get("audit", {})
            out_emps = audit_info.get("out_employees", 0)
            out_deps = audit_info.get("out_dependents", 0)
            carrier = res.get("carrier", "Unknown")
            state = res.get("state", "")
            log_universal("BENEFITS_UW", "process", primary_name, "SUCCESS", f"Carrier: {carrier} ({state}) | Generated {out_file.name} ({out_emps} emps / {out_deps} deps)", processed_by)
        except Exception:
            pass

        return _pipeline_response(res)
    except Exception as exc:
        try:
            from database.poc_db import log_universal
            log_universal("BENEFITS_UW", "process", primary_name, "FAILED", str(exc), processed_by)
        except Exception:
            pass
        log.exception("Pipeline execution failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        try:
            from protected_files import clear_transient_passwords
            clear_transient_passwords()
        except Exception:
            pass
        shutil.rmtree(work, ignore_errors=True)


@app.post(
    "/api/test-folder",
    response_model=PipelineResponse,
    tags=["Testing & Samples"],
    summary="Run Pipeline on Server Folder",
    description="Run the complete census pipeline directly against an existing folder on the server filesystem.",
)
@app.post("/test-folder", response_model=PipelineResponse, include_in_schema=False)
async def test_folder(req: FolderTestRequest, request: Request = None):
    """Run pipeline directly on a folder path on disk."""
    _prepare_pipeline_env()
    from main import run_pipeline
    import census_config as C_local

    processed_by = (request.headers.get("x-user-email") if request else None) or "SYSTEM"

    p = Path(req.folder_path)
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=400, detail=f"Folder not found: {req.folder_path}")

    log.info("=" * 70)
    log.info(">>> [API] Running pipeline directly on folder: %s", p)

    try:
        from database.poc_db import log_universal
        log_universal("BENEFITS_UW", "folder_test", p.name, "STARTED", f"Running pipeline on server folder {p}", processed_by)
    except Exception:
        pass

    try:
        res = run_pipeline(p, C_local.OUTPUT_DIR)
        out_file = Path(res["output_file"])
        txt_file = Path(res["txt_file"])

        log.info(">>> [API] Folder pipeline completed successfully!")
        log.info("    * Carrier Routing: %s (State: %s)", res.get("carrier"), res.get("state"))
        log.info("    * Primary Workbook: %s", out_file.name)
        if res.get("sbu_output_file"):
            log.info("    * SBU Workbook    : %s", Path(res["sbu_output_file"]).name)
        log.info("    * Exceptions Audit: %s", txt_file.name)
        log.info("=" * 70)

        try:
            from database.poc_db import log_universal
            carrier = res.get("carrier", "Unknown")
            state = res.get("state", "")
            log_universal("BENEFITS_UW", "folder_test", p.name, "SUCCESS", f"Carrier: {carrier} ({state}) | Generated {out_file.name}", processed_by)
        except Exception:
            pass

        resp = _pipeline_response(res)
        resp["source_folder"] = str(p)
        return resp
    except Exception as exc:
        try:
            from database.poc_db import log_universal
            log_universal("BENEFITS_UW", "folder_test", p.name, "FAILED", str(exc), processed_by)
        except Exception:
            pass
        log.exception("Folder run failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post(
    "/api/test-sample",
    response_model=PipelineResponse,
    tags=["Testing & Samples"],
    summary="One-Click Sample Test",
    description="Quick test running the pipeline on the sample files currently in the input/ folder.",
)
@app.post("/test-sample", response_model=PipelineResponse, include_in_schema=False)
async def test_sample(request: Request = None):
    """Quick one-click test running the default input/ folder."""
    _prepare_pipeline_env()
    from main import run_pipeline
    import census_config as C_local

    processed_by = (request.headers.get("x-user-email") if request else None) or "SYSTEM"

    if not C_local.INPUT_DIR.exists():
        C_local.INPUT_DIR.mkdir(parents=True, exist_ok=True)

    valid_files = [f for f in C_local.INPUT_DIR.iterdir() if f.is_file() and not f.name.startswith(".")]
    if not valid_files:
        sample_script = PROJECT_ROOT / "tests" / "make_sample_data.py"
        if sample_script.exists():
            import subprocess
            subprocess.run([sys.executable, str(sample_script), str(C_local.INPUT_DIR)], check=True)

    try:
        from database.poc_db import log_universal
        log_universal("BENEFITS_UW", "sample_test", "sample_input", "STARTED", "Running one-click sample census test", processed_by)
    except Exception:
        pass

    try:
        res = run_pipeline(C_local.INPUT_DIR, C_local.OUTPUT_DIR)

        try:
            from database.poc_db import log_universal
            out_file = Path(res.get("output_file", "sample_census.xlsx"))
            carrier = res.get("carrier", "Unknown")
            state = res.get("state", "")
            log_universal("BENEFITS_UW", "sample_test", "sample_input", "SUCCESS", f"Carrier: {carrier} ({state}) | Generated {out_file.name}", processed_by)
        except Exception:
            pass

        resp = _pipeline_response(res)
        resp["source_folder"] = str(C.INPUT_DIR)
        return resp
    except Exception as exc:
        try:
            from database.poc_db import log_universal
            log_universal("BENEFITS_UW", "sample_test", "sample_input", "FAILED", str(exc), processed_by)
        except Exception:
            pass
        log.exception("Sample test failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get(
    "/api/download/{filename}",
    tags=["Deliverables & Downloads"],
    summary="Download Generated Deliverable",
    description="Download a finalized Excel census workbook (.xlsx), exceptions report (.txt), or missing information email (.txt).",
)
@app.get("/download/{filename}", include_in_schema=False)
async def download_output(filename: str):
    """Download a generated workbook or exception report from output directory."""
    safe_name = Path(filename).name
    file_path = C.OUTPUT_DIR / safe_name
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File {safe_name} not found.")

    media_type = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if safe_name.endswith(".xlsx")
        else "text/plain; charset=utf-8"
    )
    return FileResponse(file_path, filename=safe_name, media_type=media_type)


@app.get(
    "/api/extracted-text/{filename}",
    response_model=ExtractedFileContentResponse,
    tags=["Extracted Text & OCR"],
    summary="Get Extracted Text Content",
    description="Retrieve the full structured text extracted by OCR / text extractors for a given file.",
)
@app.get("/extracted-text/{filename}", response_model=ExtractedFileContentResponse, include_in_schema=False)
async def get_extracted_text(filename: str):
    """Retrieve full extracted text for a specific document."""
    safe_name = Path(filename).name
    clean_stem = Path(filename).stem.replace(" ", "_")

    candidates = [
        C.EXTRACTED_TEXT_DIR / safe_name,
        C.OUTPUT_DIR / "extracted_text" / safe_name,
        C.EXTRACTED_TEXT_DIR / f"{clean_stem}_extracted.txt",
        C.OUTPUT_DIR / "extracted_text" / f"{clean_stem}_extracted.txt",
    ]

    for p in candidates:
        if p.exists() and p.is_file():
            content = p.read_text(encoding="utf-8", errors="replace")
            return {
                "success": True,
                "filename": p.name,
                "path": str(p),
                "content": content,
            }

    raise HTTPException(status_code=404, detail=f"Extracted text file for '{filename}' not found.")


@app.get(
    "/api/extracted-files",
    response_model=ExtractedFileListResponse,
    tags=["Extracted Text & OCR"],
    summary="List Extracted Text Files",
    description="List all structured text files generated during document OCR and parsing.",
)
@app.get("/extracted-files", response_model=ExtractedFileListResponse, include_in_schema=False)
async def list_extracted_files():
    """List all extracted text files with metadata and download links."""
    files = []
    if C.EXTRACTED_TEXT_DIR.exists():
        for f in sorted(C.EXTRACTED_TEXT_DIR.glob("*_extracted.txt")):
            files.append({
                "filename": f.name,
                "size_bytes": f.stat().st_size,
                "download_url": f"/api/download-extracted/{f.name}",
            })
    return {"files": files, "directory": str(C.EXTRACTED_TEXT_DIR)}


@app.get(
    "/api/download-extracted/{filename}",
    tags=["Extracted Text & OCR"],
    summary="Download Extracted Text File",
    description="Directly download a raw extracted text file (.txt).",
)
@app.get("/download-extracted/{filename}", include_in_schema=False)
async def download_extracted_file(filename: str):
    """Download an extracted text file directly."""
    safe_name = Path(filename).name
    p = C.EXTRACTED_TEXT_DIR / safe_name
    if not p.exists():
        p = C.OUTPUT_DIR / "extracted_text" / safe_name
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail=f"File {safe_name} not found.")
    return FileResponse(p, filename=safe_name, media_type="text/plain; charset=utf-8")


@app.post(
    "/legacy-process",
    tags=["Pipeline Processing"],
    summary="Legacy Direct-Download Processing",
    description="Legacy endpoint that accepts uploaded documents and returns the generated Excel workbook directly as a file download.",
)
async def legacy_process(files: list[UploadFile] = File(...)):
    """Legacy endpoint returning direct file response."""
    work = Path(tempfile.mkdtemp(prefix="census_legacy_"))
    in_dir = work / "in"
    in_dir.mkdir(parents=True, exist_ok=True)
    try:
        for f in files:
            with open(in_dir / Path(f.filename or "file").name, "wb") as out:
                shutil.copyfileobj(f.file, out)
        res = run_pipeline(in_dir, C.OUTPUT_DIR)
        out_file = Path(res["output_file"])
        return FileResponse(
            out_file,
            filename=out_file.name,
            headers={"X-Exception-Summary": res["summary"].replace("\n", " | ")[:1500]},
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)

