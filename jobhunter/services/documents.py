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
import secrets
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from jobhunter.config import MAX_DOCUMENT_BYTES, Paths, write_private_file
from jobhunter.db import Database
from jobhunter.util import now_iso, sha256_bytes

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".md", ".markdown", ".txt", ".json"}
KINDS = {"cv", "cv_variant", "linkedin", "github", "project", "other"}
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


# ---------------------------------------------------------------------------
# My CVs: variants (presentations) with immutable revisions
# ---------------------------------------------------------------------------
# One factual candidate profile; several presentations. The profile is built from the "profile CV" (kind
# "cv"); other variants (kind "cv_variant") never change it, never reopen onboarding and never become
# the default by themselves. Every revision is an immutable original stored byte-for-byte; a replacement
# is a new revision, never an overwrite. Bytes never travel in the cloud state or the dashboard.
VARIANT_PREFIX = "cv"
REVISION_PREFIX = "rev"
MAX_LABEL_CHARS = 60
MAX_TAGS = 8
PROFILE_LABEL = "My CV"


class CVError(DocumentError):
    pass


def _new_key(prefix: str) -> str:
    return prefix + secrets.token_hex(5)


def ensure_cv_variants(db: Database) -> dict | None:
    """Make the CV the profile was built from the first (and default) variant. Idempotent; safe on every open.
    Earlier versions of that CV (the superseded chain) become its earlier revisions, newest last."""
    prof = db.fetchone("SELECT * FROM cv_variants WHERE is_profile = 1")
    chain = db.fetchall("SELECT * FROM source_documents WHERE kind = 'cv' ORDER BY version, id")
    if not chain:
        return prof
    ts = now_iso()
    with db.tx():
        if not prof:
            has_default = db.fetchone("SELECT id FROM cv_variants WHERE is_default = 1 AND archived_at IS NULL")
            vid = db.insert("cv_variants", {"variant_key": _new_key(VARIANT_PREFIX), "label": PROFILE_LABEL, "tags_json": "[]", "is_default": 0 if has_default else 1, "is_profile": 1, "created_at": chain[0]["created_at"] or ts, "updated_at": ts})
            prof = db.fetchone("SELECT * FROM cv_variants WHERE id = ?", (vid,))
        known = {r["document_id"] for r in db.fetchall("SELECT document_id FROM cv_revisions WHERE variant_id = ?", (prof["id"],))}
        number = db.fetchone("SELECT COALESCE(MAX(number), 0) AS n FROM cv_revisions WHERE variant_id = ?", (prof["id"],))["n"]
        for d in chain:
            if d["id"] in known:
                continue
            number += 1
            db.insert("cv_revisions", {"revision_key": _new_key(REVISION_PREFIX), "variant_id": prof["id"], "document_id": d["id"], "number": number, "note": None, "review_json": None, "created_at": d["created_at"] or ts})
    return db.fetchone("SELECT * FROM cv_variants WHERE id = ?", (prof["id"],))


def _variant_row(db: Database, variant_key: str) -> dict:
    row = db.fetchone("SELECT * FROM cv_variants WHERE variant_key = ?", (variant_key,))
    if not row:
        raise CVError(f"No CV with key {variant_key}.")
    return row


def _current_revision(db: Database, variant_id: int) -> dict | None:
    return db.fetchone(
        "SELECT r.*, d.sha256, d.original_filename, d.byte_size, d.media_type, d.stored_path, d.extraction_status, d.extraction_notes, d.text_path, d.created_at AS uploaded_at "
        "FROM cv_revisions r JOIN source_documents d ON d.id = r.document_id WHERE r.variant_id = ? ORDER BY r.number DESC LIMIT 1",
        (variant_id,),
    )


def _file_available(row: dict | None) -> bool:
    if not row or not row.get("stored_path"):
        return False
    p = Path(row["stored_path"])
    return p.exists() and sha256_bytes(p.read_bytes()) == row["sha256"]


