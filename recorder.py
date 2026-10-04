"""Recording engine for MazCro.

Wraps ``pynput`` listeners and converts raw input events into
:class:`~macro_model.Action` objects with relative timing.

Every callback runs on the pynput listener thread, so each one only appends to
a list under a lock and pushes a copy to a callback — no blocking work happens
on the listener thread, which would otherwise stall the input queue.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from macro_model import Action, WindowTarget
from macro_store import log_exception, log_info

#: Mouse travel below this many pixels is treated as jitter, not a real move.
#: Recording every pixel produces enormous macros that are useless on replay.
MOVE_THRESHOLD = 3

#: Key names treated as modifiers rather than as actions in their own right.
MODIFIER_NAMES: frozenset[str] = frozenset(
    {
        "shift", "shift_l", "shift_r",
        "ctrl", "ctrl_l", "ctrl_r",
        "alt", "alt_l", "alt_r",
        "cmd", "cmd_l", "cmd_r", "win", "winleft", "winright",
    }
)


class Recorder:
    """Captures mouse and keyboard activity into a list of Actions.

    Usage::

        recorder = Recorder(on_action=self.add, on_state=self.set_state)
        recorder.start()
        ...
        recorder.stop()
    """

    def __init__(
        self,
        on_action: Callable[[Action], None] | None = None,
        on_state: Callable[[str], None] | None = None,
        hold_time_ms: int = 50,
        between_action_ms: int = 10,
        record_mouse_moves: bool = True,
    ) -> None:
        self._on_action = on_action
        self._on_state = on_state
        self.hold_time_ms = max(int(hold_time_ms), 0)
        self.between_action_ms = max(int(between_action_ms), 0)
        self.record_mouse_moves = record_mouse_moves

        self._actions: list[Action] = []
        self._lock = threading.Lock()
        self._running = threading.Event()
        self._mouse_listener = None
        self._keyboard_listener = None
        self._start_time = 0.0
        self._last_time = 0.0
        self._last_position = (-1, -1)
        self._held_keys: set[str] = set()
        #: Printable characters typed since the last non-printable key, used to
        #: coalesce a burst of typing into a single ``text_input`` action.
        self._typing_buffer: list[str] = []
        #: Delay accumulated while the current typing burst was being recorded.
        self._typing_delay: float = 0.0
        #: Milliseconds between characters recorded into ``text_input``.
        self.delay_per_char_ms = 50

    # ------------------------------------------------------------ properties
    @property
    def is_recording(self) -> bool:
        """True while listeners are active."""
        return self._running.is_set()

    @property
    def elapsed(self) -> float:
        """Seconds since recording started."""
        if not self._start_time:
            return 0.0
        return max(time.monotonic() - self._start_time, 0.0)

    def actions(self) -> list[Action]:
        """Return a copy of the recorded actions."""
        with self._lock:
            return list(self._actions)

    def clear(self) -> None:
        """Discard all recorded actions."""
        with self._lock:
            self._actions.clear()

    # ---------------------------------------------------------------- public
    def start(self, target: WindowTarget | None = None) -> None:
        """Begin recording. Safe to call when already recording (no-op)."""
        if self._running.is_set():
            return

        from pynput import keyboard, mouse  # imported late so --help works

        with self._lock:
            self._actions.clear()
        self._last_position = (-1, -1)
        self._held_keys.clear()
        self._typing_buffer.clear()
        self._typing_delay = 0.0
        self._start_time = time.monotonic()
        self._last_time = self._start_time
        self._running.set()

        self._notify(f"recording into {target.process_name}" if target else "recording")

        try:
            self._keyboard_listener = keyboard.Listener(
                on_press=self._on_key_press, on_release=self._on_key_release
            )
            self._mouse_listener = mouse.Listener(
                on_move=self._on_move,
                on_click=self._on_click,
                on_scroll=self._on_scroll,
            )
            self._keyboard_listener.start()
            self._mouse_listener.start()
            log_info("recording started")
        except Exception:
            self._running.clear()
            log_exception("could not start input listeners")
            raise

    def stop(self) -> list[Action]:
        """Stop recording and return the captured actions."""
        if not self._running.is_set():
            return self.actions()

        self._flush_typing()
        self._running.clear()
        for listener in (self._mouse_listener, self._keyboard_listener):
            if listener is None:
                continue
            try:
                listener.stop()
            except Exception:  # noqa: BLE001
                log_exception("error while stopping an input listener")
        self._mouse_listener = None
        self._keyboard_listener = None
        self._notify(f"stopped after {self.elapsed:.1f}s")
        log_info(f"recording stopped, {len(self.actions())} actions captured")
        return self.actions()

    def add_wait(self, duration: float) -> None:
        """Append an explicit wait action (used by the GUI)."""
        self._append(Action(kind="wait", delay=0.0, duration=max(duration, 0.0)))

    def add_screenshot(self, path: str = "") -> None:
        """Append a screenshot action (used by the GUI)."""
        self._append(Action(kind="screenshot", delay=0.0, path=path))

    # ------------------------------------------------------------- internals
    def _notify(self, message: str) -> None:
        if self._on_state is not None:
            try:
                self._on_state(message)
            except Exception:  # noqa: BLE001
                log_exception("recorder state callback raised")

    def _append(self, action: Action) -> None:
        """Record an action and hand a copy to the UI callback."""
        with self._lock:
            self._actions.append(action)
        if self._on_action is not None:
            try:
                self._on_action(action)
            except Exception:  # noqa: BLE001
                log_exception("recorder action callback raised")

    def _elapsed_since_last(self) -> float:
        """Return the gap since the previous event and restart the clock."""
        now = time.monotonic()
        gap = max(now - self._last_time, 0.0)
        self._last_time = now
        return gap

    # -- pynput callbacks ---------------------------------------------------
    def _on_move(self, x: int, y: int) -> None:
        if not self._running.is_set() or not self.record_mouse_moves:
            return
        if abs(x - self._last_position[0]) < MOVE_THRESHOLD and abs(y - self._last_position[1]) < MOVE_THRESHOLD:
            return
        self._last_position = (int(x), int(y))
        # Moving the pointer is a reliable separator between two runs of typing.
        self._flush_typing()
        self._append(
            Action(
                kind="move",
                delay=self._elapsed_since_last(),
                x=int(x),
                y=int(y),
                duration=0.0,
            )
        )

    def _on_click(self, x: int, y: int, button: object, pressed: bool) -> None:
        if not self._running.is_set() or not pressed:
            return
        # Clicking means the user moved to a new place: end any typing burst.
        self._flush_typing()
        name = getattr(button, "name", str(button))
        # A double click is two clicks; the player reproduces the timing, so
        # record them individually rather than collapsing them.
        self._append(
            Action(
                kind="click",
                delay=self._elapsed_since_last(),
                x=int(x),
                y=int(y),
                button=name if name in ("left", "right", "middle") else "left",
                hold_time=self.hold_time_ms,
            )
        )

    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        if not self._running.is_set():
            return
        if dx == 0 and dy == 0:
            return
        self._flush_typing()
        self._append(
            Action(
                kind="scroll",
                delay=self._elapsed_since_last(),
                x=int(x),
                y=int(y),
                amount=int(dy) if dy else int(dx),
            )
        )

    def _key_name(self, key: object) -> str:
        name = getattr(key, "name", None)
        return str(name) if name else str(key)

    def _on_key_press(self, key: object) -> None:
        if not self._running.is_set():
            return
        name = self._key_name(key)
        if name == "esc":
            # Escape stops recording rather than being recorded.
            self._flush_typing()
            self.stop()
            return
        if name in MODIFIER_NAMES:
            # Modifiers are captured as part of the key they modify.
            self._held_keys.add(name)
            return

        # Any key other than a plain character ends the current typing burst,
        # so text is grouped into one text_input action per run of characters.
        character = getattr(key, "char", None)
        if character and character.isprintable():
            self._typing_delay += self._elapsed_since_last()
            self._typing_buffer.append(character)
            return

        self._flush_typing()
        modifiers = sorted(m for m in self._held_keys if m in MODIFIER_NAMES)
        self._append(
            Action(
                kind="key_press",
                delay=self._elapsed_since_last(),
                key=name,
                modifiers=modifiers,
                hold_time=self.hold_time_ms,
            )
        )

    def _flush_typing(self) -> None:
        """Emit the pending characters as one ``text_input`` action."""
        if not self._typing_buffer:
            self._typing_delay = 0.0
            return
        text = "".join(self._typing_buffer)
        self._typing_buffer.clear()
        self._append(
            Action(
                kind="text_input",
                delay=self._typing_delay,
                text=text,
                delay_per_char=self.delay_per_char_ms,
            )
        )
        self._typing_delay = 0.0

    def _on_key_release(self, key: object) -> None:
        # Releases are tracked only to know when a modifier is held down; the
        # player's hold_key action supplies the timing itself.
        self._held_keys.discard(self._key_name(key))

    def held_modifiers(self) -> set[str]:
        """Return modifiers currently held down (for diagnostics)."""
        return set(self._held_keys)


def actions_to_macro(actions: list[Action], target: WindowTarget, name: str) -> object:
    """Bundle recorded actions and a target into a :class:`~macro_model.Macro`."""
    from macro_model import Macro

    macro = Macro(name=name, target=target)
    macro.actions = actions
    return macro
