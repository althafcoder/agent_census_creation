"""Safe ZIP archive handling and extraction for census & underwriting submissions.

Features:
  1. Zip Slip Protection: Prevents path traversal vulnerabilities (e.g. '../../evil.sh')
     by ensuring every extracted path strictly resolves within the target destination directory.
  2. Junk & System Artifact Filtration: Automatically filters out macOS resource forks
     ('__MACOSX/', '._*'), OS metadata ('.DS_Store', 'Thumbs.db', 'desktop.ini'), and temporary
     Office lock files ('~$*').
  3. Decompression Bomb Protection: Enforces strict upper bounds on both the total uncompressed
     byte size and the maximum extracted file count.
  4. Encrypted ZIP Support: Automatically attempts decompression using candidate passwords
     discovered in submission emails, RFPs, or intake notes.
  5. Deterministic Collision Handling: Flattens nested folder hierarchies inside archives while
     preserving unique, clean filenames.
"""
from __future__ import annotations

import logging
import os
import re
import zipfile
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger("census.archive")

# Upper limits to defend against decompression bombs
DEFAULT_MAX_EXTRACTED_BYTES = 500 * 1024 * 1024  # 500 MB
DEFAULT_MAX_FILES = 500

# Junk / OS metadata filenames and prefixes
_IGNORE_NAMES = {
    ".ds_store",
    "thumbs.db",
    "desktop.ini",
    ".gitkeep",
    ".gitignore",
}

_IGNORE_PREFIXES = ("__macosx", "._", "~$")

# Extensions that are OpenXML or other document formats (which internally use zip compression),
# but must be parsed directly as documents, NEVER treated as archive packages.
NON_ARCHIVE_EXTENSIONS = {
    ".xlsx", ".xlsm", ".xlsb", ".xltx", ".xltm", ".xls",
    ".docx", ".docm", ".dotx", ".dotm", ".doc",
    ".pptx", ".pptm", ".ppt",
    ".pdf", ".csv", ".txt", ".eml", ".msg",
}


def is_zip_archive(path: Path | str) -> bool:
    """Return True if path represents a user-uploaded ZIP submission archive."""
    p = Path(path)
    if not p.is_file():
        return False
    ext = p.suffix.lower()
    if ext in NON_ARCHIVE_EXTENSIONS:
        return False
    if ext in {".zip", ".zipx"}:
        return True
    # If the file has no extension or an unknown extension, check magic bytes
    if not ext:
        try:
            with open(p, "rb") as f:
                header = f.read(4)
                return header == b"PK\x03\x04"
        except Exception:
            return False
    return False


def _is_junk_entry(entry_name: str) -> bool:
    """Check if an archive entry is system metadata or OS junk."""
    normalized = entry_name.replace("\\", "/").strip("/")
    parts = normalized.split("/")

    for part in parts:
        part_low = part.lower().strip()
        if not part_low:
            continue
        if part_low in _IGNORE_NAMES:
            return True
        if any(part_low.startswith(pre) for pre in _IGNORE_PREFIXES):
            return True
        if part_low.endswith(".tmp"):
            return True
    return False


def _safe_dest_filename(dest_dir: Path, filename: str) -> Path:
    """Return a unique destination path inside dest_dir, avoiding overwriting."""
    base_name = Path(filename).name
    target = dest_dir / base_name
    if not target.exists():
        return target

    stem = target.stem
    suffix = target.suffix
    counter = 1
    while target.exists():
        target = dest_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    return target


