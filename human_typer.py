"""Human Typer: a macOS utility for natural-looking automated typing.

The module is organized in execution order:

1. Native keyboard and timing helpers.
2. JSON-backed history, presets, and settings.
3. Custom widgets and visual tokens.
4. Sequential construction of the main and settings pages.
5. Background-thread, hotkey, and AppKit event coordination.

Tkinter widgets must only be updated on the main thread. Worker threads and
AppKit delegates therefore communicate with the UI through ``_ui_queue``.
"""

import json
import math
import os
import queue
import random
import re
import sys
import threading
import time

import customtkinter as ctk
from PIL import Image, ImageDraw
from pynput import keyboard as pynput_keyboard

from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyRegular,
    NSImage,
    NSMenu,
    NSMenuItem,
    NSObject,
    NSStatusBar,
    NSWorkspace,
)
from ApplicationServices import AXIsProcessTrustedWithOptions
from Quartz import (
    CGEventCreateKeyboardEvent,
    CGEventKeyboardSetUnicodeString,
    CGEventPost,
    CGEventPostToPid,
    kCGHIDEventTap,
)

# ── App appearance ────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

LOGO_PATH     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "human_typer_icon_hd.png")
PRESETS_PATH  = os.path.expanduser("~/.humantyper_presets.json")
HISTORY_PATH  = os.path.expanduser("~/.humantyper_history.json")
SETTINGS_PATH = os.path.expanduser("~/.humantyper_settings.json")
MAX_HISTORY   = 10

WINDOW_MODE_VALUES = {
    "Dock + Menu Bar": "both",
    "Dock Only": "dock",
    "Menu Bar Only": "menubar",
    "Neither": "neither",
}

UI_COMMAND_STATUS = "status"
UI_COMMAND_PROGRESS = "progress"
UI_COMMAND_START = "start"
UI_COMMAND_SHOW = "show"
UI_COMMAND_QUIT = "quit"
UI_COMMAND_TOGGLE_PIN = "toggle_pin"

# Thread-safe queue for background work to reach tkinter's main thread.
_ui_queue = queue.Queue()

# ── Global state ──────────────────────────────────────────────────────────────
stop_flag = False
chunk_resume_event = threading.Event()
typing_thread = None
_KEY_CODES = {"\n": 36, "\t": 48}
_BACKSPACE = 51

PUNCT_DELAYS = {
    ',':  (0.00, 0.03), '.':  (0.00, 0.03), "'":  (0.02, 0.06),
    '"':  (0.05, 0.12), '!':  (0.05, 0.12), '?':  (0.05, 0.12),
    '-':  (0.06, 0.14), ':':  (0.08, 0.16), ';':  (0.08, 0.16),
    '(':  (0.10, 0.20), ')':  (0.10, 0.20), '/':  (0.10, 0.20),
    '@':  (0.12, 0.22), '#':  (0.12, 0.22), '%':  (0.12, 0.22),
    '&':  (0.12, 0.22), '_':  (0.14, 0.25), '*':  (0.14, 0.25),
    '[':  (0.14, 0.25), ']':  (0.14, 0.25), '{':  (0.16, 0.28),
    '}':  (0.16, 0.28), '\\': (0.16, 0.28), '|':  (0.16, 0.28),
    '^':  (0.16, 0.28), '~':  (0.16, 0.28), '`':  (0.16, 0.28),
}

# ── Icon builders ─────────────────────────────────────────────────────────────
def make_grid_icon(size=22, color=(160, 200, 220)):
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    gap  = s // 7
    half = (s - gap) // 2
    r    = s // 9
    for row in range(2):
        for col in range(2):
            x0 = col * (half + gap)
            y0 = row * (half + gap)
            d.rounded_rectangle([x0, y0, x0 + half, y0 + half], radius=r, fill=(*color, 255))
    return img.resize((size, size), Image.LANCZOS)

def make_gear_icon(size=22, color=(160, 200, 220)):
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)
    cx, cy = s // 2, s // 2
    teeth  = 8
    outer_r, tooth_r, inner_r, hole_r = int(s*.36), int(s*.46), int(s*.22), int(s*.13)
    th = math.pi / (teeth * 2.2)
    pts = []
    for i in range(teeth):
        base = 2 * math.pi * i / teeth
        for angle, radius in [
            (base - th * 1.6, outer_r), (base - th, tooth_r),
            (base + th,       tooth_r), (base + th * 1.6, outer_r),
        ]:
            pts.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    d.polygon(pts, fill=(*color, 255))
    d.ellipse([cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r], fill=(*color, 255))
    d.ellipse([cx - hole_r,  cy - hole_r,  cx + hole_r,  cy + hole_r],  fill=(0, 0, 0, 0))
    return img.resize((size, size), Image.LANCZOS)

# Native macOS typing helpers
def get_frontmost_pid():
    """Return the process ID of the frontmost macOS application, if any."""
    try:
        frontmost_app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return int(frontmost_app.processIdentifier()) if frontmost_app else None
    except Exception:
        return None

def _post_event(event, pid=None):
    if pid is None:
        CGEventPost(kCGHIDEventTap, event)
    else:
        CGEventPostToPid(pid, event)

def _post_key(keycode, down, pid=None):
    _post_event(CGEventCreateKeyboardEvent(None, keycode, down), pid)

def type_char(char, pid=None):
    """Post one Unicode character globally or directly to ``pid``."""
    if char in _KEY_CODES:
        _post_key(_KEY_CODES[char], True, pid)
        _post_key(_KEY_CODES[char], False, pid)
    else:
        utf16_length = len(char.encode("utf-16-le")) // 2
        for down in (True, False):
            e = CGEventCreateKeyboardEvent(None, 0, down)
            CGEventKeyboardSetUnicodeString(e, utf16_length, char)
            _post_event(e, pid)

def press_backspace(pid=None):
    _post_key(_BACKSPACE, True, pid)
    _post_key(_BACKSPACE, False, pid)

# ── Typing helpers ────────────────────────────────────────────────────────────
def punctuation_delay(char):
    if char in PUNCT_DELAYS:
        lo, hi = PUNCT_DELAYS[char]
        return random.uniform(lo, hi)
    return 0.0

def breaks_interval(intensity):
    """Choose the word interval before the next simulated human pause."""
    max_i, min_i = 50, 2
    interval = max_i - (intensity - 1) / 99 * (max_i - min_i)
    lo = max(1, int(interval * 0.7))
    hi = max(lo + 1, int(interval * 1.3))
    return random.randint(lo, hi)

def fmt_seconds(secs):
    """Format an approximate duration for the text statistics row."""
    secs = int(secs)
    if secs < 60:
        return f"~{secs}s"
    return f"~{secs // 60}m {secs % 60}s"


def format_cap_intensity(value):
    """Convert the capitalization-error slider value to its display label."""
    numeric_value = int(value)
    if numeric_value == 0:
        return "Off"
    return f"~{max(1, round(numeric_value / 2))}%"

# Adjacent keys on a standard QWERTY keyboard
_ADJACENT = {
    'q':['w','a'],'w':['q','e','a','s'],'e':['w','r','s','d'],'r':['e','t','d','f'],
    't':['r','y','f','g'],'y':['t','u','g','h'],'u':['y','i','h','j'],'i':['u','o','j','k'],
    'o':['i','p','k','l'],'p':['o','l'],
    'a':['q','w','s','z'],'s':['a','w','e','d','z','x'],'d':['s','e','r','f','x','c'],
    'f':['d','r','t','g','c','v'],'g':['f','t','y','h','v','b'],'h':['g','y','u','j','b','n'],
    'j':['h','u','i','k','n','m'],'k':['j','i','o','l','m'],'l':['k','o','p'],
    'z':['a','s','x'],'x':['z','s','d','c'],'c':['x','d','f','v'],
    'v':['c','f','g','b'],'b':['v','g','h','n'],'n':['b','h','j','m'],'m':['n','j','k'],
}

def adjacent_key(char):
    """Return a random adjacent key, or a random letter if none found."""
    c = char.lower()
    neighbors = _ADJACENT.get(c)
    if neighbors:
        return random.choice(neighbors)
    return random.choice("abcdefghijklmnopqrstuvwxyz")

