"""Window and application detection for MazCro.

Responsibilities:

* enumerate visible top-level windows and their owning process
* report the current foreground window
* resolve a :class:`~macro_model.WindowTarget` to a live window handle,
  supporting several instances of the same application
* describe the screen layout so clicks can be bounds-checked (this handles
  multi-monitor and rotated displays without hardcoded resolutions)

Windows uses ctypes against ``user32``; other platforms fall back to
``pygetwindow`` and, failing that, a conservative no-op implementation so the
application still runs (minus window awareness) rather than crashing.
"""

from __future__ import annotations

import ctypes
import platform
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator

from macro_model import WindowTarget
from macro_store import log_exception, log_info, log_warning

IS_WINDOWS = platform.system() == "Windows"

#: How often the foreground-monitor thread polls, in seconds.
POLL_INTERVAL = 0.15
#: Budget for the tasklist call that maps pids to executable names.
PROCESS_LOOKUP_TIMEOUT = 3.0


# ---------------------------------------------------------------------------
# Screen geometry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ScreenInfo:
    """The bounding box of the entire virtual desktop."""

    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def contains(self, x: int, y: int) -> bool:
        """Return True if (x, y) falls inside the virtual desktop."""
        return self.left <= x < self.right and self.top <= y < self.bottom

    def clamp(self, x: int, y: int) -> tuple[int, int]:
        """Clamp (x, y) to the nearest safe point inside the desktop.

        The result is inset by one pixel. Clamping straight to the edge would
        land the cursor exactly on a screen corner, which permanently trips
        pyautogui's fail-safe and breaks every later action in the run.
        """
        return (
            max(self.left + 1, min(x, self.right - 2)),
            max(self.top + 1, min(y, self.bottom - 2)),
        )


def get_screen_info() -> ScreenInfo:
    """Return the virtual desktop bounds.

    Uses the Windows ``GetSystemMetrics`` SM_XVIRTUALSCREEN family when
    available so multi-monitor setups report their true extent; falls back to
    tkinter, then to a single-screen assumption.
    """
    if IS_WINDOWS:
        try:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            metrics = (
                ("SM_XVIRTUALSCREEN", 76),
                ("SM_YVIRTUALSCREEN", 77),
                ("SM_CXVIRTUALSCREEN", 78),
                ("SM_CYVIRTUALSCREEN", 79),
            )
            values = {name: user32.GetSystemMetrics(code) for name, code in metrics}
            if values["SM_CXVIRTUALSCREEN"] > 0 and values["SM_CYVIRTUALSCREEN"] > 0:
                return ScreenInfo(
                    left=int(values["SM_XVIRTUALSCREEN"]),
                    top=int(values["SM_YVIRTUALSCREEN"]),
                    width=int(values["SM_CXVIRTUALSCREEN"]),
                    height=int(values["SM_CYVIRTUALSCREEN"]),
                )
        except Exception:  # noqa: BLE001 - any ctypes failure falls through
            log_exception("GetSystemMetrics failed; falling back to screen size detection")

    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        width = root.winfo_screenwidth()
        height = root.winfo_screenheight()
        root.destroy()
        if width > 0 and height > 0:
            return ScreenInfo(left=0, top=0, width=width, height=height)
    except Exception:  # noqa: BLE001
        log_warning("could not query screen size via tkinter")

    log_warning("assuming a 1920x1080 desktop; screen detection failed")
    return ScreenInfo(left=0, top=0, width=1920, height=1080)


# ---------------------------------------------------------------------------
# Windows helpers
# ---------------------------------------------------------------------------
def _user32() -> ctypes.WinDLL | None:  # type: ignore[name-defined]
    """Return user32 with the signatures we need, or None if unavailable."""
    try:
        lib = ctypes.windll.user32  # type: ignore[attr-defined]
        lib.GetWindowTextLengthW.restype = ctypes.c_int
        lib.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        lib.IsWindowVisible.restype = ctypes.c_bool
        lib.IsWindow.restype = ctypes.c_bool
        return lib  # type: ignore[return-value]
    except Exception:  # noqa: BLE001
        return None


if IS_WINDOWS:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            log_warning("could not enable DPI awareness; coordinates may be scaled")


