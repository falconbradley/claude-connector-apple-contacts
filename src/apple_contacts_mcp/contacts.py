"""Contacts.framework-backed bridge to Apple Contacts.

Wraps ``CNContactStore`` with a synchronous Python API. Unlike EventKit,
the Contacts framework is synchronous already, and hands out immutable
snapshots (``CNContact``) that are edited through ``mutableCopy()`` and
committed with a ``CNSaveRequest`` — so there is no staleness to manage:
every call fetches fresh.

All methods raise ``PermissionDeniedError`` if Contacts access has not
been granted, ``ValueError`` for bad input or missing records, and
``RuntimeError`` for other framework failures.
"""

from __future__ import annotations

import base64
import logging
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import quote

import objc  # type: ignore
import Contacts as CN  # type: ignore
from Contacts import (  # type: ignore
    CNContact,
    CNContactFetchRequest,
    CNContactFormatter,
    CNContactRelation,
    CNContactStore,
    CNContactVCardSerialization,
    CNContainer,
    CNGroup,
    CNInstantMessageAddress,
    CNLabeledValue,
    CNMutableContact,
    CNMutableGroup,
    CNMutablePostalAddress,
    CNPhoneNumber,
    CNPostalAddressFormatter,
    CNSaveRequest,
    CNSocialProfile,
)
from Foundation import (  # type: ignore
    NSCalendar,
    NSCalendarIdentifierGregorian,
    NSData,
    NSDateComponents,
)

from .models import (
    Container,
    ContactDetail,
    ContactImage,
    ContactKind,
    ContactResult,
    ContactSummary,
    ContactsStats,
    Container as _Container,  # noqa: F401  (re-export convenience)
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
    SocialProfile,
    VCardExport,
)
from . import notes as _notes
from .permissions import (
    CN_ENTITY_CONTACTS,
    GRANTED_STATUSES,
    STATUS_DENIED,
    STATUS_RESTRICTED,
    PermissionDeniedError,
    authorization_status_label,
    request_contacts_access,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants and lookup tables
# ---------------------------------------------------------------------------

# CNContactType
CN_TYPE_PERSON = 0
CN_TYPE_ORGANIZATION = 1
_KIND_TO_CN = {"person": CN_TYPE_PERSON, "organization": CN_TYPE_ORGANIZATION}
_CN_TO_KIND: dict[int, ContactKind] = {v: k for k, v in _KIND_TO_CN.items()}  # type: ignore[misc]

# CNContainerType
_CONTAINER_TYPE_LABEL = {0: "unassigned", 1: "local", 2: "exchange", 3: "cardDAV"}

# CNContactSortOrder
_SORT_TO_CN = {"default": 1, "given_name": 2, "family_name": 3}

# CNContactFormatterStyle / CNPostalAddressFormatterStyle
_FORMATTER_FULL_NAME = 0
_POSTAL_MAILING = 0

# NSDateComponentUndefined == NSIntegerMax
_UNDEFINED = 0x7FFFFFFFFFFFFFFF

_SUMMARY_KEYS: list[Any] = [
    CN.CNContactIdentifierKey,
    CN.CNContactTypeKey,
    CN.CNContactGivenNameKey,
    CN.CNContactFamilyNameKey,
    CN.CNContactMiddleNameKey,
    CN.CNContactNamePrefixKey,
    CN.CNContactNameSuffixKey,
    CN.CNContactNicknameKey,
    CN.CNContactOrganizationNameKey,
    CN.CNContactDepartmentNameKey,
    CN.CNContactJobTitleKey,
    CN.CNContactEmailAddressesKey,
    CN.CNContactPhoneNumbersKey,
    CN.CNContactUrlAddressesKey,
    CN.CNContactImageDataAvailableKey,
    CNContactFormatter.descriptorForRequiredKeysForStyle_(_FORMATTER_FULL_NAME),
]

_DETAIL_KEYS: list[Any] = _SUMMARY_KEYS + [
    CN.CNContactPreviousFamilyNameKey,
    CN.CNContactPhoneticGivenNameKey,
    CN.CNContactPhoneticMiddleNameKey,
    CN.CNContactPhoneticFamilyNameKey,
    CN.CNContactPhoneticOrganizationNameKey,
    CN.CNContactPostalAddressesKey,
    CN.CNContactSocialProfilesKey,
    CN.CNContactInstantMessageAddressesKey,
    CN.CNContactRelationsKey,
    CN.CNContactDatesKey,
    CN.CNContactBirthdayKey,
]

_IMAGE_KEYS: list[Any] = [
    CN.CNContactImageDataAvailableKey,
    CN.CNContactImageDataKey,
    CN.CNContactThumbnailImageDataKey,
]

_STATS_KEYS: list[Any] = [
    CN.CNContactIdentifierKey,
    CN.CNContactTypeKey,
    CN.CNContactEmailAddressesKey,
    CN.CNContactPhoneNumbersKey,
    CN.CNContactImageDataAvailableKey,
    CN.CNContactBirthdayKey,
]


# ---------------------------------------------------------------------------
# Labels: Apple's `_$!<Home>!$_` constants ↔ friendly strings
# ---------------------------------------------------------------------------

def _camel_to_words(s: str) -> str:
    # "HomeFax" → "home fax"; "iCloud" → "icloud"; "YoungerSister" → "younger sister"
    return re.sub(r"(?<=[a-z]{2})(?=[A-Z])", " ", s).lower()


def _build_label_tables() -> tuple[dict[str, str], dict[str, str]]:
    """Return (friendly_lower → constant, constant → friendly)."""
    to_const: dict[str, str] = {}
    to_friendly: dict[str, str] = {}
    prefixes = ("CNLabelPhoneNumber", "CNLabelEmail", "CNLabelURLAddress",
                "CNLabelDate", "CNLabelContactRelation", "CNLabel")
    for name in dir(CN):
        if not name.startswith("CNLabel"):
            continue
        const = getattr(CN, name, None)
        if not isinstance(const, str):
            continue
        try:
            friendly = str(CNLabeledValue.localizedStringForLabel_(const))
        except Exception:
            friendly = None
        # Derive an English alias from the symbol name, so the table works
        # regardless of the system locale and accepts spaced/unspaced forms.
        tail = name
        for p in prefixes:
            if name.startswith(p):
                tail = name[len(p):]
                break
        words = _camel_to_words(tail)
        aliases = {words, words.replace(" ", "")}
        if friendly:
            aliases.add(friendly.lower())
            to_friendly[const] = friendly
        else:
            to_friendly[const] = words
        for a in aliases:
            if a:
                to_const.setdefault(a, const)
    # Common spoken variants.
    extra = {
        "cell": "CNLabelPhoneNumberMobile",
        "cellphone": "CNLabelPhoneNumberMobile",
        "mobile phone": "CNLabelPhoneNumberMobile",
        "home page": "CNLabelURLAddressHomePage",
        "website": "CNLabelURLAddressHomePage",
        "fax": "CNLabelPhoneNumberWorkFax",
    }
    for alias, sym in extra.items():
        const = getattr(CN, sym, None)
        if isinstance(const, str):
            to_const.setdefault(alias, const)
    return to_const, to_friendly


_LABEL_TO_CONST, _CONST_TO_LABEL = _build_label_tables()


def _label_in(label: Optional[str]) -> Optional[str]:
    """Friendly label from the caller → Apple constant (or custom pass-through)."""
    if label is None:
        return None
    s = label.strip()
    if not s:
        return None
    if s in _CONST_TO_LABEL:          # already a constant
        return s
    return _LABEL_TO_CONST.get(s.lower(), s)


def _label_out(label: Any) -> Optional[str]:
    """Apple constant from the store → friendly label (custom labels unchanged)."""
    if label is None:
        return None
    s = str(label)
    if not s:
        return None
    if s in _CONST_TO_LABEL:
        return _CONST_TO_LABEL[s]
    if s.startswith("_$!<") and s.endswith(">!$_"):
        return s[4:-4].lower()
    return s


# ---------------------------------------------------------------------------
# Normalisation helpers (search + duplicate detection)
# ---------------------------------------------------------------------------

def _fold(s: Optional[str]) -> str:
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(s))
    stripped = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped).strip().casefold()


