"""
Localhost HTTP redirector so chat UIs can open contacts with one click.

Chat clients (Claude Desktop / Claude Code) block custom URL schemes like
addressbook:// in rendered links and in an MCP App's ``ui/open-link``
request, but happily open http(s) URLs in the default browser. This module
runs a tiny localhost-only HTTP server; the preview card and
get_contact_link carry an open_link like

    http://127.0.0.1:46327/open/410FE041-5C4E-48DA-B4DE-04C15EA3DBAC:ABPerson?t=<token>

Clicking it opens the browser, which hits this server, which checks the
contact still exists and hands its addressbook:// URL to macOS `open` —
Contacts.app fronts with the card selected.

The same design as the Apple Mail (port 46325) and Apple Notes (46326)
connectors' redirectors; this one defaults to 46327.

Security posture:
  - Bound to 127.0.0.1 only.
  - Every request must carry a per-install random token (persisted to
    disk so links in old chat transcripts keep working across restarts).
  - The only action is focusing Contacts.app on a card; no contact data
    is ever served over HTTP — result pages carry no names or fields.

Multiple server instances (Claude Desktop + a Claude Code session) share
the persisted port: the first instance binds it, later instances detect
the sibling via /ping and emit links pointing at the same port.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse

logger = logging.getLogger("apple_contacts_mcp.weblink")

_DEFAULT_PORT = 46327
_DEFAULT_STATE = (
    Path.home() / "Library" / "Application Support" / "apple-contacts-mcp"
    / "weblink.json"
)
_PING_BODY = b"apple-contacts-mcp-weblink"
_OPEN_PREFIX = "/open/"
# CNContact identifiers are `<UUID>:ABPerson`; accept that alphabet and
# nothing else, so a crafted path can never reach `open` as something other
# than an addressbook:// URL for a plausible id.
_CONTACT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")

_PAGE = """<!doctype html><meta charset="utf-8">
<title>{title}</title>
<body style="font-family: -apple-system, sans-serif; margin: 3em; color: #333">
<h3>{title}</h3><p>{detail}</p>{script}</body>
"""

# Clicking a link hands the URL to the browser, which opens a tab for it, so
# every click would leave a dead "Opened in Contacts" tab behind. Contacts.app
# coming to the front is the real confirmation, so on success the tab closes
# itself. Browsers only allow that for a tab with no history to go back to --
# exactly the case for a tab opened for this URL; where it is refused the
# page just stays, readable. Error pages never carry this: they are meant to
# be read. (Same as the Apple Mail connector.)
_CLOSE_SCRIPT = (
    "<script>setTimeout(function(){try{window.close();}catch(e){}},150);</script>"
)


class WebLinkServer:
    """Serves /open/<contact id> links that focus Contacts.app on a card."""

    def __init__(
        self,
        resolve_link: Callable[[str], Optional[str]],
        state_path: Optional[Path] = None,
        opener: Optional[Callable[[str], bool]] = None,
        preferred_port: int = _DEFAULT_PORT,
    ) -> None:
        """
        Args:
            resolve_link: contact id -> its addressbook:// URL, or None when no
                such contact exists. May raise for other failures (no
                Contacts permission, framework error); the page says so.
            state_path:   where the token and port persist.
            opener:       hands a URL to macOS; injectable for tests.
            preferred_port: port to try first when no state exists yet.
        """
        self._resolve_link = resolve_link
        self._opener = opener or self._open_with_macos
        self._state_path = state_path or _DEFAULT_STATE
        self._preferred_port = preferred_port
        self._lock = threading.Lock()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._started = False
        self.port: Optional[int] = None
        self.token: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open_link(self, contact_id: str) -> Optional[str]:
        """Return the clickable http:// link for a contact, or None if the
        redirector could not be started (or the id is not one it would serve)."""
        if not _CONTACT_ID_RE.fullmatch(contact_id or ""):
            return None
        if not self.ensure_started():
            return None
        return (
            f"http://127.0.0.1:{self.port}{_OPEN_PREFIX}"
            f"{quote(contact_id, safe=':')}?t={self.token}"
        )

    def ensure_started(self) -> bool:
        with self._lock:
            if self._started:
                return self.port is not None
            self._started = True
            try:
                self._start()
            except Exception:
                logger.exception("Web link redirector failed to start.")
                self.port = None
            return self.port is not None

    def shutdown(self) -> None:
        with self._lock:
            if self._httpd is not None:
                self._httpd.shutdown()
                self._httpd.server_close()
                self._httpd = None
            self._started = False

    # ------------------------------------------------------------------
    # Startup / state
    # ------------------------------------------------------------------

    def _start(self) -> None:
        state = self._load_state()
        self.token = state["token"]
        wanted_port = state.get("port", self._preferred_port)

        try:
            self._bind_and_serve(wanted_port)
        except OSError:
            if self._sibling_alive(wanted_port):
                # Another instance of this server owns the port; reuse it
                # in generated links, nothing to serve from here.
                logger.info("Web links served by sibling on port %d.", wanted_port)
                self.port = wanted_port
                return
            # Port taken by an unrelated process — fall back to ephemeral.
            self._bind_and_serve(0)

        state["port"] = self.port
        self._save_state(state)

    def _bind_and_serve(self, port: int) -> None:
        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        thread = threading.Thread(
            target=self._httpd.serve_forever, name="weblink", daemon=True
        )
        thread.start()
        logger.info("Web link redirector listening on 127.0.0.1:%d", self.port)

    def _sibling_alive(self, port: int) -> bool:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/ping?t={self.token}", timeout=1.0
            ) as resp:
                return resp.read(64).strip() == _PING_BODY
        except OSError:
            return False

    def _load_state(self) -> dict:
        try:
            state = json.loads(self._state_path.read_text())
            if isinstance(state.get("token"), str) and state["token"]:
                return state
        except (OSError, ValueError):
            pass
        return {"token": secrets.token_urlsafe(16), "port": self._preferred_port}

    def _save_state(self, state: dict) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state))
        except OSError as exc:
            logger.warning("Could not persist weblink state: %s", exc)

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    @staticmethod
    def _open_with_macos(url: str) -> bool:
        proc = subprocess.run(
            ["open", url], capture_output=True, stdin=subprocess.DEVNULL, timeout=15
        )
        return proc.returncode == 0

    def _make_handler(self) -> type:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args) -> None:
                logger.debug("weblink: " + fmt, *args)

            def _reply(
                self, status: int, title: str, detail: str = "",
                self_close: bool = False,
            ) -> None:
                body = _PAGE.format(
                    title=title,
                    detail=detail,
                    script=_CLOSE_SCRIPT if self_close else "",
                ).encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 (http.server API)
                parsed = urlparse(self.path)
                token = (parse_qs(parsed.query).get("t") or [""])[0]
                if not (server.token and hmac.compare_digest(token, server.token)):
                    self._reply(403, "Forbidden")
                    return

                if parsed.path == "/ping":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(_PING_BODY)))
                    self.end_headers()
                    self.wfile.write(_PING_BODY)
                    return

                if parsed.path.startswith(_OPEN_PREFIX):
                    contact_id = unquote(parsed.path[len(_OPEN_PREFIX):])
                    if _CONTACT_ID_RE.fullmatch(contact_id):
                        self._serve_contact(contact_id)
                        return

                self._reply(404, "Not found")

            def _serve_contact(self, contact_id: str) -> None:
                try:
                    link = server._resolve_link(contact_id)
                except Exception:
                    logger.exception("weblink: lookup failed for %s", contact_id)
                    self._reply(
                        500,
                        "Could not look up the contact",
                        "Apple Contacts did not answer. Check that Claude still has "
                        "Contacts access (System Settings ▸ Privacy &amp; Security "
                        "▸ Contacts) and try again.",
                    )
                    return
                if not link:
                    self._reply(
                        404,
                        "Contact not found",
                        "This contact no longer exists — it may have been deleted "
                        "or merged into another card.",
                    )
                    return
                if server._opener(link):
                    self._reply(
                        200,
                        "Opened in Contacts",
                        "The card should now be front-most in Contacts.app. "
                        "You can close this tab.",
                        self_close=True,
                    )
                else:
                    self._reply(
                        500,
                        "Could not open Contacts",
                        f'Try this link directly: <a href="{link}">{link}</a>',
                    )

        return Handler
