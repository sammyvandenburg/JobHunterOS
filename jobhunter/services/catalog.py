"""Starter catalogue loading: employers and discovery feeds into the database."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from jobhunter.db import Database
from jobhunter.util import normalize_company, now_iso

CATALOG_PATH = Path(__file__).resolve().parents[2] / "catalog" / "employers.yaml"

REGION_MAP = {"na": ["na"], "eu": ["eu"], "both": ["na", "eu"]}


def load_catalog(path: Path | None = None) -> dict:
    p = path or CATALOG_PATH
    with open(p, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    employers = data.get("employers") or []
    feeds = data.get("feeds") or []
    for e in employers:
        for key in ("name", "ats", "token", "region", "careers", "checked"):
            if key not in e:
                raise ValueError(f"catalogue entry missing '{key}': {e}")
        if e["ats"] not in {"greenhouse", "lever", "ashby"}:
            raise ValueError(f"unsupported ats in catalogue: {e['ats']}")
    return {"version": str(data.get("version")), "employers": employers, "feeds": feeds}


def source_id_for(ats: str, token: str) -> str:
    return f"{ats}:{token}"


def source_config(ats: str, token: str, employer_name: str, region: str | None = None) -> dict:
    if ats == "greenhouse":
        return {"token": token, "employer_name": employer_name}
    if ats == "lever":
        return {"site": token, "employer_name": employer_name, "region": region or "us"}
    if ats == "ashby":
        return {"board": token, "employer_name": employer_name}
    raise ValueError(ats)


def upsert_employer(
    db: Database,
    name: str,
    *,
    origin: str,
    ats_kind: str | None = None,
    ats_token: str | None = None,
    ats_region: str | None = None,
    regions: list[str] | None = None,
    careers_url: str | None = None,
    website: str | None = None,
    discovered_via: str | None = None,
    catalogue_checked_at: str | None = None,
) -> int:
    canonical = normalize_company(name) or name.lower().strip()
    row = db.fetchone("SELECT * FROM employers WHERE canonical_name = ?", (canonical,))
    if row:
        values: dict = {}
        if ats_kind and not row.get("ats_kind"):
            values.update({"ats_kind": ats_kind, "ats_token": ats_token, "ats_region": ats_region})
        if careers_url and not row.get("careers_url"):
            values["careers_url"] = careers_url
        if catalogue_checked_at:
            values["catalogue_checked_at"] = catalogue_checked_at
        if regions and not row.get("regions_json"):
            values["regions_json"] = json.dumps(regions)
        if values:
            db.update("employers", values, "id = ?", (row["id"],))
        return int(row["id"])
    return db.insert(
        "employers",
        {
            "name": name.strip(),
            "canonical_name": canonical,
            "website": website,
            "careers_url": careers_url,
            "ats_kind": ats_kind,
            "ats_token": ats_token,
            "ats_region": ats_region,
            "regions_json": json.dumps(regions or []),
            "origin": origin,
            "discovered_via": discovered_via,
            "catalogue_checked_at": catalogue_checked_at,
            "first_seen_at": now_iso(),
        },
    )


def ensure_source(db: Database, source_id: str, kind: str, config: dict, origin: str, employer_id: int | None = None, enabled: bool = True) -> bool:
    """Create a watch source if missing. Returns True when created."""
    if db.fetchone("SELECT id FROM watch_sources WHERE id = ?", (source_id,)):
        return False
    db.insert(
        "watch_sources",
        {
            "id": source_id,
            "kind": kind,
            "employer_id": employer_id,
            "config_json": json.dumps(config),
            "enabled": 1 if enabled else 0,
            "origin": origin,
            "created_at": now_iso(),
        },
    )
    return True


def sync_catalog(db: Database, path: Path | None = None) -> dict:
    """Load the catalogue into the DB without touching user edits (idempotent)."""
    cat = load_catalog(path)
    created_employers = created_sources = 0
    with db.tx():
        for e in cat["employers"]:
            before = db.fetchone("SELECT COUNT(*) AS n FROM employers")["n"]
            eid = upsert_employer(
                db,
                e["name"],
                origin="catalogue",
                ats_kind=e["ats"],
                ats_token=str(e["token"]),
                ats_region=e.get("ats_region"),
                regions=REGION_MAP.get(e["region"], ["na", "eu"]),
                careers_url=e.get("careers"),
                catalogue_checked_at=str(e.get("checked")),
            )
            after = db.fetchone("SELECT COUNT(*) AS n FROM employers")["n"]
            created_employers += int(after > before)
            sid = source_id_for(e["ats"], str(e["token"]))
            if ensure_source(db, sid, e["ats"], source_config(e["ats"], str(e["token"]), e["name"], e.get("ats_region")), "catalogue", eid):
                created_sources += 1
        for f in cat["feeds"]:
            if ensure_source(db, f["id"], f["kind"], f.get("config") or {}, "builtin", None, enabled=bool(f.get("enabled", True))):
                created_sources += 1
        db.set_setting("catalog.version", cat["version"])
        db.set_setting("catalog.synced_at", now_iso())
    return {"version": cat["version"], "employers_created": created_employers, "sources_created": created_sources}
