# Changelog

All notable changes to this project are documented here.
This project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
