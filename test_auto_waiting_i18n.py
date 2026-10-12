"""The automatic layout wait uses display translations, never log rewriting."""
import unittest
from unittest.mock import patch

import i18n
from runtime import LAYOUT_WAITING, FOCUS_WAITING


class AutomaticWaitingTranslationTests(unittest.TestCase):
    def test_focus_wait_maps_to_its_short_display_key(self):
        with patch.object(i18n, "tr", return_value="translated focus wait") as translate:
            self.assertEqual(i18n.translate_message(FOCUS_WAITING, locale="fr"), "translated focus wait")
        translate.assert_called_once_with("state.focus_waiting", locale="fr")

    def test_focus_wait_is_translated_in_every_supported_locale(self):
        for locale in i18n.SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                text = i18n.translate_message(FOCUS_WAITING, locale=locale)
                self.assertEqual(text, i18n.tr("state.focus_waiting", locale=locale))
                self.assertTrue(text.strip())
                if locale != "ru":
                    self.assertNotEqual(text, FOCUS_WAITING)

    def test_canonical_runtime_wait_has_an_exact_display_key(self):
        with patch.object(i18n, "tr", return_value="translated wait") as translate:
            self.assertEqual(i18n.translate_message(LAYOUT_WAITING, locale="de"), "translated wait")
        translate.assert_called_once_with("state.auto_panels_waiting", locale="de")

    def test_all_supported_locales_supply_the_automatic_wait_message(self):
        for locale in i18n.SUPPORTED_LOCALES:
            with self.subTest(locale=locale):
                self.assertEqual(i18n.translate_message(LAYOUT_WAITING, locale=locale),
                                 i18n.tr("state.auto_panels_waiting", locale=locale))
                self.assertTrue(i18n.tr("state.auto_panels_waiting", locale=locale).strip())
                if locale != "ru":
                    self.assertNotEqual(i18n.translate_message(LAYOUT_WAITING, locale=locale), LAYOUT_WAITING)


if __name__ == "__main__":
    unittest.main()
