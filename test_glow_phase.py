"""Brightness/saturation are not a licence to hide dark or unrelated pixels."""
import unittest
import numpy as np
from inventory_halo import _halo_chroma_pixels, _frame_pattern_matches, _frame_rail_matches


class GlowPhaseTests(unittest.TestCase):
    def setUp(self):
        self.background = np.array((106, 103, 97), dtype=np.float32)
        self.sample = np.full((30, 4, 3), (186, 123, 117), dtype=np.float32)

    def test_same_radial_hue_survives_brightness_and_saturation_pulse(self):
        for rgb in ((226, 123, 117), (146, 113, 107), (216, 153, 147)):
            with self.subTest(rgb=rgb):
                actual = np.full_like(self.sample, rgb)
                self.assertTrue(_halo_chroma_pixels(self.sample, actual, self.background).all())

    def test_dark_neutral_wrong_hue_and_extreme_gain_are_unproved(self):
        for rgb in ((20, 20, 20), (106, 103, 97), (106, 183, 177),
                    (146, 113, 187), (255, 103, 250)):
            with self.subTest(rgb=rgb):
                self.assertFalse(_halo_chroma_pixels(self.sample,
                    np.full_like(self.sample, rgb), self.background).any())

    def test_only_individually_matching_pixels_can_contribute(self):
        actual = self.sample.copy()
        actual[10:20] = (20, 20, 20)
        visible = _halo_chroma_pixels(self.sample, actual, self.background)
        self.assertTrue(visible[:10].all())
        self.assertFalse(visible[10:20].any())
        self.assertTrue(visible[20:].all())

    def test_frame_geometry_survives_glow_blending_but_not_flat_or_rearranged_pixels(self):
        rng = np.random.default_rng(17)
        sample = rng.uniform(50, 180, (36, 3, 3)).astype(np.float32)
        for gain in (.7, 1., 1.3):
            actual = (sample-sample.mean(axis=(0, 1)))*gain+sample.mean(axis=(0, 1))+(25, 8, 12)
            self.assertTrue(_frame_pattern_matches(sample, actual))
        for actual in (np.full_like(sample, sample.mean()), np.zeros_like(sample),
                       sample[::-1].copy(), sample+(60, 0, 0)):
            self.assertFalse(_frame_pattern_matches(sample, actual))

    def test_rgb_glow_field_keeps_each_metal_rail_cross_section(self):
        rng = np.random.default_rng(18)
        sample = rng.uniform(40, 170, (36, 3, 3)).astype(np.float32)
        centered = sample-sample.mean(axis=1, keepdims=True)
        illumination = np.linspace(0, 25, 36)[:, None, None]
        actual = centered*(.4, .85, .85)+sample.mean(axis=1, keepdims=True)+illumination+(20, 4, 5)
        self.assertTrue(_frame_rail_matches(sample, actual))
        for covered in (np.full_like(sample, 140), np.zeros_like(sample), sample[:, ::-1].copy()):
            self.assertFalse(_frame_rail_matches(sample, covered))


if __name__ == '__main__':
    unittest.main()
