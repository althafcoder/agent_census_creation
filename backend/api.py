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

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Ensure backend directory is in path
BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import config as C
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

app = FastAPI(
    title="Census Comparison Agent",
    description="Automated Census Creation and Invoice Comparison Engine",
    version="2.0.0",
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


class FolderTestRequest(BaseModel):
    folder_path: str


@app.get("/")
async def index():
    """Serve the web UI."""
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return JSONResponse({"message": "UI not initialized yet."})
    return FileResponse(index_file)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Handle browser favicon request cleanly without 404 logs."""
    return Response(status_code=204)


@app.get("/api/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "online",
        "provider": "anthropic",
        "model": C.ANTHROPIC_MODEL,
        "anthropic_model": C.ANTHROPIC_MODEL,
        "anthropic_configured": bool(C.ANTHROPIC_API_KEY),
        "workspace_configured": bool(C.ANTHROPIC_WORKSPACE_ID),
        "poppler_configured": bool(C.POPPLER_PATH and Path(C.POPPLER_PATH).exists()),
        "output_dir": str(C.OUTPUT_DIR),
    }


def _pipeline_response(res: dict) -> dict:
    out_file = Path(res["output_file"])
    txt_file = Path(res["txt_file"])
    sbu_file = Path(res["sbu_output_file"]) if res.get("sbu_output_file") else None
    email_file = Path(res["email_file"]) if res.get("email_file") else None

    return {
        "success": True,
        "output_filename": out_file.name,
        "txt_filename": txt_file.name,
        "download_xlsx_url": f"/api/download/{out_file.name}",
        "download_txt_url": f"/api/download/{txt_file.name}",
        "sbu_filename": sbu_file.name if sbu_file else None,
        "download_sbu_url": f"/api/download/{sbu_file.name}" if sbu_file else None,
        "email_filename": email_file.name if email_file else None,
        "download_email_url": f"/api/download/{email_file.name}" if email_file else None,
        "email_text": res.get("email_text", ""),
        "carrier": res.get("carrier", ""),
        "state": res.get("state", ""),
        "routing": res.get("routing", {}),
        "summary": res.get("summary", ""),
        "audit": res.get("audit", {}),
        "counters": getattr(res.get("findings"), "counters", {}),
        "notes": getattr(res.get("findings"), "notes", []),
        "classified_docs": res.get("classified_docs", []),
        "extracted_text_dir": res.get("extracted_text_dir", str(C.EXTRACTED_TEXT_DIR)),
    }


@app.post("/api/process")
async def process_files(files: list[UploadFile] = File(...)):
    """Upload files and run the census & invoice comparison pipeline."""
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    log.info("=" * 70)
    log.info(">>> [API] Received upload request: %d file(s) uploaded", len(files))

    work = Path(tempfile.mkdtemp(prefix="census_run_"))
    in_dir = work / "in"
    in_dir.mkdir(parents=True, exist_ok=True)

    try:
        saved_paths = []
        for f in files:
            safe_name = Path(f.filename or "file").name
            dest = in_dir / safe_name
            with open(dest, "wb") as out:
                shutil.copyfileobj(f.file, out)
            saved_paths.append(dest)
            log.info("    * Uploaded: %-40s (%d bytes)", safe_name, dest.stat().st_size)

        log.info(">>> [API] Starting Census Comparison Pipeline on %d files...", len(saved_paths))
        res = run_pipeline(saved_paths, C.OUTPUT_DIR)
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

        return _pipeline_response(res)
    except Exception as exc:
        log.exception("Pipeline execution failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.post("/api/test-folder")
async def test_folder(req: FolderTestRequest):
    """Run pipeline directly on a folder path on disk."""
    p = Path(req.folder_path)
    if not p.is_absolute():
        p = (PROJECT_ROOT / p).resolve()
    if not p.exists() or not p.is_dir():
        raise HTTPException(status_code=400, detail=f"Folder not found: {req.folder_path}")

    log.info("=" * 70)
    log.info(">>> [API] Running pipeline directly on folder: %s", p)

    try:
        res = run_pipeline(p, C.OUTPUT_DIR)
        out_file = Path(res["output_file"])
        txt_file = Path(res["txt_file"])

        log.info(">>> [API] Folder pipeline completed successfully!")
        log.info("    * Carrier Routing: %s (State: %s)", res.get("carrier"), res.get("state"))
        log.info("    * Primary Workbook: %s", out_file.name)
        if res.get("sbu_output_file"):
            log.info("    * SBU Workbook    : %s", Path(res["sbu_output_file"]).name)
        log.info("    * Exceptions Audit: %s", txt_file.name)
        log.info("=" * 70)

        resp = _pipeline_response(res)
        resp["source_folder"] = str(p)
        return resp
    except Exception as exc:
        log.exception("Folder run failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/test-sample")
async def test_sample():
    """Quick one-click test running the default input/ folder."""
    if not C.INPUT_DIR.exists():
        C.INPUT_DIR.mkdir(parents=True, exist_ok=True)

    valid_files = [f for f in C.INPUT_DIR.iterdir() if f.is_file() and not f.name.startswith(".")]
    if not valid_files:
        sample_script = PROJECT_ROOT / "tests" / "make_sample_data.py"
        if sample_script.exists():
            import subprocess
            subprocess.run([sys.executable, str(sample_script), str(C.INPUT_DIR)], check=True)

    try:
        res = run_pipeline(C.INPUT_DIR, C.OUTPUT_DIR)
        resp = _pipeline_response(res)
        resp["source_folder"] = str(C.INPUT_DIR)
        return resp
    except Exception as exc:
        log.exception("Sample test failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/download/{filename}")
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


@app.get("/api/extracted-text/{filename}")
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


@app.get("/api/extracted-files")
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


@app.get("/api/download-extracted/{filename}")
async def download_extracted_file(filename: str):
    """Download an extracted text file directly."""
    safe_name = Path(filename).name
    p = C.EXTRACTED_TEXT_DIR / safe_name
    if not p.exists():
        p = C.OUTPUT_DIR / "extracted_text" / safe_name
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail=f"File {safe_name} not found.")
    return FileResponse(p, filename=safe_name, media_type="text/plain; charset=utf-8")


@app.post("/process")
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
