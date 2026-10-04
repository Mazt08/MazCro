"""
Macro Script by Mazt  –  Python port of the AutoHotkey v2 original
Dependencies (install once):
    pip install pywin32 keyboard
"""

import os
import sys
import threading
import time
import winreg
import ctypes
import configparser
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import win32gui
import win32con
import win32api
import win32process
import keyboard  # global hotkey support

# ─────────────────────────────────────────────────────────────────────────────
# Constants / Config paths
# ─────────────────────────────────────────────────────────────────────────────
CONFIG_DIR  = r"D:\Macro"
CONFIG_FILE = os.path.join(CONFIG_DIR, "auto_click_config.ini")

# PostMessage mouse / key constants
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP   = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP   = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP   = 0x0208
WM_KEYDOWN     = 0x0100
WM_KEYUP       = 0x0101

# VK codes for modifiers
VK_SHIFT   = 0x10
VK_CTRL    = 0x11
VK_ALT     = 0x12
VK_CAPITAL = 0x14

# ─────────────────────────────────────────────────────────────────────────────
# Dark-mode detection
# ─────────────────────────────────────────────────────────────────────────────
def is_dark_mode() -> bool:
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        ) as key:
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return val == 0
    except Exception:
        return True

# ─────────────────────────────────────────────────────────────────────────────
# Colour palettes (mirrors AHK clr* variables)
# ─────────────────────────────────────────────────────────────────────────────
def build_palette(dark: bool) -> dict:
    if dark:
        return {
            "BG":       "#1B1B1F",
            "Card":     "#252529",
            "CardAlt":  "#2D2D33",
            "Control":  "#323238",
            "ListBG":   "#202024",
            "Font":     "#F2F2F4",
            "Sub":      "#9A9AA3",
            "Border":   "#3A3A42",
            "Accent":   "#4CC2FF",
            "Success":  "#4ADE80",
            "Danger":   "#F87171",
        }
    else:
        return {
            "BG":       "#F5F5F7",
            "Card":     "#FFFFFF",
            "CardAlt":  "#F0F0F3",
            "Control":  "#FFFFFF",
            "ListBG":   "#FAFAFC",
            "Font":     "#1A1A1E",
            "Sub":      "#6B6B75",
            "Border":   "#E2E2E8",
            "Accent":   "#0078D4",
            "Success":  "#16A34A",
            "Danger":   "#DC2626",
        }

# ─────────────────────────────────────────────────────────────────────────────
# Win32 helpers
# ─────────────────────────────────────────────────────────────────────────────
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW  = 0x00040000

def _enum_windows_callback(hwnd, result_list):
    """Filter to visible, titled, non-tool windows – mirrors AHK BuildWindowList."""
    if not win32gui.IsWindowVisible(hwnd):
        return True
    title = win32gui.GetWindowText(hwnd)
    if not title or title == "Program Manager":
        return True
    ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    if (ex_style & WS_EX_TOOLWINDOW) and not (ex_style & WS_EX_APPWINDOW):
        return True
    result_list.append((title, hwnd))
    return True

def build_window_list() -> tuple[list[str], list[int]]:
    """Returns (display_strings, hwnd_list)."""
    pairs: list[tuple[str, int]] = []
    win32gui.EnumWindows(_enum_windows_callback, pairs)
    labels = [f"{t} [{h}]" for t, h in pairs]
    hwnds  = [h for _, h in pairs]
    return labels, hwnds

def win_exists(hwnd: int) -> bool:
    return bool(win32gui.IsWindow(hwnd))

def win_show(hwnd: int):
    win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

def win_hide(hwnd: int):
    win32gui.ShowWindow(hwnd, win32con.SW_HIDE)

def get_active_hwnd() -> int:
    return win32gui.GetForegroundWindow()

def post_message(hwnd: int, msg: int, wparam: int, lparam: int):
    win32api.PostMessage(hwnd, msg, wparam, lparam)

def vk_for_name(name: str) -> int:
    """Get virtual-key code from a key name string (e.g. 'a', 'F1', 'Return')."""
    # Map common names to VK codes
    NAME_MAP = {
        "return": 0x0D, "enter": 0x0D,
        "space": 0x20, "tab": 0x09,
        "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
        "insert": 0x2D, "home": 0x24, "end": 0x23,
        "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
        "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
        "escape": 0x1B, "esc": 0x1B,
        "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
        "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
        "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
        "numpad0": 0x60, "numpad1": 0x61, "numpad2": 0x62, "numpad3": 0x63,
        "numpad4": 0x64, "numpad5": 0x65, "numpad6": 0x66, "numpad7": 0x67,
        "numpad8": 0x68, "numpad9": 0x69,
        "multiply": 0x6A, "add": 0x6B, "subtract": 0x6D,
        "decimal": 0x6E, "divide": 0x6F,
    }
    low = name.lower()
    if low in NAME_MAP:
        return NAME_MAP[low]
    if len(name) == 1:
        return win32api.VkKeyScan(name) & 0xFF
    return 0

def sc_for_vk(vk: int) -> int:
    """Map VK code to scan code."""
    return win32api.MapVirtualKey(vk, 0)  # MAPVK_VK_TO_VSC

def sc_for_name(name: str) -> int:
    vk = vk_for_name(name)
    if vk:
        return sc_for_vk(vk)
    return 0

def make_lparam_key(sc: int, is_up: bool) -> int:
    lp = 0x00000001 | (sc << 16)
    if is_up:
        lp |= 0xC0000000
    return lp