def do_typo(char, intensity, adjacent_only, background_mode, target_pid):
    """Simulate one typo and return the selected correction style."""
    roll          = random.random()
    wrong_weight  = 1.0
    double_weight = max(0.0, (intensity - 0.3) / 0.7)
    swap_weight   = max(0.0, (intensity - 0.6) / 0.4)
    total         = wrong_weight + double_weight + swap_weight
    wrong_thresh  = wrong_weight / total
    double_thresh = wrong_thresh + double_weight / total

    def type_it(c):
        type_char(c, target_pid if background_mode else None)

    def backspace():
        press_backspace(target_pid if background_mode else None)

    if roll < wrong_thresh:
        wrong = adjacent_key(char) if adjacent_only else random.choice("abcdefghijklmnopqrstuvwxyz")
        type_it(wrong)
        time.sleep(random.uniform(0.06, 0.14))
        backspace()
        time.sleep(0.06)
        return "wrong"
    elif roll < double_thresh:
        type_it(char)
        time.sleep(random.uniform(0.04, 0.10))
        backspace()
        time.sleep(0.06)
        return "double"
    else:
        return "swap"

# ── Core typing thread ────────────────────────────────────────────────────────
def type_text(text, wpm, typo_intensity, adjacent_only, cap_intensity, variance,
              acceleration, fatigue, punct_enabled,
              breaks_enabled, breaks_intensity, background_mode,
              countdown, chunk_mode, chunk_size,
              loop_enabled, loop_count, loop_delay,
              status_cb, progress_cb):
    """Type text using the configured humanization model.

    This function runs on a worker thread. It must report UI changes through
    ``status_cb`` and ``progress_cb`` rather than touching Tk widgets directly.
    """
    global stop_flag

    for i in range(countdown, 0, -1):
        if stop_flag:
            status_cb("Stopped.")
            return
        status_cb(f"Starting in {i}s  —  click your target window...")
        time.sleep(1)

    target_pid = get_frontmost_pid() if background_mode else None
    if background_mode and target_pid is None:
        status_cb("Could not identify the target window.")
        return

    def type_it(c):
        type_char(c, target_pid if background_mode else None)

    total_loops = loop_count if loop_enabled else 1
    loop_num    = 0

    while loop_num < total_loops:
        if stop_flag:
            break

        if loop_enabled and loop_num > 0:
            label = f"Loop {loop_num + 1}/{total_loops} — waiting {loop_delay}s..."
            status_cb(label)
            for _ in range(loop_delay):
                if stop_flag:
                    break
                time.sleep(1)
            if stop_flag:
                break

        status_cb(f"Typing{f' (loop {loop_num+1}/{total_loops})' if loop_enabled else ''}...")

        base_delay  = 1 / ((wpm * 5) / 60)
        var_pct     = variance * 0.4
        total_chars = len(text)
        chars_done  = 0
        words       = re.findall(r'\S+|\s+', text)
        words_typed = 0
        chunk_count = 0
        next_chunk_at = random.randint(max(1, chunk_size // 2), chunk_size)
        next_break    = breaks_interval(breaks_intensity) if breaks_enabled else 9999

        # Fatigue: total slowdown applied linearly over the full text (0-20%)
        fatigue_max = fatigue * 0.20

        i = 0
        while i < len(words):
            if stop_flag:
                break
            word     = words[i]
            stripped = word.strip()
            is_real  = bool(stripped)

            # ── Human break ──────────────────────────────────────────────────
            break_due = (
                breaks_enabled
                and is_real
                and words_typed > 0
                and words_typed % max(1, int(next_break)) == 0
            )
            if break_due:
                time.sleep(random.uniform(0.8, 2.5))
                next_break = breaks_interval(breaks_intensity)

            # ── Chunk mode pause ─────────────────────────────────────────────
            if chunk_mode and is_real and chunk_count > 0 and chunk_count >= next_chunk_at:
                chunk_resume_event.clear()
                status_cb("Paused — press Ctrl+Opt+Space to continue...")
                chunk_resume_event.wait()
                if stop_flag:
                    break
                chunk_count   = 0
                next_chunk_at = random.randint(max(1, chunk_size // 2), chunk_size)
                status_cb("Typing...")

            # ── Type each character ───────────────────────────────────────────
            chars      = list(word)
            word_len   = max(1, len([c for c in chars if c.isalpha()]))
            ci         = 0
            alpha_idx  = 0  # position within alphabetic chars of word

            while ci < len(chars):
                if stop_flag:
                    break
                char = chars[ci]
                t0   = time.time()

                # Fatigue multiplier — grows from 1.0 → 1+fatigue_max over full text
                fatigue_mult = 1.0 + fatigue_max * (chars_done / max(1, total_chars))

                # Acceleration multiplier — slow at word edges, fast mid-word
                # Maps 0 (start) → 1.15, 0.5 (middle) → 1.0, 1.0 (end) → 1.15
                if acceleration and char.isalpha():
                    pos   = alpha_idx / max(1, word_len - 1) if word_len > 1 else 0.5
                    accel = 1.0 + 0.15 * abs(pos * 2 - 1)  # parabola: slow-fast-slow
                    alpha_idx += 1
                else:
                    accel = 1.0

                if punct_enabled and not char.isalnum() and not char.isspace():
                    p = punctuation_delay(char)
                    if p > 0:
                        time.sleep(p)

                made_typo = False
                style     = None
                should_make_typo = (
                    typo_intensity > 0
                    and random.random() < (0.5 * typo_intensity)
                    and char.isalpha()
                )
                if should_make_typo:
                    style = do_typo(
                        char,
                        typo_intensity,
                        adjacent_only,
                        background_mode,
                        target_pid,
                    )
                    made_typo = True
                    if style == "swap" and ci + 1 < len(chars) and chars[ci + 1].isalpha():
                        next_char = chars[ci + 1]
                        type_it(next_char)
                        time.sleep(random.uniform(0.05, 0.12))
                        type_it(char)
                        time.sleep(random.uniform(0.08, 0.18))
                        pid = target_pid if background_mode else None
                        press_backspace(pid)
                        press_backspace(pid)
                        time.sleep(0.08)
                        type_it(char)
                        time.sleep(random.uniform(0.04, 0.10))
                        type_it(next_char)
                        chars_done += 2
                        ci += 2
                        alpha_idx += 1
                        progress_cb(min(1.0, chars_done / total_chars))
                        variance_multiplier = 1 + random.uniform(-var_pct, var_pct)
                        delay = base_delay * accel * fatigue_mult * variance_multiplier
                        remaining = delay - (time.time() - t0)
                        if remaining > 0:
                            time.sleep(remaining)
                        continue
                    if style == "swap":
                        # A swap needs another letter. At a word boundary, type
                        # the current character normally instead of dropping it.
                        made_typo = False

                if not made_typo or style != "swap":
                    # Capitalization error — type wrong case then backspace-correct
                    if cap_intensity > 0 and char.isalpha() and not made_typo:
                        cap_chance = 0.01 + (cap_intensity - 1) / 99 * 0.49  # 1% at 1, 50% at 100
                        if random.random() < cap_chance:
                            wrong_case = char.upper() if char.islower() else char.lower()
                            type_it(wrong_case)
                            time.sleep(random.uniform(0.06, 0.16))
                            press_backspace(target_pid if background_mode else None)
                            time.sleep(random.uniform(0.04, 0.10))
                    type_it(char)

                chars_done += 1
                progress_cb(min(1.0, chars_done / total_chars))
                delay = base_delay * accel * fatigue_mult * (1 + random.uniform(-var_pct, var_pct))
                remaining = delay - (time.time() - t0)
                if remaining > 0:
                    time.sleep(remaining)
                ci += 1

            if is_real:
                words_typed += 1
                chunk_count  += 1
                if breaks_enabled and any(p in stripped for p in ('.', '!', '?')):
                    time.sleep(random.uniform(0.4, 1.0))
            i += 1

        loop_num += 1

    if not stop_flag:
        progress_cb(1.0)
        status_cb("Done.")
    else:
        status_cb("Stopped.")

# ── History ───────────────────────────────────────────────────────────────────
def _load_json(path, default):
    """Load a JSON file, returning ``default`` when it is absent or invalid."""
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path, data):
    """Write application data in a stable, human-readable JSON format."""
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)


def load_history():
    """Return recently typed text, newest first."""
    return _load_json(HISTORY_PATH, [])

def save_to_history(text):
    """Add text to history while preserving uniqueness and the size limit."""
    history = load_history()
    # Remove duplicate if exists, then prepend
    history = [t for t in history if t != text]
    history.insert(0, text)
    history = history[:MAX_HISTORY]
    _save_json(HISTORY_PATH, history)

# ── Presets ───────────────────────────────────────────────────────────────────
def load_presets():
    """Return the map of saved preset names to typing configurations."""
    return _load_json(PRESETS_PATH, {})

def save_presets(presets):
    """Persist the complete preset map."""
    _save_json(PRESETS_PATH, presets)

# ── App settings (persists slider values, UI state, flags) ───────────────────
def _load_settings_raw():
    return _load_json(SETTINGS_PATH, {})

def _save_settings_raw(data):
    try:
        _save_json(SETTINGS_PATH, data)
    except Exception:
        pass

def save_settings():
    """Persist control values and appearance settings without blocking exit."""
    try:
        data = _load_settings_raw()
        data['ui'] = get_current_config()
        data['window_mode'] = window_mode_menu.get()
        data['color_mode']  = mode_menu.get()
        _save_settings_raw(data)
    except Exception:
        pass

def load_settings():
    """Restore saved controls, appearance, and macOS visibility behavior."""
    data = _load_settings_raw()
    ui_config = data.get('ui')
    if ui_config:
        try:
            apply_config(ui_config)
        except Exception:
            pass
    color_mode = data.get('color_mode')
    if color_mode:
        try:
            mode_menu.set(color_mode)
            ctk.set_appearance_mode(color_mode)
        except Exception:
            pass
    # Always apply window mode (defaulting to both) — this also builds the menu bar
    window_mode = data.get('window_mode', 'Dock + Menu Bar')
    try:
        window_mode_menu.set(window_mode)
    except Exception:
        pass
    apply_window_mode(WINDOW_MODE_VALUES.get(window_mode, "both"))

# ══════════════════════════════════════════════════════════════════════════════
# GUI
# ══════════════════════════════════════════════════════════════════════════════
class SmoothScrollableFrame(ctk.CTkScrollableFrame):
    """CTk scroll frame with stable macOS trackpad boundaries."""

    _EDGE_EPSILON = 1e-5
    _EDGE_LOCK_SECONDS = 0.12
    _MAX_SCROLL_STEPS = 4

    def __init__(self, *args, **kwargs):
        self._edge_lock_until = 0.0
        self._edge_lock_position = None
        super().__init__(*args, **kwargs)

    def _event_is_inside(self, widget):
        while widget is not None:
            if widget == self._parent_canvas:
                return True
            widget = getattr(widget, "master", None)
        return False

    def _event_targets_nested_scroll(self, widget):
        """Keep a child editor's wheel events out of the page scroller."""
        while widget is not None and widget != self._parent_canvas:
            if getattr(widget, "_blocks_parent_scroll", False):
                return True
            widget = getattr(widget, "master", None)
        return False

    def _mouse_wheel_all(self, event):
        if not self._event_is_inside(event.widget):
            return None
        if self._event_targets_nested_scroll(event.widget):
            # Tk's Text class has already handled the wheel event before the
            # global binding runs. Stop here so the page does not move too.
            return "break"
        if sys.platform != "darwin":
            return super()._mouse_wheel_all(event)

        canvas = self._parent_canvas
        horizontal = self._shift_pressed
        view = canvas.xview if horizontal else canvas.yview
        move_to = canvas.xview_moveto if horizontal else canvas.yview_moveto
        scroll_by = canvas.xview_scroll if horizontal else canvas.yview_scroll

        try:
            delta = int(event.delta)
        except (TypeError, ValueError):
            return "break"
        if delta == 0:
            return "break"

        steps = max(-self._MAX_SCROLL_STEPS,
                    min(self._MAX_SCROLL_STEPS, -delta))
        now = time.monotonic()
        if now < self._edge_lock_until and self._edge_lock_position is not None:
            blocked_direction = -1 if self._edge_lock_position == 0.0 else 1
            if (steps < 0) == (blocked_direction < 0):
                move_to(self._edge_lock_position)
                return "break"
            self._edge_lock_position = None

        first, last = view()
        at_start = first <= self._EDGE_EPSILON
        at_end = last >= 1.0 - self._EDGE_EPSILON

        if (steps < 0 and at_start) or (steps > 0 and at_end):
            edge = 0.0 if steps < 0 else 1.0
            move_to(edge)
            self._edge_lock_position = edge
            self._edge_lock_until = now + self._EDGE_LOCK_SECONDS
            return "break"

        self._edge_lock_position = None
        scroll_by(steps, "units")

        # Snap fractional positions to a hard boundary. macOS momentum events
        # can otherwise alternate around the last pixel and shake the content.
        first, last = view()
        if steps < 0 and first <= self._EDGE_EPSILON:
            move_to(0.0)
        elif steps > 0 and last >= 1.0 - self._EDGE_EPSILON:
            move_to(1.0)
        return "break"


TEAL          = ("#2563eb", "#5aa2ff")
TEAL_HOVER    = ("#1d4ed8", "#3f8eeb")
CARD_BG       = ("#ffffff", "#292a2d")
CARD_BDR      = ("#d5d7db", "#3c3e42")
SIDEBAR_BG    = ("#f0f1f3", "#1a1b1d")
APP_BG        = ("#f7f7f8", "#222326")
INPUT_BG      = ("#f2f3f5", "#1e1f21")
NAV_ACTIVE    = ("#e7eefc", "#30343a")
NAV_HOVER     = ("#e9eaed", "#2b2c2f")
MUTED         = ("#5f6368", "#a6a9ae")
HDR_TEXT      = ("#202124", "#f2f3f5")
PRIMARY_TEXT  = ("#ffffff", "#0b1629")
SCROLL_THUMB  = ("#aeb2b8", "#565a60")
DANGER        = ("#b63d48", "#f06a73")
DANGER_HOVER  = ("#f5dadd", "#44252a")
TEAL_RGB      = (90, 162, 255)
MUTED_RGB     = (166, 169, 174)
NAV_W         = 132
FONT_FAMILY   = "SF Pro Text"

app = ctk.CTk()
app.title("Human Typer")
app.geometry("820x760")
app.minsize(720, 660)
app.resizable(True, True)
app.configure(fg_color=APP_BG)

# Give macOS a full-resolution application icon instead of inheriting the
# low-resolution Python launcher icon in the Dock and app switcher.
_dock_icon = None
if os.path.exists(LOGO_PATH):
    _dock_icon = NSImage.alloc().initWithContentsOfFile_(LOGO_PATH)
    if _dock_icon is not None:
        NSApplication.sharedApplication().setApplicationIconImage_(_dock_icon)

# ── Menu bar status item ──────────────────────────────────────────────────────
_status_item = None

_context_menu   = None  # right-click menu
_pin_item       = None  # NSMenuItem for pin toggle
_pinned = False

class _MenuDelegate(NSObject):
    def handleClick_(self, sender):
        event = NSApplication.sharedApplication().currentEvent()
        # type 3 = right-mouse-down → show context menu
        if event and event.type() == 3:
            _status_item.popUpStatusItemMenu_(_context_menu)
        else:
            _ui_queue.put(UI_COMMAND_SHOW)

    def quitApp_(self, sender):
        _ui_queue.put(UI_COMMAND_QUIT)

    def togglePin_(self, sender):
        _ui_queue.put(UI_COMMAND_TOGGLE_PIN)

_menu_delegate = _MenuDelegate.alloc().init()


def _build_menu_bar():
    global _status_item, _context_menu, _pin_item

    _context_menu = NSMenu.alloc().init()
    _context_menu.setAutoenablesItems_(False)

    _pin_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Pin to Menu Bar", "togglePin:", "")
    _pin_item.setTarget_(_menu_delegate)
    _pin_item.setState_(0)
    _context_menu.addItem_(_pin_item)

    _context_menu.addItem_(NSMenuItem.separatorItem())

    quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Quit Human Typer", "quitApp:", "")
    quit_item.setTarget_(_menu_delegate)
    _context_menu.addItem_(quit_item)

    sb = NSStatusBar.systemStatusBar()
    _status_item = sb.statusItemWithLength_(-1)
    _status_item.setHighlightMode_(True)

    btn = _status_item.button()
    if os.path.exists(LOGO_PATH):
        ns_img = NSImage.alloc().initWithContentsOfFile_(LOGO_PATH)
        ns_img.setSize_((18, 18))
        ns_img.setTemplate_(True)
        btn.setImage_(ns_img)
    else:
        btn.setTitle_("⌨")

    btn.setAction_("handleClick:")
    btn.setTarget_(_menu_delegate)
    btn.sendActionOn_(2 | 8)  # left + right mouse down

def apply_window_mode(mode):
    """Apply one of: ``both``, ``dock``, ``menubar``, or ``neither``."""
    ns_app = NSApplication.sharedApplication()
    global _status_item
    show_menubar = mode in ("both", "menubar")
    show_dock    = mode in ("both", "dock")

    # Remove status item first if switching away from menu bar
    if not show_menubar and _status_item is not None:
        NSStatusBar.systemStatusBar().removeStatusItem_(_status_item)
        _status_item = None

    # Set activation policy BEFORE building the status item — changing it
    # after the item exists can destroy it on macOS 26+
    if show_dock:
        ns_app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    else:
        ns_app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    # Build status item after policy is stable
    if show_menubar and _status_item is None:
        _build_menu_bar()

# Set activation policy first, then build menu bar
NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyRegular)
_build_menu_bar()

_ico_home_active = ctk.CTkImage(make_grid_icon(20, TEAL_RGB),  size=(20, 20))
_ico_home_idle   = ctk.CTkImage(make_grid_icon(20, MUTED_RGB), size=(20, 20))
_ico_gear_active = ctk.CTkImage(make_gear_icon(20, TEAL_RGB),  size=(20, 20))
_ico_gear_idle   = ctk.CTkImage(make_gear_icon(20, MUTED_RGB), size=(20, 20))

if LOGO_PATH and os.path.exists(LOGO_PATH):
    _logo_img = ctk.CTkImage(Image.open(LOGO_PATH).convert("RGBA"), size=(46, 46))
else:
    _logo_img = None

# ── Sidebar ───────────────────────────────────────────────────────────────────
sidebar = ctk.CTkFrame(app, width=NAV_W, corner_radius=0, fg_color=SIDEBAR_BG)
sidebar.pack(side="left", fill="y")
sidebar.pack_propagate(False)

right_area = ctk.CTkFrame(app, corner_radius=0, fg_color=APP_BG)
right_area.pack(side="left", fill="both", expand=True)

if _logo_img:
    ctk.CTkLabel(sidebar, image=_logo_img, text="").pack(pady=(18, 5))
ctk.CTkLabel(sidebar, text="Human Typer", font=ctk.CTkFont(
    family=FONT_FAMILY, size=13, weight="bold"), text_color=HDR_TEXT).pack(pady=(0, 14))
ctk.CTkFrame(sidebar, height=1, fg_color=CARD_BDR).pack(fill="x", padx=14, pady=(0, 12))

nav_home_btn = ctk.CTkButton(
    sidebar, image=_ico_home_active, text="Type", compound="left", anchor="w",
    width=104, height=42, font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
    fg_color=NAV_ACTIVE, hover_color=NAV_HOVER, text_color=HDR_TEXT, corner_radius=10)
nav_home_btn.pack(fill="x", padx=12, pady=(0, 6))

nav_gear_btn = ctk.CTkButton(
    sidebar, image=_ico_gear_idle, text="Settings", compound="left", anchor="w",
    width=104, height=42, font=ctk.CTkFont(family=FONT_FAMILY, size=12, weight="bold"),
    fg_color="transparent", hover_color=NAV_HOVER, text_color=MUTED, corner_radius=10)
nav_gear_btn.pack(fill="x", padx=12, pady=0)

# ── Page header ───────────────────────────────────────────────────────────────
page_header = ctk.CTkFrame(right_area, height=76, corner_radius=0, fg_color="transparent")
page_header.pack(fill="x")
page_header.pack_propagate(False)

page_heading = ctk.CTkFrame(page_header, fg_color="transparent")
page_heading.pack(side="left", padx=24, pady=(13, 10))
page_title_lbl = ctk.CTkLabel(
    page_heading, text="Type text",
    font=ctk.CTkFont(family="SF Pro Display", size=23, weight="bold"),
    text_color=HDR_TEXT, anchor="w")
page_title_lbl.pack(anchor="w")
page_subtitle_lbl = ctk.CTkLabel(
    page_heading, text="Natural rhythm, precise control",
    font=ctk.CTkFont(family=FONT_FAMILY, size=11), text_color=MUTED, anchor="w")
page_subtitle_lbl.pack(anchor="w", pady=(1, 0))

ctk.CTkFrame(right_area, height=1, fg_color=CARD_BDR).pack(fill="x")

page_container = ctk.CTkFrame(right_area, corner_radius=0, fg_color="transparent")
page_container.pack(fill="both", expand=True)
page_container.grid_rowconfigure(0, weight=1)
page_container.grid_columnconfigure(0, weight=1)

# ── Helpers ───────────────────────────────────────────────────────────────────
def card(parent, **kw):
    return ctk.CTkFrame(parent, corner_radius=14, fg_color=CARD_BG,
                         border_width=1, border_color=CARD_BDR, **kw)

def section_lbl(parent, text):
    ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(
        family=FONT_FAMILY, size=11, weight="bold"),
        text_color=MUTED).pack(anchor="w", padx=4, pady=(20, 7))

