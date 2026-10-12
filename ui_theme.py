"""Shared visual theme for the dashboard and calibration views."""
from __future__ import annotations

import sys
import tkinter as tk
from tkinter import ttk

BG = "#10131d"
SURFACE = "#191e2c"
FIELD = "#222839"
BORDER = "#2c3347"
TEXT = "#eff1f7"
MUTED = "#9199ad"
ACCENT = "#9b8afb"
GREEN = "#7edbb2"
AMBER = "#edc27d"
ROSE = "#ef93a1"


def client_animations_enabled():
    """Read the Windows reduced-motion preference; never alter system settings.

    https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-systemparametersinfoa
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        query = ctypes.windll.user32.SystemParametersInfoW
        query.argtypes = (wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT)
        query.restype = wintypes.BOOL
        enabled = wintypes.BOOL(True)
        if query(0x1042, 0, ctypes.byref(enabled), 0):  # SPI_GETCLIENTAREAANIMATION only.
            return bool(enabled.value)
    except (AttributeError, OSError):
        pass
    return True


def apply_theme(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=("Segoe UI", 10), background=BG, foreground=TEXT,
                    bordercolor=BORDER, darkcolor=BORDER, lightcolor=BORDER,
                    focuscolor=ACCENT)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("TLabelframe", background=BG, bordercolor=BORDER)
    style.configure("TLabelframe.Label", background=BG, foreground=MUTED)
    style.configure("TButton", background=FIELD, foreground=TEXT, padding=(12, 8),
                    borderwidth=0)
    style.map("TButton", background=[("pressed", BORDER), ("active", "#30364d")],
              foreground=[("disabled", "#646d83")])
    for control in ("TCombobox", "TSpinbox", "TEntry"):
        style.configure(control, fieldbackground=FIELD, background=FIELD,
                        foreground=TEXT, arrowcolor=MUTED, padding=7,
                        selectbackground=ACCENT, selectforeground=BG,
                        insertcolor=TEXT, borderwidth=1)
        style.map(control, fieldbackground=[("readonly", FIELD), ("disabled", SURFACE)],
                  foreground=[("disabled", "#646d83"), ("readonly", TEXT)],
                  bordercolor=[("focus", ACCENT)])
    for control in ("TCheckbutton", "TRadiobutton"):
        style.configure(control, background=BG, foreground=TEXT)
        style.map(control, background=[("active", BG)],
                  foreground=[("disabled", "#646d83")])
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", background=SURFACE, foreground=MUTED,
                    padding=(20, 10))
    style.map("TNotebook.Tab", background=[("selected", FIELD)],
              foreground=[("selected", TEXT)])
    style.configure("Vertical.TScrollbar", background=FIELD, troughcolor=BG,
                    arrowcolor=MUTED, borderwidth=0)
    style.configure("Cycle.Horizontal.TProgressbar", background=ACCENT,
                    troughcolor=FIELD, borderwidth=0, lightcolor=ACCENT,
                    darkcolor=ACCENT, thickness=3)
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", BG)
    root.option_add("*TCombobox*Listbox.font", "{Segoe UI} 10")
    root.option_add("*background", BG)
    root.option_add("*foreground", TEXT)
    root.configure(background=BG)


def theme_text(widget):
    widget.configure(background=SURFACE, foreground=TEXT, insertbackground=TEXT,
                     selectbackground=ACCENT, selectforeground=BG, relief="flat",
                     borderwidth=0, highlightthickness=1,
                     highlightbackground=BORDER, highlightcolor=ACCENT,
                     font=("Segoe UI", 10), padx=12, pady=10)


def dark_titlebar(window):
    """Opt in to Windows' dark caption for this Tk window only."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes
        ancestor = ctypes.windll.user32.GetAncestor
        ancestor.argtypes = (wintypes.HWND, wintypes.UINT)
        ancestor.restype = wintypes.HWND
        handle = ancestor(window.winfo_id(), 2)  # GA_ROOT: this Tk top-level wrapper.
        setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
        setter.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
        value = ctypes.c_int(1)
        setter(handle, 20, ctypes.byref(value), ctypes.sizeof(value))
        # Caption colors are per-window; no Windows theme setting is changed.
        # https://learn.microsoft.com/windows/win32/api/dwmapi/ne-dwmapi-dwmwindowattribute
        for attribute, color in ((34, BORDER), (35, BG), (36, TEXT)):
            rgb = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
            value = wintypes.DWORD(rgb[0] | rgb[1] << 8 | rgb[2] << 16)
            setter(handle, attribute, ctypes.byref(value), ctypes.sizeof(value))
    except (AttributeError, ValueError, OSError, tk.TclError):
        # Older Windows versions keep their ordinary native caption.
        pass
