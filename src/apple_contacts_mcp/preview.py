"""
Inline contact preview card (MCP Apps, SEP-1865).

Chat hosts that implement the MCP Apps extension -- Claude Desktop and
claude.ai among them -- can render a tool's result with an HTML view the
server ships as a ``ui://`` resource. The host reads the resource once,
loads it in a sandboxed iframe next to the tool call in the transcript,
and forwards the tool's ``structuredContent`` to it over postMessage.

This is the Contacts member of the family the Apple Mail (``preview_email``,
``preview_thread``) and Apple Messages connectors already ship: one
self-contained document in ``ui/`` (no external scripts, styles, or images,
so it runs under the host's restrictive default CSP), the same host tokens
and handshake, the same card chrome. ``preview_contact`` is its only tool.

The module owns both halves of the contract: the resource, and the payload
builder that turns a ``ContactDetail`` into what the card renders. The
contact photo rides in ``structuredContent`` only, shrunk to a small JPEG
first -- a full contact photo is a few hundred KB of base64, which would
overflow the tool-result limit if it reached the model, and even the
"thumbnail" Contacts keeps can exceed a megabyte.

Hosts without MCP Apps ignore the ``_meta.ui`` hint and see the text
content only, so the tool still degrades to a normal ``get_contact`` answer.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any, Optional

from mcp.server.apps import Apps
from mcp.types import CallToolResult, TextContent

from .models import ContactDetail, OtherCard

logger = logging.getLogger(__name__)

# Identifier hosts use to find the view for the tool; must match the tool's
# ``_meta.ui.resourceUri``. Stable across releases so cached templates stay
# valid.
PREVIEW_URI = "ui://apple-contacts/contact-preview"

# Nested form is the spec; the flat key is the pre-GA format some hosts still
# read (the SDK tells hosts to check both). ``Apps.tool`` owns the nested one;
# this is merged in beside it.
LEGACY_UI_META: dict[str, Any] = {"ui/resourceUri": PREVIEW_URI}

# The avatar is drawn at 56 CSS px; 192 px covers a 3x display. A thumbnail
# already under the byte budget in a format every host can decode is passed
# through untouched (the median in a real store is ~13 KB); anything larger,
# or HEIC/TIFF, is re-encoded. Past the budget even after shrinking, the
# card falls back to initials rather than ship a large payload.
PHOTO_SIDE_PX = 192
MAX_PHOTO_BYTES = 48 * 1024
_WEB_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})

_HTML_PATH = Path(__file__).parent / "ui" / "contact_preview.html"


def preview_html() -> str:
    """The card's HTML document."""
    return _HTML_PATH.read_text(encoding="utf-8")


def build_apps() -> Apps:
    """The Apps extension with the preview resource registered.

    Tools opt in with ``@apps.tool(resource_uri=PREVIEW_URI, meta=LEGACY_UI_META)``;
    the instance is then handed to ``MCPServer(extensions=[apps])``.
    """
    apps = Apps()
    apps.add_html_resource(
        PREVIEW_URI,
        preview_html(),
        name="contact_preview",
        title="Contact preview card",
        description=(
            "Interactive card that renders a contact inline in the chat: photo "
            "or initials, name, nickname, company and title, phones, emails, "
            "addresses, birthday, the account it lives in, other cards for the "
            "same person, and an Open-in-Contacts button. Rendered by hosts that "
            "support MCP Apps."
        ),
        # No CSP block: the card needs no network access at all, so the host's
        # restrictive default is exactly right. The card draws its own border,
        # like the Mail and Messages cards, so ask the host not to add another.
        prefers_border=False,
    )
    return apps


# ---------------------------------------------------------------------------
# Photo
# ---------------------------------------------------------------------------

