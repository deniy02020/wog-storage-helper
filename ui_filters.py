"""Edit selected tiers without starting or modifying a game action."""
from __future__ import annotations

import tkinter as tk
from helper_config import default_transfer_filters, validate_transfer_filters
from i18n import tr
from ui_brand import apply_icon
from ui_theme import BG, BORDER, TEXT, MUTED, ROSE, dark_titlebar
from ui_widgets import AnimatedButton, AnimatedSwitch

TIER_COLORS = {1: "#b4bbc8", 2: "#80c99d", 3: "#75b9fa", 4: "#ef8794", 5: "#ba92ed", 6: "#edcf83"}


class FilterDialog(tk.Toplevel):
    def __init__(self, parent, filters, on_apply):
        super().__init__(parent)
        self.title(tr("filters.tiers"))
        self.configure(background=BG, padx=22, pady=18)
        self.geometry("460x368")
        self.minsize(460, 368)
        motion = getattr(parent, "_animations_enabled", True)
        apply_icon(self)
        self._scheduled_callbacks = [self.after(150, lambda: dark_titlebar(self))]
        self.on_apply, self.fields = on_apply, {}
        tk.Label(self, text=tr("filters.title"), background=BG, foreground=TEXT,
                 font=("Segoe UI", 19, "bold")).pack(anchor="w")
        tk.Label(self, text=tr("filters.items"), background=BG, foreground=MUTED,
                 font=("Segoe UI", 10), wraplength=430, justify="left").pack(anchor="w", pady=(4, 14))
        for tier in range(3, 7):
            row = tk.Frame(self, background=BG, height=44)
            row.pack(fill="x")
            row.pack_propagate(False)
            selected = tk.BooleanVar(self, value=filters[str(tier)]["enabled"])
            AnimatedSwitch(row, variable=selected, width=48, height=28,
                           animations_enabled=motion).pack(side="right", pady=8)
            dot = tk.Canvas(row, width=20, height=24, background=BG, highlightthickness=0)
            dot.create_oval(3, 9, 9, 15, fill=TIER_COLORS[tier], outline="")
            dot.pack(side="left")
            tk.Label(row, text=f"T{tier}", anchor="w", background=BG,
                     foreground=TEXT, font=("Segoe UI", 12, "bold")).pack(side="left", padx=4)
            self.fields[str(tier)] = selected
            if tier < 6:
                tk.Frame(self, background=BORDER, height=1).pack(fill="x")
        self.error = tk.StringVar()
        tk.Label(self, textvariable=self.error, background=BG, foreground=ROSE,
                 wraplength=430, justify="left", font=("Segoe UI", 9)).pack(anchor="w", pady=(5, 8))
        buttons = tk.Frame(self, background=BG)
        buttons.pack(side="bottom", fill="x")
        AnimatedButton(buttons, text=tr("filters.reset"), variant="ghost", width=180,
                       height=44, radius=22, command=self.reset, animations_enabled=motion).pack(side="left")
        AnimatedButton(buttons, text=tr("filters.apply"), width=180, height=44, radius=22,
                       command=self.apply, animations_enabled=motion).pack(side="right")
        self.transient(parent)
        self.grab_set()
        self.bind("<Escape>", lambda _: self.destroy())
        self._scheduled_callbacks.append(self.after_idle(self.focus_set))

    def reset(self):
        defaults = default_transfer_filters()
        for key, selected in self.fields.items():
            selected.set(defaults[key]["enabled"])
        self.error.set("")

    def apply(self):
        try:
            rules = validate_transfer_filters({key: {"enabled": selected.get()}
                                               for key, selected in self.fields.items()})
            self.on_apply(rules)
        except Exception:
            self.error.set(tr("detail.see_journal"))
            return
        self.destroy()

    def destroy(self):
        for token in getattr(self, "_scheduled_callbacks", ()):
            try:
                self.after_cancel(token)
            except tk.TclError:
                pass
        self._scheduled_callbacks = []
        super().destroy()