def opt_row(parent, label_text, pady=(10, 0)):
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", padx=16, pady=pady)
    ctk.CTkLabel(row, text=label_text, font=ctk.CTkFont(
        family=FONT_FAMILY, size=13), text_color=HDR_TEXT).pack(side="left")
    var = ctk.IntVar()
    sw  = ctk.CTkSwitch(row, text="", variable=var, width=46,
                         progress_color=TEAL, button_color=("#f0f0f0", "#e0e0e0"))
    sw.pack(side="right")
    return var, sw

# ═══════════════════════════════════════════
# MAIN PAGE
# ═══════════════════════════════════════════
main_wrap = ctk.CTkFrame(page_container, fg_color="transparent", corner_radius=0)
main_wrap.grid(row=0, column=0, sticky="nsew")
main_wrap.grid_rowconfigure(0, weight=1)
main_wrap.grid_columnconfigure(0, weight=1)

scroll = SmoothScrollableFrame(main_wrap, fg_color="transparent",
                               corner_radius=0, scrollbar_button_color=SCROLL_THUMB)
scroll.grid(row=0, column=0, sticky="nsew", padx=22, pady=(8, 0))

# ── helper: small description text under a control ───────────────────────────
def desc_lbl(parent, text):
    ctk.CTkLabel(parent, text=text, font=ctk.CTkFont(family=FONT_FAMILY, size=11),
                 text_color=MUTED, anchor="w", wraplength=540,
                 justify="left").pack(anchor="w", padx=16, pady=(0, 8))