def _variant_view(db: Database, v: dict, with_revisions: bool = False) -> dict:
    cur = _current_revision(db, v["id"])
    out = {
        "key": v["variant_key"],
        "label": v["label"],
        "tags": json.loads(v.get("tags_json") or "[]"),
        "default": bool(v["is_default"]),
        "profile_cv": bool(v["is_profile"]),
        "archived": bool(v.get("archived_at")),
        "archived_at": v.get("archived_at"),
        "created_at": v["created_at"],
        "version": cur["number"] if cur else 0,
        "revision_key": cur["revision_key"] if cur else None,
        "document_id": cur["document_id"] if cur else None,
        "filename": cur["original_filename"] if cur else None,
        "bytes": cur["byte_size"] if cur else None,
        "sha256": cur["sha256"] if cur else None,
        "uploaded_at": cur["uploaded_at"] if cur else None,
        "readable": bool(cur and cur["extraction_status"] == "ok"),
        "file_available": _file_available(cur),
        "review": json.loads(cur["review_json"]) if cur and cur.get("review_json") else [],
    }
    if with_revisions:
        out["revisions"] = [
            {"revision_key": r["revision_key"], "number": r["number"], "filename": r["original_filename"], "bytes": r["byte_size"], "sha256": r["sha256"], "uploaded_at": r["created_at"], "note": r["note"], "file_available": _file_available(r)}
            for r in db.fetchall("SELECT r.*, d.sha256, d.original_filename, d.byte_size, d.stored_path FROM cv_revisions r JOIN source_documents d ON d.id = r.document_id WHERE r.variant_id = ? ORDER BY r.number", (v["id"],))
        ]
    return out


def list_cvs(db: Database, include_archived: bool = False, with_revisions: bool = False) -> list[dict]:
    ensure_cv_variants(db)
    rows = db.fetchall("SELECT * FROM cv_variants ORDER BY is_default DESC, created_at, id")
    return [_variant_view(db, v, with_revisions) for v in rows if include_archived or not v.get("archived_at")]


def get_cv(db: Database, variant_key: str, with_revisions: bool = True) -> dict:
    ensure_cv_variants(db)
    return _variant_view(db, _variant_row(db, variant_key), with_revisions)


def default_cv(db: Database) -> dict | None:
    ensure_cv_variants(db)
    row = db.fetchone("SELECT * FROM cv_variants WHERE is_default = 1 AND archived_at IS NULL")
    if not row:
        row = db.fetchone("SELECT * FROM cv_variants WHERE archived_at IS NULL ORDER BY is_profile DESC, created_at LIMIT 1")
    return _variant_view(db, row) if row else None


def cv_revision(db: Database, revision_key: str) -> dict | None:
    r = db.fetchone(
        "SELECT r.*, v.variant_key, v.label, d.sha256, d.original_filename, d.byte_size, d.media_type, d.stored_path, d.text_path, d.extraction_status "
        "FROM cv_revisions r JOIN cv_variants v ON v.id = r.variant_id JOIN source_documents d ON d.id = r.document_id WHERE r.revision_key = ?",
        (revision_key,),
    )
    if not r:
        return None
    r["file_available"] = _file_available(r)
    return r


def _clean_label(label: str | None) -> str:
    label = re.sub(r"\s+", " ", (label or "")).strip()
    if not label:
        raise CVError("Give this CV a short name, for example 'Platform — English'.")
    return label[:MAX_LABEL_CHARS]


def _clean_tags(tags) -> list[str]:
    if isinstance(tags, str):
        tags = [t for t in re.split(r"[,;]", tags)]
    out: list[str] = []
    for t in tags or []:
        t = re.sub(r"\s+", " ", str(t)).strip().lower()[:30]
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS]


def _same_bytes_elsewhere(db: Database, digest: str, except_variant_id: int | None = None) -> dict | None:
    """The variant whose current revision has exactly these bytes (identical files are never stored twice)."""
    for v in db.fetchall("SELECT * FROM cv_variants WHERE archived_at IS NULL"):
        if except_variant_id and v["id"] == except_variant_id:
            continue
        cur = _current_revision(db, v["id"])
        if cur and cur["sha256"] == digest:
            return v
    return None


