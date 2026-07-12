#!/usr/bin/env python3
"""Linux front-end: a chromeless GTK input box, handing the text to lib/capture.

A borderless, buttonless GTK 4 window with a single wide multi-line box:

    Enter        -> submit
    Shift+Enter  -> new line (first line = task title, the rest = description)
    Esc          -> cancel (sends nothing)

The UI-agnostic logic (config, PUT, error handling, backup) lives in
lib/capture.py; this file only collects the text. On first run it also asks
for any missing config values (token masked) and saves them to
~/.config/vikunja-capture/config with mode 0600.

Bind it to a global keyboard shortcut.
"""

import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import capture  # noqa: E402

WIDTH = 700  # entry box width in px

# var -> (prompt label, mask input?)
SETUP_PROMPTS = {
    "VIKUNJA_URL": ("Vikunja URL (https://...)", False),
    "VIKUNJA_PROJECT_ID": ("Inbox project ID (e.g. 5)", False),
    "VIKUNJA_TOKEN": ("API token (tk_...)", True),
}

Gtk.init()


def _margins(widget, px: int = 8) -> None:
    for side in ("top", "bottom", "start", "end"):
        getattr(widget, f"set_margin_{side}")(px)


def gtk_entry(label: str = "", password: bool = False):
    """Show a borderless entry; return the text, or None if cancelled (Esc)."""
    result = {"text": None}
    loop = GLib.MainLoop()

    win = Gtk.Window()
    win.set_decorated(False)  # no title bar
    win.set_default_size(WIDTH, -1)
    win.connect("close-request", lambda *_: (loop.quit(), False)[1])

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    _margins(box)
    win.set_child(box)

    if label:
        box.append(Gtk.Label(label=label, xalign=0))

    entry = Gtk.Entry()
    entry.set_size_request(WIDTH, -1)
    if password:
        entry.set_visibility(False)  # masked input
    box.append(entry)

    def submit(_widget):
        result["text"] = entry.get_text()
        win.close()

    entry.connect("activate", submit)  # Enter

    keys = Gtk.EventControllerKey()
    keys.connect(
        "key-pressed",
        lambda _c, kv, _kc, _st: (win.close() or True)
        if kv == Gdk.KEY_Escape else False,  # Esc -> cancel, result stays None
    )
    win.add_controller(keys)

    win.present()
    loop.run()
    return result["text"]


def gtk_capture():
    """Show a borderless multi-line box for the capture itself.

        Enter        -> submit
        Shift+Enter  -> new line (first line is the title, the rest the body)
        Esc          -> cancel (returns None)

    The box starts one line tall and grows as you add lines. Returns the text,
    or None if cancelled.
    """
    result = {"text": None}
    loop = GLib.MainLoop()

    win = Gtk.Window()
    win.set_decorated(False)  # no title bar
    win.set_default_size(WIDTH, -1)
    win.connect("close-request", lambda *_: (loop.quit(), False)[1])

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    _margins(box)
    win.set_child(box)

    view = Gtk.TextView()
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    view.set_size_request(WIDTH, -1)
    buffer = view.get_buffer()
    box.append(view)

    # Emphasise the first line as the "title" -- but only once a second line
    # exists, so a plain one-line capture still looks plain.
    title_tag = buffer.create_tag("title", weight=700, scale=1.2)

    def restyle(*_):
        start, end = buffer.get_bounds()
        buffer.remove_tag(title_tag, start, end)
        if buffer.get_line_count() > 1:
            first_end = start.copy()
            first_end.forward_to_line_end()  # stop before the newline
            buffer.apply_tag(title_tag, start, first_end)

    buffer.connect("changed", restyle)

    def on_key(_c, keyval, _kc, state):
        if keyval == Gdk.KEY_Escape:
            win.close()  # result stays None
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                return False  # let the TextView insert a newline
            start, end = buffer.get_bounds()
            result["text"] = buffer.get_text(start, end, False)
            win.close()
            return True
        return False

    keys = Gtk.EventControllerKey()
    # Capture phase: intercept Enter before the TextView turns it into a newline.
    keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    keys.connect("key-pressed", on_key)
    win.add_controller(keys)

    win.present()
    view.grab_focus()
    loop.run()
    return result["text"]


def gtk_error(message: str) -> None:
    """Blocking error dialog -- a failure must never be silent (no notifications)."""
    loop = GLib.MainLoop()
    win = Gtk.Window(title="Vikunja capture")
    win.set_default_size(WIDTH, -1)
    win.connect("close-request", lambda *_: (loop.quit(), False)[1])

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    _margins(box, 16)
    win.set_child(box)

    label = Gtk.Label(label=message, xalign=0)
    label.set_wrap(True)
    box.append(label)

    button = Gtk.Button(label="OK")
    button.connect("clicked", lambda _b: win.close())
    box.append(button)

    win.present()
    button.grab_focus()
    loop.run()


def ensure_config() -> None:
    missing = capture.missing_vars(capture.load_config())
    if not missing:
        return
    for var in missing:
        label, password = SETUP_PROMPTS.get(var, (var, False))
        value = gtk_entry(label, password)
        if not value or not value.strip():  # cancelled -> save nothing partial
            sys.exit(0)
        value = value.strip()
        if var == "VIKUNJA_URL":
            value = value.rstrip("/")
        with capture._open_private_append(capture.CONFIG_FILE) as f:
            f.write(f"{var}={value}\n")
    os.chmod(capture.CONFIG_FILE, 0o600)


def main() -> int:
    ensure_config()
    text = gtk_capture()  # bare wide multi-line box, no label
    if not text or not text.strip():  # empty input or Esc -> exit quietly
        return 0
    # Success is silent. On failure the text is already backed up by capture();
    # surface the error via a dialog so a lost thought is never silent.
    code, error = capture.capture(text)
    if error:
        gtk_error(error)
    return code


if __name__ == "__main__":
    sys.exit(main())
