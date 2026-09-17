# Human Typer development guide

Human Typer is a deliberately small, macOS-only desktop application. The app
currently lives in one Python module because CustomTkinter builds widget trees
imperatively and many callbacks share widget state. Keeping the construction in
one module avoids a large dependency-injection layer while the project is this
size.

## Source map

`human_typer.py` is arranged in the order a new developer should read it:

1. Application paths, command names, and runtime state.
2. Native Quartz keyboard-event helpers.
3. Humanization and typing-loop logic.
4. JSON persistence for history, presets, and settings.
5. `SmoothScrollableFrame`, including macOS momentum and nested-scroll rules.
6. Visual tokens and sequential construction of the main page.
7. Sequential construction of the settings page.
8. Typing-worker, hotkey, AppKit, and Tk event coordination.

The website is the standalone `index.html`. The application and website both
use `human_typer_icon_hd.png`.

## Threading model

Tk widgets may only be read or updated on the main thread.

- `start_typing()` reads the controls and starts one daemon worker.
- `type_text()` performs timing and native keyboard delivery on that worker.
- Status and progress updates enter `_ui_queue` through `set_status()` and
  `set_progress()`.
- `_poll_ui_queue()` applies those updates from Tk's main event loop.
- AppKit menu actions and global hotkeys also enqueue commands instead of
  invoking Tk directly.
- `stop_flag` requests a cooperative stop. `chunk_resume_event` releases a
  worker paused by chunk mode.

Preserve this boundary when adding callbacks. Calling a widget method directly
from the typing worker, AppKit delegate, or hotkey listener can cause intermittent
macOS crashes.

## Typing targets

Normal mode posts Quartz keyboard events through the HID event tap. Background
mode captures the frontmost process after the countdown and posts directly to
that process. Unicode strings use UTF-16 code-unit lengths because that is what
`CGEventKeyboardSetUnicodeString` expects.

## Persistence

The app stores user data in the home directory:

- `~/.humantyper_settings.json`
- `~/.humantyper_presets.json`
- `~/.humantyper_history.json`

Settings and presets are plain JSON so they remain inspectable and backward
compatible. New configuration keys should have defaults in `apply_config()`.

## Scroll behavior

`SmoothScrollableFrame` owns page scrolling. It clamps macOS momentum events at
the top and bottom to prevent bounce-induced shaking. Nested scrollable widgets
set `_blocks_parent_scroll`; this lets the text editor consume wheel events
without also moving the page.

## Verification

Run these checks after a change:

```bash
python3 -m py_compile human_typer.py
python3 -m pip check
git diff --check
python3 human_typer.py
```

For typing changes, test normal and background modes with ASCII, smart
punctuation, accented characters, and emoji. macOS Accessibility permission is
required for actual keyboard delivery and global hotkeys.
