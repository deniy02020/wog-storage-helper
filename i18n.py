"""Helper-owned translations; no imports or corpus reuse from Companion.

Supported interface languages were checked on the official Steam page for
app 4891320 on 2026-10-11. Russian is the default and unknown-locale fallback.
Every supported locale must contain every key; there is no English fallback.
The UI owner persists the selected locale and refreshes its labels explicitly.
"""
from __future__ import annotations

from functools import lru_cache
import json
import math
from pathlib import Path
import re
from string import Formatter


DEFAULT_LOCALE = "ru"
LOCALE_NAMES = {
    "ru": "Русский", "en-US": "English", "fr": "Français", "es": "Español",
    "it": "Italiano", "uk": "Українська", "pl": "Polski", "de": "Deutsch",
    "pt-BR": "Português (Brasil)", "id": "Bahasa Indonesia", "th": "ไทย",
    "ja": "日本語", "ko": "한국어", "vi": "Tiếng Việt", "zh-CN": "简体中文",
    "tr": "Türkçe", "zh-TW": "繁體中文",
}
SUPPORTED_LOCALES = tuple(LOCALE_NAMES)
_locale = DEFAULT_LOCALE


def locale_choices():
    return tuple(LOCALE_NAMES.items())


def normalize_locale(value=None):
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_LOCALE
    value = value.strip()
    for code, name in LOCALE_NAMES.items():
        if value.casefold() in {code.casefold(), name.casefold()}:
            return code
    normalized = value.replace("_", "-").split(".", 1)[0].casefold()
    aliases = {"english": "en-US", "russian": "ru", "french": "fr",
               "german": "de", "spanish": "es", "italian": "it",
               "ukrainian": "uk", "polish": "pl", "portuguese": "pt-BR",
               "indonesian": "id", "thai": "th", "japanese": "ja",
               "korean": "ko", "vietnamese": "vi", "turkish": "tr",
               "schinese": "zh-CN", "tchinese": "zh-TW"}
    if normalized in aliases:
        return aliases[normalized]
    base = normalized.split("-", 1)[0]
    if base == "zh":
        return "zh-TW" if any(part in normalized.split("-") for part in ("tw", "hk", "mo", "hant")) else "zh-CN"
    if base == "en":
        return "en-US"
    if base == "pt":
        return "pt-BR"
    return base if base in LOCALE_NAMES else aliases.get(base, DEFAULT_LOCALE)


def set_locale(value):
    global _locale
    _locale = normalize_locale(value)
    return _locale


def get_locale():
    return _locale


def _fields(text):
    fields = set()
    for _literal, name, spec, conversion in Formatter().parse(text):
        if name is not None:
            if not name.isidentifier() or spec or conversion:
                raise ValueError("Translation placeholders must be simple names.")
            fields.add(name)
    return fields


def validate_catalog(data):
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, dict) or set(messages) != set(SUPPORTED_LOCALES):
        raise ValueError("The Helper catalog must contain all 17 supported locales.")
    source = messages[DEFAULT_LOCALE]
    if not isinstance(source, dict) or not source:
        raise ValueError("The Russian Helper catalog is empty.")
    expected = {key: _fields(text) for key, text in source.items()}
    for code, catalog in messages.items():
        if not isinstance(catalog, dict) or set(catalog) != set(source):
            raise ValueError(f"Translation keys differ for {code}.")
        for key, text in catalog.items():
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"Empty translation: {code}/{key}.")
            if _fields(text) != expected[key]:
                raise ValueError(f"Translation placeholders differ: {code}/{key}.")
    return messages


