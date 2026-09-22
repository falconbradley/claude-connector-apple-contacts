"""
Apple Contacts MCP Server
=========================
Exposes Apple Contacts to Claude Desktop via the Model Context Protocol.
Uses Apple's first-class Contacts framework (``CNContactStore``, via PyObjC)
for full coverage of the contact data model: names, organisations, labeled
emails / phones / addresses / URLs / social profiles / IM handles /
relations / dates, birthdays, images, groups, containers, and vCard.

Permission model
----------------
Contacts access is gated by macOS TCC. On first tool invocation the OS
will prompt the user to grant access; alternatively the user can pre-grant
under System Settings → Privacy & Security → Contacts for the parent
process (Claude Desktop).

Notes are the one property the framework withholds from unentitled
processes; the connector reads and writes them by scripting Contacts.app
instead, which needs a separate Automation permission and is only done
when a caller asks for notes explicitly. See notes.py.

Tools provided
--------------
Containers & groups
  list_containers            - Every account (iCloud, Google, Exchange, On My Mac)
  list_groups                - Every group, with container and member count
  create_group               - Create a group
  update_group               - Rename a group
  delete_group               - Delete a group (its contacts are kept)
  add_contacts_to_group      - Add contacts to a group (idempotent)
  remove_contacts_from_group - Remove contacts from a group

Contacts — read
  get_stats                  - Counts: contacts, people, orgs, with email/phone/image, birthdays soon
  list_contacts              - Filtered, paginated listing
  search_contacts            - Free-text search across names, org, emails, phones, URLs
  get_contact                - Full detail for one contact (optionally with notes)
  get_contact_link           - addressbook:// URL that opens the contact in Contacts.app
  get_me_card                - The user's own "me" card
  get_contact_image          - Contact photo (full or thumbnail) as base64
  export_vcards              - vCard 3.0 text for one or more contacts
  find_duplicate_contacts    - Clusters of likely duplicates by name, email, or phone

Contacts — write
  create_contact             - Create with the full property set
  update_contact             - Update any subset; lists replace; clear_* flags remove
  delete_contact             - Delete a contact (destructive)
  set_contact_image          - Set or clear the contact photo
  set_contact_notes          - Set or clear the note (via Contacts.app scripting)
  import_vcards              - Create contacts from vCard text
  merge_contacts             - Fold duplicates into one survivor and delete the rest
"""

from __future__ import annotations

import logging
import sys
from typing import Literal, Optional

from mcp.server import MCPServer

from . import __version__
from .models import (
    ContactDetail,
    ContactImage,
    ContactKind,
    ContactResult,
    ContactsStats,
    Container,
    DeleteResult,
    DuplicateCluster,
    Group,
    GroupResult,
    ImportResult,
    InstantMessage,
    LabeledDate,
    LabeledValue,
    MergeResult,
    PartialDate,
    PostalAddress,
    SearchResult,
    SocialProfile,
    VCardExport,
)
from .permissions import PermissionDeniedError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("apple_contacts_mcp")

# ---------------------------------------------------------------------------
# Lazy-initialised store. Framework bootstrap (and a possible TCC prompt) can
# take a few seconds — we MUST NOT run it at import time, the MCP client
# would time out waiting for the initialize response.
# ---------------------------------------------------------------------------

_store = None  # type: ignore[var-annotated]


# ---------------------------------------------------------------------------
# MCP server app
# ---------------------------------------------------------------------------