def _digits(s: Optional[str]) -> str:
    return re.sub(r"\D", "", s or "")


def _phone_key(s: Optional[str]) -> str:
    d = _digits(s)
    return d[-10:] if len(d) >= 10 else d


def _make_contact_link(contact_id: str) -> str:
    """Build an addressbook:// URL that opens the contact in Contacts.app."""
    return f"addressbook://{quote(contact_id, safe=':')}"


def _nserror_str(err: Any) -> str:
    if err is None:
        return "unknown error"
    try:
        desc = str(err.localizedDescription())
    except Exception:
        desc = repr(err)
    try:
        return f"{desc} ({err.domain()} code {int(err.code())})"
    except Exception:
        return desc


def _nserror_code(err: Any) -> Optional[int]:
    try:
        return int(err.code())
    except Exception:
        return None


def _bytes_of(nsdata: Any) -> bytes:
    if nsdata is None:
        return b""
    try:
        return bytes(nsdata)
    except Exception:
        return bytes(nsdata.bytes()[: nsdata.length()])


def _nsdata_of(b: bytes) -> Any:
    return NSData.dataWithBytes_length_(b, len(b))


def _sniff_mime(b: bytes) -> str:
    if b.startswith(b"\x89PNG"):
        return "image/png"
    if b.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if b.startswith(b"GIF8"):
        return "image/gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if b[4:8] == b"ftyp":
        return "image/heic"
    if b.startswith(b"II*\x00") or b.startswith(b"MM\x00*"):
        return "image/tiff"
    return "application/octet-stream"


_VCARD_MIME_TO_TYPE = {"image/png": "PNG", "image/jpeg": "JPEG", "image/gif": "GIF", "image/tiff": "TIFF"}


def _inject_vcard_photos(text: str, images: list[bytes]) -> str:
    """Add a PHOTO line to each record of a serialized vCard string.

    CNContactVCardSerialization writes every property except the photo, even
    when image data was fetched. Records come out in the order the contacts
    were passed, so `images[i]` belongs to the i-th record. Empty bytes
    leave a record untouched; formats vCard cannot label are also skipped.
    """
    sep = "\r\n" if "\r\n" in text else "\n"
    out: list[str] = []
    idx = 0
    for line in text.split(sep):
        if line.strip().upper() == "END:VCARD":
            img = images[idx] if idx < len(images) else b""
            idx += 1
            kind = _VCARD_MIME_TO_TYPE.get(_sniff_mime(img)) if img else None
            if kind:
                b64 = base64.b64encode(img).decode("ascii")
                # Fold at 75 chars per RFC 2426; continuation lines start with a space.
                head = f"PHOTO;ENCODING=b;TYPE={kind}:"
                body = head + b64
                lines = [body[:75]] + [" " + body[i:i + 74] for i in range(75, len(body), 74)]
                out.append(sep.join(lines))
        out.append(line)
    return sep.join(out)


# ---------------------------------------------------------------------------
# Conversion: framework objects ↔ Pydantic models
# ---------------------------------------------------------------------------

def _s(v: Any) -> str:
    return "" if v is None else str(v)


def _components_to_partial(comp: Any) -> Optional[PartialDate]:
    if comp is None:
        return None
    try:
        y, m, d = int(comp.year()), int(comp.month()), int(comp.day())
    except Exception:
        return None
    if m == _UNDEFINED or d == _UNDEFINED or m < 1 or d < 1:
        return None
    return PartialDate(year=None if (y == _UNDEFINED or y < 1) else y, month=m, day=d)


def _partial_to_components(pd: Optional[PartialDate]) -> Any:
    if pd is None:
        return None
    comp = NSDateComponents.alloc().init()
    comp.setCalendar_(NSCalendar.calendarWithIdentifier_(NSCalendarIdentifierGregorian))
    comp.setMonth_(pd.month)
    comp.setDay_(pd.day)
    if pd.year is not None:
        comp.setYear_(pd.year)
    return comp


def _postal_to_model(lv: Any) -> PostalAddress:
    a = lv.value()
    formatted: Optional[str] = None
    try:
        formatted = str(CNPostalAddressFormatter.stringFromPostalAddress_style_(a, _POSTAL_MAILING))
    except Exception:
        formatted = None
    return PostalAddress(
        label=_label_out(lv.label()),
        street=_s(a.street()),
        sub_locality=_s(a.subLocality()) if hasattr(a, "subLocality") else "",
        city=_s(a.city()),
        sub_administrative_area=_s(a.subAdministrativeArea()) if hasattr(a, "subAdministrativeArea") else "",
        state=_s(a.state()),
        postal_code=_s(a.postalCode()),
        country=_s(a.country()),
        iso_country_code=_s(a.ISOCountryCode()),
        formatted=formatted or None,
    )


def _model_to_postal(pa: PostalAddress) -> Any:
    a = CNMutablePostalAddress.alloc().init()
    a.setStreet_(pa.street or "")
    if hasattr(a, "setSubLocality_"):
        a.setSubLocality_(pa.sub_locality or "")
    a.setCity_(pa.city or "")
    if hasattr(a, "setSubAdministrativeArea_"):
        a.setSubAdministrativeArea_(pa.sub_administrative_area or "")
    a.setState_(pa.state or "")
    a.setPostalCode_(pa.postal_code or "")
    a.setCountry_(pa.country or "")
    a.setISOCountryCode_(pa.iso_country_code or "")
    return CNLabeledValue.labeledValueWithLabel_value_(_label_in(pa.label), a)


