"""Playback engine for MazCro.

Runs a macro on a worker thread with:

* variable substitution (``${name}`` placeholders in ``type`` actions)
* speed scaling, applied to both recorded gaps and explicit waits
* per-action holds and an inter-action delay
* pre-flight validation — target window still open, coordinates on screen,
  key names known to pynput
* a watchdog that aborts playback if it overruns its own estimate
* immediate abort if the target window disappears mid-run

Failures are logged with a full traceback and, depending on
``on_error_policy``, either skip the offending action or abort the run.
"""

from __future__ import annotations

import threading
import time
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from macro_model import Action, Macro
from macro_store import log_exception, log_info, log_warning
from window_manager import ScreenInfo, find_window, get_screen_info, window_exists

#: Keys that cannot be sent with pyautogui/pynput but are valid input names.
NON_TYPEABLE_KEYS: frozenset[str] = frozenset(
    {
        "shift", "shift_l", "shift_r", "ctrl", "ctrl_l", "ctrl_r",
        "alt", "alt_l", "alt_r", "alt_gr", "cmd", "cmd_l", "cmd_r",
        "caps_lock", "num_lock", "scroll_lock", "print_screen",
        "pause", "insert", "menu",
    }
)

#: Playback aborts if it exceeds this multiple of its own estimate.
TIMEOUT_FACTOR = 5.0
#: Absolute floor for the watchdog so tiny macros are not killed by jitter.
TIMEOUT_FLOOR = 10.0

#: Alternative spellings accepted for input even though pynput has no matching
#: enum entry. The documented JSON schema uses names like "Return", so these
#: must survive validation rather than being rejected as typos.
KEY_ALIASES: dict[str, str] = {
    "return": "enter",
    "esc": "escape",
    "del": "delete",
    "ins": "insert",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "pgdown": "pagedown",
    "win": "winleft",
    "cmd": "command",
    "control": "ctrl",
    "apps": "menu",
}

#: Modifier spellings a ``key_press`` action may carry. Anything else is
#: rejected rather than silently pressed, because an unknown modifier name
#: would otherwise leave a key stuck down.
MODIFIER_KEYS: frozenset[str] = frozenset(
    {"ctrl", "shift", "alt", "win", "command", "cmd", "winleft", "winright"}
)

#: Punctuation and symbols offered in the key dropdown and accepted by
#: playback. pyautogui lists these as valid key names, so they can be sent
#: directly -- but note that on a US layout most of them are Shift+<digit>
#: rather than a key of their own. They are stored exactly as written; see
#: :func:`warn_about_layout_symbols`.
SYMBOL_KEYS: tuple[str, ...] = tuple(
    "`~!@#$%^&*()-_=+[]{}|;:'\",.<>/?\\"
)

#: Numpad keys. pyautogui spells these without an underscore (``num5``,
#: ``numlock``), while pynput uses ``num_5``; :func:`_normalize_numpad` bridges
#: the two so a recorded macro replays regardless of which produced it.
NUMPAD_KEYS: tuple[str, ...] = (
    "num0", "num1", "num2", "num3", "num4", "num5",
    "num6", "num7", "num8", "num9", "numlock",
    "add", "subtract", "multiply", "divide", "decimal",
)

#: Alternate spellings normalised to the names pyautogui expects.
EXTRA_KEY_ALIASES: dict[str, str] = {
    "num_0": "num0", "num_1": "num1", "num_2": "num2", "num_3": "num3",
    "num_4": "num4", "num_5": "num5", "num_6": "num6", "num_7": "num7",
    "num_8": "num8", "num_9": "num9", "num_lock": "numlock",
    "decimal_sep": "decimal",
    "pgdn": "pagedown", "pgup": "pageup", "pgdown": "pagedown",
    "page_up": "pageup", "page_down": "pagedown",
    "caps": "caps_lock", "prtsc": "print_screen",
    "menu_key": "menu", "space_bar": "space",
}


#: Human-readable name for each action kind, used by the preview table.
TYPE_LABELS: dict[str, str] = {
    "click": "Mouse Click",
    "mouse_click": "Mouse Click",
    "double_click": "Double Click",
    "move": "Mouse Move",
    "scroll": "Scroll",
    "key": "Key Press",
    "key_press": "Key Press",
    "hold_key": "Key Press",
    "type": "Text Input",
    "text_input": "Text Input",
    "wait": "Wait",
    "screenshot": "Screenshot",
}


