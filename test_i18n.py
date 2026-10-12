"""Catalog and display behavior without Tk roots or desktop input."""
import json
from pathlib import Path
from string import Formatter
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import i18n


BASE = Path(__file__).parent


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((BASE / "locales.json").read_text(encoding="utf-8"))

    def test_all_official_interface_locales_have_complete_nonempty_catalogs(self):
        expected = {"en-US", "ko", "fr", "it", "de", "es", "ru", "vi", "uk",
                    "id", "ja", "zh-CN", "zh-TW", "th", "tr", "pl", "pt-BR"}
        self.assertEqual(set(i18n.LOCALE_NAMES), expected)
        catalogs = i18n.validate_catalog(self.data)
        self.assertGreaterEqual(len(catalogs["ru"]), 198)
        for locale in expected:
            with self.subTest(locale=locale):
                self.assertEqual(set(catalogs[locale]), set(catalogs["ru"]))

    def test_missing_selected_locale_key_is_rejected_instead_of_falling_back(self):
        del self.data["messages"]["ja"]["filters.unknown"]
        with self.assertRaisesRegex(ValueError, "ja"):
            i18n.validate_catalog(self.data)

    def test_changed_placeholder_is_rejected_before_display(self):
        self.data["messages"]["fr"]["interval.next"] = "Prochain contrôle dans {seconds}"
        with self.assertRaisesRegex(ValueError, "interval.next"):
            i18n.validate_catalog(self.data)

    def test_empty_selected_translation_is_rejected(self):
        self.data["messages"]["th"]["action.start"] = " "
        with self.assertRaisesRegex(ValueError, "th/action.start"):
            i18n.validate_catalog(self.data)

    def test_placeholder_cannot_access_attributes_or_apply_format_conversions(self):
        for value in ("{value.__class__}", "{value!r}", "{value:>8}"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                i18n._fields(value)

    def test_each_catalog_can_format_every_message_with_its_declared_arguments(self):
        for locale, catalog in i18n._catalog().items():
            for key, template in catalog.items():
                fields = {name for _, name, _, _ in Formatter().parse(template) if name}
                args = {name: "73" for name in fields}
                with self.subTest(locale=locale, key=key):
                    self.assertEqual(i18n.tr(key, locale=locale, **args), template.format(**args))

    def test_selected_locales_use_their_own_core_text(self):
        expected = {"ja": "言語", "ko": "언어", "zh-CN": "语言", "zh-TW": "語言",
                    "th": "ภาษา", "tr": "Dil", "uk": "Мова", "pt-BR": "Idioma",
                    "de": "Sprache", "fr": "Langue", "id": "Bahasa"}
        for locale, text in expected.items():
            with self.subTest(locale=locale):
                self.assertEqual(i18n.tr("settings.language", locale=locale), text)
                self.assertNotEqual(i18n.tr("app.title", locale=locale), i18n.tr("app.title", locale="en-US"))
                self.assertNotEqual(i18n.tr("app.title", locale=locale), i18n.tr("app.title", locale="ru"))

    def test_filters_do_not_hardcode_the_default_tier_selection(self):
        for locale in i18n.SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                self.assertNotIn("T3", i18n.tr("filters.items", locale=locale))
                self.assertTrue(i18n.tr("filters.tiers", locale=locale))

    def test_stopped_timer_text_requires_explicit_start_without_waiting_interval(self):
        for locale in i18n.SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                self.assertEqual(i18n._fields(self.data["messages"][locale]["interval.stopped"]), set())
                self.assertIn(i18n.tr("action.start", locale=locale), i18n.tr("interval.stopped", locale=locale))
                self.assertNotIn("{time}", i18n.tr("interval.stopped", locale=locale))

    def test_visible_filter_heading_only_names_tiers(self):
        self.assertEqual(i18n.tr("filters.tiers", locale="ru"), "Тиры")
        self.assertEqual(i18n.tr("filters.tiers", locale="en-US"), "Tiers")
        self.assertEqual(i18n.tr("filters.gems", locale="ru"), "Самоцветы: выбранные тиры")

    def test_short_transfer_stat_has_the_same_count_in_every_locale(self):
        for locale in i18n.SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                self.assertEqual(i18n._fields(self.data["messages"][locale]["stats.short"]), {"count"})
                self.assertIn("27", i18n.tr("stats.short", locale=locale, count=27))

    def test_installer_has_every_source_key_in_all_seventeen_locales(self):
        source = json.loads((BASE / "installer" / "strings_ru.json").read_text(encoding="utf-8"))
        extra = json.loads((BASE / "installer" / "installer_locales.json").read_text(encoding="utf-8"))["messages"]
        self.assertEqual(extra["ru"], source)
        for locale in i18n.SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                self.assertEqual(set(extra[locale]), set(source))
                self.assertTrue(all(text.strip() for text in extra[locale].values()))
                self.assertEqual(i18n.tr("installer.error_detail", locale=locale, error="E42"), "E42")
                self.assertEqual(i18n.tr("installer.title", locale=locale), extra[locale]["installer.title"])


class LocaleTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_locale("ru")

    def test_unknown_or_missing_os_locale_uses_russian(self):
        for value in (None, "", "sv-SE", "ar_SA.UTF-8", 42):
            with self.subTest(value=value):
                self.assertEqual(i18n.normalize_locale(value), "ru")

    def test_regional_and_steam_locale_aliases_preserve_supported_language(self):
        for source, result in {"de_DE.UTF-8": "de", "en_GB": "en-US", "pt_PT": "pt-BR",
                               "zh-Hant-HK": "zh-TW", "zh_CN": "zh-CN", "tchinese": "zh-TW",
                               "schinese": "zh-CN", "ko_KR": "ko", "ไทย": "th"}.items():
            with self.subTest(source=source):
                self.assertEqual(i18n.normalize_locale(source), result)

    def test_choice_names_select_their_own_locale(self):
        self.assertEqual(len(i18n.locale_choices()), 17)
        for code, name in i18n.locale_choices():
            with self.subTest(code=code):
                self.assertEqual(i18n.set_locale(name), code)
                self.assertEqual(i18n.get_locale(), code)

    def test_explicit_locale_does_not_change_current_ui_language(self):
        i18n.set_locale("ja")
        self.assertEqual(i18n.tr("settings.language", locale="de"), "Sprache")
        self.assertEqual(i18n.get_locale(), "ja")

    def test_unknown_key_and_missing_arguments_are_explicit_errors(self):
        with self.assertRaises(KeyError):
            i18n.tr("missing.product.key")
        with self.assertRaisesRegex(ValueError, "time"):
            i18n.tr("interval.next")


class DurationTests(unittest.TestCase):
    def test_interval_boundaries_and_hour_minute_combinations(self):
        for seconds, expected in ((1800, "30 мин"), (3600, "1 ч"), (5400, "1 ч 30 мин"), (86400, "24 ч")):
            with self.subTest(seconds=seconds):
                self.assertEqual(i18n.format_duration(seconds, locale="ru"), expected)

    def test_countdown_preserves_seconds_without_claiming_zero_early(self):
        self.assertEqual(i18n.format_duration(60.1, True, locale="ru"), "1 мин 1 с")
        self.assertEqual(i18n.format_duration(3661, True, locale="ru"), "1 ч 1 мин 1 с")
        self.assertEqual(i18n.format_duration(0.1, True, locale="ru"), "1 с")
        self.assertEqual(i18n.format_duration(-10, True, locale="ru"), "0 с")

    def test_units_follow_selected_language(self):
        self.assertEqual(i18n.format_duration(5400, locale="ja"), "1時間30分")
        self.assertEqual(i18n.format_duration(61, True, locale="ko"), "1분 1초")
        self.assertEqual(i18n.format_duration(1800, locale="zh-TW"), "30分鐘")

    def test_nonfinite_duration_is_rejected(self):
        for value in (float("inf"), float("nan"), -float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                i18n.format_duration(value)


class MessageTests(unittest.TestCase):
    def test_pending_inventory_cycle_is_localized_without_claiming_completion(self):
        canonical = "Видимые предметы обработаны. Полная проверка продолжится в следующем цикле."
        self.assertEqual(i18n.tr("detail.inventory_pending", locale="ru"), canonical)
        for locale in i18n.SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                translated = i18n.translate_message(canonical, locale=locale)
                self.assertEqual(translated, i18n.tr("detail.inventory_pending", locale=locale))
                self.assertNotEqual(translated, i18n.tr("detail.check_complete", locale=locale))
                self.assertEqual(i18n._fields(translated), set())
                if locale != "ru":
                    self.assertNotEqual(translated, canonical)

    def test_window_resize_action_is_translated_in_every_supported_locale(self):
        canonical = "Размер окна игры изменился. Откройте игровые панели, выполните автонастройку и проверьте один перенос."
        for locale in i18n.SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                localized = i18n.translate_message(canonical, locale=locale)
                self.assertEqual(localized, i18n.tr("error.resolution_changed", locale=locale))
                self.assertEqual(i18n._fields(localized), set())
                if locale != "ru":
                    self.assertNotEqual(localized, canonical)
        self.assertEqual(i18n.tr("error.resolution_changed", locale="ru"), canonical)

    def test_explicit_start_display_sorts_now_and_formats_next_interval(self):
        canonical = "Окно игры активировано. Первая проверка начинается сейчас; следующие — через 5400 секунд. F8 — остановка."
        for locale in i18n.SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                expected = i18n.tr("log.first_started", locale=locale, time=i18n.format_duration(5400, locale=locale))
                self.assertEqual(i18n.translate_message(canonical, locale=locale), expected)

    def test_transfer_display_preserves_actual_tier_and_cell_numbers(self):
        canonical = "Перенесён T6: сумка 4 → хранилище 27."
        self.assertEqual(i18n.translate_message(canonical, locale="en-US"),
                         "Transferred T6: inventory 4 → storage 27.")
        self.assertEqual(canonical, "Перенесён T6: сумка 4 → хранилище 27.")

    def test_pause_and_no_candidate_messages_use_selected_language(self):
        self.assertEqual(i18n.translate_message("Предметов, подходящих выбранным тирам и уровням, нет.", locale="ja"),
                         i18n.tr("detail.no_items", locale="ja"))
        self.assertEqual(i18n.translate_message("Пауза: ввод пользователя прервал фоновую проверку.", locale="de"),
                         i18n.tr("detail.user_activity", locale="de"))

    def test_dynamic_calibration_dialogs_localize_the_grid_name(self):
        text = i18n.translate_message("Выдели сетку: inventory", locale="fr")
        self.assertEqual(text, i18n.tr("legacy.dialog_grid", locale="fr", grid=i18n.tr("legacy.inventory", locale="fr")))
        self.assertEqual(i18n.translate_message("Добавить T6", locale="ja"), "T6を追加")

    def test_paths_and_preview_numbers_are_preserved_as_data(self):
        path = "C:\\Example\\résultat.png"
        self.assertIn(path, i18n.translate_message("Нативный снимок сохранён: " + path, locale="ko"))
        text = i18n.translate_message("Сумка: {3: 5, 6: 1}; неизвестных 2. Свободно на странице: 7. Кандидат: ячейка 4, T6.", locale="en-US")
        self.assertIn("{3: 5, 6: 1}", text)
        self.assertIn("cell 4, T6", text)

    def test_unknown_technical_diagnostics_are_preserved(self):
        text = "Win32 diagnostic: HWND=0x1234, native error 5"
        self.assertEqual(i18n.translate_message(text, locale="th"), text)


class LegacyDisplayTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_locale("ru")

    def test_legacy_log_keeps_canonical_file_record_but_localizes_widgets(self):
        from app import App
        i18n.set_locale("ja")
        fake = SimpleNamespace(status=Mock(), log_widget=Mock())
        canonical = "Перенесён T6: сумка 4 → хранилище 27."
        with patch("app.logging.info") as log:
            App.log(fake, canonical)
        log.assert_called_once_with(canonical)
        fake.status.set.assert_called_once_with(i18n.translate_message(canonical))
        self.assertIn(i18n.translate_message(canonical), fake.log_widget.insert.call_args.args[1])

    def test_cell_number_dialog_is_localized_without_game_input(self):
        from app import App
        i18n.set_locale("ko")
        grid = Mock(count=40)
        fake = SimpleNamespace(require_snapshot=Mock(), grid=Mock(return_value=grid))
        with patch("app.simpledialog.askinteger", return_value=None) as ask:
            self.assertIsNone(App.cell(fake, "inventory"))
        self.assertEqual(ask.call_args.args, (i18n.tr("legacy.dialog_cell_title"), i18n.tr("legacy.dialog_cell_help")))
        grid.cell_rect.assert_not_called()

    def test_unknown_exception_uses_localized_generic_dialog_and_full_log(self):
        from app import App
        i18n.set_locale("th")
        fake = SimpleNamespace(worker=None)
        error = RuntimeError("native diagnostic 0x12")
        with patch("app.messagebox.showerror") as dialog, patch("app.logging.exception") as log:
            App.safe(fake, Mock(side_effect=error))
        self.assertNotIn("native diagnostic", dialog.call_args.args[1])
        self.assertIn(i18n.tr("detail.see_journal"), dialog.call_args.args[1])
        self.assertIs(log.call_args.args[1], error)


if __name__ == "__main__":
    unittest.main()
