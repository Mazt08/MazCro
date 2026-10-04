"""MazCro - Macro Recorder/Player

A simple macro application with window selection, action queue, playback,
config save/load, and hotkey trigger.

ONE FILE, SIMPLE DESIGN:
- No complex modules
- No wait_window() blocking
- All async work on worker threads
- All errors wrapped in try/except
"""

import sys
import threading
import time
import configparser
import os
from pathlib import Path
from typing import List, Tuple, Dict, Any

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

import pyautogui
from pynput import keyboard


# ==============================================================================
# CONFIG
# ==============================================================================

APP_NAME = "MazCro"
CONFIG_PATH = Path.home() / "macros" / "auto_click_config.ini"
DEFAULT_LOOP_INTERVAL = 500  # ms
DEFAULT_SLEEP = 300  # ms
DEFAULT_HOLD = 50  # ms

# ==============================================================================
# UTILITIES
# ==============================================================================

def log(msg: str) -> None:
    """Simple log function."""
    print(f"[{APP_NAME}] {msg}")


def ensure_macros_dir() -> None:
    """Create macros directory if needed."""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def list_windows() -> List[Tuple[str, int]]:
    """Get list of visible windows.
    
    Returns: [(title, hwnd), ...]
    """
    try:
        import pygetwindow as gw
        windows = []
        for win in gw.getAllWindows():
            if win.title and win.isActive or not gw.getActiveWindow() or True:
                # Filter out hidden/minimized windows
                if win.isMinimized or win.isMaximized or not win.visible:
                    continue
                windows.append((win.title, win._hWnd if hasattr(win, '_hWnd') else 0))
        return windows
    except Exception as e:
        log(f"list_windows error: {e}")
        return [("Desktop", 0)]


def get_window_info(hwnd: int) -> Dict[str, Any]:
    """Get window info by hwnd."""
    try:
        import pygetwindow as gw
        win = gw.getWindowsWithAttribute("hwnd", hwnd)
        if win:
            w = win[0]
            return {
                "title": w.title,
                "hwnd": hwnd,
                "visible": w.visible,
                "minimized": w.isMinimized,
                "maximized": w.isMaximized,
            }
    except Exception:
        pass
    return {"title": f"Window {hwnd}", "hwnd": hwnd, "visible": True}


# ==============================================================================
# COORDINATE PICKER (NON-BLOCKING)
# ==============================================================================

class SimpleCoordinatePicker:
    """Simple coordinate picker using Toplevel (no blocking)."""
    
    def __init__(self, parent: tk.Tk, on_complete):
        self.parent = parent
        self.on_complete = on_complete  # Callback with (x, y) or None
        self._win: tk.Toplevel | None = None
        self._done = False
        self._result: Tuple[int, int] | None = None
        
    def show(self) -> None:
        """Show the picker window."""
        self._win = tk.Toplevel(self.parent)
        self._win.title("Pick Coordinate")
        self._win.geometry("320x170")
        self._win.attributes("-topmost", True)
        self._win.configure(bg="#f0f0f0")
        
        frame = ttk.Frame(self._win, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)
        
        ttk.Label(frame, text="Move mouse to desired position", foreground="#333").pack(pady=5)
        ttk.Label(frame, text="Press SPACE to confirm", foreground="#333").pack(pady=5)
        ttk.Label(frame, text="Press ESC to cancel", foreground="#666").pack(pady=5)
        
        self.pos_label = ttk.Label(frame, text="Waiting...", foreground="#666")
        self.pos_label.pack(pady=10)
        
        # Bind keys to picker window (works even without grab_set)
        self._win.bind("<space>", self._on_space)
        self._win.bind("<Escape>", self._on_cancel)
        self._win.bind("<Button-1>", self._on_click)
        
        # Start mouse tracking
        self._start_tracking()
        
        # Set focus to picker window
        self._win.focus_force()
        
    def _start_tracking(self) -> None:
        """Start tracking mouse position."""
        def track():
            if self._done or not self._win or not self._win.winfo_exists():
                return
            try:
                x, y = self._win.winfo_pointerxy()
                self.pos_label.config(text=f"X: {x}  Y: {y}")
                # Track at ~60fps
                self._win.after(16, track)
            except tk.TclError:
                pass
        
        track()
        
    def _on_space(self, event=None) -> None:
        """Handle SPACE key."""
        self._on_confirm()
        
    def _on_click(self, event=None) -> None:
        """Click anywhere to confirm coordinate."""
        try:
            x, y = self._win.winfo_pointerxy() if self._win else (0, 0)
            self._result = (x, y)
        except tk.TclError:
            self._result = (0, 0)
        self._done = True
        self._close()
        
    def _on_cancel(self, event=None) -> None:
        """Cancel picker."""
        self._result = None
        self._done = True
        self._close()
        
    def _on_confirm(self) -> None:
        """Confirm coordinate."""
        try:
            x, y = self._win.winfo_pointerxy() if self._win else (0, 0)
            self._result = (x, y)
        except tk.TclError:
            self._result = (0, 0)
        self._done = True
        self._close()
        
    def _close(self) -> None:
        """Close picker window."""
        try:
            if self._win and self._win.winfo_exists():
                self._win.destroy()
        except Exception:
            pass
        # Call callback on main thread
        self.parent.after(0, lambda: self.on_complete(self._result))