def divider(parent):
    ctk.CTkFrame(parent, height=1, fg_color=CARD_BDR).pack(fill="x", padx=12, pady=4)

# ── Text input ────────────────────────────────────────────────────────────────
section_lbl(scroll, "TEXT TO TYPE")
text_card = card(scroll)
text_card.pack(fill="x", pady=(0, 2))
text_box = ctk.CTkTextbox(
    text_card, height=170, corner_radius=10, fg_color=INPUT_BG, border_width=0,
    font=ctk.CTkFont(family=FONT_FAMILY, size=13), text_color=HDR_TEXT)
text_box.pack(fill="x", padx=8, pady=8)
# CTkScrollableFrame listens globally for wheel events. Mark both the wrapper
# and native Text widget so its own scrolling never also moves the main page.
text_box._blocks_parent_scroll = True
text_box._textbox._blocks_parent_scroll = True

def load_from_file():
    import tkinter.filedialog as fd
    path = fd.askopenfilename(filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
            text_box.delete("1.0", "end")
            text_box.insert("1.0", content)
            update_counts()
        except Exception as e:
            set_status(f"Could not open file: {e}")

meta_row = ctk.CTkFrame(scroll, fg_color="transparent")
meta_row.pack(fill="x", pady=(2, 4))
count_lbl = ctk.CTkLabel(meta_row, text="0 words  ·  0 chars",
                           font=ctk.CTkFont(size=11), text_color=MUTED)
count_lbl.pack(side="left", padx=4)
est_lbl = ctk.CTkLabel(meta_row, text="", font=ctk.CTkFont(size=11), text_color=MUTED)
est_lbl.pack(side="left", padx=(8, 0))
ctk.CTkButton(meta_row, text="Load from File…", height=26, width=120,
               font=ctk.CTkFont(size=11), fg_color="transparent",
               border_width=1, border_color=CARD_BDR, text_color=MUTED,
               hover_color=NAV_HOVER, corner_radius=8,
               command=load_from_file).pack(side="right")
ctk.CTkButton(meta_row, text="Clear", height=26, width=60,
               font=ctk.CTkFont(size=11), fg_color="transparent",
               border_width=1, border_color=CARD_BDR, text_color=MUTED,
               hover_color=NAV_HOVER, corner_radius=8,
               command=lambda: (text_box.delete("1.0", "end"), update_counts())
               ).pack(side="right", padx=(0, 6))

def update_counts(*_):
    content = text_box.get("1.0", "end").strip()
    words   = len(content.split()) if content else 0
    chars   = len(content)
    count_lbl.configure(text=f"{words} words  ·  {chars} chars")
    try:
        wpm = float(wpm_entry.get())
        if wpm > 0 and chars > 0:
            secs = chars / (wpm * 5 / 60)
            est_lbl.configure(text=f"·  Est. {fmt_seconds(secs)}")
        else:
            est_lbl.configure(text="")
    except Exception:
        est_lbl.configure(text="")

text_box.bind("<KeyRelease>", update_counts)

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — SPEED & ACCURACY
# ══════════════════════════════════════════════════════════════════════════════
section_lbl(scroll, "SPEED & ACCURACY")
speed_card = card(scroll)
speed_card.pack(fill="x", pady=(0, 4))

# WPM + Typo side by side inside the card
top_row = ctk.CTkFrame(speed_card, fg_color="transparent")
top_row.pack(fill="x", padx=14, pady=(14, 4))

# WPM entry
wpm_block = ctk.CTkFrame(top_row, fg_color="transparent")
wpm_block.pack(side="left", padx=(0, 20))
ctk.CTkLabel(wpm_block, text="Words Per Minute", font=ctk.CTkFont(size=11, weight="bold"),
             text_color=MUTED).pack(anchor="w")
wpm_entry = ctk.CTkEntry(wpm_block, width=80, height=36,
                          font=ctk.CTkFont(family=FONT_FAMILY, size=18, weight="bold"),
                          justify="center", corner_radius=8,
                          fg_color=INPUT_BG, border_width=1,
                          border_color=TEAL, text_color=HDR_TEXT)
wpm_entry.insert(0, "40")
wpm_entry.pack(pady=(4, 0))
wpm_entry.bind("<KeyRelease>", update_counts)

# Typo intensity
typo_block = ctk.CTkFrame(top_row, fg_color="transparent")
typo_block.pack(side="left", fill="x", expand=True)
typo_header = ctk.CTkFrame(typo_block, fg_color="transparent")
typo_header.pack(fill="x")
ctk.CTkLabel(typo_header, text="Typo Intensity", font=ctk.CTkFont(size=11, weight="bold"),
             text_color=MUTED).pack(side="left")
typo_val_lbl = ctk.CTkLabel(typo_header, text="0%",
                              font=ctk.CTkFont(size=12, weight="bold"), text_color=TEAL)
typo_val_lbl.pack(side="right")
typo_slider = ctk.CTkSlider(typo_block, from_=0, to=100, number_of_steps=100,
                              progress_color=TEAL, button_color=TEAL, button_hover_color=TEAL_HOVER,
                              command=lambda v: typo_val_lbl.configure(text=f"{int(v)}%"))
typo_slider.set(0)
typo_slider.pack(fill="x", pady=(8, 0))

divider(speed_card)

# Adjacent-key typos
adjacent_var, _ = opt_row(speed_card, "Adjacent-Key Typos Only")
desc_lbl(speed_card, "Mistakes use keys physically next to the correct one on QWERTY (e.g. 'r' instead of 'e'), rather than a random letter.")

divider(speed_card)

# Capitalization errors
cap_header = ctk.CTkFrame(speed_card, fg_color="transparent")
cap_header.pack(fill="x", padx=14, pady=(8, 0))
ctk.CTkLabel(cap_header, text="Capitalization Errors", font=ctk.CTkFont(size=13),
             text_color=HDR_TEXT).pack(side="left")
cap_val_lbl = ctk.CTkLabel(cap_header, text="Off",
                            font=ctk.CTkFont(size=13, weight="bold"), text_color=TEAL)
cap_val_lbl.pack(side="right")
cap_slider = ctk.CTkSlider(speed_card, from_=0, to=100, number_of_steps=100,
                            progress_color=TEAL, button_color=TEAL, button_hover_color=TEAL_HOVER,
                            command=lambda v: cap_val_lbl.configure(
                                text=format_cap_intensity(v)))
cap_slider.set(0)
cap_slider.pack(fill="x", padx=14, pady=(6, 0))
desc_lbl(speed_card, "Occasionally types a letter in the wrong case then immediately self-corrects — like accidentally holding Shift a beat too long. 0 = off, 100 = ~50% of letters affected.")

divider(speed_card)

# Typing variance
var_header = ctk.CTkFrame(speed_card, fg_color="transparent")
var_header.pack(fill="x", padx=14, pady=(8, 0))
ctk.CTkLabel(var_header, text="Typing Variance", font=ctk.CTkFont(size=13),
             text_color=HDR_TEXT).pack(side="left")
var_val_lbl = ctk.CTkLabel(var_header, text="20%",
                            font=ctk.CTkFont(size=13, weight="bold"), text_color=TEAL)
var_val_lbl.pack(side="right")
variance_slider = ctk.CTkSlider(speed_card, from_=0, to=100, number_of_steps=100,
                                 progress_color=TEAL, button_color=TEAL, button_hover_color=TEAL_HOVER,
                                 command=lambda v: var_val_lbl.configure(text=f"{int(v)}%"))
variance_slider.set(20)
variance_slider.pack(fill="x", padx=14, pady=(6, 0))
desc_lbl(speed_card, "How evenly spaced keystrokes are. 0% = robotic (perfectly even), 100% = erratic (wide random swings).")

divider(speed_card)

# Fatigue
fat_header = ctk.CTkFrame(speed_card, fg_color="transparent")
fat_header.pack(fill="x", padx=14, pady=(8, 0))
ctk.CTkLabel(fat_header, text="Fatigue Simulation", font=ctk.CTkFont(size=13),
             text_color=HDR_TEXT).pack(side="left")
fat_val_lbl = ctk.CTkLabel(fat_header, text="0%",
                            font=ctk.CTkFont(size=13, weight="bold"), text_color=TEAL)
fat_val_lbl.pack(side="right")
fatigue_slider = ctk.CTkSlider(speed_card, from_=0, to=100, number_of_steps=100,
                                progress_color=TEAL, button_color=TEAL, button_hover_color=TEAL_HOVER,
                                command=lambda v: fat_val_lbl.configure(text=f"{int(v)}%"))
fatigue_slider.set(0)
fatigue_slider.pack(fill="x", padx=14, pady=(6, 0))
desc_lbl(speed_card, "Typing gradually slows down over the course of the text, like a real person getting tired.")

divider(speed_card)

# Word acceleration
accel_var, _ = opt_row(speed_card, "Word-Level Acceleration")
desc_lbl(speed_card, "Slightly slower at the start and end of each word, fastest mid-word — matches natural finger movement rhythm.")

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — TIMING & FLOW
# ══════════════════════════════════════════════════════════════════════════════
section_lbl(scroll, "TIMING & FLOW")
flow_card = card(scroll)
flow_card.pack(fill="x", pady=(0, 4))

# Countdown
cd_row = ctk.CTkFrame(flow_card, fg_color="transparent")
cd_row.pack(fill="x", padx=14, pady=(12, 4))
ctk.CTkLabel(cd_row, text="Countdown Before Typing", font=ctk.CTkFont(size=13),
             text_color=HDR_TEXT).pack(side="left")
countdown_var = ctk.IntVar(value=5)
for secs in (3, 5, 10, 15):
    ctk.CTkRadioButton(cd_row, text=f"{secs}s", variable=countdown_var, value=secs,
                        font=ctk.CTkFont(size=12), text_color=HDR_TEXT,
                        fg_color=TEAL, hover_color=TEAL_HOVER).pack(side="right", padx=6)

divider(flow_card)

# Human breaks
breaks_var, breaks_sw = opt_row(flow_card, "Human Breaks")
desc_lbl(flow_card, "Randomly pauses mid-text as if reading ahead, and always pauses after sentences.")

# Break intensity sub-row (hidden until on)
bi_sub = ctk.CTkFrame(flow_card, fg_color="transparent")
bi_sub_top = ctk.CTkFrame(bi_sub, fg_color="transparent")
bi_sub_top.pack(fill="x", padx=14)
ctk.CTkLabel(bi_sub_top, text="Break Frequency", font=ctk.CTkFont(size=12),
             text_color=HDR_TEXT).pack(side="left")
bi_val_lbl = ctk.CTkLabel(bi_sub_top, text="20",
                            font=ctk.CTkFont(size=12, weight="bold"), text_color=TEAL)
bi_val_lbl.pack(side="right")
breaks_slider = ctk.CTkSlider(bi_sub, from_=1, to=100, number_of_steps=99,
                               progress_color=TEAL, button_color=TEAL, button_hover_color=TEAL_HOVER,
                               command=lambda v: bi_val_lbl.configure(text=str(int(v))))
breaks_slider.set(20)
breaks_slider.pack(fill="x", padx=14, pady=(6, 10))

def on_breaks_toggle():
    if breaks_var.get():
        bi_sub.pack(fill="x", pady=(0, 4))
    else:
        bi_sub.pack_forget()

breaks_sw.configure(command=on_breaks_toggle)

divider(flow_card)

# Chunk mode
chunk_var, chunk_sw = opt_row(flow_card, "Chunk Mode")
desc_lbl(flow_card, "Pauses typing every few words and waits for Ctrl+Opt+Space to continue — like reading before you type each section.")

# Chunk size sub-row
chunk_sub = ctk.CTkFrame(flow_card, fg_color="transparent")
chunk_sub_top = ctk.CTkFrame(chunk_sub, fg_color="transparent")
chunk_sub_top.pack(fill="x", padx=14)
ctk.CTkLabel(chunk_sub_top, text="Max Words Per Chunk", font=ctk.CTkFont(size=12),
             text_color=HDR_TEXT).pack(side="left")
chunk_val_lbl = ctk.CTkLabel(chunk_sub_top, text="10 words",
                              font=ctk.CTkFont(size=12, weight="bold"), text_color=TEAL)
chunk_val_lbl.pack(side="right")
chunk_size_slider = ctk.CTkSlider(chunk_sub, from_=1, to=50, number_of_steps=49,
                                   progress_color=TEAL, button_color=TEAL, button_hover_color=TEAL_HOVER,
                                   command=lambda v: chunk_val_lbl.configure(text=f"{int(v)} words"))
chunk_size_slider.set(10)
chunk_size_slider.pack(fill="x", padx=14, pady=(6, 4))
ctk.CTkLabel(chunk_sub, text="Each chunk is randomized between half this value and the max.",
             font=ctk.CTkFont(size=11), text_color=MUTED).pack(anchor="w", padx=16, pady=(0, 10))

def on_chunk_toggle():
    if chunk_var.get():
        chunk_sub.pack(fill="x")
    else:
        chunk_sub.pack_forget()

chunk_sw.configure(command=on_chunk_toggle)

divider(flow_card)

# Loop / repeat
loop_var, loop_sw = opt_row(flow_card, "Loop / Repeat")
desc_lbl(flow_card, "Type the same text multiple times with a delay between each run.")

loop_sub = ctk.CTkFrame(flow_card, fg_color="transparent")

lc_top = ctk.CTkFrame(loop_sub, fg_color="transparent")
lc_top.pack(fill="x", padx=14)
ctk.CTkLabel(lc_top, text="Number of Loops", font=ctk.CTkFont(size=12),
             text_color=HDR_TEXT).pack(side="left")
lc_val_lbl = ctk.CTkLabel(lc_top, text="2×",
                            font=ctk.CTkFont(size=12, weight="bold"), text_color=TEAL)
lc_val_lbl.pack(side="right")
loop_count_slider = ctk.CTkSlider(loop_sub, from_=2, to=50, number_of_steps=48,
                                   progress_color=TEAL, button_color=TEAL, button_hover_color=TEAL_HOVER,
                                   command=lambda v: lc_val_lbl.configure(text=f"{int(v)}×"))
loop_count_slider.set(2)
loop_count_slider.pack(fill="x", padx=14, pady=(6, 8))

ld_top = ctk.CTkFrame(loop_sub, fg_color="transparent")
ld_top.pack(fill="x", padx=14)
ctk.CTkLabel(ld_top, text="Delay Between Loops", font=ctk.CTkFont(size=12),
             text_color=HDR_TEXT).pack(side="left")
ld_val_lbl = ctk.CTkLabel(ld_top, text="5s",
                            font=ctk.CTkFont(size=12, weight="bold"), text_color=TEAL)
ld_val_lbl.pack(side="right")
loop_delay_slider = ctk.CTkSlider(loop_sub, from_=1, to=60, number_of_steps=59,
                                   progress_color=TEAL, button_color=TEAL, button_hover_color=TEAL_HOVER,
                                   command=lambda v: ld_val_lbl.configure(text=f"{int(v)}s"))
loop_delay_slider.set(5)
loop_delay_slider.pack(fill="x", padx=14, pady=(6, 14))

def on_loop_toggle():
    if loop_var.get():
        loop_sub.pack(fill="x")
    else:
        loop_sub.pack_forget()

loop_sw.configure(command=on_loop_toggle)

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — ADVANCED
# ══════════════════════════════════════════════════════════════════════════════
section_lbl(scroll, "ADVANCED")
adv_card = card(scroll)
adv_card.pack(fill="x", pady=(0, 4))

punct_var, _ = opt_row(adv_card, "Punctuation Search Delay")
desc_lbl(adv_card, "Adds a brief hesitation before rare punctuation keys (!, ?, @, etc.) like a real person hunting for them on the keyboard.")

divider(adv_card)

background_var, bg_sw = opt_row(adv_card, "Background Mode")
desc_lbl(adv_card, "Types into the last window you clicked during the countdown — you can freely switch apps while it runs.")

bg_hint = ctk.CTkLabel(adv_card,
    text="  Click your target window during the countdown, then switch freely.",
    font=ctk.CTkFont(size=11), text_color=("#1ab8cc", "#2dd4e8"))

def on_bg_toggle():
    if background_var.get():
        bg_hint.pack(anchor="w", padx=16, pady=(0, 8))
    else:
        bg_hint.pack_forget()

bg_sw.configure(command=on_bg_toggle)

# ── Status bar + progress + buttons ──────────────────────────────────────────
ctk.CTkFrame(main_wrap, height=1, fg_color=CARD_BDR).grid(row=1, column=0, sticky="ew")

status_bar = ctk.CTkFrame(main_wrap, height=42, corner_radius=0, fg_color=SIDEBAR_BG)
status_bar.grid(row=2, column=0, sticky="ew")
status_bar.grid_propagate(False)

status_left = ctk.CTkFrame(status_bar, fg_color="transparent")
status_left.pack(side="left", fill="y", padx=(22, 0))
ctk.CTkLabel(status_left, text="●", font=ctk.CTkFont(size=10), text_color=TEAL).pack(side="left", padx=(0, 4))
status_lbl = ctk.CTkLabel(status_left, text="Ready", font=ctk.CTkFont(
    family=FONT_FAMILY, size=12), text_color=MUTED)
status_lbl.pack(side="left")

progress_bar = ctk.CTkProgressBar(status_bar, height=6, corner_radius=3,
                                    progress_color=TEAL, fg_color=CARD_BDR)
progress_bar.set(0)
progress_bar.pack(side="right", fill="x", expand=True, padx=22, pady=18)

def set_status(msg):
    _ui_queue.put((UI_COMMAND_STATUS, msg))

def set_progress(val):
    _ui_queue.put((UI_COMMAND_PROGRESS, val))

btn_frame = ctk.CTkFrame(main_wrap, fg_color=SIDEBAR_BG, corner_radius=0)
btn_frame.grid(row=3, column=0, sticky="ew", padx=0, pady=0)

def _run_typing(args):
    try:
        type_text(*args)
    except Exception as exc:
        set_status(f"Typing error: {exc}")

def start_typing():
    """Validate the current controls and start one typing worker."""
    global stop_flag, typing_thread
    if typing_thread is not None and typing_thread.is_alive():
        set_status("Already typing.")
        return
    if not AXIsProcessTrustedWithOptions({'AXTrustedCheckOptionPrompt': False}):
        set_status("Grant Accessibility access, then restart Human Typer.")
        return

    stop_flag = False
    text = text_box.get("1.0", "end-1c")
    if not text:
        set_status("No text to type.")
        return
    try:
        wpm = float(wpm_entry.get())
    except Exception:
        set_status("Invalid WPM value.")
        return
    if not math.isfinite(wpm) or wpm <= 0:
        set_status("WPM must be greater than zero.")
        return

    set_progress(0)
    save_to_history(text)
    typing_args = (
        text, wpm,
        typo_slider.get() / 100,
        bool(adjacent_var.get()),
        cap_slider.get(),
        variance_slider.get() / 100,
        bool(accel_var.get()),
        fatigue_slider.get() / 100,
        punct_var.get(),
        breaks_var.get(),
        breaks_slider.get() if breaks_var.get() else 0,
        background_var.get(),
        countdown_var.get(),
        bool(chunk_var.get()),
        int(chunk_size_slider.get()),
        bool(loop_var.get()),
        int(loop_count_slider.get()),
        int(loop_delay_slider.get()),
        set_status,
        set_progress,
    )
    typing_thread = threading.Thread(
        target=_run_typing, args=(typing_args,), daemon=True)
    typing_thread.start()

def stop_typing():
    """Request a cooperative stop and release a paused chunk worker."""
    global stop_flag
    stop_flag = True
    chunk_resume_event.set()  # unblock any waiting chunk

ctk.CTkButton(btn_frame, text="Start typing", height=44,
               font=ctk.CTkFont(family=FONT_FAMILY, size=14, weight="bold"),
               fg_color=TEAL, hover_color=TEAL_HOVER, text_color=PRIMARY_TEXT,
               corner_radius=10, command=start_typing
               ).pack(side="left", fill="x", expand=True, padx=(22, 8), pady=(4, 16))

ctk.CTkButton(btn_frame, text="Stop", height=44, width=96,
               font=ctk.CTkFont(family=FONT_FAMILY, size=14, weight="bold"),
               fg_color="transparent", hover_color=DANGER_HOVER, text_color=DANGER,
               border_width=1, border_color=DANGER, corner_radius=10, command=stop_typing
               ).pack(side="right", padx=(0, 22), pady=(4, 16))

# ═══════════════════════════════════════════
# SETTINGS PAGE
# ═══════════════════════════════════════════
settings_wrap = ctk.CTkFrame(page_container, fg_color="transparent", corner_radius=0)
settings_wrap.grid(row=0, column=0, sticky="nsew")

settings_scroll = SmoothScrollableFrame(settings_wrap, fg_color="transparent",
                                        corner_radius=0, scrollbar_button_color=SCROLL_THUMB)
settings_scroll.pack(fill="both", expand=True, padx=22, pady=(8, 16))

# ── Appearance ────────────────────────────────────────────────────────────────
section_lbl(settings_scroll, "APPEARANCE")
app_card = card(settings_scroll)
app_card.pack(fill="x", pady=(0, 4))
appearance_row = ctk.CTkFrame(app_card, fg_color="transparent")
appearance_row.pack(fill="x", padx=16, pady=14)
ctk.CTkLabel(
    appearance_row,
    text="Color Mode",
    font=ctk.CTkFont(size=13),
    text_color=HDR_TEXT,
).pack(side="left")
mode_menu = ctk.CTkOptionMenu(appearance_row, values=["Dark", "Light", "System"],
                               fg_color=CARD_BG, button_color=TEAL, button_hover_color=TEAL_HOVER,
                               dropdown_fg_color=CARD_BG,
                               command=lambda v: ctk.set_appearance_mode(v), width=120)
mode_menu.set("Dark")
mode_menu.pack(side="right")

ctk.CTkFrame(app_card, height=1, fg_color=CARD_BDR).pack(fill="x", padx=10)

window_visibility_row = ctk.CTkFrame(app_card, fg_color="transparent")
window_visibility_row.pack(fill="x", padx=16, pady=14)
ctk.CTkLabel(window_visibility_row, text="Window Visibility", font=ctk.CTkFont(size=13),
             text_color=HDR_TEXT).pack(side="left")
window_mode_menu = ctk.CTkOptionMenu(
    window_visibility_row,
    values=["Dock + Menu Bar", "Dock Only", "Menu Bar Only", "Neither"],
    fg_color=CARD_BG, button_color=TEAL, button_hover_color=TEAL_HOVER,
    dropdown_fg_color=CARD_BG, width=150,
    command=lambda value: apply_window_mode(WINDOW_MODE_VALUES[value])
)
window_mode_menu.set("Dock + Menu Bar")
window_mode_menu.pack(side="right")
ctk.CTkLabel(app_card,
    text="  Menu Bar mode adds a ⌨ icon in the menu bar. "
         "Use 'Neither' if you rely solely on global hotkeys.",
    font=ctk.CTkFont(size=11), text_color=MUTED,
    anchor="w", wraplength=400, justify="left"
).pack(anchor="w", padx=16, pady=(0, 12))

# ── Hotkeys info ──────────────────────────────────────────────────────────────
section_lbl(settings_scroll, "GLOBAL HOTKEYS")
hk_card = card(settings_scroll)
hk_card.pack(fill="x", pady=(0, 4))

def hk_row(parent, key, desc):
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", padx=16, pady=6)
    ctk.CTkLabel(row, text=key, font=ctk.CTkFont(size=12, weight="bold"),
                 text_color=TEAL, width=130, anchor="w").pack(side="left")
    ctk.CTkLabel(row, text=desc, font=ctk.CTkFont(size=12),
                 text_color=HDR_TEXT).pack(side="left")

ctk.CTkFrame(hk_card, height=6, fg_color="transparent").pack()
hk_row(hk_card, "Ctrl + Option + H", "Start typing")
hk_row(hk_card, "Ctrl + Option + S", "Stop typing")
hk_row(hk_card, "Ctrl + Option + Space", "Resume chunk (when paused)")
ctk.CTkFrame(hk_card, height=6, fg_color="transparent").pack()

# ── Presets ───────────────────────────────────────────────────────────────────
section_lbl(settings_scroll, "PRESETS")
presets_card = card(settings_scroll)
presets_card.pack(fill="x", pady=(0, 4))

def get_current_config():
    return {
        "wpm":              wpm_entry.get(),
        "typo":             typo_slider.get(),
        "adjacent":         adjacent_var.get(),
        "cap_intensity":    cap_slider.get(),
        "variance":         variance_slider.get(),
        "fatigue":          fatigue_slider.get(),
        "acceleration":     accel_var.get(),
        "countdown":        countdown_var.get(),
        "punct":            punct_var.get(),
        "background":       background_var.get(),
        "chunk":            chunk_var.get(),
        "chunk_size":       chunk_size_slider.get(),
        "breaks":           breaks_var.get(),
        "breaks_intensity": breaks_slider.get(),
        "loop":             loop_var.get(),
        "loop_count":       loop_count_slider.get(),
        "loop_delay":       loop_delay_slider.get(),
    }

def apply_config(config):
    """Apply a persisted configuration to widgets and dependent UI states."""
    cap_intensity = config.get("cap_intensity", 0)

    wpm_entry.delete(0, "end")
    wpm_entry.insert(0, config.get("wpm", "40"))
    typo_slider.set(config.get("typo", 0))
    typo_val_lbl.configure(text=f"{int(config.get('typo', 0))}%")
    adjacent_var.set(config.get("adjacent", 0))
    cap_slider.set(cap_intensity)
    cap_val_lbl.configure(text=format_cap_intensity(cap_intensity))
    variance_slider.set(config.get("variance", 20))
    fatigue_slider.set(config.get("fatigue", 0))
    accel_var.set(config.get("acceleration", 0))
    countdown_var.set(config.get("countdown", 5))
    punct_var.set(config.get("punct", 0))
    background_var.set(config.get("background", 0))
    chunk_var.set(config.get("chunk", 0))
    chunk_size_slider.set(config.get("chunk_size", 10))
    breaks_var.set(config.get("breaks", 0))
    breaks_slider.set(config.get("breaks_intensity", 20))
    bi_val_lbl.configure(text=str(int(config.get("breaks_intensity", 20))))
    loop_var.set(config.get("loop", 0))
    loop_count_slider.set(config.get("loop_count", 2))
    loop_delay_slider.set(config.get("loop_delay", 5))
    on_breaks_toggle()
    on_bg_toggle()
    on_chunk_toggle()
    on_loop_toggle()
    update_counts()

def refresh_preset_list():
    for widget in preset_list_frame.winfo_children():
        widget.destroy()
    presets = load_presets()
    if not presets:
        ctk.CTkLabel(preset_list_frame, text="No saved presets yet.",
                     font=ctk.CTkFont(size=12), text_color=MUTED).pack(pady=8)
        return
    for name, config in presets.items():
        row = ctk.CTkFrame(preset_list_frame, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=3)
        ctk.CTkLabel(row, text=name, font=ctk.CTkFont(size=13),
                     text_color=HDR_TEXT).pack(side="left")
        ctk.CTkButton(row, text="Load", width=60, height=28, font=ctk.CTkFont(size=12),
                      fg_color=TEAL, hover_color=TEAL_HOVER, text_color="#0b1426",
                      corner_radius=8,
                      command=lambda preset=config: apply_config(preset)).pack(
                          side="right", padx=(4, 0))
        ctk.CTkButton(row, text="Delete", width=60, height=28, font=ctk.CTkFont(size=12),
                      fg_color=("#e05050", "#c03030"), hover_color=("#b83030", "#902020"),
                      corner_radius=8, command=lambda n=name: delete_preset(n)).pack(side="right", padx=(4, 0))

def save_preset():
    name = preset_name_entry.get().strip()
    if not name:
        return
    presets = load_presets()
    presets[name] = get_current_config()
    save_presets(presets)
    preset_name_entry.delete(0, "end")
    refresh_preset_list()

def delete_preset(name):
    presets = load_presets()
    presets.pop(name, None)
    save_presets(presets)
    refresh_preset_list()

save_row = ctk.CTkFrame(presets_card, fg_color="transparent")
save_row.pack(fill="x", padx=14, pady=(12, 6))
preset_name_entry = ctk.CTkEntry(save_row, placeholder_text="Preset name...",
                                  height=34, corner_radius=8, font=ctk.CTkFont(size=13))
preset_name_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
ctk.CTkButton(save_row, text="Save Current", height=34, width=110,
               font=ctk.CTkFont(size=12, weight="bold"),
               fg_color=TEAL, hover_color=TEAL_HOVER, text_color="#0b1426",
               corner_radius=8, command=save_preset).pack(side="right")

ctk.CTkFrame(presets_card, height=1, fg_color=CARD_BDR).pack(fill="x", padx=10)
preset_list_frame = ctk.CTkFrame(presets_card, fg_color="transparent")
preset_list_frame.pack(fill="x", pady=(4, 10))
refresh_preset_list()

# ── Text history ──────────────────────────────────────────────────────────────
section_lbl(settings_scroll, "TEXT HISTORY")
history_card = card(settings_scroll)
history_card.pack(fill="x", pady=(0, 4))

def refresh_history():
    for widget in history_list_frame.winfo_children():
        widget.destroy()
    history = load_history()
    if not history:
        ctk.CTkLabel(history_list_frame, text="No history yet.",
                     font=ctk.CTkFont(size=12), text_color=MUTED).pack(pady=8)
        return
    for entry in history:
        row = ctk.CTkFrame(history_list_frame, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=3)
        preview = entry[:55].replace('\n', ' ') + ("…" if len(entry) > 55 else "")
        ctk.CTkLabel(row, text=preview, font=ctk.CTkFont(size=12),
                     text_color=HDR_TEXT, anchor="w").pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="Load", width=55, height=26, font=ctk.CTkFont(size=11),
                      fg_color=TEAL, hover_color=TEAL_HOVER, text_color="#0b1426",
                      corner_radius=8,
                      command=lambda t=entry: load_history_entry(t)).pack(side="right", padx=(4, 0))

