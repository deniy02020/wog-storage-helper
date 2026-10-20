"""Small native-raster motion guards; public fixtures are synthetic textures."""
import json
from pathlib import Path
import unittest
import numpy as np
from PIL import Image
from auto_layout import GridEvidence,LayoutError,_measure_grid_mask
from core import GridSpec,Rect
from inventory_viewport import (InventoryViewport,recognize_inventory_viewport,
    prove_inventory_translation,InventoryMotionUnknown,_motion_raster)
from inventory_motion_policy import InventoryMotionUnobservable

GAP=(106,103,97)

def frames(offset=13, clones=False):
    rng=np.random.default_rng(4891320)
    pitch,width=27,135
    body=np.empty((pitch*20,width,3),dtype=np.uint8)
    clone=rng.integers(15,245,(pitch,width,3),dtype=np.uint8)
    for row in range(20):
        value=clone.copy() if clones else rng.integers(15,245,(pitch,width,3),dtype=np.uint8)
        value[:2]=(85,83,78);value[-5:-2]=(85,83,78);value[-2:]=GAP
        for col in range(5):
            value[:,col*27:col*27+2]=(85,83,78)
            value[:,(col+1)*27-5:(col+1)*27-2]=(85,83,78)
            value[:,(col+1)*27-2:(col+1)*27]=GAP
        body[row*pitch:(row+1)*pitch]=value
    viewport=Rect(20,20,155,236)
    result=[]
    for start in (0,offset):
        image=Image.new('RGB',(200,300))
        image.paste(Image.fromarray(body[start:start+216]),(20,20))
        result.append(image)
    views=[recognize_inventory_viewport(im,viewport,8,5,GAP,.4175) for im in result]
    return result,views


class SmallInventoryMotionTests(unittest.TestCase):
    def test_small_separator_needs_one_common_visible_line_across_all_cells(self):
        images,views=frames()
        rect=views[0].viewport
        mask=np.max(np.abs(np.asarray(images[0].crop(rect.as_box()),dtype=float)-GAP),axis=2)<=6
        mask[25,108:]=False
        _measure_grid_mask(mask,rect,8,5,.4175)
        # Independent per-cell cherry picking must not establish a border.
        mask[26,:27]=False
        with self.assertRaises(LayoutError):_measure_grid_mask(mask,rect,8,5,.4175)

    def test_popup_covering_whole_small_separator_still_rejects(self):
        images,views=frames();rect=views[0].viewport
        mask=np.max(np.abs(np.asarray(images[0].crop(rect.as_box()),dtype=float)-GAP),axis=2)<=6
        mask[25:27,108:]=False
        with self.assertRaises(LayoutError):_measure_grid_mask(mask,rect,8,5,.4175)

    def test_clipped_neutral_floor_cannot_replace_complete_grid_proof(self):
        images,views=frames(offset=13);image=images[1].copy();rect=views[1].viewport
        # Synthetic native-like bottom clip; only the excluded partial row
        # becomes neutral. Every complete row still needs its actual borders.
        image.paste(GAP,(rect.left,rect.bottom-8,rect.right,rect.bottom))
        observed=recognize_inventory_viewport(image,rect,8,5,GAP,.4175)
        self.assertLessEqual(observed.grid.rect.bottom,rect.bottom-8)
        bad=image.copy();bad.paste(GAP,(rect.left,rect.top+68,rect.right,rect.top+80))
        with self.assertRaises(LayoutError):recognize_inventory_viewport(bad,rect,8,5,GAP,.4175)
    def test_unique_small_texture_retains_direction_and_measured_distance(self):
        images,views=frames()
        self.assertAlmostEqual(prove_inventory_translation(*images,*views,'down',pixel_step=True),13,delta=.75)
        with self.assertRaises(InventoryMotionUnknown):
            prove_inventory_translation(*images,*views,'up',pixel_step=True)

    def test_cloned_small_rows_still_cannot_calibrate(self):
        images,views=frames(clones=True)
        with self.assertRaises(InventoryMotionUnobservable):
            prove_inventory_translation(*images,*views,'down',pixel_step=True)

    def test_whole_row_alias_cannot_become_a_subrow_command(self):
        images,views=frames(offset=40)
        with self.assertRaises(InventoryMotionUnknown):
            prove_inventory_translation(*images,*views,'down',pixel_step=True)

    def test_bounded_kernels_keep_all_rgb_pixels_and_reject_unapproved_widths(self):
        pixels=np.random.default_rng(1).random((90,135,3)).astype(np.float32)*255
        for sigma in (1.5,2):
            self.assertEqual(_motion_raster(pixels,sigma).shape,pixels.shape)
        for sigma in (0,3,float('nan')):
            with self.assertRaises(ValueError):_motion_raster(pixels,sigma)


