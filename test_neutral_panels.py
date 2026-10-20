"""Offline evidence for language-free landmarks; compositions are synthetic."""
import copy
import json
from pathlib import Path
import unittest
from PIL import Image
from auto_layout import AutoLayout, LayoutError, ScaleMatch, affine_rect
from core import Rect
from neutral_panels import load_models, locate, validate, verify_model, locate_storage_opener

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / 'preset_assets'


class NeutralPanelTests(unittest.TestCase):
    def test_portrait_hover_keeps_fixed_size_pointer_glow_away_from_grids(self):
        from panels import PanelController
        for scale in (.4175, .7075, 1.0):
            anchor = Rect(200,200,200+round(233*scale),200+round(41*scale))
            point=PanelController._portrait_hover_point(anchor,scale,(1800,1800))
            grid_left=anchor.left+round(428*scale)
            self.assertGreater(grid_left-point[0], 100)
            self.assertGreater(point[1]-anchor.bottom, 40)

    def compose(self, spec, both=True):
        frame = Image.new('RGB', (1000, 1000), (7, 13, 21))
        source = Rect(*spec['source_anchor_rect'])
        anchor = Rect(80, 60, 80+source.width, 60+source.height)
        for patch in spec['patches'][:2 if both else 1]:
            rect = affine_rect(Rect(*patch['source_rect']), source, ScaleMatch(anchor, 1, 0, 0))
            frame.paste(patch['sample'], rect.as_box())
        return frame, anchor

    def test_real_native_icon_pairs_have_no_caption_dependency_at_three_sizes(self):
        for spec in load_models():
            with self.subTest(spec=spec['id']):
                frame, anchor = self.compose(spec)
                sample = Image.open(ASSETS/(spec['pane']+'_header.png')).convert('RGB')
                found = locate(frame, sample, spec['pane'], (spec['canonical_scale'],))
                self.assertIsNotNone(found)
                self.assertEqual(found.rect, anchor)
                self.assertEqual(found.scale, spec['canonical_scale'])
                validate(frame, sample, spec['pane'], found.rect, found.scale, found.phase, found.variant_id)

    def test_a_single_matching_icon_is_insufficient(self):
        for pane in ('inventory', 'storage'):
            spec = next(r for r in load_models() if r['pane']==pane and r['canonical_scale']==1)
            frame, anchor = self.compose(spec, both=False)
            sample = Image.open(ASSETS/(pane+'_header.png')).convert('RGB')
            self.assertIsNone(locate(frame, sample, pane, (1,)))

    def test_occluded_second_icon_fails_fresh_validation(self):
        spec = load_models()[0]
        frame, anchor = self.compose(spec, both=False)
        sample = Image.open(ASSETS/'inventory_header.png').convert('RGB')
        with self.assertRaises(LayoutError):
            validate(frame, sample, 'inventory', anchor, 1, (0,0), spec['id'])

    def test_duplicate_panels_are_rejected(self):
        spec = load_models()[0]
        frame, _ = self.compose(spec)
        duplicated = Image.new('RGB', (2000,1000), (7,13,21))
        duplicated.paste(frame,(0,0)); duplicated.paste(frame,(1000,0))
        sample = Image.open(ASSETS/'inventory_header.png').convert('RGB')
        with self.assertRaises(LayoutError):
            locate(duplicated, sample, 'inventory', (1,))

    def test_resource_tampering_and_coordinate_changes_are_rejected(self):
        original = json.loads((ASSETS/'neutral_panels.json').read_bytes())
        for kind in ('path','offset','hash','scale'):
            value = copy.deepcopy(original)
            record = value['records'][0]
            if kind=='path': record['patches'][0]['image']='../outside.png'
            if kind=='offset': record['patches'][0]['source_rect'][0]+=4
            if kind=='hash': record['patches'][0]['sample_sha256']='0'*64
            if kind=='scale': record['canonical_scale']=True
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                verify_model(value,lambda name:(ASSETS/name).read_bytes())

    def test_house_opener_ignores_caption_and_rejects_custom_button(self):
        sample = Image.open(ASSETS/'panel_storage_open.png').convert('RGB')
        frame = Image.new('RGB',(600,300),(7,13,21))
        frame.paste(sample.crop((0,0,29,sample.height)),(150,100))
        # Deliberately no title pixels: any translated caption is irrelevant.
        self.assertEqual(locate_storage_opener(frame,sample,10),Rect(150,100,179,131))
        self.assertIs(locate_storage_opener(frame,Image.new('RGB',(142,31)),10),NotImplemented)


class PrivateNativeLanguageTests(unittest.TestCase):
    def test_actual_english_frame_proves_both_grids_and_fresh_layout(self):
        path = ROOT/'verification/language-independent-20261020/English.png'
        if not path.exists():
            self.skipTest('Private full native game capture is not distributed')
        profile=json.loads((ASSETS/'reference.json').read_bytes())
        profile['pages']=[{'id':p['id'],'button':p['button_rect'],'marker':p['marker_rect']} for p in profile['pages']]
        layout=AutoLayout(profile,ASSETS)
        frame=Image.open(path).convert('RGB')
        adopted=layout.resolve(frame)
        self.assertEqual(adopted['layout_anchor_renderers'],{'inventory':'neutral','storage':'neutral'})
        self.assertEqual(adopted['inventory']['rect'],[890,588,1215,1108])
        layout.validate_current(frame,adopted)
        # A text-free anchor must never substitute for actual grid evidence.
        occluded=frame.copy(); occluded.paste((0,0,0),adopted['storage']['rect'])
        with self.assertRaises(LayoutError): layout.validate_current(occluded,adopted)


if __name__=='__main__': unittest.main()
