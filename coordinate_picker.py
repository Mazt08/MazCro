"""Full-screen coordinate picker for MazCro.

Picking a point by typing numbers is error-prone; this module shows what the
user's actual click will hit. When :func:`pick_coordinate` is called:

* MazCro hides itself (the caller does this, so the app keeps ownership of
  its own window state),
* a borderless, always-on-top window covers the whole virtual desktop,
* a crosshair follows the cursor together with a live ``X: n, Y: n`` readout,
* a left click captures the position, ``Escape`` cancels, and a timeout
  (5 seconds by default) cancels rather than leaving an invisible window on
  screen.

The overlay is drawn with Tk canvas primitives only, so Pillow is not
required. On Windows the window is made click-through-free with
``WS_EX_LAYERED`` transparency; on other platforms the window uses its own
background and simply sits on top.

The whole thing is modal: :func:`pick_coordinate` blocks until the user
clicks, cancels, or the timer fires, and always returns a position or
``None``.
"""

from __future__ import annotations

import ctypes
import platform
import tkinter as tk
from typing import Callable

from macro_store import log_exception, log_info
from window_manager import ScreenInfo, get_screen_info

#: Seconds the picker waits for a click before giving up on its own.
DEFAULT_TIMEOUT = 5.0

#: How often the crosshair is repositioned. pynput can fire far more often
#: than the screen can redraw, so polling position() at ~60 Hz is plenty and
#: keeps the UI thread cheap.
TRACK_INTERVAL_MS = 16

BACKGROUND = "#0b1220"
CROSSHAIR = "#22d3ee"
TEXT = "#e2e8f0"
ACCENT = "#f97316"

IS_WINDOWS = platform.system() == "Windows"


def _apply_layered(win: tk.Toplevel, alpha: float) -> None:
    """Make a Windows top-level window layered with the given opacity.

    A layered window composites against the desktop instead of being opaque,
    so the user still sees what they are aiming at. Clicks are *not* passed
    through: the picker needs to receive the selection click.
    """
    if not IS_WINDOWS:
        return
    try:
        win.update_idletasks()
        hwnd = win.winfo_id()
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        LWA_ALPHA = 0x02
        ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_LAYERED)
        ctypes.windll.user32.SetLayeredWindowAttributes(
            hwnd, 0, max(0, min(255, int(alpha * 255))), LWA_ALPHA
        )
    except Exception:  # noqa: BLE001
        log_exception("could not make the picker window layered")


