"""Whole-message visual fallback boundaries, independent of OCR confidence."""
import base64
import json
from pathlib import Path
import unittest
import numpy as np
from PIL import Image
from connection_vision import DisconnectRecognizer, full_text_similarity
from core import Rect


class WholeTextReferenceTests(unittest.TestCase):
    def setUp(self):
        record = json.loads((Path(__file__).parent/'disconnect_visual.json').read_bytes())['references'][0]
        self.regions = {}
        self.image = Image.new('RGB',(320,220),(214,210,198))
        for kind, y in [('title',10),('body',70)]:
            field = record['regions'][kind]
            w,h = field['width'],field['height']
            pixels = np.unpackbits(np.frombuffer(base64.b64decode(field['bits']),dtype='uint8'))[:w*h].reshape(h,w)
            gray = np.where(pixels,255 if kind=='title' else 40,50 if kind=='title' else 214).astype('uint8')
            self.image.paste(Image.fromarray(gray).convert('RGB'),(20,y))
            self.regions[kind] = Rect(20,y,20+w,y+h)
        self.recognizer = DisconnectRecognizer()

    def tearDown(self):
        self.recognizer.close()

    def prove(self,image):
        return self.recognizer._visual_locale(image,self.regions['title'],self.regions['body'])

    def test_both_complete_fixed_texts_match_without_general_ocr(self):
        self.assertEqual(self.prove(self.image),'zh-TW')

    def test_title_alone_does_not_authorize_input(self):
        image=self.image.copy()
        image.paste((214,210,198),self.regions['body'].as_box())
        self.assertIsNone(self.prove(image))

    def test_body_alone_does_not_authorize_input(self):
        image=self.image.copy()
        image.paste((50,50,50),self.regions['title'].as_box())
        self.assertIsNone(self.prove(image))

    def test_incomplete_body_is_rejected_even_with_exact_title(self):
        image=self.image.copy();r=self.regions['body']
        image.paste((214,210,198),(r.left,r.top,r.left+r.width//2,r.bottom))
        self.assertIsNone(self.prove(image))

    def test_generic_title_and_empty_body_do_not_match(self):
        self.assertIsNone(self.prove(Image.new('RGB',self.image.size,'gray')))

    def test_blank_or_partial_ink_is_not_background_similarity(self):
        full=np.ones((20,100),dtype='uint8')
        self.assertEqual(full_text_similarity(full,np.zeros_like(full)),0)
        partial=full.copy();partial[:,:50]=0
        self.assertEqual(full_text_similarity(full,partial),0)

    def test_alignment_tolerance_does_not_accept_reversed_message(self):
        self.assertIsNone(self.prove(self.image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)))

    def test_changed_reference_catalog_cannot_authorize_input(self):
        self.recognizer.messages['zh-TW']['body']='An unrelated purchase prompt'
        with self.assertRaisesRegex(ValueError,'catalog text mismatch'):
            self.prove(self.image)


class WholeRewardReferenceTests(unittest.TestCase):
    def setUp(self):
        record = json.loads((Path(__file__).parent/'disconnect_visual.json').read_bytes())['references'][1]
        self.bounds = {}
        self.image = Image.new('RGB',(320,150),(50,50,50))
        for role,x,y in [('reward_title',20,10),('reward_last_login',20,70),('reward_duration',140,70)]:
            field=record['regions'][role];w,h=field['width'],field['height']
            bits=np.unpackbits(np.frombuffer(base64.b64decode(field['bits']),dtype='uint8'))[:w*h].reshape(h,w)
            pixels=np.where(bits,255,50).astype('uint8')
            self.image.paste(Image.fromarray(pixels).convert('RGB'),(x,y))
            self.bounds[role]=Rect(x,y,x+w,y+h)
        self.recognizer=DisconnectRecognizer()

    def tearDown(self):
        self.recognizer.close()

    def test_all_three_captions_are_required(self):
        self.assertEqual(self.recognizer._visual_match(self.image,self.bounds,'reward'),'zh-TW')
        for role,region in self.bounds.items():
            with self.subTest(role=role):
                image=self.image.copy();image.paste((50,50,50),region.as_box())
                self.assertIsNone(self.recognizer._visual_match(image,self.bounds,'reward'))

    def test_reward_reference_cannot_prove_disconnect_dialog(self):
        self.assertIsNone(self.recognizer._visual_match(self.image,
            {'title':self.bounds['reward_title'],'body':self.bounds['reward_duration']},'disconnect'))


class EveryLocaleCaptionBoundaryTests(unittest.TestCase):
    def test_every_catalog_reference_requires_all_its_complete_captions(self):
        records=json.loads((Path(__file__).parent/'disconnect_visual.json').read_bytes())['references']
        for record in records:
            with self.subTest(locale=record['locale'],phase=record['kind']):
                image=Image.new('RGB',(1700,1200),(50,50,50))
                bounds={}
                y=10
                for role,field in record['regions'].items():
                    w,h=field['width'],field['height']
                    bits=np.unpackbits(np.frombuffer(base64.b64decode(field['bits']),dtype='uint8'))[:w*h].reshape(h,w)
                    foreground,background=(40,214) if role=='body' else (255,50)
                    image.paste(Image.fromarray(np.where(bits,foreground,background).astype('uint8')).convert('RGB'),(10,y))
                    bounds[role]=Rect(10,y,10+w,y+h)
                    y+=h+10
                recognizer=DisconnectRecognizer()
                try:
                    self.assertEqual(recognizer._visual_match(image,bounds,record['kind']),record['locale'])
                    for role,region in bounds.items():
                        with self.subTest(missing_caption=role):
                            damaged=image.copy()
                            damaged.paste((214,214,214) if role=='body' else (50,50,50),region.as_box())
                            self.assertIsNone(recognizer._visual_match(damaged,bounds,record['kind']))
                finally:
                    recognizer.close()


if __name__=='__main__':
    unittest.main()
