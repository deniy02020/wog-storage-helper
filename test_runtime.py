"""Runner boundary tests use a fake desktop; they never send OS input."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import shutil
from unittest.mock import Mock

from PIL import Image, ImageDraw

from runtime import Runner


def cell(tier=None, *, equipment=True):
    image = Image.new("RGB", (24, 24), (12, 15, 18))
    if tier is not None:
        draw = ImageDraw.Draw(image)
        draw.rectangle((1, 1, 22, 22), fill=(60, 80, 100))
        draw.rectangle((3, 3, 10, 10), fill=(20 + 30 * tier, 250 - 25 * tier, 25 * tier))
        draw.rectangle((12, 12, 20, 20), fill=(240, 240, 240) if equipment else (0, 0, 0))
    return image


def frame(inventory, storage):
    image = Image.new("RGB", (96, 64), (30, 35, 40))
    for index, tile in enumerate(inventory):
        image.paste(tile, (index * 24, 16))
    for index, tile in enumerate(storage):
        image.paste(tile, (48 + index * 24, 16))
    return image


class FakeDesktop:
    def __init__(self, frames, active=True):
        self.frames = list(frames)
        self.is_active = active
        self.inputs = []

    def capture(self, handle):
        if not self.is_active:
            raise RuntimeError("not foreground")
        return self.frames.pop(0)

    def active(self, handle):
        return self.is_active

    def stop_pressed(self):
        return False

    def drag(self, *arguments):
        self.inputs.append(("drag", arguments))

    def click(self, *arguments, **kwargs):
        self.inputs.append(("click", arguments))

    def move(self, *arguments):
        self.inputs.append(("move", arguments))


class FakeGear:
    def match(self, crop):
        position = (16, 16) if crop.width >= 24 else (4, 4)
        return SimpleNamespace(is_equipment=crop.getpixel(position)[0] > 200)


class RuntimeTestFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        label = "equipment_label_yellow.png"
        shutil.copyfile(Path(__file__).parent / "preset_assets" / label, self.directory / label)
        cell().save(self.directory / "empty.png")
        for tier in range(1, 7):
            cell(tier).crop((3, 3, 11, 11)).save(self.directory / f"T{tier}.png")
        neutral = frame([cell(), cell()], [cell(), cell()])
        neutral.crop((0, 0, 12, 8)).save(self.directory / "anchor1.png")
        neutral.crop((48, 0, 60, 8)).save(self.directory / "anchor2.png")
        self.profile = {
            "size": [96, 64], "mode": "drag", "supervised_ok": False,
            "inventory": {"rect": [0, 16, 48, 40], "rows": 1, "columns": 2},
            "storage": {"rect": [48, 16, 96, 40], "rows": 1, "columns": 2},
            "badge": [3, 3, 11, 11], "icon_rect": [12, 12, 21, 21],
            "equipment_label_samples": [label],
            "tiers": {str(t): [f"T{t}.png"] for t in range(1, 7)},
            "inventory_empty": ["empty.png"], "storage_empty": ["empty.png"],
            "anchors": [{"rect": [0, 0, 12, 8], "image": "anchor1.png"},
                        {"rect": [48, 0, 60, 8], "image": "anchor2.png"}],
        }

    def tearDown(self):
        self.temp.cleanup()

    def runner(self, frames, active=True):
        desktop = FakeDesktop(frames, active)
        runner = Runner(desktop, 5, self.profile, self.directory)
        runner.gear = FakeGear()
        runner.wait = lambda _: None  # Each frame is deterministic; no wall-clock waits.
        return runner, desktop


class RunnerBoundaryTests(RuntimeTestFixture, unittest.TestCase):
    def test_readback_parks_with_tagged_native_api_when_available(self):
        before = frame([cell(), cell()], [cell(), cell()])
        runner, desktop = self.runner([before])
        desktop.park = Mock()
        desktop.move = Mock(side_effect=AssertionError("legacy move must not park the game pointer"))

        self.assertIs(runner._park_and_capture(), before)

        desktop.park.assert_called_once_with(5, (6, 4), (96, 64))
        desktop.move.assert_not_called()

    def test_failed_tagged_parking_never_falls_back_to_unchecked_move(self):
        from windows import WindowGeometryChanged
        runner, desktop = self.runner([])
        error = WindowGeometryChanged("changed")
        desktop.park = Mock(side_effect=error)
        desktop.move = Mock()

        with self.assertRaises(WindowGeometryChanged) as raised:
            runner._park_and_capture()

        self.assertIs(raised.exception, error)
        desktop.move.assert_not_called()

    def test_combined_mode_transfers_blue_gem_and_keeps_green_gem(self):
        self.profile["include_gems"] = True
        self.profile["equipment_label_samples"] = []
        before = frame([cell(2, equipment=False), cell(3, equipment=False)], [cell(), cell()])
        after = frame([cell(2, equipment=False), cell()], [cell(3, equipment=False), cell()])
        runner, desktop = self.runner([before, before.copy(), after])
        runner.gear = SimpleNamespace(match=Mock(side_effect=AssertionError("Lv guard must not run in combined mode")))

        self.assertTrue(runner.once())

        transfers = [(action, args) for action, args in desktop.inputs if action != "move"]
        self.assertEqual(transfers, [("drag", (5, (36, 28), (60, 28), (96, 64)))])
        runner.gear.match.assert_not_called()

    def test_legacy_profiles_migrate_to_combined_mode_with_blue_gems(self):
        for setting in (None, False):
            with self.subTest(include_gems=setting):
                self.profile["supervised_ok"] = True
                if setting is None:
                    self.profile.pop("include_gems", None)
                else:
                    self.profile["include_gems"] = setting
                before = frame([cell(3, equipment=False), cell()], [cell(), cell()])
                runner, desktop = self.runner([before])
                self.assertEqual(runner.preview()[3].tier, 3)
                self.assertTrue(runner.profile["include_gems"])
                if setting is False:
                    self.assertFalse(runner.profile["supervised_ok"])
                self.assertEqual(desktop.inputs, [])

    def test_combined_mode_rejects_gray_green_and_unknown_tier_frames(self):
        self.profile["include_gems"] = True
        for tier in (1, 2, 7):
            with self.subTest(tier=tier):
                before = frame([cell(tier, equipment=False), cell()], [cell(), cell()])
                runner, desktop = self.runner([before])
                _, inventory, _, candidate = runner.preview()
                self.assertIsNone(candidate)
                self.assertEqual(inventory.cells[0].tier, tier if tier < 3 else None)
                self.assertEqual(desktop.inputs, [])

    def test_invalid_combined_mode_values_prevent_runner_construction(self):
        for setting in (None, "false", "true", 0, 1, [], {}):
            with self.subTest(include_gems=setting):
                self.profile["include_gems"] = setting
                desktop = FakeDesktop([])
                with self.assertRaises(ValueError):
                    Runner(desktop, 5, self.profile, self.directory)
                self.assertEqual(desktop.inputs, [])

    def test_configured_sort_does_not_hide_new_blue_gear_after_green(self):
        # New drops can appear below a green item even after earlier sorting.
        # A configured sort button must not make preview assume tier ordering.
        before = frame([cell(2), cell(3)], [cell(), cell()])
        before.crop((24, 0, 36, 8)).save(self.directory / "sort.png")
        self.profile["sort_button"] = {"rect": [24, 0, 36, 8], "image": "sort.png"}
        runner, desktop = self.runner([before])

        _, _, _, candidate = runner.preview()

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.source_index, 1)
        self.assertEqual(candidate.tier, 3)
        self.assertEqual(desktop.inputs, [])

    def test_t3_plus_threshold_skips_gray_and_green_then_selects_blue_red_or_purple(self):
        for tier in range(1, 6):
            with self.subTest(tier=tier):
                before = frame([cell(2), cell(tier)], [cell(), cell()])
                runner, desktop = self.runner([before])
                _, inventory, storage, candidate = runner.preview()
                if tier < 3:
                    self.assertIsNone(candidate)
                else:
                    self.assertEqual(candidate.source_index, 1)
                    self.assertEqual(candidate.tier, tier)
                self.assertEqual(desktop.inputs, [])

    def test_combined_mode_transfers_first_high_tier_item_without_level_restriction(self):
        before = frame([cell(3, equipment=False), cell(4)], [cell(), cell()])
        after = frame([cell(4), cell()], [cell(3, equipment=False), cell()])
        runner, desktop = self.runner([before, before.copy(), after])
        self.assertTrue(runner.once())
        self.assertEqual(desktop.inputs[0][0], "drag")
        self.assertEqual(desktop.inputs[0][1][1], (12, 28))

    def test_full_visible_storage_stops_before_any_input(self):
        before = frame([cell(3), cell()], [cell(4), cell(5)])
        runner, desktop = self.runner([before])
        with self.assertRaisesRegex(RuntimeError, "свободных"):
            runner.once()
        self.assertEqual(desktop.inputs, [])

    def test_background_game_cannot_send_input(self):
        runner, desktop = self.runner([], active=False)
        with self.assertRaisesRegex(RuntimeError, "foreground"):
            runner.once()
        self.assertEqual(desktop.inputs, [])

    def test_changed_item_before_input_cancels_transfer(self):
        before = frame([cell(3), cell()], [cell(), cell()])
        changed = frame([cell(4), cell()], [cell(), cell()])
        runner, desktop = self.runner([before, changed])
        with self.assertRaisesRegex(RuntimeError, "изменился"):
            runner.once()
        self.assertEqual(desktop.inputs, [])

    def test_failed_transfer_has_one_attempt_and_no_retry(self):
        before = frame([cell(3), cell()], [cell(), cell()])
        runner, desktop = self.runner([before, before.copy(), before.copy()])
        with self.assertRaisesRegex(RuntimeError, "не подтверждён"):
            runner.once()
        self.assertEqual(sum(action == "drag" for action, _ in desktop.inputs), 1)

    def test_explicit_monitor_without_single_proof_still_scans_before_input(self):
        runner, desktop = self.runner([frame([cell(2), cell()], [cell(), cell()])])
        waits = []
        def stopped_after_cycle(seconds):
            waits.append(seconds)
            raise InterruptedError("test complete")
        runner.wait = stopped_after_cycle
        with self.assertRaisesRegex(InterruptedError, "test complete"):
            runner.monitor(1800)
        self.assertEqual(waits, [1800])
        self.assertFalse(runner.profile["supervised_ok"])
        self.assertEqual(desktop.inputs, [])

    def test_sorting_brings_hidden_priority_into_first_cell_before_transfer(self):
        unsorted = frame([cell(2), cell(4)], [cell(), cell()])
        sorted_frame = frame([cell(4), cell(2)], [cell(), cell()])
        after_move = frame([cell(2), cell()], [cell(4), cell()])
        unsorted.crop((24, 0, 36, 8)).save(self.directory / "sort.png")
        self.profile["sort_button"] = {"rect": [24, 0, 36, 8], "image": "sort.png"}
        runner, desktop = self.runner([unsorted, sorted_frame, sorted_frame, sorted_frame, after_move])
        self.assertTrue(runner.once())
        self.assertEqual(desktop.inputs[0][0], "click")
        self.assertEqual(desktop.inputs[0][1][1], (30, 4))
        drag = next(args for action, args in desktop.inputs if action == "drag")
        self.assertEqual(drag[1], (12, 28))

    def test_missing_sort_button_does_not_click(self):
        before = frame([cell(4), cell(2)], [cell(), cell()])
        Image.new("RGB", (12, 8), "white").save(self.directory / "sort.png")
        self.profile["sort_button"] = {"rect": [24, 0, 36, 8], "image": "sort.png"}
        runner, desktop = self.runner([before])
        with self.assertRaisesRegex(RuntimeError, "сортировки"):
            runner.once()
        self.assertEqual(desktop.inputs, [])


if __name__ == "__main__":
    unittest.main()