def pick_coordinate(parent: tk.Tk, on_complete) -> None:
    """Open coordinate picker (non-blocking)."""
    picker = SimpleCoordinatePicker(parent, on_complete)
    picker.show()


# ==============================================================================
# CONFIG HANDLER
# ==============================================================================

def save_config(path: Path | None = None, **kwargs) -> bool:
    """Save config to INI file."""
    if path is None:
        path = CONFIG_PATH
        
    ensure_macros_dir()
    
    cfg = configparser.ConfigParser()
    
    # General
    cfg["General"] = {
        "hide_window": str(int(kwargs.get("hide_window", False))),
        "loop_enabled": str(int(kwargs.get("loop_enabled", False))),
        "loop_interval": str(kwargs.get("loop_interval", DEFAULT_LOOP_INTERVAL)),
        "trigger_key": str(kwargs.get("trigger_key", "")),
    }
    
    # Selected windows
    windows = kwargs.get("target_windows", [])
    cfg["Targets"] = {"count": str(len(windows))}
    for i, (title, hwnd) in enumerate(windows):
        cfg[f"Target{i}"] = {"title": str(title), "hwnd": str(hwnd)}
    
    # Actions
    actions = kwargs.get("actions", [])
    cfg["Actions"] = {"count": str(len(actions))}
    for i, action in enumerate(actions):
        section = f"Action{i}"
        cfg[section] = {k: str(v) for k, v in action.items()}
    
    try:
        with open(path, "w") as f:
            cfg.write(f)
        log(f"Config saved to {path}")
        return True
    except Exception as e:
        log(f"save_config error: {e}")
        return False


def load_config(path: Path | None = None) -> Dict[str, Any]:
    """Load config from INI file."""
    if path is None:
        path = CONFIG_PATH
        
    if not path.exists():
        return {}
        
    cfg = configparser.ConfigParser()
    cfg.read(path)
    
    state = {
        "hide_window": cfg.getboolean("General", "hide_window", fallback=False),
        "loop_enabled": cfg.getboolean("General", "loop_enabled", fallback=False),
        "loop_interval": cfg.getint("General", "loop_interval", fallback=DEFAULT_LOOP_INTERVAL),
        "trigger_key": cfg.get("General", "trigger_key", fallback=""),
        "target_windows": [],
        "actions": [],
    }
    
    # Load targets
    try:
        count = cfg.getint("Targets", "count", fallback=0)
        for i in range(count):
            title = cfg.get(f"Target{i}", "title", fallback="")
            hwnd = cfg.getint(f"Target{i}", "hwnd", fallback=0)
            if title:
                state["target_windows"].append((title, hwnd))
    except Exception as e:
        log(f"load_targets error: {e}")
    
    # Load actions
    try:
        count = cfg.getint("Actions", "count", fallback=0)
        for i in range(count):
            section = f"Action{i}"
            action = dict(cfg[section])
            state["actions"].append(action)
    except Exception as e:
        log(f"load_actions error: {e}")
    
    log(f"Config loaded: {len(state['actions'])} actions, {len(state['target_windows'])} targets")
    return state