def add_cv_variant(db: Database, paths: Paths, data: bytes, original_filename: str | None, label: str, tags=None, media_type: str | None = None, make_default: bool = False) -> dict:
    """A new finished CV presentation. Stored byte-for-byte as revision 1 of a new variant. It never replaces
    another CV, never becomes the default unless asked, and never touches the approved profile."""
    ensure_cv_variants(db)
    label = _clean_label(label)
    tags = _clean_tags(tags)
    if db.fetchone("SELECT id FROM cv_variants WHERE lower(label) = lower(?) AND archived_at IS NULL", (label,)):
        raise CVError(f"You already have a CV called '{label}'. Pick another name, or upload a new version of that one.")
    digest = sha256_bytes(data) if data else ""
    dup = _same_bytes_elsewhere(db, digest) if digest else None
    if dup:
        raise CVError(f"This is the same file as your CV '{dup['label']}'. Nothing was added.")
    doc = add_document(db, paths, "cv_variant", data, original_filename, media_type=media_type, label=label)
    review = review_claims(db, document_text(db, doc["id"])) if doc["extraction_status"] == "ok" else []
    ts = now_iso()
    with db.tx():
        is_default = 1 if (make_default or not db.fetchone("SELECT id FROM cv_variants WHERE is_default = 1 AND archived_at IS NULL")) else 0
        if is_default:
            db.execute("UPDATE cv_variants SET is_default = 0")
        vid = db.insert("cv_variants", {"variant_key": _new_key(VARIANT_PREFIX), "label": label, "tags_json": json.dumps(tags), "is_default": is_default, "is_profile": 0, "created_at": ts, "updated_at": ts})
        db.insert("cv_revisions", {"revision_key": _new_key(REVISION_PREFIX), "variant_id": vid, "document_id": doc["id"], "number": 1, "note": None, "review_json": json.dumps(review) if review else None, "created_at": ts})
    return _variant_view(db, db.fetchone("SELECT * FROM cv_variants WHERE id = ?", (vid,)), with_revisions=True)


def add_cv_revision(db: Database, paths: Paths, variant_key: str, data: bytes, original_filename: str | None, note: str | None = None, media_type: str | None = None) -> dict:
    """Upload a new version of one CV: a new immutable revision. The earlier revisions stay exactly as they
    were (applications keep pointing at the version that was sent). Identical bytes add nothing.
    For the profile CV this is the existing "new CV" path: the approved profile is marked for a review."""
    ensure_cv_variants(db)
    v = _variant_row(db, variant_key)
    if v.get("archived_at"):
        raise CVError("This CV is archived. Bring it back first (unarchive) to add a version.")
    cur = _current_revision(db, v["id"])
    digest = sha256_bytes(data) if data else ""
    if cur and cur["sha256"] == digest:
        return {**_variant_view(db, v, with_revisions=True), "duplicate": True, "note": "That is already the current version of this CV. Nothing changed."}
    dup = _same_bytes_elsewhere(db, digest, except_variant_id=v["id"]) if digest else None
    if dup:
        raise CVError(f"This is the same file as your CV '{dup['label']}'. Nothing was added.")
    kind = "cv" if v["is_profile"] else "cv_variant"
    doc = add_document(db, paths, kind, data, original_filename, media_type=media_type, label=v["label"])
    review = review_claims(db, document_text(db, doc["id"])) if doc["extraction_status"] == "ok" and not v["is_profile"] else []
    ts = now_iso()
    with db.tx():
        number = (cur["number"] if cur else 0) + 1
        db.insert("cv_revisions", {"revision_key": _new_key(REVISION_PREFIX), "variant_id": v["id"], "document_id": doc["id"], "number": number, "note": (note or "")[:300] or None, "review_json": json.dumps(review) if review else None, "created_at": ts})
        db.update("cv_variants", {"updated_at": ts}, "id = ?", (v["id"],))
    out = _variant_view(db, v, with_revisions=True)
    out["duplicate"] = False
    if v["is_profile"]:
        out["note"] = "This is the CV your profile was built from, so Hunter will ask you to check your profile again before preparing applications."
    return out


def set_default_cv(db: Database, variant_key: str) -> dict:
    ensure_cv_variants(db)
    v = _variant_row(db, variant_key)
    if v.get("archived_at"):
        raise CVError("An archived CV cannot be the default. Unarchive it first.")
    with db.tx():
        db.execute("UPDATE cv_variants SET is_default = 0")
        db.update("cv_variants", {"is_default": 1, "updated_at": now_iso()}, "id = ?", (v["id"],))
    return _variant_view(db, _variant_row(db, variant_key))


def rename_cv(db: Database, variant_key: str, label: str | None = None, tags=None) -> dict:
    ensure_cv_variants(db)
    v = _variant_row(db, variant_key)
    values: dict = {"updated_at": now_iso()}
    if label is not None:
        label = _clean_label(label)
        other = db.fetchone("SELECT id FROM cv_variants WHERE lower(label) = lower(?) AND id != ? AND archived_at IS NULL", (label, v["id"]))
        if other:
            raise CVError(f"You already have a CV called '{label}'.")
        values["label"] = label
    if tags is not None:
        values["tags_json"] = json.dumps(_clean_tags(tags))
    with db.tx():
        db.update("cv_variants", values, "id = ?", (v["id"],))
    return _variant_view(db, _variant_row(db, variant_key))


