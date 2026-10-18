"""Original materials: byte-preserving storage plus safe text extraction.

Originals are written once to ``documents/<sha256>.<ext>`` and never modified
in normal use. Extracted text is a separate derived file. Parsing never
executes embedded content: PDF text via pypdf, DOCX via zip + XML, Markdown and
plain text read as-is. Scanned PDFs without a text layer are reported as empty,
never silently accepted.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from jobhunter.config import MAX_DOCUMENT_BYTES, Paths, write_private_file
from jobhunter.db import Database
from jobhunter.util import now_iso, sha256_bytes

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".md", ".markdown", ".txt", ".json"}
KINDS = {"cv", "linkedin", "github", "project", "other"}
MAX_DOCX_UNCOMPRESSED = 50 * 1024 * 1024
MIN_USEFUL_CHARS = 200


class DocumentError(Exception):
    pass


@dataclass
class Extraction:
    status: str  # ok | empty | unsupported | failed
    text: str
    notes: str
    pages: int | None = None


def extract_text(data: bytes, ext: str) -> Extraction:
    ext = ext.lower()
    try:
        if ext == ".pdf":
            return _extract_pdf(data)
        if ext == ".docx":
            return _extract_docx(data)
        if ext in {".md", ".markdown", ".txt", ".json"}:
            text = _decode(data)
            if len(text.strip()) < MIN_USEFUL_CHARS:
                return Extraction("empty", text, "The file has very little text.")
            return Extraction("ok", text, "Read as plain text.")
    except DocumentError as exc:
        return Extraction("failed", "", str(exc))
    except Exception as exc:  # defensive: parsers can raise many things
        return Extraction("failed", "", f"Could not read this file: {type(exc).__name__}")
    return Extraction("unsupported", "", f"Unsupported file type {ext}. Use PDF, DOCX, Markdown, or plain text.")


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _extract_pdf(data: bytes) -> Extraction:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        raise DocumentError("pypdf is not installed") from exc
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise DocumentError("This PDF is password protected. Please provide an unprotected copy.") from exc
    pages = []
    for i, page in enumerate(reader.pages, start=1):
        if i > 60:
            break
        try:
            txt = page.extract_text() or ""
        except Exception:
            txt = ""
        pages.append(f"[[page {i}]]\n{txt.strip()}")
    text = "\n\n".join(pages)
    plain = re.sub(r"\[\[page \d+\]\]", "", text).strip()
    if len(plain) < MIN_USEFUL_CHARS:
        return Extraction(
            "empty",
            text,
            "This PDF has no readable text layer (it may be a scan). Please provide a text-based PDF, a DOCX, or paste the text.",
            pages=len(reader.pages),
        )
    return Extraction("ok", text, f"Read {len(reader.pages)} page(s).", pages=len(reader.pages))


def _extract_docx(data: bytes) -> Extraction:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DocumentError("This is not a valid DOCX file.") from exc
    total = sum(i.file_size for i in zf.infolist())
    if total > MAX_DOCX_UNCOMPRESSED:
        raise DocumentError("This DOCX expands to an unreasonable size and was not read.")
    try:
        xml = zf.read("word/document.xml")
    except KeyError as exc:
        raise DocumentError("This DOCX has no document body.") from exc
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise DocumentError("This DOCX could not be parsed.") from exc
    paras = []
    idx = 0
    for p in root.iter(f"{{{ns['w']}}}p"):
        texts = [t.text or "" for t in p.iter(f"{{{ns['w']}}}t")]
        line = "".join(texts).strip()
        if line:
            idx += 1
            paras.append(line)
    text = "\n".join(paras)
    if len(text.strip()) < MIN_USEFUL_CHARS:
        return Extraction("empty", text, "This DOCX contains almost no text.")
    return Extraction("ok", text, f"Read {len(paras)} paragraph(s).")


def _ext_for(filename: str | None, media_type: str | None) -> str:
    if filename:
        ext = Path(filename).suffix.lower()
        if ext in SUPPORTED_EXTENSIONS:
            return ext
    mapping = {
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "text/markdown": ".md",
        "text/plain": ".txt",
        "application/json": ".json",
    }
    return mapping.get((media_type or "").split(";")[0].strip(), "")


def add_document(
    db: Database,
    paths: Paths,
    kind: str,
    data: bytes,
    original_filename: str | None,
    media_type: str | None = None,
    label: str | None = None,
    source_url: str | None = None,
) -> dict:
    """Store an original byte-for-byte and extract text. Returns the row."""
    if kind not in KINDS:
        raise DocumentError(f"unknown document kind: {kind}")
    if not data:
        raise DocumentError("The file is empty.")
    if len(data) > MAX_DOCUMENT_BYTES:
        raise DocumentError(f"The file is larger than {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB.")
    ext = _ext_for(original_filename, media_type)
    if not ext:
        raise DocumentError("Unsupported file type. Use PDF, DOCX, Markdown, or plain text.")
    digest = sha256_bytes(data)
    stored = paths.documents_dir / f"{digest}{ext}"
    if not stored.exists():
        write_private_file(stored, data)
    else:
        # Immutability check: an existing original must match its hash.
        if sha256_bytes(stored.read_bytes()) != digest:
            raise DocumentError("Stored original does not match its hash. Refusing to continue.")
    extraction = extract_text(data, ext)
    text_path = None
    if extraction.text:
        text_path = paths.derived_dir / f"{digest}.txt"
        write_private_file(text_path, extraction.text.encode("utf-8"))

    prev = db.fetchone(
        "SELECT id, version FROM source_documents WHERE kind = ? AND superseded_by IS NULL ORDER BY version DESC LIMIT 1",
        (kind,),
    )
    version = (prev["version"] + 1) if prev else 1
    with db.tx():
        doc_id = db.insert(
            "source_documents",
            {
                "kind": kind,
                "label": label,
                "original_filename": original_filename,
                "media_type": media_type,
                "byte_size": len(data),
                "sha256": digest,
                "version": version,
                "stored_path": str(stored),
                "text_path": str(text_path) if text_path else None,
                "extraction_status": extraction.status,
                "extraction_notes": extraction.notes,
                "source_url": source_url,
                "created_at": now_iso(),
            },
        )
        if prev and kind in {"cv", "linkedin"}:
            db.update("source_documents", {"superseded_by": doc_id}, "id = ?", (prev["id"],))
            # A replaced document means the approved profile needs a review, never a silent overwrite.
            db.set_setting(
                "profile.needs_review",
                {"reason": f"A new {kind.upper() if kind == 'cv' else 'LinkedIn'} document (version {version}) was added.", "at": now_iso()},
            )
    return get_document(db, doc_id)


def add_text_document(db: Database, paths: Paths, kind: str, text: str, label: str, source_url: str | None = None) -> dict:
    return add_document(db, paths, kind, text.encode("utf-8"), f"{label}.txt", "text/plain", label=label, source_url=source_url)


def get_document(db: Database, doc_id: int) -> dict:
    row = db.fetchone("SELECT * FROM source_documents WHERE id = ?", (doc_id,))
    if not row:
        raise DocumentError(f"document {doc_id} not found")
    return row


def list_documents(db: Database, kind: str | None = None, current_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM source_documents"
    clauses, params = [], []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if current_only:
        clauses.append("superseded_by IS NULL")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY kind, version"
    return db.fetchall(sql, tuple(params))


def current_document(db: Database, kind: str) -> dict | None:
    rows = list_documents(db, kind=kind)
    return rows[-1] if rows else None


def document_text(db: Database, doc_id: int) -> str:
    row = get_document(db, doc_id)
    if not row.get("text_path"):
        return ""
    p = Path(row["text_path"])
    return p.read_text(encoding="utf-8") if p.exists() else ""


def verify_integrity(db: Database) -> list[dict]:
    """Check every stored original against its recorded hash."""
    out = []
    for row in db.fetchall("SELECT id, kind, sha256, stored_path FROM source_documents"):
        p = Path(row["stored_path"])
        ok = p.exists() and sha256_bytes(p.read_bytes()) == row["sha256"]
        out.append({"id": row["id"], "kind": row["kind"], "ok": ok, "path": str(p)})
    return out


def github_document(db: Database, paths: Paths, payload: dict, label: str) -> dict:
    """Store GitHub evidence as a JSON text document so it has a stable id and hash."""
    text = json.dumps(payload, indent=2, sort_keys=True)
    return add_document(db, paths, "github", text.encode("utf-8"), f"{label}.json", "application/json", label=label)
