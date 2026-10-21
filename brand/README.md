# KubeCraft brand assets

Put the supplied KubeCraft mark here, exactly as KubeCraft provided it:

- `KubeCraft-Cog-3.png`: the white cog on a black square. Never recoloured, redrawn or cropped.
- `ASSET_NOTES.md`: where the file came from, who approved it, and its licence.

Hunter picks the file up on its own: `uv run jobhunter board build --out <file>` inlines it into the dashboard
page, and the local dashboard serves it at `/static/brand/KubeCraft-Cog-3.png`. Until the file is here every
surface shows the accessible text mark "KC" with the name "KubeCraft". See `docs/brand.md`.

Nothing else belongs in this folder.