mcp = MCPServer(
    "Apple Contacts",
    instructions=(
        "Access to Apple Contacts on this Mac via the Contacts framework. "
        "You can list accounts (containers) and groups; search, read, create, "
        "update, and delete contacts with every field Contacts.app shows; "
        "manage group membership; read and set contact photos; import and "
        "export vCards; and find and merge duplicate contacts. Contact ids are "
        "stable and shared with Contacts.app. Notes are only read when asked "
        "for (include_notes=true) because they go through a separate "
        "Automation permission."
    ),
    version=__version__,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_store():
    """Return the ContactsStore, initialising on first call.

    Re-attempts on every call if init previously failed (the user may have
    granted Contacts access since the last attempt).
    """
    global _store
    if _store is not None:
        return _store
    from .contacts import ContactsStore  # heavy import deferred to first use
    try:
        _store = ContactsStore()
        logger.info("Apple Contacts MCP ready (Contacts.framework).")
        return _store
    except PermissionDeniedError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Could not initialise the Contacts store: {exc}") from exc


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), 500))


SortOrder = Literal["default", "given_name", "family_name"]
DuplicateBy = Literal["name", "email", "phone"]


# ---------------------------------------------------------------------------
# Tools — containers & groups
# ---------------------------------------------------------------------------

@mcp.tool()
def list_containers() -> list[Container]:
    """List every contacts account (container) on this Mac.

    One container per account: iCloud, Google, Exchange, "On My Mac", etc.
    `is_default` marks where new contacts land when no container is given.
    Use the returned ids with tools that take a `container_id`.
    """
    return _require_store().list_containers()


@mcp.tool()
def list_groups(container_id: Optional[str] = None, with_counts: bool = True) -> list[Group]:
    """List contact groups, optionally within one container.

    Args:
        container_id: Restrict to this container; null = all containers.
        with_counts:  Include member_count per group (one extra fetch per group).
    """
    return _require_store().list_groups(container_id=container_id, with_counts=with_counts)


@mcp.tool()
def create_group(name: str, container_id: Optional[str] = None) -> GroupResult:
    """Create a new contact group.

    Args:
        name:         Non-empty group name.
        container_id: Container to host the group; null = the default container.
    """
    return _require_store().create_group(name=name, container_id=container_id)


@mcp.tool()
def update_group(group_id: str, name: str) -> GroupResult:
    """Rename a contact group.

    Args:
        group_id: Identifier from list_groups.
        name:     New non-empty name.
    """
    return _require_store().update_group(group_id=group_id, name=name)


@mcp.tool()
def delete_group(group_id: str) -> GroupResult:
    """Delete a contact group. The contacts in it are NOT deleted.

    The returned `member_count` is how many contacts were in the group at
    the moment of deletion.

    Args:
        group_id: Identifier from list_groups.
    """
    return _require_store().delete_group(group_id)


@mcp.tool()
def add_contacts_to_group(group_id: str, contact_ids: list[str]) -> GroupResult:
    """Add contacts to a group. Contacts already in the group are skipped, so
    calling this twice is safe.

    Args:
        group_id:    Identifier from list_groups.
        contact_ids: Contact identifiers to add.
    """
    if not contact_ids:
        raise ValueError("`contact_ids` must contain at least one id.")
    return _require_store().add_to_group(group_id, contact_ids)


@mcp.tool()
def remove_contacts_from_group(group_id: str, contact_ids: list[str]) -> GroupResult:
    """Remove contacts from a group. Ids not in the group are ignored.

    Args:
        group_id:    Identifier from list_groups.
        contact_ids: Contact identifiers to remove.
    """
    if not contact_ids:
        raise ValueError("`contact_ids` must contain at least one id.")
    return _require_store().remove_from_group(group_id, contact_ids)


# ---------------------------------------------------------------------------
# Tools — contacts (read)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_stats() -> ContactsStats:
    """Return aggregate counts: containers, groups, contacts, people vs
    organisations, how many have an email / phone / photo, and birthdays in
    the next 30 days."""
    return _require_store().get_stats()