def _import_input() -> tuple[Any, Any] | None:
    """Import pyautogui and pynput lazily, returning None if unavailable."""
    try:
        import pyautogui
        from pynput import keyboard

        pyautogui.FAILSAFE = True
        return pyautogui, keyboard
    except Exception:  # noqa: BLE001
        log_exception("pyautogui/pynput are not importable; playback is disabled")
        return None


def valid_key_names() -> set[str]:
    """Return every key name pynput can emit, lowercased.

    Used to validate ``key`` and ``hold_key`` actions before a run so an
    invalid name fails fast instead of halfway through playback.
    """
    try:
        from pynput import keyboard

        return {key.name.lower() for key in keyboard.Key}
    except Exception:  # noqa: BLE001
        log_warning("could not enumerate pynput key names")
        return set()


def available_key_names() -> list[str]:
    """Return a sorted, GUI-friendly list of key names.

    Combines pynput's ``Key`` enum with single printable characters and the
    documented aliases, so the Add Action dropdown only ever offers keys that
    playback will accept.
    """
    names = set(valid_key_names())
    if not names:
        log_warning("could not enumerate pynput key names; using the built-in list")
    names |= set(KEY_ALIASES)
    names |= set(character for character in string.ascii_lowercase)
    names |= set(string.digits)
    names |= {
        "space", "enter", "tab", "backspace", "escape", "delete",
        "home", "end", "pageup", "pagedown", "up", "down", "left", "right",
    }
    names |= {f"f{n}" for n in range(1, 13)}
    # Symbols and numpad keys round out the dropdown.
    names |= set(SYMBOL_KEYS)
    names |= set(NUMPAD_KEYS)
    names |= {"`"}
    return sorted(names)


def warn_about_layout_symbols(action: Action) -> None:
    """Log a one-off note when a symbol key is sent as a bare press.

    Symbols are stored and replayed exactly as written. On a US layout most of
    them (``!``, ``@``, ``#``...) are really Shift+<digit>, so a bare press may
    produce nothing. This is a log-only heads-up: the user asked for symbols
    to be sent literally rather than rewritten into modifier combos.
    """
    if action.kind not in ("key", "key_press", "hold_key"):
        return
    name = action.key
    if len(name) == 1 and name in SYMBOL_KEYS:
        log_warning(
            f"symbol key {name!r} is being sent as a bare press; on a US layout "
            f"it is produced by Shift+<digit>. Add Shift explicitly if nothing appears."
        )


@dataclass
class _PassResult:
    """How one pass through the action list ended."""

    completed: int
    aborted: str = ""
    skipped: list[str] = field(default_factory=list)


@dataclass
class PlaybackResult:
    """Outcome of one playback run."""

    ok: bool
    message: str
    completed: int = 0
    total: int = 0
    duration: float = 0.0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"{self.message} ({self.completed}/{self.total} actions, {self.duration:.1f}s)"