# ─────────────────────────────────────────────────────────────────────────────
# ExecuteQueue logic
# ─────────────────────────────────────────────────────────────────────────────
def execute_queue(target_hwnds: list[int], action_queue: list[dict]):
    """Mirrors AHK ExecuteQueue: reset modifiers then run all actions."""
    # Step 0 – release any stuck Shift/Ctrl/Alt/CapsLock
    for hwnd in target_hwnds:
        if not win_exists(hwnd):
            continue
        for vk in (VK_SHIFT, VK_CTRL, VK_ALT, VK_CAPITAL):
            sc = sc_for_vk(vk)
            lp = make_lparam_key(sc, is_up=True)
            try:
                post_message(hwnd, WM_KEYUP, vk, lp)
            except Exception:
                pass

    for act in action_queue:
        live = [h for h in target_hwnds if win_exists(h)]
        if not live:
            continue

        if act["type"] == "Click":
            btn = act["button"]
            if btn == "Left":
                msg_down, msg_up = WM_LBUTTONDOWN, WM_LBUTTONUP
            elif btn == "Right":
                msg_down, msg_up = WM_RBUTTONDOWN, WM_RBUTTONUP
            else:
                msg_down, msg_up = WM_MBUTTONDOWN, WM_MBUTTONUP

            x, y   = act["x"], act["y"]
            lparam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)
            hold   = act["hold"]

            if hold > 0:
                for hwnd in live:
                    try:
                        post_message(hwnd, msg_down, 0x0001, lparam)
                    except Exception:
                        pass
                time.sleep(hold / 1000.0)
                for hwnd in live:
                    try:
                        post_message(hwnd, msg_up, 0x0000, lparam)
                    except Exception:
                        pass
            else:
                for hwnd in live:
                    try:
                        post_message(hwnd, msg_down, 0x0001, lparam)
                        post_message(hwnd, msg_up,   0x0000, lparam)
                    except Exception:
                        pass

        elif act["type"] == "Key":
            key_str = act["key"]
            # Parse AHK-style modifiers (^ = Ctrl, ! = Alt, + = Shift)
            mods    = ""
            base    = key_str
            while base and base[0] in ("^", "!", "+"):
                mods += base[0]
                base  = base[1:]

            vk = vk_for_name(base)
            if vk == 0:
                # Fallback: can't resolve VK, skip gracefully
                time.sleep(act["sleep"] / 1000.0)
                continue

            sc = sc_for_name(base)
            lp_down = make_lparam_key(sc, is_up=False)
            lp_up   = make_lparam_key(sc, is_up=True)

            mod_vks = []
            if "^" in mods:
                mod_vks.append(VK_CTRL)
            if "!" in mods:
                mod_vks.append(VK_ALT)
            if "+" in mods:
                mod_vks.append(VK_SHIFT)

            # Press modifiers
            for mvk in mod_vks:
                msc = sc_for_vk(mvk)
                mlp = make_lparam_key(msc, is_up=False)
                for hwnd in live:
                    try:
                        post_message(hwnd, WM_KEYDOWN, mvk, mlp)
                    except Exception:
                        pass

            hold = act["hold"]
            if hold > 0:
                for hwnd in live:
                    try:
                        post_message(hwnd, WM_KEYDOWN, vk, lp_down)
                    except Exception:
                        pass
                time.sleep(hold / 1000.0)
                for hwnd in live:
                    try:
                        post_message(hwnd, WM_KEYUP, vk, lp_up)
                    except Exception:
                        pass
            else:
                for hwnd in live:
                    try:
                        post_message(hwnd, WM_KEYDOWN, vk, lp_down)
                        post_message(hwnd, WM_KEYUP,   vk, lp_up)
                    except Exception:
                        pass

            # Release modifiers in reverse
            for mvk in reversed(mod_vks):
                msc = sc_for_vk(mvk)
                mlp = make_lparam_key(msc, is_up=True)
                for hwnd in live:
                    try:
                        post_message(hwnd, WM_KEYUP, mvk, mlp)
                    except Exception:
                        pass

        time.sleep(act["sleep"] / 1000.0)

