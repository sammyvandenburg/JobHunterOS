"""One name set everywhere: JobHunterOS (the product and the dashboard), KubeCraft (the parent), Hunter (the
assistant). The KubeCraft mark is the supplied file, inlined untouched when present; otherwise an accessible text
mark. Nothing invented stands in for the cog, and the email reads without images."""

from __future__ import annotations

import base64
import re
from pathlib import Path

from fastapi.testclient import TestClient

from jobhunter.services import board, notifications
from tests.conftest import approve_profile

ROOT = Path(__file__).resolve().parents[1]
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


def test_the_dashboard_page_is_named_and_marked_without_invented_artwork():
    page = board.page_path().read_text(encoding="utf-8")
    assert "<title>JobHunterOS by KubeCraft</title>" in page
    assert re.search(r'<div class="brand">.*JobHunterOS<small>by KubeCraft</small>', page, re.S)
    assert "<svg" not in page.split("</header>")[0]  # no drawn cog in the header: the mark is the supplied file or text
    assert 'aria-label="KubeCraft"' in page and "KUBECRAFT-MARK" in page


def test_board_build_inlines_the_supplied_mark_untouched_or_falls_back_to_text(tmp_path, monkeypatch):
    out = tmp_path / "page.html"
    monkeypatch.setattr(board, "BRAND_PNG", tmp_path / "missing.png")
    res = board.build_page(out)
    html = out.read_text(encoding="utf-8")
    assert res["mark"] == "text" and 'class="mark"' in html and "data:image/png" not in html and ">KC<" in html
    png = tmp_path / "KubeCraft-Cog-3.png"
    png.write_bytes(PNG)
    monkeypatch.setattr(board, "BRAND_PNG", png)
    res = board.build_page(out)
    html = out.read_text(encoding="utf-8")
    assert res["mark"] == "file" and 'alt="KubeCraft"' in html
    m = re.search(r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"', html)
    assert m and base64.b64decode(m.group(1)) == PNG  # byte for byte: never recoloured or redrawn
    assert ">KC<" not in html.split("</header>")[0]


def test_the_local_dashboard_and_the_email_carry_the_same_names(ctx, db, monkeypatch, tmp_path):
    from jobhunter.web.app import create_app

    app = create_app(ctx)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    html = client.get("/").text
    assert "JobHunterOS by KubeCraft" in html and "logo.svg" not in html
    assert re.search(r'class="name">JobHunterOS<', html) and "by KubeCraft" in html
    assert client.get("/static/logo.svg").status_code == 404
    # The mark is served only when the supplied file exists.
    monkeypatch.setattr(board, "BRAND_PNG", tmp_path / "KubeCraft-Cog-3.png")
    assert client.get("/static/brand/KubeCraft-Cog-3.png").status_code == 404
    (tmp_path / "KubeCraft-Cog-3.png").write_bytes(PNG)
    r = client.get("/static/brand/KubeCraft-Cog-3.png")
    assert r.status_code == 200 and r.content == PNG
    # The digest: subject and footer name the product and the assistant, no images needed to read it.
    approve_profile(db, ctx, countries="Netherlands")
    from tests.test_board import _seed_jobs

    _seed_jobs(ctx, db)
    notifications.set_recipient(db, "student@example.com")
    jobs = notifications.pending_jobs(db, limit=5)
    d = notifications.compose(db, jobs)
    assert d.subject.startswith("JobHunterOS: ") and "worth a look" in d.subject
    assert d.text.rstrip().endswith("Hunter, your JobHunterOS assistant by KubeCraft") and "<img" not in d.html
    assert "JobHunterOS" in d.html and "KubeCraft" in d.html


def test_readme_and_skill_use_the_official_names():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.splitlines()[0].strip() == "# JobHunterOS by KubeCraft"
    skill = (ROOT / ".claude" / "skills" / "jobhunter" / "SKILL.md").read_text(encoding="utf-8")
    assert "JobHunterOS" in skill and "Hunter" in skill
    assert not (ROOT / "jobhunter" / "web" / "static" / "logo.svg").exists()
