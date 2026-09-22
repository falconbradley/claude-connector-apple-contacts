# Apple Contacts MCP

A Claude Desktop extension that gives **Claude full access to Apple Contacts** on macOS via Apple's first-class **Contacts framework**. Read, search, create, update, and delete contacts and groups — including photos, vCard import/export, and finding and merging duplicates.

Packaged as an [MCPB desktop extension](https://support.claude.com/en/articles/12922929-building-desktop-extensions-with-mcpb) with the Contacts.app icon and one-click install.

---

## What it does

| Tool | Description |
|------|-------------|
| `list_containers` | Every account (iCloud, Google, Exchange, On My Mac) with id, name, type, and default flag |
| `list_groups` | Every group with its container and member count, optionally within one container |
| `create_group` | Create a group (optional container) |
| `update_group` | Rename a group |
| `delete_group` | Delete a group — its contacts are kept |
| `add_contacts_to_group` | Add contacts to a group (idempotent) |
| `remove_contacts_from_group` | Remove contacts from a group |
| `get_stats` | Counts: containers, groups, contacts, people vs organisations, with email / phone / photo, birthdays in the next 30 days |
| `list_contacts` | Filter by container, group, kind, text, has-email / has-phone / has-image; sort; paginate |
| `search_contacts` | Free-text search across names, organisation, emails, URLs, and phone digits |
| `get_contact` | Full detail — every labeled field, birthday, groups, container, and (on request) notes |
| `get_contact_link` | `addressbook://` URL that opens the contact in Contacts.app |
| `get_me_card` | The user's own "me" card |
| `get_contact_image` | Contact photo, full or thumbnail, as base64 with MIME type |
| `export_vcards` | vCard 3.0 text for one or more contacts, optionally with photos |
| `find_duplicate_contacts` | Clusters of likely duplicates by same name, shared email, or shared phone |
| `create_contact` | Create with the full property set |
| `update_contact` | Update any subset; lists replace; `clear_*` flags remove |
| `delete_contact` | Delete a contact (destructive) |
| `set_contact_image` | Set a photo from base64 or a file path, or clear it |
| `set_contact_notes` | Set or clear the note (via Contacts.app scripting — see below) |
| `import_vcards` | Create contacts from vCard text |
| `merge_contacts` | Fold duplicates into one survivor and delete the rest (destructive) |

## How it works

Communication with Contacts happens through the **Contacts framework** (`CNContactStore`, `CNContact`, `CNMutableContact`, `CNSaveRequest`, `CNGroup`, `CNContainer`, `CNContactVCardSerialization`) via PyObjC — the same route the companion [Apple Calendar](https://github.com/falconbradley/claude-connector-apple-calendar) and [Apple Reminders](https://github.com/falconbradley/claude-connector-apple-reminders) connectors take with EventKit.

Unlike EventKit, the Contacts framework is synchronous and hands out immutable snapshots, so there is no async bridging and no cache to keep fresh: every tool call fetches from the store. Reads over a few thousand contacts take well under a second.

### Labels

Contacts stores labels as constants like `_$!<Home>!$_`. The connector translates them both ways, so tools take and return friendly strings:

| You write | Contacts.app shows |
|---|---|
| `home`, `work`, `other`, `school` | home, work, other, school |
| `mobile` / `cell`, `iPhone`, `main`, `home fax`, `work fax`, `pager` | mobile, iPhone, main, home fax, … |
| `iCloud` (emails), `homepage` / `website` (URLs) | iCloud, homepage |
| `anniversary` (dates); `spouse`, `father`, `mother`, `assistant`, `manager`, … (relations) | anniversary, spouse, … |
| anything else | your text, verbatim, as a custom label |

### Identifiers

Contact ids are `CNContact.identifier` — the same `<UUID>:ABPerson` string Contacts.app's own scripting dictionary uses — and are stable across launches. Unified contacts (cards linked across accounts) appear once, under the unified id.

---

## Notes

The `note` field is the one property this connector cannot get from the framework. Since macOS 10.15 the Contacts framework refuses to return or store `note` unless the calling process holds the `com.apple.developer.contacts.notes` entitlement, which Apple grants to signed apps on request. An unsigned Python interpreter launched by `uv run` can never carry it.

Contacts.app's **scripting dictionary**, however, exposes `note` on `person` with no such restriction. So when notes are asked for, the connector reads and writes them by scripting Contacts.app — the way the companion Apple Mail connector talks to Mail.

What that means in practice:

- Notes are **only touched when explicitly requested**: `get_contact(include_notes=true)`, `get_me_card(include_notes=true)`, `set_contact_notes`, or a `notes` argument on `create_contact` / `update_contact`. Listing and searching never read notes.
- The first such call should prompt for **Automation** permission, separate from the Contacts permission. Claude Desktop launches extension servers through a helper that makes `uv` itself the responsible process, so the entry may appear under **uv** rather than **Claude** in System Settings → Privacy & Security → Automation.
- Contacts.app answers in well under a second when idle, but for 10–30 s after changes made through the framework (an iCloud round trip after a create or update) it holds Apple Events while it syncs. The connector waits up to 40 s; on a timeout it fails fast for 30 s with a message that says to retry shortly, and names the Automation pane in case it never answers.
- It may launch Contacts.app in the background, and it is slow compared with the framework (hundreds of ms per call).
- In results, `notes` is `null` when not requested **or** when unreadable — never an empty string standing in for "unknown". When unreadable, `notes_unavailable_reason` says why.
- `merge_contacts` deletes contacts, and a deleted contact's note is gone. So it reads notes first, appends any distinct ones to the survivor, and **refuses** if a note could not be read. `ignore_notes=true` overrides that.

If the framework ever does hand over notes (for example, a future signed build with the entitlement), the connector uses it automatically and the fallback never runs.

---

## Known limitations

- **iCloud photos set through the connector are not read back.** After iCloud syncs the change, Contacts.app's own record carries the photo and displays it, but the Contacts framework's unified view reports the contact as having no image, so `get_contact_image` fails. Photos that have been on iCloud contacts for a while read fine, and On My Mac contacts work end to end. Under investigation; see the changelog.
- **iCloud normalises some fields on sync.** A social profile's label is dropped and IM service names are rewritten (Jabber becomes `JabberInstant`). The create/update response shows what was written; a later read shows what iCloud kept.
- **Removing contacts from iCloud / CardDAV groups goes through Contacts.app.** On current macOS the framework's `removeMember` reports success and changes nothing for CardDAV groups. The connector detects that and asks Contacts.app to do it, so removal needs the same Automation permission as notes on those accounts. Local (On My Mac) groups are handled by the framework alone. If neither route changes the membership, the tool fails rather than reporting success.
- `find_duplicate_contacts` returns the largest clusters first, so in a store with many real duplicates a two-contact pair can fall outside the default `limit` of 50. Raise `limit` or scope with `container_id`.
- Linked-card management (link / unlink unified contacts) and non-Gregorian birthdays have no public API and are not exposed.

---

## Requirements

- macOS 14 Sonoma or later
- Python 3.11+
- Claude Desktop with extension support
- Contacts permission granted to Claude Desktop (see below)
- Automation permission for Contacts.app — **only** for notes (see [Notes](#notes))

---

## Installation

### Option 1: Desktop Extension (recommended)

Download the latest `.mcpb` from [Releases](../../releases), then **double-click** to install.

Or build from source:

```bash
git clone https://github.com/falconbradley/claude-connector-apple-contacts.git
cd claude-connector-apple-contacts
./build.sh
```

Then double-click `dist/apple-contacts.mcpb` (or drag it into Claude Desktop).

The extension appears in **Settings > Extensions** with the Contacts icon.

### Option 2: Manual MCP config

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "apple-contacts": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/claude-connector-apple-contacts", "apple-contacts-mcp"]
    }
  }
}
```

### Permissions

The first time Claude calls a Contacts tool, macOS will prompt you to grant **Contacts** access. Click **OK**.

If the prompt doesn't appear (which can happen with unsigned interpreters launched as child processes):

1. Open **System Settings → Privacy & Security → Contacts**
2. Add Claude Desktop (or whichever process is running `uv`) and enable it
3. Quit and relaunch Claude Desktop

macOS 15's **limited access** (sharing a chosen subset of contacts) is treated as granted; the tools simply see the subset.

To verify access status, run:

```bash
sqlite3 ~/Library/Application\ Support/com.apple.TCC/TCC.db \
  "SELECT client, auth_value FROM access WHERE service='kTCCServiceAddressBook'"
```

(`auth_value` of `2` = allowed, `0` = denied.)

---

## Usage examples

Once installed, ask Claude things like:

- "Find everyone at Impulse Labs and add them to a group called Work."
- "What's Ada's mobile number?" / "Show me Ada's card."
- "Add a contact for Dr. Grace Hopper, work email grace@example.com, birthday December 9."
- "Are there any duplicate contacts?" → then "Merge those two Grace entries, keep the one with the photo."
- "Export my Family group as a vCard."
- "Whose birthdays are coming up this month?" (via `get_stats` and `list_contacts`)

---

## Development

### Project structure

```
claude-connector-apple-contacts/
├── manifest.json                    # MCPB extension manifest
├── icon.png                         # Contacts.app icon (extracted)
├── icons/                           # Multi-size icons
├── pyproject.toml                   # Python package + dependencies
├── uv.lock                          # Pinned dependency set that ships in the .mcpb
├── build.sh                         # Validate + pack build script
├── CHANGELOG.md
├── .github/workflows/               # CI (static tests, manifest gates) and release
├── tools/                           # Dev-only: icon extraction from Contacts.app
├── tests/
│   └── test_e2e.py                  # Static + live tests
└── src/
    └── apple_contacts_mcp/
        ├── __init__.py
        ├── server.py                # MCP tools (MCPServer)
        ├── contacts.py              # Contacts.framework-backed ContactsStore
        ├── notes.py                 # Notes via Contacts.app scripting
        ├── permissions.py           # TCC grant helpers
        └── models.py                # Pydantic data models
```

### Tests

```bash
# Static tests — model shapes, validation, label tables, helpers, and a
# manifest ↔ server tool-list check. No permissions needed.
uv run python tests/test_e2e.py --skip-live

# Full suite. A plain shell is denied Contacts access on macOS (the request
# returns "Access Denied" without a prompt), so run it the way Claude Desktop
# runs the server: through its disclaimer helper, which makes `uv` the
# process macOS holds responsible — the same `uv` you granted Contacts to.
/Applications/Claude.app/Contents/Helpers/disclaimer --pgroup -- \
  ~/.local/bin/uv run --project "$PWD" python tests/test_e2e.py
```

Two groups:

- **A — static.** Always runs.
- **B — live.** Operates against a dedicated `__claude_mcp_test__` group and
  contacts with the family name `__ClaudeMCPTest__`, created at setup and
  removed at the end (including leftovers from an interrupted run — so do
  not run it while other test records you care about exist). Needs
  Contacts permission for the responsible process, which the disclaimer
  invocation above provides. Notes are deliberately not exercised — they
  would trigger an Automation prompt and launch Contacts.app.

---

## Roadmap

**v1 — shipped**
- [x] Containers and groups: list, create, rename, delete, membership
- [x] Contacts: list, search, get, create, update, delete with the full property set
- [x] Photos: get (full / thumbnail), set, clear
- [x] vCard export and import
- [x] Duplicate detection and merging
- [x] Notes via Contacts.app scripting
- [x] `addressbook://` deep links, "me" card, stats

**v2 — under consideration**
- [ ] Link / unlink unified cards (no public API; would need Contacts.app scripting)
- [ ] Non-Gregorian birthdays
- [ ] Bulk operations (delete many, move many between groups)
- [ ] Framework-native notes if a signed build with the entitlement becomes practical

---

## Security & privacy

- All data stays on your Mac — this is a local MCP server. The framework reads the same store Contacts.app uses, including iCloud-synced contacts if iCloud Contacts is enabled.
- Operations are gated by macOS TCC: nothing happens until you grant Contacts access, and notes additionally need Automation permission.
- macOS-only (`"platforms": ["darwin"]` in manifest).
- The connector never reaches outside Contacts — no Calendar, Mail, or Messages data.
- Destructive operations (`delete_contact`, `delete_group`, `merge_contacts`) are explicit tools the model must choose to call; they never run as side effects of reads.

---

## Troubleshooting

**"Apple Contacts access was not granted"**
Open **System Settings → Privacy & Security → Contacts**, ensure Claude Desktop is in the list and toggled on, then restart Claude Desktop.

**Permission prompt doesn't appear**
Unsigned Python interpreters launched by Claude Desktop sometimes don't trigger the prompt automatically. Add Claude Desktop manually under **System Settings → Privacy & Security → Contacts**.

**`notes` is null with a `notes_unavailable_reason`**
Expected unless Automation permission has been granted. Enable **Claude → Contacts** under **System Settings → Privacy & Security → Automation** and try again. If Contacts.app shows a dialog, dismiss it.

**A tool failed with a message like `Contact not found: …`**
That is the intended, readable failure. If you instead see only `Error executing tool <name>`, the server hit something unexpected; the full traceback is in `~/Library/Logs/Claude/mcp-server-Apple Contacts.log`.

**A label came back as `_$!<Something>!$_`**
That is an Apple constant the connector's table does not know. It should still be displayed lowercased without the wrapper; if not, open an issue with the label text.

**Extension doesn't appear after install**
Make sure you're running a recent Claude Desktop that supports MCPB extensions. Restart Claude Desktop after installing.

---

## License

[MIT](LICENSE)