# ─────────────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────────────
def ensure_config_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# Main Application
# ─────────────────────────────────────────────────────────────────────────────
class MacroApp:
    def __init__(self):
        # ── State ──────────────────────────────────────────────────────────
        self.target_hwnds:       list[int]  = []
        self.action_queue:       list[dict] = []
        self.is_running:         bool       = False
        self.is_looping:         bool       = False
        self.currently_hidden:   int        = 0
        self.selected_hk:        str        = ""
        self.hwnd_list:          list[int]  = []
        self.win_labels:         list[str]  = []
        self.picked_x:           int        = 0
        self.picked_y:           int        = 0
        self.loop_thread:        threading.Thread | None = None
        self.prev_active_hwnd:   int        = 0
        self._registered_hk:     str        = ""   # last keyboard.add_hotkey key

        try:
            self.prev_active_hwnd = get_active_hwnd()
        except Exception:
            pass

        # ── Theme ──────────────────────────────────────────────────────────
        self.dark   = is_dark_mode()
        self.clr    = build_palette(self.dark)

        # ── Root window ────────────────────────────────────────────────────
        self.root = tk.Tk()
        self.root.title("Macro Script by Mazt")
        self.root.resizable(False, False)
        self.root.configure(bg=self.clr["BG"])

        # Remove default title-bar maximize/minimize on Windows
        # (tkinter has no -MaximizeBox; we just use resizable=False which is enough)

        self._build_menu()
        self._build_ui()
        self._build_window_list_initial()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Control-s>", lambda e: self._menu_save_now())

    # ─────────────────────────────────────────────────────────────────────
    # Menu
    # ─────────────────────────────────────────────────────────────────────
    def _build_menu(self):
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Save Now\t Ctrl+S", command=self._menu_save_now)
        file_menu.add_command(label="Save As…",          command=self._menu_save_as)
        file_menu.add_command(label="Load…",             command=self._menu_load_from)
        file_menu.add_separator()
        file_menu.add_command(label="Open Config Folder", command=self._menu_open_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Exit",              command=self.root.destroy)
        menubar.add_cascade(label="File", menu=file_menu)
        self.root.config(menu=menubar)

    # ─────────────────────────────────────────────────────────────────────
    # UI construction
    # ─────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        clr = self.clr
        W   = 440   # total window inner width

        # We use a single canvas / frame approach; tkinter doesn't have
        # pixel-perfect positioning like AHK, so we replicate via place().
        self.canvas = tk.Frame(self.root, bg=clr["BG"])
        self.canvas.pack(fill="both", expand=True, padx=0, pady=0)

        def place(widget, x, y, w=None, h=None):
            kw = {"x": x, "y": y}
            if w is not None:
                kw["width"]  = w
            if h is not None:
                kw["height"] = h
            widget.place(**kw)

        # ── Header (y=0, h=42) ─────────────────────────────────────────
        hdr = tk.Frame(self.canvas, bg=clr["Card"], width=W, height=42)
        place(hdr, 0, 0, W, 42)

        accent_bar = tk.Frame(self.canvas, bg=clr["Accent"], width=4, height=42)
        place(accent_bar, 0, 0, 4, 42)

        lbl_title = tk.Label(self.canvas, text="Macro Script by Mazt",
                             bg=clr["Card"], fg=clr["Accent"],
                             font=("Segoe UI Variable Display", 12, "bold"), anchor="w")
        place(lbl_title, 16, 6, W - 20, 20)

        lbl_sub = tk.Label(self.canvas, text="Multi-client automation utility",
                           bg=clr["Card"], fg=clr["Sub"],
                           font=("Segoe UI", 8), anchor="w")
        place(lbl_sub, 16, 26, W - 20, 14)

        # ── Section 1: Target Windows (y=50, h=122) ────────────────────
        S1Y = 50
        sec1 = tk.Frame(self.canvas, bg=clr["Card"], width=W, height=122)
        place(sec1, 0, S1Y, W, 122)

        place(tk.Frame(self.canvas, bg=clr["Accent"], width=3, height=14), 0, S1Y + 10, 3, 14)

        place(tk.Label(self.canvas, text="Target Windows",
                       bg=clr["Card"], fg=clr["Font"],
                       font=("Segoe UI Variable Display", 10, "bold"), anchor="w"),
              16, S1Y + 8, 300, 16)

        place(tk.Label(self.canvas, text="Ctrl+Click to select multiple",
                       bg=clr["Card"], fg=clr["Sub"],
                       font=("Segoe UI", 8), anchor="w"),
              16, S1Y + 26, 300, 14)

        # ListBox
        lb_frame = tk.Frame(self.canvas, bg=clr["Border"])
        place(lb_frame, 16, S1Y + 42, 380, 50)

        self.lb_win = tk.Listbox(lb_frame, selectmode=tk.EXTENDED,
                                 bg=clr["CardAlt"], fg=clr["Font"],
                                 selectbackground=clr["Accent"],
                                 selectforeground=clr["Card"],
                                 font=("Segoe UI", 9),
                                 borderwidth=0, highlightthickness=0,
                                 activestyle="none",
                                 height=3)
        self.lb_win.pack(fill="both", expand=True)
        self.lb_win.bind("<<ListboxSelect>>", self._on_window_select)

        btn_refresh = tk.Button(self.canvas, text="R",
                                bg=clr["Control"], fg=clr["Font"],
                                activebackground=clr["CardAlt"],
                                font=("Segoe UI", 9), relief="flat",
                                command=self._refresh_window_list)
        place(btn_refresh, 400, S1Y + 42, 30, 24)

        self.chk_hide_var = tk.BooleanVar()
        self.chk_hide = tk.Checkbutton(self.canvas,
                                       text="Hide window while running",
                                       variable=self.chk_hide_var,
                                       bg=clr["Card"], fg=clr["Font"],
                                       activebackground=clr["Card"],
                                       selectcolor=clr["Control"],
                                       font=("Segoe UI", 9),
                                       command=self._toggle_hide)
        place(self.chk_hide, 16, S1Y + 98, W - 30, 20)

        # ── Section 2: Add Action (y=180, h=150) ──────────────────────
        S2Y = 180
        sec2 = tk.Frame(self.canvas, bg=clr["Card"], width=W, height=150)
        place(sec2, 0, S2Y, W, 150)

        place(tk.Frame(self.canvas, bg=clr["Accent"], width=3, height=14), 0, S2Y + 10, 3, 14)
        place(tk.Label(self.canvas, text="Add Action",
                       bg=clr["Card"], fg=clr["Font"],
                       font=("Segoe UI Variable Display", 10, "bold"), anchor="w"),
              16, S2Y + 8, 300, 16)

        place(tk.Label(self.canvas, text="Coordinate",
                       bg=clr["Card"], fg=clr["Sub"],
                       font=("Segoe UI", 8), anchor="w"),
              16, S2Y + 32, 80, 18)

        btn_pick = tk.Button(self.canvas, text="Pick Coordinate",
                             bg=clr["Control"], fg=clr["Font"],
                             activebackground=clr["CardAlt"],
                             font=("Segoe UI", 9), relief="flat",
                             command=self._pick_coord)
        place(btn_pick, 100, S2Y + 28, 140, 24)

        self.lbl_coord = tk.Label(self.canvas, text="X: -   Y: -",
                                  bg=clr["Card"], fg=clr["Sub"],
                                  font=("Segoe UI", 9), anchor="w")
        place(self.lbl_coord, 250, S2Y + 32, 180, 18)

        place(tk.Label(self.canvas, text="Action type",
                       bg=clr["Card"], fg=clr["Sub"],
                       font=("Segoe UI", 8), anchor="w"),
              16, S2Y + 56, 300, 18)

        self.action_type_var = tk.IntVar(value=1)  # 1 = Click, 2 = Key
        rad_click = tk.Radiobutton(self.canvas, text="Mouse Click",
                                   variable=self.action_type_var, value=1,
                                   bg=clr["Card"], fg=clr["Font"],
                                   activebackground=clr["Card"],
                                   selectcolor=clr["Control"],
                                   font=("Segoe UI", 9))
        rad_key   = tk.Radiobutton(self.canvas, text="Keyboard",
                                   variable=self.action_type_var, value=2,
                                   bg=clr["Card"], fg=clr["Font"],
                                   activebackground=clr["Card"],
                                   selectcolor=clr["Control"],
                                   font=("Segoe UI", 9))
        place(rad_click, 16,  S2Y + 74, 110, 20)
        place(rad_key,   130, S2Y + 74, 110, 20)

        self.ddl_mouse_btn = ttk.Combobox(self.canvas,
                                          values=["Left", "Right", "Middle"],
                                          state="readonly",
                                          font=("Segoe UI", 9), width=8)
        self.ddl_mouse_btn.current(0)
        place(self.ddl_mouse_btn, 240, S2Y + 74, 90, 22)

        self.txt_key_input = tk.Entry(self.canvas,
                                      bg=clr["Control"], fg=clr["Font"],
                                      insertbackground=clr["Font"],
                                      font=("Segoe UI", 9), relief="flat",
                                      highlightthickness=1,
                                      highlightcolor=clr["Border"],
                                      highlightbackground=clr["Border"])
        place(self.txt_key_input, 336, S2Y + 74, 86, 22)

        place(tk.Label(self.canvas, text="Sleep after (ms)",
                       bg=clr["Card"], fg=clr["Sub"],
                       font=("Segoe UI", 8), anchor="w"),
              16, S2Y + 100, 110, 18)
        place(tk.Label(self.canvas, text="Hold (ms)",
                       bg=clr["Card"], fg=clr["Sub"],
                       font=("Segoe UI", 8), anchor="w"),
              180, S2Y + 100, 80, 18)

        self.num_sleep = tk.Entry(self.canvas,
                                  bg=clr["Control"], fg=clr["Font"],
                                  insertbackground=clr["Font"],
                                  font=("Segoe UI", 9), relief="flat",
                                  highlightthickness=1,
                                  highlightcolor=clr["Border"],
                                  highlightbackground=clr["Border"])
        self.num_sleep.insert(0, "300")
        place(self.num_sleep, 16, S2Y + 118, 100, 22)

        self.num_hold = tk.Entry(self.canvas,
                                 bg=clr["Control"], fg=clr["Font"],
                                 insertbackground=clr["Font"],
                                 font=("Segoe UI", 9), relief="flat",
                                 highlightthickness=1,
                                 highlightcolor=clr["Border"],
                                 highlightbackground=clr["Border"])
        self.num_hold.insert(0, "50")
        place(self.num_hold, 180, S2Y + 118, 100, 22)

        btn_add = tk.Button(self.canvas, text="+ Add to Queue",
                            bg=clr["Accent"], fg=clr["Card"],
                            activebackground=clr["Accent"],
                            font=("Segoe UI", 9, "bold"), relief="flat",
                            command=self._add_action)
        place(btn_add, 292, S2Y + 118, 130, 22)

        # ── Section 3: Action Queue (y=338, h=200) ────────────────────
        S3Y = 338
        sec3 = tk.Frame(self.canvas, bg=clr["Card"], width=W, height=200)
        place(sec3, 0, S3Y, W, 200)

        place(tk.Frame(self.canvas, bg=clr["Accent"], width=3, height=14), 0, S3Y + 10, 3, 14)
        place(tk.Label(self.canvas, text="Action Queue",
                       bg=clr["Card"], fg=clr["Font"],
                       font=("Segoe UI Variable Display", 10, "bold"), anchor="w"),
              16, S3Y + 8, 300, 16)

        # Treeview as ListView
        tv_frame = tk.Frame(self.canvas, bg=clr["Border"])
        place(tv_frame, 16, S3Y + 30, 404, 130)

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Queue.Treeview",
                        background=clr["ListBG"],
                        foreground=clr["Font"],
                        fieldbackground=clr["ListBG"],
                        rowheight=22,
                        borderwidth=0)
        style.configure("Queue.Treeview.Heading",
                        background=clr["CardAlt"],
                        foreground=clr["Font"],
                        relief="flat")
        style.map("Queue.Treeview",
                  background=[("selected", clr["Accent"])],
                  foreground=[("selected", clr["Card"])])

        self.tv = ttk.Treeview(tv_frame,
                               columns=("#", "Type", "Detail", "Sleep", "Hold"),
                               show="headings",
                               selectmode="browse",
                               style="Queue.Treeview")
        self.tv.heading("#",      text="#")
        self.tv.heading("Type",   text="Type")
        self.tv.heading("Detail", text="Detail")
        self.tv.heading("Sleep",  text="Sleep")
        self.tv.heading("Hold",   text="Hold")
        self.tv.column("#",      width=28,  stretch=False)
        self.tv.column("Type",   width=82,  stretch=False)
        self.tv.column("Detail", width=168, stretch=False)
        self.tv.column("Sleep",  width=60,  stretch=False)
        self.tv.column("Hold",   width=60,  stretch=False)

        tv_scroll = ttk.Scrollbar(tv_frame, orient="vertical", command=self.tv.yview)
        self.tv.configure(yscrollcommand=tv_scroll.set)
        self.tv.pack(side="left", fill="both", expand=True)
        tv_scroll.pack(side="right", fill="y")
        self.tv.bind("<<TreeviewSelect>>", self._on_queue_click)

        place(tk.Label(self.canvas, text="Sleep / Hold",
                       bg=clr["Card"], fg=clr["Sub"],
                       font=("Segoe UI", 8), anchor="w"),
              16, S3Y + 166, 100, 18)

        self.edt_edit_sleep = tk.Entry(self.canvas,
                                       bg=clr["Control"], fg=clr["Font"],
                                       insertbackground=clr["Font"],
                                       font=("Segoe UI", 9), relief="flat",
                                       highlightthickness=1,
                                       highlightcolor=clr["Border"],
                                       highlightbackground=clr["Border"])
        place(self.edt_edit_sleep, 16, S3Y + 182, 60, 22)

        self.edt_edit_hold = tk.Entry(self.canvas,
                                      bg=clr["Control"], fg=clr["Font"],
                                      insertbackground=clr["Font"],
                                      font=("Segoe UI", 9), relief="flat",
                                      highlightthickness=1,
                                      highlightcolor=clr["Border"],
                                      highlightbackground=clr["Border"])
        place(self.edt_edit_hold, 84, S3Y + 182, 60, 22)

        btn_update = tk.Button(self.canvas, text="Update",
                               bg=clr["Control"], fg=clr["Font"],
                               activebackground=clr["CardAlt"],
                               font=("Segoe UI", 9), relief="flat",
                               command=self._update_sleep)
        place(btn_update, 152, S3Y + 182, 130, 22)

        btn_del = tk.Button(self.canvas, text="Remove",
                            bg=clr["Danger"], fg="#FFFFFF",
                            activebackground=clr["Danger"],
                            font=("Segoe UI", 9), relief="flat",
                            command=self._delete_action)
        btn_clear = tk.Button(self.canvas, text="Clear",
                              bg=clr["Control"], fg=clr["Font"],
                              activebackground=clr["CardAlt"],
                              font=("Segoe UI", 9), relief="flat",
                              command=self._clear_actions)
        place(btn_del,   290, S3Y + 182, 62, 22)
        place(btn_clear, 358, S3Y + 182, 62, 22)

        # ── Section 4: Trigger & Options (y=546, h=82) ───────────────
        S4Y = 546
        sec4 = tk.Frame(self.canvas, bg=clr["Card"], width=W, height=82)
        place(sec4, 0, S4Y, W, 82)

        place(tk.Frame(self.canvas, bg=clr["Accent"], width=3, height=14), 0, S4Y + 10, 3, 14)
        place(tk.Label(self.canvas, text="Trigger & Options",
                       bg=clr["Card"], fg=clr["Font"],
                       font=("Segoe UI Variable Display", 10, "bold"), anchor="w"),
              16, S4Y + 8, 300, 16)

        place(tk.Label(self.canvas, text="Trigger hotkey",
                       bg=clr["Card"], fg=clr["Sub"],
                       font=("Segoe UI", 8), anchor="w"),
              16, S4Y + 32, 110, 18)

        # Hotkey entry (plain text entry; user types e.g. "F6" or "ctrl+f6")
        self.hk_entry = tk.Entry(self.canvas,
                                 bg=clr["Control"], fg=clr["Font"],
                                 insertbackground=clr["Font"],
                                 font=("Segoe UI", 9), relief="flat",
                                 highlightthickness=1,
                                 highlightcolor=clr["Border"],
                                 highlightbackground=clr["Border"])
        self.hk_entry.insert(0, "")
        place(self.hk_entry, 16, S4Y + 50, 160, 22)

        self.chk_loop_var = tk.BooleanVar(value=True)
        chk_loop = tk.Checkbutton(self.canvas, text="Loop",
                                  variable=self.chk_loop_var,
                                  bg=clr["Card"], fg=clr["Font"],
                                  activebackground=clr["Card"],
                                  selectcolor=clr["Control"],
                                  font=("Segoe UI", 9))
        place(chk_loop, 200, S4Y + 52, 60, 20)

        place(tk.Label(self.canvas, text="Interval",
                       bg=clr["Card"], fg=clr["Sub"],
                       font=("Segoe UI", 8), anchor="w"),
              264, S4Y + 54, 60, 18)

        self.edt_interval = tk.Entry(self.canvas,
                                     bg=clr["Control"], fg=clr["Font"],
                                     insertbackground=clr["Font"],
                                     font=("Segoe UI", 9), relief="flat",
                                     highlightthickness=1,
                                     highlightcolor=clr["Border"],
                                     highlightbackground=clr["Border"])
        self.edt_interval.insert(0, "500")
        place(self.edt_interval, 326, S4Y + 50, 70, 22)

        place(tk.Label(self.canvas, text="ms",
                       bg=clr["Card"], fg=clr["Sub"],
                       font=("Segoe UI", 8), anchor="w"),
              400, S4Y + 54, 30, 18)

        # ── Status bar (y=636, h=30) ───────────────────────────────────
        SBY = 636
        sec_sb = tk.Frame(self.canvas, bg=clr["Card"], width=W, height=30)
        place(sec_sb, 0, SBY, W, 30)

        self.lbl_status = tk.Label(self.canvas, text="Ready",
                                   bg=clr["Card"], fg=clr["Sub"],
                                   font=("Segoe UI", 9), anchor="w")
        place(self.lbl_status, 16, SBY + 8, 240, 18)

        btn_save = tk.Button(self.canvas, text="Save",
                             bg=clr["Control"], fg=clr["Font"],
                             activebackground=clr["CardAlt"],
                             font=("Segoe UI", 9), relief="flat",
                             command=self._menu_save_as)
        btn_load = tk.Button(self.canvas, text="Load",
                             bg=clr["Control"], fg=clr["Font"],
                             activebackground=clr["CardAlt"],
                             font=("Segoe UI", 9), relief="flat",
                             command=self._menu_load_from)
        btn_open = tk.Button(self.canvas, text="Open",
                             bg=clr["Control"], fg=clr["Font"],
                             activebackground=clr["CardAlt"],
                             font=("Segoe UI", 9), relief="flat",
                             command=self._menu_open_folder)
        place(btn_save, 260, SBY + 4, 56, 22)
        place(btn_load, 320, SBY + 4, 56, 22)
        place(btn_open, 380, SBY + 4, 44, 22)

        # ── Toggle button (y=670) ─────────────────────────────────────
        self.btn_toggle = tk.Button(self.canvas,
                                    text="START  -  Activate Hotkey",
                                    bg=clr["Accent"], fg=clr["Card"],
                                    activebackground=clr["Accent"],
                                    font=("Segoe UI", 11, "bold"), relief="flat",
                                    command=self._toggle_start)
        place(self.btn_toggle, 0, SBY + 36, W, 36)

        # ── Total window size ─────────────────────────────────────────
        total_h = SBY + 36 + 36
        self.root.geometry(f"{W}x{total_h}")
        self.canvas.config(width=W, height=total_h)

    # ─────────────────────────────────────────────────────────────────────
    # Window list
    # ─────────────────────────────────────────────────────────────────────
    def _build_window_list_initial(self):
        self.win_labels, self.hwnd_list = build_window_list()
        self.lb_win.delete(0, tk.END)
        for lbl in self.win_labels:
            self.lb_win.insert(tk.END, lbl)

        # Pre-select the previously active window
        default_idx = 0
        for i, hwnd in enumerate(self.hwnd_list):
            if hwnd == self.prev_active_hwnd:
                default_idx = i
                break
        if self.hwnd_list:
            self.lb_win.selection_set(default_idx)
            self.lb_win.see(default_idx)
            self.target_hwnds = [self.hwnd_list[default_idx]]
        else:
            self.target_hwnds = []

    def _refresh_window_list(self):
        prev_hwnds = list(self.target_hwnds)
        self.win_labels, self.hwnd_list = build_window_list()
        self.lb_win.delete(0, tk.END)
        for lbl in self.win_labels:
            self.lb_win.insert(tk.END, lbl)

        found_idx = -1
        if prev_hwnds:
            for i, hwnd in enumerate(self.hwnd_list):
                if hwnd == prev_hwnds[0]:
                    found_idx = i
                    break
        if found_idx < 0:
            for i, hwnd in enumerate(self.hwnd_list):
                if hwnd == self.prev_active_hwnd:
                    found_idx = i
                    break
        if found_idx < 0 and self.hwnd_list:
            found_idx = 0

        if found_idx >= 0 and self.hwnd_list:
            self.lb_win.selection_set(found_idx)
            self.lb_win.see(found_idx)
            self.target_hwnds = [self.hwnd_list[found_idx]]
        else:
            self.target_hwnds = []

        if (self.currently_hidden
                and not win_exists(self.currently_hidden)):
            self.currently_hidden = 0
            self.chk_hide_var.set(False)

    def _on_window_select(self, event=None):
        sel = self.lb_win.curselection()
        selected = []
        for idx in sel:
            if 0 <= idx < len(self.hwnd_list):
                selected.append(self.hwnd_list[idx])
        self.target_hwnds = selected
        # If we hidden a window that's no longer selected, show it
        if (self.currently_hidden
                and self.currently_hidden not in self.target_hwnds):
            if win_exists(self.currently_hidden):
                win_show(self.currently_hidden)
            self.currently_hidden = 0
            self.chk_hide_var.set(False)

    def _toggle_hide(self):
        if not self.target_hwnds:
            self.chk_hide_var.set(False)
            return
        if self.chk_hide_var.get():
            for hwnd in self.target_hwnds:
                try:
                    win_hide(hwnd)
                except Exception:
                    pass
            self.currently_hidden = self.target_hwnds[0]
        else:
            for hwnd in self.target_hwnds:
                try:
                    win_show(hwnd)
                except Exception:
                    pass
            self.currently_hidden = 0

    # ─────────────────────────────────────────────────────────────────────
    # Coordinate picker
    # ─────────────────────────────────────────────────────────────────────
    def _pick_coord(self):
        if not self.target_hwnds:
            messagebox.showerror("Error", "Please select a target window first.")
            return

        hwnd     = self.target_hwnds[0]
        was_hidden = self.chk_hide_var.get()

        if was_hidden:
            win_show(hwnd)
            time.sleep(0.2)

        # Bring target window to front
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass

        # Show tooltip overlay
        overlay = self._show_pick_tooltip("Click on the target position…")

        def wait_for_click():
            # Poll for left-button press
            while True:
                state = win32api.GetAsyncKeyState(0x01)  # VK_LBUTTON
                if state & 0x8000:
                    break
                time.sleep(0.01)

            # Capture position
            pt         = win32api.GetCursorPos()          # screen coords
            win_under  = win32gui.WindowFromPoint(pt)

            # Convert to client coords relative to target window
            client_pt  = win32gui.ScreenToClient(hwnd, pt)
            mx, my     = client_pt

            overlay.destroy_later = True

            # Verify click was inside target window
            root_hwnd = win32gui.GetAncestor(win_under, 2)  # GA_ROOT = 2
            if root_hwnd != hwnd:
                self.root.after(0, lambda: messagebox.showwarning(
                    "Warning",
                    "Click was outside the target window. Try again."))
                if was_hidden:
                    win_hide(hwnd)
                return

            self.picked_x = mx
            self.picked_y = my
            self.root.after(0, lambda: self.lbl_coord.config(
                text=f"X: {mx}   Y: {my}"))
            if was_hidden:
                win_hide(hwnd)

        # Dismiss tooltip from main thread after worker finishes
        t = threading.Thread(target=wait_for_click, daemon=True)
        t.start()

        def poll_done():
            if t.is_alive():
                self.root.after(100, poll_done)
            else:
                try:
                    overlay.destroy()
                except Exception:
                    pass

        self.root.after(100, poll_done)

    def _show_pick_tooltip(self, text: str) -> tk.Toplevel:
        tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.attributes("-alpha", 0.85)
        tip.configure(bg="#222222")
        tk.Label(tip, text=text, bg="#222222", fg="#FFFFFF",
                 font=("Segoe UI", 10), padx=12, pady=6).pack()
        # Position near cursor
        x, y = win32api.GetCursorPos()
        tip.geometry(f"+{x + 16}+{y + 16}")
        tip.destroy_later = False
        return tip

    # ─────────────────────────────────────────────────────────────────────
    # Action queue operations
    # ─────────────────────────────────────────────────────────────────────
    def _add_action(self):
        sleep_str = self.num_sleep.get().strip()
        hold_str  = self.num_hold.get().strip()
        try:
            sleep_val = max(1, int(sleep_str))
        except ValueError:
            sleep_val = 300
        try:
            hold_val = max(0, int(hold_str))
        except ValueError:
            hold_val = 50

        row_num = len(self.action_queue) + 1

        if self.action_type_var.get() == 1:  # Mouse Click
            if self.picked_x == 0 and self.picked_y == 0:
                messagebox.showwarning("Warning", "Pick a coordinate first.")
                return
            btn = self.ddl_mouse_btn.get()
            act = {
                "type":   "Click",
                "x":      self.picked_x,
                "y":      self.picked_y,
                "button": btn,
                "sleep":  sleep_val,
                "hold":   hold_val,
            }
            self.action_queue.append(act)
            self.tv.insert("", tk.END,
                           values=(row_num, f"{btn} Click",
                                   f"X: {self.picked_x}  Y: {self.picked_y}",
                                   sleep_val, hold_val))
        else:  # Keyboard
            key_str = self.txt_key_input.get().strip()
            if not key_str:
                messagebox.showwarning("Warning", "Enter a valid keyboard input.")
                return
            act = {
                "type":  "Key",
                "key":   key_str,
                "sleep": sleep_val,
                "hold":  hold_val,
            }
            self.action_queue.append(act)
            self.tv.insert("", tk.END,
                           values=(row_num, "Keyboard", key_str,
                                   sleep_val, hold_val))

    def _on_queue_click(self, event=None):
        sel = self.tv.selection()
        if not sel:
            return
        idx = self.tv.index(sel[0])
        if 0 <= idx < len(self.action_queue):
            act = self.action_queue[idx]
            self.edt_edit_sleep.delete(0, tk.END)
            self.edt_edit_sleep.insert(0, str(act["sleep"]))
            self.edt_edit_hold.delete(0, tk.END)
            self.edt_edit_hold.insert(0, str(act["hold"]))

    def _update_sleep(self):
        sel = self.tv.selection()
        if not sel:
            messagebox.showinfo("Info", "Select a row first.")
            return
        idx = self.tv.index(sel[0])
        try:
            new_sleep = int(self.edt_edit_sleep.get().strip())
            if new_sleep < 1:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Warning", "Enter a valid sleep value (ms).")
            return
        try:
            new_hold = int(self.edt_edit_hold.get().strip())
            if new_hold < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Warning", "Enter a valid hold value (ms).")
            return

        self.action_queue[idx]["sleep"] = new_sleep
        self.action_queue[idx]["hold"]  = new_hold
        iid    = sel[0]
        values = list(self.tv.item(iid, "values"))
        values[3] = new_sleep
        values[4] = new_hold
        self.tv.item(iid, values=values)

    def _delete_action(self):
        sel = self.tv.selection()
        if not sel:
            messagebox.showinfo("Info", "Select an action first.")
            return
        idx = self.tv.index(sel[0])
        self.tv.delete(sel[0])
        del self.action_queue[idx]
        # Renumber
        for i, iid in enumerate(self.tv.get_children()):
            values      = list(self.tv.item(iid, "values"))
            values[0]   = i + 1
            self.tv.item(iid, values=values)

    def _clear_actions(self):
        self.tv.delete(*self.tv.get_children())
        self.action_queue.clear()

    # ─────────────────────────────────────────────────────────────────────
    # Status helper
    # ─────────────────────────────────────────────────────────────────────
    def _update_status(self, text: str, color: str = ""):
        fg = color if color else self.clr["Sub"]
        self.lbl_status.config(text=text, fg=fg)

    # ─────────────────────────────────────────────────────────────────────
    # Hotkey / Run logic
    # ─────────────────────────────────────────────────────────────────────
    def _toggle_start(self):
        if not self.is_running:
            hk = self.hk_entry.get().strip()
            if not hk:
                messagebox.showwarning("Warning", "Set a trigger hotkey first.")
                return
            if not self.action_queue:
                messagebox.showwarning("Warning", "The queue is empty.")
                return
            try:
                keyboard.add_hotkey(hk, self._run_actions)
                self._registered_hk = hk
                self.selected_hk    = hk
                self.is_running     = True
                self.is_looping     = False
                self.btn_toggle.config(text="STOP  -  Deactivate Hotkey")
                self._update_status(f"Running  -  hotkey {hk}", self.clr["Success"])
            except Exception as e:
                messagebox.showerror("Error", f"Invalid hotkey or conflict:\n{e}")
        else:
            self.is_looping = False
            if self._registered_hk:
                try:
                    keyboard.remove_hotkey(self._registered_hk)
                except Exception:
                    pass
                self._registered_hk = ""
            self.is_running = False
            self.btn_toggle.config(text="START  -  Activate Hotkey")
            self._update_status("Ready")

    def _run_actions(self):
        """Called from keyboard hotkey thread."""
        if not self.target_hwnds:
            return
        if not any(win_exists(h) for h in self.target_hwnds):
            return

        do_loop     = self.chk_loop_var.get()
        try:
            interval_ms = int(self.edt_interval.get().strip())
        except ValueError:
            interval_ms = 500

        if self.is_looping:
            # Toggle off current loop
            self.is_looping = False
            self.root.after(0, lambda: self._update_status(
                f"Running  -  hotkey {self.selected_hk}", self.clr["Success"]))
            return

        if not do_loop:
            execute_queue(list(self.target_hwnds), list(self.action_queue))
            return

        # Start loop in background thread
        self.is_looping = True
        self.root.after(0, lambda: self.btn_toggle.config(
            text="STOP LOOP  -  or press hotkey"))
        self.root.after(0, lambda: self._update_status("Looping", self.clr["Success"]))

        def loop_worker():
            while self.is_looping:
                if not any(win_exists(h) for h in self.target_hwnds):
                    break
                execute_queue(list(self.target_hwnds), list(self.action_queue))
                if interval_ms > 0:
                    time.sleep(interval_ms / 1000.0)
            self.root.after(0, lambda: self.btn_toggle.config(
                text="START  -  Activate Hotkey"))
            self.root.after(0, lambda: self._update_status("Ready"))

        self.loop_thread = threading.Thread(target=loop_worker, daemon=True)
        self.loop_thread.start()

    # ─────────────────────────────────────────────────────────────────────
    # Config save / load
    # ─────────────────────────────────────────────────────────────────────
    def _save_config_to(self, path: str):
        ensure_config_dir()
        cfg = configparser.ConfigParser()

        cfg["General"] = {
            "PickedX":      str(self.picked_x),
            "PickedY":      str(self.picked_y),
            "HideWindow":   "1" if self.chk_hide_var.get() else "0",
            "LoopEnabled":  "1" if self.chk_loop_var.get()  else "0",
            "LoopInterval": self.edt_interval.get().strip() or "500",
            "TriggerKey":   self.hk_entry.get().strip(),
            "ActionCount":  str(len(self.action_queue)),
        }

        # Selected window titles
        sel_titles = []
        for idx in self.lb_win.curselection():
            if 0 <= idx < len(self.hwnd_list):
                try:
                    t = win32gui.GetWindowText(self.hwnd_list[idx])
                    if t:
                        sel_titles.append(t)
                except Exception:
                    pass

        cfg["Targets"] = {"Count": str(len(sel_titles))}
        for i, t in enumerate(sel_titles, 1):
            cfg["Targets"][f"Title{i}"] = t

        for i, act in enumerate(self.action_queue, 1):
            sec = f"Action{i}"
            cfg[sec] = {
                "Type":  act["type"],
                "Sleep": str(act["sleep"]),
                "Hold":  str(act["hold"]),
            }
            if act["type"] == "Click":
                cfg[sec]["X"]      = str(act["x"])
                cfg[sec]["Y"]      = str(act["y"])
                cfg[sec]["Button"] = act["button"]
            else:
                cfg[sec]["Key"] = act["key"]

        with open(path, "w", encoding="utf-8") as f:
            cfg.write(f)

    def _load_config_from(self, path: str):
        if not os.path.isfile(path):
            raise FileNotFoundError(f"File not found: {path}")

        cfg = configparser.ConfigParser()
        cfg.read(path, encoding="utf-8")

        gen = cfg["General"] if "General" in cfg else {}
        self.picked_x = int(gen.get("pickedx", "0"))
        self.picked_y = int(gen.get("pickedy", "0"))
        if self.picked_x or self.picked_y:
            self.lbl_coord.config(text=f"X: {self.picked_x}   Y: {self.picked_y}")

        self.chk_hide_var.set(gen.get("hidewindow", "0") == "1")
        self.chk_loop_var.set(gen.get("loopenabled", "1") == "1")

        self.edt_interval.delete(0, tk.END)
        self.edt_interval.insert(0, gen.get("loopinterval", "500"))

        self.hk_entry.delete(0, tk.END)
        self.hk_entry.insert(0, gen.get("triggerkey", ""))

        # Actions
        self._clear_actions()
        count = int(gen.get("actioncount", "0"))
        for i in range(1, count + 1):
            sec = f"action{i}"
            if sec not in cfg:
                continue
            a_type = cfg[sec].get("type", "")
            if not a_type:
                continue
            sleep_v = int(cfg[sec].get("sleep", "300"))
            hold_v  = int(cfg[sec].get("hold",  "50"))
            if a_type == "Click":
                x   = int(cfg[sec].get("x", "0"))
                y   = int(cfg[sec].get("y", "0"))
                btn = cfg[sec].get("button", "Left")
                act = {"type": "Click", "x": x, "y": y,
                       "button": btn, "sleep": sleep_v, "hold": hold_v}
                self.action_queue.append(act)
                self.tv.insert("", tk.END,
                               values=(i, f"{btn} Click",
                                       f"X: {x}  Y: {y}", sleep_v, hold_v))
            else:
                key = cfg[sec].get("key", "")
                act = {"type": "Key", "key": key,
                       "sleep": sleep_v, "hold": hold_v}
                self.action_queue.append(act)
                self.tv.insert("", tk.END,
                               values=(i, "Keyboard", key, sleep_v, hold_v))

        # Rebuild + re-select windows
        self.win_labels, self.hwnd_list = build_window_list()
        self.lb_win.delete(0, tk.END)
        for lbl in self.win_labels:
            self.lb_win.insert(tk.END, lbl)

        tgt_sec    = cfg["Targets"] if "Targets" in cfg else {}
        t_count    = int(tgt_sec.get("count", "0"))
        saved_titles = [tgt_sec.get(f"title{j}", "")
                        for j in range(1, t_count + 1)
                        if tgt_sec.get(f"title{j}", "")]

        matched_rows = []
        for saved in saved_titles:
            # Strip HWND suffix and match by prefix
            import re
            clean  = re.sub(r"\s*\[\d+\]$", "", saved)
            needle = clean[:8]
            if not needle:
                continue
            for row_idx, hwnd in enumerate(self.hwnd_list):
                try:
                    cur_title = win32gui.GetWindowText(hwnd)
                except Exception:
                    continue
                if needle.lower() in cur_title.lower():
                    matched_rows.append(row_idx)
                    break

        self.lb_win.selection_clear(0, tk.END)
        for row_idx in matched_rows:
            self.lb_win.selection_set(row_idx)

        self.target_hwnds = [self.hwnd_list[r]
                             for r in matched_rows
                             if 0 <= r < len(self.hwnd_list)]

        if self.chk_hide_var.get() and self.target_hwnds:
            for hwnd in self.target_hwnds:
                try:
                    win_hide(hwnd)
                except Exception:
                    pass
            self.currently_hidden = self.target_hwnds[0]

    # ─────────────────────────────────────────────────────────────────────
    # Menu handlers
    # ─────────────────────────────────────────────────────────────────────
    def _menu_save_now(self, event=None):
        ensure_config_dir()
        try:
            self._save_config_to(CONFIG_FILE)
            self._update_status(f"Saved to {CONFIG_FILE}", self.clr["Success"])
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save:\n{e}")

    def _menu_save_as(self):
        ensure_config_dir()
        path = filedialog.asksaveasfilename(
            initialdir=CONFIG_DIR,
            initialfile="auto_click_config.ini",
            title="Save Config As",
            filetypes=[("INI files", "*.ini"), ("All files", "*.*")],
            defaultextension=".ini"
        )
        if not path:
            return
        try:
            self._save_config_to(path)
            self._update_status(f"Saved to {path}", self.clr["Success"])
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save:\n{e}")

    def _menu_load_from(self):
        ensure_config_dir()
        path = filedialog.askopenfilename(
            initialdir=CONFIG_DIR,
            title="Load Config",
            filetypes=[("INI files", "*.ini"), ("All files", "*.*")]
        )
        if not path:
            return
        try:
            self._load_config_from(path)
            self._update_status(f"Loaded from {path}", self.clr["Success"])
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load:\n{e}")

    def _menu_open_folder(self):
        ensure_config_dir()
        try:
            os.startfile(CONFIG_DIR)
        except Exception as e:
            messagebox.showerror("Error", f"Could not open folder:\n{e}")

    # ─────────────────────────────────────────────────────────────────────
    # Close handler
    # ─────────────────────────────────────────────────────────────────────
    def _on_close(self):
        self.is_looping = False
        # Unhide any hidden windows
        if self.currently_hidden and win_exists(self.currently_hidden):
            win_show(self.currently_hidden)
        for hwnd in self.target_hwnds:
            try:
                win_show(hwnd)
            except Exception:
                pass
        # Unregister hotkey
        if self._registered_hk:
            try:
                keyboard.remove_hotkey(self._registered_hk)
            except Exception:
                pass
        self.root.destroy()

    # ─────────────────────────────────────────────────────────────────────
    # Entry point
    # ─────────────────────────────────────────────────────────────────────
    def run(self):
        self.root.mainloop()


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = MacroApp()
    app.run()