class Player(threading.Thread):
    """Executes a :class:`~macro_model.Macro` on a background thread."""

    def __init__(
        self,
        macro: Macro,
        speed: float = 1.0,
        between_action_ms: int = 10,
        on_finish: Callable[[PlaybackResult], None] | None = None,
        on_log: Callable[[str], None] | None = None,
        variable_overrides: dict[str, str] | None = None,
        on_error_policy: str = "stop",
        require_target_window: bool = True,
        refocus: bool = True,
        loop_count: int = 1,
        loop_interval: float = 0.0,
    ) -> None:
        super().__init__(name="MazCroPlayer", daemon=True)
        self.macro = macro
        self.speed = max(float(speed), 0.05)
        self.between_action_ms = max(int(between_action_ms), 0)
        self.on_finish = on_finish
        self.on_log = on_log
        self.variable_overrides = variable_overrides or {}
        self.on_error_policy = on_error_policy if on_error_policy in ("stop", "skip") else "stop"
        self.require_target_window = require_target_window
        self.refocus = refocus
        #: 1 means "play once"; 0 means "repeat until stopped".
        self.loop_count = max(int(loop_count), 0)
        #: Seconds to wait between loop repetitions.
        self.loop_interval = max(float(loop_interval), 0.0)

        self._stop_event = threading.Event()
        self._screen: ScreenInfo = get_screen_info()
        self._keys = self._valid_keys()

    # ------------------------------------------------------------ properties
    @property
    def is_playing(self) -> bool:
        return self.is_alive() and not self._stop_event.is_set()

    def _valid_keys(self) -> set[str]:
        names = valid_key_names()
        if not names:
            # Keep going with a permissive set rather than rejecting everything.
            return {"enter", "return", "tab", "escape", "esc", "space", "backspace"}
        return names

    # ---------------------------------------------------------------- public
    def stop(self) -> None:
        """Request an immediate stop. Safe from any thread, any time."""
        self._stop_event.set()

    def cancel(self) -> None:
        """Alias for :meth:`stop`."""
        self.stop()

    # -------------------------------------------------------------- lifecycle
    def run(self) -> None:  # noqa: PLR0912, PLR0915 - inherent to a state machine
        started = time.monotonic()
        actions = self.macro.resolved_actions(self.variable_overrides)
        total = len(actions)
        errors: list[str] = []
        completed = 0

        result = PlaybackResult(ok=False, message="", completed=0, total=total)

        if not actions:
            result.message = "macro has no actions"
            result.duration = 0.0
            self._finish(result)
            return

        inputs = _import_input()
        if inputs is None:
            result.message = "pyautogui is unavailable; install requirements.txt"
            self._finish(result)
            return
        pyautogui = inputs[0]

        # Unresolved ${name} placeholders would be typed literally into the
        # target application, so fail before touching the screen.
        available = set(self.macro.variable_map()) | set(self.variable_overrides)
        missing = self.macro.missing_variables(available)
        if missing:
            detail = "no value for " + ", ".join(f"${{{n}}}" for n in missing)
            result.message = detail
            result.errors.append(detail)
            self._log(f"ABORT: {detail}")
            self._finish(result)
            return

        # ---- pre-flight validation ------------------------------------
        if self.require_target_window:
            ok, detail = self._preflight()
            if not ok:
                result.message = detail
                result.errors.append(detail)
                self._log(f"ABORT: {detail}")
                self._finish(result)
                return
            self._log(f"pre-flight ok: {detail}")

        repetitions = self.loop_count if self.loop_count else None
        passes = 0
        aborted = ""
        while repetitions is None or passes < repetitions:
            if passes > 0:
                self._log(f"loop {passes} of {repetitions or '∞'} done, repeating...")
                if self.loop_interval > 0 and self._wait(self.loop_interval):
                    aborted = "stopped by user"
                    break
            passes += 1
            pass_result = self._run_once(actions, pyautogui, started, passes)
            completed += pass_result.completed
            errors.extend(pass_result.skipped)
            if pass_result.aborted:
                aborted = pass_result.aborted
                break

        elapsed = time.monotonic() - started
        result.completed = completed
        result.total = total if repetitions in (None, 1) else total * passes
        result.errors = errors
        result.duration = elapsed

        if aborted:
            result.ok = False
            result.message = aborted
        elif errors:
            result.ok = True
            result.message = f"completed with {len(errors)} skipped action(s)"
        else:
            result.ok = True
            result.message = "completed"
            if repetitions not in (None, 1):
                result.message += f" ({passes}x)"

        self._finish(result)

    def _run_once(
        self, actions: list[Action], pyautogui: Any, started: float, pass_number: int
    ) -> "_PassResult":
        """Execute the action list once, returning how that pass ended.

        Split out of :meth:`run` so looping re-enters a clean, testable unit
        rather than duplicating the whole action loop inside a ``while``.
        """
        aborted = ""
        skipped: list[str] = []
        completed = 0

        self._screen = get_screen_info()
        # Each pass gets its own watchdog budget measured from the moment it
        # began, so a long loop never trips a whole-run time limit and a single
        # slow pass still gets aborted.
        pass_started = time.monotonic()
        budget = max(
            self.macro.expected_duration(self.speed) * TIMEOUT_FACTOR, TIMEOUT_FLOOR
        )
        deadline = pass_started + budget
        between = self.between_action_ms / 1000.0

        for index, action in enumerate(actions, start=1):
            if self._stop_event.is_set():
                aborted = "stopped by user"
                break

            if time.monotonic() > deadline:
                aborted = f"timeout: exceeded {TIMEOUT_FACTOR:.0f}x the estimated runtime"
                log_warning(f"playback watchdog fired for macro {self.macro.name!r}")
                break

            # Recorded gap, scaled by speed.
            if action.delay > 0:
                if self._wait(action.delay / self.speed):
                    aborted = "stopped by user"
                    break

            # The target window must stay open for the whole run.
            if self.require_target_window and index % 5 == 1:
                if not self._target_still_open():
                    aborted = "target window closed during playback"
                    log_error_target(self.macro, aborted)
                    break

            try:
                self._execute(action, pyautogui)
                completed += 1
            except Exception as exc:  # noqa: BLE001
                message = f"action {index} ({action.kind}) failed: {exc}"
                skipped.append(message)
                log_exception(f"playback failure in macro {self.macro.name!r}: {message}")
                self._log(f"ERROR: {message}")
                if self.on_error_policy == "stop":
                    aborted = f"aborted at action {index} ({action.kind})"
                    break
            else:
                if between > 0 and self._wait(between):
                    aborted = "stopped by user"
                    break

        return _PassResult(completed=completed, aborted=aborted, skipped=skipped)

    # ------------------------------------------------------------ validation
    def _preflight(self) -> tuple[bool, str]:
        """Check that at least one target window exists and the macro is sane."""
        targets = self.macro.all_targets()
        if not any(
            t.process_name or t.title or t.title_pattern for t in targets
        ):
            return True, "no target window required for this macro"

        labels: list[str] = []
        for target in targets:
            if not (target.process_name or target.title or target.title_pattern):
                continue
            win = find_window(target)
            if win is None:
                continue
            if win.handle and not window_exists(win.handle):
                continue
            labels.append(win.process_label)
            if self.refocus and not _is_foreground(win.handle):
                focus_window_safe(win.handle)
            if len(labels) == 1 and not self.macro.extra_targets:
                return True, f"target window ready ({win.process_label})"

        if not labels:
            wanted = targets[0].process_name or targets[0].title
            return False, f"target application {wanted!r} is not running"
        return True, "target window ready (" + ", ".join(labels[:3]) + ")"

    def _target_still_open(self) -> bool:
        """Re-check that at least one target window is still open."""
        for target in self.macro.all_targets():
            if not (target.process_name or target.title or target.title_pattern):
                return True
            win = find_window(target)
            if win is not None:
                # Update the cached handle: windows get recreated (dialogs, tabs).
                if win.handle and win.handle != target.handle:
                    target.handle = win.handle
                return True
        return False

    def _validate_position(self, action: Action) -> tuple[int, int]:
        """Bounds-check an action's coordinates against the virtual desktop."""
        x, y = action.x, action.y
        if self._screen.contains(x, y):
            return x, y
        clamped = self._screen.clamp(x, y)
        log_warning(
            f"action coordinates ({x}, {y}) are outside the desktop "
            f"{self._screen.width}x{self._screen.height}; clamped to {clamped}"
        )
        return clamped

    def _validate_key(self, key: str) -> str:
        """Normalise and validate a key name, returning the pyautogui spelling.

        Accepts a key if pynput knows it, if it is a documented alias
        (``Return``, ``Esc``, ``PgDn``...) or if it is a modifier that cannot
        be typed but can be held down. The returned name always uses the
        spelling pyautogui expects, which differs from pynput's for a few keys
        (pynput says ``esc`` and ``del``; pyautogui wants ``escape`` and
        ``delete``).
        """
        if not key or not key.strip():
            raise ValueError("no key name supplied")
        lowered = key.strip().lower()

        if lowered in KEY_ALIASES:
            return KEY_ALIASES[lowered]
        numpad = EXTRA_KEY_ALIASES.get(lowered)
        if numpad is not None:
            return numpad
        # A single printable character is not in pynput's Key enum (which only
        # lists named keys), but it is a perfectly valid thing to press, and the
        # Add Action key dropdown offers exactly these.
        if len(lowered) == 1 and lowered.isprintable():
            return lowered
        if lowered in self._keys or lowered in NON_TYPEABLE_KEYS:
            return lowered
        if lowered in SYMBOL_KEYS or lowered in NUMPAD_KEYS:
            return lowered
        raise ValueError(f"unknown key name {key!r}")

    # -------------------------------------------------------------- execution
    def _execute(self, action: Action, pyautogui: Any) -> None:
        """Run a single action."""
        kind = action.kind

        if kind == "wait":
            self._wait(action.duration / self.speed)

        elif kind == "move":
            x, y = self._validate_position(action)
            duration = action.duration / self.speed
            if duration > 0:
                pyautogui.moveTo(x, y, duration=duration)
            else:
                pyautogui.moveTo(x, y)

        elif kind in ("click", "mouse_click"):
            x, y = self._validate_position(action)
            button = action.button if action.button in ("left", "right", "middle") else "left"
            hold = max(action.hold_time, 0) / 1000.0
            pyautogui.moveTo(x, y)
            if hold:
                pyautogui.mouseDown(button=button)
                self._wait(hold)
                pyautogui.mouseUp(button=button)
            else:
                pyautogui.click(button=button, x=x, y=y)

        elif kind == "double_click":
            x, y = self._validate_position(action)
            pyautogui.doubleClick(x=x, y=y, button=action.button or "left")

        elif kind == "type":
            if not action.text:
                raise ValueError("type action has no text")
            # interval is in seconds; a floor keeps typing from stalling.
            interval = min(max(0.001, 0.01 / self.speed), 0.05)
            pyautogui.typewrite(action.text, interval=interval)

        elif kind == "text_input":
            self._type_text(action, pyautogui)

        elif kind == "key":
            name = self._validate_key(action.key)
            hold = max(action.hold_time, 0) / 1000.0
            if hold:
                pyautogui.keyDown(name)
                self._wait(hold)
                pyautogui.keyUp(name)
            else:
                pyautogui.press(name)

        elif kind == "key_press":
            self._press_combo(action, pyautogui)

        elif kind == "hold_key":
            name = self._validate_key(action.key)
            seconds = max(action.hold_time, 1) / 1000.0
            pyautogui.keyDown(name)
            try:
                self._wait(seconds)
            finally:
                # Always release, even if the wait was interrupted; otherwise
                # the key stays logically stuck down for the whole session.
                pyautogui.keyUp(name)

        elif kind == "scroll":
            x, y = self._validate_position(action)
            pyautogui.scroll(action.amount, x=x, y=y)

        elif kind == "screenshot":
            self._take_screenshot(action, pyautogui)

        else:
            raise ValueError(f"unsupported action type {kind!r}")

    def _type_text(self, action: Action, pyautogui: Any) -> None:
        """Type ``action.text`` one character at a time, pausing between keys.

        Typing character by character (rather than one bulk ``typewrite``) is
        what makes the action replay faithfully: applications with key-driven
        autocomplete need to see the real inter-key gaps, and a stop request
        can interrupt between characters instead of after the whole string.

        Newlines are sent as ``enter`` presses because pyautogui's typewrite
        emits them inconsistently across platforms.
        """
        text = action.text
        if not text:
            raise ValueError("text_input action has no text")

        interval = max(action.delay_per_char, 0) / 1000.0 / self.speed
        for index, char in enumerate(text):
            if self._stop_event.is_set():
                return
            if index:
                if self._wait(interval):
                    return
            if char == "\n":
                pyautogui.press("enter")
            elif char == "\t":
                pyautogui.press("tab")
            else:
                pyautogui.typewrite(char)

    def _press_combo(self, action: Action, pyautogui: Any) -> None:
        """Press modifiers plus the main key, hold, then release everything.

        Release happens in a ``finally`` block in reverse order: if the wait is
        interrupted, or the key raises, the modifiers must still come back up or
        the keyboard stays logically stuck for the rest of the session.
        """
        key_name = self._validate_key(action.key)
        warn_about_layout_symbols(action)

        pressed: list[str] = []
        try:
            for modifier in action.modifiers:
                name = modifier.strip().lower()
                if name not in MODIFIER_KEYS:
                    raise ValueError(f"unknown modifier {modifier!r}")
                resolved = KEY_ALIASES.get(name, name)
                pyautogui.keyDown(resolved)
                pressed.append(resolved)

            hold = max(action.hold_time, 0) / 1000.0 / self.speed
            pyautogui.keyDown(key_name)
            pressed.append(key_name)
            if hold:
                self._wait(hold)
        finally:
            for name in reversed(pressed):
                try:
                    pyautogui.keyUp(name)
                except Exception:  # noqa: BLE001
                    log_exception(f"could not release {name!r}")

    def _take_screenshot(self, action: Action, pyautogui: Any) -> None:
        """Capture the screen to ``action.path`` or the macro folder."""
        if action.path:
            destination = Path(action.path).expanduser()
        else:
            from macro_store import macros_dir

            stamp = time.strftime("%Y%m%d-%H%M%S")
            destination = macros_dir() / f"screenshot-{stamp}.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        pyautogui.screenshot(str(destination))
        self._log(f"screenshot saved to {destination}")

    # --------------------------------------------------------------- helpers
    def _wait(self, seconds: float) -> bool:
        """Sleep, returning True if a stop was requested.

        Uses the stop event as the sleep primitive so a stop request interrupts
        the delay immediately instead of after the full duration.
        """
        if seconds <= 0:
            return self._stop_event.is_set()
        return self._stop_event.wait(seconds)

    def _log(self, message: str) -> None:
        if self.on_log is not None:
            try:
                self.on_log(message)
            except Exception:  # noqa: BLE001
                log_exception("player log callback raised")

    def _finish(self, result: PlaybackResult) -> None:
        """Report the outcome, guarding against a raising callback."""
        if result.ok:
            log_info(f"playback of {self.macro.name!r}: {result.summary()}")
        else:
            log_warning(f"playback of {self.macro.name!r}: {result.summary()}")
            for detail in result.errors:
                log_warning(f"  detail: {detail}")

        if self.on_finish is not None:
            try:
                self.on_finish(result)
            except Exception:  # noqa: BLE001
                log_exception("player finish callback raised")