def unpack_zip_archive(
    archive_path: Path | str,
    dest_dir: Path | str,
    candidate_passwords: Optional[Iterable[str]] = None,
    max_total_bytes: int = DEFAULT_MAX_EXTRACTED_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
) -> list[Path]:
    """Safely extract all non-junk files from a ZIP archive into dest_dir.

    Args:
        archive_path: Path to the .zip archive file.
        dest_dir: Destination directory where files will be unpacked.
        candidate_passwords: Optional iterable of candidate passwords to test if encrypted.
        max_total_bytes: Upper limit on total uncompressed bytes (bomb guard).
        max_files: Upper limit on total extracted files.

    Returns:
        List of Path objects for all successfully extracted files.

    Raises:
        ValueError: If Zip Slip attack is detected or bomb limits are exceeded.
        RuntimeError: If the archive is encrypted and cannot be unlocked.
    """
    archive_path = Path(archive_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_root = dest_dir.resolve()

    if not archive_path.exists():
        raise FileNotFoundError(f"Archive not found: {archive_path}")

    # Gather password candidates
    passwords_to_try: list[bytes] = [b""]  # Always try unencrypted first
    if candidate_passwords:
        for pw in candidate_passwords:
            if pw and str(pw).strip():
                b_pw = str(pw).strip().encode("utf-8")
                if b_pw not in passwords_to_try:
                    passwords_to_try.append(b_pw)

    # Also pull from transient password store if available
    try:
        from protected_files import get_current_store
        store = get_current_store()
        for cand in store.get_candidates(archive_path.name):
            b_pw = cand.strip().encode("utf-8")
            if b_pw not in passwords_to_try:
                passwords_to_try.append(b_pw)
    except Exception:
        pass

    extracted_files: list[Path] = []
    total_uncompressed_bytes = 0

    with zipfile.ZipFile(archive_path, "r") as zf:
        infolist = zf.infolist()

        # 1. Inspect members and enforce safety limits
        valid_members: list[zipfile.ZipInfo] = []
        for info in infolist:
            # Skip directories
            if info.is_dir() or info.filename.endswith(("/", "\\")):
                continue

            # Skip junk
            if _is_junk_entry(info.filename):
                log.debug("   [ARCHIVE] Skipping system junk entry: %s", info.filename)
                continue

            # Zip Slip check: ensure normalized path does not escape dest_root
            dest_target = (dest_dir / info.filename).resolve()
            try:
                common = os.path.commonpath([str(dest_root), str(dest_target)])
                if common != str(dest_root):
                    raise ValueError(f"Zip-Slip path traversal attack detected in archive: '{info.filename}'")
            except Exception as _ce:
                if isinstance(_ce, ValueError):
                    raise
                raise ValueError(f"Invalid path traversal in archive: '{info.filename}'")

            valid_members.append(info)
            total_uncompressed_bytes += info.file_size

            if len(valid_members) > max_files:
                raise ValueError(
                    f"Archive exceeds maximum allowed file count ({max_files}): {archive_path.name}"
                )
            if total_uncompressed_bytes > max_total_bytes:
                raise ValueError(
                    f"Archive uncompressed size ({total_uncompressed_bytes} bytes) exceeds limit "
                    f"({max_total_bytes} bytes): {archive_path.name}"
                )

        log.info(
            "[ARCHIVE] Unpacking %d submission file(s) from '%s' into '%s'...",
            len(valid_members),
            archive_path.name,
            dest_dir.name,
        )

        # 2. Extract each valid member with password trial if necessary
        for info in valid_members:
            dest_file = _safe_dest_filename(dest_dir, info.filename)
            extracted = False
            last_err = None

            for pw in passwords_to_try:
                try:
                    with zf.open(info, "r", pwd=pw if pw else None) as source_stream:
                        with open(dest_file, "wb") as target_stream:
                            import shutil
                            shutil.copyfileobj(source_stream, target_stream)
                    extracted = True
                    break
                except (RuntimeError, zipfile.BadZipFile) as exc:
                    last_err = exc
                    continue

            if not extracted:
                if info.flag_bits & 0x1:
                    log.error("   [ARCHIVE] Protected entry '%s' failed decryption.", info.filename)
                    raise RuntimeError(
                        f"Encrypted ZIP entry '{info.filename}' could not be unlocked with provided passwords."
                    ) from last_err
                else:
                    raise RuntimeError(
                        f"Failed to extract member '{info.filename}' from archive: {last_err}"
                    ) from last_err

            extracted_files.append(dest_file)
            log.info("   -> Extracted: %-40s (%d bytes)", dest_file.name, dest_file.stat().st_size)

    return extracted_files


def expand_all_archives(
    input_paths: list[Path | str],
    unpack_dir: Path,
    candidate_passwords: Optional[Iterable[str]] = None,
) -> tuple[list[Path], list[Path]]:
    """Scan input_paths for ZIP archives and unpack them into unpack_dir.

    Args:
        input_paths: List of file paths to process.
        unpack_dir: Directory where archive contents should be placed.
        candidate_passwords: Optional passwords to try.

    Returns:
        tuple (all_files, extracted_files)
        where all_files includes both regular files from input_paths and the newly extracted files,
        excluding the original .zip files.
    """
    regular_files: list[Path] = []
    extracted_files: list[Path] = []

    for item in input_paths:
        p = Path(item)
        if not p.exists():
            continue
        if is_zip_archive(p):
            unpacked = unpack_zip_archive(
                p,
                unpack_dir,
                candidate_passwords=candidate_passwords,
            )
            extracted_files.extend(unpacked)
        else:
            regular_files.append(p)

    return regular_files + extracted_files, extracted_files

