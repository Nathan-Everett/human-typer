# Human Typer

A macOS app that types text for you — but makes it look human. Built with Python and customtkinter.

## Features

- Adjustable WPM and realistic typing variance
- Unicode text support, including smart punctuation, accents, and emoji
- Typo simulation (random, adjacent-key, capitalization errors)
- Fatigue simulation — gradually slows down like a real person
- Word-level acceleration — faster mid-word, slower at edges
- Human breaks — random pauses and sentence hesitations
- Chunk mode — pause every few words and resume with a hotkey
- Loop / repeat — type the same text multiple times
- Punctuation search delay — hesitates before rare symbols
- Background mode — types into any window even when the app isn't focused
- Countdown timer before typing starts
- Menu bar support — pin to menu bar and run in the background
- Presets — save and load your favorite settings
- Text history — quickly reload recently typed text
- Load from file — import any `.txt` file

## Requirements

- macOS (uses Quartz and AppKit for background typing and menu bar)
- Python 3.9+

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python3 human_typer.py
```

On first launch, allow the Python app under **System Settings → Privacy &
Security → Accessibility**, then restart Human Typer. macOS blocks simulated
keystrokes until this permission is granted.

### Global Hotkeys

| Hotkey | Action |
|---|---|
| Ctrl + Option + H | Start typing |
| Ctrl + Option + S | Stop typing |
| Ctrl + Option + Space | Resume chunk (when paused) |

## Bugs & Feedback

This app is still in active development and may have bugs. If you run into any issues or have suggestions, feel free to open an issue on GitHub — any feedback is appreciated!

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for the source layout, threading model,
persistence format, scroll behavior, and verification checklist.
