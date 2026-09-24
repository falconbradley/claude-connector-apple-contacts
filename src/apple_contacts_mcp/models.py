"""Pydantic models for Apple Contacts MCP server."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Containers (accounts) and groups
# ---------------------------------------------------------------------------

ContainerType = Literal["local", "exchange", "cardDAV", "unassigned", "unknown"]


class Container(BaseModel):
    """A CNContainer — one per account (iCloud, Google, Exchange, On My Mac)."""
    id: str                                   # CNContainer.identifier
    name: str
    type: ContainerType = "unknown"
    is_default: bool = False                  # where new contacts land when no container is given


class Group(BaseModel):
    """A CNGroup — a named set of contacts within one container."""
    id: str                                   # CNGroup.identifier
    name: str
    container_id: Optional[str] = None
    container_name: Optional[str] = None
    member_count: Optional[int] = None        # None when counts were not requested


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

class LabeledValue(BaseModel):
    """A labeled string — an email, phone number, URL, or relation name.

    `label` is a friendly name: "home", "work", "mobile", "iPhone", "main",
    "other", "iCloud", "homepage", or any custom text. Apple's internal
    `_$!<Home>!$_` constants are translated in both directions.
    """
    label: Optional[str] = None
    value: str


class PostalAddress(BaseModel):
    label: Optional[str] = None
    street: str = ""                          # may contain newlines
    sub_locality: str = ""                    # neighbourhood / district
    city: str = ""
    sub_administrative_area: str = ""         # county
    state: str = ""
    postal_code: str = ""
    country: str = ""
    iso_country_code: str = ""                # e.g. "us"
    formatted: Optional[str] = None           # read-only: system-formatted mailing label


class SocialProfile(BaseModel):
    label: Optional[str] = None
    service: str                              # "twitter", "linkedin", "facebook", … or custom
    username: str = ""
    url: Optional[str] = None
    user_identifier: Optional[str] = None


class InstantMessage(BaseModel):
    label: Optional[str] = None
    service: str                              # "jabber", "skype", … or custom
    username: str


class PartialDate(BaseModel):
    """A calendar date whose year may be unknown (Contacts allows year-less birthdays)."""
    year: Optional[int] = Field(default=None, ge=1)
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)


class LabeledDate(BaseModel):
    label: Optional[str] = None               # "anniversary", "other", or custom
    date: PartialDate


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

ContactKind = Literal["person", "organization"]


class ContactSummary(BaseModel):
    id: str                                   # CNContact.identifier (unified)
    kind: ContactKind = "person"
    display_name: str                         # system-formatted full name (or org name)
    given_name: str = ""
    family_name: str = ""
    organization_name: str = ""
    job_title: str = ""
    primary_email: Optional[str] = None
    primary_phone: Optional[str] = None
    has_image: bool = False
    contact_link: Optional[str] = None        # addressbook:// URL


class ContactDetail(ContactSummary):
    """Full contact with every public Contacts-framework property."""
    name_prefix: str = ""
    middle_name: str = ""
    name_suffix: str = ""
    nickname: str = ""
    previous_family_name: str = ""
    phonetic_given_name: str = ""
    phonetic_middle_name: str = ""
    phonetic_family_name: str = ""
    phonetic_organization_name: str = ""
    department_name: str = ""

    emails: list[LabeledValue] = []
    phones: list[LabeledValue] = []
    postal_addresses: list[PostalAddress] = []
    urls: list[LabeledValue] = []
    social_profiles: list[SocialProfile] = []
    instant_messages: list[InstantMessage] = []
    relations: list[LabeledValue] = []        # value = the related person's name
    dates: list[LabeledDate] = []
    birthday: Optional[PartialDate] = None

    # Notes. Apple gates the `note` property behind an entitlement
    # (com.apple.developer.contacts.notes) that an unsigned interpreter
    # cannot hold, so the framework refuses it. The connector then falls
    # back to scripting Contacts.app, which has its own permission prompt
    # and is only attempted when notes are explicitly requested. None means
    # "not read" or "could not be read" — never "empty". See
    # notes_unavailable_reason for which.
    notes: Optional[str] = None
    notes_unavailable_reason: Optional[str] = None

    group_ids: list[str] = []
    group_names: list[str] = []
    container_id: Optional[str] = None
    container_name: Optional[str] = None


class ContactImage(BaseModel):
    contact_id: str
    is_thumbnail: bool
    mime_type: str
    size: int
    data_base64: str


# ---------------------------------------------------------------------------
# Result envelopes
# ---------------------------------------------------------------------------

class SearchResult(BaseModel):
    total: int
    offset: int
    limit: int
    contacts: list[ContactSummary]


class ContactsStats(BaseModel):
    container_count: int
    group_count: int
    contact_count: int
    person_count: int
    organization_count: int
    with_email: int
    with_phone: int
    with_image: int
    birthdays_next_30_days: int


class GroupResult(BaseModel):
    """Returned by create/update/delete group and membership operations."""
    group: Optional[Group] = None
    success: bool
    member_count: Optional[int] = None        # after the operation, when known


class ContactResult(BaseModel):
    """Returned by create/update/image/notes operations."""
    contact: ContactDetail
    success: bool


class DeleteResult(BaseModel):
    id: str
    success: bool


class VCardExport(BaseModel):
    contact_count: int
    vcard: str                                # one or more vCard 3.0 records, concatenated


class ImportResult(BaseModel):
    created_count: int
    contacts: list[ContactSummary]


DuplicateReason = Literal["same_name", "shared_email", "shared_phone"]


class DuplicateCluster(BaseModel):
    reason: DuplicateReason
    key: str                                  # the normalised name / email / phone that matched
    contacts: list[ContactSummary]


class MergeResult(BaseModel):
    contact: ContactDetail                    # the surviving, merged contact
    merged_ids: list[str]                     # ids that were folded in and deleted
    success: bool


class OtherCard(BaseModel):
    """Another card that looks like the same person, noted on the preview card.

    `linked` cards are joined to the previewed one in Contacts.app's unified
    view, so their fields already show on it. Unlinked ones are separate
    cards with the same name — typically one person saved in two accounts,
    such as iCloud and Google — and `only_here` / `only_there` list the
    emails and phone numbers that differ between the two.
    """
    id: str
    display_name: str
    container_id: Optional[str] = None
    container_name: Optional[str] = None
    linked: bool = False
    only_here: list[str] = []                 # on the previewed card, not this one
    only_there: list[str] = []                # on this card, not the previewed one
    open_link: Optional[str] = None           # clickable localhost link (unlinked cards)
