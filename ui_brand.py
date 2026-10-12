"""Original Helper icon, kept separate from game recognition assets."""
from __future__ import annotations

import os
import tkinter as tk

from paths import resource_path


ICON_PNG = resource_path("assets/wog_storage_helper.png")
ICON_ICO = resource_path("assets/wog_storage_helper.ico")


def apply_icon(window) -> bool:
    """Set local Tk/Windows icons; missing decoration never blocks the Helper.

    The caller supplies an existing Tk or Toplevel. Keep the PhotoImage alive
    on that window; paths.resource_path works from bundled __file__ as well.
    """
    applied = False
    try:
        icon = tk.PhotoImage(master=window, file=str(ICON_PNG))
        window.iconphoto(True, icon)
        window._wog_brand_icon = icon
        applied = True
    except (tk.TclError, OSError):
        pass
    if os.name == "nt":
        try:
            window.iconbitmap(str(ICON_ICO))
            applied = True
        except (tk.TclError, OSError):
            pass
    return applied