@dataclass(frozen=True)
class AppWindow:
    """A visible top-level window."""

    handle: int
    title: str
    process_name: str
    pid: int = 0
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)

    @property
    def process_label(self) -> str:
        """Human-readable label for the GUI dropdown."""
        name = self.process_name or "unknown"
        title = (self.title or "(no title)").strip()
        if len(title) > 60:
            title = title[:57] + "..."
        return f"{name} — {title}"

    def to_target(self) -> WindowTarget:
        """Convert to a WindowTarget that can be persisted in a macro."""
        import re

        # Escape the title so the stored regex matches this exact window, while
        # still tolerating a later change in surrounding text.
        pattern = f"^{re.escape(self.title)}$" if self.title else ""
        return WindowTarget(
            process_name=self.process_name,
            title=self.title,
            title_pattern=pattern,
            handle=self.handle,
        )


def _window_title(hwnd: int) -> str:
    lib = _user32()
    if lib is None:
        return ""
    try:
        length = lib.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        lib.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value
    except Exception:  # noqa: BLE001
        return ""


def _process_id(hwnd: int) -> int:
    if not IS_WINDOWS:
        return 0
    try:
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(  # type: ignore[attr-defined]
            hwnd, ctypes.byref(pid)
        )
        return int(pid.value)
    except Exception:  # noqa: BLE001
        return 0


def _process_names() -> dict[int, str]:
    """Map pid -> executable name using a single ``tasklist`` call.

    Returns an empty dict on failure so callers can fall back to the previous
    good result instead of losing process names entirely.
    """
    if not IS_WINDOWS:
        return {}
    try:
        completed = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=PROCESS_LOOKUP_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log_warning(f"tasklist timed out after {PROCESS_LOOKUP_TIMEOUT:.0f}s")
        return {}
    except (subprocess.SubprocessError, OSError) as exc:
        log_warning(f"tasklist failed: {exc}")
        return {}

    mapping: dict[int, str] = {}
    for line in completed.stdout.splitlines():
        parts = [p.strip('" ') for p in line.split('","')]
        if len(parts) < 2:
            continue
        name = parts[0]
        raw_pid = parts[1].replace(",", "")
        try:
            mapping[int(raw_pid)] = name
        except ValueError:
            continue
    return mapping


class _ProcessCache:
    """Caches pid -> executable name for window enumeration.

    Enumerating processes is by far the most expensive part of refreshing the
    application list, and the GUI does that every second. Without a cache the
    ``tasklist`` call runs continuously and eventually starts timing out on a
    busy machine, which silently degrades the dropdown to empty process names.

    The trade-off is that closing and reopening an app is noticed within
    ``ttl`` seconds rather than instantly, which is imperceptible at a 1 Hz
    refresh rate.
    """

    def __init__(self, ttl: float = 5.0) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._data: dict[int, str] = {}
        self._stamp = 0.0
        self._failed_at = 0.0
        self._failure_backoff = 15.0

    def get(self) -> dict[int, str]:
        """Return the pid -> name map, refreshing it only when stale.

        After a failed lookup the cache keeps serving the last good data and
        suppresses further attempts for ``_failure_backoff`` seconds, so a
        machine where ``tasklist`` is slow does not stall the UI thread.
        """
        with self._lock:
            now = time.monotonic()
            if self._stamp and now - self._stamp <= self._ttl:
                return self._data
            if self._failed_at and now - self._failed_at <= self._failure_backoff:
                return self._data

            data = _process_names()
            if data:
                self._data = data
                self._stamp = now
                self._failed_at = 0.0
            else:
                self._stamp = 0.0
                self._failed_at = now
            return self._data


_PROCESS_CACHE = _ProcessCache()


def _enumerate_windows() -> Iterator[AppWindow]:
    """Yield every visible top-level window with a non-empty title."""
    if not IS_WINDOWS:
        return

    lib = _user32()
    if lib is None:
        return

    processes = _PROCESS_CACHE.get()
    found: list[AppWindow] = []

    try:
        WNDENUMPROC = ctypes.WINFUNCTYPE(  # type: ignore[name-defined]
            ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p
        )

        def callback(hwnd: int, _lparam: object) -> bool:
            try:
                if not lib.IsWindowVisible(hwnd):
                    return True
                title = _window_title(hwnd)
                if not title.strip():
                    return True
                pid = _process_id(hwnd)
                rect = (0, 0, 0, 0)
                try:
                    class RECT(ctypes.Structure):
                        _fields_ = [
                            ("left", ctypes.c_long),
                            ("top", ctypes.c_long),
                            ("right", ctypes.c_long),
                            ("bottom", ctypes.c_long),
                        ]

                    r = RECT()
                    if lib.GetWindowRect(hwnd, ctypes.byref(r)):
                        rect = (int(r.left), int(r.top), int(r.right), int(r.bottom))
                except Exception:  # noqa: BLE001
                    pass
                found.append(
                    AppWindow(
                        handle=int(hwnd),
                        title=title,
                        process_name=processes.get(pid, ""),
                        pid=pid,
                        rect=rect,
                    )
                )
            except Exception:  # noqa: BLE001
                pass
            return True

        lib.EnumWindows(WNDENUMPROC(callback), 0)
    except Exception:
        log_exception("window enumeration failed")
        return

    yield from found


