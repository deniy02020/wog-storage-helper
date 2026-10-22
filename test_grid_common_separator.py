"""Whole-stripe proof must use one common observed line across every cell."""
import unittest
import numpy as np
from auto_layout import _measure_grid_mask,LayoutError
from core import Rect


class CommonSeparatorTests(unittest.TestCase):
    def grid(self):
        mask=np.zeros((195,195),dtype=bool)
        for edge in (65,130,195):
            mask[:,edge-5:edge]=True
            mask[edge-5:edge,:]=True
        return mask

    def test_visible_neighbor_line_proves_stripe_at_full_scale(self):
        for horizontal in (False,True):
            mask=self.grid()
            mask[10:55,62]=False
            if horizontal:mask=mask.T
            evidence=_measure_grid_mask(mask,Rect(0,0,195,195),3,3,1.)
            self.assertAlmostEqual(evidence.pitch_x,65.)
            self.assertAlmostEqual(evidence.pitch_y,65.)

    def test_different_lines_per_cell_cannot_hide_an_occlusion(self):
        for horizontal in (False,True):
            mask=self.grid()
            for offset,x in enumerate(range(60,65)):
                row=offset%3
                mask[row*65+10:row*65+40,x]=False
            if horizontal:mask=mask.T
            with self.assertRaises(LayoutError):
                _measure_grid_mask(mask,Rect(0,0,195,195),3,3,1.)

    def test_occluded_full_stripe_in_one_cell_still_fails(self):
        mask=self.grid();mask[10:55,60:65]=False
        with self.assertRaises(LayoutError):
            _measure_grid_mask(mask,Rect(0,0,195,195),3,3,1.)


if __name__=='__main__':unittest.main()