def load_history_entry(text):
    text_box.delete("1.0", "end")
    text_box.insert("1.0", text)
    update_counts()
    show_page("main")

ctk.CTkFrame(history_card, height=6, fg_color="transparent").pack()
history_list_frame = ctk.CTkFrame(history_card, fg_color="transparent")
history_list_frame.pack(fill="x", pady=(0, 8))
refresh_history()

# ── Page switching ────────────────────────────────────────────────────────────
def show_page(name):
    if name == "main":
        main_wrap.tkraise()
        page_title_lbl.configure(text="Type text")
        page_subtitle_lbl.configure(text="Natural rhythm, precise control")
        nav_home_btn.configure(image=_ico_home_active, fg_color=NAV_ACTIVE,
                               text_color=HDR_TEXT)
        nav_gear_btn.configure(image=_ico_gear_idle, fg_color="transparent",
                               text_color=MUTED)
    else:
        settings_wrap.tkraise()
        page_title_lbl.configure(text="Settings")
        page_subtitle_lbl.configure(text="Appearance, shortcuts, and saved presets")
        nav_home_btn.configure(image=_ico_home_idle, fg_color="transparent",
                               text_color=MUTED)
        nav_gear_btn.configure(image=_ico_gear_active, fg_color=NAV_ACTIVE,
                               text_color=HDR_TEXT)
        refresh_preset_list()
        refresh_history()