def _enumerate_ports() -> Iterator[AppWindow]:
    """Enumerate windows via pygetwindow (non-Windows fallback)."""
    try:
        import pygetwindow

        for win in pygetwindow.getAllWindows():
            title = (getattr(win, "title", "") or "").strip()
            if not title:
                continue
            yield AppWindow(
                handle=int(getattr(win, "_hWnd", 0) or 0),
                title=title,
                process_name="",
                pid=0,
                rect=(
                    int(getattr(win, "left", 0)),
                    int(getattr(win, "top", 0)),
                    int(getattr(win, "right", 0)),
                    int(getattr(win, "bottom", 0)),
                ),
            )
    except Exception:  # noqa: BLE001
        log_exception("pygetwindow enumeration failed")


def list_windows() -> list[AppWindow]:
    """Return all visible windows, sorted by process then title."""
    windows = list(_enumerate_windows()) if IS_WINDOWS else list(_enumerate_ports())
    windows.sort(key=lambda w: (w.process_name.lower(), w.title.lower()))
    return windows


def list_running_apps() -> list[tuple[str, str]]:
    """Return de-duplicated ``(process_name, title)`` pairs for the dropdown."""
    seen: dict[tuple[str, str], None] = {}
    for win in list_windows():
        if not win.process_name and not win.title:
            continue
        seen.setdefault((win.process_name or "unknown", win.title), None)
    return [(proc, title) for proc, title in seen]


def window_for_process(process_name: str) -> list[AppWindow]:
    """Return every window owned by ``process_name`` (case-insensitive)."""
    wanted = (process_name or "").lower()
    return [w for w in list_windows() if w.process_name.lower() == wanted]


def get_foreground_window() -> AppWindow | None:
    """Return the currently focused window, if it can be determined."""
    if not IS_WINDOWS:
        return None
    try:
        lib = _user32()
        if lib is None:
            return None
        hwnd = int(lib.GetForegroundWindow() or 0)
        if hwnd == 0:
            return None
        title = _window_title(hwnd)
        if not title:
            return None
        pid = _process_id(hwnd)
        name = _PROCESS_CACHE.get().get(pid, "")
        return AppWindow(handle=hwnd, title=title, process_name=name, pid=pid)
    except Exception:  # noqa: BLE001
        log_exception("could not determine the foreground window")
        return None


def window_exists(handle: int) -> bool:
    """Return True if ``handle`` still refers to a live window."""
    if not handle or not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.user32.IsWindow(handle))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


def find_window(target: WindowTarget) -> AppWindow | None:
    """Resolve ``target`` to a live window.

    Strategy, in order:

    1. the exact handle stored in the macro, if still valid
    2. any window of the same process whose title matches the pattern
    3. any window of the same process (when no title filter is set)

    Returns ``None`` when nothing matches, which callers treat as "the target
    application has closed".
    """
    if not target.process_name and not target.title and not target.title_pattern:
        return None

    windows = list_windows()
    wanted_proc = (target.process_name or "").lower()

    # 1. cached handle
    if target.handle:
        for win in windows:
            if win.handle == target.handle:
                return win
        log_info(f"stale window handle {target.handle}; falling back to a title search")

    # 2. process + title pattern
    by_pattern = [
        w
        for w in windows
        if (not wanted_proc or w.process_name.lower() == wanted_proc)
        and target.matches_title(w.title)
    ]
    if by_pattern:
        # Prefer an exact title hit over a regex-only hit.
        for win in by_pattern:
            if win.title == target.title:
                return win
        return by_pattern[0]

    # 3. process-only fallback.
    # Deliberately skipped when the caller supplied an explicit title rule:
    # honouring a regex the user typed is more important than being lenient,
    # and silently matching a different window would replay a macro into the
    # wrong application.
    if wanted_proc and not target.title_pattern and not target.title:
        by_proc = [w for w in windows if w.process_name.lower() == wanted_proc]
        if by_proc:
            return by_proc[0]

    return None