# ==============================================================================
# HOTKEY MANAGER
# ==============================================================================

class HotkeyManager:
    """Manages global hotkey listener."""
    
    def __init__(self, on_trigger, on_key_set=None):
        self.on_trigger = on_trigger
        self.on_key_set = on_key_set
        self._trigger_key = ""
        self._listener: keyboard.Listener | None = None
        self._listening_for_key = False
        self._lock = threading.Lock()
        
    def set_key(self, key: str) -> None:
        """Set the hotkey."""
        with self._lock:
            self._trigger_key = key
        
    def get_key(self) -> str:
        """Get current hotkey."""
        with self._lock:
            return self._trigger_key
        
    def start_listening(self) -> None:
        """Start listening for a key to set as hotkey."""
        self._listening_for_key = True
        self._start_listener()
        
    def start_hotkey(self) -> None:
        """Start listening for hotkey presses."""
        self._listening_for_key = False
        self._start_listener()
        
    def stop(self) -> None:
        """Stop the listener."""
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
            self._listening_for_key = False
            
    def _start_listener(self) -> None:
        """Start the pynput listener."""
        self.stop()
        
        def on_press(key):
            try:
                key_str = self._key_to_str(key)
                
                if self._listening_for_key:
                    # Set hotkey mode
                    self.set_key(key_str)
                    self._listening_for_key = False
                    if self.on_key_set:
                        self.on_key_set(key_str)
                else:
                    # Hotkey trigger mode
                    with self._lock:
                        trigger = self._trigger_key
                    if key_str == trigger:
                        self.on_trigger()
                        
            except Exception as e:
                log(f"hotkey error: {e}")
                
        try:
            self._listener = keyboard.Listener(on_press=on_press)
            self._listener.start()
        except Exception as e:
            log(f"hotkey start error: {e}")
            
    def _key_to_str(self, key) -> str:
        """Convert key object to string."""
        if hasattr(key, 'char') and key.char:
            return key.char.lower()
        if hasattr(key, 'name'):
            return key.name.lower()
        return str(key).lower().strip("'")

    def restart(self) -> None:
        """Restart listener with current key."""
        if self._trigger_key:
            self.start_hotkey()
        else:
            self.stop()

# ==============================================================================
# MACRO APP (MAIN GUI)
# ==============================================================================

