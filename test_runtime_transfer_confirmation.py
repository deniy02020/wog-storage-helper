"""A fresh loot drop must not cause a second item click after readback."""
import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from core import choose_transfer
from runtime import Runner
from test_runtime import RuntimeTestFixture, cell, frame


class TransferConfirmationTests(RuntimeTestFixture, TestCase):
    def automatic_runner(self, frames):
        runner, desktop = self.runner(frames)
        runner.canonical_profile["automatic_layout"] = True
        runner.profile["automatic_layout"] = True
        return runner, desktop

    def test_same_tier_drop_elsewhere_uses_actual_destination_badge_and_one_input(self):
        before = frame([cell(3), cell()], [cell(), cell()])
        after = frame([cell(), cell(3)], [cell(3), cell()])
        runner, desktop = self.automatic_runner([before, before.copy(), after])
        original = copy.deepcopy(runner.profile)
        self.assertTrue(runner.once(sort=False))
        self.assertEqual(sum(kind == "drag" for kind, _ in desktop.inputs), 1)
        self.assertFalse(runner._transfer_input_issued)
        self.assertEqual(runner.profile, original)
        self.assertEqual(runner.inv_config.max_tier_rmse, 14)
        self.assertEqual(runner.inv_config.min_margin, 5)

    def test_wrong_unknown_or_missing_destination_stays_unverified_without_replay(self):
        for destination in (cell(4), cell(7), cell()):
            with self.subTest(destination=destination.getpixel((3, 3))):
                before = frame([cell(3), cell()], [cell(), cell()])
                after = frame([cell(), cell(3)], [destination, cell()])
                runner, desktop = self.automatic_runner([before, before.copy(), after])
                with patch("runtime.LAYOUT_READBACK_SECONDS", 0), patch("runtime.logging.info") as log:
                    with self.assertRaisesRegex(RuntimeError, "Перенос не подтверждён"):
                        runner.once(sort=False)
                    self.assertTrue(runner._transfer_input_issued)
                    with self.assertRaisesRegex(RuntimeError, "Предыдущий ввод не подтверждён"):
                        runner.once(sort=False)
                self.assertEqual(sum(kind == "drag" for kind, _ in desktop.inputs), 1)
                log.assert_called_once()
                self.assertEqual(log.call_args.args[0], "transfer_readback_unverified %s")
                audit = json.loads(log.call_args.args[1])
                self.assertEqual(audit["candidate"]["tier"], 3)
                self.assertEqual(audit["before"]["inventory"]["tier_counts"], {"3": 1})
                self.assertEqual(audit["after"]["inventory"]["tier_counts"], {"3": 1})
                self.assertIs(audit["after"]["inventory"]["cells"][0]["empty"], True)

    def test_same_position_replacement_cannot_use_destination_only(self):
        before = frame([cell(3), cell()], [cell(), cell()])
        after = frame([cell(3), cell()], [cell(3), cell()])
        runner, desktop = self.automatic_runner([before, before.copy(), after])
        with patch("runtime.LAYOUT_READBACK_SECONDS", 0), patch("runtime.logging.info"):
            with self.assertRaisesRegex(RuntimeError, "Перенос не подтверждён"):
                runner.once(sort=False)
        self.assertEqual(sum(kind == "drag" for kind, _ in desktop.inputs), 1)
        self.assertTrue(runner._transfer_input_issued)

    def test_unrelated_storage_occupancy_change_stays_unverified(self):
        before = frame([cell(3), cell()], [cell(), cell()])
        after = frame([cell(), cell(3)], [cell(3), cell(4)])
        runner, desktop = self.automatic_runner([before, before.copy(), after])
        with patch("runtime.LAYOUT_READBACK_SECONDS", 0), patch("runtime.logging.info"):
            with self.assertRaisesRegex(RuntimeError, "Перенос не подтверждён"):
                runner.once(sort=False)
        self.assertEqual(sum(kind == "drag" for kind, _ in desktop.inputs), 1)
        self.assertTrue(runner._transfer_input_issued)

    def test_physical_destination_matcher_uses_storage_dimensions_and_rejects_weak_evidence(self):
        before = frame([cell(3), cell()], [cell(), cell()])
        after = frame([cell(), cell(3)], [cell(3), cell()])
        runner, desktop = self.automatic_runner([])
        inv, store = runner.scans(before)
        after_inv, after_store = runner.scans(after)
        candidate = choose_transfer(inv, store)
        destination = after_store.cells[candidate.destination_index]
        # Isolate the second classifier from these deliberately synthetic
        # badges. Native storage pixels have an independent saved replay.
        unknown = replace(destination, tier=None, rmse=25., margin=1.)
        cases = [(3, 2., 20., True), (4, 2., 20., False),
                 (3, 15., 20., False), (3, 2., 4., False),
                 (3, float('nan'), 20., False), (None, 2., 20., False)]
        for tier, rmse, margin, expected in cases:
            with self.subTest(tier=tier, rmse=rmse, margin=margin):
                matcher = SimpleNamespace(match=Mock(return_value=SimpleNamespace(
                    tier=tier, rmse=rmse, margin=margin, reason='explicit test matcher')))
                runner.adaptive_tier_matcher = matcher
                with patch('runtime.classify_cell', return_value=unknown):
                    result, observed = runner._verify_transfer_readback(
                        after, inv, after_inv, store, after_store, candidate)
                self.assertEqual(result.ok, expected)
                self.assertEqual(matcher.match.call_args.args[1], runner.layout_scales['storage'])
                reference = runner.canonical_store.cell_rect(destination.index)
                self.assertEqual(matcher.match.call_args.kwargs['canonical_size'],
                                 (reference.width, reference.height))
                self.assertEqual(matcher.match.call_args.args[0].tobytes(),
                                 after.crop(destination.rect.as_box()).tobytes())
                self.assertEqual(observed.cells[destination.index].tier, 3 if expected else None)
        self.assertEqual(desktop.inputs, [])

    def test_known_conflicting_destination_is_not_overridden_by_physical_matcher(self):
        before = frame([cell(3), cell()], [cell(), cell()])
        after = frame([cell(), cell(3)], [cell(4), cell()])
        runner, _ = self.automatic_runner([])
        inv, store = runner.scans(before)
        after_inv, after_store = runner.scans(after)
        candidate = choose_transfer(inv, store)
        runner.adaptive_tier_matcher = SimpleNamespace(match=Mock())
        result, _ = runner._verify_transfer_readback(after, inv, after_inv, store, after_store, candidate)
        self.assertFalse(result.ok)
        runner.adaptive_tier_matcher.match.assert_not_called()
