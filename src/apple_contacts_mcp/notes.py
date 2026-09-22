"""Contact notes via Contacts.app scripting. See appscript.py for the why.

Kept as a thin module so the notes-specific names stay where callers and
tests found them.
"""

from __future__ import annotations

from .appscript import (  # noqa: F401
    NotesUnavailable,
    ScriptingUnavailable,
    _JXA_NOTE as _JXA,
    read_note,
    write_note,
)