@mcp.tool()
def list_contacts(
    container_id: Optional[str] = None,
    group_id: Optional[str] = None,
    kind: Optional[ContactKind] = None,
    text: Optional[str] = None,
    has_email: Optional[bool] = None,
    has_phone: Optional[bool] = None,
    has_image: Optional[bool] = None,
    sort: SortOrder = "default",
    limit: int = 50,
    offset: int = 0,
) -> SearchResult:
    """List contacts with optional filters and pagination.

    Args:
        container_id: Only contacts in this container (account).
        group_id:     Only members of this group (takes precedence over container_id).
        kind:         "person" or "organization"; null = both.
        text:         Case- and accent-insensitive substring match across names,
                      nickname, organisation, department, job title, emails, URLs,
                      and phone digits.
        has_email:    true = only contacts with an email; false = only without.
        has_phone:    Same for phone numbers.
        has_image:    Same for a contact photo.
        sort:         "default" (the user's Contacts.app setting), "given_name", or "family_name".
        limit:        Max results per page (default 50, max 500).
        offset:       Pagination offset.
    """
    limit = _clamp_limit(limit)
    total, rows = _require_store().list_contacts(
        container_id=container_id, group_id=group_id, kind=kind, text=text,
        has_email=has_email, has_phone=has_phone, has_image=has_image,
        sort=sort, limit=limit, offset=offset,
    )
    return SearchResult(total=total, offset=offset, limit=limit, contacts=rows)


@mcp.tool()
def search_contacts(
    query: str,
    container_id: Optional[str] = None,
    group_id: Optional[str] = None,
    kind: Optional[ContactKind] = None,
    limit: int = 50,
    offset: int = 0,
) -> SearchResult:
    """Search contacts by free text.

    Matches case- and accent-insensitively across names, nickname,
    organisation, department, job title, emails, and URLs. A query with three
    or more digits also matches phone numbers by digit sequence, so
    "555 0199" finds "+1 (415) 555-0199".

    Args:
        query:        Required, non-empty.
        container_id: Restrict to this container.
        group_id:     Restrict to this group.
        kind:         "person" or "organization"; null = both.
        limit:        Max results (default 50, max 500).
        offset:       Pagination offset.
    """
    if not query or not query.strip():
        raise ValueError("`query` must be a non-empty string.")
    limit = _clamp_limit(limit)
    total, rows = _require_store().search_contacts(
        query=query.strip(), container_id=container_id, group_id=group_id,
        kind=kind, limit=limit, offset=offset,
    )
    return SearchResult(total=total, offset=offset, limit=limit, contacts=rows)


@mcp.tool()
def get_contact(contact_id: str, include_notes: bool = False) -> ContactDetail:
    """Fetch one contact with every property.

    Includes all labeled values (emails, phones, postal addresses, URLs,
    social profiles, IM handles, relations, dates), birthday, group
    memberships, and container.

    Notes are only fetched when `include_notes` is true: the Contacts
    framework withholds them from unentitled processes, so they come from
    Contacts.app scripting, which has its own permission prompt and
    launches Contacts.app in the background. `notes` is null (not empty)
    when not requested or when unreadable, with the reason in
    `notes_unavailable_reason`.

    Args:
        contact_id:    Identifier from list_contacts / search_contacts.
        include_notes: Also read the note field (see above).
    """
    return _require_store().get_contact(contact_id, include_notes=include_notes)


@mcp.tool()
def get_contact_link(contact_id: str) -> dict:
    """Return an addressbook:// URL that opens the contact in Contacts.app.

    Args:
        contact_id: Identifier from list_contacts / search_contacts.
    """
    link = _require_store().get_contact_link(contact_id)
    return {"contact_id": contact_id, "contact_link": link}


@mcp.tool()
def get_me_card(include_notes: bool = False) -> ContactDetail:
    """Return the user's own contact card (the one marked "me" in Contacts.app).

    Raises if no card is designated as "me".

    Args:
        include_notes: Also read the note field (see get_contact).
    """
    me = _require_store().get_me_card(include_notes=include_notes)
    if me is None:
        raise ValueError("No contact is designated as the 'me' card in Contacts.app.")
    return me