def is_foreground(target: WindowTarget) -> bool:
    """Return True when a window matching ``target`` currently has focus."""
    win = get_foreground_window()
    if win is None:
        return False
    if target.process_name and win.process_name.lower() != target.process_name.lower():
        return False
    return target.matches_title(win.title)


def focus_window(win: AppWindow) -> bool:
    """Bring ``win`` to the foreground. Returns True on success."""
    if not IS_WINDOWS or not win.handle:
        return False
    try:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        # Restore first: a minimised window cannot take focus.
        user32.ShowWindow(win.handle, 9)  # SW_RESTORE
        # Attach to the foreground thread so SetForegroundWindow is permitted.
        fg = user32.GetForegroundWindow()
        cur = ctypes.windll.kernel32.GetCurrentThreadId()
        target_tid = user32.GetWindowThreadProcessId(fg, None) if fg else cur
        attached = False
        if target_tid and target_tid != cur:
            attached = bool(user32.AttachThreadInput(target_tid, cur, True))
        try:
            user32.BringWindowToTop(win.handle)
            user32.SetForegroundWindow(win.handle)
        finally:
            if attached:
                user32.AttachThreadInput(target_tid, cur, False)
        return True
    except Exception:  # noqa: BLE001
        log_exception(f"could not focus window {win.title!r}")
        return False


class ForegroundMonitor(threading.Thread):
    """Watches the foreground window and fires a callback on activation.

    This is what makes the Alt+Tab trigger work: when the user switches to the
    macro's target application, the callback runs and playback starts. The
    thread polls rather than hooks, which keeps it simple and reliable across
    platforms at the cost of a small latency (see ``POLL_INTERVAL``).
    """

    def __init__(
        self,
        target: WindowTarget,
        on_activate: Callable[[AppWindow], None],
        interval: float = POLL_INTERVAL,
    ) -> None:
        super().__init__(name="ForegroundMonitor", daemon=True)
        self._target = target
        self._on_activate = on_activate
        self._interval = interval
        self._stop_event = threading.Event()
        self._active = threading.Event()
        self._target_lock = threading.Lock()

    def set_target(self, target: WindowTarget) -> None:
        """Change the window being watched."""
        with self._target_lock:
            self._target = target
        self._active.clear()

    def stop(self) -> None:
        """Request the thread to exit; safe to call more than once."""
        self._stop_event.set()

    def run(self) -> None:  # noqa: PLR0912 - the branching is inherent here
        log_info("foreground monitor started")
        try:
            while not self._stop_event.is_set():
                try:
                    with self._target_lock:
                        target = self._target
                    if target and (target.process_name or target.title_pattern):
                        win = get_foreground_window()
                        if win is not None:
                            same_process = (
                                not target.process_name
                                or win.process_name.lower() == target.process_name.lower()
                            )
                            if same_process and target.matches_title(win.title):
                                # Fire once per activation: _active stays set until
                                # focus leaves the target window again.
                                if not self._active.is_set():
                                    self._active.set()
                                    try:
                                        self._on_activate(win)
                                    except Exception:  # noqa: BLE001
                                        log_exception("foreground callback raised")
                                else:
                                    self._active.wait(0.01)
                            else:
                                self._active.clear()
                    self._stop_event.wait(self._interval)
                except Exception:  # noqa: BLE001
                    log_exception("foreground monitor iteration failed")
                    self._stop_event.wait(self._interval)
        finally:
            log_info("foreground monitor stopped")


def target_alive(target: WindowTarget) -> tuple[bool, str]:
    """Return ``(alive, message)`` describing the target's state."""
    if not target.process_name and not target.title and not target.title_pattern:
        return False, "no target application selected"
    win = find_window(target)
    if win is None:
        return False, f"target window for {target.process_name or target.title!r} is not open"
    if not window_exists(win.handle) and win.handle:
        return False, "target window handle became invalid"
    return True, f"target ready: {win.process_label}"


def target_is_foreground(target: WindowTarget) -> bool:
    """Convenience wrapper used by the hotkey handler."""
    return is_foreground(target)
