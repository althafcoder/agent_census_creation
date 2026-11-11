"""Tests for ZIP archive processing, security defenses (Zip Slip), junk filtration, and pipeline integration."""
from __future__ import annotations

import io
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

# Ensure backend is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from archive_utils import (
    expand_all_archives,
    is_zip_archive,
    unpack_zip_archive,
)
from main import run_pipeline


@pytest.fixture
def temp_dir():
    d = Path(tempfile.mkdtemp(prefix="test_zip_"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_is_zip_archive(temp_dir):
    zip_path = temp_dir / "test.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("sample.txt", "hello")
    assert is_zip_archive(zip_path) is True

    # .xlsx and .docx files are zip containers under the hood, but must NEVER be treated as zip archives
    xlsx_path = temp_dir / "census.xlsx"
    with zipfile.ZipFile(xlsx_path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<xml></xml>")
    assert is_zip_archive(xlsx_path) is False

    docx_path = temp_dir / "ghq.docx"
    with zipfile.ZipFile(docx_path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<xml></xml>")
    assert is_zip_archive(docx_path) is False

    non_zip = temp_dir / "test.txt"
    non_zip.write_text("not a zip", encoding="utf-8")
    assert is_zip_archive(non_zip) is False


def test_zip_slip_protection(temp_dir):
    """Ensure path traversal attacks in zip archives are detected and blocked."""
    evil_zip = temp_dir / "evil.zip"
    dest_dir = temp_dir / "unpacked"
    dest_dir.mkdir()

    with zipfile.ZipFile(evil_zip, "w") as zf:
        # Create an entry attempting to escape to parent directory
        zf.writestr("../escaped.txt", "malicious payload")

    with pytest.raises(ValueError, match="Zip-Slip path traversal attack detected"):
        unpack_zip_archive(evil_zip, dest_dir)


def test_junk_and_metadata_filtration(temp_dir):
    """Ensure macOS resource forks, OS metadata, and temporary lock files are ignored."""
    zip_path = temp_dir / "with_junk.zip"
    dest_dir = temp_dir / "unpacked"
    dest_dir.mkdir()

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("valid_census.xlsx", b"dummy excel content")
        zf.writestr("__MACOSX/._valid_census.xlsx", b"mac junk")
        zf.writestr(".DS_Store", b"mac junk")
        zf.writestr("subfolder/Thumbs.db", b"windows junk")
        zf.writestr("subfolder/~$lockfile.xlsx", b"office lock junk")

    extracted = unpack_zip_archive(zip_path, dest_dir)
    extracted_names = [p.name for p in extracted]

    assert "valid_census.xlsx" in extracted_names
    assert len(extracted) == 1
    assert not any("__MACOSX" in p.name or "DS_Store" in p.name or "Thumbs.db" in p.name for p in extracted)


def test_nested_folders_and_collision_handling(temp_dir):
    """Ensure files in nested subfolders are extracted and name collisions handled deterministically."""
    zip_path = temp_dir / "nested.zip"
    dest_dir = temp_dir / "unpacked"
    dest_dir.mkdir()

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("invoices/october/billing.pdf", b"billing oct")
        zf.writestr("invoices/september/billing.pdf", b"billing sept")
        zf.writestr("census/roster.csv", b"ee,dob,tier\nJohn,1980-01-01,EE\n")

    extracted = unpack_zip_archive(zip_path, dest_dir)
    extracted_names = [p.name for p in extracted]

    assert len(extracted) == 3
    assert "roster.csv" in extracted_names
    assert "billing.pdf" in extracted_names
    assert "billing_1.pdf" in extracted_names


def test_expand_all_archives(temp_dir):
    """Ensure expand_all_archives replaces zip files with unpacked contents while preserving regular files."""
    regular_file = temp_dir / "standalone.pdf"
    regular_file.write_bytes(b"regular pdf")

    zip_path = temp_dir / "archive.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("unpacked1.xlsx", b"excel 1")
        zf.writestr("unpacked2.xlsx", b"excel 2")

    unpack_dir = temp_dir / "out"
    all_files, extracted = expand_all_archives([regular_file, zip_path], unpack_dir)

    all_names = {p.name for p in all_files}
    assert all_names == {"standalone.pdf", "unpacked1.xlsx", "unpacked2.xlsx"}
    assert zip_path not in all_files
    assert len(extracted) == 2


def test_e2e_pipeline_with_zip_source(temp_dir):
    """Test full end-to-end pipeline run when source is a .zip archive."""
    import census_config as C

    # Check if sample files exist in input/
    sample_files = list(C.INPUT_DIR.glob("*.*"))
    valid_samples = [f for f in sample_files if f.is_file() and not f.name.startswith(".")]

    if not valid_samples:
        pytest.skip("No sample files found in input/ directory for E2E test")

    # Package input sample files into a single zip archive
    submission_zip = temp_dir / "Client_Submission_Package.zip"
    with zipfile.ZipFile(submission_zip, "w") as zf:
        for f in valid_samples:
            zf.write(f, arcname=f"Uploads/{f.name}")

    out_dir = temp_dir / "pipeline_output"
    out_dir.mkdir()

    # Run pipeline directly with the ZIP file as source
    res = run_pipeline(source=submission_zip, output_dir=out_dir)

    assert "output_file" in res and res["output_file"] is not None
    assert Path(res["output_file"]).exists()
    assert "txt_file" in res and res["txt_file"] is not None
    assert Path(res["txt_file"]).exists()
    assert "summary" in res and len(res["summary"]) > 0

    # Verify that api._pipeline_response bundles an All Deliverables ZIP
    from api import _pipeline_response
    api_resp = _pipeline_response(res)
    assert api_resp.get("all_zip_filename") is not None
    assert api_resp["all_zip_filename"].endswith(".zip")
    assert api_resp.get("download_all_zip_url") is not None
    assert api_resp["download_all_zip_url"].startswith("/api/download/")

    # Check zip contents
    zip_path = C.OUTPUT_DIR / api_resp["all_zip_filename"]
    assert zip_path.exists()
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        assert Path(res["output_file"]).name in names
        assert Path(res["txt_file"]).name in names