@mcp.tool()
def get_contact_image(contact_id: str, thumbnail: bool = False) -> ContactImage:
    """Return the contact's photo as base64, with its MIME type.

    Args:
        contact_id: Identifier from list_contacts / search_contacts.
        thumbnail:  true = the small square thumbnail Contacts keeps; false = full image.
    """
    img = _require_store().get_contact_image(contact_id, thumbnail=thumbnail)
    if img is None:
        raise ValueError(f"Contact {contact_id} has no photo.")
    return img


@mcp.tool()
def export_vcards(contact_ids: list[str], include_images: bool = False) -> VCardExport:
    """Export one or more contacts as vCard 3.0 text.

    Args:
        contact_ids:    Identifiers to export.
        include_images: Embed contact photos (can make the output large).
    """
    if not contact_ids:
        raise ValueError("`contact_ids` must contain at least one id.")
    return _require_store().export_vcards(contact_ids, include_images=include_images)


@mcp.tool()
def find_duplicate_contacts(
    by: Optional[list[DuplicateBy]] = None,
    container_id: Optional[str] = None,
    limit: int = 50,
) -> list[DuplicateCluster]:
    """Find clusters of likely duplicate contacts.

    Each cluster names the reason it matched and the normalised key:
      - same_name:     identical given+family name (or organisation name),
                       ignoring case, accents, and spacing
      - shared_email:  an email address that appears on more than one contact
      - shared_phone:  a phone number (last 10 digits) on more than one contact

    Nothing is changed; pass the ids to merge_contacts to act on a cluster.

    Args:
        by:           Which signals to use; default all three.
        container_id: Restrict to one container.
        limit:        Max clusters to return, largest first (default 50).
    """
    return _require_store().find_duplicates(
        by=by or ["name", "email", "phone"], container_id=container_id, limit=max(1, int(limit)),
    )


# ---------------------------------------------------------------------------
# Tools — contacts (write)
# ---------------------------------------------------------------------------

@mcp.tool()
def create_contact(
    given_name: Optional[str] = None,
    family_name: Optional[str] = None,
    middle_name: Optional[str] = None,
    name_prefix: Optional[str] = None,
    name_suffix: Optional[str] = None,
    nickname: Optional[str] = None,
    organization_name: Optional[str] = None,
    department_name: Optional[str] = None,
    job_title: Optional[str] = None,
    kind: ContactKind = "person",
    emails: Optional[list[LabeledValue]] = None,
    phones: Optional[list[LabeledValue]] = None,
    postal_addresses: Optional[list[PostalAddress]] = None,
    urls: Optional[list[LabeledValue]] = None,
    social_profiles: Optional[list[SocialProfile]] = None,
    instant_messages: Optional[list[InstantMessage]] = None,
    relations: Optional[list[LabeledValue]] = None,
    dates: Optional[list[LabeledDate]] = None,
    birthday: Optional[PartialDate] = None,
    notes: Optional[str] = None,
    container_id: Optional[str] = None,
    group_ids: Optional[list[str]] = None,
    previous_family_name: Optional[str] = None,
    phonetic_given_name: Optional[str] = None,
    phonetic_middle_name: Optional[str] = None,
    phonetic_family_name: Optional[str] = None,
    phonetic_organization_name: Optional[str] = None,
) -> ContactResult:
    """Create a new contact.

    At least one of given_name, family_name, or organization_name is required.
    Labels on emails/phones/urls/relations/dates are friendly strings —
    "home", "work", "mobile", "iPhone", "main", "other", "iCloud", "homepage",
    "anniversary", "father", … — or any custom text.

    Args:
        given_name … job_title: Name and organisation fields.
        kind:              "person" (default) or "organization".
        emails:            [{label, value}] email addresses.
        phones:            [{label, value}] phone numbers, any formatting.
        postal_addresses:  [{label, street, city, state, postal_code, country, …}].
        urls:              [{label, value}] web addresses.
        social_profiles:   [{label, service, username, url, user_identifier}].
        instant_messages:  [{label, service, username}].
        relations:         [{label, value}] where value is the related person's name
                           and label is e.g. "spouse", "mother", "assistant".
        dates:             [{label, date: {year?, month, day}}] e.g. anniversaries.
        birthday:          {year?, month, day}; year may be omitted.
        notes:             Free-text note. Written via Contacts.app scripting after
                           the contact is created; if that step fails the contact
                           still exists and `notes_unavailable_reason` says why.
        container_id:      Account to create in; null = the default container.
        group_ids:         Groups to add the new contact to.
        previous_family_name, phonetic_*: Rarely used name fields.
    """
    scalars = {
        "given_name": given_name, "family_name": family_name, "middle_name": middle_name,
        "name_prefix": name_prefix, "name_suffix": name_suffix, "nickname": nickname,
        "previous_family_name": previous_family_name,
        "phonetic_given_name": phonetic_given_name, "phonetic_middle_name": phonetic_middle_name,
        "phonetic_family_name": phonetic_family_name,
        "organization_name": organization_name, "phonetic_organization_name": phonetic_organization_name,
        "department_name": department_name, "job_title": job_title,
    }
    if not any((v or "").strip() for v in (given_name, family_name, organization_name)):
        raise ValueError("Provide at least one of given_name, family_name, organization_name.")
    detail = _require_store().create_contact(
        kind=kind, scalars=scalars, emails=emails, phones=phones,
        postal_addresses=postal_addresses, urls=urls, social_profiles=social_profiles,
        instant_messages=instant_messages, relations=relations, dates=dates,
        birthday=birthday, notes=notes, container_id=container_id, group_ids=group_ids,
    )
    return ContactResult(contact=detail, success=True)


