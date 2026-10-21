# Brand: JobHunterOS by KubeCraft

## Names
- **JobHunterOS**: the product and the dashboard. Page title: "JobHunterOS by KubeCraft". Header: "JobHunterOS" with
  a smaller "by KubeCraft" and the KubeCraft mark.
- **KubeCraft**: the parent brand. Never "Kubecraft", "KubeCraft OS" or "Hunter OS".
- **Hunter**: the assistant that speaks to the student (the `/jobhunter` skill, the digest's sender line, the CLI's
  voice). Hunter is part of JobHunterOS; it is not the product's name.

Where the names appear and must agree: the artifact dashboard (`jobhunter/board/hunter-board.html`), the local
dashboard (`jobhunter/web`), the digest subject and footer (`jobhunter/services/notifications.py`), the README,
the onboarding wording in `.claude/skills/jobhunter/`, and `jobhunter board page` (the publish title).
`tests/test_branding.py` checks them.

## The mark: provenance and rules
The authentic KubeCraft mark is the **white cog on black** that KubeCraft supplied: a round cog with radial teeth
and an inner ring, white on a black square. The file Hunter uses is `brand/KubeCraft-Cog-3.png`, exactly as
supplied: it is never recoloured, redrawn, cropped or given new geometry. Scaling for display is the only change.

State on 2026-10-21: `brand/KubeCraft-Cog-3.png` is the supplied file, byte for byte (SHA-256
`5d86782fa2674a22efad5b27f8a966c59075ff7f3a3b30f9b34d8fe1cf7ba6c7`, 124,354 bytes; provenance in
`brand/ASSET_NOTES.md`). How Hunter uses it:

- `uv run jobhunter board build --out <file>` writes the dashboard page with the mark inlined as a data URI when
  `brand/KubeCraft-Cog-3.png` exists next to the code; otherwise the page carries the accessible text mark.
- The local dashboard serves `brand/KubeCraft-Cog-3.png` at `/static/brand/KubeCraft-Cog-3.png` when it exists.
- The email is readable without images: no logo image is embedded; the subject and the footer carry the names.

Should the file ever be missing (a copy made without it), every surface shows the text fallback: the letters
"KC" in a square, with the accessible name "KubeCraft". Nothing invented stands in for the cog.

## What was there before, and why it was removed
`jobhunter/web/static/logo.svg` and the inline SVG the dashboard page used to carry were drawn during
development: an orange cog with eight spokes. They were not supplied by KubeCraft and do not match the supplied
mark (which is white on black), so they are not authentic and are no longer shown. The orange accent colour of
the dashboard is a design choice of this product, not a KubeCraft brand colour; KubeCraft can change the tokens
at the top of `hunter-board.html` (`--accent`) and `app.css`.

## Google sign-in
The shared Google OAuth client KubeCraft publishes (docs/email.md) should carry the display name "JobHunterOS by KubeCraft", so the consent screen and the local dashboard say the same thing.

## Accessibility
The mark always has a text alternative ("KubeCraft"). The header reads "JobHunterOS by KubeCraft" to screen
readers in that order. Colour never carries meaning on its own.

## Adding the asset (KubeCraft)
1. Put the supplied file at `brand/KubeCraft-Cog-3.png` (and `brand/ASSET_NOTES.md` with its origin and licence).
2. Run `uv run pytest tests/test_branding.py`: it checks the page inlines the file untouched and the local
   dashboard serves it.
3. Re-publish each student's dashboard page once (`jobhunter board build`, then the Artifact publish with the
   same URL), as described in `.claude/skills/jobhunter/board.md`.
