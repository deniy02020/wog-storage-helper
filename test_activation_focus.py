"""Neutral-heading activation with injected fake Win32 and fake screen frames."""
import ctypes
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from core import Rect
from panels import ActivationTarget
from input_activity import ActivityState, HELPER_INPUT_TAG, UserActivityInterrupted
from test_start import FakeFocusUser32
from test_windows import WindowTestBoundary, FakeUser32, Input
from windows import FocusUnavailable, WindowGeometryChanged


class ActivationUser32(FakeFocusUser32):
    def __init__(self):
        super().__init__()
        self.allow_activation = False
        self.activation_return = False
        self.physical_target = self.CHILD
        self.on_physical_hit = lambda: None
        self.physical_calls = 0
        self.metrics = {76: -1920, 77: -600, 78: 5760, 79: 2160}
        self.raw_last_input = 10000

    def IsWindow(self, handle):
        return self.exists and handle in (self.GAME, self.OVERLAY)

    def WindowFromPhysicalPoint(self, point):
        self.physical_calls += 1
        self.on_physical_hit()
        return self.physical_target

    def GetSystemMetrics(self, index):
        return self.metrics[index]


class ActivationFocusTests(WindowTestBoundary, unittest.TestCase):
    def setup_activation(self):
        desktop = self.desktop([FakeUser32.CHILD])
        desktop.u = ActivationUser32()
        desktop.input_observer = ActivityState(600, clock=self.clock.monotonic)
        desktop.input_observer.ensure_alive = Mock()
        desktop._background_token = None
        desktop.k.GetTickCount64 = Mock(return_value=610000)
        def last_input(pointer):
            pointer._obj.dwTime = desktop.u.raw_last_input
            return True
        desktop.u.GetLastInputInfo = Mock(side_effect=last_input)
        desktop.events = []
        desktop.on_down = lambda: setattr(desktop.u, "foreground", desktop.u.GAME)
        desktop.on_up = lambda: None
        desktop.on_move = lambda: None
        desktop.refuse_up = False
        def send(count, pointer, size):
            event = ctypes.cast(pointer, ctypes.POINTER(Input)).contents.mi
            desktop.events.append((event.dwFlags, event.dwExtraInfo))
            desktop.input_observer.record(event.dwExtraInfo)
            desktop.u.raw_last_input = 610000
            if event.dwFlags & 0x8000:
                desktop.u.cursor = (int(event.dx * desktop.u.metrics[78] / 65536) + desktop.u.metrics[76],
                                    int(event.dy * desktop.u.metrics[79] / 65536) + desktop.u.metrics[77])
                desktop.on_move()
            elif event.dwFlags == 2:
                desktop.on_down()
            elif event.dwFlags == 4:
                desktop.on_up()
                if desktop.refuse_up:
                    return 0
            return count
        desktop.u.SendInput = send
        self.captures = []
        self.on_capture = lambda: None
        self.on_frame = lambda _image, _index: None
        def grab(*, bbox, all_screens):
            self.assertTrue(all_screens)
            self.captures.append(bbox)
            self.on_capture()
            frame = Image.new("RGB", (bbox[2] - bbox[0], bbox[3] - bbox[1]), (31, 25, 42))
            self.on_frame(frame, len(self.captures))
            return frame
        capture_patch = patch("PIL.ImageGrab.grab", side_effect=grab)
        capture_patch.start(); self.addCleanup(capture_patch.stop)
        desktop.guard = Mock()
        desktop.target = ActivationTarget(Rect(100, 40, 200, 80), "storage")
        desktop.reader = Mock(side_effect=lambda image: desktop.target)
        return desktop

    def begin(self, desktop):
        return desktop.begin_background_cycle(desktop.u.GAME, activation_target=desktop.reader, activation_guard=desktop.guard)

    def flags(self, desktop):
        return [flags for flags, _ in desktop.events]

    def assert_no_acquisition(self, desktop):
        self.assertEqual(desktop.u.restore_calls, [])
        self.assertEqual(desktop.u.activation_calls, [])
        self.assertEqual(desktop.events, [])
        self.assertEqual(self.captures, [])
        desktop.reader.assert_not_called()
        self.assertIsNone(desktop._background_token)

    def test_idle_601_with_any_preheld_mouse_or_modifier_has_zero_focus_or_input(self):
        for key in (0x01, 0x02, 0x04, 0x05, 0x06, 0x10, 0x11, 0x12, 0x5B, 0x5C):
            for minimized in (False, True):
                with self.subTest(key=key, minimized=minimized):
                    desktop = self.setup_activation()
                    self.clock.sleep(1)
                    desktop.u.minimized = minimized
                    desktop.u.held_keys.add(key)
                    with self.assertRaises(FocusUnavailable) as caught:
                        self.begin(desktop)
                    self.assertIs(caught.exception.input_issued, False)
                    self.assertTrue(caught.exception.diagnostics["acquisition_blocked"])
                    self.assertTrue(all(type(value) is bool for value in caught.exception.diagnostics.values()))
                    self.assert_no_acquisition(desktop)
                    self.assertEqual(desktop.u.minimized, minimized)

    def test_capture_menu_or_move_size_prevents_even_show_or_hover(self):
        cases = [("gui_capture", FakeUser32.OVERLAY), ("gui_capture", FakeUser32.GAME),
                 ("gui_flags", 2), ("gui_flags", 4), ("gui_flags", 8), ("gui_flags", 16),
                 ("gui_menu_owner", FakeUser32.OVERLAY), ("gui_move_size", FakeUser32.OVERLAY)]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                desktop = self.setup_activation()
                self.clock.sleep(1)
                setattr(desktop.u, field, value)
                with self.assertRaises(FocusUnavailable) as caught:
                    self.begin(desktop)
                self.assertFalse(caught.exception.input_issued)
                self.assert_no_acquisition(desktop)
                self.assertEqual(desktop.u.foreground, desktop.u.OVERLAY)

    def test_unavailable_or_racing_foreground_query_is_fail_closed_before_show(self):
        for state in ("unavailable", "foreground_race", "no_active", "button_during_query"):
            with self.subTest(state=state):
                desktop = self.setup_activation()
                if state == "unavailable": desktop.u.gui_available = False
                elif state == "foreground_race":
                    desktop.u.on_gui_query = lambda: setattr(desktop.u, "foreground", 999)
                elif state == "no_active": desktop.u.foreground = 0
                else: desktop.u.on_gui_query = lambda: desktop.u.held_keys.add(0x01)
                with self.assertRaises(FocusUnavailable): self.begin(desktop)
                self.assert_no_acquisition(desktop)

    def test_capture_or_button_acquired_after_second_proof_cancels_before_down(self):
        for held in (False, True):
            with self.subTest(held_button=held):
                desktop = self.setup_activation()
                def capture_late():
                    if desktop.u.physical_calls == 2:
                        if held: desktop.u.held_keys.add(0x01)
                        else: desktop.u.gui_capture = desktop.u.OVERLAY
                desktop.u.on_physical_hit = capture_late
                with self.assertRaises(FocusUnavailable) as caught:
                    self.begin(desktop)
                self.assertFalse(caught.exception.input_issued)
                self.assertEqual(len(self.captures), 2)
                self.assertEqual(desktop.reader.call_count, 2)
                self.assertEqual(self.flags(desktop), [0xC001])
                self.assertIsNone(desktop._background_token)

    def test_capture_after_first_header_prevents_the_first_move(self):
        desktop = self.setup_activation()
        def capture_after_header(_image):
            desktop.u.gui_capture = desktop.u.OVERLAY
            return desktop.target
        desktop.reader.side_effect = capture_after_header
        with self.assertRaises(FocusUnavailable): self.begin(desktop)
        self.assertEqual(desktop.events, [])
        self.assertEqual(len(self.captures), 1)

    def test_own_accepted_down_capture_does_not_block_mandatory_up(self):
        desktop = self.setup_activation()
        def own_down():
            desktop.u.held_keys.add(0x01)
            desktop.u.gui_capture = desktop.u.GAME
            desktop.u.foreground = desktop.u.GAME
        def own_up():
            desktop.u.held_keys.clear()
            desktop.u.gui_capture = 0
        desktop.on_down, desktop.on_up = own_down, own_up
        self.begin(desktop)
        self.assertEqual(self.flags(desktop), [0xC001, 2, 4])
        self.assertTrue(desktop.last_activation_diagnostics["up_accepted"])
        self.assertFalse(desktop.u.held_keys)
        self.assertEqual(desktop.u.gui_capture, 0)

    def test_low_pressed_bits_and_caret_blinking_do_not_claim_a_held_gesture(self):
        desktop = self.setup_activation()
        original = desktop.u.GetAsyncKeyState
        desktop.u.GetAsyncKeyState = lambda key: original(key) if key == 0x77 else 1
        desktop.u.gui_flags = 1
        self.begin(desktop)
        self.assertEqual(self.flags(desktop), [0xC001, 2, 4])

    def test_explicit_start_also_cannot_show_through_foreign_capture(self):
        desktop = self.setup_activation()
        desktop.u.gui_capture = desktop.u.OVERLAY
        with self.assertRaises(FocusUnavailable): desktop.activate_for_start(desktop.u.GAME)
        self.assert_no_acquisition(desktop)

    def test_proved_blank_patch_uses_its_center_not_the_caption_center(self):
        for proof in ("a" * 64, "A" * 64):
            with self.subTest(proof_case=proof[0]):
                desktop = self.setup_activation()
                patch_rect, caption = Rect(240, 40, 260, 60), Rect(100, 40, 200, 80)
                desktop.target = ActivationTarget(patch_rect, "storage", caption, proof)
                down_points = []
                def activated():
                    down_points.append(desktop.u.cursor)
                    desktop.u.foreground = desktop.u.GAME
                desktop.on_down = activated
                self.begin(desktop)
                expected = (desktop.u.box[0] + patch_rect.center[0],
                            desktop.u.box[1] + patch_rect.center[1])
                self.assertEqual(down_points, [expected])
                self.assertNotEqual(patch_rect.center, caption.center)
                self.assertEqual(desktop.u.physical_calls, 2)
                self.assertEqual(self.flags(desktop), [0xC001, 2, 4])

    def test_malformed_anchor_provenance_is_rejected_before_first_move(self):
        caption = Rect(100, 40, 200, 80)
        invalid = [(None, "a" * 64), (caption, None), ((100, 40, 200, 80), "a" * 64),
                   (Rect(-1, 40, 200, 80), "a" * 64),
                   (Rect(490, 40, 510, 80), "a" * 64),
                   (Rect(100, 390, 200, 410), "a" * 64),
                   (Rect(100, 40, 103, 80), "a" * 64),
                   (Rect(True, 40, 200, 80), "a" * 64)]
        invalid.extend((caption, proof) for proof in ("a" * 63, "a" * 65, "g" * 64,
                       "a" * 63 + " ", b"a" * 64, True, 123))
        for anchor, proof in invalid:
            with self.subTest(anchor=anchor, proof=proof):
                desktop = self.setup_activation()
                desktop.target = ActivationTarget(desktop.target.rect, "storage", anchor, proof)
                with self.assertRaises(ValueError): self.begin(desktop)
                self.assertEqual(desktop.events, [])
                self.assertEqual(len(self.captures), 1)
                self.assertIsNone(desktop._background_token)

    def test_changed_caption_or_proof_on_second_frame_prevents_down_at_same_patch(self):
        patch_rect, caption = Rect(240, 40, 260, 60), Rect(100, 40, 200, 80)
        for anchor, proof in ((Rect(101, 40, 201, 80), "a" * 64),
                              (caption, "b" * 64), (caption, "A" * 64)):
            with self.subTest(anchor=anchor, proof_case=proof[0]):
                desktop = self.setup_activation()
                first = ActivationTarget(patch_rect, "storage", caption, "a" * 64)
                second = ActivationTarget(patch_rect, "storage", anchor, proof)
                desktop.reader.side_effect = [first, second]
                with self.assertRaises(FocusUnavailable) as caught:
                    self.begin(desktop)
                self.assertFalse(caught.exception.input_issued)
                self.assertEqual(len(self.captures), 2)
                self.assertEqual(self.flags(desktop), [])

    def test_provenance_target_still_releases_and_is_terminal_after_accepted_down(self):
        desktop = self.setup_activation()
        desktop.target = ActivationTarget(Rect(240, 40, 260, 60), "storage",
                                          Rect(100, 40, 200, 80), "a" * 64)
        desktop.on_down = lambda: setattr(desktop.u, "game_pid", desktop.u.game_pid + 1)
        with self.assertRaises(WindowGeometryChanged) as caught:
            self.begin(desktop)
        self.assertTrue(caught.exception.input_issued)
        self.assertEqual(self.flags(desktop), [0xC001, 2, 4])

    def bound_target(self, desktop):
        desktop.target = ActivationTarget(Rect(240, 40, 260, 60), "storage",
                                          Rect(100, 40, 200, 80), "a" * 64)

    def test_bound_provider_proofs_both_precede_move_then_full_caption_is_captured(self):
        desktop = self.setup_activation()
        self.bound_target(desktop)
        order = []
        self.on_capture = lambda: order.append("capture")
        desktop.reader.side_effect = lambda _image: (order.append("provider"), desktop.target)[1]
        desktop.on_move = lambda: order.append("move")
        def down():
            order.append("down")
            desktop.u.foreground = desktop.u.GAME
        desktop.on_down = down
        desktop.on_up = lambda: order.append("up")
        self.begin(desktop)
        self.assertEqual(order, ["capture", "provider", "capture", "provider",
                                 "move", "capture", "down", "up"])
        self.assertEqual(desktop.reader.call_count, 2)
        self.assertEqual(self.flags(desktop), [0xC001, 2, 4])

    def test_slow_global_search_can_propose_only_with_fresh_exact_patch_recheck(self):
        from panels import PanelController
        for changed in (False, True):
            with self.subTest(changed=changed):
                desktop = self.setup_activation()
                self.bound_target(desktop)
                owner = PanelController.__new__(PanelController)
                def propose(image):
                    self.clock.sleep(3.)
                    target = desktop.target
                    owner._activation_pixels = (image.size, target,
                        image.crop(target.anchor_rect.as_box()).tobytes(),
                        image.crop(target.rect.as_box()).tobytes())
                    return target
                # A real bound provider exposes the product's narrow recheck.
                class Provider:
                    def read(_self, image): return propose(image)
                    def recheck_activation_target(_self, image, target):
                        return owner.recheck_activation_target(image, target)
                desktop.reader = Provider().read
                def frame(image, index):
                    if changed and index == 2:
                        image.putpixel(desktop.target.rect.center, (99, 88, 77))
                self.on_frame = frame
                if changed:
                    with self.assertRaises(FocusUnavailable): self.begin(desktop)
                    self.assertEqual(self.flags(desktop), [])
                else:
                    self.begin(desktop)
                    self.assertEqual(self.flags(desktop), [0xC001, 2, 4])

    def test_post_move_caption_pixel_change_is_safe_refusal_without_down(self):
        desktop = self.setup_activation()
        self.bound_target(desktop)
        def changed(frame, index):
            if index == 3:
                # Even one changed pixel is rejected; no widened RMSE or mask.
                frame.putpixel((desktop.target.anchor_rect.left + 1,
                                desktop.target.anchor_rect.top + 1), (32, 25, 42))
        self.on_frame = changed
        with self.assertRaises(FocusUnavailable) as caught:
            self.begin(desktop)
        self.assertFalse(caught.exception.input_issued)
        self.assertEqual(caught.exception.diagnostics["activation_stage"], 3)
        self.assertEqual(self.flags(desktop), [0xC001])
        self.assertEqual(desktop.reader.call_count, 2)
        self.assertIsNone(desktop._background_token)

    def test_own_cursor_pixels_on_blank_patch_do_not_replace_caption_evidence(self):
        desktop = self.setup_activation()
        self.bound_target(desktop)
        def software_cursor(frame, index):
            if index == 3:
                frame.paste((0, 150, 255), desktop.target.rect.as_box())
        self.on_frame = software_cursor
        self.begin(desktop)
        self.assertEqual(len(self.captures), 3)
        self.assertEqual(desktop.reader.call_count, 2)
        self.assertEqual(self.flags(desktop), [0xC001, 2, 4])

    def test_user_takeover_at_every_bound_proof_stage_prevents_down(self):
        for stage in ("capture1", "provider1", "capture2", "provider2", "move", "capture3", "last_hit"):
            with self.subTest(stage=stage):
                desktop = self.setup_activation()
                self.bound_target(desktop)
                def takeover():
                    desktop.input_observer.record(0)
                    desktop.u.foreground = 999
                self.on_capture = lambda: takeover() if stage == f"capture{len(self.captures)}" else None
                def provider(_image):
                    if stage == f"provider{desktop.reader.call_count}": takeover()
                    return desktop.target
                desktop.reader.side_effect = provider
                desktop.on_move = lambda: takeover() if stage == "move" else None
                desktop.u.on_physical_hit = lambda: takeover() if stage == "last_hit" and desktop.u.physical_calls == 2 else None
                with self.assertRaises(UserActivityInterrupted): self.begin(desktop)
                self.assertEqual(self.flags(desktop), [0xC001] if stage in ("move", "capture3", "last_hit") else [])
                self.assertEqual(desktop.u.foreground, 999)
                self.assertEqual(desktop.u.restore_calls, [(desktop.u.GAME, 5)])
                self.assertEqual(desktop.u.activation_calls, [desktop.u.GAME])

    def test_bound_capture_and_last_hit_cannot_renew_expired_pre_move_proof(self):
        for stage in ("capture3", "last_hit"):
            with self.subTest(stage=stage):
                desktop = self.setup_activation()
                self.bound_target(desktop)
                self.on_capture = lambda: self.clock.sleep(2.01) if stage == "capture3" and len(self.captures) == 3 else None
                desktop.u.on_physical_hit = lambda: self.clock.sleep(2.01) if stage == "last_hit" and desktop.u.physical_calls == 2 else None
                expected = FocusUnavailable if stage == "capture3" else WindowGeometryChanged
                with self.assertRaises(expected) as caught: self.begin(desktop)
                self.assertFalse(caught.exception.input_issued)
                self.assertEqual(self.flags(desktop), [0xC001])

    def test_f8_and_ui_stop_during_bound_proofs_cancel_without_any_button(self):
        for ui_stop in (False, True):
            for stage in ("capture2", "move", "capture3"):
                with self.subTest(ui_stop=ui_stop, stage=stage):
                    desktop = self.setup_activation()
                    self.bound_target(desktop)
                    def stop():
                        if ui_stop:
                            desktop.guard.side_effect = InterruptedError("UI Stop")
                        else:
                            desktop.u.stop = True
                    self.on_capture = lambda: stop() if stage == f"capture{len(self.captures)}" else None
                    desktop.on_move = lambda: stop() if stage == "move" else None
                    with self.assertRaises(InterruptedError): self.begin(desktop)
                    self.assertEqual(self.flags(desktop), [] if stage == "capture2" else [0xC001])
                    self.assertIsNone(desktop._background_token)

    def test_one_tagged_gesture_uses_two_fresh_frames_and_verifies_actual_focus(self):
        desktop = self.setup_activation()
        token = self.begin(desktop)
        self.assertEqual(self.flags(desktop), [0xC001, 2, 4])
        self.assertTrue(all(tag == HELPER_INPUT_TAG for _, tag in desktop.events))
        self.assertEqual(len(self.captures), 2)
        self.assertEqual(desktop.reader.call_count, 2)
        self.assertEqual(desktop.u.restore_calls, [(desktop.u.GAME, 5)])
        self.assertEqual(desktop.u.activation_calls, [desktop.u.GAME])
        self.assertEqual(token.previous_window, desktop.u.OVERLAY)
        self.assertTrue(desktop.last_activation_diagnostics["down_accepted"])
        self.assertTrue(desktop.last_activation_diagnostics["up_accepted"])
        self.assertTrue(desktop.last_activation_diagnostics["focus_verified"])

    def test_raw_windows_idle_reset_by_tagged_hover_does_not_cancel_observer_idle(self):
        desktop = self.setup_activation()
        self.begin(desktop)
        self.assertEqual(desktop._windows_idle_seconds(), 0)
        self.assertGreaterEqual(desktop.idle_seconds(), 600)
        self.assertEqual(desktop.input_observer.snapshot()[0], 0)
        self.assertEqual(self.flags(desktop), [0xC001, 2, 4])

    def test_negative_monitor_origin_translates_proven_heading_center(self):
        desktop = self.setup_activation()
        desktop.u.box = [-1000, -300, -500, 100]
        self.begin(desktop)
        self.assertEqual(desktop.u.cursor, (-850, -240))
        self.assertEqual(self.captures, [tuple(desktop.u.box)] * 2)

    def test_missing_header_sends_no_input_and_legacy_no_callback_never_captures(self):
        desktop = self.setup_activation()
        desktop.reader.return_value = None
        desktop.reader.side_effect = None
        with self.assertRaises(FocusUnavailable) as caught:
            self.begin(desktop)
        self.assertIs(caught.exception.input_issued, False)
        self.assertEqual(desktop.events, [])
        with self.assertRaises(FocusUnavailable):
            desktop.begin_background_cycle(desktop.u.GAME)
        self.assertEqual(len(self.captures), 1)

    def test_changed_missing_or_different_pane_second_header_sends_no_button(self):
        for target in (None, ActivationTarget(Rect(101, 40, 201, 80), "storage"),
                       ActivationTarget(Rect(100, 40, 200, 80), "inventory")):
            with self.subTest(target=target):
                desktop = self.setup_activation()
                desktop.reader.side_effect = [desktop.target, target]
                with self.assertRaises(FocusUnavailable):
                    self.begin(desktop)
                self.assertEqual(self.flags(desktop), [0xC001])

    def test_occlusion_and_logical_physical_hit_disagreement_never_receive_button(self):
        for physical, logical in ((FakeUser32.OVERLAY, FakeUser32.OVERLAY),
                                  (FakeUser32.CHILD, FakeUser32.OVERLAY),
                                  (FakeUser32.OVERLAY, FakeUser32.CHILD)):
            with self.subTest(physical=physical, logical=logical):
                desktop = self.setup_activation()
                desktop.u.physical_target = physical
                desktop.u.targets = [logical]
                with self.assertRaises(WindowGeometryChanged) as caught:
                    self.begin(desktop)
                self.assertFalse(caught.exception.input_issued)
                self.assertEqual(self.flags(desktop), [0xC001])

    def test_move_resize_pid_or_new_foreground_after_hover_prevents_down(self):
        for change in ("move", "resize", "pid", "foreground"):
            with self.subTest(change=change):
                desktop = self.setup_activation()
                def mutate():
                    if change == "move":
                        desktop.u.box[0] += 1; desktop.u.box[2] += 1
                    elif change == "resize":
                        desktop.u.box[2] -= 1
                    elif change == "pid":
                        desktop.u.game_pid += 1
                    else:
                        desktop.u.foreground = 999
                desktop.on_move = mutate
                with self.assertRaises(WindowGeometryChanged) as caught:
                    self.begin(desktop)
                self.assertFalse(caught.exception.input_issued)
                self.assertEqual(self.flags(desktop), [0xC001])

    def test_overlay_appearing_in_last_physical_hit_test_cancels_before_down(self):
        desktop = self.setup_activation()
        def obstruct():
            if desktop.u.physical_calls == 2:
                desktop.u.physical_target = desktop.u.OVERLAY
        desktop.u.on_physical_hit = obstruct
        with self.assertRaises(WindowGeometryChanged) as caught:
            self.begin(desktop)
        self.assertFalse(caught.exception.input_issued)
        self.assertEqual(self.flags(desktop), [0xC001])

    def test_move_during_capture_discards_frame_before_target_callback_or_hover(self):
        desktop = self.setup_activation()
        self.on_capture = lambda: desktop.u.box.__setitem__(2, desktop.u.box[2] - 1)
        with self.assertRaises(WindowGeometryChanged):
            self.begin(desktop)
        desktop.reader.assert_not_called()
        self.assertEqual(desktop.events, [])

    def test_user_input_and_ui_stop_after_hover_cancel_before_any_button(self):
        for stop in (False, True):
            with self.subTest(ui_stop=stop):
                desktop = self.setup_activation()
                if stop:
                    desktop.on_move = lambda: setattr(desktop.guard, "side_effect", InterruptedError("UI Stop"))
                    expected = InterruptedError
                else:
                    desktop.on_move = lambda: desktop.input_observer.record(0)
                    expected = UserActivityInterrupted
                with self.assertRaises(expected):
                    self.begin(desktop)
                self.assertEqual(self.flags(desktop), [0xC001])
                self.assertIsNone(desktop._background_token)

    def test_f8_or_ui_stop_after_down_always_releases_without_retry(self):
        for stop in (False, True):
            with self.subTest(ui_stop=stop):
                desktop = self.setup_activation()
                if stop:
                    desktop.on_down = lambda: setattr(desktop.guard, "side_effect", InterruptedError("UI Stop"))
                else:
                    desktop.on_down = lambda: setattr(desktop.u, "stop", True)
                with self.assertRaises(InterruptedError):
                    self.begin(desktop)
                self.assertEqual(self.flags(desktop), [0xC001, 2, 4])

    def test_accepted_down_without_confirmed_focus_is_terminal_not_safe_waiting(self):
        desktop = self.setup_activation()
        desktop.on_down = lambda: None
        with self.assertRaises(WindowGeometryChanged) as caught:
            self.begin(desktop)
        self.assertTrue(caught.exception.input_issued)
        self.assertNotIsInstance(caught.exception, FocusUnavailable)
        self.assertEqual(self.flags(desktop), [0xC001, 2, 4])
        self.assertFalse(desktop.last_activation_diagnostics["focus_verified"])

    def test_post_down_geometry_or_user_takeover_is_terminal_and_leaves_focus_choice(self):
        for change in ("resize", "pid", "user"):
            with self.subTest(change=change):
                desktop = self.setup_activation()
                def mutate():
                    if change == "resize": desktop.u.box[2] -= 1
                    elif change == "pid": desktop.u.game_pid += 1
                    else:
                        desktop.input_observer.record(77); desktop.u.foreground = 999
                desktop.on_down = mutate
                with self.assertRaises(WindowGeometryChanged) as caught:
                    self.begin(desktop)
                self.assertTrue(caught.exception.input_issued)
                self.assertEqual(self.flags(desktop), [0xC001, 2, 4])
                self.assertEqual(desktop.u.restore_calls, [(desktop.u.GAME, 5)])
                self.assertEqual(desktop.u.activation_calls, [desktop.u.GAME])

    def test_up_rejection_keeps_accepted_down_terminal_and_does_not_repeat_either_button(self):
        desktop = self.setup_activation()
        desktop.refuse_up = True
        with self.assertRaises(WindowGeometryChanged) as caught:
            self.begin(desktop)
        self.assertTrue(caught.exception.input_issued)
        self.assertFalse(caught.exception.diagnostics["activation_up_accepted"])
        self.assertTrue(desktop.last_activation_diagnostics["down_accepted"])
        self.assertFalse(desktop.last_activation_diagnostics["up_accepted"])
        self.assertEqual(self.flags(desktop), [0xC001, 2, 4])

    def test_pid_race_after_helper_return_promotes_final_cycle_check_to_terminal(self):
        desktop = self.setup_activation()
        operation = desktop._activate_from_header
        def activate(*args):
            issued = operation(*args)
            desktop.u.game_pid += 1
            return issued
        desktop._activate_from_header = activate
        with self.assertRaises(WindowGeometryChanged) as caught:
            self.begin(desktop)
        self.assertTrue(caught.exception.input_issued)
        self.assertEqual(self.flags(desktop), [0xC001, 2, 4])

    def test_unknown_error_after_helper_return_cannot_become_preinput_wait(self):
        desktop = self.setup_activation()
        original = desktop.check_background_cycle
        armed = [False]
        operation = desktop._activate_from_header
        def activate(*args):
            issued = operation(*args); armed[0] = True; return issued
        def background(token=None):
            if armed[0]: raise RuntimeError("Unknown final check error")
            return original(token)
        desktop._activate_from_header = activate
        desktop.check_background_cycle = background
        with self.assertRaises(WindowGeometryChanged) as caught:
            self.begin(desktop)
        self.assertTrue(caught.exception.input_issued)
        self.assertEqual(self.flags(desktop), [0xC001, 2, 4])

    def test_expired_header_and_invalid_target_are_never_button_evidence(self):
        for invalid in (True, False):
            with self.subTest(invalid=invalid):
                desktop = self.setup_activation()
                if invalid:
                    desktop.reader.side_effect = lambda _image: {"rect": desktop.target.rect, "pane": "storage"}
                    expected = ValueError
                else:
                    def slow(_image):
                        self.clock.sleep(2.01); return desktop.target
                    desktop.reader.side_effect = slow
                    expected = FocusUnavailable
                with self.assertRaises(expected): self.begin(desktop)
                self.assertEqual(desktop.events, [])

    def test_inactive_ordinary_capture_and_click_guards_are_not_relaxed(self):
        desktop = self.setup_activation()
        with self.assertRaises(WindowGeometryChanged): desktop.capture(desktop.u.GAME)
        with self.assertRaises(WindowGeometryChanged): desktop.click(desktop.u.GAME, (150, 60), (500, 400))
        self.assertEqual(desktop.events, [])
        self.assertEqual(self.captures, [])


if __name__ == "__main__": unittest.main()
