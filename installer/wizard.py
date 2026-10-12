"""Helper-owned dark setup/uninstall wizard; operations start only on explicit buttons."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk

from i18n import LOCALE_NAMES, get_locale, locale_choices, set_locale, tr
from paths import resource_path
from ui_brand import ICON_PNG, apply_icon
from ui_theme import (ACCENT, BG, FIELD, GREEN, MUTED, ROSE, SURFACE, TEXT,
                      apply_theme, dark_titlebar)
from ui_widgets import AnimatedButton, AnimatedSwitch, RoundedCard, StatusOrbit
from ui_vault_motion import VaultMotion
from installer.core import (EXECUTABLE, PRODUCT_DIRECTORY, UNINSTALLER, InstallCancelled,
                            InstallError, InstallResult, UninstallResult, default_parent,
                            install, uninstall, validate_target)
from installer.windows_integration import (WindowsIntegration, helper_is_running,
                                           schedule_deferred_cleanup)


class Wizard(tk.Tk):
    def __init__(self, *, uninstalling=False, install_root=None, integrations=True,
                 review=False, language="ru"):
        super().__init__()
        set_locale(language)
        self.uninstalling, self.integrations, self.review = uninstalling, integrations, review
        # An uninstaller launched from its installed directory manages that directory only.
        # Source/preview mode uses the default per-user target unless explicitly isolated.
        if install_root is not None:
            self.parent_directory = Path(install_root).absolute()
        elif uninstalling and getattr(sys, "frozen", False):
            self.parent_directory = Path(sys.executable).resolve().parent.parent
        else:
            self.parent_directory = default_parent()
        local = os.environ.get("LOCALAPPDATA", "")
        if not local or not Path(local).is_absolute():
            raise InstallError("LOCALAPPDATA is unavailable or invalid.")
        self.profile_directory = Path(local) / PRODUCT_DIRECTORY
        self.target = validate_target(self.parent_directory / PRODUCT_DIRECTORY,
                                      self.parent_directory, self.profile_directory)
        self._isolated = install_root is not None and self.target != (default_parent() / PRODUCT_DIRECTORY).resolve()
        self.desktop = tk.BooleanVar(self, value=False)
        self.start_menu = tk.BooleanVar(self, value=True)
        self.language = tk.StringVar(self, value=LOCALE_NAMES[get_locale()])
        self.events = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker = None
        self.busy = False
        self.page = "welcome"
        self.result = None
        self.failure = ""
        self.progress_value = 0.0
        self._poll_id = None
        self._closed = False
        self._cleanup_scheduled = False
        self.vault = None
        self.geometry("860x660")
        self.minsize(820, 640)
        apply_theme(self)
        apply_icon(self)
        self.after_idle(lambda: dark_titlebar(self))
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Escape>", lambda _event: self._close())
        self._render()
        self._poll_id = self.after(80, self._poll)

    def _label(self, parent, key, *, size=11, color=TEXT, bold=False, wrap=520, **params):
        label = tk.Label(parent, text=tr(key, **params), background=parent.cget("background"),
                         foreground=color, font=("Segoe UI", size, "bold" if bold else "normal"),
                         justify="left", anchor="w", wraplength=wrap)
        label.pack(fill="x", pady=(0, 9))
        return label

    def _button(self, parent, key, command, *, variant="primary", side="right", disabled=False):
        button = AnimatedButton(parent, text=tr(key), command=command, variant=variant,
                                state="disabled" if disabled else "normal", width=190,
                                height=44, font=("Segoe UI", 10, "bold"))
        button.pack(side=side, padx=(8 if side == "right" else 0, 0))
        return button

    def _card(self, parent, *, padding=20):
        card = RoundedCard(parent, padding=padding, background=BG, bg=SURFACE)
        card.pack(fill="x", pady=(0, 16))
        return card.body

    def _clear_page(self):
        for widget in self.winfo_children():
            widget.destroy()
        self.vault = None

    def _decorative_vault(self, parent):
        # The rail has its own fixed width; this decoration cannot push form
        # controls or translated cards out of the page. It has no app callbacks.
        self.vault = VaultMotion(parent, width=195, height=110, bg=BG,
                                 active=self.busy and self.page == "working"
                                 and not (self.uninstalling or self.review))
        self.vault.pack(side="bottom", fill="x", pady=(0, 6))

    def _render(self):
        self._clear_page()
        title_key = "installer.uninstall_title" if self.uninstalling else "installer.title"
        self.title(tr(title_key))
        outer = tk.Frame(self, background=BG)
        outer.pack(fill="both", expand=True, padx=24, pady=24)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)
        rail = tk.Frame(outer, background=BG, width=195)
        rail.grid(row=0, column=0, sticky="nsw", padx=(0, 25))
        rail.pack_propagate(False)
        try:
            self._brand = tk.PhotoImage(file=str(ICON_PNG)).subsample(4, 4)
            tk.Label(rail, image=self._brand, background=BG).pack(anchor="w", pady=(8, 20))
        except (OSError, tk.TclError):
            self._brand = None
        tk.Label(rail, text="WoG Storage\nHelper", background=BG, foreground=TEXT,
                 justify="left", font=("Segoe UI", 20, "bold")).pack(anchor="w")
        self._label(rail, "installer.tagline", size=10, color=MUTED, wrap=188).pack_configure(pady=(10, 38))
        keys = ["installer.step_welcome", "installer.step_options", "installer.step_install", "installer.step_done"]
        if self.uninstalling:
            keys[2] = "installer.uninstalling"
        step = {"welcome": 0, "options": 1, "working": 2}.get(self.page, 3)
        visible_number = 0
        for index, key in enumerate(keys):
            if self.uninstalling and index == 1:
                continue
            visible_number += 1
            row = tk.Frame(rail, background=BG)
            row.pack(fill="x", pady=10)
            tk.Label(row, text="✓" if index < step else str(visible_number), background=BG,
                     foreground=GREEN if index < step else ACCENT if index == step else MUTED,
                     font=("Segoe UI", 11, "bold"), width=2).pack(side="left", anchor="n")
            tk.Label(row, text=tr(key), background=BG, foreground=TEXT if index == step else MUTED,
                     font=("Segoe UI", 10), anchor="w", justify="left", wraplength=157).pack(side="left")
        self._decorative_vault(rail)
        panel = tk.Frame(outer, background=BG)
        panel.grid(row=0, column=1, sticky="nsew")
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)
        header = tk.Frame(panel, background=BG)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 24))
        self._label(header, title_key, size=10, color=MUTED).pack_configure(side="left")
        self.language_box = ttk.Combobox(header, textvariable=self.language,
                                        values=[name for _code, name in locale_choices()],
                                        state="disabled" if self.busy else "readonly", width=21)
        self.language_box.pack(side="right", padx=(8, 0))
        self.language_box.bind("<<ComboboxSelected>>", self._change_language)
        content = tk.Frame(panel, background=BG)
        content.grid(row=1, column=0, sticky="nsew")
        footer = tk.Frame(panel, background=BG)
        footer.grid(row=2, column=0, sticky="ew", pady=(18, 0))
        if self.page == "welcome":
            self._welcome(content, footer)
        elif self.page == "options":
            self._render_options(content, footer)
        elif self.page == "working":
            self._working(content, footer)
        else:
            self._finished(content, footer)

    def _welcome(self, content, footer):
        self._label(content, "installer.uninstall_welcome" if self.uninstalling else "installer.welcome_title",
                    size=23, bold=True, wrap=530)
        self._label(content, "installer.uninstall_detail" if self.uninstalling else "installer.welcome_detail",
                    color=MUTED, wrap=530).pack_configure(pady=(6, 24))
        for title, detail in (("installer.local_title", "installer.local_detail"),
                              ("installer.profile_title", "installer.profile_detail")):
            body = self._card(content)
            self._label(body, title, size=12, bold=True)
            self._label(body, detail, color=MUTED, wrap=470)
        self._button(footer, "installer.uninstall" if self.uninstalling else "installer.next",
                     self._start if self.uninstalling else lambda: self._navigate("options"),
                     disabled=self.review and self.uninstalling)
        self._button(footer, "installer.cancel", self._close, variant="ghost", side="left")

    def _render_options(self, content, footer):
        self._label(content, "installer.options_title", size=23, bold=True)
        body = self._card(content)
        self._label(body, "installer.destination", size=12, bold=True)
        tk.Label(body, text=str(self.target), background=FIELD, foreground=TEXT,
                 font=("Segoe UI", 10), anchor="w", justify="left", wraplength=475,
                 padx=14, pady=14).pack(fill="x", pady=(0, 12))
        self._label(body, "installer.per_user", color=MUTED, wrap=475)
        choices = self._card(content)
        for key, variable in (("installer.desktop", self.desktop), ("installer.start_menu", self.start_menu)):
            row = tk.Frame(choices, background=SURFACE)
            row.pack(fill="x", pady=7)
            AnimatedSwitch(row, variable=variable, state="normal" if self.integrations else "disabled",
                           bg=SURFACE).pack(side="right", padx=(12, 0))
            tk.Label(row, text=tr(key), background=SURFACE, foreground=TEXT, font=("Segoe UI", 11),
                     anchor="w", justify="left", wraplength=385).pack(side="left", fill="x", expand=True)
        self._button(footer, "installer.install", self._start, disabled=self.review)
        self._button(footer, "installer.back", lambda: self._navigate("welcome"), variant="ghost", side="left")

    def _working(self, content, footer):
        body = self._card(content, padding=26)
        StatusOrbit(body, active=True, size=52, bg=SURFACE).pack(anchor="w", pady=(5, 18))
        self._label(body, "installer.uninstalling" if self.uninstalling else "installer.installing",
                    size=23, bold=True, wrap=460)
        self._label(body, "installer.uninstall_detail" if self.uninstalling else "installer.installing_detail",
                    color=MUTED, wrap=460).pack_configure(pady=(8, 25))
        self.progress_bar = ttk.Progressbar(body, maximum=1, value=self.progress_value,
                                           style="Cycle.Horizontal.TProgressbar")
        self.progress_bar.pack(fill="x", pady=(0, 10))
        self.percentage = tk.Label(body, text=f"{self.progress_value:.0%}", background=SURFACE,
                                   foreground=ACCENT, font=("Segoe UI", 11, "bold"), anchor="e")
        self.percentage.pack(fill="x")
        self.cancel_button = self._button(footer, "installer.cancel", self._close, variant="ghost",
                                         disabled=self.uninstalling)

    def _finished(self, content, footer):
        success, cancelled = self.page == "done", self.page == "cancelled"
        color = GREEN if success else MUTED if cancelled else ROSE
        body = self._card(content, padding=26)
        StatusOrbit(body, active=False, size=52, color=color, bg=SURFACE).pack(anchor="w", pady=(5, 18))
        title = ("installer.removed_title" if self.uninstalling else "installer.done_title") if success else (
                 "installer.cancel_title" if cancelled else "installer.error_title")
        detail = ("installer.removed_detail" if self.uninstalling else "installer.done_detail") if success else (
                  "installer.cancel_detail" if cancelled else "installer.error_detail")
        self._label(body, title, size=23, bold=True, wrap=460)
        self._label(body, detail, color=MUTED, wrap=460,
                    **({"error": self.failure} if not success and not cancelled else {}))
        if isinstance(self.result, UninstallResult) and self.result.retained:
            self._label(body, "installer.retained", color=MUTED, wrap=460)
        self._button(footer, "installer.finish", self._close)
        if success and not self.uninstalling:
            self._button(footer, "installer.launch", self._launch, variant="secondary", side="left")
        elif not success:
            self._button(footer, "installer.retry", lambda: self._navigate("welcome" if self.uninstalling else "options"),
                         variant="ghost", side="left")

    def _change_language(self, _event=None):
        if self.busy:
            return
        set_locale(self.language.get())
        self._render()

    def _navigate(self, page):
        if self.busy:
            return
        self.page = page
        self._render()

    def _start(self):
        if self.busy or self.review:
            return
        # The default installed Helper must be closed before program files can change.
        # Explicit isolated review roots with integration disabled do not affect that app.
        try:
            if not (self._isolated and not self.integrations) and helper_is_running():
                raise InstallError(tr("installer.close_helper"))
            if not self.uninstalling:
                metadata = json.loads(resource_path("installer/resources/payload_metadata.json").read_text(encoding="utf-8"))
                payload = resource_path("installer/resources/payload.zip")
                uninstaller_exe = resource_path("installer/resources/uninstaller.exe")
                if not payload.is_file() or not uninstaller_exe.is_file():
                    raise InstallError("The complete bundled setup payload is missing.")
            else:
                metadata, payload, uninstaller_exe = {}, None, None
        except Exception as exc:
            self.failure, self.page = str(exc), "error"
            self._render()
            return
        manager = WindowsIntegration(desktop=self.desktop.get(), start_menu=self.start_menu.get(),
                                     enabled=self.integrations, version=metadata.get("version", "local-beta"))
        self.cancel_event.clear()
        self.result, self.progress_value = None, 0.0
        self.busy, self.page = True, "working"
        self._render()
        def work():
            try:
                if self.uninstalling:
                    deferred = bool(getattr(sys, "frozen", False)) and Path(sys.executable).resolve() == self.target / UNINSTALLER
                    result = uninstall(self.target, self.parent_directory, profile=self.profile_directory,
                                       defer_uninstaller=deferred, progress=lambda value: self.events.put(("progress", value)),
                                       integrate=lambda target, old: manager.apply(target, old, removing=True))
                else:
                    result = install(payload, self.target, self.parent_directory, profile=self.profile_directory,
                                     expected_sha256=metadata["sha256"], uninstaller=uninstaller_exe,
                                     progress=lambda value: self.events.put(("progress", value)),
                                     cancelled=self.cancel_event.is_set, integrate=manager.apply)
                self.events.put(("done", result))
            except InstallCancelled:
                self.events.put(("cancelled", None))
            except Exception as exc:
                self.events.put(("error", str(exc)))
        self.worker = threading.Thread(target=work, name="HelperInstaller", daemon=False)
        self.worker.start()

    def _poll(self):
        self._poll_id = None
        if self._closed:
            return
        latest_progress = None
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                latest_progress = value
            else:
                self.busy = False
                self.page = kind
                self.result = value if kind == "done" else None
                self.failure = value if kind == "error" else ""
                self._render()
        if latest_progress is not None and self.busy:
            self.progress_value = latest_progress
            self.progress_bar.configure(value=latest_progress)
            self.percentage.configure(text=f"{latest_progress:.0%}")
        self._poll_id = self.after(80, self._poll)

    def _launch(self):
        if self.busy or self.review or self.uninstalling or self.page != "done":
            return
        try:
            subprocess.Popen([str(self.target / EXECUTABLE)], cwd=self.target,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self.failure, self.page = str(exc), "error"
            self._render()
            return
        self._close()

    def _close(self):
        if self.busy:
            if not self.uninstalling:
                self.cancel_event.set()
                self.cancel_button.configure(state="disabled")
            return
        if self._closed:
            return
        if isinstance(self.result, UninstallResult) and self.result.deferred and not self._cleanup_scheduled:
            try:
                schedule_deferred_cleanup(self.target, self.parent_directory, self.result.deferred, os.getpid())
                self._cleanup_scheduled = True
            except OSError as exc:
                self.failure, self.page = str(exc), "error"
                self._render()
                return
        self._closed = True
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        self.destroy()