nav_home_btn.configure(command=lambda: show_page("main"))
nav_gear_btn.configure(command=lambda: show_page("settings"))

# ── Global hotkeys (pynput) ───────────────────────────────────────────────────
# Ctrl + Option + H → Start
# Ctrl + Option + S → Stop
# Ctrl + Option + Space → Resume chunk
_pressed = set()

_CTRL  = {pynput_keyboard.Key.ctrl, pynput_keyboard.Key.ctrl_l, pynput_keyboard.Key.ctrl_r}
_ALT   = {pynput_keyboard.Key.alt,  pynput_keyboard.Key.alt_l,  pynput_keyboard.Key.alt_r}
_H_KEY = pynput_keyboard.KeyCode.from_char('h')
_S_KEY = pynput_keyboard.KeyCode.from_char('s')

def _ctrl_opt():
    return bool(_pressed & _CTRL) and bool(_pressed & _ALT)

def on_key_press(key):
    _pressed.add(key)

    if _ctrl_opt():
        if key == _H_KEY:
            _ui_queue.put(UI_COMMAND_START)
        elif key == _S_KEY:
            stop_typing()
        elif key == pynput_keyboard.Key.space:
            chunk_resume_event.set()

def on_key_release(key):
    _pressed.discard(key)

def _start_hotkey_listener():
    trusted = bool(AXIsProcessTrustedWithOptions({'AXTrustedCheckOptionPrompt': False}))
    if not trusted:
        # Only show the system permission dialog if we haven't prompted before
        settings = _load_settings_raw()
        if not settings.get('accessibility_prompted', False):
            AXIsProcessTrustedWithOptions({'AXTrustedCheckOptionPrompt': True})
            settings['accessibility_prompted'] = True
            _save_settings_raw(settings)
        set_status("Grant Accessibility access, then restart Human Typer.")
        return
    try:
        listener = pynput_keyboard.Listener(
            on_press=on_key_press, on_release=on_key_release, daemon=True)
        listener.start()
    except Exception as exc:
        set_status(f"Global hotkeys unavailable: {exc}")

