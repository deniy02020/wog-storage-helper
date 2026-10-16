"""Readable session journal. Original diagnostics remain in the application log.

The dashboard calls ``append`` on Tk's main thread while draining its worker
queue. Only visible cards are painted, so a long session does not create one Tk
widget per transfer. This window never reads or modifies the game or log files.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import tkinter as tk
from tkinter import font as tkfont, ttk

from i18n import tr, translate_message
from ui_brand import apply_icon
from ui_theme import BG, SURFACE, FIELD, BORDER, TEXT, MUTED, ACCENT, GREEN, AMBER, ROSE, dark_titlebar
from ui_widgets import AnimatedButton, AnimatedReveal, RoundedCard, _rounded_points


_TRANSFER = re.compile(r"Перенесён T([1-6]): сумка (\d+) → хранилище (\d+)\.")
_UNCONFIRMED_TRANSFER = re.compile(r"^Перенос не подтверждён; повторных кликов не будет\.(?:\s|$)")
_ERROR = re.compile(r"не удалось|не разрешила|не совпал|не совпала|не найден|ошибк|отмен[её]н|работа остановлена|хранилище заполнено", re.I)
_WAIT = re.compile(r"^(?:Пауза:|Ожидаю|Вернитесь|Фоновая проверка прервана)|прерван[ао]? вводом", re.I)
_WAIT_MESSAGES = frozenset({
    "Видимые предметы обработаны. Полная проверка продолжится в следующем цикле.",
    "Полная проверка инвентаря пока не подтверждена. Повторю в следующем цикле.",
})
_TECHNICAL = re.compile(r"Traceback|\b(?:RMS|RMSE|HWND|PID|SHA256|capture|input_issued)\b|[A-Za-z]:[\\/]|\{.*\}", re.I)


@dataclass(frozen=True)
class JournalEntry:
    index: int
    stamp: str
    raw: str
    kind: str
    tier: str | None = None

    @classmethod
    def from_record(cls, index, stamp, message):
        raw = str(message)
        transfer = _TRANSFER.fullmatch(raw.strip())
        kind = ("transfer" if transfer else "waiting" if raw.strip() in _WAIT_MESSAGES or _WAIT.search(raw.strip()) else
                "attention" if _UNCONFIRMED_TRANSFER.match(raw.strip()) or _ERROR.search(raw) else "activity")
        return cls(index, str(stamp).strip(), raw, kind, transfer[1] if transfer else None)

    def summary(self, *, locale=None):
        if self.kind == "transfer":
            return tr("journal.transfer_summary", locale=locale, tier=self.tier)
        if _UNCONFIRMED_TRANSFER.match(self.raw.strip()):
            return tr("journal.transfer_unconfirmed", locale=locale)
        translated = translate_message(self.raw, locale=locale)
        # Unmapped diagnostics can contain paths, native IDs and pixel scores.
        # Keep them intact in Details rather than putting them in event cards.
        known = translate_message(self.raw, locale="en-US") != self.raw
        if not known or _TECHNICAL.search(translated):
            return tr("journal.diagnostic", locale=locale)
        return " ".join(translated.split())

    def label(self, *, locale=None):
        return tr("journal.kind." + self.kind, locale=locale)

    def matches(self, category="all", query="", *, locale=None):
        if category == "transfer" and self.kind != "transfer":
            return False
        if category == "attention" and self.kind not in {"attention", "waiting"}:
            return False
        needle = query.strip().casefold()
        if not needle:
            return True
        # Raw details are searchable, but remain hidden until the user opens
        # the selected event. Search never executes content from a log entry.
        haystack = " ".join((self.stamp, self.raw, self.summary(locale=locale), self.label(locale=locale)))
        return needle in haystack.casefold()


def filtered_entries(entries, category="all", query="", *, locale=None):
    return [entry for entry in entries if entry.matches(category, query, locale=locale)]


def copy_entries(entries, *, locale=None):
    """Copy the visible summaries; technical details stay in their own panel."""
    return "\n".join(f"{entry.stamp}  {entry.summary(locale=locale)}" for entry in entries)


def at_tail(view):
    # A fraction like 99.8% can still be dozens of cards from the end of a
    # long session. Follow only when the actual final edge is visible.
    return bool(view) and view[-1] >= 1.0 - 1e-9


def wrap_lines(text, width, measure, limit=2):
    """Wrap localized text, including CJK strings without word separators."""
    lines, remaining = [], str(text).strip()
    width = max(1, width)
    while remaining and len(lines) < limit:
        if measure(remaining) <= width:
            lines.append(remaining)
            break
        low, high = 1, len(remaining)
        while low < high:
            mid = (low + high + 1) // 2
            if measure(remaining[:mid]) <= width:
                low = mid
            else:
                high = mid - 1
        count = low
        if len(lines) == limit - 1:
            while count > 1 and measure(remaining[:count].rstrip() + "…") > width:
                count -= 1
            lines.append(remaining[:count].rstrip() + "…")
            break
        space = remaining.rfind(" ", 0, count + 1)
        if space >= count // 2 and space > 0:
            count = space
        lines.append(remaining[:count].rstrip())
        remaining = remaining[count:].lstrip()
    return lines


class EventJournal(tk.Toplevel):
    CARD_HEIGHT = 94
    COLORS = {"transfer": GREEN, "waiting": AMBER, "attention": ROSE, "activity": ACCENT}

    def __init__(self, master, records=(), *, animations_enabled=True):
        super().__init__(master)
        self._closed = False
        self._timers = set()
        self._render_token = None
        self._entries = [JournalEntry.from_record(index, *record) for index, record in enumerate(records)]
        self._visible = list(self._entries)
        self._selected = None
        self._category = "all"
        self._follow_tail = True
        self._new_count = 0
        self._details_open = False
        self._animations_enabled = bool(animations_enabled)
        self.title("WoG Storage Helper · " + tr("journal.title"))
        self.geometry("850x650")
        self.minsize(680, 490)
        self.configure(background=BG, padx=24, pady=22)
        self.transient(master)
        apply_icon(self)
        self._body_font = tkfont.Font(self, family="Segoe UI", size=11)
        self._label_font = tkfont.Font(self, family="Segoe UI", size=9)
        self._build()
        self.bind("<Destroy>", self._destroy_event, add="+")
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Control-f>", self._focus_search)
        self.bind("<Control-F>", self._focus_search)
        self._search_trace = self._query.trace_add("write", self._filter_changed)
        self._schedule(150, lambda: dark_titlebar(self))
        self._request_render()
        self._schedule(0, self.canvas.focus_set)

    def _build(self):
        heading = tk.Frame(self, background=BG)
        heading.pack(fill="x")
        tk.Label(heading, text=tr("journal.heading"), foreground=TEXT, background=BG,
                 font=("Segoe UI", 22, "bold")).pack(side="left")
        self._counter = tk.StringVar()
        tk.Label(heading, textvariable=self._counter, foreground=MUTED, background=BG,
                 font=("Segoe UI", 9)).pack(side="right", pady=(9, 0))
        tk.Label(self, text=tr("journal.session"), foreground=MUTED, background=BG,
                 font=("Segoe UI", 10)).pack(anchor="w", pady=(4, 18))
        filters = tk.Frame(self, background=BG)
        filters.pack(fill="x", pady=(0, 12))
        self._filter_buttons = {}
        for value in ("all", "transfer", "attention"):
            button = AnimatedButton(filters, text=tr("journal.filter." + value), width=158,
                height=36, radius=18, variant="secondary" if value == "all" else "ghost",
                selected=value == "all", font=("Segoe UI", 9, "bold"),
                command=lambda choice=value: self._choose_filter(choice),
                animations_enabled=self._animations_enabled)
            button.pack(side="left", padx=(0, 6))
            self._filter_buttons[value] = button
        search = RoundedCard(self, padding=10, radius=16, bg=FIELD, background=BG)
        search.pack(fill="x", pady=(0, 14))
        tk.Label(search.body, text="⌕", background=FIELD, foreground=MUTED,
                 font=("Segoe UI", 17)).pack(side="left", padx=(2, 9))
        self._query = tk.StringVar()
        self.search = tk.Entry(search.body, textvariable=self._query, relief="flat", borderwidth=0,
            background=FIELD, foreground=TEXT, insertbackground=TEXT, font=("Segoe UI", 11),
            selectbackground=ACCENT, selectforeground=BG, highlightthickness=0)
        self.search.pack(side="left", fill="x", expand=True)
        self._search_hint = tk.Label(search.body, text=tr("journal.search"), background=FIELD,
                                    foreground=MUTED, font=("Segoe UI", 9))
        self._search_hint.pack(side="right", padx=(8, 3))
        self.search.bind("<FocusIn>", lambda _event: self._search_hint.configure(text="Ctrl+F"))
        self.search.bind("<FocusOut>", lambda _event: self._search_hint.configure(text=tr("journal.search")))
        footer = tk.Frame(self, background=BG)
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        self._notice = tk.StringVar(value=tr("journal.select_event"))
        tk.Label(footer, textvariable=self._notice, foreground=MUTED, background=BG,
                 font=("Segoe UI", 9)).pack(side="left")
        self.copy_button = AnimatedButton(footer, text=tr("journal.copy"), width=155, height=34,
            radius=17, variant="ghost", command=self._copy_visible,
            animations_enabled=self._animations_enabled)
        self.copy_button.pack(side="right")
        self.details_button = AnimatedButton(footer, text=tr("journal.details"), width=122, height=34,
            radius=17, variant="ghost", state="disabled", command=self._toggle_details,
            animations_enabled=self._animations_enabled)
        self.details_button.pack(side="right", padx=(0, 6))
        self.details = AnimatedReveal(self, height=155, revealed=False, bg=BG,
                                      animations_enabled=self._animations_enabled)
        self.details.pack(side="bottom", fill="x", pady=(8, 0))
        detail_header = tk.Frame(self.details.body, background=BG)
        detail_header.pack(fill="x", pady=(0, 7))
        tk.Label(detail_header, text=tr("journal.technical"), foreground=MUTED, background=BG,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.details_text = tk.Text(self.details.body, height=5, wrap="word", relief="flat",
            background=SURFACE, foreground=TEXT, insertbackground=TEXT, padx=14, pady=10,
            font=("Consolas", 9), borderwidth=0, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
            selectbackground=ACCENT, selectforeground=BG, state="disabled")
        self.details_text.pack(fill="both", expand=True)
        self.jump_button = AnimatedButton(self, text=tr("journal.new", count=0), width=200, height=32,
            radius=16, variant="secondary", command=self._jump_to_latest,
            animations_enabled=self._animations_enabled)
        list_frame = tk.Frame(self, background=BG)
        self._list_frame = list_frame
        list_frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(list_frame, background=BG, borderwidth=0, highlightthickness=0,
                                takefocus=1, yscrollincrement=self.CARD_HEIGHT // 3)
        self._scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self._scroll)
        self._scrollbar.pack(side="right", fill="y", padx=(10, 0))
        self.canvas.configure(yscrollcommand=self._view_changed)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self._request_render())
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-1>", self._clicked)
        self.canvas.bind("<Control-c>", self._copy_visible)
        self.canvas.bind("<Control-C>", self._copy_visible)
        for name, delta in (("Up", -1), ("Down", 1), ("Prior", -4), ("Next", 4)):
            self.canvas.bind("<" + name + ">", lambda _event, step=delta: self._navigate(step))
        self.canvas.bind("<Home>", lambda _event: self._navigate(-len(self._visible)))
        self.canvas.bind("<End>", lambda _event: self._navigate(len(self._visible)))
        self.canvas.bind("<Return>", lambda _event: self._toggle_details())

    def _schedule(self, delay, callback):
        if self._closed:
            return None
        token = None
        def run():
            self._timers.discard(token)
            if not self._closed:
                callback()
        token = self.after(delay, run)
        self._timers.add(token)
        return token

    def _request_render(self):
        if self._render_token is None and not self._closed:
            self._render_token = self._schedule(0, self._render)

    def append(self, stamp, message):
        if self._closed:
            return
        entry = JournalEntry.from_record(len(self._entries), stamp, message)
        self._entries.append(entry)
        if entry.matches(self._category, self._query.get()):
            self._visible.append(entry)
            if not self._follow_tail:
                self._new_count += 1
        self._request_render()

    def _choose_filter(self, value):
        self._category = value
        for category, button in self._filter_buttons.items():
            button.configure(selected=category == value,
                             variant="secondary" if category == value else "ghost")
        self._filter_changed()

    def _filter_changed(self, *_args):
        self._visible = filtered_entries(self._entries, self._category, self._query.get())
        self._follow_tail, self._new_count = True, 0
        if self._selected not in self._visible:
            self._selected = None
            self._details_open = False
            self.details.configure(revealed=False)
        self._request_render()
        self._update_selection()

    def _view_changed(self, first, last):
        self._scrollbar.set(first, last)
        # Redraw virtual rows when scrolling without forcing the reader back
        # down. Tail preference is changed only by explicit user scrolling.
        self._request_render()

    def _scroll(self, *args):
        self.canvas.yview(*args)
        self._follow_tail = at_tail(self.canvas.yview())
        if self._follow_tail:
            self._new_count = 0
        self._request_render()

    def _wheel(self, event):
        if event.delta:
            self._scroll("scroll", -math.ceil(abs(event.delta) / 120) * (1 if event.delta > 0 else -1), "units")
        return "break"

    def _render(self):
        self._render_token = None
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        total_height = max(height, len(self._visible) * self.CARD_HEIGHT)
        region = (0, 0, width, total_height)
        if getattr(self, "_scroll_region", None) != region:
            self._scroll_region = region
            self.canvas.configure(scrollregion=region)
        if self._follow_tail and not at_tail(self.canvas.yview()):
            self.canvas.yview_moveto(1)
        if self._follow_tail:
            self._new_count = 0
        self.canvas.delete("event")
        self._counter.set(tr("journal.count", count=len(self._visible), total=len(self._entries)))
        self.copy_button.configure(state="normal" if self._visible else "disabled")
        if self._new_count:
            self.jump_button.configure(text=tr("journal.new", count=self._new_count))
            if not self.jump_button.winfo_manager():
                self.jump_button.pack(side="bottom", pady=(8, 0), before=self._list_frame)
        elif self.jump_button.winfo_manager():
            self.jump_button.pack_forget()
        if not self._visible:
            self.canvas.create_text(width / 2, height / 2 - 15,
                text=tr("journal.no_matches" if self._entries else "journal.empty"), fill=MUTED,
                font=("Segoe UI", 12), tags="event")
            return
        top = self.canvas.canvasy(0)
        start = max(0, int(top // self.CARD_HEIGHT))
        end = min(len(self._visible), int((top + height) // self.CARD_HEIGHT) + 2)
        for row in range(start, end):
            entry = self._visible[row]
            y = row * self.CARD_HEIGHT
            color = self.COLORS[entry.kind]
            selected = entry == self._selected
            self.canvas.create_polygon(_rounded_points(1, y + 2, width - 1, y + 84, 16),
                fill=FIELD if selected else SURFACE, outline=ACCENT if selected else BORDER,
                width=1, tags="event")
            self.canvas.create_oval(17, y + 19, 23, y + 25, fill=color, outline="", tags="event")
            self.canvas.create_text(32, y + 22, anchor="w", text=entry.label(), fill=color,
                font=self._label_font, tags="event")
            self.canvas.create_text(width - 17, y + 22, anchor="e", text=entry.stamp,
                fill=MUTED, font=self._label_font, tags="event")
            lines = wrap_lines(entry.summary(), max(1, width - 36), self._body_font.measure)
            self.canvas.create_text(18, y + 38, anchor="nw", text="\n".join(lines), fill=TEXT,
                font=self._body_font, tags="event")

    def _clicked(self, event):
        row = int(self.canvas.canvasy(event.y) // self.CARD_HEIGHT)
        if 0 <= row < len(self._visible):
            self._selected = self._visible[row]
            self.canvas.focus_set()
            self._update_selection()
            self._request_render()

    def _navigate(self, step):
        if not self._visible:
            return "break"
        current = self._visible.index(self._selected) if self._selected in self._visible else len(self._visible) - 1
        target = max(0, min(len(self._visible) - 1, current + step))
        self._selected = self._visible[target]
        top, height = self.canvas.canvasy(0), max(1, self.canvas.winfo_height())
        row_top = target * self.CARD_HEIGHT
        total = max(height, len(self._visible) * self.CARD_HEIGHT)
        if row_top < top:
            self.canvas.yview_moveto(row_top / total)
        elif row_top + self.CARD_HEIGHT > top + height:
            self.canvas.yview_moveto((row_top + self.CARD_HEIGHT - height) / total)
        self._follow_tail = at_tail(self.canvas.yview())
        self._update_selection()
        self._request_render()
        return "break"

    def _update_selection(self):
        entry = self._selected
        self.details_button.configure(state="normal" if entry else "disabled",
            text=tr("journal.hide_details" if self._details_open else "journal.details"))
        self._notice.set(f"{entry.stamp} · {entry.label()}" if entry else tr("journal.select_event"))
        self.details_text.configure(state="normal")
        self.details_text.delete("1.0", "end")
        if entry:
            self.details_text.insert("end", f"{entry.stamp}  {entry.raw}")
        self.details_text.configure(state="disabled")

    def _toggle_details(self):
        if self._selected:
            self._details_open = not self._details_open
            self.details.configure(revealed=self._details_open)
            self._update_selection()
        return "break"

    def _jump_to_latest(self):
        self._follow_tail, self._new_count = True, 0
        self._request_render()

    def _focus_search(self, _event=None):
        self.search.focus_set()
        self.search.selection_range(0, "end")
        return "break"

    def _copy_visible(self, _event=None):
        if self._visible:
            self.clipboard_clear()
            self.clipboard_append(copy_entries(self._visible))
            self._notice.set(tr("journal.copied", count=len(self._visible)))
        return "break"

    def _cleanup(self):
        if self._closed:
            return
        self._closed = True
        for token in tuple(self._timers):
            try:
                self.after_cancel(token)
            except tk.TclError:
                pass
        self._timers.clear()
        self._render_token = None
        if hasattr(self, "_search_trace"):
            self._query.trace_remove("write", self._search_trace)

    def _destroy_event(self, event):
        if event.widget is self:
            self._cleanup()

    def destroy(self):
        self._cleanup()
        super().destroy()
