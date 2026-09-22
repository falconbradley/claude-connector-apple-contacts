"""Contact notes via Contacts.app scripting.

Why this file exists
--------------------
Since macOS 10.15 the Contacts framework refuses to return or store the
``note`` property unless the calling process holds the
``com.apple.developer.contacts.notes`` entitlement. Apple grants that
entitlement to signed apps on request; an unsigned Python interpreter
launched by ``uv run`` can never carry it. Every other property works.

Contacts.app's own scripting dictionary, however, exposes ``note`` on
``person`` with no such restriction. So when the framework declines, the
connector reads and writes notes by asking Contacts.app — the same route
the companion Apple Mail connector uses for everything.

Trade-offs, stated plainly:

- It needs **Automation** permission (System Settings → Privacy & Security
  → Automation → Claude → Contacts), which macOS prompts for separately
  from the Contacts permission.
- It launches Contacts.app in the background if it is not running.
- It is slow compared with the framework (hundreds of ms per call).

For those reasons notes are only touched when a caller asks for them:
``get_contact(include_notes=true)``, or a ``notes`` argument on
``create_contact`` / ``update_contact``.

The person ``id`` in Contacts.app's dictionary is the same string the
framework reports as ``CNContact.identifier`` (``<UUID>:ABPerson``), so no
translation is needed.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

_TIMEOUT_S = 30.0

# JXA rather than AppleScript so arguments travel via argv, never string
# interpolation — a note containing a quote must not break the script.
_JXA = r"""
function run(argv) {
    const [id, mode, text] = argv;
    const app = Application("Contacts");
    let person = null;
    try { person = app.people.byId(id); person.id(); } catch (e) { person = null; }
    if (person === null) {
        const matches = app.people.whose({ id: id })();
        if (matches.length === 0) { return "__NOT_FOUND__"; }
        person = matches[0];
    }
    if (mode === "get") {
        const n = person.note();
        return n === null || n === undefined ? "" : n;
    }
    person.note = text;
    app.save();
    return "__OK__";
}
"""


class NotesUnavailable(RuntimeError):
    """Notes could not be read or written; the message says why."""


def _run(contact_id: str, mode: str, text: str = "") -> str:
    try:
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", _JXA, "--", contact_id, mode, text],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise NotesUnavailable("osascript is not available on this system.") from exc
    except subprocess.TimeoutExpired as exc:
        raise NotesUnavailable(
            "Contacts.app did not answer within 30 s. If an Automation permission "
            "prompt is showing, approve it and try again."
        ) from exc

    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "-1743" in err or "not allowed" in err.lower():
            raise NotesUnavailable(
                "Automation permission for Contacts.app was not granted. Enable "
                "Claude → Contacts under System Settings → Privacy & Security → "
                "Automation, then try again."
            )
        raise NotesUnavailable(f"Contacts.app scripting failed: {err or 'unknown error'}")

    out = proc.stdout
    # osascript appends a trailing newline to string results.
    if out.endswith("\n"):
        out = out[:-1]
    if out == "__NOT_FOUND__":
        raise ValueError(f"Contact not found in Contacts.app: {contact_id}")
    return out


def read_note(contact_id: str) -> Optional[str]:
    """Return the contact's note, or None if it has none."""
    out = _run(contact_id, "get")
    return out if out else None


def write_note(contact_id: str, text: Optional[str]) -> None:
    """Set (or clear, with None / "") the contact's note and save Contacts.app."""
    out = _run(contact_id, "set", text or "")
    if out != "__OK__":
        raise NotesUnavailable(f"Unexpected reply from Contacts.app: {out!r}")