def archive_cv(db: Database, variant_key: str, archive: bool = True) -> dict:
    """Hide a CV (or bring it back). Nothing is rewritten: its revisions and the applications that used them stay."""
    ensure_cv_variants(db)
    v = _variant_row(db, variant_key)
    ts = now_iso()
    with db.tx():
        if archive:
            if v["is_default"]:
                other = db.fetchone("SELECT id FROM cv_variants WHERE id != ? AND archived_at IS NULL ORDER BY is_profile DESC, created_at LIMIT 1", (v["id"],))
                if other:
                    db.update("cv_variants", {"is_default": 1, "updated_at": ts}, "id = ?", (other["id"],))
            db.update("cv_variants", {"archived_at": ts, "is_default": 0, "updated_at": ts}, "id = ?", (v["id"],))
        else:
            db.update("cv_variants", {"archived_at": None, "updated_at": ts}, "id = ?", (v["id"],))
    return _variant_view(db, _variant_row(db, variant_key))


def delete_cv(db: Database, variant_key: str) -> dict:
    """Remove a CV for good. Refused when an application recorded one of its versions as sent (history must
    stay true) and for the profile CV (the profile was built from it): those can be archived instead."""
    ensure_cv_variants(db)
    v = _variant_row(db, variant_key)
    if v["is_profile"]:
        raise CVError("This is the CV your profile was built from. Archive it instead of deleting it.")
    keys = [r["revision_key"] for r in db.fetchall("SELECT revision_key FROM cv_revisions WHERE variant_id = ?", (v["id"],))]
    used = db.fetchone(f"SELECT COUNT(*) AS n FROM applications WHERE submitted_cv_revision_key IN ({','.join('?' * len(keys)) or 'NULL'})", tuple(keys))["n"] if keys else 0
    if used:
        raise CVError("You told Hunter you sent this CV for an application, so it stays in your history. Archive it instead.")
    docs_ids = [r["document_id"] for r in db.fetchall("SELECT document_id FROM cv_revisions WHERE variant_id = ?", (v["id"],))]
    with db.tx():
        db.execute("UPDATE applications SET cv_variant_key = NULL, cv_choice = NULL WHERE cv_variant_key = ?", (variant_key,))
        db.execute("DELETE FROM cv_revisions WHERE variant_id = ?", (v["id"],))
        db.execute("DELETE FROM cv_variants WHERE id = ?", (v["id"],))
        for did in docs_ids:
            row = db.fetchone("SELECT * FROM source_documents WHERE id = ?", (did,))
            db.execute("DELETE FROM source_documents WHERE id = ?", (did,))
            if row and not db.fetchone("SELECT id FROM source_documents WHERE sha256 = ?", (row["sha256"],)):
                for key in ("stored_path", "text_path"):
                    p = Path(row[key]) if row.get(key) else None
                    if p and p.exists():
                        p.unlink()
        if v["is_default"]:
            other = db.fetchone("SELECT id FROM cv_variants WHERE archived_at IS NULL ORDER BY is_profile DESC, created_at LIMIT 1")
            if other:
                db.update("cv_variants", {"is_default": 1}, "id = ?", (other["id"],))
    return {"key": variant_key, "deleted": True}


def cv_file(db: Database, key: str) -> dict:
    """The original bytes of a CV version (by revision key) or of a variant's current version, verified
    against the recorded hash. When the file is not on this computer (a fresh cloud session, a copy made
    without the data folder) the answer says what to do: add the CV again."""
    ensure_cv_variants(db)
    rev = cv_revision(db, key)
    if not rev:
        v = _variant_row(db, key)
        cur = _current_revision(db, v["id"])
        rev = cv_revision(db, cur["revision_key"]) if cur else None
        if not rev:
            raise CVError("This CV has no file yet.")
    p = Path(rev["stored_path"])
    if not p.exists():
        raise CVError(f"The file for '{rev['label']}' (version {rev['number']}, {rev['original_filename']}) is not on this computer. Add this CV again to preview or download it; Hunter checks it is the same file by its hash.")
    data = p.read_bytes()
    if sha256_bytes(data) != rev["sha256"]:
        raise CVError("The stored file does not match its recorded hash. Hunter refuses to hand out a changed original.")
    return {"variant_key": rev["variant_key"], "label": rev["label"], "revision_key": rev["revision_key"], "number": rev["number"], "filename": rev["original_filename"], "media_type": rev["media_type"], "bytes": len(data), "sha256": rev["sha256"], "path": str(p), "data": data}


