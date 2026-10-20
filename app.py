"""Russian calibration UI for the WoG Storage Helper prototype."""
from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageDraw, ImageTk

from core import Rect, GridSpec
from runtime import Runner, FOCUS_WAITING
from storage_cycle import StorageFull, STORAGE_FULL_MESSAGE
from windows import FocusUnavailable, WindowsDesktop

from paths import BASE, get_profile_dir, bootstrap_new_profile
from helper_config import MIN_INTERVAL, migrate_profile, validate_interval, validate_transfer_filters
from i18n import tr, translate_message, get_locale
from session_logging import configure_session_logging

PROFILE_DIR = get_profile_dir()


class RegionDialog(tk.Toplevel):
    def __init__(self, parent, image, prompt):
        super().__init__(parent)
        prompt = translate_message(prompt)
        self.title(prompt)
        self.result = None
        self.image_size = image.size
        self.scale = min(1, 1080 / image.width, 620 / image.height)
        display = image.resize((max(1, round(image.width * self.scale)),
                                max(1, round(image.height * self.scale))))
        self.picture = ImageTk.PhotoImage(display)
        ttk.Label(self, text=prompt + "\n" + tr("legacy.dialog_instruction"), wraplength=1000).pack(padx=12, pady=8)
        self.canvas = tk.Canvas(self, width=display.width, height=display.height, highlightthickness=0)
        self.canvas.pack(padx=8)
        self.canvas.create_image(0, 0, anchor="nw", image=self.picture)
        self.box_id = None
        self.start = None
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.motion)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        bar = ttk.Frame(self)
        bar.pack(pady=8)
        ttk.Button(bar, text=tr("action.done"), command=self.finish).pack(side="left", padx=8)
        ttk.Button(bar, text=tr("action.cancel"), command=self.destroy).pack(side="left", padx=8)
        self.bind("<Escape>", lambda _: self.destroy())
        self.transient(parent)
        self.grab_set()
        self.wait_window()

    def press(self, event):
        self.start = (event.x, event.y)
        if self.box_id:
            self.canvas.delete(self.box_id)
        self.box_id = self.canvas.create_rectangle(event.x, event.y, event.x, event.y, outline="#00ffcc", width=2)

    def motion(self, event):
        if self.start:
            self.canvas.coords(self.box_id, *self.start, event.x, event.y)

    def release(self, event):
        self.motion(event)

    def finish(self):
        if not self.box_id:
            return
        x1, y1, x2, y2 = self.canvas.coords(self.box_id)
        width, height = self.image_size
        rect = Rect(max(0, round(min(x1, x2) / self.scale)),
                    max(0, round(min(y1, y2) / self.scale)),
                    min(width, round(max(x1, x2) / self.scale)),
                    min(height, round(max(y1, y2) / self.scale)))
        if not rect.fits(width, height):
            return
        self.result = rect
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        self.desktop = WindowsDesktop()  # Set DPI awareness before creating Tk.
        super().__init__()
        self.title("WoG · Экипировка и самоцветы T3+ · Прототип")
        self.geometry("1040x800")
        self.minsize(920, 690)
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        bootstrap_new_profile(PROFILE_DIR)
        self.profile_file = PROFILE_DIR / "settings.json"
        self.profile = {"mode": "right", "tiers": {}, "interval": 1800, "include_gems": True}
        if self.profile_file.exists():
            try:
                self.profile.update(json.loads(self.profile_file.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                messagebox.showwarning(tr("settings.profile"), tr("error.profile"))
        self.profile = migrate_profile(self.profile)
        self.snapshot = None
        self.windows = []
        self.handle = None
        self.worker = None
        self.cancel = threading.Event()
        self.events = queue.Queue()
        self.actions = []
        self.configure(padx=14, pady=10)
        ttk.Label(self, text="Инвентарь → хранилище: экипировка и самоцветы T3+", font=("Segoe UI", 13, "bold")).pack(anchor="w")
        ttk.Label(self, text="Прототип: сначала настройка и один перенос под наблюдением. Запуск активирует выбранное окно игры. F8 — стоп.", wraplength=1000).pack(anchor="w", pady=(4, 10))
        top = ttk.Frame(self)
        top.pack(fill="x")
        self.window_choice = ttk.Combobox(top, state="readonly", width=65)
        self.window_choice.pack(side="left", fill="x", expand=True)
        self.window_choice.bind("<<ComboboxSelected>>", self.select_window)
        self.button(top, "Обновить окна", self.refresh_windows).pack(side="left", padx=5)
        self.button(top, "Снять окно игры", self.capture).pack(side="left")
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, pady=10)
        setup = ttk.Frame(notebook, padding=10)
        run = ttk.Frame(notebook, padding=10)
        notebook.add(setup, text="1. Настройка")
        notebook.add(run, text="2. Проверка и AFK")

        self.button(setup, "Автонастройка по твоим скриншотам", self.auto_setup).pack(anchor="w", pady=6)

        grids = ttk.LabelFrame(setup, text="Сетки: выделяй только полные ячейки, без заголовков", padding=8)
        grids.pack(fill="x", pady=5)
        self.grid_vars = {}
        for row, (key, title, default_rows, default_cols) in enumerate([
                ("inventory", "Инвентарь", 8, 5), ("storage", "Хранилище: видимая страница", 7, 6)]):
            spec = self.profile.get(key, {})
            rows = tk.StringVar(value=str(spec.get("rows", default_rows)))
            cols = tk.StringVar(value=str(spec.get("columns", default_cols)))
            self.grid_vars[key] = (rows, cols)
            ttk.Label(grids, text=title, width=33).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Label(grids, text="Строк").grid(row=row, column=1)
            ttk.Spinbox(grids, from_=1, to=30, width=4, textvariable=rows).grid(row=row, column=2, padx=4)
            ttk.Label(grids, text="Столбцов").grid(row=row, column=3)
            ttk.Spinbox(grids, from_=1, to=30, width=4, textvariable=cols).grid(row=row, column=4, padx=4)
            self.button(grids, "Выделить сетку", lambda k=key: self.mark_grid(k)).grid(row=row, column=5, padx=8)
            self.button(grids, "Пустая ячейка", lambda k=key: self.add_empty(k)).grid(row=row, column=6)

        regions = ttk.LabelFrame(setup, text="Образцы внутри первой ячейки инвентаря", padding=8)
        regions.pack(fill="x", pady=5)
        self.button(regions, "Фрагмент цветной рамки", lambda: self.mark_cell_region("badge")).pack(side="left", padx=5)

        samples = ttk.LabelFrame(setup, text="Образцы рамок: обязательны T1, T2 и каждый переносимый тир", padding=8)
        samples.pack(fill="x", pady=5)
        for tier in range(1, 7):
            self.button(samples, f"Добавить T{tier}", lambda t=tier: self.add_tier(t)).pack(side="left", padx=4)

        anchors = ttk.LabelFrame(setup, text="Ориентиры: неподвижные части обоих открытых окон", padding=8)
        anchors.pack(fill="x", pady=5)
        self.button(anchors, "Ориентир инвентаря", lambda: self.add_anchor(0)).pack(side="left", padx=5)
        self.button(anchors, "Ориентир хранилища", lambda: self.add_anchor(1)).pack(side="left", padx=5)
        self.button(anchors, "Сохранить снимок…", self.export_snapshot).pack(side="left", padx=5)

        actions = ttk.LabelFrame(setup, text="Способ переноса — выбери только проверенный вручную в игре", padding=8)
        actions.pack(fill="x", pady=5)
        self.mode = tk.StringVar(value=self.profile.get("mode", "right"))
        for title, value in [("Перетаскивание", "drag"), ("Правый клик", "right"), ("Выбор вещи → кнопка", "button")]:
            ttk.Radiobutton(actions, text=title, variable=self.mode, value=value, command=self.mode_changed).pack(side="left", padx=6)
        self.button(actions, "Запомнить кнопку переноса", self.mark_button).pack(side="left", padx=6)
        self.calibration_info = tk.StringVar()
        ttk.Label(setup, textvariable=self.calibration_info, wraplength=980).pack(anchor="w", pady=8)

        ttk.Label(run, text="Каждая проверка сканирует обе сетки. После переноса координаты вычисляются заново.", wraplength=950).pack(anchor="w", pady=8)
        controls = ttk.Frame(run)
        controls.pack(fill="x", pady=6)
        self.button(controls, "Предпросмотр — без кликов", self.preview).pack(side="left", padx=5)
        self.button(controls, "Один перенос (через 5 с)", lambda: self.start(False)).pack(side="left", padx=5)
        self.button(controls, "Запустить AFK (через 5 с)", lambda: self.start(True)).pack(side="left", padx=5)
        ttk.Button(controls, text="СТОП · F8", command=self.stop).pack(side="left", padx=5)
        interval_row = ttk.Frame(run)
        interval_row.pack(anchor="w", pady=8)
        ttk.Label(interval_row, text="Интервал проверки, секунд:").pack(side="left")
        self.interval = tk.StringVar(value=str(self.profile.get("interval", 1800)))
        ttk.Spinbox(interval_row, from_=MIN_INTERVAL, to=86400, textvariable=self.interval, width=7).pack(side="left", padx=8)
        ttk.Label(interval_row, text="Ячейка сумки:").pack(side="left", padx=(12, 0))
        self.tooltip_cell = tk.StringVar(value="1")
        ttk.Spinbox(interval_row, from_=1, to=40, textvariable=self.tooltip_cell, width=4).pack(side="left", padx=5)
        self.button(interval_row, "Снять подсказку ячейки", self.capture_tooltip).pack(side="left", padx=5)
        self.include_gems = tk.BooleanVar(value=True)  # Legacy setup compatibility.
        self.tooltip_path = tk.StringVar(value="")
        ttk.Label(run, textvariable=self.tooltip_path, wraplength=950).pack(anchor="w")
        ttk.Label(run, text="Перед переносом сумка сортируется: высокие тиры перемещаются в начало. Используются твои 4 страницы хранилища.\nПри запуске игра активируется один раз. AFK приостанавливается при переключении в другое окно; вернись в игру для продолжения.\nПри неподтверждённом действии повторного клика не будет. Неизвестные тиры пропускаются. В режиме только экипировки требуются буквы Lv.\nРаспознавание проверено на твоих снимках; реальный перенос требует пробного запуска. Подробности — README.md.", wraplength=950).pack(anchor="w", pady=10)
        self.preview_canvas = tk.Canvas(run, background="#222", height=360)
        self.preview_canvas.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="Выбери окно игры, открой обе панели и сними окно.")
        ttk.Label(self, textvariable=self.status, wraplength=1000).pack(anchor="w")
        self.log_widget = tk.Text(self, height=5, state="disabled", wrap="word")
        self.log_widget.pack(fill="x", pady=5)
        configure_session_logging(PROFILE_DIR)
        self.refresh_windows()
        self.update_info()
        self.after(100, self.poll)
        self.protocol("WM_DELETE_WINDOW", self.close)

    def button(self, parent, text, command):
        widget = ttk.Button(parent, text=text, command=lambda: self.safe(command))
        self.actions.append(widget)
        return widget

    def safe(self, command):
        if self.worker and self.worker.is_alive():
            return
        try:
            command()
        except Exception as exc:
            logging.exception("Calibration action failed: %s", exc)
            display = translate_message(str(exc))
            if get_locale() != "ru" and display == str(exc):
                display = tr("state.error") + "\n" + tr("detail.see_journal")
            messagebox.showerror(tr("settings.calibration"), display)

    def log(self, message):
        logging.info(message)
        display = translate_message(message)
        self.status.set(display)
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", time.strftime("%H:%M:%S ") + display + "\n")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def refresh_windows(self):
        self.windows = [w for w in self.desktop.windows() if w.handle != self.winfo_id() and "WoG ·" not in w.title]
        self.window_choice["values"] = [f"{Path(w.executable).name or '?'} · {w.title}" for w in self.windows]
        selected = next((i for i, w in enumerate(self.windows) if "genesis" in (w.title + w.executable).lower()), None)
        if selected is not None:
            self.window_choice.current(selected)
            self.select_window()

    def select_window(self, _=None):
        index = self.window_choice.current()
        if index >= 0:
            self.handle = self.windows[index].handle

    def save(self, invalidate=True):
        self.profile["mode"] = self.mode.get()
        if invalidate:
            self.profile["supervised_ok"] = False
        temp = self.profile_file.with_suffix(".tmp")
        temp.write_text(json.dumps(self.profile, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.profile_file)
        self.update_info()

    def mode_changed(self):
        self.save()

    def include_gems_changed(self):
        # Product selection always includes equipment and gems.
        self.profile["include_gems"] = True
        self.include_gems.set(True)
        self.save(invalidate=False)

    def save_transfer_filters(self, filters):
        selected = validate_transfer_filters(filters)
        changed = selected != self.profile.get("transfer_filters")
        self.profile["transfer_filters"] = selected
        self.save(invalidate=changed)

    def update_info(self):
        tiers = ", ".join(f"T{k}: {len(v)}" for k, v in sorted(self.profile.get("tiers", {}).items())) or "нет"
        self.calibration_info.set("Образцы рамок: " + tiers + ". Проверка одного переноса: " + ("пройдена" if self.profile.get("supervised_ok") else "ещё не пройдена") + ".")

    def require_snapshot(self):
        if self.snapshot is None:
            raise ValueError("Сначала нажми «Снять окно игры» и переключись в игру.")
        if list(self.snapshot.size) != self.profile.get("size"):
            raise ValueError("Снимок и профиль имеют разные размеры.")

    def prepare_game_window(self, *, hide_helper=True):
        if self.handle is None:
            raise ValueError("Выбери окно игры.")
        try:
            self.desktop.activate_for_start(self.handle)
            if hide_helper:
                self.iconify()
        except Exception:
            if hide_helper:
                self.deiconify()
            raise

    def capture(self, then=None):
        self.prepare_game_window()
        self.log("Окно игры активировано. Снимок через 5 секунд; обе сетки должны быть видны. Оставайся в игре.")
        self.after(5000, lambda: self.finish_capture(then))

    def capture_tooltip(self, index=None):
        try:
            index = int(self.tooltip_cell.get() if index is None else index)
        except (TypeError, ValueError):
            raise ValueError("Номер ячейки подсказки: целое число от 1 до 40.")
        if not 1 <= index <= 40:
            raise ValueError("Номер ячейки подсказки: от 1 до 40.")
        if not self.profile.get("size") or not self.profile.get("inventory"):
            raise ValueError("Сначала настрой размер окна и сетку сумки.")
        from runtime import make_grid
        grid = make_grid(self.profile["inventory"])
        expected_size = tuple(self.profile["size"])
        if index > grid.count or not grid.rect.fits(*expected_size):
            raise ValueError("Ячейка подсказки выходит за настроенную сетку сумки.")
        point = grid.cell_rect(index - 1).center
        handle = self.handle
        self.prepare_game_window()
        self.log(f"Окно игры активировано. Подсказка ячейки {index} через 5 секунд — только наведение, без клика. Оставайся в игре.")
        self.after(5000, lambda: self.finish_tooltip_capture(handle, index, point, expected_size))

    def finish_tooltip_capture(self, handle, index, point, expected_size):
        try:
            self.desktop.move(handle, point, expected_size)
            time.sleep(1.0)
            if self.desktop.stop_pressed():
                raise InterruptedError("Снимок подсказки отменён: F8.")
            image = self.desktop.capture(handle)
            if image.size != expected_size:
                raise ValueError("Размер окна изменился. Снимок подсказки не сохранён; настрой сетку заново.")
            path = PROFILE_DIR / f"tooltip-cell-{index}-{time.time_ns()}.png"
            image.save(path)
            self.tooltip_path.set(str(path))
            self.log(f"Подсказка ячейки {index} сохранена: {path}")
        except Exception as exc:
            self.log(str(exc))
        finally:
            self.deiconify()

    def finish_capture(self, then):
        try:
            image = self.desktop.capture(self.handle)
            calibrated = bool(self.profile.get("size") and self.profile.get("inventory")
                              and self.profile.get("storage") and self.profile.get("anchors"))
            # A diagnostic capture is not a new calibration. Existing coordinates
            # and samples remain canonical; the runner adopts physical geometry.
            if not calibrated:
                self.profile["size"] = list(image.size)
            self.snapshot = image
            self.snapshot_path = PROFILE_DIR / f"calibration-capture-{time.time_ns()}.png"
            image.save(self.snapshot_path)
            if not calibrated:
                self.save()
            self.log(f"Снимок {image.width}×{image.height} готов. Можно выделять сетки и добавлять образцы.")
            self.log(f"Нативный снимок сохранён: {self.snapshot_path}")
        except Exception as exc:
            self.log(str(exc))
            image = None
        finally:
            self.deiconify()
        if image and then:
            self.safe(then)

    def mark_grid(self, key):
        self.require_snapshot()
        rows, cols = [int(v.get()) for v in self.grid_vars[key]]
        if not (1 <= rows <= 30 and 1 <= cols <= 30):
            raise ValueError("Строки и столбцы: 1–30.")
        dialog = RegionDialog(self, self.snapshot, "Выдели сетку: " + key)
        if dialog.result:
            rect = dialog.result
            # Keep equal cell sizes; show the snapped grid in preview for inspection.
            cell_w, cell_h = round(rect.width / cols), round(rect.height / rows)
            rect = Rect(rect.left, rect.top, rect.left + cell_w * cols, rect.top + cell_h * rows)
            if min(cell_w, cell_h) < 12 or not rect.fits(*self.snapshot.size):
                raise ValueError("Сетка слишком маленькая либо выходит за снимок.")
            self.profile[key] = {"rect": list(rect.as_box()), "rows": rows, "columns": cols}
            self.profile[key + "_empty"] = []
            if key == "inventory":
                self.profile["tiers"] = {}
                self.profile.pop("badge", None)
                self.profile.pop("icon_rect", None)
            self.save()
            self.log("Сетка сохранена. Проверь точность границ в предпросмотре; добавь образец пустой ячейки.")

    def auto_setup(self):
        self.capture(self.finish_auto_setup)

    def finish_auto_setup(self):
        from autocalibration import build_profile
        interval = self.profile.get("interval", 1800)
        filters = self.profile.get("transfer_filters")
        self.profile = build_profile(self.snapshot, PROFILE_DIR, adaptive=True)
        self.profile["include_gems"] = True
        self.profile["interval"] = interval
        if filters is not None:
            self.profile["transfer_filters"] = filters
        self.profile = migrate_profile(self.profile)
        self.include_gems.set(True)
        self.mode.set("right")
        for key in ["inventory", "storage"]:
            self.grid_vars[key][0].set(str(self.profile[key]["rows"]))
            self.grid_vars[key][1].set(str(self.profile[key]["columns"]))
        self.save()
        self.show_preview()
        self.log("Автонастройка готова. Проверьте один перенос по выбранным фильтрам.")

    def grid(self, key):
        from runtime import make_grid
        if key not in self.profile:
            raise ValueError("Сначала выдели сетку " + key)
        return make_grid(self.profile[key])

    def cell(self, key):
        self.require_snapshot()
        grid = self.grid(key)
        index = simpledialog.askinteger(tr("legacy.dialog_cell_title"), tr("legacy.dialog_cell_help"), minvalue=1, maxvalue=grid.count, parent=self)
        return grid.cell_rect(index - 1) if index else None

    def save_image(self, image, prefix):
        name = f"{prefix}_{time.time_ns()}.png"
        image.save(PROFILE_DIR / name)
        return name

    def add_empty(self, key):
        rect = self.cell(key)
        if rect:
            name = self.save_image(self.snapshot.crop(rect.as_box()), key + "_empty")
            self.profile.setdefault(key + "_empty", []).append(name)
            self.save()

    def mark_cell_region(self, key):
        self.require_snapshot()
        rect = self.grid("inventory").cell_rect(0)
        prompt = "Фрагмент цветной рамки — без Lv и рисунка" if key == "badge" else "Только буквы Lv и их обводка, без числа уровня"
        dialog = RegionDialog(self, self.snapshot.crop(rect.as_box()), prompt)
        if dialog.result:
            if key == "badge":
                self.profile[key] = list(dialog.result.as_box())
                self.profile["tiers"] = {}
            else:
                image = self.snapshot.crop(rect.as_box()).crop(dialog.result.as_box())
                name = self.save_image(image, "equipment_label")
                self.profile.setdefault("equipment_label_samples", []).append(name)
            self.save()

    def add_tier(self, tier):
        if "badge" not in self.profile:
            raise ValueError("Сначала выдели фрагмент рамки.")
        rect = self.cell("inventory")
        if rect:
            badge = self.snapshot.crop(rect.as_box()).crop(tuple(self.profile["badge"]))
            name = self.save_image(badge, f"T{tier}")
            self.profile.setdefault("tiers", {}).setdefault(str(tier), []).append(name)
            self.save()

    def add_anchor(self, index):
        self.require_snapshot()
        dialog = RegionDialog(self, self.snapshot, "Неподвижный фрагмент заголовка " + ("инвентаря" if index == 0 else "хранилища"))
        if dialog.result:
            rect = dialog.result
            anchors = self.profile.setdefault("anchors", [])
            while len(anchors) <= index:
                anchors.append(None)
            anchors[index] = {"rect": list(rect.as_box()), "image": self.save_image(self.snapshot.crop(rect.as_box()), f"anchor{index}")}
            self.save()

    def mark_button(self):
        self.require_snapshot()
        dialog = RegionDialog(self, self.snapshot, "Кнопка «В хранилище» после ручного выбора предмета")
        if dialog.result:
            rect = dialog.result
            self.profile["transfer_button"] = {"rect": list(rect.as_box()), "image": self.save_image(self.snapshot.crop(rect.as_box()), "transfer_button")}
            self.save()

    def export_snapshot(self):
        self.require_snapshot()
        name = filedialog.asksaveasfilename(defaultextension=".png", initialfile="wog_inventory_storage.png", filetypes=[("PNG", "*.png")], parent=self)
        if name:
            self.snapshot.save(name)
            self.log("Снимок сохранён.")

    def make_runner(self):
        if self.handle is None:
            raise ValueError("Выбери окно игры.")
        if not all(self.profile.get(k) for k in ["size", "inventory", "storage", "badge"]):
            raise ValueError("Настрой обе сетки и область рамки.")
        if not all(self.profile.get("anchors", [])) or len(self.profile.get("anchors", [])) < 2:
            raise ValueError("Запомни два ориентира открытых панелей.")
        # Saved coordinates and samples remain a canonical baseline. The runner
        # resolves the actual panel scale/position on each cycle in memory.
        runtime_profile = dict(self.profile, automatic_layout=True)
        return Runner(self.desktop, self.handle, runtime_profile, PROFILE_DIR, self.cancel,
                      lambda message: self.events.put(("log", message)),
                      state_callback=lambda payload: self.events.put(("state", payload)),
                      handle_resolver=self.resolve_game_handle)

    def resolve_game_handle(self):
        from game_identity import game_windows
        candidates = game_windows(self.desktop.windows())
        return candidates[0].handle if len(candidates) == 1 else None

    def open_panels(self, inspect_pages=False):
        """Explicit UI action: open and verify panels without sorting or transfer."""
        self.cancel = threading.Event()
        runner = self.make_runner()
        for button in self.actions:
            button.configure(state="disabled")
        self.window_choice.configure(state="disabled")
        try:
            self.prepare_game_window()
        except InterruptedError as exc:
            # F8 may be consumed while the explicit foreground request is still
            # on the UI thread. Preserve that cancellation for the normal poll
            # path without constructing a worker or granting transfer proof.
            self.cancel.set()
            for button in self.actions:
                button.configure(state="normal")
            self.window_choice.configure(state="readonly")
            self.events.put(("log", str(exc)))
            self.events.put(("finished", None))
            return
        except Exception:
            for button in self.actions:
                button.configure(state="normal")
            self.window_choice.configure(state="readonly")
            raise
        self.log("Проверка игровых панелей через 5 секунд. F8 — остановка.")

        def work():
            ready = False
            try:
                runner.wait(5)
                if inspect_pages:
                    pages = runner.inspect_storage_pages()
                    for page in pages:
                        self.events.put(("log", f"Страница {page['id']}: свободно {page['free']}, неопределённых ячеек {page['unknown']}."))
                    self.events.put(("log", "Проверка страниц завершена; исходная страница восстановлена."))
                else:
                    runner.prepare_panels()
                ready = True
                self.events.put(("log", "Окна персонажа и хранилища открыты и проверены."))
            except InterruptedError as exc:
                self.cancel.set()
                self.events.put(("log", str(exc)))
            except Exception as exc:
                logging.exception("Helper cycle stopped: %s", exc)
                self.events.put(("log", str(exc)))
            finally:
                self.events.put(("panels_ready", ready))
                self.events.put(("finished", None))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def preview(self):
        self.capture(self.show_preview)

    def show_preview(self):
        runner = self.make_runner()
        image, inv, store, candidate = runner.preview(self.snapshot)
        overlay = image.copy()
        draw = ImageDraw.Draw(overlay)
        for cell in inv.cells:
            color = "#33ff88" if cell.tier and cell.tier >= 3 else "#aaaaaa"
            draw.rectangle(cell.rect.as_box(), outline=color, width=2)
            draw.text((cell.rect.left + 2, cell.rect.top + 2), cell.label or "?", fill=color)
        for cell in store.cells:
            draw.rectangle(cell.rect.as_box(), outline="#00bbff" if cell.empty is True else "#999999", width=1)
        if candidate:
            draw.rectangle(candidate.source_rect.as_box(), outline="#ffff00", width=4)
            draw.rectangle(candidate.destination_rect.as_box(), outline="#ffff00", width=4)
        scale = min(960 / image.width, 370 / image.height, 1)
        display = overlay.resize((round(image.width * scale), round(image.height * scale)))
        self.preview_picture = ImageTk.PhotoImage(display)
        self.preview_canvas.delete("all")
        self.preview_canvas.create_image(0, 0, image=self.preview_picture, anchor="nw")
        summary = f"Сумка: {inv.tier_counts}; неизвестных {inv.unknown_count}. Свободно на странице: {store.empty_count}. "
        summary += f"Кандидат: ячейка {candidate.source_index + 1}, T{candidate.tier}." if candidate else "Распознанной подходящей экипировки нет."
        self.log(summary)

    def start(self, continuous):
        interval = validate_interval(self.interval.get())
        self.profile["interval"] = interval
        self.save(invalidate=False)
        self.cancel = threading.Event()
        runner = self.make_runner()
        for button in self.actions:
            button.configure(state="disabled")
        self.window_choice.configure(state="disabled")
        focus_pending = False
        try:
            self.prepare_game_window(hide_helper=not continuous)
        except InterruptedError as exc:
            self.cancel.set()
            for button in self.actions:
                button.configure(state="normal")
            self.window_choice.configure(state="readonly")
            self.events.put(("log", str(exc)))
            self.events.put(("finished", None))
            return
        except FocusUnavailable as exc:
            if not continuous or exc.input_issued is not False:
                for button in self.actions:
                    button.configure(state="normal")
                self.window_choice.configure(state="readonly")
                raise
            # Explicit Start authorizes the scheduler even when Windows denies
            # this one activation request. Keep Helper visible and let monitor
            # honour ordinary foreground/idle/input checks before any transfer.
            logging.info(exc.diagnostic_message)
            focus_pending = True
            self.log(FOCUS_WAITING)
            runner.state("paused", FOCUS_WAITING)
        except Exception:
            for button in self.actions:
                button.configure(state="normal")
            self.window_choice.configure(state="readonly")
            raise
        if continuous:
            if not focus_pending:
                self.log(f"Окно игры активировано. Первая проверка начинается сейчас; следующие — через {interval:g} секунд. F8 — остановка.")
        else:
            self.log("Окно игры активировано. Старт через 5 секунд. F8 — остановка. При смене окна AFK ждёт возвращения в игру.")

        def work():
            try:
                if continuous:
                    runner.enable_connection_monitor()
                    runner.wait(1.0)  # Allow the explicitly restored game to render.
                    runner.monitor(interval)
                else:
                    runner.wait(5)
                    moved = runner.once()
                    self.events.put(("verified", moved))
            except StorageFull:
                self.events.put(("log", STORAGE_FULL_MESSAGE))
                self.events.put(("state", {"state": "storage_full", "message": STORAGE_FULL_MESSAGE,
                                           "next_check_at": None}))
            except InterruptedError as exc:
                self.cancel.set()
                self.events.put(("log", str(exc)))
            except Exception as exc:
                logging.exception("Helper cycle stopped: %s", exc)
                self.events.put(("log", str(exc)))
            finally:
                close_connection = getattr(runner, 'close_connection_monitor', None)
                if close_connection is not None:
                    close_connection()
                self.events.put(("finished", None))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def stop(self):
        self.cancel.set()

    def poll(self):
        if self.worker and self.worker.is_alive() and self.desktop.stop_pressed():
            self.cancel.set()
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.log(value)
                elif kind == "verified" and value:
                    self.profile["supervised_ok"] = True
                    self.save(invalidate=False)
                    self.log("Один перенос подтверждён. Проверь его результат в игре перед AFK.")
                elif kind == "finished":
                    for button in self.actions:
                        button.configure(state="normal")
                    self.window_choice.configure(state="readonly")
                    self.deiconify()
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def close(self):
        self.cancel.set()
        if self.worker and self.worker.is_alive():
            self.after(150, self.close)
        else:
            close_desktop = getattr(self.desktop, "close", None)
            if close_desktop is not None:
                close_desktop()
            self.destroy()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        import unittest
        suite = unittest.defaultTestLoader.discover(str(BASE), pattern="test_*.py")
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(0 if result.wasSuccessful() else 1)
    try:
        App().mainloop()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
