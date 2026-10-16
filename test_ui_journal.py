"""Journal display/data contracts without creating Tk windows or game input."""
from pathlib import Path
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock, patch

import app
from dashboard import ProductApp
from i18n import SUPPORTED_LOCALES, set_locale, tr, validate_catalog
from ui_journal import EventJournal, JournalEntry, at_tail, copy_entries, filtered_entries, wrap_lines


class JournalRecordsTests(unittest.TestCase):
    def tearDown(self):
        set_locale("ru")

    def test_only_verified_transfer_messages_get_transfer_badges(self):
        for tier in range(1, 7):
            entry = JournalEntry.from_record(0, "09:42:16 ", f"Перенесён T{tier}: сумка 12 → хранилище 9.")
            self.assertEqual((entry.kind, entry.tier, entry.stamp), ("transfer", str(tier), "09:42:16"))
        for raw in ("Перенесён T7: сумка 12 → хранилище 9.", "Кандидат: T3",
                    "Перенесён T3: сумка 12 → хранилище 9. Не подтверждён."):
            self.assertNotEqual(JournalEntry.from_record(0, "09:42:16", raw).kind, "transfer")

    def test_original_diagnostics_remain_intact_and_are_searchable_but_not_in_summary(self):
        raw = "Traceback\nCapture RMSE=23.478 PID=4055 HWND=9013\nC:\\private\\player\\settings.json"
        entry = JournalEntry.from_record(0, "09:42:16", raw)
        self.assertEqual(entry.kind, "activity")
        self.assertEqual(entry.raw, raw)
        self.assertTrue(entry.matches(query="PLAYER"))
        for locale in SUPPORTED_LOCALES:
            summary = entry.summary(locale=locale)
            self.assertEqual(summary, tr("journal.diagnostic", locale=locale))
            self.assertNotIn("4055", summary)
            self.assertNotIn("private", copy_entries([entry], locale=locale))

    def test_known_russian_message_is_readable_even_when_translation_equals_source(self):
        entry = JournalEntry.from_record(0, "09:42:16", "Проверка закончена. Ожидаю следующий цикл.")
        self.assertEqual(entry.summary(locale="ru"), tr("detail.check_complete", locale="ru"))
        self.assertNotEqual(entry.summary(locale="ru"), tr("journal.diagnostic", locale="ru"))

    def test_pending_inventory_cycle_is_a_localized_waiting_event(self):
        for canonical in ("Видимые предметы обработаны. Полная проверка продолжится в следующем цикле.",
                          "Полная проверка инвентаря пока не подтверждена. Повторю в следующем цикле."):
            entry = JournalEntry.from_record(4, "09:42:16", canonical)
            self.assertEqual(entry.kind, "waiting")
            self.assertIsNone(entry.tier)
            self.assertEqual(entry.raw, canonical)
            for locale in SUPPORTED_LOCALES:
                with self.subTest(canonical=canonical, locale=locale):
                    self.assertEqual(entry.summary(locale=locale), tr("detail.inventory_pending", locale=locale))
                    self.assertEqual(entry.label(locale=locale), tr("journal.kind.waiting", locale=locale))
                    self.assertEqual(filtered_entries([entry], "attention", locale=locale), [entry])
                    self.assertEqual(filtered_entries([entry], "transfer", locale=locale), [])
            fabricated = JournalEntry.from_record(5, "09:42:17", canonical + " native diagnostic")
            self.assertNotEqual(fabricated.kind, "waiting")

    def test_unconfirmed_transfer_is_visible_as_attention_with_localized_summary(self):
        raw = ("Перенос не подтверждён; повторных кликов не будет. "
               "inventory tier count did not decrease by exactly one")
        entry = JournalEntry.from_record(14, "14:14:33", raw)
        self.assertEqual(entry.kind, "attention")
        self.assertIsNone(entry.tier)
        self.assertEqual(entry.raw, raw)
        for locale in SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                summary = entry.summary(locale=locale)
                self.assertEqual(summary, tr("journal.transfer_unconfirmed", locale=locale))
                self.assertNotEqual(summary, tr("journal.diagnostic", locale=locale))
                self.assertNotIn("inventory tier count", copy_entries([entry], locale=locale))
                self.assertEqual(entry.label(locale=locale), tr("journal.kind.attention", locale=locale))
                self.assertEqual(filtered_entries([entry], "attention", locale=locale), [entry])
                self.assertEqual(filtered_entries([entry], "transfer", locale=locale), [])
                self.assertTrue(entry.matches("attention", "exactly one", locale=locale))

    def test_unconfirmed_transfer_prefix_does_not_turn_normal_waits_into_errors(self):
        records = [
            ("06:51:33", "Ожидаю, пока игровые панели станут видны. Проверка повторится автоматически."),
            ("14:14:33", "Перенос не подтверждён; повторных кликов не будет. "
                         "inventory tier count did not decrease by exactly one"),
            ("15:46:44", "Вернитесь в игру; после 10 минут без ввода Helper сможет открыть её сам."),
        ]
        entries = [JournalEntry.from_record(index, stamp, raw)
                   for index, (stamp, raw) in enumerate(records)]
        self.assertEqual([entry.kind for entry in entries], ["waiting", "attention", "waiting"])
        for locale in SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                self.assertEqual(filtered_entries(entries, "attention", locale=locale), entries)
                self.assertEqual(entries[0].summary(locale=locale), tr("state.auto_panels_waiting", locale=locale))
                self.assertEqual(entries[2].summary(locale=locale), tr("mode.wait_hint", locale=locale))
        prefix = "Перенос не подтверждён; повторных кликов не будет."
        for raw in (prefix, prefix + "\nnew native diagnostic"):
            self.assertEqual(JournalEntry.from_record(0, "14:14:33", raw).kind, "attention")
        for raw in (prefix + "fragment", "Служебная запись: " + prefix):
            self.assertEqual(JournalEntry.from_record(0, "14:14:33", raw).kind, "activity")

    def test_transfers_are_localized_without_technical_cell_numbers(self):
        entry = JournalEntry.from_record(0, "09:42:16", "Перенесён T5: сумка 431 → хранилище 212.")
        for locale in SUPPORTED_LOCALES:
            summary = entry.summary(locale=locale)
            self.assertEqual(summary, tr("journal.transfer_summary", locale=locale, tier=5))
            self.assertNotIn("431", summary)
            self.assertNotIn("212", summary)

    def test_attention_filter_includes_waits_and_failed_operations(self):
        records = ["Перенесён T3: сумка 1 → хранилище 2.",
                   "Пауза: ввод пользователя прервал фоновую проверку.",
                   "Кнопка сортировки не совпала с образцом. Клик отменён.",
                   "Проверка закончена. Ожидаю следующий цикл."]
        entries = [JournalEntry.from_record(i, "09:42:16", raw) for i, raw in enumerate(records)]
        self.assertEqual([e.index for e in filtered_entries(entries, "transfer")], [0])
        self.assertEqual([e.index for e in filtered_entries(entries, "attention")], [1, 2])
        self.assertEqual([e.index for e in filtered_entries(entries)], [0, 1, 2, 3])

    def test_search_matches_localized_summary_timestamp_and_raw_case_insensitively(self):
        entry = JournalEntry.from_record(0, "09:42:16", "Перенесён T4: сумка 1 → хранилище 2.")
        for query in ("  T4  ", "09:42", "ПЕРЕНЕСЁН", "хранилище"):
            self.assertTrue(entry.matches(query=query, locale="ru"))
        self.assertTrue(entry.matches(query="storage", locale="en-US"))
        self.assertFalse(entry.matches("attention", "storage", locale="en-US"))
        self.assertFalse(entry.matches(query="not-an-event", locale="en-US"))

    def test_copy_preserves_filter_order_and_timestamps_without_mutating_records(self):
        entries = [JournalEntry.from_record(i, f"09:42:{16+i}", f"Перенесён T3: сумка {i+1} → хранилище 2.")
                   for i in range(3)]
        before = tuple(entries)
        copied = copy_entries(entries, locale="en-US")
        self.assertEqual([line.split()[0] for line in copied.splitlines()], ["09:42:16", "09:42:17", "09:42:18"])
        self.assertEqual(tuple(entries), before)
        self.assertEqual(copy_entries([]), "")

    def test_log_content_is_plain_data_with_no_html_or_code_interpretation(self):
        raw = '<script>require("fs").unlinkSync("player.db")</script>'
        entry = JournalEntry.from_record(0, "09:42:16", raw)
        self.assertEqual(entry.raw, raw)
        self.assertTrue(entry.matches(query="unlinkSync"))
        self.assertEqual(entry.summary(locale="en-US"), tr("journal.diagnostic", locale="en-US"))


