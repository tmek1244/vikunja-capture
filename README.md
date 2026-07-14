# vikunja-capture

Instant task capture for [Vikunja](https://vikunja.io): one keyboard shortcut →
a text field pops up → type your thought → it lands as a task in the Inbox
project. No UI, no friction. Organizing (labels, dates, moving to other
projects) happens later, manually, when reviewing the Inbox.

The input is a **borderless, buttonless GTK box** — no title bar, no OK/Cancel,
just a single wide box. Enter submits, Esc cancels. Press **Shift+Enter** to add
more lines: the **first line becomes the task title** and everything after it
becomes the task **description**.

Type **`#tag`** anywhere in the title to attach a label: `buy coffee #errand`
creates the task *"buy coffee"* with the **errand** label. The `#tag` is removed
from the title, and the label is created in Vikunja if it doesn't exist yet
(an existing label is reused, matched case-insensitively). See
[Tags](#tags-labels) for the extra token permission this needs.

## Requirements (Linux)

- `python3` (3.8+) — the scripts; standard library only, no pip packages
- `python3-gi` (PyGObject + GTK 4) — the input box and error dialog

```sh
# Debian/Ubuntu
sudo apt install python3 python3-gi gir1.2-gtk-4.0
```

## Setup

### 1. Create an API token in Vikunja

In the Vikunja UI: **Settings → API Tokens → Create a Token**, and grant it
**only** the `tasks:create` permission — nothing else. This is deliberate
(least privilege): if the token ever leaks, the worst anyone can do is add
junk tasks to your Inbox. In particular, do **not** add `read_all`.

If you ever build something that needs to *read* tasks, create a separate
token for it instead of widening this one.

**If you want `#tag` support**, the token needs three more permissions on top
of `tasks: create`:

- **`labels: create`** — create a label for a new tag
- **`labels: read all`** — find an existing label so tags are reused, not duplicated
- **`tasksLabels: create`** — attach a label to a task (this is a *separate*
  permission group from `labels`; without it the attach step fails with `401`)

This is a real widening of the least-privilege model above: a leaked token
could then also list your labels and create new ones (still no access to task
*contents*). If that trade-off isn't worth it to you, leave those off and just
don't use `#tag` — captures without tags keep working on a
`tasks: create`-only token.

### 2. Configure

Either just run the script — on first run it asks for the missing values in a
GTK box (the token input is masked) and saves them to
`~/.config/vikunja-capture/config` with `chmod 600` — or set it up manually:

```sh
mkdir -p ~/.config/vikunja-capture
cp config.example ~/.config/vikunja-capture/config
chmod 600 ~/.config/vikunja-capture/config
# then edit the file and fill in URL, project ID and token
```

The config file lives outside the repo and is git-ignored, as is the local
backup file. Never commit either.

### 3. Test it

```sh
./linux/vikunja-capture-gtk.py
```

Type something, press Enter, and check that the task appeared in your
**Inbox** project. This first capture also serves as verification that the
configured project ID really is the Inbox — the create-only token can't read
project names, so the script cannot check this for you.

### 4. Bind a global shortcut (GNOME)

**Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts →
"+"**, then:

- **Name:** Vikunja capture
- **Command:** `/full/path/to/vikunja-capture/linux/vikunja-capture-gtk.py`
- **Shortcut:** whatever you like, e.g. `Super+T`

The box is GTK 4, so it renders natively on GNOME Wayland (and X11) with no
extra flags. Adjust the `WIDTH` constant at the top of the script to taste.

## Behavior

- Empty input or Esc → exits quietly, sends nothing.
- **Shift+Enter** inserts a newline; the box grows as you type. The first line
  is the task title, any following lines are the description.
- Success → **completely silent**, no popup or notification. The box just
  disappears. (No system notifications are used anywhere — by design.)

### Tags (labels)

- A **`#tag`** token in the title becomes a Vikunja **label** and is removed
  from the title: `renew passport #admin #travel` → task *"renew passport"*
  with the **admin** and **travel** labels.
- A tag is a `#` at the start of the title or after a space, followed by
  letters, digits, `_` or `-`. This leaves `C#`, `C++` and `#` inside URLs
  untouched. Tags only come from the **title** line, not the description.
- Missing labels are **created**; existing ones are **reused** (matched
  case-insensitively, so `#work` finds a `Work` label). Duplicate tags in one
  capture are collapsed.
- Labels are **best-effort**: once the task is created your thought is safe, so
  if a label can't be attached (e.g. the token lacks label permissions) the
  task still lands and the error dialog names the tag to add by hand — the
  capture is **not** re-backed-up (that would duplicate the task).
- Any failure (offline, bad token, missing permission, wrong project) →
  **the text is never lost**: it is appended with a timestamp to
  `~/.config/vikunja-capture/failed-captures.txt`, and a blocking GTK error
  dialog says what went wrong. Re-sending backed-up captures is currently
  manual — open the file and re-enter them.

## Token rotation

1. Create a new token in Vikunja (again `tasks:create` only).
2. Replace `VIKUNJA_TOKEN` in `~/.config/vikunja-capture/config`.
3. Delete the old token in the Vikunja UI.

## Repo layout

```
lib/capture.py                # shared, UI-agnostic logic: config, PUT, tags/labels, errors, backup
linux/vikunja-capture-gtk.py  # Linux front-end: chromeless GTK box + first-run config setup
config.example                # config template (no real secrets)
```

The API layer (`lib/capture.py`) is separate from the input layer on purpose:
the future macOS variant only needs to collect text differently and call the
same module. Note the Vikunja API quirk it encapsulates: **`PUT` creates** a
task, `POST` edits one.
