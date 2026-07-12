#!/usr/bin/env python3
"""Shared capture logic for vikunja-capture.

UI-agnostic: the Linux (GTK) and future macOS front-ends only collect the
text and hand it to this module (import it, or run it as a script).

Usage:
    capture.py "task title"   Create a task in the configured Vikunja project.
    capture.py --check        Print names of missing config variables, one per
                              line. Exits 0 if config is complete, 1 otherwise.

Exit codes:
    0  task created (or input was empty -- nothing to do)
    1  configuration incomplete
    2  sending failed -- text was appended to the local backup file

Success is completely silent (no notifications -- that's a design choice). The
one hard rule is that a *failure* must never be silent: any
failure with non-empty input appends the text to failed-captures.txt first and
returns an error message for the front-end to show. When run standalone the
error message is printed to stderr.
"""

import html
import json
import os
import sys
import urllib.error
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


def _description_html(text: str) -> str:
    """Vikunja stores the description as HTML; escape it and keep line breaks."""
    return html.escape(text).replace("\n", "<br>")


def send(config: dict, title: str, description: str = "") -> int:
    """PUT the task to Vikunja; return the HTTP status (0 = no response).

    PUT is correct here: in the Vikunja API, PUT creates a task and POST
    edits an existing one.
    """
    url = (f"{config['VIKUNJA_URL'].rstrip('/')}/api/v1/projects/"
           f"{config['VIKUNJA_PROJECT_ID']}/tasks")
    payload = {"title": title}
    if description:
        payload["description"] = _description_html(description)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method="PUT",
        headers={
            "Authorization": f"Bearer {config['VIKUNJA_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


def capture(text: str):
    """Full flow: validate, send, back up on failure.

    The first line becomes the task title and the rest (if any) the
    description. Returns (exit_code, error_message). error_message is None on
    success or empty input; otherwise a human-readable string describing the
    failure.
    """
    title, description = split_title_description(text)
    if not title:
        return 0, None

    config = load_config()
    if missing_vars(config):
        backup_text(text)
        return 1, (f"Config incomplete -- copy config.example to {CONFIG_FILE} "
                   "and fill it in. Text saved to backup.")

    status = send(config, title, description)
    if status == 201:
        return 0, None

    backup_text(text)
    message = ERROR_MESSAGES.get(status, f"HTTP {status}. Text saved to backup.")
    return 2, message


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