@lru_cache(maxsize=1)
def _catalog():
    path = Path(__file__).with_name("locales.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    messages = validate_catalog(data)
    # Optional installer translations are independently authored and packaged.
    installer_path = Path(__file__).parent / "installer" / "installer_locales.json"
    if installer_path.is_file():
        extra = json.loads(installer_path.read_text(encoding="utf-8"))
        if "messages" in extra:
            extra = extra["messages"]
        if set(extra) != set(SUPPORTED_LOCALES):
            raise ValueError("Installer translations must contain all supported locales.")
        for code in SUPPORTED_LOCALES:
            overlap = set(messages[code]) & set(extra[code])
            if overlap:
                raise ValueError("Installer translations duplicate Helper keys.")
            messages[code].update(extra[code])
        validate_catalog({"messages": messages})
    return messages


def catalog_keys():
    return tuple(_catalog()[DEFAULT_LOCALE])


def tr(key, *, locale=None, **args):
    code = get_locale() if locale is None else normalize_locale(locale)
    catalog = _catalog()[code]
    if key not in catalog:
        raise KeyError(f"Unknown Helper translation key: {key}")
    missing = _fields(catalog[key]) - set(args)
    if missing:
        raise ValueError(f"Missing translation arguments for {key}: {', '.join(sorted(missing))}")
    return catalog[key].format(**args)


def format_duration(seconds, countdown=False, *, locale=None):
    value = float(seconds)
    if not math.isfinite(value):
        raise ValueError("Duration must be finite.")
    total = max(0, math.ceil(value))
    if not countdown and total >= 60:
        total = math.ceil(total / 60) * 60
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        if countdown and seconds:
            return tr("time.hms", locale=locale, hours=hours, minutes=minutes, seconds=seconds)
        if minutes:
            return tr("time.hm", locale=locale, hours=hours, minutes=minutes)
        return tr("time.hours", locale=locale, value=hours)
    if minutes:
        if countdown and seconds:
            return tr("time.ms", locale=locale, minutes=minutes, seconds=seconds)
        return tr("time.minutes", locale=locale, value=minutes)
    return tr("time.seconds", locale=locale, value=seconds)


LEGACY_TEXT_KEYS = {
    "Обновить окна": "legacy.refresh",
    "Снять окно игры": "settings.screenshot",
    "1. Настройка": "settings.calibration",
    "2. Проверка и AFK": "legacy.run_tab",
    "Автонастройка по твоим скриншотам": "settings.auto",
    "Сетки: выделяй только полные ячейки, без заголовков": "legacy.grids",
    "Инвентарь": "legacy.inventory",
    "Хранилище: видимая страница": "legacy.storage",
    "Строк": "legacy.rows", "Столбцов": "legacy.columns",
    "Выделить сетку": "legacy.mark_grid", "Пустая ячейка": "legacy.empty",
    "Образцы внутри первой ячейки инвентаря": "legacy.sample_region",
    "Фрагмент цветной рамки": "legacy.badge",
    "Буквы Lv на экипировке": "legacy.lv",
    "Рамка определяет тир. Буквы Lv — признак экипировки; число не учитывается.": "legacy.lv_help",
    "Образцы рамок: обязательны T1, T2 и каждый переносимый тир": "legacy.tiers",
    "Ориентиры: неподвижные части обоих открытых окон": "legacy.anchors",
    "Ориентир инвентаря": "legacy.inventory_anchor",
    "Ориентир хранилища": "legacy.storage_anchor",
    "Сохранить снимок…": "settings.export",
    "Способ переноса — выбери только проверенный вручную в игре": "legacy.transfer_method",
    "Перетаскивание": "legacy.drag", "Правый клик": "legacy.right",
    "Выбор вещи → кнопка": "legacy.button",
    "Запомнить кнопку переноса": "legacy.mark_button",
    "Каждая проверка сканирует обе сетки. После переноса координаты вычисляются заново.": "legacy.scan_help",
    "Предпросмотр — без кликов": "settings.preview",
    "Один перенос (через 5 с)": "action.single",
    "Запустить AFK (через 5 с)": "action.start", "СТОП · F8": "legacy.stop",
    "Интервал проверки, секунд:": "legacy.interval",
    "Ячейка сумки:": "legacy.cell", "Снять подсказку ячейки": "legacy.tooltip",
    "Включать самоцветы T3+": "legacy.gems",
    "Выбери окно игры, открой обе панели и сними окно.": "legacy.initial",
    "← Главная панель": "action.back", "Открыть игровые панели": "settings.panels",
    "Проверить страницы": "settings.pages",
    "WoG · Экипировка и самоцветы T3+ · Прототип": "app.name",
    "Профиль": "settings.profile",
    "Сохранить": "settings.save",
    "Номер ячейки": "legacy.dialog_cell_title",
    "Номер ячейки: слева направо, сверху вниз, начиная с 1.": "legacy.dialog_cell_help",
    "Фрагмент цветной рамки — без Lv и рисунка": "legacy.dialog_badge",
    "Только буквы Lv и их обводка, без числа уровня": "legacy.dialog_lv",
    "Неподвижный фрагмент заголовка инвентаря": "legacy.dialog_inventory_anchor",
    "Неподвижный фрагмент заголовка хранилища": "legacy.dialog_storage_anchor",
    "Кнопка «В хранилище» после ручного выбора предмета": "legacy.dialog_button",
    "Перед переносом сумка сортируется: высокие тиры перемещаются в начало. Используются твои 4 страницы хранилища.\nПри запуске игра активируется один раз. AFK приостанавливается при переключении в другое окно; вернись в игру для продолжения.\nПри неподтверждённом действии повторного клика не будет. Неизвестные тиры пропускаются. В режиме только экипировки требуются буквы Lv.\nРаспознавание проверено на твоих снимках; реальный перенос требует пробного запуска. Подробности — README.md.": "legacy.runtime_help",
}

_EXACT_MESSAGES = {
    "Интервал: от 60 до 86400 секунд (1 минута — 24 часа).": "error.interval",
    "Сумка обновляется. Повторю распознавание перед переносом.": "detail.preflight_waiting",
    "Полная проверка инвентаря пока не подтверждена. Повторю в следующем цикле.": "detail.inventory_pending",
    "Видимые предметы обработаны. Полная проверка продолжится в следующем цикле.": "detail.inventory_pending",
    "Все доступные страницы проверены дважды. Хранилище заполнено; сортировка и таймер остановлены.": "storage_full_stopped",
    "Проверка инвентаря не завершена. Работа остановлена.": "cycle_incomplete",
    "Сортирую оставшиеся предметы": "inventory_resorting",
    "Ожидаю доступа к игре.": "state.focus_waiting",
    "Ожидаю, пока игровые панели станут видны. Проверка повторится автоматически.": "state.auto_panels_waiting",
    "Не удалось распознать ячейки предметов. Проверка повторится автоматически.": "state.auto_grid_waiting",
    "Не удалось подтвердить положение игровых панелей. Текущая проверка отменена.": "error.layout_changed",
    "Размер или масштаб игры изменился. Текущая проверка отменена.": "error.layout_changed",
    "Размер окна игры изменился. Откройте игровые панели, выполните автонастройку и проверьте один перенос.": "error.resolution_changed",
    "Предметов, подходящих выбранным тирам, нет.": "detail.no_items",
    "Предметов, подходящих выбранным тирам и уровням, нет.": "detail.no_items",
    "Автонастройка готова. Проверьте один перенос по выбранным фильтрам.": "log.auto_ready",
    "Сетка сохранена. Проверь точность границ в предпросмотре; добавь образец пустой ячейки.": "log.grid_saved",
    "Windows не разрешила активировать прежнее окно после проверки.": "error.restore_window",
    "Не удалось прочитать профиль. Настрой программу заново.": "error.profile",
    "Таймер запущен. Можно вернуться к работе.": "detail.timer_started",
    "Пауза: ожидаю возвращение в игру или 10 минут без ввода.": "mode.wait_hint",
    "Вернитесь в игру; после 10 минут без ввода Helper сможет открыть её сам.": "mode.wait_hint",
    "Фоновая проверка прервана вводом пользователя. Следующая проверка по таймеру.": "detail.user_activity",
    "Пауза: ввод пользователя прервал фоновую проверку.": "detail.user_activity",
    "Подготовка игры к переносу.": "state.preparing",
    "Ожидаю завершения текущей проверки.": "detail.stopping",
    "Игровые панели открыты. Можно настроить распознавание.": "detail.panels_ready",
    "Проверяю и открываю панели игры.": "state.opening",
    "Распознаю предметы и проверяю хранилище.": "detail.working",
    "Подходящей распознанной экипировки и самоцветов T3+ нет.": "detail.no_items",
    "Подходящей распознанной экипировки T3+ нет.": "detail.no_items",
    "Подходящих предметов пока нет.": "detail.no_items",
    "Перенос подтверждён. Проверяю следующую вещь.": "detail.checking_next",
    "Окна персонажа и хранилища открыты и проверены.": "detail.panels_ready",
    "Один перенос подтверждён. Проверь его результат в игре перед AFK.": "detail.single_success",
    "Один перенос подтверждён.": "detail.single_success",
    "Ожидаю начала проверки игры.": "state.preparing",
    "Игра закрыта. Ожидаю её перезапуск.": "game.wait",
    "Игра снова найдена. Ожидаю следующую проверку.": "game.found",
    "Ожидаю активное окно игры.": "game.wait",
    "Ожидаю паузу в работе пользователя для фоновой проверки.": "state.idle_wait",
    "Окно игры активно: продолжаю.": "game.open",
    "Проверка закончена. Ожидаю следующий цикл.": "detail.check_complete",
    "Пауза: окно игры закрыто; ожидаю перезапуск.": "game.wait",
    "Пауза: активным должно быть окно игры.": "game.wait",
    "Пауза: фоновая проверка ждёт, пока пользователь не работает с мышью и клавиатурой.": "state.idle_wait",
    "Сначала выполни один перенос под наблюдением.": "detail.single_required",
    "Перенос остановлен. Можно запустить снова.": "detail.stopped",
    "Остановлено (F8 / кнопка Стоп).": "state.stopped",
    "Снимок сохранён.": "log.saved",
    "Проверка страниц завершена; исходная страница восстановлена.": "log.pages_done",
    "Размер окна изменился. Нужна новая настройка.": "error.window_size",
    "Интерфейс изменился или перекрыт: ориентир не совпал.": "error.overlay",
    "Состав сумки изменился во время распознавания. Перенос отменён.": "error.inventory_changed",
    "Хранилище изменилось во время распознавания. Перенос отменён.": "error.storage_changed",
    "Выбранная вещь изменилась. Перенос отменён.": "error.item_changed",
    "Кнопка сортировки не совпала с образцом. Клик отменён.": "error.sort",
    "На настроенных страницах нет распознанных свободных ячеек. Работа остановлена.": "error.pages_full",
}


def translate_message(message, *, locale=None):
    """Translate known UI/runtime messages; preserve unknown technical details.

    This is display-only. Callers must keep canonical messages for transfer
    verification, counters, logging and diagnostics instead of parsing output.
    """
    raw = str(message)
    text = raw.strip()
    key = _EXACT_MESSAGES.get(text) or LEGACY_TEXT_KEYS.get(text)
    if key:
        return tr(key, locale=locale)
    match = re.fullmatch(r"Окно игры активировано\. Первая проверка начинается сейчас; следующие — через ([\d.]+) секунд\. F8 — остановка\.", text)
    if match:
        return tr("log.first_started", locale=locale, time=format_duration(float(match[1]), locale=locale))
    if text.startswith("Выдели сетку: "):
        name = text.removeprefix("Выдели сетку: ")
        grid_key = {"inventory": "legacy.inventory", "storage": "legacy.storage"}.get(name)
        return tr("legacy.dialog_grid", locale=locale,
                  grid=tr(grid_key, locale=locale) if grid_key else name)
    if text.startswith("Окно игры активировано. Снимок через 5 секунд;"):
        return tr("log.capture_started", locale=locale, seconds=5)
    match = re.fullmatch(r"Окно игры активировано\. Подсказка ячейки (\d+) через 5 секунд — только наведение, без клика\. Оставайся в игре\.", text)
    if match:
        return tr("log.tooltip_started", locale=locale, cell=match[1], seconds=5)
    match = re.fullmatch(r"Подсказка ячейки (\d+) сохранена: (.+)", text, re.DOTALL)
    if match:
        return tr("log.tooltip_saved", locale=locale, cell=match[1], path=match[2])
    match = re.fullmatch(r"Снимок (\d+)×(\d+) готов\. Можно выделять сетки и добавлять образцы\.", text)
    if match:
        return tr("log.capture_ready", locale=locale, width=match[1], height=match[2])
    if text.startswith("Нативный снимок сохранён: "):
        return tr("log.native_saved", locale=locale, path=text.removeprefix("Нативный снимок сохранён: "))
    if text == "Проверка игровых панелей через 5 секунд. F8 — остановка.":
        return tr("log.panels_started", locale=locale, seconds=5)
    match = re.fullmatch(r"Сумка: (\{.*\}); неизвестных (\d+)\. Свободно на странице: (\d+)\. (.*)", text)
    if match:
        candidate_match = re.fullmatch(r"Кандидат: ячейка (\d+), T([1-6])\.", match[4])
        candidate = (tr("log.preview_candidate", locale=locale, cell=candidate_match[1], tier=candidate_match[2])
                     if candidate_match else tr("detail.no_items", locale=locale))
        return tr("log.preview", locale=locale, counts=match[1], unknown=match[2], free=match[3], candidate=candidate)
    match = re.fullmatch(r"Перенесён T([1-6]): сумка (\d+) → хранилище (\d+)\.", text)
    if match:
        return tr("log.transferred", locale=locale, tier=match[1], source=match[2], destination=match[3])
    match = re.fullmatch(r"Страница (\d+): свободно (\d+), (?:неопределённых ячеек|неизвестных) (\d+)\.", text)
    if match:
        return tr("log.pages", locale=locale, page=match[1], free=match[2], unknown=match[3])
    match = re.fullmatch(r"(?:Выбрана|Проверяю) страниц[ау] хранилища (\d+)\.", text)
    if match:
        return tr("log.page_selected", locale=locale, page=match[1])
    match = re.fullmatch(r"Добавить T([1-6])", text)
    if match:
        return tr("legacy.add_tier", locale=locale, tier=match[1])
    match = re.fullmatch(r"Следующая (?:проверка|сортировка) через (\d+) с\.", text)
    if match:
        return tr("interval.next", locale=locale, time=format_duration(int(match[1]), True, locale=locale))
    if text.startswith("Окно игры активировано. Старт через 5 секунд."):
        return tr("log.started", locale=locale, seconds=5)
    match = re.match(r"Таймер запущен: первая проверка через ([\d.]+) секунд", text)
    if match:
        return tr("interval.every", locale=locale, time=format_duration(float(match[1]), locale=locale))
    if text.startswith("Пауза: "):
        return tr("log.paused", locale=locale, reason=translate_message(text[7:], locale=locale))
    return raw