class PrivateSmallNativeMotionTests(unittest.TestCase):
    def test_actual_small_bottom_clip_proves_only_complete_rows(self):
        root=Path(__file__).parent/'verification/language-independent-20261020/normal-cycle-small-r3/last-observed.png'
        if not root.exists():self.skipTest('Private complete native game capture is not distributed')
        from auto_layout import AutoLayout
        assets=Path(__file__).parent/'preset_assets';profile=json.loads((assets/'reference.json').read_bytes())
        profile['pages']=[{'id':p['id'],'button':p['button_rect'],'marker':p['marker_rect']} for p in profile['pages']]
        layout=AutoLayout(profile,assets);image=Image.open(root).convert('RGB')
        adopted=layout.resolve(image);layout.validate_current(image,adopted)
        self.assertEqual(adopted['inventory'],{'rect':[372,263,507,451],'rows':7,'columns':5})

    def test_actual_small_item_halo_leaves_a_complete_independent_separator(self):
        root=Path(__file__).parent/'verification/language-independent-20261020/normal-cycle-small/last-observed.png'
        if not root.exists():self.skipTest('Private complete native game capture is not distributed')
        from auto_layout import AutoLayout
        assets=Path(__file__).parent/'preset_assets'
        profile=json.loads((assets/'reference.json').read_bytes())
        profile['pages']=[{'id':p['id'],'button':p['button_rect'],'marker':p['marker_rect']} for p in profile['pages']]
        layout=AutoLayout(profile,assets);image=Image.open(root).convert('RGB')
        adopted=layout.resolve(image);layout.validate_current(image,adopted)

    def test_actual_small_game_reraster_preserves_original_score_and_alias_limits(self):
        root=Path(__file__).parent/'verification/language-independent-20261020/motion-pair-50'
        if not (root/'report.json').exists():
            self.skipTest('Private complete native game captures are not distributed')
        report=json.loads((root/'report.json').read_bytes())
        self.assertGreater(report['scores']['True']['best'][0],5)
        self.assertGreater(report['scores']['True']['margin'],5)
        views=[]
        for name in ('before','after'):
            v=report[name];g=v['grid'];e=v['evidence']
            views.append(InventoryViewport(GridSpec(Rect(**g['rect']),g['rows'],g['columns']),
                Rect(**v['viewport']),v['row_pitch'],v['phase'],v['identity'],
                GridEvidence(e['pitch_x'],e['pitch_y'],tuple(map(tuple,e['vertical_stripes'])),tuple(map(tuple,e['horizontal_stripes'])))))
        images=[Image.open(root/(name+'.png')).convert('RGB') for name in ('before','after')]
        self.assertEqual(prove_inventory_translation(*images,*views,'down',pixel_step=True),13)
        self.assertEqual(prove_inventory_translation(images[1],images[0],views[1],views[0],'up',pixel_step=True),13)
        with self.assertRaises(InventoryMotionUnknown):
            prove_inventory_translation(*images,*views,'up',pixel_step=True)

if __name__=='__main__':unittest.main()
