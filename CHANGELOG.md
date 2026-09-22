# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.2] — 2026-09-22

Second live acceptance pass, this time with the live suite runnable from a
shell (see Tests in the README).

### Fixed

- **Removing contacts from iCloud / CardDAV groups now works.** On current
  macOS `CNSaveRequest.removeMember:fromGroup:` reports success and changes
  nothing for CardDAV groups, whatever record it is given — raw or unified
  member, mutable or immutable group, fresh store, after a delay. Contacts.app
  scripting removes them instantly, so the connector verifies the framework
  result and falls back to Contacts.app when the membership did not change.
  If neither route changes it, the tool now fails instead of reporting success.
- **Notes stalled after writes.** The earlier timeouts were not a missing
  Automation grant: Contacts.app holds Apple Events for 10–30 s while it syncs
  changes just made through the framework (an iCloud round trip). The
  scripting timeout is now 40 s, the fail-fast backoff after a timeout is
  30 s, and the message says to retry shortly before pointing at the
  Automation pane. Verified: notes read and write within seconds of a burst
  of iCloud writes.
- `find_duplicate_contacts` documentation now says that small clusters can be
  cut off by `limit` in a store with many real duplicates; the live test asks
  for everything.
- Contacts.app scripting no longer inherits the server's stdin.

### Changed

- Contacts.app scripting lives in `appscript.py` (notes and group removal);
  `notes.py` re-exports the notes names.

### Known limitations (observed, not fixed)

- Photos freshly set on iCloud contacts are still not read back through the
  framework's unified view; Contacts.app shows them. Unchanged from 0.1.1.

## [0.1.1] — 2026-09-22

Fixes from the first live acceptance run against a real store (iCloud,
Google, USC CardDAV accounts plus On My Mac).

### Fixed

- **`remove_contacts_from_group` did nothing** on CardDAV groups and failed
  with a Core Data error (134092) on local groups. `CNSaveRequest.removeMember`
  needs the raw member record inside the group's container, not the unified
  contact; the connector now resolves unified ids to raw members first.
- **`export_vcards(include_images=true)` crashed** with
  `CNPropertyNotFetchedException`: the image-availability key was not fetched
  alongside the image data.
- **`find_duplicate_contacts` reported each pair once**, under the first
  reason that matched, so a pair sharing both a name and an email never
  appeared as `shared_email`. De-duplication is now per reason.
- **Errors reached the client as a bare `Error executing tool <name>`.** All
  deliberate failures (bad input, record not found, framework refusal,
  permission denied) are now raised as `ToolError`, so the message —
  e.g. `Contact not found: …` — is what the caller sees.
- **Notes stalled every caller for 30 s** when Contacts.app scripting had no
  Automation grant; a merge that read several notes overran the client's
  tool timeout. The timeout is now 20 s, a timed-out call blocks further
  attempts for two minutes with an immediate, explicit reason, and the
  message names the System Settings pane to fix it.
- Instant-message and social-profile service names are mapped to Apple's
  constants on write (`jabber` → `Jabber`, `twitter` → `Twitter`), so a
  synced account normalises them predictably.

### Known limitations (observed, not fixed)

- **iCloud photos set through the framework are not read back.** After the
  CardDAV round trip, Contacts.app's own record carries the JPEG and shows
  it, but `CNContact.imageDataAvailable` on the unified contact reports
  false, so `get_contact_image` says the contact has no photo. Long-standing
  iCloud photos read fine. On My Mac photos work end to end. Under
  investigation.
- iCloud normalises some fields on sync: a social profile's label is dropped
  and IM services are rewritten (Jabber becomes `JabberInstant`). The create
  response shows what was sent; a later read shows what iCloud kept.

## [0.1.0] — 2026-09-22

Initial release.

### Added

- **Containers & groups**: `list_containers` (with default-container flag),
  `list_groups` (with container and member counts), `create_group`,
  `update_group`, `delete_group`, `add_contacts_to_group` (idempotent),
  `remove_contacts_from_group`.
- **Contact reads**: `list_contacts` (container / group / kind / text /
  has-email / has-phone / has-image filters, sort order, pagination),
  `search_contacts` (case- and accent-insensitive across names,
  organisation, emails, URLs, and phone digits), `get_contact` (every
  public property: all name parts, organisation, labeled emails / phones /
  postal addresses / URLs / social profiles / IM handles / relations /
  dates, birthday, groups, container), `get_contact_link` (`addressbook://`
  deep link), `get_me_card`, `get_contact_image` (full or thumbnail, base64
  with sniffed MIME type), `export_vcards`, `get_stats`.
- **Duplicates**: `find_duplicate_contacts` — clusters by normalised name,
  shared email, or shared phone (last 10 digits).
- **Contact writes**: `create_contact` and `update_contact` with the full
  property set and friendly labels in both directions (`home`, `work`,
  `mobile`, `iPhone`, `anniversary`, `spouse`, … or any custom text; lists
  replace, `clear_*` flags remove), `delete_contact`, `set_contact_image`
  (base64 or file path; PNG / JPEG / GIF / WebP / HEIC / TIFF),
  `import_vcards`, `merge_contacts` (fills empty scalars, unions labeled
  lists without duplicates, carries over birthday / photo / group
  memberships, appends distinct notes, deletes the rest).
- **Notes** via Contacts.app scripting (`set_contact_notes`, and
  `include_notes` / `notes` arguments elsewhere), because the framework
  withholds the `note` property from unentitled processes. Only touched
  when asked for; `notes` is null with a `notes_unavailable_reason` when
  unreadable, never an empty string. `merge_contacts` refuses to delete a
  contact whose note it could not read unless told to ignore notes.
- macOS 15 "limited access" is treated as granted: tools see the shared subset.
- CI: static tests on macOS across Python 3.11/3.13, a server-import check,
  manifest validation, and version/tool-list consistency gates.
- Release workflow: on a `v*` tag, tests, verifies tag↔manifest version,
  builds, and publishes the `.mcpb` bundles.

### Known limitations

- Notes need Automation permission (Claude → Contacts) and launch
  Contacts.app in the background; the framework route would need a signed
  binary carrying `com.apple.developer.contacts.notes`.
- Linked-card management (link / unlink unified contacts) has no public
  API and is not exposed.
- Non-Gregorian birthdays are not surfaced.
