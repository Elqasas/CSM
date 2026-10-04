"""Cache-busting for the stylesheet on the development server.

Production needs none of this: ``ManifestStaticFilesStorage`` already puts a
content hash in every filename. The dev server serves ``app.css`` under one
unchanging URL, so a browser keeps showing yesterday's colours after the file
changes - and an inline SVG drawn for today's stylesheet renders as a black
square against yesterday's. The file's modification time in the query string
gives every edit a new URL.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders


def asset_version(request):
    if not settings.DEBUG:
        return {"asset_version": ""}
    path = finders.find("css/app.css")
    if not path:
        return {"asset_version": ""}
    return {"asset_version": str(int(Path(path).stat().st_mtime))}
