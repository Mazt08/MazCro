"""MazCro — a screen macro recorder and player.

Tkinter GUI entry point. All long-running work (recording, playback, window
monitoring, hotkey capture) happens on background threads; the UI thread only
ever receives results through Tkinter's ``after`` queue, so the window stays
responsive.

Layout, top to bottom:

* target application picker (auto-refreshing dropdown)
* record / stop controls with status and action counter
* playback controls with a 0.5x-2.0x speed slider
* saved macro list with new / load / save / rename / delete / import / export
* variables editor and hotkey assignment
* a read-only event log showing the last 20 lines
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from collections import deque
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable

from macro_model import Action, Macro, Variable
from macro_store import (
    delete_macro,
    error_log_path,
    export_macro,
    get_logger,
    import_macro,
    list_macros,
    load_macro,
    macros_dir,
    save_macro,
)
from player import PlaybackResult, Player, dry_run
from recorder import Recorder
from window_manager import (
    AppWindow,
    ForegroundMonitor,
    find_window,
    get_screen_info,
    is_foreground,
    list_windows,
)

APP_TITLE = "MazCro — Macro Recorder"
REFRESH_MS = 1000
MAX_LOG_LINES = 20
#: Seconds the "Set Hotkey" button waits for the user to press a key.
HOTKEY_CAPTURE_SECONDS = 5.0
#: Delay before playback starts, so the user can switch to the target window.
PLAYBACK_ARM_DELAY_MS = 800

ACCENT = "#2563eb"
MUTED = "#6b7280"


class HotkeyManager:
    """Global hotkey listener that only fires when the target window is active.

    The hotkey itself does nothing if focus is elsewhere, which prevents the
    app from replaying a macro while the user is typing in the macro editor.
    """

    def __init__(self, is_active: Callable[[], bool], on_trigger: Callable[[str], None]) -> None:
        self._is_active = is_active
        self._on_trigger = on_trigger
        self._key = ""
        self._listener = None
        self._lock = threading.Lock()

    @property
    def key(self) -> str:
        return self._key

    def set_key(self, key: str) -> None:
        """Assign a hotkey, restarting the listener so it picks up the change."""
        with self._lock:
            self._key = (key or "").lower()
        self.restart()

    def restart(self) -> None:
        """Rebuild the pynput listener for the current key."""
        self.stop()
        if not self._key:
            return
        try:
            from pynput import keyboard

            wanted = self._key

            def on_press(key: object) -> None:
                name = getattr(key, "name", None) or str(key)
                if str(name).lower() != wanted:
                    return
                if not self._is_active():
                    return
                try:
                    self._on_trigger(str(name))
                except Exception:  # noqa: BLE001
                    get_logger().exception("hotkey callback raised")

            self._listener = keyboard.Listener(on_press=on_press)
            self._listener.start()
            get_logger().info(f"hotkey {self._key!r} registered")
        except Exception:  # noqa: BLE001
            self._listener = None
            get_logger().exception("could not start the hotkey listener")

    def stop(self) -> None:
        """Stop and discard the listener. Safe to call repeatedly."""
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:  # noqa: BLE001
                get_logger().exception("error stopping the hotkey listener")


class KeyCapture:
    """One-shot listener that grabs the next key the user presses."""

    def __init__(self, on_captured: Callable[[str], None], timeout: float) -> None:
        self._on_captured = on_captured
        self._listener = None
        self._timer: threading.Timer | None = None
        self._done = threading.Event()
        self._timeout = timeout

    def start(self) -> None:
        """Begin listening. Raises if pynput cannot attach."""
        from pynput import keyboard

        def on_press(key: object) -> None:
            if self._done.is_set():
                return
            self._done.set()
            name = getattr(key, "name", None) or str(key)
            self._cancel_timer()
            try:
                self._on_captured(str(name))
            except Exception:  # noqa: BLE001
                get_logger().exception("hotkey capture callback raised")

        self._listener = keyboard.Listener(on_press=on_press)
        self._listener.start()
        self._timer = threading.Timer(self._timeout, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()

    def _on_timeout(self) -> None:
        if not self._done.is_set():
            self._done.set()
            try:
                self._on_captured("")
            except Exception:  # noqa: BLE001
                get_logger().exception("hotkey timeout callback raised")

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def stop(self) -> None:
        self._done.set()
        self._cancel_timer()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:  # noqa: BLE001
                get_logger().exception("error stopping the key capture listener")


class VariablesDialog(tk.Toplevel):
    """Modal editor for a macro's name/value variables."""

    def __init__(self, parent: "MacroApp", macro: Macro) -> None:
        super().__init__(parent)
        self.title("Edit variables")
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)
        self._macro = macro
        self._rows: list[tuple[ttk.Entry, ttk.Entry, ttk.Frame]] = []

        body = ttk.Frame(self, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            body, text="Reference a variable in a type action with ${name}",
            foreground=MUTED,
        ).pack(anchor=tk.W, pady=(0, 8))

        header = ttk.Frame(body)
        header.pack(fill=tk.X)
        ttk.Label(header, text="Name", width=18).pack(side=tk.LEFT)
        ttk.Label(header, text="Value", width=34).pack(side=tk.LEFT, padx=(6, 0))

        self._list = ttk.Frame(body)
        self._list.pack(fill=tk.BOTH, expand=True, pady=(4, 8))

        for variable in macro.variables:
            self._add_row(variable.name, variable.value)
        if not macro.variables:
            self._add_row("username", "")
            self._add_row("password", "")

        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X)
        ttk.Button(buttons, text="Add row", command=self._add_empty_row).pack(side=tk.LEFT)
        ttk.Button(buttons, text="Save", command=self._save).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side=tk.RIGHT, padx=(0, 6))

        self.bind("<Return>", lambda _e: self._save())
        self.bind("<Escape>", lambda _e: self.destroy())

    def _add_row(self, name: str = "", value: str = "") -> None:
        row = ttk.Frame(self._list)
        row.pack(fill=tk.X, pady=2)
        name_entry = ttk.Entry(row, width=20)
        name_entry.insert(0, name)
        name_entry.pack(side=tk.LEFT)
        value_entry = ttk.Entry(row, width=36)
        value_entry.insert(0, value)
        value_entry.pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(
            row, text="Remove", width=9, command=lambda r=row: self._remove_row(r)
        ).pack(side=tk.LEFT, padx=(6, 0))
        self._rows.append((name_entry, value_entry, row))

    def _add_empty_row(self) -> None:
        self._add_row()
        if self._rows:
            self._rows[-1][0].focus_set()

    def _remove_row(self, row: ttk.Frame) -> None:
        self._rows = [triple for triple in self._rows if triple[2] is not row]
        row.destroy()

    def _save(self) -> None:
        """Validate and apply the edited variables."""
        collected: list[Variable] = []
        seen: set[str] = set()
        for name_entry, value_entry, _row in self._rows:
            name = name_entry.get().strip()
            if not name:
                continue
            if not name.replace("_", "").isalnum():
                messagebox.showerror(
                    self.title(), f"{name!r} is not a valid variable name", parent=self
                )
                return
            if name in seen:
                messagebox.showerror(
                    self.title(), f"Duplicate variable name {name!r}", parent=self
                )
                return
            seen.add(name)
            collected.append(Variable(name=name, value=value_entry.get()))

        self._macro.variables = collected
        self.destroy()


