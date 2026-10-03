"""MazCro - a Windows macro recorder and player with a Tkinter UI.

Record mouse clicks, movement, scrolling and keyboard input, then replay it.
"""

from __future__ import annotations

import json
import threading
import time
import tkinter as tk
from dataclasses import asdict, dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

import pyautogui
from pynput import keyboard, mouse

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.0

APP_TITLE = "MazCro Macro Recorder"


@dataclass
class Action:
    """A single recorded event."""

    kind: str
    delay: float
    x: int = 0
    y: int = 0
    button: str = ""
    dx: int = 0
    dy: int = 0
    key: str = ""

    def describe(self) -> str:
        if self.kind == "move":
            return f"Move      -> ({self.x}, {self.y})"
        if self.kind == "click":
            return f"Click {self.button:<8}-> ({self.x}, {self.y})"
        if self.kind == "scroll":
            return f"Scroll {self.dx}, {self.dy}"
        if self.kind == "key":
            return f"Key       -> {self.key}"
        return self.kind


def _fmt_key(key: keyboard.Key | str) -> str:
    if isinstance(key, keyboard.Key):
        return key.name
    return str(key)


class Recorder(threading.Thread):
    """Listens to global mouse/keyboard events and appends Actions."""

    def __init__(self, sink: Callable[[Action], None], should_ignore: Callable[[], bool]):
        super().__init__(daemon=True)
        self._sink = sink
        self._should_ignore = should_ignore
        self._stop = threading.Event()
        self._last = time.monotonic()
        self._mouse_listener: mouse.Listener | None = None
        self._kb_listener: keyboard.Listener | None = None

    def _emit(self, **kwargs: Any) -> None:
        if self._should_ignore():
            return
        now = time.monotonic()
        action = Action(kind=kwargs.pop("kind"), delay=round(now - self._last, 4), **kwargs)
        self._last = now
        self._sink(action)

    # --- mouse callbacks ---
    def _on_move(self, x: int, y: int) -> None:
        self._emit(kind="move", x=int(x), y=int(y))

    def _on_click(self, x: int, y: int, button: Any, pressed: bool) -> None:
        if not pressed:
            return
        self._emit(kind="click", x=int(x), y=int(y), button=button.name)

    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        self._emit(kind="scroll", x=int(x), y=int(y), dx=int(dx), dy=int(dy))

    # --- keyboard callbacks ---
    def _on_press(self, key: Any) -> None:
        name = _fmt_key(key)
        if name == "esc":
            self._stop.set()
            return
        if name in ("ctrl", "alt", "shift"):
            return
        self._emit(kind="key", key=name)

    def run(self) -> None:
        self._last = time.monotonic()
        self._mouse_listener = mouse.Listener(
            on_move=self._on_move, on_click=self._on_click, on_scroll=self._on_scroll
        )
        self._kb_listener = keyboard.Listener(on_press=self._on_press)
        self._mouse_listener.start()
        self._kb_listener.start()
        while not self._stop.wait(0.1):
            pass
        self._stop.set()

    def stop(self) -> None:
        self._stop.set()
        for listener in (self._mouse_listener, self._kb_listener):
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass


class Player(threading.Thread):
    """Replays actions on a worker thread, honouring the stop event."""

    def __init__(
        self,
        actions: list[Action],
        speed: float,
        repeats: int,
        on_done: Callable[[str], None],
    ):
        super().__init__(daemon=True)
        self.actions = actions
        self.speed = max(speed, 0.01)
        self.repeats = repeats
        self._on_done = on_done
        self._stop = threading.Event()

    def _apply(self, action: Action) -> None:
        if action.kind == "move":
            pyautogui.moveTo(action.x, action.y, duration=0)
        elif action.kind == "click":
            pyautogui.click(x=action.x, y=action.y, button=action.button)
        elif action.kind == "scroll":
            pyautogui.scroll(action.dy, x=action.x, y=action.y)
        elif action.kind == "key":
            pyautogui.press(action.key)

    def run(self) -> None:
        try:
            for _run in range(self.repeats):
                if self._stop.is_set():
                    self._on_done("Stopped.")
                    return
                for action in self.actions:
                    if self._stop.is_set():
                        self._on_done("Stopped.")
                        return
                    if self._stop.wait(action.delay / self.speed):
                        self._on_done("Stopped.")
                        return
                    self._apply(action)
            self._on_done(f"Done. {self.repeats} run(s) completed.")
        except Exception as exc:  # noqa: BLE001
            self._on_done(f"Playback error: {exc}")

    def stop(self) -> None:
        self._stop.set()


class MacroApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("820x520")
        self.minsize(640, 400)

        self.actions: list[Action] = []
        self.recorder: Recorder | None = None
        self.player: Player | None = None

        self.speed_var = tk.DoubleVar(value=1.0)
        self.repeat_var = tk.IntVar(value=1)
        self.status_var = tk.StringVar(value="Ready. Press Record to start.")
        self.count_var = tk.StringVar(value="0 actions")

        self._build_ui()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda _event: self._stop_recording())

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        controls = ttk.Frame(self, padding=10)
        controls.pack(fill=tk.X)

        self.record_btn = ttk.Button(controls, text="Record", command=self._start_recording)
        self.record_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.stop_btn = ttk.Button(controls, text="Stop", command=self._stop_all, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.play_btn = ttk.Button(controls, text="Play", command=self._start_playback)
        self.play_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.clear_btn = ttk.Button(controls, text="Clear", command=self._clear)
        self.clear_btn.pack(side=tk.LEFT, padx=(0, 6))

        ttk.Separator(controls, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)

        self.save_btn = ttk.Button(controls, text="Save", command=self._save)
        self.save_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.load_btn = ttk.Button(controls, text="Load", command=self._load)
        self.load_btn.pack(side=tk.LEFT)

        opts = ttk.Frame(self, padding=(10, 0))
        opts.pack(fill=tk.X)
        ttk.Label(opts, text="Speed:").pack(side=tk.LEFT)
        ttk.Scale(opts, from_=0.25, to=4.0, variable=self.speed_var,
                  orient=tk.HORIZONTAL, length=140).pack(side=tk.LEFT, padx=6)
        ttk.Label(opts, text="Repeats:").pack(side=tk.LEFT, padx=(14, 0))
        ttk.Spinbox(opts, from_=1, to=999, textvariable=self.repeat_var, width=6).pack(side=tk.LEFT, padx=6)

        body = ttk.Frame(self, padding=10)
        body.pack(fill=tk.BOTH, expand=True)

        list_frame = ttk.Frame(body)
        list_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        columns = ("idx", "delay", "action")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode=tk.EXTENDED)
        self.tree.heading("idx", text="#")
        self.tree.heading("delay", text="Delay (s)")
        self.tree.heading("action", text="Action")
        self.tree.column("idx", width=50, anchor=tk.E, stretch=False)
        self.tree.column("delay", width=80, anchor=tk.E, stretch=False)
        self.tree.column("action", width=420, anchor=tk.W)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        scroll.pack(side=tk.LEFT, fill=tk.Y)
        self.tree.configure(yscrollcommand=scroll.set)

        side = ttk.Frame(body, padding=(10, 0))
        side.pack(side=tk.LEFT, fill=tk.Y)
        ttk.Button(side, text="Delete\nselected", command=self._delete_selected).pack(fill=tk.X)
        ttk.Button(side, text="Move up", command=lambda: self._move_selected(-1)).pack(fill=tk.X, pady=(6, 0))
        ttk.Button(side, text="Move down", command=lambda: self._move_selected(1)).pack(fill=tk.X, pady=(6, 0))

        ttk.Label(self, textvariable=self.count_var, padding=(10, 0)).pack(anchor=tk.W)
        ttk.Label(self, textvariable=self.status_var, relief=tk.SUNKEN,
                  anchor=tk.W, padding=6).pack(fill=tk.X, side=tk.BOTTOM)

    # --------------------------------------------------------------- helpers
    def _pointer_below_window(self) -> bool:
        x, y = pyautogui.position()
        wx, wy = self.winfo_rootx(), self.winfo_rooty()
        ww, wh = self.winfo_width(), self.winfo_height()
        return wx <= x < wx + ww and wy <= y < wy + wh

    def _should_ignore(self) -> bool:
        return self._pointer_below_window()

    def _refresh_list(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, action in enumerate(self.actions, start=1):
            self.tree.insert("", tk.END, iid=str(i), values=(i, f"{action.delay:.3f}", action.describe()))
        self.count_var.set(f"{len(self.actions)} actions")

    def _append(self, action: Action) -> None:
        self.actions.append(action)
        self.tree.insert("", tk.END, iid=str(len(self.actions)),
                          values=(len(self.actions), f"{action.delay:.3f}", action.describe()))
        self.count_var.set(f"{len(self.actions)} actions")

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    # -------------------------------------------------------------- commands
    def _start_recording(self) -> None:
        if self.recorder and self.recorder.is_alive():
            return
        self.actions.clear()
        self._refresh_list()
        self.recorder = Recorder(self._append, self._should_ignore)
        self.recorder.start()
        self.record_btn.configure(state=tk.DISABLED)
        self.play_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self._set_status("Recording... perform your actions, press Esc or Stop to finish.")

    def _stop_recording(self) -> None:
        if self.recorder:
            self.recorder.stop()
            self.recorder = None
            self._set_status(f"Recorded {len(self.actions)} actions.")
        self.record_btn.configure(state=tk.NORMAL)
        self.play_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)

    def _start_playback(self) -> None:
        if not self.actions:
            messagebox.showinfo(APP_TITLE, "Nothing to play. Record or load a macro first.")
            return
        try:
            repeats = max(int(self.repeat_var.get()), 1)
        except ValueError:
            repeats = 1
        self.after(300, self._launch_player, repeats)

    def _launch_player(self, repeats: int) -> None:
        self.player = Player(
            list(self.actions),
            self.speed_var.get(),
            repeats,
            lambda msg: self.after(0, self._on_playback_done, msg),
        )
        self.player.start()
        self.play_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self._set_status(f"Playing {repeats} run(s)... move the mouse to a corner for failsafe.")

    def _on_playback_done(self, msg: str) -> None:
        self._set_status(msg)
        self.player = None
        self.play_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)

    def _stop_all(self) -> None:
        if self.recorder:
            self._stop_recording()
        if self.player:
            self.player.stop()

    def _clear(self) -> None:
        self._stop_all()
        self.actions.clear()
        self._refresh_list()
        self._set_status("Cleared.")

    def _selected_indices(self) -> list[int]:
        return sorted(int(iid) for iid in self.tree.selection())

    def _delete_selected(self) -> None:
        for idx in reversed(self._selected_indices()):
            del self.actions[idx - 1]
        self._refresh_list()

    def _move_selected(self, offset: int) -> None:
        picked = self._selected_indices()
        if not picked:
            return
        ordered = picked if offset < 0 else list(reversed(picked))
        for idx in ordered:
            target = idx - 1 + offset
            if 0 <= target < len(self.actions):
                self.actions[idx - 1], self.actions[target] = self.actions[target], self.actions[idx - 1]
        self._refresh_list()
        self.tree.selection_set([str(max(1, i + offset)) for i in picked])

    # ------------------------------------------------------------ save / load
    def _save(self) -> None:
        if not self.actions:
            messagebox.showinfo(APP_TITLE, "Nothing to save.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("Macro files", "*.json")], title="Save macro"
        )
        if not path:
            return
        payload = {
            "app": APP_TITLE,
            "speed": self.speed_var.get(),
            "repeats": max(int(self.repeat_var.get()), 1),
            "actions": [asdict(a) for a in self.actions],
        }
        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        self._set_status(f"Saved to {path}")

    def _load(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Macro files", "*.json")], title="Load macro")
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            self.actions = [Action(**row) for row in payload["actions"]]
            if "speed" in payload:
                self.speed_var.set(float(payload["speed"]))
            if "repeats" in payload:
                self.repeat_var.set(int(payload["repeats"]))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Could not load macro:\n{exc}")
            return
        self._refresh_list()
        self._set_status(f"Loaded {len(self.actions)} actions from {Path(path).name}.")

    def _on_close(self) -> None:
        self._stop_all()
        self.destroy()


if __name__ == "__main__":
    MacroApp().mainloop()
