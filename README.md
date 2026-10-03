# MazCro

A Windows macro recorder and player with a graphical UI, written in Python.

MazCro records your mouse and keyboard activity against a **specific target
application**, then replays it — either on demand, via a hotkey, or automatically
the moment you Alt+Tab back to that app.

![MazCro](screenshot.png)

## Features

### Target application binding

- Dropdown of every visible window, auto-refreshing every second
- Macros remember their target's process name and window-title regex
- Multiple windows of the same app are matched by title
- Playback **refuses to start** if the target has closed, and aborts immediately
  if it closes mid-run
- Optional **auto-trigger**: Alt+Tab to the target and the macro fires by itself

### Actions

| Action | Fields | Notes |
| --- | --- | --- |
| `wait` | `duration` | explicit pause, in seconds |
| `click` | `x`, `y`, `button`, `hold_time` | hold is in milliseconds |
| `double_click` | `x`, `y`, `button` | |
| `move` | `x`, `y`, `duration` | glides over `duration` seconds |
| `type` | `text` | supports `${variable}` substitution |
| `key` | `key`, `hold_time` | single press, e.g. `Return`, `tab` |
| `hold_key` | `key`, `hold_time` | holds then always releases |
| `scroll` | `x`, `y`, `amount` | positive scrolls up |
| `screenshot` | `path` | optional, for verification |

### Timing

- **Speed slider, 0.5x – 2.0x**, applied to recorded gaps and explicit waits
- Configurable **hold time** (default 50 ms) and **between-action wait** (default 10 ms)
- Recorded timing gaps between actions are preserved and scaled

### Variables

Define name/value pairs and reference them as `${name}` inside any `type` action.
Values are stored in the macro, and can be overridden at playback time through a
prompt dialog.

### Reliability

- **Bounds checking** — clicks outside the desktop are clamped to a safe inset
  position, so a macro recorded on one monitor still works on another
- **Key validation** against `pynput`'s key table before a run begins
- **Watchdog** aborts playback if it overruns 5x its own estimate
- **Multi-monitor aware** via `GetSystemMetrics` virtual-screen metrics
- **Fail-safe** — move the mouse into a screen corner to abort instantly

### Interface

Target picker, recording controls, playback controls with speed slider, saved
macro list, variables editor, hotkey assignment, and a read-only log showing the
last 20 events.

## Requirements

- Windows (primary target). macOS and Linux run, but without window awareness.
- Python 3.10+

## Installation

```powershell
git clone https://github.com/Mazt08/MazCro.git
cd MazCro
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Usage

```powershell
.\.venv\Scripts\python.exe main.py
```

### Recording a macro

1. Pick your target application from the **Select Target Application** dropdown
2. Click **Record**
3. Perform your actions — every click, keystroke, scroll and mouse move is captured
4. Press **Esc** or click **Stop**

Modifiers are not recorded separately; they are carried by the key they modify.
Mouse movement is captured but you can turn it off with the **Record mouse moves**
checkbox if you only want clicks.

### Playing it back

1. Set the **Speed** slider
2. Choose an error policy: **stop** on the first failure, or **skip** and continue
3. Click **Play**

There is a short 800 ms delay before playback begins so you can switch to the
target window. Use **Preview** first to see the action list without moving the mouse.

### Alt+Tab auto-trigger

Tick **Auto-trigger on Alt+Tab**. A background thread watches the foreground
window and starts the macro the moment your target app gains focus. It fires once
per activation, not continuously, and does nothing while you are working in
another window.

### Hotkey

Click **Set Hotkey**, then press the key you want (5 second timeout). The hotkey
only fires while the target window is active — so you cannot trigger a macro by
accidentally pressing the key while typing elsewhere. **Esc** cancels instead of
assigning.

### Variables

Click **Edit variables**, add name/value rows, then use `${name}` in a type action.
On playback you are prompted to confirm or change each value first.

### Macro files

Macros are saved as JSON in `~/macros/`. You can also **Import** and **Export**
them to share or back up.

```json
{
  "name": "Login Automation",
  "target_app": "chrome.exe",
  "target_window_title": "Gmail",
  "created": "2025-01-01T10:00:00",
  "last_modified": "2025-01-01T10:00:00",
  "playback_speed": 1.0,
  "variables": { "username": "user@example.com", "password": "secret" },
  "actions": [
    { "type": "wait", "delay": 0, "duration": 0.5 },
    { "type": "click", "delay": 0.2, "x": 250, "y": 150, "button": "left", "hold_time": 50 },
    { "type": "type", "delay": 0.1, "text": "${username}" },
    { "type": "key", "delay": 0.1, "key": "Return", "hold_time": 50 }
  ]
}
```

`delay` is the recorded gap **before** that action, in seconds. Dividing it by the
speed factor is what makes 2x playback twice as fast.

### Logging

Everything is written to `~/macro_errors.log`, rotated at 2 MB with the 5 most
recent files kept. The in-app log panel mirrors the last 20 lines.

## Architecture

| Module | Responsibility |
| --- | --- |
| `main.py` | Tkinter GUI, hotkey manager, thread lifecycle |
| `macro_model.py` | `Macro`, `Action`, `Variable`, `WindowTarget` dataclasses |
| `macro_store.py` | JSON persistence and the rotating log |
| `recorder.py` | pynput listeners producing `Action` objects |
| `player.py` | playback thread, timing, validation, watchdog |
| `window_manager.py` | window enumeration, foreground monitor, screen metrics |

Recording, playback, window monitoring and hotkey capture each run on their own
daemon thread. The UI thread only receives results through Tkinter's `after`
queue, so the window never freezes.

## Building a standalone EXE

### Downloading a prebuilt EXE

Go to the [Releases page](https://github.com/Mazt08/MazCro/releases) and download the
latest `MazCro-*.zip`. Unzip it and double-click `MazCro.exe` — no Python needed. A
SHA256 checksum file is published with each release.

### Building it yourself

| Spec | Result | Best for |
| --- | --- | --- |
| `MazCro-onefile.spec` | one `MazCro.exe` (~13 MB) | distributing to other people |
| `MazCro.spec` | `dist\MazCro\MazCro.exe` plus `_internal\` (~4 MB) | faster startup |

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller

# single file, easiest to share
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm MazCro-onefile.spec

# or folder build, starts faster
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm MazCro.spec
```

Both specs list `pynput`'s and `pyautogui`'s win32 backends as hidden imports,
because both libraries choose their platform backend at runtime and would
otherwise be stripped out — producing an app that opens and then crashes the
moment you press Record.

## Troubleshooting

| Problem | Cause and fix |
| --- | --- |
| Record button reports it cannot start | Global input capture needs matching privileges. Run MazCro as administrator. |
| Playback says the target is not running | The target window is closed. Reopen it, or clear the target to play anywhere. |
| Nothing happens on Alt+Tab | Auto-trigger is off, the macro has no actions, or focus never left the target. |
| Hotkey does nothing | The hotkey only fires while the target window is active. |
| Playback stops unexpectedly | Check `~/macro_errors.log`; a failing action is logged with a full traceback. |
| Clicks land in the wrong place | Resolution or window layout changed. Re-record, or loosen the title regex. |

## Disclaimer

Recording and replaying input can violate the terms of service of online games
and applications, and may be detected by anti-cheat systems. Some antivirus tools
flag applications that inject input as suspicious. Use MazCro only for automation
you are permitted to automate.

## License

MIT