# Slight delay so window is visible before any permission dialog appears
app.after(500, _start_hotkey_listener)

def _show_window():
    app.deiconify()
    app.update_idletasks()
    app.update()
    app.lift()
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    app.after(50, app.update)  # second pass — ensures content is fully painted

def _on_close():
    save_settings()
    app.withdraw()

def _poll_ui_queue():
    global _pinned

    try:
        while True:
            command = _ui_queue.get_nowait()
            if isinstance(command, tuple) and command[0] == UI_COMMAND_STATUS:
                status_lbl.configure(text=command[1])
            elif isinstance(command, tuple) and command[0] == UI_COMMAND_PROGRESS:
                progress_bar.set(command[1])
            elif command == UI_COMMAND_START:
                start_typing()
            elif command == UI_COMMAND_SHOW:
                _show_window()
            elif command == UI_COMMAND_QUIT:
                _pinned = False
                save_settings()
                app.destroy()
                os._exit(0)
            elif command == UI_COMMAND_TOGGLE_PIN:
                _pinned = not _pinned
                if _pin_item:
                    _pin_item.setState_(1 if _pinned else 0)
    except queue.Empty:
        pass
    app.after(100, _poll_ui_queue)

app.protocol("WM_DELETE_WINDOW", _on_close)

# Dock icon clicked while window is hidden → show it
app.createcommand(
    "::tk::mac::ReopenApplication",
    lambda: _ui_queue.put(UI_COMMAND_SHOW),
)

show_page("main")
app.after(150, load_settings)
app.update_idletasks()
app.update()
_poll_ui_queue()
app.mainloop()