class CoordinatePicker:
    """The transparent overlay. Create with :func:`pick_coordinate`."""

    def __init__(
        self,
        screen: ScreenInfo,
        on_position: Callable[[int, int], None] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.screen = screen
        self.on_position = on_position
        self.timeout = timeout
        self.result: tuple[int, int] | None = None
        self.cancelled = False

        self._root: tk.Tk | None = None
        self._win: tk.Toplevel | None = None
        self._canvas: tk.Canvas | None = None
        self._h_line: int | None = None
        self._v_line: int | None = None
        self._marker: int | None = None
        self._label: tk.Label | None = None
        self._done = False

    # ------------------------------------------------------------------ build
    def _build(self) -> None:
        self._root = tk.Tk()
        self._root.withdraw()

        win = tk.Toplevel(self._root)
        self._win = win
        win.title("MazCro coordinate picker")
        win.overrideredirect(True)
        win.geometry(
            f"{self.screen.width}x{self.screen.height}"
            f"+{self.screen.left}+{self.screen.top}"
        )
        win.attributes("-topmost", True)

        canvas = tk.Canvas(
            win,
            width=self.screen.width,
            height=self.screen.height,
            highlightthickness=0,
            cursor="crosshair",
            background=BACKGROUND,
        )
        canvas.pack(fill=tk.BOTH, expand=True)
        self._canvas = canvas

        cx, cy = self.screen.width // 2, self.screen.height // 2
        self._h_line = canvas.create_line(0, cy, self.screen.width, cy, fill=CROSSHAIR, width=1)
        self._v_line = canvas.create_line(cx, 0, cx, self.screen.height, fill=CROSSHAIR, width=1)
        self._marker = canvas.create_oval(
            cx - 12, cy - 12, cx + 12, cy + 12, outline=ACCENT, width=2
        )
        canvas.create_text(
            self.screen.width // 2,
            64,
            text="Move the mouse to a point, then LEFT CLICK to select it.",
            fill=TEXT,
            font=("TkDefaultFont", 14, "bold"),
        )
        canvas.create_text(
            self.screen.width // 2,
            92,
            text=f"Escape cancels  •  auto-cancels in {self.timeout:.0f}s",
            fill=TEXT,
            font=("TkDefaultFont", 10),
        )

        self._label = tk.Label(
            canvas,
            text="X: 0   Y: 0",
            background=ACCENT,
            foreground="#0b1220",
            font=("Consolas", 14, "bold"),
            padx=10,
            pady=4,
        )
        self._label.place(x=16, y=16)

        _apply_layered(win, alpha=0.35)

        win.bind("<Escape>", lambda _e: self._cancel())
        canvas.bind("<Button-1>", self._on_click)
        win.focus_force()

    # --------------------------------------------------------------- tracking
    def _track(self) -> None:
        """Move the crosshair to the pointer and refresh the readout."""
        if self._done or self._win is None:
            return
        try:
            x, y = self._win.winfo_pointerxy()
        except tk.TclError:
            return
        # winfo_pointerxy is screen-relative; convert to overlay coordinates.
        lx = x - self.screen.left
        ly = y - self.screen.top
        try:
            self._canvas.coords(self._h_line, 0, ly, self.screen.width, ly)
            self._canvas.coords(self._v_line, lx, 0, lx, self.screen.height)
            self._canvas.coords(self._marker, lx - 12, ly - 12, lx + 12, ly + 12)
            if self._label is not None:
                self._label.configure(text=f"X: {lx}   Y: {ly}")
                self._label.place(x=min(lx + 18, self.screen.width - 140), y=min(ly + 18, self.screen.height - 50))
        except tk.TclError:
            return
        if self.on_position is not None:
            try:
                self.on_position(lx, ly)
            except Exception:  # noqa: BLE001
                log_exception("picker position callback raised")
        self._win.after(TRACK_INTERVAL_MS, self._track)

    def _on_click(self, _event: tk.Event) -> None:
        x, y = self._win.winfo_pointerxy() if self._win else (0, 0)
        self.result = (x - self.screen.left, y - self.screen.top)
        self._finish()

    # --------------------------------------------------------------- teardown
    def _cancel(self) -> None:
        self.cancelled = True
        self._finish()

    def _finish(self) -> None:
        """Tear the overlay down exactly once, whatever ended the session.

        Idempotency matters because three different paths can finish a pick --
        a click, Escape, and the timeout timer -- and Tk will raise TclError if
        the root is destroyed twice.
        """
        if self._done:
            return
        self._done = True
        root, self._root = self._root, None
        self._win = None
        self._canvas = None
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                # The window can already be gone if the user closed the picker
                # or the interpreter is shutting down; nothing left to clean up.
                pass

    def run(self) -> tuple[int, int] | None:
        """Show the overlay and block until the user clicks, cancels, or times out."""
        self._build()
        root = self._root
        assert root is not None
        root.after(0, self._track)
        if self.timeout > 0:
            root.after(int(self.timeout * 1000), self._cancel)
        try:
            root.mainloop()
        except tk.TclError:
            log_exception("the coordinate picker window closed unexpectedly")
            return None
        finally:
            self._finish()
        return self.result


def pick_coordinate(
    timeout: float = DEFAULT_TIMEOUT,
    on_position: Callable[[int, int], None] | None = None,
) -> tuple[int, int] | None:
    """Block until the user picks a screen coordinate.

    Returns ``(x, y)`` in screen pixels, or ``None`` if the user pressed
    Escape or the timeout elapsed. The caller is responsible for hiding its
    own window before calling this, and restoring it afterwards.
    """
    try:
        screen = get_screen_info()
        picker = CoordinatePicker(screen, on_position=on_position, timeout=timeout)
        result = picker.run()
        log_info(
            f"coordinate picker {'cancelled' if result is None else f'selected {result}'}"
        )
        return result
    except Exception:  # noqa: BLE001
        log_exception("the coordinate picker failed to start")
        return None