def _labeled_strings(values: Any, getter: Callable[[Any], str]) -> list[LabeledValue]:
    out: list[LabeledValue] = []
    for lv in values or []:
        try:
            v = getter(lv.value())
        except Exception:
            continue
        if v is None:
            continue
        out.append(LabeledValue(label=_label_out(lv.label()), value=str(v)))
    return out


def _display_name(c: Any) -> str:
    name = None
    try:
        name = CNContactFormatter.stringFromContact_style_(c, _FORMATTER_FULL_NAME)
    except Exception:
        name = None
    if name:
        return str(name)
    org = _s(c.organizationName()) if c.isKeyAvailable_(CN.CNContactOrganizationNameKey) else ""
    if org:
        return org
    if c.isKeyAvailable_(CN.CNContactEmailAddressesKey):
        emails = c.emailAddresses() or []
        if emails:
            return str(emails[0].value())
    if c.isKeyAvailable_(CN.CNContactPhoneNumbersKey):
        phones = c.phoneNumbers() or []
        if phones:
            return str(phones[0].value().stringValue())
    return "(no name)"


def _contact_to_summary(c: Any) -> ContactSummary:
    emails = c.emailAddresses() or [] if c.isKeyAvailable_(CN.CNContactEmailAddressesKey) else []
    phones = c.phoneNumbers() or [] if c.isKeyAvailable_(CN.CNContactPhoneNumbersKey) else []
    ident = str(c.identifier())
    return ContactSummary(
        id=ident,
        kind=_CN_TO_KIND.get(int(c.contactType()), "person"),
        display_name=_display_name(c),
        given_name=_s(c.givenName()),
        family_name=_s(c.familyName()),
        organization_name=_s(c.organizationName()),
        job_title=_s(c.jobTitle()),
        primary_email=str(emails[0].value()) if emails else None,
        primary_phone=str(phones[0].value().stringValue()) if phones else None,
        has_image=bool(c.imageDataAvailable()) if c.isKeyAvailable_(CN.CNContactImageDataAvailableKey) else False,
        contact_link=_make_contact_link(ident),
    )


def _contact_to_detail(c: Any) -> ContactDetail:
    summary = _contact_to_summary(c)

    socials: list[SocialProfile] = []
    for lv in c.socialProfiles() or []:
        p = lv.value()
        socials.append(SocialProfile(
            label=_label_out(lv.label()),
            service=_s(p.service()),
            username=_s(p.username()),
            url=_s(p.urlString()) or None,
            user_identifier=_s(p.userIdentifier()) or None,
        ))

    ims: list[InstantMessage] = []
    for lv in c.instantMessageAddresses() or []:
        p = lv.value()
        ims.append(InstantMessage(
            label=_label_out(lv.label()),
            service=_s(p.service()),
            username=_s(p.username()),
        ))

    dates: list[LabeledDate] = []
    for lv in c.dates() or []:
        pd = _components_to_partial(lv.value())
        if pd is not None:
            dates.append(LabeledDate(label=_label_out(lv.label()), date=pd))

    return ContactDetail(
        **summary.model_dump(),
        name_prefix=_s(c.namePrefix()),
        middle_name=_s(c.middleName()),
        name_suffix=_s(c.nameSuffix()),
        nickname=_s(c.nickname()),
        previous_family_name=_s(c.previousFamilyName()),
        phonetic_given_name=_s(c.phoneticGivenName()),
        phonetic_middle_name=_s(c.phoneticMiddleName()),
        phonetic_family_name=_s(c.phoneticFamilyName()),
        phonetic_organization_name=_s(c.phoneticOrganizationName()),
        department_name=_s(c.departmentName()),
        emails=_labeled_strings(c.emailAddresses(), lambda v: str(v)),
        phones=_labeled_strings(c.phoneNumbers(), lambda v: str(v.stringValue())),
        postal_addresses=[_postal_to_model(lv) for lv in c.postalAddresses() or []],
        urls=_labeled_strings(c.urlAddresses(), lambda v: str(v)),
        social_profiles=socials,
        instant_messages=ims,
        relations=_labeled_strings(c.contactRelations(), lambda v: str(v.name())),
        dates=dates,
        birthday=_components_to_partial(c.birthday()),
    )


def _apply_labeled(values: Optional[list[LabeledValue]], make: Callable[[str], Any]) -> list[Any]:
    out = []
    for lv in values or []:
        v = (lv.value or "").strip()
        if not v:
            continue
        out.append(CNLabeledValue.labeledValueWithLabel_value_(_label_in(lv.label), make(v)))
    return out


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

