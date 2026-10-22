"""Candidate positions are not separator evidence and cannot authorize input."""
import unittest
from auto_layout import LayoutError
from inventory_halo import _column_candidates,prove_inventory_halo_mask
from test_inventory_halo import generated_frames,GAP
import numpy as np


class ColumnCandidateTests(unittest.TestCase):
    def test_three_regular_raw_columns_suggest_missing_positions_only(self):
        observed=[(190,195),(255,260),(320,325)]
        result=_column_candidates(observed,325,5,1.)
        self.assertEqual(result[2:],observed)
        self.assertEqual(result[:2],[(62,63),(127,128)])

    def test_too_few_ambiguous_wrong_pitch_or_broad_columns_fail(self):
        for observed in ([(255,260),(320,325)],[(190,195),(192,197),(320,325)],
                         [(170,175),(240,245),(320,325)],[(180,195),(255,260),(320,325)]):
            with self.subTest(observed=observed),self.assertRaises(LayoutError):
                _column_candidates(observed,325,5,1.)

    def test_unproved_pixels_in_missing_columns_are_never_repaired(self):
        (image,),rect=generated_frames()
        image.paste('black',(rect.left+60,rect.top,rect.left+65,rect.bottom))
        image.paste('black',(rect.left+125,rect.top,rect.left+130,rect.bottom))
        pixels=np.asarray(image.crop(rect.as_box()),dtype=np.float32)
        raw=np.max(abs(pixels-GAP),axis=2)<=6
        saved=raw.copy()
        with self.assertRaises(LayoutError):
            prove_inventory_halo_mask(image,rect,8,5,GAP,1.,raw)
        np.testing.assert_array_equal(raw,saved)


if __name__=='__main__':unittest.main()
