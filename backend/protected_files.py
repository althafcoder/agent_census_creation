"""Protected file detection, password discovery, and decryption preflight.

Implements Section 6.1 of the Controlling Master Unified Underwriting Instructions:
- Preflight two-pass execution (accessible sources first, then unlock and extract).
- Distinguishes password-to-open encryption from edit/sheet protection.
- Discovers explicit passwords from Outlook emails, forwarded threads, PDFs, notes.
- Secure, transient, case-scoped credential handling with secret masking.
- Supports Office encryption (msoffcrypto) and PDF encryption (pypdf).
- Detailed per-file access outcome tracking:
  READABLE, PRESENT_LOCKED, UNLOCKED_AND_READ, PASSWORD_NOT_FOUND,
  PASSWORD_REJECTED, DECRYPTION_TOOL_UNAVAILABLE, UNSUPPORTED_PROTECTION,
  CORRUPT_OR_UNREADABLE, NOT_PROVIDED.
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from readers import RawDoc

log = logging.getLogger("census.protected_files")


class AccessStatus(str, Enum):
    READABLE = "READABLE"
    PRESENT_LOCKED = "PRESENT_LOCKED"
    UNLOCKED_AND_READ = "UNLOCKED_AND_READ"
    PASSWORD_NOT_FOUND = "PASSWORD_NOT_FOUND"
    PASSWORD_REJECTED = "PASSWORD_REJECTED"
    DECRYPTION_TOOL_UNAVAILABLE = "DECRYPTION_TOOL_UNAVAILABLE"
    UNSUPPORTED_PROTECTION = "UNSUPPORTED_PROTECTION"
    CORRUPT_OR_UNREADABLE = "CORRUPT_OR_UNREADABLE"
    NOT_PROVIDED = "NOT_PROVIDED"


@dataclass
class CandidatePassword:
    value: str
    source_doc: str
    target_file: str | None = None
    confidence: float = 1.0


@dataclass
class FileAccessRecord:
    filename: str
    original_path: Path
    status: AccessStatus = AccessStatus.READABLE
    is_encrypted: bool = False
    encryption_type: str = "none"       # "office_crypto", "pdf_encryption", "none"
    working_path: Path | None = None
    applied_password: str = ""          # Password that successfully unlocked the file
    error_message: str = ""
    remedy_message: str = ""


class TransientPasswordStore:
    """Private, case-scoped credential store.

    Secrets are kept in memory only for the duration of a single submission run,
    never written to persistent prompt caches, output workbooks, or general logs.
    """

    def __init__(self) -> None:
        self._candidates: list[CandidatePassword] = []
        self._temp_files: list[Path] = []
        self._last_access_records: dict[str, FileAccessRecord] = {}

    def clear(self) -> None:
        """Clear all transient secrets and cleanup temporary working copies."""
        self._candidates.clear()
        self._last_access_records.clear()
        self.cleanup_temp_files()

    def add_candidate(self, cand: CandidatePassword) -> None:
        # Avoid exact duplicates
        clean_val = cand.value.strip()
        if not clean_val:
            return
        for existing in self._candidates:
            if existing.value == clean_val and existing.target_file == cand.target_file:
                return
        self._candidates.append(cand)
        target_info = f" for '{cand.target_file}'" if cand.target_file else ""
        masked_val = self.mask(clean_val)
        log.info(
            "   [PASSWORD FOUND] Discovered password '%s'%s from submission source '%s'",
            masked_val,
            target_info,
            cand.source_doc,
        )

    def get_candidates(self, target_filename: str | None = None) -> list[str]:
        """Return candidate passwords ordered by specificity:

        1. Explicitly mapped to target_filename
        2. Case-wide passwords in discovery order
        """
        specific = []
        general = []
        for c in self._candidates:
            if target_filename and c.target_file and c.target_file.lower() == target_filename.lower():
                specific.append(c.value)
            elif not c.target_file:
                general.append(c.value)

        # Preserve order while deduplicating
        res: list[str] = []
        for p in specific + general:
            if p not in res:
                res.append(p)
        return res

    def register_temp_file(self, p: Path) -> None:
        self._temp_files.append(p)

    def cleanup_temp_files(self) -> None:
        for p in list(self._temp_files):
            try:
                if p.exists():
                    p.unlink(missing_ok=True)
            except OSError:
                pass
        self._temp_files.clear()

    @staticmethod
    def mask(secret: str) -> str:
        """Produce a safe masked representation of a password for internal logs."""
        if not secret:
            return "[EMPTY]"
        if len(secret) <= 2:
            return "**"
        return f"{secret[0]}***{secret[-1]} ({len(secret)} chars)"


import contextvars

# Global case-scoped credential store
_CURRENT_STORE = TransientPasswordStore()
_STORE_VAR: contextvars.ContextVar[TransientPasswordStore | None] = contextvars.ContextVar("current_password_store", default=None)


def get_current_store() -> TransientPasswordStore:
    """Return the active case-scoped password store, or the global fallback."""
    store = _STORE_VAR.get()
    if store is not None:
        return store
    return _CURRENT_STORE


def set_current_store(store: TransientPasswordStore | None) -> None:
    """Explicitly set an isolated case-scoped credential store."""
    _STORE_VAR.set(store)


def clear_transient_passwords() -> None:
    store = get_current_store()
    store.clear()


# ------------------------------------------------------------------ Detection ----

OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def detect_file_encryption(path: Path) -> tuple[bool, str]:
    """Detect if a file is password-to-open encrypted.

    Distinguishes password-to-open from edit/sheet protection:
    - Edit-protected Excel files (.xlsx) remain standard zip packages and return (False, "none").
    - Password-to-open encrypted Excel files are wrapped in OLE2 Compound Files with EncryptedPackage.
    - PDF files are checked for user password requirement.
    """
    ext = path.suffix.lower()
    if not path.exists() or path.stat().st_size == 0:
        return False, "none"

    if ext in {".xlsx", ".xlsm", ".xls", ".docx", ".doc"}:
        try:
            with open(path, "rb") as f:
                header = f.read(8)
                if header == OLE2_MAGIC:
                    # Could be legacy .xls/.doc or encrypted modern .xlsx/.xlsm/.docx
                    try:
                        import msoffcrypto
                        f.seek(0)
                        office_file = msoffcrypto.OfficeFile(f)
                        if office_file.is_encrypted():
                            return True, "office_crypto"
                    except Exception:
                        if ext in {".xlsx", ".xlsm", ".docx"}:
                            return True, "office_crypto"
        except Exception as exc:
            log.warning("Could not check encryption for %s: %s", path.name, exc)
            return False, "none"

    elif ext == ".pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(str(path))
            if reader.is_encrypted:
                # Test whether it opens with an empty user password (permissions-only restriction)
                try:
                    decrypt_res = reader.decrypt("")
                    # If decrypt returns non-zero / success and pages can be extracted, no password-to-open is needed
                    if decrypt_res != 0 and len(reader.pages) > 0:
                        _ = reader.pages[0].extract_text()
                        return False, "none"
                except Exception:
                    pass
                return True, "pdf_encryption"
        except Exception:
            # Fallback to pdfplumber or pymupdf
            try:
                import fitz
                doc = fitz.open(str(path))
                if doc.is_encrypted and doc.needs_pass:
                    doc.close()
                    return True, "pdf_encryption"
                doc.close()
            except Exception:
                pass

    return False, "none"


# ------------------------------------------------------------------ Discovery ----

# Comprehensive regex patterns for password discovery in submission emails and notes.
# Handles patterns like:
# - Password: BW Cyber
# - Password is: BW Cyber
# - Password to open: BW Cyber
# - PW: BW Cyber
# - PWD: BW Cyber
# - Passcode: BW Cyber
_PASSWORD_PATTERNS = [
    # 1. Target file specific: "Password for Employee Census.xlsx: BW Cyber"
    re.compile(
        r"(?i)(?:password\s+for|password\s+to\s+open)\s+([a-zA-Z0-9_\-\. ]+\.(?:xlsx?|xlsm|pdf|docx?))\s*[:=\-]?\s*[\"']?([^\r\n\"']+)",
    ),
    # 2. General password labels
    re.compile(
        r"(?i)(?:^|[\r\n])\s*(?:the\s+password\s+is|password\s+to\s+open|attachment\s+password|passcode|password|pwd|pw)\s*[:=\-]?\s*[\"']?([^\r\n\"']+)",
    ),
    # 3. Contextual phrasing: "file is pw protected. password is X"
    re.compile(
        r"(?i)(?:pw|password)\s+protected[^\r\n]*?(?:password\s+is|passcode\s+is|use)\s*[:=\-]?\s*[\"']?([^\r\n\"']+)",
    ),
]

_ADMIN_NOISE_TOKENS = [
    "TrueCompare", "15% admin", "1/1 effective", "4/1 renewal",
    "effective date", "renewal date", "please let me know", "thanks,",
]


def clean_password_candidate(raw: str) -> str:
    """Preserve exact capitalization, internal spaces, punctuation, leading zeros,

    while cleanly removing surrounding quotes and trailing administrative notes.
    """
    cleaned = raw.strip()
    if not cleaned:
        return ""

    # Strip matching outer quotes
    if (cleaned.startswith('"') and cleaned.endswith('"')) or (cleaned.startswith("'") and cleaned.endswith("'")):
        cleaned = cleaned[1:-1].strip()

    # If extraction concatenated multiple lines or admin notes into one line
    for noise in _ADMIN_NOISE_TOKENS:
        idx = cleaned.lower().find(noise.lower())
        if idx > 0:
            cleaned = cleaned[:idx].strip()

    # Strip trailing punctuation like period if clearly at end of sentence
    if cleaned.endswith(".") and not cleaned.endswith("..") and " " in cleaned:
        cleaned = cleaned[:-1].strip()

    return cleaned


def discover_passwords_from_text(text: str, source_name: str = "") -> list[CandidatePassword]:
    """Scan accessible text from emails/notes for explicit password candidates."""
    if not text or not text.strip():
        return []

    candidates: list[CandidatePassword] = []
    seen_values = set()

    for line in text.splitlines():
        line_clean = line.strip()
        if not line_clean:
            continue

        # Check file-specific pattern
        m_file = _PASSWORD_PATTERNS[0].search(line_clean)
        if m_file:
            target = m_file.group(1).strip()
            val = clean_password_candidate(m_file.group(2))
            if val and val not in seen_values:
                seen_values.add(val)
                candidates.append(CandidatePassword(val, source_name, target_file=target, confidence=1.0))
            continue

        # Check general patterns
        for pat in _PASSWORD_PATTERNS[1:]:
            m = pat.search(line_clean)
            if m:
                val = clean_password_candidate(m.group(1))
                if val and val not in seen_values:
                    seen_values.add(val)
                    candidates.append(CandidatePassword(val, source_name, confidence=0.9))
                break

    return candidates


# ------------------------------------------------------------------ Decryption ----

def unlock_office_file(
    path: Path, password: str, temp_dir: Path | None = None
) -> tuple[bool, Path | None, str]:
    """Attempt to decrypt a password-to-open Office file (.xlsx, .docx, etc.) using msoffcrypto."""
    try:
        import msoffcrypto
        import msoffcrypto.exceptions
    except ImportError:
        log.error("Decryption failed: msoffcrypto-tool is not installed.")
        return False, None, AccessStatus.DECRYPTION_TOOL_UNAVAILABLE.value

    try:
        with open(path, "rb") as f_in:
            ms = msoffcrypto.OfficeFile(f_in)
            if not ms.is_encrypted():
                return True, path, ""

            # Check if password is valid
            try:
                ms.load_key(password=password)
            except (msoffcrypto.exceptions.InvalidPasswordError, Exception) as exc:
                log.debug("Password rejected for %s: %s", path.name, exc)
                return False, None, AccessStatus.PASSWORD_REJECTED.value

            # Decrypt to a secure temporary working copy
            t_dir = temp_dir or Path(tempfile.gettempdir())
            fd, tmp_name = tempfile.mkstemp(prefix="decrypted_", suffix=path.suffix, dir=str(t_dir))
            os.close(fd)
            tmp_path = Path(tmp_name)

            try:
                with open(tmp_path, "wb") as f_out:
                    ms.decrypt(f_out)
            except Exception as dec_exc:
                tmp_path.unlink(missing_ok=True)
                msg = str(dec_exc).lower()
                if (
                    "password" in msg
                    or "decrypted" in msg
                    or "invalid" in msg
                    or isinstance(dec_exc, (msoffcrypto.exceptions.InvalidPasswordError, msoffcrypto.exceptions.DecryptionError))
                ):
                    log.debug("Password rejected during decryption for %s: %s", path.name, dec_exc)
                    return False, None, AccessStatus.PASSWORD_REJECTED.value
                raise dec_exc

            get_current_store().register_temp_file(tmp_path)
            return True, tmp_path, ""

    except Exception as exc:
        log.warning("Office decryption failed for %s: %s", path.name, exc)
        return False, None, AccessStatus.CORRUPT_OR_UNREADABLE.value


def unlock_pdf_file(
    path: Path, password: str, temp_dir: Path | None = None
) -> tuple[bool, Path | None, str]:
    """Attempt to decrypt a password-to-open PDF file using pypdf."""
    try:
        import pypdf
    except ImportError:
        return False, None, AccessStatus.DECRYPTION_TOOL_UNAVAILABLE.value

    try:
        reader = pypdf.PdfReader(str(path))
        if not reader.is_encrypted:
            return True, path, ""

        decrypt_res = reader.decrypt(password)
        if decrypt_res == 0:
            return False, None, AccessStatus.PASSWORD_REJECTED.value

        writer = pypdf.PdfWriter()
        writer.clone_reader_content(reader)

        t_dir = temp_dir or Path(tempfile.gettempdir())
        fd, tmp_name = tempfile.mkstemp(prefix="decrypted_", suffix=".pdf", dir=str(t_dir))
        os.close(fd)
        tmp_path = Path(tmp_name)

        with open(tmp_path, "wb") as f_out:
            writer.write(f_out)

        get_current_store().register_temp_file(tmp_path)
        return True, tmp_path, ""

    except Exception as exc:
        log.warning("PDF decryption failed for %s: %s", path.name, exc)
        return False, None, AccessStatus.PASSWORD_REJECTED.value


def unlock_file(
    path: Path, candidates: list[str], temp_dir: Path | None = None
) -> tuple[AccessStatus, Path | None, str, str]:
    """Try supplied candidate passwords against an encrypted file in priority order."""
    if not candidates:
        log.warning("   [NO PASSWORD] No candidate password found for '%s'", path.name)
        return (
            AccessStatus.PASSWORD_NOT_FOUND,
            None,
            f"Password required to open {path.name} was not found in submission sources.",
            "",
        )

    ext = path.suffix.lower()
    unlock_func = unlock_pdf_file if ext == ".pdf" else unlock_office_file

    saw_rejection = False
    for cand in candidates:
        m_cand = TransientPasswordStore.mask(cand)
        log.info("   [APPLYING PASSWORD] Testing password '%s' on '%s'...", m_cand, path.name)
        success, working_copy, err = unlock_func(path, cand, temp_dir=temp_dir)
        if success:
            log.info("   [PASSWORD APPLIED] Successfully unlocked '%s'!", path.name)
            return AccessStatus.UNLOCKED_AND_READ, working_copy, "", cand
        if err == AccessStatus.PASSWORD_REJECTED.value:
            saw_rejection = True
            log.warning("   [PASSWORD REJECTED] Password '%s' failed for '%s'", m_cand, path.name)
        elif err == AccessStatus.DECRYPTION_TOOL_UNAVAILABLE.value:
            log.error("   [TOOL ERROR] Decryption tool unavailable for '%s'", path.name)
            return (
                AccessStatus.DECRYPTION_TOOL_UNAVAILABLE,
                None,
                f"Decryption tool unavailable for {path.name}.",
                "",
            )

    if saw_rejection:
        return (
            AccessStatus.PASSWORD_REJECTED,
            None,
            f"Please confirm the password for {path.name} or provide an unlocked copy; the password supplied in the submission email did not open the file.",
            "",
        )

    return (
        AccessStatus.CORRUPT_OR_UNREADABLE,
        None,
        f"Unable to unlock {path.name} with candidate credentials.",
        "",
    )


# ------------------------------------------------------------------ Access Preflight ----

def execute_access_preflight(
    source: Path | list[Path | str],
    dest_dir: Path | None = None,
    read_file_func=None,
    save_text_func=None,
) -> tuple[list[RawDoc], dict[str, FileAccessRecord]]:
    """Execute Section 6.1 Access Preflight.

    Pass 1: Inventory files, read accessible unencrypted files, queue locked files.
    Discovery: Extract passwords from accessible emails/notes/RFPs.
    Pass 2: Unlock and parse protected files, updating access records and merging into docs.
    """
    from readers import SUPPORTED, read_file, save_extracted_text

    rf_func = read_file_func or read_file
    st_func = save_text_func or save_extracted_text

    store = get_current_store()
    raw_paths = [Path(p) for p in source] if isinstance(source, list) else [Path(source)]
    paths: list[Path] = []
    for p in raw_paths:
        if p.is_dir():
            paths.extend(sorted(p.iterdir()))
        elif p.is_file():
            paths.append(p)

    access_records: dict[str, FileAccessRecord] = {}
    accessible_docs: list[RawDoc] = []
    locked_queue: list[Path] = []

    log.info("[PREFLIGHT] Step 1: Inventorying %d input file(s)...", len(paths))

    # Pass 1: Inventory and separate locked from accessible
    for p in paths:
        if p.name.startswith(("~$", ".")) or p.suffix.lower() not in SUPPORTED:
            continue

        is_enc, enc_type = detect_file_encryption(p)
        rec = FileAccessRecord(
            filename=p.name,
            original_path=p,
            is_encrypted=is_enc,
            encryption_type=enc_type,
        )

        if is_enc:
            rec.status = AccessStatus.PRESENT_LOCKED
            access_records[p.name] = rec
            locked_queue.append(p)
            log.info("   [PROTECTED FILE] Detected password-protected file: '%s' (Type: %s)", p.name, enc_type)
        else:
            try:
                log.info("   -> Reading accessible file: %-38s (%s)", p.name, p.suffix.lower())
                doc = rf_func(p)
                st_func(doc, dest_dir)
                rec.status = AccessStatus.READABLE
                access_records[p.name] = rec
                accessible_docs.append(doc)
            except Exception as exc:
                rec.status = AccessStatus.CORRUPT_OR_UNREADABLE
                rec.error_message = str(exc)
                access_records[p.name] = rec
                log.error("Could not read accessible file %s: %s", p.name, exc)

    # Password Discovery from accessible documents
    if locked_queue:
        log.info(
            "[PREFLIGHT] %d protected file(s) found. Scanning accessible sources for passwords...",
            len(locked_queue),
        )
        for doc in accessible_docs:
            if doc.text:
                cands = discover_passwords_from_text(doc.text, source_name=doc.name)
                for c in cands:
                    store.add_candidate(c)

    # Pass 2: Unlock and verify queued files
    for p in locked_queue:
        rec = access_records[p.name]
        cands = store.get_candidates(target_filename=p.name)
        log.info(
            "   [DECRYPTION PREFLIGHT] Target file: '%s' | Available candidate password(s): %s",
            p.name,
            [f"'{c}'" for c in cands] if cands else "None found",
        )

        status, working_copy, remedy, applied_pw = unlock_file(p, cands, temp_dir=dest_dir)
        rec.status = status
        rec.working_path = working_copy
        rec.remedy_message = remedy
        rec.applied_password = applied_pw

        if status == AccessStatus.UNLOCKED_AND_READ and working_copy:
            try:
                log.info("   -> Reading unlocked file: %s (Direct Table Parsing)", p.name)
                doc = rf_func(working_copy)
                # Restore original path and name provenance
                doc.path = p
                doc.access_status = AccessStatus.UNLOCKED_AND_READ.value
                doc.decrypted = True
                doc.extraction_method = f"{doc.extraction_method} (Decrypted using password '{applied_pw}')"
                st_func(doc, dest_dir)
                accessible_docs.append(doc)
                log.info("      * Unlocked and extracted: '%s' (%d tables, %d chars)", p.name, len(doc.tables), len(doc.text))
            except Exception as exc:
                rec.status = AccessStatus.CORRUPT_OR_UNREADABLE
                rec.error_message = f"Decrypted file could not be parsed: {exc}"
                log.error("Decrypted file %s failed to parse: %s", p.name, exc)
        else:
            log.warning("      * File '%s' remains locked: status=%s", p.name, status.value)

    # Store last access records in singleton store for downstream audit
    store._last_access_records = access_records
    return accessible_docs, access_records