def photo_data_uri(raw: bytes) -> Optional[str]:
    """A ``data:`` URI small enough to embed in the card, or None.

    None means "show initials": no photo, an undecodable one, or one that is
    still over ``MAX_PHOTO_BYTES`` after shrinking.
    """
    if not raw:
        return None
    from .contacts import _sniff_mime  # PyObjC-heavy; keep it off the import path

    data, mime = raw, _sniff_mime(raw)
    if len(data) > MAX_PHOTO_BYTES or mime not in _WEB_IMAGE_TYPES:
        shrunk = shrink_to_jpeg(raw, PHOTO_SIDE_PX)
        if not shrunk:
            return None
        data, mime = shrunk, "image/jpeg"
    if len(data) > MAX_PHOTO_BYTES:
        return None
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def shrink_to_jpeg(raw: bytes, side: int = PHOTO_SIDE_PX) -> Optional[bytes]:
    """Centre-crop ``raw`` to a square and re-encode it as a ``side``-px JPEG.

    Contacts thumbnails are square already (they carry the user's crop); the
    crop matters for the full-image fallback. Drawn through NSImage, which
    decodes HEIC and honours EXIF orientation. JPEG has no alpha, so the
    square is filled white first -- a transparent logo would otherwise come
    out on black. Returns None if the image cannot be decoded or drawn.
    """
    try:
        from AppKit import (  # type: ignore
            NSBitmapImageFileTypeJPEG,
            NSBitmapImageRep,
            NSColor,
            NSCompositingOperationSourceOver,
            NSDeviceRGBColorSpace,
            NSGraphicsContext,
            NSImage,
            NSImageCompressionFactor,
            NSImageInterpolationHigh,
            NSRectFill,
        )
        from Foundation import NSData, NSMakeRect  # type: ignore

        img = NSImage.alloc().initWithData_(NSData.dataWithBytes_length_(raw, len(raw)))
        if img is None:
            return None
        size = img.size()
        edge = min(size.width, size.height)
        if edge <= 0:
            return None
        src = NSMakeRect((size.width - edge) / 2, (size.height - edge) / 2, edge, edge)
        dst = NSMakeRect(0, 0, side, side)
        rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(  # noqa: E501
            None, side, side, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0
        )
        ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
        if ctx is None:
            return None
        NSGraphicsContext.saveGraphicsState()
        try:
            NSGraphicsContext.setCurrentContext_(ctx)
            ctx.setImageInterpolation_(NSImageInterpolationHigh)
            NSColor.whiteColor().set()
            NSRectFill(dst)
            img.drawInRect_fromRect_operation_fraction_(dst, src, NSCompositingOperationSourceOver, 1.0)
            ctx.flushGraphics()
        finally:
            NSGraphicsContext.restoreGraphicsState()
        out = rep.representationUsingType_properties_(
            NSBitmapImageFileTypeJPEG, {NSImageCompressionFactor: 0.82}
        )
        return bytes(out) if out is not None else None
    except Exception:
        logger.warning("Could not shrink a contact photo for the preview card.", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Payload and result
# ---------------------------------------------------------------------------

def build_preview_payload(
    detail: ContactDetail,
    *,
    photo: Optional[str],
    open_link: Optional[str],
    other_cards: list[OtherCard],
    is_me: bool,
) -> dict[str, Any]:
    """Shape a contact into what the card renders.

    The card's contract is ``get_contact``'s JSON (same field names, so the
    two never drift) plus keys only the card needs: ``photo`` (a ``data:``
    URI or null), ``open_link`` (the localhost link behind the
    Open-in-Contacts button -- chat hosts refuse addressbook://), the other
    cards for the same person, whether this is the user's own card, and
    ``accounts``.

    ``accounts`` is every account the contact's fields come from: its own
    container first, then those of the cards linked into it. A linked
    contact often has no container of its own -- its unified identifier
    belongs to none of the per-account cards (160 of 813 in a real store) --
    so ``container_name`` alone would leave the card unable to say where it
    lives.
    """
    accounts: list[str] = [detail.container_name] if detail.container_name else []
    for card in other_cards:
        if card.linked and card.container_name and card.container_name not in accounts:
            accounts.append(card.container_name)
    return {
        **detail.model_dump(mode="json"),
        "photo": photo,
        "open_link": open_link,
        "other_cards": [c.model_dump(mode="json") for c in other_cards],
        "is_me": is_me,
        "accounts": accounts,
    }


def build_preview_result(payload: dict[str, Any]) -> CallToolResult:
    """Assemble the tool result: text for the model, structured data for the card.

    ``content`` is what the model reads and what non-Apps hosts display: the
    payload without the photo, which would only burn context (the model
    knows whether there is one from ``has_image``). ``structuredContent``
    goes to the iframe and carries everything.
    """
    model_view = {k: v for k, v in payload.items() if k != "photo"}
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(model_view, indent=2, ensure_ascii=False))],
        structured_content=payload,
    )
