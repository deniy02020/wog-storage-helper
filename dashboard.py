"""Minimal automatic product UI. Technical journal stays behind settings.

The base App owns profile location (development, explicit WOG_HELPER_PROFILE,
portable or packaged per-user storage), transfer validation and cancellation.
Runtime may enqueue ('state', {'state': ..., 'message': ...,
'next_check_at': monotonic_deadline_or_None}); discovery itself never sends input.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import queue
import re
import sys
import time
import tkinter as tk
from tkinter import ttk

from app import App, BASE
from game_identity import game_windows
from ui_theme import (apply_theme, theme_text, dark_titlebar, client_animations_enabled, BG, SURFACE, FIELD,
                      BORDER, TEXT, MUTED, ACCENT, GREEN, AMBER, ROSE)
from ui_widgets import AnimatedButton, AnimatedReveal, _rounded_points
from ui_brand import apply_icon
from ui_vault_motion import VaultMotion
from ui_time_picker import TimeWheel, CountdownDial
from ui_filters import FilterDialog, TIER_COLORS
from ui_journal import EventJournal
from helper_config import validate_interval, default_transfer_filters, IDLE_SECONDS
from i18n import (tr, set_locale, get_locale, LOCALE_NAMES, format_duration,
                  translate_message, LEGACY_TEXT_KEYS)


@dataclass(frozen=True)
class GameSelection:
    matches: tuple

    @property
    def window(self):
        return self.matches[0] if len(self.matches) == 1 else None

    @property
    def state(self):
        return "missing" if not self.matches else "ready" if len(self.matches) == 1 else "ambiguous"


def select_game_window(windows) -> GameSelection:
    """Require the exact executable and complete title, allowing two aliases."""
    return GameSelection(tuple(game_windows(windows)))


def parse_interval(value) -> float:
    return validate_interval(value)


class ProductApp(App):
    def __init__(self):
        self._dashboard_ready = False
        self._advanced_open = False
        self._runtime_state = "idle"
        self._runtime_message = ""
        self._next_check_at = None
        self._confirmed_count = 0
        self._verified_result = None
        self._panels_result = None
        self._last_worker_message = ""
        self._profile_problem = ""
        self._journal = None
        self._journal_records = []
        self._activity_state = "work"
        super().__init__()
        if self.profile.get("storage_full") is True:
            self._runtime_state = "storage_full"
        set_locale(self.profile.get("language", "ru"))
        self.title("WoG Storage Helper")
        self._legacy_layout = [(widget, widget.pack_info()) for widget in self.winfo_children()
                               if widget.winfo_manager() == "pack"]
        for widget, _ in self._legacy_layout:
            widget.pack_forget()
        apply_theme(self)
        self._animations_enabled = client_animations_enabled()
        self.configure(padx=24, pady=22)
        theme_text(self.log_widget)
        apply_icon(self)
        self.after(150, lambda: dark_titlebar(self))
        self._build_advanced_header()
        self._capture_legacy_text()
        self._translate_legacy()
        self._build_dashboard()
        self._dashboard_ready = True
        self.show_dashboard()
        self.update_info()
        self.refresh_windows()
        self.after(2000, self._discover_tick)

    def _build_dashboard(self):
        self.dashboard = tk.Frame(self, background=BG)
        self._main_actions = []
        def label(parent, text=None, variable=None, size=10, color=TEXT, bold=False, **options):
            return tk.Label(parent, text=text, textvariable=variable, background=BG,
                foreground=color, font=("Segoe UI",size,"bold" if bold else "normal"),
                borderwidth=0, **options)
        stage = tk.Frame(self.dashboard, background=BG)
        stage.pack(fill="both", expand=True)
        motion = self._animations_enabled
        self.idle_scene = AnimatedReveal(stage, height=526, revealed=True, bg=BG, animations_enabled=motion)
        self.idle_scene.pack(fill="x")
        idle = self.idle_scene.body
        header = tk.Frame(idle, background=BG)
        header.pack(fill="x", pady=(0,6))
        if hasattr(self, "_wog_brand_icon"):
            self._dashboard_brand_icon = self._wog_brand_icon.subsample(7,7)
            tk.Label(header,image=self._dashboard_brand_icon,background=BG,borderwidth=0).pack(side="left",padx=(0,10))
        titles = tk.Frame(header,background=BG)
        titles.pack(side="left")
        label(titles,"WoG",size=15,bold=True).pack(anchor="w")
        label(titles,"Storage Helper",size=9,color=MUTED).pack(anchor="w")
        game_row = tk.Frame(header,background=BG)
        game_row.pack(side="right")
        self.game_dot = tk.Canvas(game_row,width=12,height=12,background=BG,highlightthickness=0)
        self.game_dot.create_oval(2,2,10,10,fill=MUTED,outline="",tags="dot")
        self.game_dot.pack(side="left",padx=(0,6))
        self.game_text = tk.StringVar()
        label(game_row,variable=self.game_text,size=9,color=MUTED).pack(side="left")
        self.vault_motion = VaultMotion(idle,width=552,height=92,bg=BG,animations_enabled=motion)
        self.vault_motion.pack(fill="x",pady=(2,0))
        label(idle,tr("interval.title"),size=9,color=MUTED).pack(anchor="center",pady=(3,0))
        self.interval_choice = TimeWheel(idle,variable=self.interval,formatter=format_duration,
            command=lambda:self.safe(self.save_interval),height=294,bg=BG,animations_enabled=motion)
        self.interval_choice.pack(fill="x")
        filter_row = tk.Frame(idle,background=BG)
        filter_row.pack(fill="x",pady=(2,8))
        self.filter_chips = tk.Canvas(filter_row,height=32,background=BG,highlightthickness=0)
        self.filter_chips.pack(side="left",fill="x",expand=True)
        self.filters_button = AnimatedButton(filter_row,text=tr("filters.tiers"),
            width=125,height=34,radius=17,variant="ghost",command=self.show_filters,animations_enabled=motion)
        self.filters_button.pack(side="right")
        self.settings_link = AnimatedButton(idle,text="⚙",width=42,height=34,radius=17,
            variant="ghost",font=("Segoe UI",19),command=self.show_settings,animations_enabled=motion)
        self.settings_link.pack(anchor="e")
        self.active_scene = AnimatedReveal(stage,height=450,revealed=False,bg=BG,animations_enabled=motion)
        self.active_scene.pack(fill="x")
        self.countdown_dial = CountdownDial(self.active_scene.body,height=438,bg=BG,animations_enabled=motion)
        self.countdown_dial.pack(fill="x")
        self.status_orbit = self.countdown_dial
        self.countdown_text = tk.StringVar()
        self.cycle_progress = ttk.Progressbar(self.dashboard,style="Cycle.Horizontal.TProgressbar",maximum=100,value=0)
        self.activity_text = tk.StringVar()
        bottom = tk.Frame(self.dashboard,background=BG)
        bottom.pack(side="bottom",fill="x",pady=(0,6))
        self.run_title,self.run_detail,self.count_text = tk.StringVar(),tk.StringVar(),tk.StringVar(value="0")
        label(bottom,variable=self.run_title,size=11,color=MUTED,wraplength=545,justify="center").pack(anchor="center",pady=(0,12))
        self.detail_label = label(bottom,variable=self.run_detail,size=9,color=MUTED,wraplength=520,justify="center")
        self.stats_text = tk.StringVar()
        self.stats_label = label(bottom,variable=self.stats_text,size=9,color=ACCENT)
        self.primary = AnimatedButton(bottom,text=tr("action.start"),height=60,radius=30,
            font=("Segoe UI",13,"bold"),command=self.toggle_run,animations_enabled=motion)
        self.primary.pack(fill="x")
        self.language_choice = tk.StringVar(value=LOCALE_NAMES[get_locale()])
        self.language_selector = ttk.Combobox(idle,state="readonly",width=22,
            textvariable=self.language_choice,values=tuple(LOCALE_NAMES.values()))
        self._main_actions.extend((self.interval_choice,self.filters_button,self.settings_link,self.language_selector))
        self.actions.extend(self._main_actions)
        self._refresh_filter_chips()

    def show_settings(self):
        if self.worker and self.worker.is_alive():
            return
        dialog = tk.Toplevel(self)
        dialog.title(tr("settings.title"))
        dialog.configure(background=BG,padx=24,pady=24)
        dialog.geometry("470x240")
        dialog.minsize(470,240)
        apply_icon(dialog)
        tokens = [dialog.after(150,lambda:dark_titlebar(dialog))]
        def on_destroy(event):
            if event.widget is dialog:
                for token in tokens:
                    try:
                        dialog.after_cancel(token)
                    except tk.TclError:
                        pass
        dialog.bind("<Destroy>",on_destroy,add="+")
        tk.Label(dialog,text=tr("settings.title"),font=("Segoe UI",18,"bold"),
            foreground=TEXT,background=BG).pack(anchor="w",pady=(0,20))
        row = tk.Frame(dialog,background=BG)
        row.pack(fill="x",pady=(0,22))
        tk.Label(row,text=tr("settings.language"),background=BG,foreground=MUTED,
            font=("Segoe UI",10)).pack(side="left")
        selector = ttk.Combobox(row,state="readonly",width=23,
            values=tuple(LOCALE_NAMES.values()),textvariable=self.language_choice)
        selector.pack(side="right")
        def change(_event):
            dialog.destroy()
            self.safe(self.change_language)
        selector.bind("<<ComboboxSelected>>",change)
        def journal():
            dialog.destroy()
            self.show_journal()
        AnimatedButton(dialog,text=tr("journal.title"),height=44,radius=22,
            variant="secondary",command=journal,animations_enabled=self._animations_enabled).pack(fill="x")
        dialog.transient(self)
        dialog.grab_set()
        dialog.bind("<Escape>",lambda _:dialog.destroy())
        tokens.append(dialog.after_idle(selector.focus_set))

    def _build_advanced_header(self):
        # Base App's internal diagnostic widgets are never exposed by ProductApp.
        self._advanced_header = ttk.Frame(self)

    def _capture_legacy_text(self):
        self._legacy_text = []
        self._legacy_tabs = []
        def visit(widget):
            try:
                original = widget.cget("text")
                if original:
                    self._legacy_text.append((widget, original))
            except tk.TclError:
                pass
            if isinstance(widget, ttk.Notebook):
                for tab in widget.tabs():
                    self._legacy_tabs.append((widget, tab, widget.tab(tab, "text")))
            for child in widget.winfo_children():
                visit(child)
        for widget, _ in self._legacy_layout:
            visit(widget)
        visit(self._advanced_header)

    def _translate_legacy(self):
        for widget, original in self._legacy_text:
            key = LEGACY_TEXT_KEYS.get(original)
            if not key and original.startswith("Инвентарь → хранилище:"):
                key = "legacy.heading"
            elif not key and original.startswith("Прототип: сначала настройка"):
                key = "legacy.intro"
            elif not key and original.startswith("Перед переносом сумка сортируется:"):
                key = "legacy.runtime_help"
            widget.configure(text=tr(key) if key else translate_message(original))
        for notebook, tab, original in self._legacy_tabs:
            notebook.tab(tab, text=translate_message(original))

    def change_language(self):
        selected = self.language_choice.get()
        code = next((code for code, name in LOCALE_NAMES.items() if name == selected), "ru")
        if code == get_locale():
            return
        set_locale(code)
        self.profile["language"] = code
        self.save(invalidate=False)
        if self._journal is not None and self._journal.winfo_exists():
            self._journal.destroy()
            self._journal = None
        self.actions = [widget for widget in self.actions if widget not in self._main_actions]
        self.dashboard.destroy()
        self._translate_legacy()
        self._build_dashboard()
        self.show_dashboard()
        self.refresh_windows()
        self._update_dashboard()

    def _refresh_filter_chips(self):
        if not hasattr(self, "filter_chips"):
            return
        canvas = self.filter_chips
        canvas.delete("all")
        rules = self.profile.get("transfer_filters", default_transfer_filters())
        selected = [(int(key), rule) for key, rule in rules.items() if rule["enabled"]]
        for index, (tier, rule) in enumerate(selected):
            x = index * 45
            canvas.create_polygon(_rounded_points(x + 1, 1, x + 41, 27, 8),
                                  smooth=True, splinesteps=24, fill=SURFACE, outline=BORDER)
            canvas.create_text(x + 21, 14, text=f"T{tier}",
                fill=TIER_COLORS[tier], font=("Segoe UI", 9, "bold"))
        if not selected:
            canvas.create_text(0, 14, anchor="w", text=tr("filters.none"), fill=MUTED,
                               font=("Segoe UI", 9))

    def show_filters(self):
        if self.worker and self.worker.is_alive():
            return
        def save_rules(rules):
            try:
                self.save_transfer_filters(rules)
            except Exception as exc:
                self._record_error(exc)
                raise
            self._refresh_filter_chips()
            self.set_runtime_state("idle")
        FilterDialog(self, self.profile.get("transfer_filters", default_transfer_filters()), save_rules)

    def choose_interval(self, seconds):
        self.interval.set(str(seconds))
        self.save_interval()

    def show_dashboard(self):
        self._advanced_open = False
        self._advanced_header.pack_forget()
        for widget, _ in self._legacy_layout:
            widget.pack_forget()
        self.dashboard.pack(fill="both", expand=True)
        self.minsize(620, 720)
        self.geometry("620x720")

    def show_advanced(self):
        # Kept as a harmless compatibility entry point for old callers.
        self.show_dashboard()

    def refresh_windows(self):
        if self.worker and self.worker.is_alive():
            return
        selection = select_game_window(self.desktop.windows())
        self._game_selection = selection
        self.windows = list(selection.matches)
        self.window_choice["values"] = [f"Genesis.exe · {window.title}" for window in self.windows]
        self.handle = selection.window.handle if selection.window else None
        if selection.window:
            self.window_choice.current(0)
        else:
            self.window_choice.set("")
        if self._dashboard_ready:
            self.game_text.set(tr({"ready": "game.found", "missing": "game.missing",
                                   "ambiguous": "game.ambiguous"}[selection.state]))
            self.update_info()

    def select_window(self, _=None):
        # Advanced view cannot turn an ambiguous selection into an arbitrary one.
        self.handle = self.windows[0].handle if len(self.windows) == 1 else None

    def _discover_tick(self):
        if not (self.worker and self.worker.is_alive()):
            try:
                self.refresh_windows()
            except Exception as exc:
                self.handle = None
                self.game_text.set(tr("game.missing"))
                self._record_error(exc)
        try:
            self._activity_state = ("game" if self.handle and self.desktop.active(self.handle) else
                                    "afk" if self.desktop.idle_seconds() >= IDLE_SECONDS else "work")
        except Exception:
            # A failed activity observer must never be represented as idle.
            self._activity_state = "work"
        self._update_dashboard()
        self.after(2000, self._discover_tick)

    def update_info(self):
        App.update_info(self)
        if not self._dashboard_ready:
            return
        tiers = ", ".join(f"T{k}: {len(v)}" for k, v in sorted(self.profile.get("tiers", {}).items())) or tr("legacy.none")
        self.calibration_info.set(tr("legacy.calibration_summary", tiers=tiers,
            proof=tr("legacy.proof_yes" if self.profile.get("supervised_ok") else "legacy.proof_no")))
        self._profile_problem = ""
        if self.handle is not None and not (self.worker and self.worker.is_alive()):
            try:
                self.make_runner()  # Constructor validates saved data; no capture/input.
            except Exception as exc:
                self._profile_problem = str(exc)
        self._update_dashboard()

    def save_interval(self):
        value = parse_interval(self.interval.get())
        self.interval.set(f"{value:g}")
        self.profile["interval"] = value
        self.save(invalidate=False)

    def start(self, continuous):
        self.refresh_windows()
        selection = self._game_selection
        if selection.state == "missing":
            raise ValueError("Открой War of Genesis Idle Loot и повтори запуск.")
        if selection.state == "ambiguous":
            raise ValueError("Закрой лишнее окно игры: для запуска нужно ровно одно.")
        if getattr(self, "_journal", None) is not None and self._journal.winfo_exists():
            self._journal.destroy()
            self._journal = None
        if hasattr(self, "interval_choice") and hasattr(self.interval_choice, "commit_pending"):
            self.interval_choice.commit_pending(notify=False)
        self.save_interval()
        self._confirmed_count = 0
        self._verified_result = None
        self._panels_result = None
        self._last_worker_message = ""
        self._continuous = continuous
        self._runtime_state = "starting"
        self._runtime_message = "Подготовка игры к переносу."
        self._next_check_at = None
        App.start(self, continuous)
        if self.profile.pop("storage_full", False):
            self.save(invalidate=False)
        self._update_dashboard()

    def toggle_run(self):
        if self.worker and self.worker.is_alive():
            self.stop()
            self.set_runtime_state("stopping", "Ожидаю завершения текущей проверки.")
        else:
            self.safe(lambda: self.start(True))

    def open_panels(self, inspect_pages=False):
        self.refresh_windows()
        if self._game_selection.state == "missing":
            raise ValueError("Открой War of Genesis Idle Loot и повтори запуск.")
        if self._game_selection.state == "ambiguous":
            raise ValueError("Закрой лишнее окно игры: для запуска нужно ровно одно.")
        self._panels_result = None
        self._verified_result = None
        self._confirmed_count = 0
        self._last_worker_message = ""
        self.set_runtime_state("opening", "Проверка страниц хранилища." if inspect_pages else "Открытие игровых панелей.")
        App.open_panels(self, inspect_pages=inspect_pages)

    def safe(self, command):
        # Retain App's busy-worker gate, but present errors on the status card.
        def checked():
            try:
                command()
            except Exception as exc:
                self._record_error(exc)
        App.safe(self, checked)

    def _record_error(self, exc):
        self._runtime_state = "error"
        self._runtime_message = str(exc)
        self._next_check_at = None
        self.log(str(exc))

    def set_runtime_state(self, state, message="", next_check_at=None):
        self._runtime_state = state
        self._runtime_message = str(message)
        self._next_check_at = next_check_at if isinstance(next_check_at, (int, float)) and math.isfinite(next_check_at) else None
        if state == "storage_full":
            self._next_check_at = None
            self.profile["storage_full"] = True
            self.save(invalidate=False)
        if self._dashboard_ready:
            self._update_dashboard()

    def log(self, message):
        App.log(self, message)
        self._last_worker_message = str(message)
        if re.fullmatch(r"Перенесён T[1-6]: сумка \d+ → хранилище \d+\.", str(message)):
            self._confirmed_count += 1
        elif str(message).startswith("Пауза:"):
            self._runtime_state, self._runtime_message = "paused", str(message)
        elif str(message).startswith("Окно игры активировано. Старт"):
            self._runtime_state, self._runtime_message = "starting", str(message)
        stamp = time.strftime("%H:%M:%S ")
        if hasattr(self, "_journal_records"):
            self._journal_records.append((stamp, str(message)))
        if self._journal is not None and self._journal.winfo_exists():
            self._journal.append(stamp, message)
        if self._dashboard_ready:
            self._update_dashboard()

    def _update_dashboard(self):
        busy = bool(self.worker and self.worker.is_alive())
        storage_full = self._runtime_state == "storage_full"
        for widget in getattr(self, "_main_actions", ()):
            widget.configure(state="disabled" if busy else
                             "readonly" if widget is self.language_selector else "normal")
        self.count_text.set(str(self._confirmed_count))
        self.primary.configure(text=tr("storage_space_available" if storage_full and not busy else
                                       "action.stop" if busy else "action.start"))
        ready = self.handle is not None and not self._profile_problem
        self.primary.configure(state="normal" if busy or ready else "disabled",
                               variant="secondary" if busy else "primary", selected=busy)
        if hasattr(self, "idle_scene"):
            self.idle_scene.configure(revealed=not (busy or storage_full))
            self.active_scene.configure(revealed=busy or storage_full)
            self.vault_motion.configure(state="disabled" if busy else "normal")
        names = {"idle": "ready", "ready": "ready", "starting": "preparing",
                 "running": "running", "scanning": "scanning", "checking": "scanning",
                 "opening": "opening", "transferring": "transferring", "waiting": "waiting",
                 "paused": "paused", "stopping": "stopping", "stopped": "stopped",
                 "finished": "completed", "error": "error"}
        title = tr("state." + names.get(self._runtime_state, "scanning"))
        detail = translate_message(self._runtime_message)
        if storage_full:
            title, detail = tr("storage_full_title"), tr("storage_full_body")
        # Unknown technical diagnostics remain verbatim in the journal only.
        if get_locale() != "ru" and self._runtime_message and detail == self._runtime_message:
            detail = tr("detail.see_journal")
        if not busy and self._runtime_state not in ("error", "finished", "stopped", "storage_full"):
            if self.handle is None:
                title, detail = tr("state.game_wait"), tr("detail.game_missing")
            elif self._profile_problem:
                title, detail = tr("state.error"), tr("detail.see_journal")
            else:
                title, detail = tr("state.ready"), tr("detail.ready")
        interval = self.profile.get("interval", 1800)
        remaining = None
        if busy and self._next_check_at is not None:
            remaining = max(0, math.ceil(self._next_check_at - time.monotonic()))
            countdown = tr("interval.next", time=format_duration(remaining, countdown=True))
            if self._runtime_state == "waiting":
                detail = countdown
        else:
            countdown = tr("interval.stopped") if not busy else tr("detail.working")
        if hasattr(self, "countdown_text"):
            self.countdown_text.set(countdown)
        if hasattr(self, "activity_text"):
            self.activity_text.set(tr({"game": "game.open", "afk": "idle.ten_minutes",
                                      "work": "mode.work"}.get(self._activity_state, "mode.work")))
        self.run_title.set(title)
        self.run_detail.set(" ".join(str(detail).split()))
        if hasattr(self, "detail_label"):
            show_detail = storage_full
            if show_detail and not self.detail_label.winfo_manager():
                self.detail_label.pack(anchor="center", pady=(5, 0))
            elif not show_detail and self.detail_label.winfo_manager():
                self.detail_label.pack_forget()
        if hasattr(self, "stats_text"):
            self.stats_text.set(tr("stats.short", count=self._confirmed_count))
            if self.stats_label.winfo_manager():
                self.stats_label.pack_forget()
        if hasattr(self, "status_orbit"):
            color = (ROSE if self._runtime_state == "error" else
                     AMBER if self._runtime_state in ("paused", "stopping", "storage_full") else
                     ACCENT if busy else GREEN if ready else MUTED)
            moving_items = busy and self._runtime_state in (
                "starting", "running", "working", "scanning", "checking", "opening", "transferring")
            self.status_orbit.configure(active=moving_items and not self._advanced_open, color=color)
            self.game_dot.itemconfigure("dot", fill=GREEN if self.handle else MUTED)
            progress = 0
            if busy and remaining is not None and isinstance(interval, (int, float)) and interval > 0:
                progress = max(0, min(100, 100 * (1 - remaining / interval)))
            self.cycle_progress.configure(value=progress)
            if hasattr(self, "countdown_dial"):
                self.countdown_dial.configure(seconds=remaining, active=busy, color=color)

    def poll(self):
        if self.worker and self.worker.is_alive() and self.desktop.stop_pressed():
            self.cancel.set()
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.log(value)
                elif kind == "state" and isinstance(value, dict):
                    self.set_runtime_state(value.get("state", "running"), value.get("message", ""), value.get("next_check_at"))
                elif kind == "verified":
                    self._verified_result = bool(value)
                    if value:
                        self.profile["supervised_ok"] = True
                        self.save(invalidate=False)
                        self.log("Один перенос подтверждён. Проверь его результат в игре перед AFK.")
                elif kind == "panels_ready":
                    self._panels_result = bool(value)
                elif kind == "finished":
                    for button in self.actions:
                        button.configure(state="normal")
                    self.window_choice.configure(state="readonly")
                    if self._runtime_state == "storage_full":
                        self._next_check_at = None
                    elif self.cancel.is_set():
                        self.set_runtime_state("stopped", "Перенос остановлен. Можно запустить снова.")
                    elif self._verified_result is not None:
                        self.set_runtime_state("finished", "Один перенос подтверждён." if self._verified_result else "Подходящих предметов пока нет.")
                    elif self._panels_result is not None:
                        self.set_runtime_state("finished" if self._panels_result else "error",
                            "Игровые панели открыты. Можно настроить распознавание." if self._panels_result else self._last_worker_message)
                    else:
                        self.set_runtime_state("error", self._last_worker_message or "Проверка завершилась без подтверждения.")
                    self.deiconify()
        except queue.Empty:
            pass
        if self._dashboard_ready:
            self._update_dashboard()
        self.after(100, self.poll)

    def show_journal(self):
        if self._journal is not None and self._journal.winfo_exists():
            self._journal.lift()
            return
        self._journal = EventJournal(self, self._journal_records,
                                     animations_enabled=self._animations_enabled)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        import unittest
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.discover(str(BASE)))
        raise SystemExit(not result.wasSuccessful())
    from instance import SingleInstance
    from tkinter import messagebox
    guard = SingleInstance()
    try:
        if not guard.acquire():
            raise SystemExit(0)
        ProductApp().mainloop()
    except Exception as exc:
        if sys.stderr is not None:
            print(str(exc), file=sys.stderr)
        error_window = tk.Tk()
        error_window.withdraw()
        messagebox.showerror("WoG Storage Helper", str(exc), parent=error_window)
        error_window.destroy()
        raise SystemExit(1)
    finally:
        guard.close()
