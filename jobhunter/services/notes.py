"""Private notes on a job. Stored in their own table, keyed by the stable job id,
so a rediscovered or updated vacancy never touches them. Deletes are soft."""

from __future__ import annotations

from jobhunter.db import Database
from jobhunter.util import now_iso

MAX_NOTE_CHARS = 10_000


class NoteError(Exception):
    pass


def add(db: Database, job_id: int, body: str) -> dict:
    body = (body or "").strip()
    if not body:
        raise NoteError("The note is empty.")
    if len(body) > MAX_NOTE_CHARS:
        raise NoteError("That note is too long (max 10,000 characters).")
    if not db.fetchone("SELECT id FROM jobs WHERE id = ?", (job_id,)):
        raise NoteError(f"Job {job_id} not found.")
    ts = now_iso()
    with db.tx():
        nid = db.insert("job_notes", {"job_id": job_id, "body": body, "created_at": ts, "updated_at": ts})
    return get(db, nid)


def update(db: Database, note_id: int, body: str) -> dict:
    body = (body or "").strip()
    if not body:
        raise NoteError("The note is empty. Delete it instead if you want it gone.")
    if len(body) > MAX_NOTE_CHARS:
        raise NoteError("That note is too long (max 10,000 characters).")
    row = get(db, note_id)
    if row["body"] != body:
        with db.tx():
            db.update("job_notes", {"body": body, "updated_at": now_iso()}, "id = ? AND deleted_at IS NULL", (note_id,))
    return get(db, note_id)


def delete(db: Database, note_id: int) -> dict:
    get(db, note_id)
    with db.tx():
        db.update("job_notes", {"deleted_at": now_iso()}, "id = ?", (note_id,))
    return {"note_id": note_id, "deleted": True}


def get(db: Database, note_id: int) -> dict:
    row = db.fetchone("SELECT * FROM job_notes WHERE id = ? AND deleted_at IS NULL", (note_id,))
    if not row:
        raise NoteError(f"Note {note_id} not found.")
    return row


def list_for_job(db: Database, job_id: int) -> list[dict]:
    return db.fetchall("SELECT * FROM job_notes WHERE job_id = ? AND deleted_at IS NULL ORDER BY updated_at DESC, id DESC", (job_id,))


def latest_for_job(db: Database, job_id: int) -> dict | None:
    return db.fetchone("SELECT * FROM job_notes WHERE job_id = ? AND deleted_at IS NULL ORDER BY updated_at DESC, id DESC LIMIT 1", (job_id,))


def count_for_job(db: Database, job_id: int) -> int:
    return db.fetchone("SELECT COUNT(*) AS n FROM job_notes WHERE job_id = ? AND deleted_at IS NULL", (job_id,))["n"]
