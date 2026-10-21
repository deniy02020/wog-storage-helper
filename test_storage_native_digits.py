"""Small native page glyph regression; no game capture, input or private files."""
import unittest
import numpy as np
from storage_pages import _digit,StoragePagesUnknown

def mask(rows):
    return np.array([[p=='#' for p in row] for row in rows],dtype=bool)

class NativePageDigitTests(unittest.TestCase):
    def test_authentic_small_selected_three_and_inactive_five_are_distinct(self):
        cases={3:('..#..','.####','##.##','..###','..###','##.##','.####'),
               5:('####','##..','####','#..#','#..#','####')}
        for expected,rows in cases.items():
            observed=mask(rows)
            self.assertGreaterEqual(_digit(observed,expected),.77)
            for wrong in set(range(1,7))-{expected}:
                with self.subTest(expected=expected,wrong=wrong),self.assertRaises(StoragePagesUnknown):
                    _digit(observed,wrong)

    def test_blank_and_solid_markers_do_not_prove_a_page_number(self):
        for observed in (np.zeros((7,5),dtype=bool),np.ones((7,5),dtype=bool)):
            for expected in range(1,7):
                with self.subTest(expected=expected),self.assertRaises(StoragePagesUnknown):
                    _digit(observed,expected)

    def test_open_lower_bowl_cannot_authorize_page_six(self):
        for rows in (('.###.','##...','##...','##...','##...','.###.'),
                     ('.###.','##...','##.##','##..#','##..#','.###.')):
            with self.assertRaises(StoragePagesUnknown):_digit(mask(rows),6)

if __name__=='__main__':unittest.main()
