"""Real timing/receipt boundaries with injected clocks; no game or network input."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from PIL import Image, ImageDraw

from core import Rect
from connection_vision import DisconnectRecognizer, DisconnectProof, modal_candidates, text_matches
from connection_runtime import ConnectionMonitor
from reconnection import ConnectionLost, ReconnectPolicy, RETRY_SECONDS, TRANSFER_INTERRUPTED


def modal(*, text_colour=(60, 60, 60)):
    image = Image.new('RGB', (410, 335), 'black')
    draw = ImageDraw.Draw(image)
    draw.rectangle((8, 5, 392, 310), fill=(66, 63, 57))
    draw.rectangle((8, 59, 392, 310), fill=(214, 210, 198))
    draw.rectangle((30, 260, 175, 298), fill=(137, 121, 121))
    draw.rectangle((215, 260, 365, 298), fill=(99, 131, 153))
    return image


class ReconnectPolicyTests(unittest.TestCase):
    def test_first_attempt_and_retries_are_five_minutes_apart(self):
        policy = ReconnectPolicy()
        self.assertTrue(policy.lost(100))
        self.assertEqual(policy.next_attempt_at, 400)
        self.assertFalse(policy.due(399.999))
        self.assertTrue(policy.due(400))
        policy.issued(400)
        self.assertEqual((policy.attempts, policy.next_attempt_at), (1, 700))
        self.assertFalse(policy.due(699.999))
        self.assertFalse(policy.lost(650))
        self.assertEqual(policy.next_attempt_at, 700)

    def test_new_outage_gets_its_own_deadline(self):
        policy = ReconnectPolicy()
        policy.lost(0)
        policy.issued(300)
        self.assertTrue(policy.restored())
        self.assertFalse(policy.due(900))
        policy.lost(1000)
        self.assertEqual((policy.attempts, policy.next_attempt_at), (0, 1300))

    def test_cannot_reserve_early_or_with_nonfinite_clock(self):
        policy = ReconnectPolicy()
        for value in (True, float('nan'), float('inf'), '0'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                policy.lost(value)
        policy.lost(0)
        with self.assertRaises(ValueError):
            policy.issued(299)


class DisconnectVisionTests(unittest.TestCase):
    def setUp(self):
        self.messages = json.loads((Path(__file__).parent/'disconnect_messages.json').read_bytes())['messages']

    def detector(self, locale, *, body=None, title=None, confidence=95):
        model = self.messages[locale]
        def read(image, language):
            if language != model['ocr']:
                return 'unrelated game text', 95
            return ((title or model['title']) if image.height < 70 else
                    (model['body'] if body is None else body)), confidence
        return DisconnectRecognizer(read_text=read)

    def test_authentic_message_catalog_covers_all_game_locales(self):
        from i18n import SUPPORTED_LOCALES
        self.assertEqual(set(self.messages), set(SUPPORTED_LOCALES))
        for locale in self.messages:
            with self.subTest(locale=locale):
                proof = self.detector(locale).detect(modal())
                self.assertEqual(proof.locale, locale)
                self.assertTrue(proof.button.left > proof.rect.center[0])

    def test_generic_confirm_or_title_alone_cannot_authorize_click(self):
        for body in ('Confirm', 'Delete this item?', 'Buy a storage page?', ''):
            with self.subTest(body=body):
                self.assertIsNone(self.detector('en-US', body=body).detect(modal()))
        self.assertIsNone(self.detector('en-US', title='Delete item').detect(modal()))

    def test_low_confidence_and_partial_message_rejected(self):
        self.assertIsNone(self.detector('ru', confidence=69).detect(modal()))
        self.assertFalse(text_matches('Connection lost', self.messages['en-US']['body'], 99))

    def test_missing_cancel_or_blue_button_rejected_before_ocr(self):
        for bounds in ((30,260,175,298), (215,260,365,298)):
            image = modal()
            ImageDraw.Draw(image).rectangle(bounds, fill=(214,210,198))
            read = Mock(side_effect=AssertionError('incomplete modal must not use OCR'))
            self.assertIsNone(DisconnectRecognizer(read_text=read).detect(image))
            read.assert_not_called()

    def test_ambiguous_duplicate_dialog_rejected(self):
        image = Image.new('RGB', (840,350), 'black')
        image.paste(modal(), (0,0)); image.paste(modal(), (420,0))
        self.assertIsNone(self.detector('en-US').detect(image))

    def test_inventory_like_gray_cells_do_not_trigger_ocr(self):
        image = Image.new('RGB', (600,600), 'black')
        draw = ImageDraw.Draw(image)
        for y in range(0,600,60):
            for x in range(0,600,60):
                draw.rectangle((x,y,x+55,y+55), fill=(214,210,198))
        self.assertEqual(modal_candidates(image), ())

    def test_reward_title_and_both_labels_are_required_in_every_locale(self):
        image = Image.new('RGB',(1100,760),'black')
        draw = ImageDraw.Draw(image)
        draw.rectangle((601,437,1068,720),fill=(214,210,198))
        draw.rectangle((728,676,943,716),fill=(99,131,153))
        for locale,model in self.messages.items():
            with self.subTest(locale=locale):
                expected = iter((model['reward_title'], model['reward_last_login'], model['reward_duration']))
                read = lambda im,lang: (next(expected),95) if lang == model['ocr'] else ('unrelated',95)
                proof = DisconnectRecognizer(read_text=read).detect_reward(image)
                self.assertEqual(proof.locale,locale)
                self.assertEqual(image.getpixel(proof.point),(99,131,153))
        for missing in (1,2):
            texts = [self.messages['en-US'][k] for k in ('reward_title','reward_last_login','reward_duration')]
            texts[missing] = 'unrelated'
            expected = iter(texts)
            read = lambda im,lang: (next(expected),95) if lang == 'eng' else ('unrelated',95)
            self.assertIsNone(DisconnectRecognizer(read_text=read).detect_reward(image))


class ConnectionMonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.image = Image.new('RGB', (900,700), 'black')
        self.proof = DisconnectProof(Rect(10,10,390,310), Rect(210,260,360,300),
                                     'ru', self.image.size, 'one-modal')
        self.recognizer = SimpleNamespace(detect=Mock(return_value=self.proof),
                                          detect_reward=Mock(return_value=None), close=Mock())
        self.runner = SimpleNamespace(
            desktop=SimpleNamespace(capture=Mock(return_value=self.image), active=Mock(return_value=True),
                                    click=Mock(), idle_seconds=Mock(return_value=0)),
            handle=5, check_stop=Mock(), log=Mock(), state=Mock(),
            panel_controller=SimpleNamespace(locate=Mock(return_value=[Rect(1,1,20,20), None])),
            reset_after_reconnection=Mock(), _transfer_input_issued=False,
            _panel_open_input_pending=Mock(return_value=False))
        self.runner.wait = self.wait
        self.monitor = ConnectionMonitor(self.runner, self.recognizer, clock=lambda: self.now,
                                         settle_seconds=0)

    def wait(self, seconds):
        self.now += seconds

    def test_no_click_until_due_then_only_one_fresh_proved_click(self):
        self.assertFalse(self.monitor.step())
        self.now = 299
        self.assertFalse(self.monitor.step())
        self.runner.desktop.click.assert_not_called()
        self.now = 300
        self.assertFalse(self.monitor.step())
        self.runner.desktop.click.assert_called_once_with(5, self.proof.point, self.image.size)
        self.assertEqual(self.monitor.policy.next_attempt_at, 600.15)
        self.monitor.step()
        self.runner.desktop.click.assert_called_once()

    def test_changed_dialog_before_click_is_not_accepted(self):
        self.monitor.policy.lost(0)
        self.now = 300
        self.recognizer.detect.side_effect = [self.proof, None]
        self.assertFalse(self.monitor.step())
        self.runner.desktop.click.assert_not_called()
        self.assertEqual(self.monitor.policy.attempts, 0)

    def test_native_error_after_attempt_reserves_cooldown(self):
        self.monitor.policy.lost(0)
        self.now = 300
        self.runner.desktop.click.side_effect = RuntimeError('unknown native receipt')
        with self.assertRaisesRegex(RuntimeError, 'unknown native receipt'):
            self.monitor.step()
        self.assertFalse(self.monitor.policy.due(301))
        self.assertEqual(self.monitor.policy.attempts, 1)

    def test_stop_before_second_capture_prevents_input(self):
        self.monitor.policy.lost(0)
        self.now = 300
        self.runner.wait = Mock(side_effect=InterruptedError('F8'))
        with self.assertRaises(InterruptedError): self.monitor.step()
        self.runner.desktop.click.assert_not_called()

    def test_recovery_requires_two_positive_game_frames_and_new_baseline(self):
        self.monitor.policy.lost(0)
        self.monitor.cycle_deadline = 3600
        self.runner._transfer_input_issued = True
        self.recognizer.detect.return_value = None
        self.assertTrue(self.monitor.step())
        self.runner.reset_after_reconnection.assert_called_once_with()
        self.runner.log.assert_any_call(TRANSFER_INTERRUPTED)
        self.assertEqual(self.monitor.sort_wait_remaining(), 0)
        self.assertFalse(self.monitor.policy.disconnected)
        self.runner.desktop.click.assert_not_called()

    def test_unknown_screen_is_not_success_or_a_click_target(self):
        self.monitor.policy.lost(0)
        self.recognizer.detect.return_value = None
        self.monitor._gameplay_visible = Mock(return_value=False)
        self.assertFalse(self.monitor.step())
        self.assertTrue(self.monitor.policy.disconnected)
        self.runner.reset_after_reconnection.assert_not_called()
        self.runner.desktop.click.assert_not_called()

    def test_offline_reward_is_closed_once_before_resuming_inventory(self):
        self.recognizer.detect.return_value = None
        self.recognizer.detect_reward.return_value = self.proof
        self.assertFalse(self.monitor.step())
        self.runner.desktop.click.assert_called_once_with(5,self.proof.point,self.image.size)
        self.assertFalse(self.monitor.step())
        self.runner.desktop.click.assert_called_once()
        self.runner.reset_after_reconnection.assert_not_called()
        self.recognizer.detect_reward.return_value = None
        self.assertTrue(self.monitor.step())
        self.runner.reset_after_reconnection.assert_called_once()

    def test_three_second_settling_prevents_early_success_before_rewards_arrive(self):
        self.monitor.settle_seconds = 3
        self.monitor.policy.lost(0)
        self.recognizer.detect.return_value = None
        self.assertFalse(self.monitor.step())
        self.now = 2.99
        self.assertFalse(self.monitor.step())
        self.now = 3
        self.assertTrue(self.monitor.step())
        self.runner.reset_after_reconnection.assert_called_once()

    def test_reward_with_changed_fresh_proof_cannot_be_clicked(self):
        self.recognizer.detect.return_value = None
        self.recognizer.detect_reward.side_effect = [self.proof,None]
        self.assertFalse(self.monitor.step())
        self.runner.desktop.click.assert_not_called()
        self.assertFalse(self.monitor.reward_input_issued)

    def test_hour_countdown_notices_outage_after_ten_seconds(self):
        self.monitor.wait_between_cycles(3600)
        self.assertEqual(self.now, 10)
        self.assertTrue(self.monitor.policy.disconnected)
        self.assertEqual(self.monitor.cycle_deadline, 3600)
        self.runner.desktop.click.assert_not_called()

    def test_active_user_in_other_app_is_never_captured_or_activated(self):
        self.runner.desktop.active.return_value = False
        self.monitor.wait_between_cycles(70)
        self.assertEqual(self.now, 70)
        self.runner.desktop.capture.assert_not_called()
        self.runner.desktop.click.assert_not_called()

    def test_idle_background_health_probe_preserves_sort_deadline(self):
        self.runner.desktop.active.return_value = False
        self.runner.desktop.idle_seconds.return_value = 600
        self.monitor.wait_between_cycles(3600)
        self.assertEqual(self.now, 60)
        self.assertEqual(self.monitor.sort_wait_remaining(), 3540)
        self.runner.desktop.capture.assert_not_called()
        self.runner.desktop.click.assert_not_called()

    def test_in_cycle_disconnect_uses_distinct_exception(self):
        with self.assertRaises(ConnectionLost): self.monitor.guard_frame(self.image)
        self.runner.desktop.click.assert_not_called()


if __name__ == '__main__':
    unittest.main()
