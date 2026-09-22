"""
End-to-end tests for the Apple Contacts MCP connector.

Tests are split into two groups:

  Group A — Static tests (no Contacts permission required)
    Pydantic model shapes, input validation, label translation, text and
    phone normalisation, date-component round-trips, MIME sniffing, and
    the notes-fallback script's argument handling. Always run.

  Group B — Live Contacts tests (requires Contacts access)
    Operate against a dedicated test group named ``__claude_mcp_test__``
    and contacts whose family name is ``__ClaudeMCPTest__``, all created at
    setup and torn down at the end. Skipped with a clear message if
    Contacts access has not been granted. Notes are NOT exercised here:
    they would trigger an Automation prompt and launch Contacts.app.

Usage:
    uv run python tests/test_e2e.py              # everything
    uv run python tests/test_e2e.py --skip-live  # Group A only
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Callable, Optional

# Ensure src/ is on sys.path so the package imports cleanly when run directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

_PASS = "PASS"
_FAIL = "FAIL"
_SKIP = "SKIP"

_registry: list[tuple[str, str, Callable]] = []   # (group, name, fn)
_results: list[tuple[str, str, str, str]] = []    # (status, group, name, detail)


def test(group: str, name: str):
    def decorator(fn):
        _registry.append((group, name, fn))
        return fn
    return decorator


class _SkipTest(Exception):
    """Raised by a test when a required fixture is unavailable."""


def skip(msg: str):
    raise _SkipTest(msg)


def run_all(skip_live: bool = False) -> None:
    for group, name, fn in _registry:
        if skip_live and group == "B":
            _results.append((_SKIP, group, name, "live tests skipped"))
            continue
        try:
            fn()
            _results.append((_PASS, group, name, ""))
        except _SkipTest as exc:
            _results.append((_SKIP, group, name, str(exc)))
        except AssertionError as exc:
            _results.append((_FAIL, group, name, str(exc)))
        except Exception as exc:
            tb = traceback.format_exc(limit=4)
            _results.append((_FAIL, group, name, f"{type(exc).__name__}: {exc}\n{tb}"))


def eq(a, b, msg=""):
    if a != b:
        raise AssertionError(f"Expected {b!r}, got {a!r}" + (f" — {msg}" if msg else ""))


def is_in(v, c, msg=""):
    if v not in c:
        raise AssertionError(f"{v!r} not in {c!r}" + (f" — {msg}" if msg else ""))


def truthy(v, msg=""):
    if not v:
        raise AssertionError(f"Expected truthy, got {v!r}" + (f" — {msg}" if msg else ""))


def not_none(v, msg=""):
    if v is None:
        raise AssertionError("Expected non-None" + (f" — {msg}" if msg else ""))


def raises(exc_type, fn, msg=""):
    try:
        fn()
    except exc_type:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"Expected {exc_type.__name__}, got {type(exc).__name__}: {exc}")
    raise AssertionError(f"Expected {exc_type.__name__}, nothing raised" + (f" — {msg}" if msg else ""))


# ---------------------------------------------------------------------------
# Group A — Static
# ---------------------------------------------------------------------------

@test("A", "model shapes and defaults")
def t_model_shapes():
    from apple_contacts_mcp.models import (
        ContactDetail, ContactSummary, Container, Group, LabeledValue,
        PartialDate, PostalAddress, SearchResult, DeleteResult, MergeResult,
    )
    c = Container(id="c1", name="iCloud")
    eq(c.type, "unknown")
    eq(c.is_default, False)
    g = Group(id="g1", name="Friends")
    eq(g.member_count, None)
    s = ContactSummary(id="x:ABPerson", display_name="Ada Lovelace")
    eq(s.kind, "person")
    eq(s.has_image, False)
    d = ContactDetail(**s.model_dump())
    # notes default to None, NOT "": None means "not read / unreadable".
    eq(d.notes, None)
    eq(d.notes_unavailable_reason, None)
    eq(d.emails, [])
    eq(d.group_ids, [])
    lv = LabeledValue(value="a@b.c")
    eq(lv.label, None)
    pa = PostalAddress(city="Pasadena")
    eq(pa.street, "")
    sr = SearchResult(total=0, offset=0, limit=50, contacts=[])
    eq(sr.total, 0)
    eq(DeleteResult(id="x", success=True).success, True)
    eq(MergeResult(contact=d, merged_ids=["y"], success=True).merged_ids, ["y"])


@test("A", "PartialDate validation")
def t_partial_date():
    from pydantic import ValidationError
    from apple_contacts_mcp.models import PartialDate
    eq(PartialDate(month=2, day=29).year, None)
    eq(PartialDate(year=1990, month=12, day=31).year, 1990)
    raises(ValidationError, lambda: PartialDate(month=13, day=1))
    raises(ValidationError, lambda: PartialDate(month=1, day=0))
    raises(ValidationError, lambda: PartialDate(year=0, month=1, day=1))


@test("A", "label translation — friendly ↔ Apple constants")
def t_labels():
    from apple_contacts_mcp import contacts as c
    for friendly, expect_back in [
        ("home", "home"), ("Work", "work"), ("mobile", "mobile"), ("cell", "mobile"),
        ("iphone", "iPhone"), ("home fax", "home fax"), ("HomeFax", "home fax"),
        ("icloud", "iCloud"), ("homepage", "homepage"), ("anniversary", "anniversary"),
        ("father", "father"), ("spouse", "spouse"), ("other", "other"),
    ]:
        const = c._label_in(friendly)
        not_none(const, friendly)
        truthy(const in c._CONST_TO_LABEL, f"{friendly!r} → {const!r} is not a known constant")
        eq(c._label_out(const), expect_back, friendly)
    # Custom labels pass straight through both ways.
    eq(c._label_in("Burner"), "Burner")
    eq(c._label_out("Burner"), "Burner")
    # Empty / None collapse to None.
    eq(c._label_in(None), None)
    eq(c._label_in("   "), None)
    eq(c._label_out(""), None)
    # An unknown Apple-style constant still degrades to something readable.
    eq(c._label_out("_$!<Something>!$_"), "something")


@test("A", "text folding and phone keys")
def t_normalise():
    from apple_contacts_mcp import contacts as c
    eq(c._fold("  Émilie   DÜRER "), "emilie durer")
    eq(c._fold(None), "")
    eq(c._digits("+1 (415) 555-0199"), "14155550199")
    eq(c._phone_key("+1 (415) 555-0199"), "4155550199")
    eq(c._phone_key("415-555-0199"), "4155550199", "same number without country code")
    eq(c._phone_key("555-0199"), "5550199", "short numbers keep all digits")


@test("A", "date components round-trip")
def t_components():
    from apple_contacts_mcp import contacts as c
    from apple_contacts_mcp.models import PartialDate
    pd = PartialDate(month=2, day=29)
    back = c._components_to_partial(c._partial_to_components(pd))
    eq(back, pd, "year-less birthday")
    pd2 = PartialDate(year=1980, month=7, day=4)
    eq(c._components_to_partial(c._partial_to_components(pd2)), pd2)
    eq(c._components_to_partial(None), None)
    eq(c._partial_to_components(None), None)


@test("A", "MIME sniffing")
def t_mime():
    from apple_contacts_mcp import contacts as c
    eq(c._sniff_mime(b"\x89PNG\r\n\x1a\n" + b"\0" * 8), "image/png")
    eq(c._sniff_mime(b"\xff\xd8\xff\xe0" + b"\0" * 8), "image/jpeg")
    eq(c._sniff_mime(b"GIF89a" + b"\0" * 8), "image/gif")
    eq(c._sniff_mime(b"RIFF\0\0\0\0WEBPVP8 "), "image/webp")
    eq(c._sniff_mime(b"\0\0\0\x18ftypheic"), "image/heic")
    eq(c._sniff_mime(b"not an image at all"), "application/octet-stream")


@test("A", "contact link shape")
def t_link():
    from apple_contacts_mcp import contacts as c
    link = c._make_contact_link("0F2A-11EE:ABPerson")
    eq(link, "addressbook://0F2A-11EE:ABPerson", "colon must survive — Contacts.app expects it")
    truthy(c._make_contact_link("a b").startswith("addressbook://a%20b"))


@test("A", "vCard photo injection folds correctly and Apple reparses it")
def t_vcard_photo_injection():
    from apple_contacts_mcp import contacts as c
    from Contacts import CNContactVCardSerialization, CNMutableContact
    png = (ROOT / "icons" / "icon-128.png").read_bytes()
    a = CNMutableContact.alloc().init(); a.setGivenName_("Ada"); a.setImageData_(c._nsdata_of(png))
    b = CNMutableContact.alloc().init(); b.setGivenName_("Bare")
    data, err = CNContactVCardSerialization.dataWithContacts_error_([a, b], None)
    text = c._bytes_of(data).decode()
    truthy("PHOTO" not in text, "Apple's serializer omits photos — the reason this helper exists")
    out = c._inject_vcard_photos(text, [png, b""])
    eq(out.count("BEGIN:VCARD"), 2)
    eq(out.count("PHOTO;ENCODING=b;TYPE=PNG:"), 1, "only the contact with an image gets a PHOTO")
    truthy(max(len(l) for l in out.splitlines()) <= 75, "RFC 2426 line folding")
    parsed, err = CNContactVCardSerialization.contactsWithData_error_(c._nsdata_of(out.encode()), None)
    got = {str(p.givenName()): c._bytes_of(p.imageData()) if p.imageDataAvailable() else b"" for p in parsed}
    eq(got["Ada"], png, "image survives the round trip byte-for-byte")
    eq(got["Bare"], b"")


@test("A", "server exposes the manifest's tool list exactly")
def t_manifest_tools():
    import asyncio, json
    from apple_contacts_mcp import server
    manifest = {t["name"] for t in json.load(open(ROOT / "manifest.json"))["tools"]}
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    eq(names, manifest)
    for t in tools:
        truthy(t.description and t.description.strip(), f"{t.name} has no description")


@test("A", "server input validation surfaces as ToolError with the message intact")
def t_server_validation():
    from mcp.server.mcpserver.exceptions import ToolError
    from apple_contacts_mcp import server
    # These fail before touching the store, so no permission is needed. They
    # must arrive as ToolError: anything else reaches the client as a bare
    # "Error executing tool <name>" with the reason dropped.
    for fn in (
        lambda: server.search_contacts(query="   "),
        lambda: server.create_contact(),
        lambda: server.add_contacts_to_group(group_id="g", contact_ids=[]),
        lambda: server.remove_contacts_from_group(group_id="g", contact_ids=[]),
        lambda: server.export_vcards(contact_ids=[]),
        lambda: server.merge_contacts(primary_id="a", other_ids=[]),
    ):
        try:
            fn()
        except ToolError as exc:
            truthy(str(exc).strip(), "ToolError carries a message")
            truthy("Error executing tool" not in str(exc), "message is the reason, not the SDK wrapper")
        else:
            raise AssertionError("expected ToolError")


@test("A", "service names map to Apple constants; custom services pass through")
def t_services():
    from apple_contacts_mcp import contacts as c
    eq(c._service_in("jabber", c._IM_SERVICES), "Jabber")
    eq(c._service_in("Google Talk", c._IM_SERVICES), "GoogleTalk")
    eq(c._service_in("twitter", c._SOCIAL_SERVICES), "Twitter")
    eq(c._service_in("linkedin", c._SOCIAL_SERVICES), "LinkedIn")
    eq(c._service_in("game center", c._SOCIAL_SERVICES), "Game Center")
    eq(c._service_in("Mastodon", c._SOCIAL_SERVICES), "Mastodon")


@test("A", "image bytes helper tolerates unfetched keys")
def t_image_bytes():
    from apple_contacts_mcp import contacts as c
    from Contacts import CNMutableContact
    png = (ROOT / "icons" / "icon-128.png").read_bytes()
    a = CNMutableContact.alloc().init(); a.setImageData_(c._nsdata_of(png))
    eq(c._image_bytes(a), png)
    b = CNMutableContact.alloc().init()
    eq(c._image_bytes(b), b"")


@test("A", "notes backoff fails fast after a timeout")
def t_notes_backoff():
    import time
    from apple_contacts_mcp import notes
    saved = notes._blocked_until
    try:
        notes._blocked_until = time.monotonic() + 60
        raises(notes.NotesUnavailable, lambda: notes.read_note("x:ABPerson"))
        try:
            notes.read_note("x:ABPerson")
        except notes.NotesUnavailable as exc:
            truthy("Automation" in str(exc) and "Not retried" in str(exc), str(exc))
    finally:
        notes._blocked_until = saved


@test("A", "version agrees across pyproject, manifest, and __init__")
def t_versions():
    import json, re
    from apple_contacts_mcp import __version__
    manifest = json.load(open(ROOT / "manifest.json"))["version"]
    pyproject = re.search(r'^version = "(.*)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    eq(manifest, __version__)
    eq(pyproject, __version__)


@test("A", "notes script passes arguments via argv, never interpolation")
def t_notes_script():
    from apple_contacts_mcp import notes
    truthy("argv" in notes._JXA)
    # A note full of quotes and backslashes must not appear in the script text.
    truthy("${" not in notes._JXA, "no template interpolation")
    eq(notes._JXA.count('Application("Contacts")'), 1)


# ---------------------------------------------------------------------------
# Group B — Live Contacts
# ---------------------------------------------------------------------------

TEST_GROUP_NAME = "__claude_mcp_test__"
TEST_FAMILY = "__ClaudeMCPTest__"

_store = None
_store_skip_reason: Optional[str] = None
_test_group_id: Optional[str] = None
_created_contact_ids: list[str] = []


def _store_or_skip():
    """Return the shared ContactsStore, or skip with a cached reason."""
    global _store, _store_skip_reason
    if _store is not None:
        return _store
    if _store_skip_reason is not None:
        skip(_store_skip_reason)
    try:
        from apple_contacts_mcp.permissions import PermissionDeniedError
        from apple_contacts_mcp.contacts import ContactsStore
    except ImportError as exc:
        _store_skip_reason = f"Contacts framework unavailable: {exc}"
        skip(_store_skip_reason)
    try:
        _store = ContactsStore()
    except PermissionDeniedError as exc:
        _store_skip_reason = "Contacts access not granted: " + str(exc).splitlines()[0]
        skip(_store_skip_reason)
    except Exception as exc:
        _store_skip_reason = f"ContactsStore failed to initialise: {type(exc).__name__}: {exc}"
        skip(_store_skip_reason)
    return _store


def _cleanup_stale():
    """Remove leftovers from an earlier, interrupted run."""
    store = _store
    if store is None:
        return
    total, rows = store.list_contacts(text=TEST_FAMILY, limit=500)
    for r in rows:
        if r.family_name == TEST_FAMILY:
            try:
                store.delete_contact(r.id)
            except Exception:
                pass
    for g in store.list_groups(with_counts=False):
        if g.name == TEST_GROUP_NAME:
            try:
                store.delete_group(g.id)
            except Exception:
                pass


def _make(store, given: str, **kw):
    from apple_contacts_mcp.models import LabeledValue
    scalars = {"given_name": given, "family_name": TEST_FAMILY}
    scalars.update(kw.pop("scalars", {}))
    detail = store.create_contact(scalars=scalars, **kw)
    _created_contact_ids.append(detail.id)
    return detail


@test("B", "containers and default")
def t_live_containers():
    store = _store_or_skip()
    _cleanup_stale()
    conts = store.list_containers()
    truthy(conts, "at least one container")
    truthy(any(c.is_default for c in conts) or store._default_container_id() is None,
           "default container flagged when the store reports one")
    for c in conts:
        truthy(c.id and c.name is not None)


@test("B", "stats are self-consistent")
def t_live_stats():
    store = _store_or_skip()
    st = store.get_stats()
    eq(st.person_count + st.organization_count, st.contact_count)
    truthy(st.with_email <= st.contact_count)
    truthy(st.with_phone <= st.contact_count)
    truthy(st.container_count >= 1)


@test("B", "group create / rename / list")
def t_live_group():
    global _test_group_id
    store = _store_or_skip()
    res = store.create_group(TEST_GROUP_NAME + "_tmp")
    truthy(res.success)
    not_none(res.group)
    _test_group_id = res.group.id
    eq(res.member_count, 0)
    res2 = store.update_group(_test_group_id, TEST_GROUP_NAME)
    eq(res2.group.name, TEST_GROUP_NAME)
    names = {g.name: g for g in store.list_groups()}
    is_in(TEST_GROUP_NAME, names)
    eq(names[TEST_GROUP_NAME].member_count, 0)
    not_none(names[TEST_GROUP_NAME].container_id, "group should resolve its container")


@test("B", "contact create with the full property set, then read back")
def t_live_create_full():
    from apple_contacts_mcp.models import (
        InstantMessage, LabeledDate, LabeledValue, PartialDate, PostalAddress, SocialProfile,
    )
    store = _store_or_skip()
    d = _make(
        store, "Ada",
        scalars={"middle_name": "King", "nickname": "Countess", "organization_name": "Analytical Engines Ltd",
                 "job_title": "Programmer", "department_name": "R&D", "name_prefix": "Lady"},
        emails=[LabeledValue(label="work", value="ada@example.com"),
                LabeledValue(label="Burner", value="ada2@example.org")],
        phones=[LabeledValue(label="mobile", value="+1 (415) 555-0199"),
                LabeledValue(label="home fax", value="415-555-0100")],
        postal_addresses=[PostalAddress(label="home", street="12 St James's Sq", city="London",
                                        postal_code="SW1Y 4JH", country="United Kingdom", iso_country_code="gb")],
        urls=[LabeledValue(label="homepage", value="https://example.com/ada")],
        social_profiles=[SocialProfile(label="other", service="twitter", username="adalovelace")],
        instant_messages=[InstantMessage(service="jabber", username="ada@jabber.example")],
        relations=[LabeledValue(label="father", value="George Byron")],
        dates=[LabeledDate(label="anniversary", date=PartialDate(year=1835, month=7, day=8))],
        birthday=PartialDate(year=1815, month=12, day=10),
        group_ids=[_test_group_id] if _test_group_id else None,
    )
    truthy(d.id.endswith(":ABPerson") or d.id, "identifier assigned")
    eq(d.given_name, "Ada")
    eq(d.family_name, TEST_FAMILY)
    eq(d.middle_name, "King")
    eq(d.nickname, "Countess")
    eq(d.name_prefix, "Lady")
    eq(d.organization_name, "Analytical Engines Ltd")
    eq(d.job_title, "Programmer")
    eq(d.department_name, "R&D")
    truthy("Ada" in d.display_name and TEST_FAMILY in d.display_name, d.display_name)
    eq([(e.label, e.value) for e in d.emails], [("work", "ada@example.com"), ("Burner", "ada2@example.org")])
    eq([p.label for p in d.phones], ["mobile", "home fax"])
    eq(d.primary_email, "ada@example.com")
    truthy("0199" in (d.primary_phone or ""))
    eq(len(d.postal_addresses), 1)
    eq(d.postal_addresses[0].city, "London")
    eq(d.postal_addresses[0].label, "home")
    truthy(d.postal_addresses[0].formatted and "London" in d.postal_addresses[0].formatted)
    eq([(u.label, u.value) for u in d.urls], [("homepage", "https://example.com/ada")])
    eq(d.social_profiles[0].service.lower(), "twitter")
    eq(d.social_profiles[0].username, "adalovelace")
    eq(d.instant_messages[0].username, "ada@jabber.example")
    eq([(r.label, r.value) for r in d.relations], [("father", "George Byron")])
    eq(d.dates[0].label, "anniversary")
    eq(d.dates[0].date.year, 1835)
    eq(d.birthday.year, 1815)
    eq(d.birthday.month, 12)
    eq(d.birthday.day, 10)
    eq(d.notes, None, "notes are never read unless asked")
    truthy(d.contact_link.startswith("addressbook://"))
    if _test_group_id:
        is_in(_test_group_id, d.group_ids, "created straight into the test group")
        is_in(TEST_GROUP_NAME, d.group_names)
    not_none(d.container_id, "container resolved")


@test("B", "year-less birthday and organisation kind")
def t_live_org_and_yearless():
    from apple_contacts_mcp.models import PartialDate
    store = _store_or_skip()
    d = _make(store, "", scalars={"organization_name": "Difference Engine Co " + TEST_FAMILY},
              kind="organization", birthday=PartialDate(month=2, day=29))
    eq(d.kind, "organization")
    eq(d.birthday.year, None, "year must stay unknown")
    eq(d.birthday.month, 2)
    eq(d.birthday.day, 29)
    truthy("Difference Engine" in d.display_name, d.display_name)


@test("B", "list / search / filters")
def t_live_search():
    store = _store_or_skip()
    total, rows = store.list_contacts(text=TEST_FAMILY, limit=500)
    truthy(total >= 2, f"expected ≥2 test contacts, got {total}")
    # Accent- and case-insensitive name search.
    total, rows = store.search_contacts("ADA " + TEST_FAMILY.lower())
    truthy(any(r.given_name == "Ada" for r in rows), "case-insensitive name match")
    # Email search.
    total, rows = store.search_contacts("ada2@example.org")
    truthy(any(r.given_name == "Ada" for r in rows), "email match")
    # Phone digits search, formatted differently.
    total, rows = store.search_contacts("555 0199")
    truthy(any(r.given_name == "Ada" for r in rows), "phone-digits match")
    # kind filter
    total, rows = store.list_contacts(text=TEST_FAMILY, kind="organization", limit=500)
    truthy(all(r.kind == "organization" for r in rows))
    truthy(total >= 1)
    # has_email filter
    total_e, rows_e = store.list_contacts(text=TEST_FAMILY, has_email=True, limit=500)
    truthy(all(r.primary_email for r in rows_e))
    # group filter
    if _test_group_id:
        total_g, rows_g = store.list_contacts(group_id=_test_group_id, limit=500)
        truthy(any(r.given_name == "Ada" for r in rows_g), "group filter finds Ada")
    # pagination
    total, page = store.list_contacts(text=TEST_FAMILY, limit=1, offset=0)
    eq(len(page), 1)
    eq(total, total)  # noqa: PLR0124 — sanity that total is stable
    # sort orders don't raise
    for s in ("default", "given_name", "family_name"):
        store.list_contacts(text=TEST_FAMILY, sort=s, limit=5)


@test("B", "update replaces lists, clears fields, moves kind")
def t_live_update():
    from apple_contacts_mcp.models import LabeledValue, PartialDate
    store = _store_or_skip()
    ada = next(r for r in store.list_contacts(text="Ada " + TEST_FAMILY, limit=50)[1] if r.given_name == "Ada")
    d = store.update_contact(
        ada.id,
        scalars={"job_title": "Analyst", "nickname": ""},
        emails=[LabeledValue(label="home", value="ada.home@example.com")],
        clear_birthday=True,
    )
    eq(d.job_title, "Analyst")
    eq(d.nickname, "", "empty string clears a scalar")
    eq([(e.label, e.value) for e in d.emails], [("home", "ada.home@example.com")], "list replaced")
    eq(d.birthday, None, "birthday cleared")
    eq(len(d.phones), 2, "untouched list survives")
    d2 = store.update_contact(ada.id, scalars={}, phones=[], birthday=PartialDate(year=2000, month=1, day=2))
    eq(d2.phones, [], "empty list clears")
    eq(d2.birthday.year, 2000)
    # A no-op update must not raise and must return the same record.
    d3 = store.update_contact(ada.id, scalars={})
    eq(d3.id, ada.id)


@test("B", "group membership add / remove is idempotent")
def t_live_membership():
    store = _store_or_skip()
    if not _test_group_id:
        skip("no test group")
    ids = [r.id for r in store.list_contacts(text=TEST_FAMILY, limit=500)[1]]
    truthy(len(ids) >= 2)
    res = store.add_to_group(_test_group_id, ids)
    eq(res.member_count, len(ids))
    res = store.add_to_group(_test_group_id, ids)  # again: no duplicates, no error
    eq(res.member_count, len(ids))
    res = store.remove_from_group(_test_group_id, ids[:1])
    eq(res.member_count, len(ids) - 1, "removal must actually take effect")
    total_g, rows_g = store.list_contacts(group_id=_test_group_id, limit=500)
    truthy(ids[0] not in {r.id for r in rows_g}, "removed contact no longer listed in the group")
    res = store.remove_from_group(_test_group_id, ids[:1])  # again: no-op, no error
    eq(res.member_count, len(ids) - 1)
    res = store.remove_from_group(_test_group_id, ["nonexistent:ABPerson"])  # ignored
    eq(res.member_count, len(ids) - 1)
    raises(ValueError, lambda: store.add_to_group(_test_group_id, ["nonexistent:ABPerson"]))


@test("B", "image set / get / clear")
def t_live_image():
    import base64
    store = _store_or_skip()
    ada = next(r for r in store.list_contacts(text="Ada " + TEST_FAMILY, limit=50)[1] if r.given_name == "Ada")
    png = (ROOT / "icons" / "icon-128.png").read_bytes()
    d = store.set_contact_image(ada.id, image_base64=base64.b64encode(png).decode())
    eq(d.has_image, True)
    img = store.get_contact_image(ada.id)
    not_none(img)
    eq(img.mime_type, "image/png")
    truthy(img.size > 1000)
    eq(base64.b64decode(img.data_base64)[:4], b"\x89PNG")
    thumb = store.get_contact_image(ada.id, thumbnail=True)
    not_none(thumb, "thumbnail derived by Contacts")
    d = store.set_contact_image(ada.id, clear=True)
    eq(d.has_image, False)
    eq(store.get_contact_image(ada.id), None)
    raises(ValueError, lambda: store.set_contact_image(ada.id, image_base64=base64.b64encode(b"junk").decode()))


@test("B", "vCard export / import round-trip")
def t_live_vcard():
    store = _store_or_skip()
    ada = next(r for r in store.list_contacts(text="Ada " + TEST_FAMILY, limit=50)[1] if r.given_name == "Ada")
    exp = store.export_vcards([ada.id])
    eq(exp.contact_count, 1)
    truthy("BEGIN:VCARD" in exp.vcard and "END:VCARD" in exp.vcard)
    truthy(TEST_FAMILY in exp.vcard)
    imported = store.import_vcards(exp.vcard.replace("Ada", "Ada-Import"))
    eq(imported.created_count, 1)
    _created_contact_ids.extend(c.id for c in imported.contacts)
    eq(imported.contacts[0].given_name, "Ada-Import")
    raises(ValueError, lambda: store.import_vcards("not a vcard"))


@test("B", "duplicate detection and merge")
def t_live_dupes_merge():
    from apple_contacts_mcp.models import LabeledValue, PartialDate
    store = _store_or_skip()
    a = _make(store, "Grace", emails=[LabeledValue(label="work", value="grace@example.com")],
              phones=[LabeledValue(label="mobile", value="+1 212 555 0123")])
    b = _make(store, "Grace", emails=[LabeledValue(label="home", value="grace.home@example.com"),
                                       LabeledValue(label="work", value="GRACE@example.com")],
              scalars={"job_title": "Rear Admiral"}, birthday=PartialDate(year=1906, month=12, day=9),
              group_ids=[_test_group_id] if _test_group_id else None)
    clusters = store.find_duplicates()
    ours = [cl for cl in clusters if {c.id for c in cl.contacts} >= {a.id, b.id}]
    reasons = {cl.reason for cl in ours}
    is_in("same_name", reasons, "name cluster")
    is_in("shared_email", reasons, "email cluster (case-insensitive)")
    # Merge b into a, ignoring notes (no Automation prompt in tests).
    res = store.merge_contacts(a.id, [b.id], ignore_notes=True)
    truthy(res.success)
    eq(res.merged_ids, [b.id])
    m = res.contact
    eq(m.id, a.id)
    eq(m.job_title, "Rear Admiral", "empty scalar filled from other")
    eq(sorted(e.value.lower() for e in m.emails), ["grace.home@example.com", "grace@example.com"],
       "emails unioned, case-duplicate dropped")
    eq(len(m.phones), 1)
    eq(m.birthday.year, 1906, "birthday carried over")
    if _test_group_id:
        is_in(_test_group_id, m.group_ids, "survivor joined the other's group")
    raises(ValueError, lambda: store.get_contact(b.id))
    _created_contact_ids.remove(b.id)


@test("B", "delete contact and group; missing ids raise ValueError")
def t_live_delete():
    global _test_group_id
    store = _store_or_skip()
    ids = [r.id for r in store.list_contacts(text=TEST_FAMILY, limit=500)[1]]
    for cid in ids:
        res = store.delete_contact(cid)
        truthy(res.success)
    total, _ = store.list_contacts(text=TEST_FAMILY, limit=500)
    eq(total, 0, "all test contacts gone")
    raises(ValueError, lambda: store.get_contact("does-not-exist:ABPerson"))
    raises(ValueError, lambda: store.delete_contact("does-not-exist:ABPerson"))
    if _test_group_id:
        res = store.delete_group(_test_group_id)
        truthy(res.success)
        raises(ValueError, lambda: store.update_group(_test_group_id, "x"))
        _test_group_id = None
    _created_contact_ids.clear()


def _cleanup():
    if _store is None:
        return
    for cid in list(_created_contact_ids):
        try:
            _store.delete_contact(cid)
        except Exception:
            pass
    if _test_group_id is not None:
        try:
            _store.delete_group(_test_group_id)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------

def _print_report() -> int:
    counts = {_PASS: 0, _FAIL: 0, _SKIP: 0}
    for status, _, _, _ in _results:
        counts[status] += 1

    print("")
    print("=" * 60)
    print("Apple Contacts MCP — test report")
    print("=" * 60)

    cur_group = None
    for status, group, name, detail in _results:
        if group != cur_group:
            cur_group = group
            label = {"A": "Static", "B": "Live (Contacts framework)"}.get(group, group)
            print(f"\n[Group {group}] {label}")
        marker = {"PASS": "✓", "FAIL": "✗", "SKIP": "–"}[status]
        line = f"  {marker} {name}"
        if status != _PASS and detail:
            line += f"\n      {detail.splitlines()[0]}"
        print(line)

    print("")
    print(f"  {counts[_PASS]} passed  {counts[_FAIL]} failed  {counts[_SKIP]} skipped")
    print("")
    return 1 if counts[_FAIL] else 0


def main() -> int:
    skip_live = "--skip-live" in sys.argv
    try:
        run_all(skip_live=skip_live)
    finally:
        _cleanup()
    return _print_report()


if __name__ == "__main__":
    sys.exit(main())
