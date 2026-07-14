#!/usr/bin/env python3
"""Shared capture logic for vikunja-capture.

UI-agnostic: the Linux (GTK) and future macOS front-ends only collect the
text and hand it to this module (import it, or run it as a script).

Usage:
    capture.py "task title"   Create a task in the configured Vikunja project.
    capture.py --check        Print names of missing config variables, one per
                              line. Exits 0 if config is complete, 1 otherwise.

Tags: a "#tag" token in the title (at the start or after a space) is stripped
from the title and attached to the task as a Vikunja label -- creating the
label if it doesn't exist yet. This needs a token with the label permissions
in addition to tasks:create (see README).

Exit codes:
    0  task created (or input was empty -- nothing to do)
    1  configuration incomplete
    2  not fully captured -- either sending failed (text appended to the local
       backup file) or the task was created but a label couldn't be attached

Success is completely silent (no notifications -- that's a design choice). The
one hard rule is that a *failure* must never be silent: any
failure with non-empty input appends the text to failed-captures.txt first and
returns an error message for the front-end to show. When run standalone the
error message is printed to stderr. Labels are best-effort: once the task
exists the thought is safe, so a failed label is reported but not backed up
(that would re-create the task as a duplicate).
"""

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

CONFIG_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    / "vikunja-capture"
)
CONFIG_FILE = CONFIG_DIR / "config"
BACKUP_FILE = CONFIG_DIR / "failed-captures.txt"

REQUIRED_VARS = ("VIKUNJA_URL", "VIKUNJA_PROJECT_ID", "VIKUNJA_TOKEN")

ERROR_MESSAGES = {
    0: "Vikunja unreachable (offline?). Text saved to local backup.",
    401: "Token invalid or expired (401) -- check your token. Text saved to backup.",
    403: "Token lacks the tasks:create permission (403). Text saved to backup.",
    404: "Wrong project ID or URL (404). Text saved to backup.",
}


def load_config() -> dict:
    """Parse KEY=VALUE lines; comments and blank lines are ignored."""
    config = {}
    if CONFIG_FILE.is_file():
        for line in CONFIG_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                config[key.strip()] = value.strip()
    return config


def missing_vars(config: dict) -> list:
    return [var for var in REQUIRED_VARS if not config.get(var)]