class MacroApp(tk.Tk):
    """The main application window."""

    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("900x780")
        self.minsize(760, 640)

        get_logger().info("=" * 50)
        get_logger().info("MazCro starting")

        # --- state ---------------------------------------------------------
        self.macro = Macro(name="Untitled macro")
        self.windows: list[AppWindow] = []
        self.recorder: Recorder | None = None
        self.player: Player | None = None
        self.monitor: ForegroundMonitor | None = None
        self.capture: KeyCapture | None = None
        self.hotkey = HotkeyManager(self._target_is_active, self._on_hotkey)
        self._auto_trigger = tk.BooleanVar(value=False)
        self._log_lines: deque[str] = deque(maxlen=MAX_LOG_LINES)
        self._closing = False
        self._record_started = 0.0

        # --- variables ------------------------------------------------------
        self.speed_var = tk.DoubleVar(value=1.0)
        self.hold_time_var = tk.IntVar(value=50)
        self.between_action_var = tk.IntVar(value=10)
        self.record_moves_var = tk.BooleanVar(value=True)
        self.error_policy_var = tk.StringVar(value="stop")
        self.status_var = tk.StringVar(value="Ready.")
        self.counter_var = tk.StringVar(value="0 actions")
        self.hotkey_var = tk.StringVar(value="(not set)")
        self.target_var = tk.StringVar(value="(no target selected)")
        self.speed_label_var = tk.StringVar(value="1.00x")
        self.auto_refresh_var = tk.BooleanVar(value=True)

        self._build_ui()
        self._set_status("Ready. Pick a target application, then press Record.")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._start_monitor()
        self.refresh_windows()
        self._tick()
        self.after(100, self._tick_counter)
        self._log(f"macros stored in {macros_dir()}")
        self._log(f"error log: {error_log_path()}")

    # ------------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill=tk.BOTH, expand=True)

        # -- 1. target application ------------------------------------------
        target_box = ttk.LabelFrame(outer, text="Select Target Application", padding=8)
        target_box.pack(fill=tk.X)
        row = ttk.Frame(target_box)
        row.pack(fill=tk.X)
        self.app_combo = ttk.Combobox(row, state="readonly", width=62)
        self.app_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.app_combo.bind("<<ComboboxSelected>>", self._on_app_selected)
        ttk.Button(row, text="Refresh", width=9, command=self.refresh_windows).pack(
            side=tk.LEFT, padx=(6, 0)
        )
        ttk.Checkbutton(
            row, text="Auto", variable=self.auto_refresh_var
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(target_box, textvariable=self.target_var, foreground=MUTED).pack(
            anchor=tk.W, pady=(6, 0)
        )

        # -- 2. recording ------------------------------------------------------
        rec_box = ttk.LabelFrame(outer, text="Recording", padding=8)
        rec_box.pack(fill=tk.X, pady=(8, 0))
        rec_row = ttk.Frame(rec_box)
        rec_row.pack(fill=tk.X)
        self.record_btn = ttk.Button(rec_row, text="Record", command=self.start_recording)
        self.record_btn.pack(side=tk.LEFT)
        self.stop_record_btn = ttk.Button(
            rec_row, text="Stop", command=self.stop_recording, state=tk.DISABLED
        )
        self.stop_record_btn.pack(side=tk.LEFT, padx=6)
        self.add_wait_btn = ttk.Button(
            rec_row, text="Add wait", command=self._add_wait_prompt, state=tk.DISABLED
        )
        self.add_wait_btn.pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(rec_row, text="Clear actions", command=self._clear_actions).pack(side=tk.LEFT)

        opts = ttk.Frame(rec_box)
        opts.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(opts, text="Hold time (ms):").pack(side=tk.LEFT)
        ttk.Spinbox(opts, from_=0, to=5000, increment=10, width=6,
                    textvariable=self.hold_time_var).pack(side=tk.LEFT, padx=(4, 12))
        ttk.Label(opts, text="Between actions (ms):").pack(side=tk.LEFT)
        ttk.Spinbox(opts, from_=0, to=5000, increment=5, width=6,
                    textvariable=self.between_action_var).pack(side=tk.LEFT, padx=(4, 12))
        ttk.Checkbutton(opts, text="Record mouse moves", variable=self.record_moves_var).pack(
            side=tk.LEFT
        )

        ttk.Label(rec_box, textvariable=self.counter_var, foreground=MUTED).pack(
            anchor=tk.W, pady=(6, 0)
        )

        # -- 3. playback -------------------------------------------------------
        play_box = ttk.LabelFrame(outer, text="Playback", padding=8)
        play_box.pack(fill=tk.X, pady=(8, 0))
        play_row = ttk.Frame(play_box)
        play_row.pack(fill=tk.X)
        self.play_btn = ttk.Button(play_row, text="Play", command=self.play_macro)
        self.play_btn.pack(side=tk.LEFT)
        self.stop_play_btn = ttk.Button(
            play_row, text="Stop", command=self.stop_playback, state=tk.DISABLED
        )
        self.stop_play_btn.pack(side=tk.LEFT, padx=6)
        ttk.Button(play_row, text="Preview", command=self._preview_macro).pack(side=tk.LEFT)
        ttk.Button(play_row, text="Edit variables", command=self._edit_variables).pack(
            side=tk.LEFT, padx=6
        )

        speed_row = ttk.Frame(play_box)
        speed_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(speed_row, text="Speed:").pack(side=tk.LEFT)
        ttk.Scale(
            speed_row, from_=0.5, to=2.0, orient=tk.HORIZONTAL, length=240,
            variable=self.speed_var, command=self._on_speed_change,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Label(speed_row, textvariable=self.speed_label_var, width=7,
                  foreground=ACCENT).pack(side=tk.LEFT)
        ttk.Label(speed_row, text="   On error:").pack(side=tk.LEFT)
        ttk.Combobox(
            speed_row, textvariable=self.error_policy_var, state="readonly", width=6,
            values=("stop", "skip"),
        ).pack(side=tk.LEFT, padx=4)
        ttk.Checkbutton(
            speed_row, text="Auto-trigger on Alt+Tab", variable=self._auto_trigger,
            command=self._sync_monitor_target,
        ).pack(side=tk.LEFT, padx=(12, 0))

        # -- 4. macros ---------------------------------------------------------
        macro_box = ttk.LabelFrame(outer, text="Macros", padding=8)
        macro_box.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        macro_body = ttk.Frame(macro_box)
        macro_body.pack(fill=tk.BOTH, expand=True)

        list_frame = ttk.Frame(macro_body)
        list_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.macro_list = tk.Listbox(list_frame, height=6, exportselection=False)
        self.macro_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.macro_list.bind("<<ListboxSelect>>", self._on_macro_selected)
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.macro_list.yview)
        scrollbar.pack(side=tk.LEFT, fill=tk.Y)
        self.macro_list.configure(yscrollcommand=scrollbar.set)

        macro_side = ttk.Frame(macro_body)
        macro_side.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0))
        for label, command in (
            ("New", self._new_macro),
            ("Load", self._load_macro),
            ("Save", self._save_macro),
            ("Rename", self._rename_macro),
            ("Delete", self._delete_macro),
            ("Import", self._import_macro),
            ("Export", self._export_macro),
        ):
            ttk.Button(macro_side, text=label, width=11, command=command).pack(
                fill=tk.X, pady=(0, 4)
            )

        name_row = ttk.Frame(macro_box)
        name_row.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(name_row, text="Macro name:").pack(side=tk.LEFT)
        self.name_entry = ttk.Entry(name_row)
        self.name_entry.insert(0, self.macro.name)
        self.name_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Label(name_row, text="Title match (regex):").pack(side=tk.LEFT, padx=(8, 0))
        self.pattern_entry = ttk.Entry(name_row, width=24)
        self.pattern_entry.pack(side=tk.LEFT, padx=6)

        # -- 5. hotkey ----------------------------------------------------------
        hot_box = ttk.LabelFrame(outer, text="Hotkey", padding=8)
        hot_box.pack(fill=tk.X, pady=(8, 0))
        hot_row = ttk.Frame(hot_box)
        hot_row.pack(fill=tk.X)
        ttk.Button(hot_row, text="Set Hotkey", command=self._set_hotkey).pack(side=tk.LEFT)
        ttk.Button(hot_row, text="Clear", width=9, command=self._clear_hotkey).pack(
            side=tk.LEFT, padx=6
        )
        ttk.Label(hot_row, text="Fires only while the target window is active:").pack(
            side=tk.LEFT, padx=(6, 4)
        )
        ttk.Label(hot_row, textvariable=self.hotkey_var, foreground=ACCENT,
                  font=("TkDefaultFont", 10, "bold")).pack(side=tk.LEFT)

        # -- 6. log --------------------------------------------------------------
        log_box = ttk.LabelFrame(outer, text="Event log", padding=8)
        log_box.pack(fill=tk.BOTH, pady=(8, 0))
        self.log_text = tk.Text(
            log_box, height=4, wrap=tk.WORD, state=tk.DISABLED,
            background="#0f172a", foreground="#e2e8f0", relief=tk.FLAT,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)
        ttk.Button(log_box, text="Clear log", command=self._clear_log).pack(
            anchor=tk.E, pady=(4, 0)
        )

        ttk.Label(
            self, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W, padding=6
        ).pack(fill=tk.X, side=tk.BOTTOM)

    # --------------------------------------------------------------- helpers
    def _set_status(self, message: str) -> None:
        self.status_var.set(message)

    def _log(self, message: str) -> None:
        """Append a timestamped line to the on-screen log (keeps 20 lines)."""
        self._log_lines.append(f"{time.strftime('%H:%M:%S')}  {message}")
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.insert("1.0", "\n".join(self._log_lines))
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)
        get_logger().info(message)

    def _clear_log(self) -> None:
        self._log_lines.clear()
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _safe(self, func: Callable[[], None]) -> None:
        """Run ``func`` on the UI thread, logging any exception."""
        try:
            func()
        except Exception:  # noqa: BLE001
            get_logger().exception("UI callback failed")

    def _commit_name(self) -> None:
        """Copy the name and regex fields from the widgets into the macro."""
        name = self.name_entry.get().strip()
        if name:
            self.macro.name = name
        pattern = self.pattern_entry.get().strip()
        self.macro.target.title_pattern = pattern

    # ------------------------------------------------------------- app list
    def refresh_windows(self) -> None:
        """Repopulate the application dropdown. Never raises."""
        previous = self.app_combo.get()
        try:
            self.windows = list_windows()
        except Exception:  # noqa: BLE001
            get_logger().exception("window refresh failed")
            self.windows = []

        labels = [w.process_label for w in self.windows]
        self.app_combo["values"] = labels
        if previous and previous in labels:
            self.app_combo.set(previous)

    def _on_app_selected(self, _event: object = None) -> None:
        label = self.app_combo.get()
        for win in self.windows:
            if win.process_label != label:
                continue
            self.macro.target = win.to_target()
            self.pattern_entry.delete(0, tk.END)
            self.pattern_entry.insert(0, self.macro.target.title_pattern)
            self.target_var.set(f"Target: {win.process_label}")
            self._sync_monitor_target()
            self._log(f"target set to {win.process_name} / {win.title!r}")
            return

    def _start_monitor(self) -> None:
        try:
            self.monitor = ForegroundMonitor(self.macro.target, self._on_target_activated)
            self.monitor.start()
        except Exception:  # noqa: BLE001
            self.monitor = None
            get_logger().exception("could not start the foreground monitor")

    def _sync_monitor_target(self) -> None:
        if self.monitor is not None:
            self.monitor.set_target(self.macro.target)

    def _target_is_active(self) -> bool:
        """True when the macro's target window currently has focus."""
        try:
            return is_foreground(self.macro.target)
        except Exception:  # noqa: BLE001
            get_logger().exception("foreground check failed")
            return False

    def _on_target_activated(self, win: AppWindow) -> None:
        """ForegroundMonitor callback — runs on the monitor thread."""
        if not self._auto_trigger.get() or self._closing:
            return
        self.after(0, self._safe(lambda: self._auto_trigger_play(win)))

    def _auto_trigger_play(self, win: AppWindow) -> None:
        if self.player is not None and self.player.is_alive():
            self._log("auto-trigger ignored: a macro is already playing")
            return
        if not self.macro.actions:
            self._log("auto-trigger ignored: macro has no actions")
            return
        self._log(f"auto-trigger: {win.process_label} became active")
        self._play()

    # ------------------------------------------------------------- recording
    def start_recording(self) -> None:
        if self.recorder and self.recorder.is_recording:
            return
        try:
            self._commit_name()
            self.macro.clear_actions()

            self.recorder = Recorder(
                on_action=self._on_recorded_action,
                on_state=lambda msg: self.after(0, self._log, msg),
                hold_time_ms=max(int(self.hold_time_var.get() or 0), 0),
                between_action_ms=max(int(self.between_action_var.get() or 0), 0),
                record_mouse_moves=bool(self.record_moves_var.get()),
            )
            self.recorder.start(self.macro.target)
        except Exception:  # noqa: BLE001
            get_logger().exception("could not start recording")
            self.recorder = None
            self._set_status("Could not start recording — see the error log.")
            messagebox.showerror(
                APP_TITLE,
                "Recording could not start.\n\n"
                "Global input capture needs the same privileges as this window.\n"
                "Try running MazCro as administrator.",
                parent=self,
            )
            return

        self._record_started = time.monotonic()
        self.record_btn.configure(state=tk.DISABLED)
        self.stop_record_btn.configure(state=tk.NORMAL)
        self.add_wait_btn.configure(state=tk.NORMAL)
        self.play_btn.configure(state=tk.DISABLED)
        self._set_status("Recording — press Esc or click Stop to finish.")

    def stop_recording(self) -> None:
        if self.recorder is None:
            return
        actions = self.recorder.stop()
        self.recorder = None
        self.record_btn.configure(state=tk.NORMAL)
        self.stop_record_btn.configure(state=tk.DISABLED)
        self.add_wait_btn.configure(state=tk.DISABLED)
        self.play_btn.configure(state=tk.NORMAL)
        self.macro.actions = actions
        self._update_counter()
        self._log(f"recorded {len(actions)} actions in {time.monotonic() - self._record_started:.1f}s")
        self._set_status(f"Recorded {len(actions)} actions.")

    def _on_recorded_action(self, action: Action) -> None:
        """Recorder callback — runs on the pynput listener thread."""
        self.after(0, self._safe(lambda: self.macro.add_action(action)))
        self.after(0, self._safe(self._update_counter))

    def _update_counter(self) -> None:
        self.counter_var.set(f"{len(self.macro.actions)} actions")

    def _tick_counter(self) -> None:
        """Keep the elapsed-time status line fresh while recording."""
        if self.recorder and self.recorder.is_recording:
            self._set_status(f"Recording... {self.recorder.elapsed:.1f}s")
        self.after(100, self._tick_counter)

    def _add_wait_prompt(self) -> None:
        """Insert an explicit wait action at the end of the macro."""
        dialog = tk.Toplevel(self)
        dialog.title("Insert wait")
        dialog.transient(self)
        dialog.resizable(False, False)
        ttk.Label(dialog, text="Duration in seconds:", padding=10).grid(row=0, column=0, sticky=tk.W)
        entry = ttk.Entry(dialog, width=10)
        entry.insert(0, "0.5")
        entry.grid(row=0, column=1, padx=(0, 10))
        entry.focus_set()

        def accept() -> None:
            try:
                seconds = float(entry.get())
            except ValueError:
                messagebox.showerror("Insert wait", "Enter a number of seconds.", parent=dialog)
                return
            if seconds < 0:
                messagebox.showerror("Insert wait", "Duration cannot be negative.", parent=dialog)
                return
            self.macro.add_action(Action(kind="wait", duration=seconds))
            self._update_counter()
            self._log(f"inserted a wait of {seconds}s")
            dialog.destroy()

        ttk.Button(dialog, text="Add", command=accept).grid(
            row=1, column=0, columnspan=2, pady=(0, 10)
        )
        entry.bind("<Return>", lambda _e: accept())
        entry.bind("<Escape>", lambda _e: dialog.destroy())

    def _clear_actions(self) -> None:
        self.macro.clear_actions()
        self._update_counter()
        self._log("cleared all recorded actions")
        self._set_status("Cleared.")

    # -------------------------------------------------------------- playback
    def _on_speed_change(self, value: str) -> None:
        try:
            self.speed_label_var.set(f"{float(value):.2f}x")
        except (TypeError, ValueError):
            self.speed_label_var.set(f"{self.speed_var.get():.2f}x")

    def play_macro(self) -> None:
        self._commit_name()
        if not self.macro.actions:
            messagebox.showinfo(APP_TITLE, "Record or load a macro first.", parent=self)
            return
        if self.macro.variables and not self._prompt_variables():
            return
        self._play()

    def _prompt_variables(self) -> bool:
        """Ask for variable values before playing. False means cancelled."""
        dialog = tk.Toplevel(self)
        dialog.title("Enter variable values")
        dialog.transient(self)
        dialog.grab_set()
        entries: dict[str, ttk.Entry] = {}

        ttk.Label(dialog, text="Values for this run:", padding=(12, 8, 12, 4)).pack(anchor=tk.W)
        body = ttk.Frame(dialog, padding=(12, 0, 12, 0))
        body.pack(fill=tk.BOTH, expand=True)
        for variable in self.macro.variables:
            row = ttk.Frame(body)
            row.pack(fill=tk.X, pady=3)
            ttk.Label(row, text=variable.name, width=16).pack(side=tk.LEFT)
            entry = ttk.Entry(row, width=30)
            entry.insert(0, variable.value)
            entry.pack(side=tk.LEFT, padx=(6, 0))
            entries[variable.name] = entry

        buttons = ttk.Frame(dialog, padding=12)
        buttons.pack(fill=tk.X)
        outcome = {"ok": False}

        def accept() -> None:
            for name, entry in entries.items():
                self.macro.set_variable(name, entry.get())
            outcome["ok"] = True
            dialog.destroy()

        ttk.Button(buttons, text="Play", command=accept).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side=tk.RIGHT, padx=(0, 6))
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        self.wait_window(dialog)
        return outcome["ok"]

    def _require_target(self) -> bool:
        target = self.macro.target
        return bool(target.process_name or target.title or target.title_pattern)

    def _check_target(self) -> tuple[bool, str]:
        if not self._require_target():
            return True, "no target window required"
        win = find_window(self.macro.target)
        if win is None:
            name = self.macro.target.process_name or self.macro.target.title
            return False, f"target {name!r} is not open"
        return True, win.process_label

    def _play(self) -> None:
        self._commit_name()
        if not self.macro.actions:
            self._set_status("Nothing to play.")
            return

        try:
            speed = float(self.speed_var.get())
        except (TypeError, ValueError):
            speed = 1.0
        self.macro.playback_speed = speed

        alive, detail = self._check_target()
        if not alive and self._require_target():
            self._log(f"playback blocked: {detail}")
            self._set_status(detail)
            return

        self.play_btn.configure(state=tk.DISABLED)
        self.stop_play_btn.configure(state=tk.NORMAL)
        self._set_status(f"Playing in {PLAYBACK_ARM_DELAY_MS}ms — switch to the target window.")
        self._log(f"playback armed: {len(self.macro.actions)} actions at {speed:.2f}x")
        self.after(PLAYBACK_ARM_DELAY_MS, self._safe(self._launch_player))

    def _launch_player(self) -> None:
        self.player = Player(
            self.macro,
            speed=float(self.speed_var.get()),
            between_action_ms=max(int(self.between_action_var.get() or 0), 0),
            on_finish=lambda result: self.after(0, self._safe(lambda: self._on_playback_done(result))),
            on_log=lambda message: self.after(0, self._log, message),
            on_error_policy=self.error_policy_var.get(),
            require_target_window=self._require_target(),
            refocus=True,
        )
        self.player.start()
        self._set_status("Playing...")

    def stop_playback(self) -> None:
        if self.player is not None:
            self.player.stop()
            self._log("stop requested")

    def _on_playback_done(self, result: PlaybackResult) -> None:
        self.player = None
        self.play_btn.configure(state=tk.NORMAL)
        self.stop_play_btn.configure(state=tk.DISABLED)
        self._log(f"playback {result.summary()}")
        for detail in result.errors[:5]:
            self._log(f"  {detail}")
        self._set_status(result.summary())

    def _preview_macro(self) -> None:
        """Show what the macro would do, without touching the mouse."""
        lines = dry_run(self.macro)
        if not lines:
            messagebox.showinfo(APP_TITLE, "This macro has no actions.", parent=self)
            return
        window = tk.Toplevel(self)
        window.title(f"Preview — {self.macro.name}")
        window.geometry("620x400")
        text = tk.Text(window, wrap=tk.NONE)
        text.insert("1.0", "\n".join(lines))
        text.configure(state=tk.DISABLED)
        text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        ttk.Button(window, text="Close", command=window.destroy).pack(pady=(0, 8))

    # ---------------------------------------------------------- macro files
    def _tick(self) -> None:
        """Refresh the application list once a second.

        The reschedule passes the bare method, never ``_safe(self._tick)``:
        ``_safe`` *invokes* its argument, so wrapping the call would run the
        tick immediately as well as scheduling it, recursing without end.
        """
        if self._closing:
            return
        try:
            if self.auto_refresh_var.get():
                self.refresh_windows()
        except Exception:  # noqa: BLE001
            get_logger().exception("window refresh failed")
        self.after(REFRESH_MS, self._tick)

    def _refresh_macro_list(self, select: str | None = None) -> None:
        names = list_macros()
        self.macro_list.delete(0, tk.END)
        for name in names:
            self.macro_list.insert(tk.END, name)
        if select and select in names:
            self.macro_list.selection_clear(0, tk.END)
            self.macro_list.selection_set(names.index(select))

    def _on_macro_selected(self, _event: object = None) -> None:
        selection = self.macro_list.curselection()
        if selection:
            self._set_status(f"Selected {self.macro_list.get(selection[0])!r}. Click Load to open it.")

    def _new_macro(self) -> None:
        self.macro = Macro(name="Untitled macro")
        self.name_entry.delete(0, tk.END)
        self.name_entry.insert(0, self.macro.name)
        self.pattern_entry.delete(0, tk.END)
        self._update_counter()
        self._sync_monitor_target()
        self._log("started a new macro")
        self._set_status("New macro. Record something, then Save.")

    def _load_macro(self) -> None:
        selection = self.macro_list.curselection()
        if not selection:
            messagebox.showinfo(APP_TITLE, "Select a macro from the list first.", parent=self)
            return
        name = self.macro_list.get(selection[0])
        try:
            self.macro = load_macro(name)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            messagebox.showerror(APP_TITLE, f"Could not load the macro:\n\n{exc}", parent=self)
            self._log(f"load failed for {name!r}: {exc}")
            return

        self.name_entry.delete(0, tk.END)
        self.name_entry.insert(0, self.macro.name)
        self.pattern_entry.delete(0, tk.END)
        self.pattern_entry.insert(0, self.macro.target.title_pattern)
        self._update_counter()
        self._sync_monitor_target()
        self._log(f"loaded macro {self.macro.name!r}")
        self._set_status(self.macro.summarise())

    def _save_macro(self) -> None:
        self._commit_name()
        if not self.macro.actions and not messagebox.askyesno(
            APP_TITLE, "This macro has no actions. Save it anyway?", parent=self
        ):
            return
        try:
            path = save_macro(self.macro)
        except (ValueError, RuntimeError, OSError) as exc:
            messagebox.showerror(APP_TITLE, f"Could not save the macro:\n\n{exc}", parent=self)
            return
        self._refresh_macro_list(self.macro.name)
        self._log(f"saved to {path.name}")
        self._set_status(f"Saved {self.macro.name!r}.")

    def _rename_macro(self) -> None:
        selection = self.macro_list.curselection()
        if not selection:
            messagebox.showinfo(APP_TITLE, "Select a macro to rename.", parent=self)
            return
        old_name = self.macro_list.get(selection[0])
        new_name = simpledialog.askstring(
            "Rename macro", "New name:", initialvalue=old_name, parent=self
        )
        if not new_name or new_name.strip() == old_name:
            return
        try:
            loaded = load_macro(old_name)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self)
            return
        loaded.name = new_name.strip()
        try:
            save_macro(loaded)
            delete_macro(old_name)
        except (ValueError, RuntimeError) as exc:
            messagebox.showerror(APP_TITLE, f"Rename failed:\n\n{exc}", parent=self)
            return
        self._refresh_macro_list(loaded.name)
        self._log(f"renamed {old_name!r} to {loaded.name!r}")

    def _delete_macro(self) -> None:
        selection = self.macro_list.curselection()
        if not selection:
            messagebox.showinfo(APP_TITLE, "Select a macro to delete.", parent=self)
            return
        name = self.macro_list.get(selection[0])
        if not messagebox.askyesno(APP_TITLE, f"Delete macro {name!r}?", parent=self):
            return
        try:
            self._log(
                f"deleted macro {name!r}" if delete_macro(name) else f"macro {name!r} was already gone"
            )
        except RuntimeError as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self)
        self._refresh_macro_list()

    def _import_macro(self) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="Import macro",
            filetypes=[("Macro files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            macro = import_macro(path)
        except (ValueError, RuntimeError, OSError) as exc:
            messagebox.showerror(APP_TITLE, f"Import failed:\n\n{exc}", parent=self)
            return
        self._refresh_macro_list(macro.name)
        self._log(f"imported {macro.name!r}")

    def _export_macro(self) -> None:
        self._commit_name()
        if not self.macro.actions:
            messagebox.showinfo(APP_TITLE, "Nothing to export.", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="Export macro", defaultextension=".json",
            initialfile=f"{self.macro.name}.json", filetypes=[("Macro files", "*.json")],
        )
        if not path:
            return
        try:
            export_macro(self.macro, path)
        except (RuntimeError, OSError) as exc:
            messagebox.showerror(APP_TITLE, f"Export failed:\n\n{exc}", parent=self)
            return
        self._log(f"exported to {path}")

    def _edit_variables(self) -> None:
        self._commit_name()
        VariablesDialog(self, self.macro)
        self._log(f"{len(self.macro.variables)} variable(s) defined")
        self._set_status(
            f"{len(self.macro.variables)} variable(s). Use them as ${{name}} in type actions."
        )

    # --------------------------------------------------------------- hotkey
    def _set_hotkey(self) -> None:
        if self.capture is not None:
            return
        self.hotkey_var.set("press a key...")
        self._set_status(f"Press the key to use as a hotkey ({HOTKEY_CAPTURE_SECONDS:.0f}s).")

        def captured(key: str) -> None:
            self.after(0, self._safe(lambda: self._apply_hotkey(key)))

        self.capture = KeyCapture(captured, HOTKEY_CAPTURE_SECONDS)
        try:
            self.capture.start()
        except Exception:  # noqa: BLE001
            self.capture = None
            get_logger().exception("could not start hotkey capture")
            self.hotkey_var.set("(not set)")
            self._set_status("Could not listen for a hotkey — see the error log.")

    def _apply_hotkey(self, key: str) -> None:
        if self.capture is not None:
            self.capture.stop()
            self.capture = None
        if not key:
            self.hotkey_var.set(self.hotkey.key or "(not set)")
            self._set_status("Hotkey capture timed out.")
            return
        if key.lower() in ("escape", "esc"):
            self.hotkey_var.set(self.hotkey.key or "(not set)")
            self._set_status("Hotkey unchanged.")
            return
        self.hotkey.set_key(key)
        self.hotkey_var.set(key.upper())
        self._log(f"hotkey set to {key.upper()} (fires only when the target window is active)")
        self._set_status(f"Hotkey {key.upper()} armed.")

    def _clear_hotkey(self) -> None:
        self.hotkey.set_key("")
        self.hotkey_var.set("(not set)")
        self._log("hotkey cleared")
        self._set_status("Hotkey cleared.")

    def _on_hotkey(self, key: str) -> None:
        """Hotkey callback — runs on the hotkey listener thread."""
        self.after(0, self._safe(lambda: self._hotkey_play(key)))

    def _hotkey_play(self, key: str) -> None:
        if self.player is not None and self.player.is_alive():
            self._log("hotkey ignored: a macro is already playing")
            return
        if not self.macro.actions:
            self._log(f"hotkey {key.upper()}: no actions recorded")
            return
        self._log(f"hotkey {key.upper()} triggered")
        self._play()

    # ----------------------------------------------------------------- exit
    def _on_close(self) -> None:
        """Stop every worker thread, then destroy the window."""
        if self._closing:
            return
        self._closing = True
        self._set_status("Shutting down...")

        for stopper in (
            lambda: self.recorder.stop() if self.recorder else None,
            lambda: self.player.stop() if self.player else None,
            lambda: self.capture.stop() if self.capture else None,
            self.hotkey.stop,
            lambda: self.monitor.stop() if self.monitor else None,
        ):
            try:
                stopper()
            except Exception:  # noqa: BLE001
                get_logger().exception("error while stopping a worker")

        # Give the workers a moment to notice their stop flags. They are daemon
        # threads, so this wait is bounded and cannot hang the shutdown.
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            busy = [t for t in (self.player, self.monitor) if t is not None and t.is_alive()]
            if not busy:
                break
            time.sleep(0.05)

        get_logger().info("MazCro stopped cleanly")
        try:
            self.destroy()
        except tk.TclError:
            pass


def main() -> int:
    """Entry point. Returns a process exit code."""
    get_logger().info("MazCro launching")
    try:
        app = MacroApp()
    except Exception:  # noqa: BLE001
        get_logger().exception("the GUI could not be created")
        return 1

    screen = get_screen_info()
    app._log(f"desktop: {screen.width}x{screen.height} at ({screen.left}, {screen.top})")

    try:
        app.mainloop()
    except KeyboardInterrupt:
        get_logger().info("interrupted by the user")
    except Exception:  # noqa: BLE001
        get_logger().exception("unhandled error in the main loop")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