class JournalInteractionTests(unittest.TestCase):
    def test_unconfirmed_transfer_details_preserve_original_diagnostic(self):
        raw = ("Перенос не подтверждён; повторных кликов не будет. "
               "inventory tier count did not decrease by exactly one")
        state = SimpleNamespace(_selected=JournalEntry.from_record(14, "14:14:33", raw),
                                _details_open=True, details_button=Mock(), _notice=Mock(), details_text=Mock())
        EventJournal._update_selection(state)
        state.details_text.insert.assert_called_once_with("end", "14:14:33  " + raw)
        state.details_text.configure.assert_called_with(state="disabled")

    def test_reading_old_events_does_not_resume_tail_following_on_append(self):
        state = SimpleNamespace(_closed=False, _entries=[], _visible=[], _category="all",
                                _query=Mock(get=Mock(return_value="")), _follow_tail=False,
                                _new_count=0, _request_render=Mock())
        EventJournal.append(state, "09:42:16", "Перенесён T3: сумка 1 → хранилище 2.")
        EventJournal.append(state, "09:42:17", "Перенесён T4: сумка 1 → хранилище 3.")
        self.assertFalse(state._follow_tail)
        self.assertEqual(state._new_count, 2)
        self.assertEqual([e.index for e in state._visible], [0, 1])

    def test_filtered_out_append_preserves_log_and_does_not_show_new_event_notice(self):
        state = SimpleNamespace(_closed=False, _entries=[], _visible=[], _category="transfer",
                                _query=Mock(get=Mock(return_value="")), _follow_tail=False,
                                _new_count=0, _request_render=Mock())
        raw = "Кнопка сортировки не совпала с образцом. Клик отменён."
        EventJournal.append(state, "09:42:16", raw)
        self.assertEqual(len(state._entries), 1)
        self.assertEqual(state._entries[0].raw, raw)
        self.assertEqual(state._visible, [])
        self.assertEqual(state._new_count, 0)

    def test_closed_window_cannot_accept_late_worker_append(self):
        state = SimpleNamespace(_closed=True, _entries=[], _request_render=Mock())
        EventJournal.append(state, "09:42:16", "new event")
        self.assertEqual(state._entries, [])
        state._request_render.assert_not_called()

    def test_user_scroll_is_the_only_action_that_switches_to_reading_old_records(self):
        state = SimpleNamespace(canvas=Mock(), _follow_tail=True, _new_count=2, _request_render=Mock())
        state.canvas.yview.return_value = (0.2, 0.6)
        EventJournal._scroll(state, "moveto", 0.2)
        self.assertFalse(state._follow_tail)
        self.assertEqual(state._new_count, 2)
        state.canvas.yview.return_value = (0.7, 1.0)
        EventJournal._scroll(state, "moveto", 1.0)
        self.assertTrue(state._follow_tail)
        self.assertEqual(state._new_count, 0)

    def test_destroy_cancels_all_pending_callbacks_and_search_trace_once(self):
        state = SimpleNamespace(_closed=False, _timers={"render", "caption"}, _render_token="render",
                                after_cancel=Mock(), _query=Mock(), _search_trace="trace")
        EventJournal._cleanup(state)
        EventJournal._cleanup(state)
        self.assertTrue(state._closed)
        self.assertEqual(state._timers, set())
        self.assertIsNone(state._render_token)
        self.assertEqual({call.args[0] for call in state.after_cancel.call_args_list}, {"render", "caption"})
        state._query.trace_remove.assert_called_once_with("write", "trace")

    def test_delayed_callback_after_destroy_does_not_touch_tk_widgets(self):
        state = SimpleNamespace(_closed=False, _timers=set(), after=Mock(return_value="callback"))
        callback = Mock()
        EventJournal._schedule(state, 150, callback)
        scheduled = state.after.call_args.args[1]
        state._closed = True
        scheduled()
        callback.assert_not_called()

    def test_dashboard_keeps_existing_file_log_and_original_records(self):
        journal = Mock(winfo_exists=Mock(return_value=True))
        state = SimpleNamespace(_confirmed_count=0, _journal=journal, _journal_records=[], _dashboard_ready=False)
        raw = "Перенесён T3: сумка 1 → хранилище 2."
        with patch.object(app.App, "log") as original_log, patch("dashboard.time.strftime", return_value="09:42:16 "):
            ProductApp.log(state, raw)
        original_log.assert_called_once_with(state, raw)
        journal.append.assert_called_once_with("09:42:16 ", raw)
        self.assertEqual(state._journal_records, [("09:42:16 ", raw)])
        self.assertEqual(state._confirmed_count, 1)