def _is_foreground(handle: int) -> bool:
    """Return True if ``handle`` is the foreground window."""
    import ctypes
    import platform

    if platform.system() != "Windows" or not handle:
        return False
    try:
        return int(ctypes.windll.user32.GetForegroundWindow()) == int(handle)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


def focus_window_safe(handle: int) -> bool:
    """Bring a window to the foreground by handle, swallowing errors."""
    import ctypes
    import platform

    if platform.system() != "Windows" or not handle:
        return False
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.ShowWindow(handle, 9)  # SW_RESTORE
        return bool(user32.SetForegroundWindow(handle))
    except Exception:  # noqa: BLE001
        log_exception("could not refocus the target window")
        return False


def log_error_target(macro: Macro, message: str) -> None:
    """Log a playback abort caused by the target window disappearing."""
    log_warning(f"macro {macro.name!r}: {message}")


def dry_run(macro: Macro, overrides: dict[str, str] | None = None) -> list[str]:
    """Return a human-readable plan of what a macro would do.

    Used by the GUI's preview button so a macro can be checked without
    actually moving the mouse.
    """
    lines: list[str] = []
    merged = macro.variable_map()
    merged.update(overrides or {})
    lines.append(f"{'#':>3}  {'Type':<12} {'Detail':<44} {'Sleep':>6} {'Hold':>6}")
    lines.append("-" * 76)
    for index, action in enumerate(macro.actions, start=1):
        resolved = Action.from_dict(action.to_dict()).substitute(merged)
        label = TYPE_LABELS.get(resolved.kind, resolved.kind)
        hold = (
            f"{resolved.hold_time}"
            if resolved.kind in ("click", "mouse_click", "key", "key_press",
                                 "hold_key", "double_click")
            else "-"
        )
        detail = resolved.describe()
        if len(detail) > 44:
            detail = detail[:41] + "..."
        lines.append(
            f"{index:>3}  {label:<12} {detail:<44} "
            f"{int(round(resolved.delay * 1000)):>6} {hold:>6}"
        )
    return lines