@mcp.tool()
def update_contact(
    contact_id: str,
    given_name: Optional[str] = None,
    family_name: Optional[str] = None,
    middle_name: Optional[str] = None,
    name_prefix: Optional[str] = None,
    name_suffix: Optional[str] = None,
    nickname: Optional[str] = None,
    organization_name: Optional[str] = None,
    department_name: Optional[str] = None,
    job_title: Optional[str] = None,
    kind: Optional[ContactKind] = None,
    emails: Optional[list[LabeledValue]] = None,
    phones: Optional[list[LabeledValue]] = None,
    postal_addresses: Optional[list[PostalAddress]] = None,
    urls: Optional[list[LabeledValue]] = None,
    social_profiles: Optional[list[SocialProfile]] = None,
    instant_messages: Optional[list[InstantMessage]] = None,
    relations: Optional[list[LabeledValue]] = None,
    dates: Optional[list[LabeledDate]] = None,
    birthday: Optional[PartialDate] = None,
    notes: Optional[str] = None,
    clear_birthday: bool = False,
    clear_notes: bool = False,
    previous_family_name: Optional[str] = None,
    phonetic_given_name: Optional[str] = None,
    phonetic_middle_name: Optional[str] = None,
    phonetic_family_name: Optional[str] = None,
    phonetic_organization_name: Optional[str] = None,
) -> ContactResult:
    """Update any subset of a contact's properties.

    Omitted fields are left untouched. Scalar fields (names, organisation,
    job title, …) are replaced by the value given; pass "" to clear one.
    List fields (emails, phones, addresses, …) REPLACE the whole list when
    given — read the contact first, then send the full desired list; pass []
    to clear a list. `clear_birthday` and `clear_notes` remove those fields.

    Args:
        contact_id: Identifier of the contact to update.
        (others):   See create_contact for shapes and label conventions.
    """
    scalars = {
        "given_name": given_name, "family_name": family_name, "middle_name": middle_name,
        "name_prefix": name_prefix, "name_suffix": name_suffix, "nickname": nickname,
        "previous_family_name": previous_family_name,
        "phonetic_given_name": phonetic_given_name, "phonetic_middle_name": phonetic_middle_name,
        "phonetic_family_name": phonetic_family_name,
        "organization_name": organization_name, "phonetic_organization_name": phonetic_organization_name,
        "department_name": department_name, "job_title": job_title,
    }
    detail = _require_store().update_contact(
        contact_id, kind=kind, scalars=scalars, emails=emails, phones=phones,
        postal_addresses=postal_addresses, urls=urls, social_profiles=social_profiles,
        instant_messages=instant_messages, relations=relations, dates=dates,
        birthday=birthday, clear_birthday=clear_birthday, notes=notes, clear_notes=clear_notes,
    )
    return ContactResult(contact=detail, success=True)


