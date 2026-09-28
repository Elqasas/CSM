"""Refit an account logo to the tile it is shown in.

The mark is drawn at 50x50 CSS pixels at most, and a high-density screen spends
up to three device pixels on each, so a 150x150 square is the most detail the
page can ever use.

Every logo is stored as a solid square tile, the way a well-made one (a brand
mark on its own background, filling the square with an even margin) already
is. A tile rather than a transparent cut-out, because a brand logo is drawn for
one background: shown transparent in the dark theme, dark ink disappears into
the page, and an upload with a white background baked in reads as a white box
floating in a dark square. An opaque tile looks the same in both themes.

The steps:

1. Transparency is flattened onto white - or onto a dark tile when the mark
   itself is near-white, since a white logo on white is no logo at all.
2. The background is read from the image's own edge. Where the edge is one
   colour, empty space in that colour is cropped away, so a file with a wide
   empty border does not shrink the mark to a speck.
3. The mark is scaled to fit inside the square with a fixed margin, and the
   margin is filled with that same background colour - so a logo on a coloured
   square stays a coloured tile rather than acquiring a white frame.
"""

from __future__ import annotations

import io
from pathlib import PurePosixPath

from django.core.files.base import ContentFile

#: Shown at 50x50 CSS pixels; three device pixels to each on the densest screens.
LOGO_STORED_PX = 150

#: The share of the square the mark may fill; the rest is an even margin.
LOGO_FILL = 0.84

WHITE = (255, 255, 255)

#: The tile behind a mark that is itself white. Neutral, and dark enough that
#: white ink reads on it.
DARK_TILE = (31, 41, 55)

#: How far a pixel may stray from the background and still count as background.
#: JPEG noise and anti-aliasing put an "empty" white edge a few steps off 255.
_TOLERANCE = 24

#: The share of edge pixels that must agree before the edge counts as one colour.
_UNIFORM_EDGE = 0.9


def _flatten(image):
    """Put a transparent image on a solid tile, choosing white unless the mark is white."""
    from PIL import Image, ImageStat

    alpha = image.getchannel("A")
    if alpha.getextrema() == (255, 255):
        return image.convert("RGB")

    # A white mark has next to no dark ink. Brightness on average is not the
    # test: a white box with dark lettering inside it averages near-white too,
    # and that one belongs on white.
    opaque = alpha.point(lambda a: 255 if a > 128 else 0)
    backdrop = WHITE
    if opaque.getbbox():
        ink = image.convert("L").point(lambda v: 255 if v < 160 else 0)
        dark = ImageStat.Stat(ink, mask=opaque).mean[0] / 255
        if dark < 0.02:
            backdrop = DARK_TILE

    tile = Image.new("RGB", image.size, backdrop)
    tile.paste(image, mask=alpha)
    return tile


def _edge_colour(image):
    """The colour the image's border is made of, or None when the edge is busy."""
    width, height = image.size
    pixels = image.load()
    edge = [pixels[x, 0] for x in range(width)] + [pixels[x, height - 1] for x in range(width)]
    edge += [pixels[0, y] for y in range(height)] + [pixels[width - 1, y] for y in range(height)]

    median = tuple(sorted(channel)[len(channel) // 2] for channel in zip(*edge, strict=True))
    close = sum(
        1 for px in edge if max(abs(a - b) for a, b in zip(px, median, strict=True)) <= _TOLERANCE
    )
    if close < _UNIFORM_EDGE * len(edge):
        return None
    # A JPEG's "white" is rarely 255; snap it, or the tile shows a faint grey margin.
    if min(median) >= 255 - _TOLERANCE:
        return WHITE
    return median


def _crop_to_mark(image, background):
    """Crop away empty space in the background colour around the mark."""
    from PIL import Image, ImageChops

    difference = ImageChops.difference(image, Image.new("RGB", image.size, background))
    mask = difference.convert("L").point(lambda v: 255 if v > _TOLERANCE else 0)
    box = mask.getbbox()
    return image.crop(box) if box else image


def fit_logo(source, name: str) -> ContentFile:
    """Return ``source`` refitted to the logo tile, as a PNG named after ``name``."""
    from PIL import Image, ImageOps

    source.seek(0)
    with Image.open(source) as opened:
        # A phone photo is stored sideways with an EXIF note saying so; the
        # browser honours the note, a resize discards it, so apply it first.
        image = _flatten(ImageOps.exif_transpose(opened).convert("RGBA"))

    # A screenshot often nests one empty frame in another - white page, grey
    # panel, then the mark - so crop until the edge stops being one colour or
    # stops giving anything up. A busy edge (a photo, a pattern) has no empty
    # space to find and no single colour to extend, and stays whole on white.
    background = WHITE
    for _ in range(3):
        edge = _edge_colour(image)
        if edge is None:
            break
        cropped = _crop_to_mark(image, edge)
        # Nothing to crop means the edge is the mark itself - a solid block of
        # the logo reaching the border - not empty space around it.
        if cropped.size == image.size:
            break
        background = edge
        image = cropped

    inner = round(LOGO_STORED_PX * LOGO_FILL)
    scale = inner / max(image.width, image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    image = image.resize(size, Image.Resampling.LANCZOS)

    tile = Image.new("RGB", (LOGO_STORED_PX, LOGO_STORED_PX), background)
    tile.paste(image, ((LOGO_STORED_PX - size[0]) // 2, (LOGO_STORED_PX - size[1]) // 2))

    buffer = io.BytesIO()
    tile.save(buffer, format="PNG", optimize=True)
    stem = PurePosixPath(name).stem or "logo"
    return ContentFile(buffer.getvalue(), name=f"{stem}.png")


def is_fitted(source) -> bool:
    """Whether ``source`` is already a refitted logo.

    Refitting is not idempotent - resampling a resample softens it each time -
    so a square opaque PNG of the stored size is left as it is. Opaque matters:
    the first version of the refit stored transparent tiles, and those are
    exactly the ones that need redoing.
    """
    from PIL import Image

    source.seek(0)
    with Image.open(source) as image:
        return (
            image.format == "PNG"
            and image.size == (LOGO_STORED_PX, LOGO_STORED_PX)
            and image.mode == "RGB"
        )