class MacroApp:
    """Main application window."""
    
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("550x680")
        self.root.minsize(500, 650)
        
        # State
        self.actions: List[Dict[str, Any]] = []
        self.selected_windows: List[Tuple[str, int]] = []
        self.hide_window = tk.BooleanVar(value=False)
        self.loop_enabled = tk.BooleanVar(value=False)
        self.loop_interval = tk.IntVar(value=DEFAULT_LOOP_INTERVAL)
        self.trigger_key = tk.StringVar(value="")
        
        # Action form
        self.action_type = tk.StringVar(value="click")
        self.pick_x = tk.IntVar(value=0)
        self.pick_y = tk.IntVar(value=0)
        self.mouse_button = tk.StringVar(value="left")
        self.key_name = tk.StringVar(value="enter")
        self.action_sleep = tk.IntVar(value=DEFAULT_SLEEP)
        self.action_hold = tk.IntVar(value=DEFAULT_HOLD)
        
        # Playback state
        self.is_running = False
        self.playback_thread: threading.Thread | None = None
        
        # Hotkey manager
        self.hotkey = HotkeyManager(self.play_macro, self._on_hotkey_set)
        
        # Build UI
        self._build_ui()
        
        # Load config
        self._load_config()
        
        # Start hotkey listener
        self._update_hotkey_listener()
        
    def _build_ui(self) -> None:
        """Build the GUI."""
        # Style
        style = ttk.Style(self.root)
        style.theme_use("clam")
        
        # Main frame
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill=tk.BOTH, expand=True)
        
        # 1. Title
        ttk.Label(main, text="MazCro - Macro Recorder", font=("Segoe UI", 16, "bold")).pack(pady=(0, 15))
        
        # 2. Windows section
        win_frame = ttk.LabelFrame(main, text="Target Windows", padding=8)
        win_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Window list
        list_frame = ttk.Frame(win_frame)
        list_frame.pack(fill=tk.BOTH, expand=True)
        
        scroll_y = ttk.Scrollbar(list_frame, orient=tk.VERTICAL)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.window_list = tk.Listbox(
            list_frame, 
            selectmode=tk.MULTIPLE,
            yscrollcommand=scroll_y.set,
            height=4,
            bg="#f0f0f0"
        )
        self.window_list.pack(fill=tk.BOTH, expand=True)
        scroll_y.config(command=self.window_list.yview)
        
        # Window refresh
        ttk.Button(win_frame, text="Refresh Windows", command=self._refresh_windows).pack(fill=tk.X, pady=(5, 0))
        
        # Hide checkbox
        ttk.Checkbutton(win_frame, text="Hide target windows while running", variable=self.hide_window).pack(anchor=tk.W, pady=(5, 0))
        
        # 3. Actions section
        action_frame = ttk.LabelFrame(main, text="Add Action", padding=8)
        action_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Type selector
        type_frame = ttk.Frame(action_frame)
        type_frame.pack(fill=tk.X)
        ttk.Label(type_frame, text="Type:").pack(side=tk.LEFT)
        
        for label, value in (("Click", "click"), ("Key", "key")):
            ttk.Radiobutton(type_frame, text=label, value=value, variable=self.action_type, command=self._sync_action_form).pack(side=tk.LEFT, padx=(0, 10))
        
        # Click fields
        self.click_frame = ttk.Frame(action_frame)
        ttk.Label(self.click_frame, text="X:").pack(side=tk.LEFT)
        ttk.Spinbox(self.click_frame, from_=0, to=10000, textvariable=self.pick_x, width=8).pack(side=tk.LEFT, padx=(2, 10))
        ttk.Label(self.click_frame, text="Y:").pack(side=tk.LEFT)
        ttk.Spinbox(self.click_frame, from_=0, to=10000, textvariable=self.pick_y, width=8).pack(side=tk.LEFT, padx=(2, 10))
        ttk.Label(self.click_frame, text="Button:").pack(side=tk.LEFT, padx=(10, 0))
        ttk.Combobox(self.click_frame, textvariable=self.mouse_button, values=("left", "right", "middle"), state="readonly", width=8).pack(side=tk.LEFT, padx=(2, 10))
        self.click_frame.pack(fill=tk.X, pady=(5, 0))
        
        # Key fields
        self.key_frame = ttk.Frame(action_frame)
        ttk.Label(self.key_frame, text="Key:").pack(side=tk.LEFT)
        ttk.Entry(self.key_frame, textvariable=self.key_name, width=20).pack(side=tk.LEFT, padx=(2, 10))
        self.key_frame.pack_forget()  # Hidden initially
        
        # Pick coordinate button
        ttk.Button(action_frame, text="Pick Coordinate", command=self._on_pick_coordinate).pack(fill=tk.X, pady=(5, 0))
        
        # Timing fields
        time_frame = ttk.Frame(action_frame)
        time_frame.pack(fill=tk.X, pady=(5, 0))
        ttk.Label(time_frame, text="Sleep (ms):").pack(side=tk.LEFT)
        ttk.Spinbox(time_frame, from_=0, to=10000, textvariable=self.action_sleep, width=8).pack(side=tk.LEFT, padx=(2, 10))
        ttk.Label(time_frame, text="Hold (ms):").pack(side=tk.LEFT, padx=(10, 0))
        ttk.Spinbox(time_frame, from_=0, to=10000, textvariable=self.action_hold, width=8).pack(side=tk.LEFT, padx=(2, 10))
        
        # Add button
        ttk.Button(action_frame, text="+ Add to Queue", command=self._add_action).pack(fill=tk.X, pady=(5, 0))
        
        # 4. Queue section
        queue_frame = ttk.LabelFrame(main, text="Action Queue", padding=8)
        queue_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        queue_list_frame = ttk.Frame(queue_frame)
        queue_list_frame.pack(fill=tk.BOTH, expand=True)
        
        scroll_q_y = ttk.Scrollbar(queue_list_frame, orient=tk.VERTICAL)
        scroll_q_y.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.queue_list = tk.Listbox(queue_list_frame, yscrollcommand=scroll_q_y.set, height=6)
        self.queue_list.pack(fill=tk.BOTH, expand=True)
        scroll_q_y.config(command=self.queue_list.yview)
        
        # Queue buttons
        btn_frame = ttk.Frame(queue_frame)
        btn_frame.pack(fill=tk.X, pady=(5, 0))
        ttk.Button(btn_frame, text="Remove Selected", command=self._remove_selected).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="Clear All", command=self._clear_actions).pack(side=tk.LEFT)
        
        # 5. Playback section
        play_frame = ttk.LabelFrame(main, text="Playback", padding=8)
        play_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Hotkey
        hotkey_frame = ttk.Frame(play_frame)
        hotkey_frame.pack(fill=tk.X)
        ttk.Button(hotkey_frame, text="Set Hotkey", command=self._on_set_hotkey, width=12).pack(side=tk.LEFT)
        ttk.Label(hotkey_frame, textvariable=self.trigger_key, foreground="#2563eb", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(10, 0))
        
        # Loop options
        loop_frame = ttk.Frame(play_frame)
        loop_frame.pack(fill=tk.X, pady=(5, 0))
        ttk.Checkbutton(loop_frame, text="Loop", variable=self.loop_enabled, command=self._update_hotkey_listener).pack(side=tk.LEFT)
        ttk.Label(loop_frame, text="Interval (ms):").pack(side=tk.LEFT, padx=(10, 0))
        ttk.Spinbox(loop_frame, from_=100, to=3600000, textvariable=self.loop_interval, width=10).pack(side=tk.LEFT, padx=(2, 0))
        
        # Play button
        self.play_btn = ttk.Button(play_frame, text="Play", command=self._on_play)
        self.play_btn.pack(fill=tk.X, pady=(5, 0))
        
        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        status_frame = ttk.Frame(self.root, relief=tk.SUNKEN, padding=5)
        status_frame.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Label(status_frame, textvariable=self.status_var, anchor=tk.W).pack(side=tk.LEFT)
        
        # Menu
        self._build_menu()
        
    def _build_menu(self) -> None:
        """Build the menu bar."""
        menu = tk.Menu(self.root)
        self.root.config(menu=menu)
        
        file_menu = tk.Menu(menu, tearoff=False)
        menu.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Save", command=self._on_save)
        file_menu.add_command(label="Load", command=self._on_load)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_exit)
        
    def _sync_action_form(self) -> None:
        """Sync action form based on type."""
        if self.action_type.get() == "click":
            self.click_frame.pack(fill=tk.X, pady=(5, 0))
            self.key_frame.pack_forget()
        else:
            self.click_frame.pack_forget()
            self.key_frame.pack(fill=tk.X, pady=(5, 0))
            
    def _refresh_windows(self) -> None:
        """Refresh the window list."""
        try:
            windows = list_windows()
            self.window_list.delete(0, tk.END)
            for title, hwnd in windows:
                self.window_list.insert(tk.END, f"{title} (ID: {hwnd})")
            log(f"Refreshed {len(windows)} windows")
        except Exception as e:
            log(f"refresh_windows error: {e}")
            
    def _on_pick_coordinate(self) -> None:
        """Open coordinate picker."""
        def on_complete(result):
            if result:
                x, y = result
                self.pick_x.set(x)
                self.pick_y.set(y)
                self.status_var.set(f"Coordinate picked: ({x}, {y})")
                log(f"Picked coordinate: ({x}, {y})")
                
        pick_coordinate(self.root, on_complete)
        
    def _add_action(self) -> None:
        """Add action to queue."""
        action_type = self.action_type.get()
        
        if action_type == "click":
            action = {
                "type": "click",
                "x": self.pick_x.get(),
                "y": self.pick_y.get(),
                "button": self.mouse_button.get(),
                "sleep": self.action_sleep.get(),
                "hold": self.action_hold.get(),
            }
        else:
            action = {
                "type": "key",
                "key": self.key_name.get(),
                "sleep": self.action_sleep.get(),
                "hold": self.action_hold.get(),
            }
            
        self.actions.append(action)
        self._update_queue_display()
        self.status_var.set(f"Added: {action_type}")
        log(f"Added action: {action_type}")
        
    def _remove_selected(self) -> None:
        """Remove selected action."""
        selection = self.queue_list.curselection()
        if selection:
            idx = selection[0]
            self.actions.pop(idx)
            self._update_queue_display()
            self.status_var.set("Removed action")
            
    def _clear_actions(self) -> None:
        """Clear all actions."""
        self.actions.clear()
        self._update_queue_display()
        self.status_var.set("Cleared all actions")
        
    def _update_queue_display(self) -> None:
        """Update the queue listbox."""
        self.queue_list.delete(0, tk.END)
        for i, action in enumerate(self.actions, 1):
            if action["type"] == "click":
                text = f"{i}. Click ({action['x']}, {action['y']}) {action['button']}"
            else:
                text = f"{i}. Key: {action['key']}"
            self.queue_list.insert(tk.END, text)
            
    def _on_set_hotkey(self) -> None:
        """Start setting hotkey."""
        self.status_var.set("Press a key to set hotkey...")
        self.hotkey.start_listening()
        
    def _on_hotkey_set(self, key: str) -> None:
        """Called when hotkey is set."""
        self.trigger_key.set(key)
        self.status_var.set(f"Hotkey set to: {key}")
        self._update_hotkey_listener()
        
    def _update_hotkey_listener(self) -> None:
        """Update hotkey listener state."""
        if self.trigger_key.get() and self.loop_enabled.get():
            self.hotkey.restart()
        else:
            self.hotkey.stop()
            
    def _on_play(self) -> None:
        """Play the macro."""
        if not self.actions:
            messagebox.showwarning(APP_NAME, "No actions to play!")
            return
            
        self.play_macro()
        
    def play_macro(self) -> None:
        """Execute the macro actions."""
        if self.is_running:
            return  # Already running
            
        # Start on worker thread
        thread = threading.Thread(target=self._play_worker, daemon=True)
        thread.start()
        
    def _play_worker(self) -> None:
        """Worker thread for playback."""
        self.is_running = True
        self.root.after(0, lambda: self.play_btn.config(state=tk.DISABLED))
        self.root.after(0, lambda: self.status_var.set("Running..."))
        
        try:
            while True:
                self._execute_actions()
                
                # Check if loop enabled
                if not self.loop_enabled.get():
                    break
                    
                # Wait before looping
                interval = self.loop_interval.get() / 1000.0
                self.root.after(0, lambda: self.status_var.set(f"Looping in {interval:.1f}s..."))
                time.sleep(interval)
                
        except Exception as e:
            log(f"playback error: {e}")
            
        finally:
            self.is_running = False
            self.root.after(0, lambda: self.play_btn.config(state=tk.NORMAL))
            self.root.after(0, lambda: self.status_var.set("Done"))
            
    def _execute_actions(self) -> None:
        """Execute all actions."""
        for i, action in enumerate(self.actions, 1):
            if not self.is_running:
                break
                
            self.root.after(0, lambda n=i: self.status_var.set(f"Action {n}/{len(self.actions)}..."))
            
            try:
                if action["type"] == "click":
                    self._execute_click(action)
                elif action["type"] == "key":
                    self._execute_key(action)
                    
                # Sleep after action
                sleep_ms = action.get("sleep", DEFAULT_SLEEP)
                time.sleep(sleep_ms / 1000.0)
                
            except Exception as e:
                log(f"action {i} error: {e}")
                
    def _execute_click(self, action: Dict[str, Any]) -> None:
        """Execute a click action."""
        x = action["x"]
        y = action["y"]
        button = action.get("button", "left")
        hold_ms = action.get("hold", DEFAULT_HOLD)
        
        pyautogui.moveTo(x, y)
        pyautogui.mouseDown(button=button)
        time.sleep(hold_ms / 1000.0)
        pyautogui.mouseUp(button=button)
        
    def _execute_key(self, action: Dict[str, Any]) -> None:
        """Execute a key action."""
        key = action.get("key", "enter")
        hold_ms = action.get("hold", DEFAULT_HOLD)
        
        pyautogui.keyDown(key)
        time.sleep(hold_ms / 1000.0)
        pyautogui.keyUp(key)
        
    def _on_save(self) -> None:
        """Save config."""
        try:
            # Get selected windows
            selected_indices = self.window_list.curselection()
            self.selected_windows = []
            for idx in selected_indices:
                text = self.window_list.get(idx)
                # Parse "Title (ID: 12345)"
                try:
                    hwnd = int(text.split("(ID: ")[-1].rstrip(")"))
                    title = text.split(" (ID:")[0]
                    self.selected_windows.append((title, hwnd))
                except Exception:
                    pass
                    
            state = {
                "hide_window": self.hide_window.get(),
                "loop_enabled": self.loop_enabled.get(),
                "loop_interval": self.loop_interval.get(),
                "trigger_key": self.trigger_key.get(),
                "target_windows": self.selected_windows,
                "actions": self.actions,
            }
            
            path = filedialog.asksaveasfilename(
                initialdir=CONFIG_PATH.parent,
                initialfile=CONFIG_PATH.name,
                defaultextension=".ini",
                filetypes=[("INI files", "*.ini"), ("All files", "*.*")]
            )
            
            if path:
                save_config(Path(path), **state)
                messagebox.showinfo(APP_NAME, "Config saved!")
                
        except Exception as e:
            log(f"save error: {e}")
            messagebox.showerror(APP_NAME, f"Save failed: {e}")
            
    def _on_load(self) -> None:
        """Load config."""
        try:
            path = filedialog.askopenfilename(
                initialdir=CONFIG_PATH.parent,
                defaultextension=".ini",
                filetypes=[("INI files", "*.ini"), ("All files", "*.*")]
            )
            
            if path:
                state = load_config(Path(path))
                
                # Restore UI state
                self.hide_window.set(state.get("hide_window", False))
                self.loop_enabled.set(state.get("loop_enabled", False))
                self.loop_interval.set(state.get("loop_interval", DEFAULT_LOOP_INTERVAL))
                self.trigger_key.set(state.get("trigger_key", ""))
                
                # Restore windows
                self.selected_windows = state.get("target_windows", [])
                self.window_list.delete(0, tk.END)
                for title, hwnd in self.selected_windows:
                    self.window_list.insert(tk.END, f"{title} (ID: {hwnd})")
                    
                # Restore actions
                self.actions = state.get("actions", [])
                self._update_queue_display()
                
                # Update hotkey listener
                self._update_hotkey_listener()
                
                messagebox.showinfo(APP_NAME, "Config loaded!")
                
        except Exception as e:
            log(f"load error: {e}")
            messagebox.showerror(APP_NAME, f"Load failed: {e}")
            
    def _load_config(self) -> None:
        """Load default config if exists."""
        if CONFIG_PATH.exists():
            state = load_config(CONFIG_PATH)
            
            self.hide_window.set(state.get("hide_window", False))
            self.loop_enabled.set(state.get("loop_enabled", False))
            self.loop_interval.set(state.get("loop_interval", DEFAULT_LOOP_INTERVAL))
            self.trigger_key.set(state.get("trigger_key", ""))
            
            self.selected_windows = state.get("target_windows", [])
            self.actions = state.get("actions", [])
            
            self._update_queue_display()
            self._update_hotkey_listener()
            
    def _on_exit(self) -> None:
        """Exit the app."""
        self.hotkey.stop()
        self.root.destroy()
        
    def run(self) -> None:
        """Run the main loop."""
        self.root.protocol("WM_DELETE_WINDOW", self._on_exit)
        self._refresh_windows()
        self.root.mainloop()


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    """Entry point."""
    try:
        ensure_macros_dir()
        app = MacroApp()
        app.run()
    except KeyboardInterrupt:
        log("Interrupted by user")
    except Exception as e:
        log(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