def _open_private_append(path: Path):
    """Open for append, creating with 0600 so the file is never world-readable."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return os.fdopen(fd, "a")


def backup_text(text: str) -> None:
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    with _open_private_append(BACKUP_FILE) as f:
        f.write(f"{timestamp}\t{text}\n")


def split_title_description(text: str):
    """First line is the task title; any remaining lines are the description.

    Returns (title, description), both stripped. This is where the multi-line
    convention lives so every front-end shares it: the input widget just hands
    over the raw text (newlines and all).
    """
    title, _, description = text.strip().partition("\n")
    return title.strip(), description.strip()


# A tag is a '#' at the start of the string or after whitespace, followed by
# letters/digits/'_'/'-'. The (?<!\S) lookbehind means 'C#', '#' inside URLs and
# other mid-word hashes are left alone -- only deliberate '#tag' tokens match.
_TAG_RE = re.compile(r"(?<!\S)#([\w-]+)")


def extract_tags(title: str):
    """Split '#tag' tokens out of a title.

    Returns (clean_title, tags): the title with the tags removed and whitespace
    tidied, plus the tag names in order with duplicates dropped
    case-insensitively (the first spelling wins). The tags keep their original
    casing so a freshly created label reads the way it was typed.
    """
    tags, seen = [], set()
    for match in _TAG_RE.finditer(title):
        tag = match.group(1)
        if tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    clean = _TAG_RE.sub("", title)
    clean = re.sub(r"\s{2,}", " ", clean).strip()
    return clean, tags


def _description_html(text: str) -> str:
    """Vikunja stores the description as HTML; escape it and keep line breaks."""
    return html.escape(text).replace("\n", "<br>")


def _api(config: dict, method: str, path: str, payload=None):
    """Make one authenticated Vikunja API call.

    `path` is relative to /api/v1 and must start with '/'. Returns
    (status, data): status is the HTTP status (0 on a network-level failure --
    offline, timeout) and data is the parsed JSON body, or None when there is
    no body or it isn't valid JSON.
    """
    url = f"{config['VIKUNJA_URL'].rstrip('/')}/api/v1{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {config['VIKUNJA_TOKEN']}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status, body = response.status, response.read()
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None
    try:
        return status, (json.loads(body) if body else None)
    except (json.JSONDecodeError, ValueError):
        return status, None


def send(config: dict, title: str, description: str = ""):
    """PUT the task to Vikunja; return (status, task).

    status is the HTTP status (0 = no response) and task is the created task
    dict (or None). PUT is correct here: in the Vikunja API, PUT creates a task
    and POST edits an existing one.
    """
    payload = {"title": title}
    if description:
        payload["description"] = _description_html(description)
    path = f"/projects/{config['VIKUNJA_PROJECT_ID']}/tasks"
    return _api(config, "PUT", path, payload)


def find_or_create_label(config: dict, name: str):
    """Return (label_id, error) for the label titled `name`, creating it if
    absent.

    Existing labels are matched case-insensitively so '#work' reuses a 'Work'
    label rather than spawning a near-duplicate. error is a short string on
    failure (else None), and label_id is None whenever error is set.
    """
    status, data = _api(config, "GET", f"/labels?s={urllib.parse.quote(name)}")
    if status == 200 and isinstance(data, list):
        for label in data:
            if str(label.get("title", "")).lower() == name.lower():
                return label.get("id"), None
    elif status not in (200, 404):
        return None, f"label lookup failed (HTTP {status})"

    status, data = _api(config, "PUT", "/labels", {"title": name})
    if status == 201 and isinstance(data, dict) and data.get("id"):
        return data["id"], None
    return None, f"couldn't create label (HTTP {status})"


def add_label_to_task(config: dict, task_id, label_id):
    """Attach an existing label to a task. Returns (ok, error) -- error is a
    short string on failure (else None)."""
    status, _ = _api(config, "PUT", f"/tasks/{task_id}/labels",
                     {"label_id": label_id})
    if status == 201:
        return True, None
    return False, f"attach failed (HTTP {status})"


def apply_tags(config: dict, task_id, tags: list) -> list:
    """Attach each tag to the task as a label. Returns a list of
    "tag (reason)" strings for the tags that couldn't be applied (empty list =
    all attached)."""
    failed = []
    for tag in tags:
        label_id, error = find_or_create_label(config, tag)
        if label_id is None:
            failed.append(f"{tag} ({error})")
            continue
        ok, error = add_label_to_task(config, task_id, label_id)
        if not ok:
            failed.append(f"{tag} ({error})")
    return failed


def capture(text: str):
    """Full flow: validate, send, back up on failure.

    The first line becomes the task title and the rest (if any) the
    description; any '#tag' tokens in the title become labels. Returns
    (exit_code, error_message). error_message is None on success or empty
    input; otherwise a human-readable string describing the failure.
    """
    raw_title, description = split_title_description(text)
    title, tags = extract_tags(raw_title)
    if not title:
        # The title was nothing but tags -- there's nothing to label, so keep
        # it verbatim rather than create an empty task.
        title, tags = raw_title, []
    if not title:
        return 0, None

    config = load_config()
    if missing_vars(config):
        backup_text(text)
        return 1, (f"Config incomplete -- copy config.example to {CONFIG_FILE} "
                   "and fill it in. Text saved to backup.")

    status, task = send(config, title, description)
    if status != 201:
        backup_text(text)
        message = ERROR_MESSAGES.get(status, f"HTTP {status}. Text saved to backup.")
        return 2, message

    # The task exists now, so the thought is safe. Labels are best-effort: on
    # failure we tell the user (to add them by hand) but never back up, which
    # would re-create the task as a duplicate.
    if tags:
        task_id = task.get("id") if isinstance(task, dict) else None
        if not task_id:
            return 2, ("Task created, but its id couldn't be read to add "
                       "labels -- add them manually.")
        failed = apply_tags(config, task_id, tags)
        if failed:
            return 2, (f"Task created, but couldn't add label(s): "
                       f"{', '.join(failed)}. Check the token has label "
                       "permissions, then add them manually.")
    return 0, None


def main(argv: list) -> int:
    if argv and argv[0] == "--check":
        missing = missing_vars(load_config())
        for var in missing:
            print(var)
        return 1 if missing else 0

    code, error = capture(argv[0] if argv else "")
    if error:
        print(error, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
