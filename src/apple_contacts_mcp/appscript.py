"""Contacts.app scripting: the fallback for what the framework will not do.

Two things need it:

- **Notes.** Since macOS 10.15 the Contacts framework refuses to return or
  store the ``note`` property unless the process holds the
  ``com.apple.developer.contacts.notes`` entitlement, which an unsigned
  interpreter cannot. Contacts.app's scripting dictionary exposes ``note``
  freely.
- **Removing a contact from a CardDAV group.** ``CNSaveRequest
  removeMember:fromGroup:`` reports success and changes nothing for iCloud
  (and other CardDAV) groups on current macOS, whichever record it is given.
  Contacts.app's ``remove person from group`` works.

Both go through ``osascript`` running JXA. Arguments travel via argv, never
string interpolation, so a note containing a quote cannot break the script.
The person and group ``id`` in Contacts.app's dictionary are the same
strings the framework reports as ``identifier``, so no translation is needed.

Costs, stated plainly: it needs **Automation** permission for the process
macOS holds responsible (Claude Desktop launches extension servers through a
helper that makes ``uv`` itself responsible, so the entry may appear under
``uv`` rather than ``Claude``); it launches Contacts.app in the background if
needed; and it is slow next to the framework (hundreds of ms per call).
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Contacts.app answers in well under a second when idle, but while it is
# syncing changes just written through the framework (an iCloud round trip
# after a create or update) it can hold Apple Events for 10–30 s. Give it
# time, but stay under the MCP client's own tool timeout.
_TIMEOUT_S = 40.0

# After a timeout, later calls fail immediately for a short while instead of
# each stalling for the full timeout — a merge reads several notes in a row
# and would otherwise overrun the client. Short, because the usual cause is
# transient.
_BACKOFF_S = 30.0
_blocked_until = 0.0

_PERMISSION_HINT = (
    "Contacts.app did not answer within {t:.0f} s. It is usually busy syncing "
    "changes made a moment ago; wait a few seconds and try again. If it never "
    "answers, macOS may be waiting on an Automation permission prompt: check "
    "System Settings → Privacy & Security → Automation for a Claude or uv entry "
    "and enable Contacts under it."
)


class ScriptingUnavailable(RuntimeError):
    """Contacts.app could not be scripted; the message says why."""


# Kept for callers and tests that know the older name.
NotesUnavailable = ScriptingUnavailable


_JXA_NOTE = r"""
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

_JXA_GROUP_REMOVE = r"""
function run(argv) {
    const [groupId, ...contactIds] = argv;
    const app = Application("Contacts");
    let group = null;
    try { group = app.groups.byId(groupId); group.id(); } catch (e) { group = null; }
    if (group === null) { return "__GROUP_NOT_FOUND__"; }
    let removed = 0;
    for (const id of contactIds) {
        let person = null;
        try { person = app.people.byId(id); person.id(); } catch (e) { person = null; }
        if (person === null) { continue; }
        try { app.remove(person, { from: group }); removed += 1; } catch (e) { /* not a member */ }
    }
    if (removed > 0) { app.save(); }
    return "__OK__:" + removed;
}
"""


def _run_jxa(script: str, argv: list[str]) -> str:
    global _blocked_until
    now = time.monotonic()
    if now < _blocked_until:
        raise ScriptingUnavailable(
            _PERMISSION_HINT.format(t=_TIMEOUT_S)
            + f" (Not retried: a call {int(_BACKOFF_S - (_blocked_until - now))} s ago timed out.)"
        )
    try:
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script, "--", *argv],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise ScriptingUnavailable("osascript is not available on this system.") from exc
    except subprocess.TimeoutExpired as exc:
        _blocked_until = time.monotonic() + _BACKOFF_S
        raise ScriptingUnavailable(_PERMISSION_HINT.format(t=_TIMEOUT_S)) from exc

    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        low = err.lower()
        if "-1743" in err or "not allowed" in low or "not authorized" in low:
            raise ScriptingUnavailable(
                "Automation permission for Contacts.app was not granted. Enable "
                "Contacts under the Claude or uv entry in System Settings → Privacy "
                "& Security → Automation, then try again."
            )
        raise ScriptingUnavailable(f"Contacts.app scripting failed: {err or 'unknown error'}")

    out = proc.stdout
    if out.endswith("\n"):
        out = out[:-1]
    return out


def read_note(contact_id: str) -> Optional[str]:
    """Return the contact's note, or None if it has none."""
    out = _run_jxa(_JXA_NOTE, [contact_id, "get"])
    if out == "__NOT_FOUND__":
        raise ValueError(f"Contact not found in Contacts.app: {contact_id}")
    return out if out else None


def write_note(contact_id: str, text: Optional[str]) -> None:
    """Set (or clear, with None / "") the contact's note and save Contacts.app."""
    out = _run_jxa(_JXA_NOTE, [contact_id, "set", text or ""])
    if out == "__NOT_FOUND__":
        raise ValueError(f"Contact not found in Contacts.app: {contact_id}")
    if out != "__OK__":
        raise ScriptingUnavailable(f"Unexpected reply from Contacts.app: {out!r}")


def remove_from_group(group_id: str, contact_ids: list[str]) -> int:
    """Remove contacts from a group through Contacts.app. Returns how many were removed."""
    out = _run_jxa(_JXA_GROUP_REMOVE, [group_id, *contact_ids])
    if out == "__GROUP_NOT_FOUND__":
        raise ValueError(f"Group not found in Contacts.app: {group_id}")
    if not out.startswith("__OK__:"):
        raise ScriptingUnavailable(f"Unexpected reply from Contacts.app: {out!r}")
    return int(out.split(":", 1)[1])