def cv_text(db: Database, key: str) -> str:
    rev = cv_revision(db, key)
    if not rev:
        v = _variant_row(db, key)
        cur = _current_revision(db, v["id"])
        rev = cv_revision(db, cur["revision_key"]) if cur else None
    return document_text(db, rev["document_id"]) if rev else ""


CERT_WORDS = {"cka": "CKA", "ckad": "CKAD", "cks": "CKS", "aws certified": "AWS Certified", "azure certified": "Azure certified", "terraform associate": "Terraform Associate", "rhcsa": "RHCSA", "rhce": "RHCE", "lpic": "LPIC", "gcp professional": "GCP Professional", "ccna": "CCNA"}
YEARS_RX = re.compile(r"(\d{1,2})\+?\s+years?", re.I)
LEAD_RX = re.compile(r"\b(team lead|tech lead|engineering manager|head of|lead engineer|principal|staff engineer)\b", re.I)


def review_claims(db: Database, text: str) -> list[dict]:
    """Factual claims in a CV presentation that the approved profile does not show. These are questions for a
    concise review, not corrections: the person decides which document is right. Nothing is rewritten."""
    from jobhunter.services import profile as profile_svc

    prof = profile_svc.approved(db)
    if not prof or not text:
        return []
    facts = [f for f in prof["facts"] if f.status != "rejected"]
    fact_text = " ".join(f.text.lower() for f in facts)
    low = text.lower()
    out: list[dict] = []
    for needle, name in CERT_WORDS.items():
        if needle in low and needle not in fact_text:
            out.append({"kind": "certification", "claim": name, "text": f"This CV mentions {name}, which your approved profile does not list. Check which one is right."})
    years = (prof.get("targets") or {}).get("professional_years_relevant")
    for m in YEARS_RX.finditer(text):
        n = int(m.group(1))
        if years is not None and n > float(years) + 1 and f"{n} years" not in fact_text:
            out.append({"kind": "years", "claim": f"{n} years", "text": f"This CV says '{m.group(0)}'; your profile shows about {float(years):g} years of relevant paid work."})
            break
    paid = [f for f in facts if f.category == "role" and f.experience_kind == "professional"]
    m = LEAD_RX.search(text)
    if m and not any(LEAD_RX.search(f.text) for f in paid):
        out.append({"kind": "title", "claim": m.group(0), "text": f"This CV uses the title '{m.group(0)}', which none of the paid roles in your profile show."})
    return out[:6]


LANG_TAGS = {"english": {"english", "en"}, "dutch": {"dutch", "nl", "nederlands"}, "german": {"german", "de", "deutsch"}}
DIRECTION_TAGS = {"platform": {"platform"}, "devops": {"devops"}, "sre": {"sre", "reliability"}, "cloud": {"cloud", "aws", "azure", "gcp"}, "security": {"security"}, "data": {"data"}, "kubernetes": {"kubernetes", "k8s"}}


def suggest_cv(db: Database, job: dict) -> dict | None:
    """One short reason to pick a CV other than the default: a tag or label that names the job's direction or
    language. Never more than that; the person chooses, and only for this job."""
    cvs = [c for c in list_cvs(db) if not c["archived"]]
    default = next((c for c in cvs if c["default"]), None)
    if len(cvs) < 2:
        return None
    title = (job.get("title") or "").lower()
    text = ((job.get("description_text") or job.get("description") or "")[:4000]).lower()
    from jobhunter.services.matching.requirements import languages as job_languages

    try:
        langs = {str(x[0] if isinstance(x, (tuple, list)) else x).lower() for x in (job_languages(text) or [])}
    except Exception:
        langs = set()
    for c in cvs:
        if default and c["key"] == default["key"]:
            continue
        words = set(c["tags"]) | set(re.findall(r"[a-z]+", c["label"].lower()))
        for direction, aliases in DIRECTION_TAGS.items():
            if words & aliases and re.search(rf"\b{direction}\b", title):
                return {"variant_key": c["key"], "label": c["label"], "reason": f"The job title says '{direction}' and this CV is marked '{direction}'."}
        for lang, aliases in LANG_TAGS.items():
            if words & aliases and lang in langs and not (default and (set(default["tags"]) | set(re.findall(r"[a-z]+", default["label"].lower()))) & aliases):
                return {"variant_key": c["key"], "label": c["label"], "reason": f"The posting asks for {lang.capitalize()} and this CV is marked '{lang}'."}
    return None