class ContactsStore:
    """Synchronous wrapper around CNContactStore."""

    def __init__(self) -> None:
        self._store = CNContactStore.alloc().init()
        self._access_granted = False
        # Whether the framework will hand us the `note` property. None =
        # not yet probed. See notes.py for why this is usually False.
        self._framework_notes: Optional[bool] = None
        self._ensure_access()

    # ------------------------------------------------------------------
    # Permission gate
    # ------------------------------------------------------------------

    def _ensure_access(self) -> None:
        if self._access_granted:
            return
        status = int(CNContactStore.authorizationStatusForEntityType_(CN_ENTITY_CONTACTS))
        logger.info("Contacts authorization status: %s (%d)", authorization_status_label(status), status)
        if status in GRANTED_STATUSES:
            self._access_granted = True
            return
        if status in (STATUS_RESTRICTED, STATUS_DENIED):
            raise PermissionDeniedError(f"Status: {authorization_status_label(status)}")
        if not request_contacts_access(self._store):
            raise PermissionDeniedError()
        self._access_granted = True

    # ------------------------------------------------------------------
    # Low-level fetch helpers
    # ------------------------------------------------------------------

    def _save(self, req: Any, what: str) -> None:
        ok, err = self._store.executeSaveRequest_error_(req, None)
        if not ok:
            raise RuntimeError(f"Could not {what}: {_nserror_str(err)}")

    def _fetch(
        self,
        keys: list[Any],
        predicate: Any = None,
        sort: str = "default",
    ) -> list[Any]:
        """Enumerate unified contacts matching `predicate` (None = all)."""
        req = CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
        if predicate is not None:
            req.setPredicate_(predicate)
        req.setUnifyResults_(True)
        req.setSortOrder_(_SORT_TO_CN.get(sort, 1))
        results: list[Any] = []

        def block(contact: Any, stop: Any) -> None:
            results.append(contact)

        try:
            ok, err = self._store.enumerateContactsWithFetchRequest_error_usingBlock_(req, None, block)
        except objc.error as exc:  # ObjC exception surfaced by PyObjC
            raise RuntimeError(f"Contacts fetch failed: {exc}") from exc
        if not ok:
            raise RuntimeError(f"Contacts fetch failed: {_nserror_str(err)}")
        # Unified results can still repeat an identifier when a predicate
        # spans containers; keep the first.
        seen: set[str] = set()
        unique: list[Any] = []
        for c in results:
            ident = str(c.identifier())
            if ident in seen:
                continue
            seen.add(ident)
            unique.append(c)
        return unique

    def _fetch_one(self, contact_id: str, keys: list[Any]) -> Any:
        c, err = self._store.unifiedContactWithIdentifier_keysToFetch_error_(contact_id, keys, None)
        if c is None:
            code = _nserror_code(err)
            if code == 200:  # CNErrorCodeRecordDoesNotExist
                raise ValueError(f"Contact not found: {contact_id}")
            raise RuntimeError(f"Could not fetch contact {contact_id}: {_nserror_str(err)}")
        return c

    def _fetch_by_ids(self, ids: Iterable[str], keys: list[Any]) -> list[Any]:
        ids = list(ids)
        if not ids:
            return []
        pred = CNContact.predicateForContactsWithIdentifiers_(ids)
        found = self._fetch(keys, pred)
        by_id = {str(c.identifier()): c for c in found}
        return [by_id[i] for i in ids if i in by_id]

    # ------------------------------------------------------------------
    # Containers
    # ------------------------------------------------------------------

    def _all_containers(self) -> list[Any]:
        conts, err = self._store.containersMatchingPredicate_error_(None, None)
        if conts is None or (err is not None and len(conts) == 0):
            raise RuntimeError(f"Could not list containers: {_nserror_str(err)}")
        return list(conts)

    def _default_container_id(self) -> Optional[str]:
        try:
            v = self._store.defaultContainerIdentifier()
            return str(v) if v else None
        except Exception:
            return None

    def _container_to_model(self, cont: Any, default_id: Optional[str]) -> Container:
        ident = str(cont.identifier())
        return Container(
            id=ident,
            name=_s(cont.name()),
            type=_CONTAINER_TYPE_LABEL.get(int(cont.type()), "unknown"),  # type: ignore[arg-type]
            is_default=(ident == default_id),
        )

    def list_containers(self) -> list[Container]:
        default_id = self._default_container_id()
        return [self._container_to_model(c, default_id) for c in self._all_containers()]

    def _container_name_map(self) -> dict[str, str]:
        return {str(c.identifier()): _s(c.name()) for c in self._all_containers()}

    def _container_of_contact(self, contact_id: str) -> Optional[Any]:
        pred = CNContainer.predicateForContainerOfContactWithIdentifier_(contact_id)
        conts, err = self._store.containersMatchingPredicate_error_(pred, None)
        if conts is None or len(conts) == 0:
            return None
        return conts[0]

    def _container_of_group(self, group_id: str) -> Optional[Any]:
        pred = CNContainer.predicateForContainerOfGroupWithIdentifier_(group_id)
        conts, err = self._store.containersMatchingPredicate_error_(pred, None)
        if conts is None or len(conts) == 0:
            return None
        return conts[0]

    def _require_container(self, container_id: Optional[str]) -> Optional[str]:
        """Validate a caller-supplied container id (None = store default)."""
        if container_id is None:
            return None
        known = {str(c.identifier()) for c in self._all_containers()}
        if container_id not in known:
            raise ValueError(f"Container not found: {container_id}")
        return container_id

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    def _all_groups(self, container_id: Optional[str] = None) -> list[Any]:
        pred = None
        if container_id is not None:
            pred = CNGroup.predicateForGroupsInContainerWithIdentifier_(container_id)
        groups, err = self._store.groupsMatchingPredicate_error_(pred, None)
        if groups is None or (err is not None and len(groups) == 0):
            raise RuntimeError(f"Could not list groups: {_nserror_str(err)}")
        return list(groups)

    def _get_group(self, group_id: str) -> Any:
        pred = CNGroup.predicateForGroupsWithIdentifiers_([group_id])
        groups, err = self._store.groupsMatchingPredicate_error_(pred, None)
        if not groups:
            raise ValueError(f"Group not found: {group_id}")
        return groups[0]

    def _group_member_ids(self, group_id: str) -> list[str]:
        pred = CNContact.predicateForContactsInGroupWithIdentifier_(group_id)
        return [str(c.identifier()) for c in self._fetch([CN.CNContactIdentifierKey], pred)]

    def _group_to_model(
        self,
        g: Any,
        container_names: dict[str, str],
        with_count: bool,
    ) -> Group:
        gid = str(g.identifier())
        cont = self._container_of_group(gid)
        cid = str(cont.identifier()) if cont is not None else None
        return Group(
            id=gid,
            name=_s(g.name()),
            container_id=cid,
            container_name=container_names.get(cid) if cid else None,
            member_count=len(self._group_member_ids(gid)) if with_count else None,
        )

    def list_groups(self, container_id: Optional[str] = None, with_counts: bool = True) -> list[Group]:
        self._require_container(container_id)
        names = self._container_name_map()
        return [self._group_to_model(g, names, with_counts) for g in self._all_groups(container_id)]

    def create_group(self, name: str, container_id: Optional[str] = None) -> GroupResult:
        if not name or not name.strip():
            raise ValueError("Group name must be a non-empty string.")
        cid = self._require_container(container_id)
        g = CNMutableGroup.alloc().init()
        g.setName_(name.strip())
        req = CNSaveRequest.alloc().init()
        req.addGroup_toContainerWithIdentifier_(g, cid)
        self._save(req, "create group")
        model = self._group_to_model(self._get_group(str(g.identifier())), self._container_name_map(), True)
        return GroupResult(group=model, success=True, member_count=0)

    def update_group(self, group_id: str, name: str) -> GroupResult:
        if not name or not name.strip():
            raise ValueError("Group name must be a non-empty string.")
        g = self._get_group(group_id).mutableCopy()
        g.setName_(name.strip())
        req = CNSaveRequest.alloc().init()
        req.updateGroup_(g)
        self._save(req, "rename group")
        model = self._group_to_model(self._get_group(group_id), self._container_name_map(), True)
        return GroupResult(group=model, success=True, member_count=model.member_count)

    def delete_group(self, group_id: str) -> GroupResult:
        g = self._get_group(group_id)
        count = len(self._group_member_ids(group_id))
        req = CNSaveRequest.alloc().init()
        req.deleteGroup_(g.mutableCopy())
        self._save(req, "delete group")
        return GroupResult(group=None, success=True, member_count=count)

    def add_to_group(self, group_id: str, contact_ids: list[str]) -> GroupResult:
        g = self._get_group(group_id)
        contacts = self._fetch_by_ids(contact_ids, [CN.CNContactIdentifierKey])
        found = {str(c.identifier()) for c in contacts}
        missing = [i for i in contact_ids if i not in found]
        if missing:
            raise ValueError(f"Contact(s) not found: {', '.join(missing)}")
        already = set(self._group_member_ids(group_id))
        req = CNSaveRequest.alloc().init()
        added = 0
        for c in contacts:
            if str(c.identifier()) in already:
                continue
            req.addMember_toGroup_(c, g)
            added += 1
        if added:
            self._save(req, "add contacts to group")
        model = self._group_to_model(self._get_group(group_id), self._container_name_map(), True)
        return GroupResult(group=model, success=True, member_count=model.member_count)

    def remove_from_group(self, group_id: str, contact_ids: list[str]) -> GroupResult:
        g = self._get_group(group_id)
        members = set(self._group_member_ids(group_id))
        targets = [i for i in contact_ids if i in members]
        if targets:
            contacts = self._fetch_by_ids(targets, [CN.CNContactIdentifierKey])
            req = CNSaveRequest.alloc().init()
            for c in contacts:
                req.removeMember_fromGroup_(c, g)
            self._save(req, "remove contacts from group")
        model = self._group_to_model(self._get_group(group_id), self._container_name_map(), True)
        return GroupResult(group=model, success=True, member_count=model.member_count)

    def _membership_map(self) -> dict[str, list[Any]]:
        """contact id → [CNGroup] across every container."""
        out: dict[str, list[Any]] = {}
        for g in self._all_groups():
            for cid in self._group_member_ids(str(g.identifier())):
                out.setdefault(cid, []).append(g)
        return out

    # ------------------------------------------------------------------
    # Contacts — read
    # ------------------------------------------------------------------

    def _scope_predicate(self, container_id: Optional[str], group_id: Optional[str]) -> Any:
        if group_id is not None:
            self._get_group(group_id)  # validates
            return CNContact.predicateForContactsInGroupWithIdentifier_(group_id)
        if container_id is not None:
            self._require_container(container_id)
            return CNContact.predicateForContactsInContainerWithIdentifier_(container_id)
        return None

    @staticmethod
    def _haystack(c: Any) -> str:
        parts = [
            c.givenName(), c.familyName(), c.middleName(), c.nickname(),
            c.namePrefix(), c.nameSuffix(),
            c.organizationName(), c.departmentName(), c.jobTitle(),
        ]
        text = " ".join(_s(p) for p in parts)
        text += " " + _display_name(c)
        for lv in c.emailAddresses() or []:
            text += " " + _s(lv.value())
        for lv in c.urlAddresses() or []:
            text += " " + _s(lv.value())
        return _fold(text)

    @staticmethod
    def _phone_haystack(c: Any) -> str:
        return " ".join(_digits(str(lv.value().stringValue())) for lv in c.phoneNumbers() or [])

    def _matches_text(self, c: Any, needle: str) -> bool:
        n = _fold(needle)
        if not n:
            return True
        if n in self._haystack(c):
            return True
        nd = _digits(needle)
        if len(nd) >= 3 and nd in self._phone_haystack(c):
            return True
        return False

    def list_contacts(
        self,
        container_id: Optional[str] = None,
        group_id: Optional[str] = None,
        kind: Optional[ContactKind] = None,
        text: Optional[str] = None,
        has_email: Optional[bool] = None,
        has_phone: Optional[bool] = None,
        has_image: Optional[bool] = None,
        sort: str = "default",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[ContactSummary]]:
        pred = self._scope_predicate(container_id, group_id)
        rows = self._fetch(_SUMMARY_KEYS, pred, sort=sort)
        if kind is not None:
            want = _KIND_TO_CN[kind]
            rows = [c for c in rows if int(c.contactType()) == want]
        if has_email is not None:
            rows = [c for c in rows if bool(c.emailAddresses()) == has_email]
        if has_phone is not None:
            rows = [c for c in rows if bool(c.phoneNumbers()) == has_phone]
        if has_image is not None:
            rows = [c for c in rows if bool(c.imageDataAvailable()) == has_image]
        if text:
            rows = [c for c in rows if self._matches_text(c, text)]
        total = len(rows)
        page = rows[offset: offset + limit]
        return total, [_contact_to_summary(c) for c in page]

    def search_contacts(
        self,
        query: str,
        container_id: Optional[str] = None,
        group_id: Optional[str] = None,
        kind: Optional[ContactKind] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, list[ContactSummary]]:
        return self.list_contacts(
            container_id=container_id, group_id=group_id, kind=kind,
            text=query, limit=limit, offset=offset,
        )

    def _attach_groups_and_container(self, detail: ContactDetail) -> ContactDetail:
        groups = self._membership_map().get(detail.id, [])
        detail.group_ids = [str(g.identifier()) for g in groups]
        detail.group_names = [_s(g.name()) for g in groups]
        cont = self._container_of_contact(detail.id)
        if cont is not None:
            detail.container_id = str(cont.identifier())
            detail.container_name = _s(cont.name())
        return detail

    def get_contact(self, contact_id: str, include_notes: bool = False) -> ContactDetail:
        c = self._fetch_one(contact_id, _DETAIL_KEYS)
        detail = self._attach_groups_and_container(_contact_to_detail(c))
        if include_notes:
            self._read_notes_into(detail)
        return detail

    def get_contact_link(self, contact_id: str) -> str:
        self._fetch_one(contact_id, [CN.CNContactIdentifierKey])
        return _make_contact_link(contact_id)

    def get_me_card(self, include_notes: bool = False) -> Optional[ContactDetail]:
        fn = getattr(self._store, "unifiedMeContactWithKeysToFetch_error_", None)
        if fn is None:
            raise RuntimeError("This macOS version exposes no 'me' card API.")
        c, err = fn(_DETAIL_KEYS, None)
        if c is None:
            if _nserror_code(err) == 200:  # record does not exist
                return None
            raise RuntimeError(f"Could not read the 'me' card: {_nserror_str(err)}")
        detail = self._attach_groups_and_container(_contact_to_detail(c))
        if include_notes:
            self._read_notes_into(detail)
        return detail

    def get_contact_image(self, contact_id: str, thumbnail: bool = False) -> Optional[ContactImage]:
        c = self._fetch_one(contact_id, _IMAGE_KEYS)
        if not c.imageDataAvailable():
            return None
        data = _bytes_of(c.thumbnailImageData() if thumbnail else c.imageData())
        if not data and thumbnail:
            data = _bytes_of(c.imageData())
        if not data:
            return None
        return ContactImage(
            contact_id=contact_id,
            is_thumbnail=thumbnail,
            mime_type=_sniff_mime(data),
            size=len(data),
            data_base64=base64.b64encode(data).decode("ascii"),
        )

    def get_stats(self) -> ContactsStats:
        rows = self._fetch(_STATS_KEYS)
        today = date.today()
        horizon = today + timedelta(days=30)
        bdays = 0
        for c in rows:
            pd = _components_to_partial(c.birthday())
            if pd is None:
                continue
            for year in (today.year, today.year + 1):
                try:
                    d = date(year, pd.month, pd.day)
                except ValueError:
                    # Feb 29 in a non-leap year: Contacts shows it on Mar 1.
                    d = date(year, 3, 1)
                if today <= d <= horizon:
                    bdays += 1
                    break
        people = sum(1 for c in rows if int(c.contactType()) == CN_TYPE_PERSON)
        return ContactsStats(
            container_count=len(self._all_containers()),
            group_count=len(self._all_groups()),
            contact_count=len(rows),
            person_count=people,
            organization_count=len(rows) - people,
            with_email=sum(1 for c in rows if c.emailAddresses()),
            with_phone=sum(1 for c in rows if c.phoneNumbers()),
            with_image=sum(1 for c in rows if c.imageDataAvailable()),
            birthdays_next_30_days=bdays,
        )

    # ------------------------------------------------------------------
    # Notes
    # ------------------------------------------------------------------

    def _probe_framework_notes(self, contact_id: str) -> Optional[str]:
        """Try the framework's `note`. Returns the note (or "") if the
        framework cooperates; None if it refused."""
        try:
            c, err = self._store.unifiedContactWithIdentifier_keysToFetch_error_(
                contact_id, [CN.CNContactNoteKey], None
            )
        except objc.error:
            self._framework_notes = False
            return None
        if c is None or not c.isKeyAvailable_(CN.CNContactNoteKey):
            self._framework_notes = False
            return None
        self._framework_notes = True
        return _s(c.note())

    def _read_notes_into(self, detail: ContactDetail) -> ContactDetail:
        if self._framework_notes is not False:
            note = self._probe_framework_notes(detail.id)
            if note is not None:
                detail.notes = note or None
                detail.notes_unavailable_reason = None
                return detail
        try:
            detail.notes = _notes.read_note(detail.id)
            detail.notes_unavailable_reason = None
        except _notes.NotesUnavailable as exc:
            detail.notes = None
            detail.notes_unavailable_reason = (
                "The Contacts framework withholds notes from unentitled processes, "
                f"and the Contacts.app fallback failed: {exc}"
            )
        except ValueError as exc:
            detail.notes = None
            detail.notes_unavailable_reason = str(exc)
        return detail

    def _write_notes(self, contact_id: str, text: Optional[str]) -> Optional[str]:
        """Write a note by whichever route works. Returns an unavailable-reason or None."""
        if self._framework_notes is None:
            self._probe_framework_notes(contact_id)
        if self._framework_notes:
            c = self._fetch_one(contact_id, [CN.CNContactNoteKey]).mutableCopy()
            c.setNote_(text or "")
            req = CNSaveRequest.alloc().init()
            req.updateContact_(c)
            self._save(req, "update note")
            return None
        try:
            _notes.write_note(contact_id, text)
            return None
        except _notes.NotesUnavailable as exc:
            return (
                "The Contacts framework withholds notes from unentitled processes, "
                f"and the Contacts.app fallback failed: {exc}"
            )

    def set_contact_notes(self, contact_id: str, notes: Optional[str]) -> ContactDetail:
        reason = self._write_notes(contact_id, notes)
        if reason is not None:
            raise RuntimeError(f"Could not write notes: {reason}")
        return self.get_contact(contact_id, include_notes=True)

    # ------------------------------------------------------------------
    # Contacts — write
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_scalars(mc: Any, fields: dict[str, Optional[str]]) -> None:
        setters = {
            "given_name": mc.setGivenName_,
            "family_name": mc.setFamilyName_,
            "middle_name": mc.setMiddleName_,
            "name_prefix": mc.setNamePrefix_,
            "name_suffix": mc.setNameSuffix_,
            "nickname": mc.setNickname_,
            "previous_family_name": mc.setPreviousFamilyName_,
            "phonetic_given_name": mc.setPhoneticGivenName_,
            "phonetic_middle_name": mc.setPhoneticMiddleName_,
            "phonetic_family_name": mc.setPhoneticFamilyName_,
            "organization_name": mc.setOrganizationName_,
            "phonetic_organization_name": mc.setPhoneticOrganizationName_,
            "department_name": mc.setDepartmentName_,
            "job_title": mc.setJobTitle_,
        }
        for key, value in fields.items():
            if value is None:
                continue
            setters[key](value)

    @staticmethod
    def _apply_lists(
        mc: Any,
        emails: Optional[list[LabeledValue]],
        phones: Optional[list[LabeledValue]],
        postal_addresses: Optional[list[PostalAddress]],
        urls: Optional[list[LabeledValue]],
        social_profiles: Optional[list[SocialProfile]],
        instant_messages: Optional[list[InstantMessage]],
        relations: Optional[list[LabeledValue]],
        dates: Optional[list[LabeledDate]],
    ) -> None:
        if emails is not None:
            mc.setEmailAddresses_(_apply_labeled(emails, lambda v: v))
        if phones is not None:
            mc.setPhoneNumbers_(_apply_labeled(phones, CNPhoneNumber.phoneNumberWithStringValue_))
        if postal_addresses is not None:
            mc.setPostalAddresses_([_model_to_postal(p) for p in postal_addresses])
        if urls is not None:
            mc.setUrlAddresses_(_apply_labeled(urls, lambda v: v))
        if social_profiles is not None:
            vals = []
            for sp in social_profiles:
                prof = CNSocialProfile.alloc().initWithUrlString_username_userIdentifier_service_(
                    sp.url or None, sp.username or "", sp.user_identifier or None, sp.service or ""
                )
                vals.append(CNLabeledValue.labeledValueWithLabel_value_(_label_in(sp.label), prof))
            mc.setSocialProfiles_(vals)
        if instant_messages is not None:
            vals = []
            for im in instant_messages:
                addr = CNInstantMessageAddress.alloc().initWithUsername_service_(im.username, im.service)
                vals.append(CNLabeledValue.labeledValueWithLabel_value_(_label_in(im.label), addr))
            mc.setInstantMessageAddresses_(vals)
        if relations is not None:
            mc.setContactRelations_(_apply_labeled(relations, CNContactRelation.contactRelationWithName_))
        if dates is not None:
            vals = []
            for ld in dates:
                vals.append(CNLabeledValue.labeledValueWithLabel_value_(
                    _label_in(ld.label), _partial_to_components(ld.date)
                ))
            mc.setDates_(vals)

    def create_contact(
        self,
        *,
        kind: ContactKind = "person",
        scalars: dict[str, Optional[str]],
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
    ) -> ContactDetail:
        cid = self._require_container(container_id)
        mc = CNMutableContact.alloc().init()
        mc.setContactType_(_KIND_TO_CN[kind])
        self._apply_scalars(mc, scalars)
        self._apply_lists(mc, emails, phones, postal_addresses, urls,
                          social_profiles, instant_messages, relations, dates)
        if birthday is not None:
            mc.setBirthday_(_partial_to_components(birthday))

        groups = [self._get_group(g) for g in (group_ids or [])]
        req = CNSaveRequest.alloc().init()
        req.addContact_toContainerWithIdentifier_(mc, cid)
        for g in groups:
            req.addMember_toGroup_(mc, g)
        self._save(req, "create contact")
        ident = str(mc.identifier())

        detail = self.get_contact(ident)
        if notes is not None and notes.strip():
            # The contact exists at this point; notes are a second step
            # with their own permission, so a failure is reported, not raised.
            reason = self._write_notes(ident, notes)
            if reason is None:
                detail.notes = notes
            else:
                detail.notes_unavailable_reason = reason
        return detail

    def update_contact(
        self,
        contact_id: str,
        *,
        kind: Optional[ContactKind] = None,
        scalars: dict[str, Optional[str]],
        emails: Optional[list[LabeledValue]] = None,
        phones: Optional[list[LabeledValue]] = None,
        postal_addresses: Optional[list[PostalAddress]] = None,
        urls: Optional[list[LabeledValue]] = None,
        social_profiles: Optional[list[SocialProfile]] = None,
        instant_messages: Optional[list[InstantMessage]] = None,
        relations: Optional[list[LabeledValue]] = None,
        dates: Optional[list[LabeledDate]] = None,
        birthday: Optional[PartialDate] = None,
        clear_birthday: bool = False,
        notes: Optional[str] = None,
        clear_notes: bool = False,
    ) -> ContactDetail:
        mc = self._fetch_one(contact_id, _DETAIL_KEYS).mutableCopy()
        if kind is not None:
            mc.setContactType_(_KIND_TO_CN[kind])
        self._apply_scalars(mc, scalars)
        self._apply_lists(mc, emails, phones, postal_addresses, urls,
                          social_profiles, instant_messages, relations, dates)
        if clear_birthday:
            mc.setBirthday_(None)
        elif birthday is not None:
            mc.setBirthday_(_partial_to_components(birthday))

        touched = (
            kind is not None or any(v is not None for v in scalars.values())
            or any(x is not None for x in (emails, phones, postal_addresses, urls,
                                           social_profiles, instant_messages, relations, dates))
            or birthday is not None or clear_birthday
        )
        if touched:
            req = CNSaveRequest.alloc().init()
            req.updateContact_(mc)
            self._save(req, "update contact")

        detail = self.get_contact(contact_id)
        if clear_notes or (notes is not None):
            text = None if clear_notes else notes
            reason = self._write_notes(contact_id, text)
            if reason is None:
                detail.notes = text or None
            else:
                detail.notes_unavailable_reason = reason
        return detail

    def delete_contact(self, contact_id: str) -> DeleteResult:
        c = self._fetch_one(contact_id, [CN.CNContactIdentifierKey])
        req = CNSaveRequest.alloc().init()
        req.deleteContact_(c.mutableCopy())
        self._save(req, "delete contact")
        return DeleteResult(id=contact_id, success=True)

    def set_contact_image(
        self,
        contact_id: str,
        image_base64: Optional[str] = None,
        image_path: Optional[str] = None,
        clear: bool = False,
    ) -> ContactDetail:
        if clear:
            data: Optional[bytes] = None
        elif image_base64:
            try:
                data = base64.b64decode(image_base64, validate=True)
            except Exception as exc:
                raise ValueError(f"image_base64 is not valid base64: {exc}") from exc
        elif image_path:
            p = Path(image_path).expanduser()
            if not p.is_file():
                raise ValueError(f"Image file not found: {image_path}")
            data = p.read_bytes()
        else:
            raise ValueError("Provide image_base64, image_path, or clear=true.")
        if data is not None and _sniff_mime(data) == "application/octet-stream":
            raise ValueError("Image data is not a recognised image format (PNG, JPEG, GIF, WebP, HEIC, TIFF).")

        mc = self._fetch_one(contact_id, _IMAGE_KEYS).mutableCopy()
        mc.setImageData_(_nsdata_of(data) if data is not None else None)
        req = CNSaveRequest.alloc().init()
        req.updateContact_(mc)
        self._save(req, "set contact image")
        return self.get_contact(contact_id)

    # ------------------------------------------------------------------
    # vCard
    # ------------------------------------------------------------------

    def export_vcards(self, contact_ids: list[str], include_images: bool = False) -> VCardExport:
        keys = [CNContactVCardSerialization.descriptorForRequiredKeys()]
        if include_images:
            keys.append(CN.CNContactImageDataKey)
        contacts = self._fetch_by_ids(contact_ids, keys)
        found = {str(c.identifier()) for c in contacts}
        missing = [i for i in contact_ids if i not in found]
        if missing:
            raise ValueError(f"Contact(s) not found: {', '.join(missing)}")
        data, err = CNContactVCardSerialization.dataWithContacts_error_(contacts, None)
        if data is None:
            raise RuntimeError(f"vCard export failed: {_nserror_str(err)}")
        text = _bytes_of(data).decode("utf-8", "replace")
        if include_images:
            text = _inject_vcard_photos(text, [_bytes_of(c.imageData()) if c.imageDataAvailable() else b""
                                               for c in contacts])
        return VCardExport(contact_count=len(contacts), vcard=text)

    def import_vcards(self, vcard: str, container_id: Optional[str] = None) -> ImportResult:
        if not vcard or "BEGIN:VCARD" not in vcard.upper():
            raise ValueError("`vcard` must contain at least one BEGIN:VCARD … END:VCARD record.")
        cid = self._require_container(container_id)
        parsed, err = CNContactVCardSerialization.contactsWithData_error_(_nsdata_of(vcard.encode("utf-8")), None)
        if parsed is None:
            raise RuntimeError(f"vCard parse failed: {_nserror_str(err)}")
        if len(parsed) == 0:
            raise ValueError("No contacts were found in the vCard data.")
        req = CNSaveRequest.alloc().init()
        mutables = []
        for c in parsed:
            mc = c.mutableCopy()
            req.addContact_toContainerWithIdentifier_(mc, cid)
            mutables.append(mc)
        self._save(req, "import vCard")
        ids = [str(m.identifier()) for m in mutables]
        rows = self._fetch_by_ids(ids, _SUMMARY_KEYS)
        return ImportResult(created_count=len(rows), contacts=[_contact_to_summary(c) for c in rows])

    # ------------------------------------------------------------------
    # Duplicates and merging
    # ------------------------------------------------------------------

    def find_duplicates(
        self,
        by: Iterable[str] = ("name", "email", "phone"),
        container_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[DuplicateCluster]:
        by = set(by)
        pred = self._scope_predicate(container_id, None)
        rows = self._fetch(_SUMMARY_KEYS, pred)
        clusters: list[DuplicateCluster] = []
        seen_pairs: set[frozenset[str]] = set()

        def emit(reason: str, key: str, members: list[Any]) -> None:
            ids = frozenset(str(c.identifier()) for c in members)
            if len(ids) < 2 or ids in seen_pairs:
                return
            seen_pairs.add(ids)
            clusters.append(DuplicateCluster(
                reason=reason, key=key,  # type: ignore[arg-type]
                contacts=[_contact_to_summary(c) for c in members],
            ))

        if "name" in by:
            buckets: dict[str, list[Any]] = {}
            for c in rows:
                if int(c.contactType()) == CN_TYPE_ORGANIZATION:
                    key = _fold(c.organizationName())
                else:
                    key = _fold(f"{_s(c.givenName())} {_s(c.familyName())}")
                    if not key:
                        key = _fold(c.organizationName())
                if key:
                    buckets.setdefault(key, []).append(c)
            for key, members in buckets.items():
                emit("same_name", key, members)

        if "email" in by:
            buckets = {}
            for c in rows:
                for lv in c.emailAddresses() or []:
                    key = _fold(lv.value())
                    if key:
                        buckets.setdefault(key, []).append(c)
            for key, members in buckets.items():
                uniq = list({str(m.identifier()): m for m in members}.values())
                emit("shared_email", key, uniq)

        if "phone" in by:
            buckets = {}
            for c in rows:
                for lv in c.phoneNumbers() or []:
                    key = _phone_key(str(lv.value().stringValue()))
                    if len(key) >= 7:
                        buckets.setdefault(key, []).append(c)
            for key, members in buckets.items():
                uniq = list({str(m.identifier()): m for m in members}.values())
                emit("shared_phone", key, uniq)

        clusters.sort(key=lambda cl: (-len(cl.contacts), cl.reason, cl.key))
        return clusters[:limit]

    def merge_contacts(
        self,
        primary_id: str,
        other_ids: list[str],
        ignore_notes: bool = False,
    ) -> MergeResult:
        other_ids = [i for i in dict.fromkeys(other_ids) if i != primary_id]
        if not other_ids:
            raise ValueError("`other_ids` must name at least one contact other than the primary.")
        keys = _DETAIL_KEYS + [CN.CNContactImageDataKey]
        primary = self._fetch_one(primary_id, keys)
        others = self._fetch_by_ids(other_ids, keys)
        found = {str(c.identifier()) for c in others}
        missing = [i for i in other_ids if i not in found]
        if missing:
            raise ValueError(f"Contact(s) not found: {', '.join(missing)}")

        # Notes first: deleting a contact loses its note, and the framework
        # will not even show us that it had one. Refuse rather than guess.
        primary_note: Optional[str] = None
        other_notes: list[str] = []
        notes_reason: Optional[str] = None
        if not ignore_notes:
            pd = ContactDetail(**_contact_to_detail(primary).model_dump())
            self._read_notes_into(pd)
            if pd.notes_unavailable_reason:
                notes_reason = pd.notes_unavailable_reason
            primary_note = pd.notes
            for c in others:
                od = ContactDetail(**_contact_to_detail(c).model_dump())
                self._read_notes_into(od)
                if od.notes_unavailable_reason:
                    notes_reason = od.notes_unavailable_reason
                elif od.notes and od.notes != primary_note:
                    other_notes.append(od.notes)
            if notes_reason:
                raise RuntimeError(
                    "Refusing to merge: the notes on one of these contacts could not be read, "
                    "and deleting it would lose them. Reason: "
                    f"{notes_reason} Pass ignore_notes=true to merge anyway."
                )

        mc = primary.mutableCopy()

        def fill(getter: str, setter: str) -> None:
            if _s(getattr(mc, getter)()):
                return
            for o in others:
                v = _s(getattr(o, getter)())
                if v:
                    getattr(mc, setter)(v)
                    return

        for g, s in (
            ("givenName", "setGivenName_"), ("familyName", "setFamilyName_"),
            ("middleName", "setMiddleName_"), ("namePrefix", "setNamePrefix_"),
            ("nameSuffix", "setNameSuffix_"), ("nickname", "setNickname_"),
            ("previousFamilyName", "setPreviousFamilyName_"),
            ("phoneticGivenName", "setPhoneticGivenName_"),
            ("phoneticMiddleName", "setPhoneticMiddleName_"),
            ("phoneticFamilyName", "setPhoneticFamilyName_"),
            ("organizationName", "setOrganizationName_"),
            ("phoneticOrganizationName", "setPhoneticOrganizationName_"),
            ("departmentName", "setDepartmentName_"), ("jobTitle", "setJobTitle_"),
        ):
            fill(g, s)

        def union(getter: str, setter: str, keyfn: Callable[[Any], str]) -> None:
            merged = list(getattr(mc, getter)() or [])
            seen = {keyfn(lv) for lv in merged}
            for o in others:
                for lv in getattr(o, getter)() or []:
                    k = keyfn(lv)
                    if k and k not in seen:
                        seen.add(k)
                        merged.append(lv)
            getattr(mc, setter)(merged)

        union("emailAddresses", "setEmailAddresses_", lambda lv: _fold(lv.value()))
        union("phoneNumbers", "setPhoneNumbers_", lambda lv: _phone_key(str(lv.value().stringValue())))
        union("urlAddresses", "setUrlAddresses_", lambda lv: _fold(lv.value()))
        union("postalAddresses", "setPostalAddresses_",
              lambda lv: _fold(f"{lv.value().street()}|{lv.value().city()}|{lv.value().postalCode()}"))
        union("socialProfiles", "setSocialProfiles_",
              lambda lv: _fold(f"{lv.value().service()}|{lv.value().username()}|{lv.value().urlString()}"))
        union("instantMessageAddresses", "setInstantMessageAddresses_",
              lambda lv: _fold(f"{lv.value().service()}|{lv.value().username()}"))
        union("contactRelations", "setContactRelations_",
              lambda lv: _fold(f"{_label_out(lv.label())}|{lv.value().name()}"))

        def date_key(lv: Any) -> str:
            pd_ = _components_to_partial(lv.value())
            return f"{_label_out(lv.label())}|{pd_.year}|{pd_.month}|{pd_.day}" if pd_ else ""
        union("dates", "setDates_", date_key)

        if mc.birthday() is None:
            for o in others:
                if o.birthday() is not None:
                    mc.setBirthday_(o.birthday())
                    break
        if not mc.imageDataAvailable():
            for o in others:
                if o.imageDataAvailable() and o.imageData() is not None:
                    mc.setImageData_(o.imageData())
                    break

        # Group memberships: the survivor joins every group any other was in.
        membership = self._membership_map()
        primary_groups = {str(g.identifier()) for g in membership.get(primary_id, [])}
        to_join: dict[str, Any] = {}
        for oid in other_ids:
            for g in membership.get(oid, []):
                gid = str(g.identifier())
                if gid not in primary_groups:
                    to_join[gid] = g

        req = CNSaveRequest.alloc().init()
        req.updateContact_(mc)
        for g in to_join.values():
            req.addMember_toGroup_(mc, g)
        for o in others:
            req.deleteContact_(o.mutableCopy())
        self._save(req, "merge contacts")

        if other_notes:
            combined = "\n\n".join(([primary_note] if primary_note else []) + other_notes)
            self._write_notes(primary_id, combined)

        detail = self.get_contact(primary_id, include_notes=not ignore_notes)
        return MergeResult(contact=detail, merged_ids=other_ids, success=True)
