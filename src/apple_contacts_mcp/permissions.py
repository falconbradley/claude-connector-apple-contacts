"""TCC permission helpers for the Contacts entity.

Contacts access goes through ``CNContactStore``:

- ``+authorizationStatusForEntityType:`` reports the current grant.
- ``-requestAccessForEntityType:completionHandler:`` prompts if the status
  is still "not determined".

The prompt needs ``NSContactsUsageDescription`` in the calling process's
Info.plist to appear. When the process is an unsigned, dynamically-launched
Python interpreter (which is what ``uv run`` produces under Claude Desktop),
the system attaches the prompt to the *responsible process* — typically
Claude Desktop itself.

If the user has previously granted access via System Settings the call
returns ``True`` immediately; otherwise we surface a structured
``PermissionDeniedError`` with remediation steps.

macOS 15 added a "limited" status, where the user picks a subset of
contacts to share. The connector treats it as granted: every tool simply
sees the shared subset.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# CNEntityType
CN_ENTITY_CONTACTS = 0

# CNAuthorizationStatus
STATUS_NOT_DETERMINED = 0
STATUS_RESTRICTED = 1
STATUS_DENIED = 2
STATUS_AUTHORIZED = 3
STATUS_LIMITED = 4  # macOS 15+

GRANTED_STATUSES = {STATUS_AUTHORIZED, STATUS_LIMITED}


class PermissionDeniedError(RuntimeError):
    """Raised when Contacts access is not granted."""

    def __init__(self, underlying: Optional[str] = None) -> None:
        msg = (
            "Apple Contacts access was not granted to this process.\n\n"
            "To fix:\n"
            "  1. Open System Settings → Privacy & Security → Contacts\n"
            "  2. Enable 'Claude' (or the parent app, e.g. Terminal/iTerm)\n"
            "  3. Quit and relaunch Claude Desktop\n"
        )
        if underlying:
            msg += f"\nUnderlying error: {underlying}"
        super().__init__(msg)


def request_contacts_access(store) -> bool:
    """Synchronously request Contacts access on a CNContactStore.

    Returns True if access was granted, False otherwise. Blocks until the
    OS callback fires (or 30 s timeout, after which we assume denial).
    """
    granted = {"value": False, "error": None}
    done = threading.Event()

    def callback(ok: bool, err) -> None:  # type: ignore[no-untyped-def]
        granted["value"] = bool(ok)
        granted["error"] = err
        done.set()

    if not hasattr(store, "requestAccessForEntityType_completionHandler_"):
        raise PermissionDeniedError("CNContactStore exposes no access-request method.")

    store.requestAccessForEntityType_completionHandler_(CN_ENTITY_CONTACTS, callback)

    if not done.wait(timeout=30.0):
        logger.warning("Contacts access prompt timed out after 30s.")
        return False

    err = granted["error"]
    if err is not None:
        try:
            err_msg = str(err.localizedDescription())
        except Exception:
            err_msg = repr(err)
        logger.warning("Contacts access request returned error: %s", err_msg)

    return bool(granted["value"])


def authorization_status_label(status: int) -> str:
    """Map CNAuthorizationStatus integer to a friendly label."""
    return {
        STATUS_NOT_DETERMINED: "not determined",
        STATUS_RESTRICTED: "restricted",
        STATUS_DENIED: "denied",
        STATUS_AUTHORIZED: "authorized",
        STATUS_LIMITED: "limited",
    }.get(status, f"unknown ({status})")