@mcp.tool()
def delete_contact(contact_id: str) -> DeleteResult:
    """Delete a contact. DESTRUCTIVE and not undoable from here.

    Args:
        contact_id: Identifier of the contact to delete.
    """
    return _require_store().delete_contact(contact_id)


@mcp.tool()
def set_contact_image(
    contact_id: str,
    image_base64: Optional[str] = None,
    image_path: Optional[str] = None,
    clear: bool = False,
) -> ContactResult:
    """Set or clear a contact's photo.

    Provide exactly one of image_base64 or image_path, or clear=true.
    PNG, JPEG, GIF, WebP, HEIC, and TIFF are accepted.

    Args:
        contact_id:   Identifier of the contact.
        image_base64: Image bytes, base64-encoded.
        image_path:   Path to an image file on this Mac.
        clear:        true removes the current photo.
    """
    detail = _require_store().set_contact_image(
        contact_id, image_base64=image_base64, image_path=image_path, clear=clear,
    )
    return ContactResult(contact=detail, success=True)


@mcp.tool()
def set_contact_notes(contact_id: str, notes: Optional[str] = None) -> ContactResult:
    """Set (or clear, with null / "") the free-text note on a contact.

    Notes go through Contacts.app scripting because the framework withholds
    them from unentitled processes. The first call prompts for Automation
    permission (Claude → Contacts) and may launch Contacts.app in the
    background. The returned contact includes the note as read back.

    Args:
        contact_id: Identifier of the contact.
        notes:      New note text; null or "" clears it.
    """
    detail = _require_store().set_contact_notes(contact_id, notes)
    return ContactResult(contact=detail, success=True)


@mcp.tool()
def import_vcards(vcard: str, container_id: Optional[str] = None) -> ImportResult:
    """Create contacts from vCard text (one or many records).

    Args:
        vcard:        vCard 3.0/4.0 text containing BEGIN:VCARD … END:VCARD records.
        container_id: Account to create in; null = the default container.
    """
    return _require_store().import_vcards(vcard, container_id=container_id)


@mcp.tool()
def merge_contacts(
    primary_id: str,
    other_ids: list[str],
    ignore_notes: bool = False,
) -> MergeResult:
    """Merge duplicate contacts into one. DESTRUCTIVE: the others are deleted.

    The primary contact survives. Every empty scalar field on it is filled
    from the others (first non-empty wins); labeled lists (emails, phones,
    addresses, URLs, profiles, relations, dates) are unioned without
    duplicates; a missing birthday or photo is taken from the others; and
    the survivor joins every group any of the others belonged to.

    Notes: the framework cannot show whether a contact has a note, so the
    merge first reads notes via Contacts.app (Automation permission) and
    appends any distinct notes to the primary's. If notes cannot be read it
    REFUSES rather than silently discarding them; pass ignore_notes=true to
    merge anyway.

    Args:
        primary_id:   The contact to keep.
        other_ids:    Contacts to fold in and delete.
        ignore_notes: Proceed even if notes cannot be read (they will be lost).
    """
    if not other_ids:
        raise ValueError("`other_ids` must contain at least one id.")
    return _require_store().merge_contacts(primary_id, other_ids, ignore_notes=ignore_notes)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