class JournalTextTests(unittest.TestCase):
    def test_long_chinese_text_wraps_without_spaces_and_keeps_visible_lines_bounded(self):
        lines = wrap_lines("这里没有空格但所有文字都应保持在卡片范围内", 8, len)
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(len(line) <= 8 for line in lines))
        self.assertTrue(lines[-1].endswith("…"))

    def test_whole_short_message_and_word_boundaries_are_preserved(self):
        self.assertEqual(wrap_lines("Transfer confirmed", 30, len), ["Transfer confirmed"])
        self.assertEqual(wrap_lines("Transfer confirmed next item", 19, len), ["Transfer confirmed", "next item"])
        self.assertEqual(wrap_lines("", 1, len), [])

    def test_empty_or_one_screen_journal_is_considered_at_tail(self):
        self.assertTrue(at_tail((0.0, 1.0)))
        self.assertTrue(at_tail((0.83, 1.0)))
        self.assertFalse(at_tail((0.1, 0.8)))
        self.assertFalse(at_tail((0.997, 0.998)))
        self.assertFalse(at_tail(()))

    def test_journal_and_full_storage_controls_have_real_translations_in_all_locales(self):
        data = json.loads(Path(__file__).with_name("locales.json").read_text(encoding="utf-8"))
        catalogs = validate_catalog(data)
        keys = [key for key in catalogs["ru"] if key.startswith("journal.")]
        keys += ["storage_full_title", "storage_full_body", "storage_space_available", "storage_rechecking",
                 "cycle_incomplete", "inventory_resorting", "storage_full_stopped", "detail.inventory_pending"]
        for locale in SUPPORTED_LOCALES:
            for key in keys:
                with self.subTest(locale=locale, key=key):
                    self.assertTrue(tr(key, locale=locale, tier=3, count=42, total=57).strip())
                    if locale not in {"ru", "uk"}:
                        self.assertNotEqual(catalogs[locale][key], catalogs["ru"][key])


if __name__ == "__main__":
    unittest.main()
