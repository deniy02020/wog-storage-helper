"""Bundled narrow ring proof on synthetic pixels; no desktop or OS input."""
import copy
from io import BytesIO
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from auto_layout import LayoutError, measure_grid
from core import Rect
from inventory_halo import (CAP_OFFSETS, FULL_OFFSETS, InventoryHaloModels,prove_inventory_halo_mask,
                            verify_inventory_halo_resources)
from inventory_viewport import recognize_inventory_viewport, _lattice


ASSETS = Path(__file__).parent / "preset_assets"
GAP = (106, 103, 97)


def generated_frames(scale=1., offsets=(0,), *, pitch=65):
    """Explicitly synthetic repeated chrome; no native image or UI capture."""
    rng = np.random.default_rng(4891320)
    pixels = rng.integers(15, 85, (pitch*20, 325, 3), dtype=np.uint8)
    for row in range(20):
        tile = pixels[row*pitch:(row+1)*pitch]
        tile[:4] = (85, 83, 78)
        tile[pitch-9:pitch-5] = (85, 83, 78)
        tile[pitch-5:pitch] = GAP
        for column in range(5):
            tile[:, column*65:column*65+4] = (85, 83, 78)
            tile[:, (column+1)*65-9:(column+1)*65-5] = (85, 83, 78)
            tile[:, (column+1)*65-5:(column+1)*65] = GAP
    body = Image.fromarray(pixels).resize((round(325*scale), round(pitch*20*scale)),
                                         Image.Resampling.BILINEAR)
    rect = Rect(71, 53, 71+round(325*scale), 53+round(520*scale))
    frames = []
    for offset in offsets:
        image = Image.new("RGB", (850, 850), (17, 21, 29))
        top = round(offset*scale)
        image.paste(body.crop((0, top, rect.width, top+rect.height)), (rect.left, rect.top))
        frames.append(image)
    return frames, rect


class InventoryHaloProofTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.models = InventoryHaloModels(ASSETS)
        cls.document = json.loads((ASSETS / "inventory_halos.json").read_text(encoding="utf-8"))
        cls.primary_full_count = sum(m["kind"]=="full" for m in cls.document["models"])

    def scene(self, *, with_frame=True, offset=0, model_index=0):
        (image,), rect = generated_frames(offsets=(offset,))
        origin = rect.left + 4*65, rect.top + 4*65-offset
        # Genuine bundled narrow lanes painted onto an explicitly SYNTHETIC
        # grid. No item icon, label, game screenshot or native result is made.
        model = self.models.models["full"][model_index]
        for label, sample in model.items():
            if label.startswith("frame_") and not with_frame:
                continue
            left, top, _, _ = FULL_OFFSETS[label]
            image.paste(sample, (origin[0]+left, origin[1]+top))
        return image, rect, origin

    def prove(self, image, rect, **kwargs):
        return measure_grid(image, rect, 8, 5, GAP, 1., allow_inventory_halo=True, **kwargs)

    def two_halo_scene(self, *, column=4, scale=1., offset=0):
        # Explicitly synthetic composition of two already authenticated cues.
        # Their separate glow lanes can trim a long vertical projection without
        # removing its run, while untouched row segments still prove its center.
        (image,), rect = generated_frames(scale=scale, offsets=(offset,))
        origins = [(rect.left+round(column*65*scale), rect.top+round((row*65-offset)*scale))
                   for row in (2, 5)]
        for origin in origins:
            for label, sample in self.models.models["full"][0].items():
                left, top, right, bottom = (round(value*scale) for value in FULL_OFFSETS[label])
                patch_image = sample.resize((right-left, bottom-top), Image.Resampling.BILINEAR)
                image.paste(patch_image, (origin[0]+left, origin[1]+top))
        return image, rect

    def test_two_authenticated_halos_keep_all_same_column_separator_proofs(self):
        for column in (2, 4):
            with self.subTest(column=column):
                image, rect = self.two_halo_scene(column=column)
                with self.assertRaises(LayoutError):
                    measure_grid(image, rect, 8, 5, GAP, 1.)
                evidence = self.prove(image, rect)
                self.assertAlmostEqual(evidence.pitch_x, 65.)
                self.assertAlmostEqual(evidence.pitch_y, 65.)
                self.assertEqual(len(evidence.vertical_stripes), 5)
                self.assertEqual(len(evidence.horizontal_stripes), 8)
                self.assertEqual(recognize_inventory_viewport(image, rect, 8, 5, GAP, 1.).evidence,
                                 evidence)

    def test_two_halo_candidate_keeps_scaled_and_shifted_complete_row_guards(self):
        for scale, offset in ((.5, 0), (.75, 0), (1.25, 0), (1., 13), (1., 31)):
            with self.subTest(scale=scale, offset=offset):
                image, rect = self.two_halo_scene(scale=scale, offset=offset)
                viewport = recognize_inventory_viewport(image, rect, 8, 5, GAP, scale)
                self.assertEqual(viewport.grid.rows, 7 if offset else 8)
                self.assertEqual(measure_grid(image, viewport.grid.rect, viewport.grid.rows, 5,
                                             GAP, scale, allow_inventory_halo=True), viewport.evidence)

    def test_two_halo_candidate_never_excuses_missing_separator_or_unrelated_opaque_pixels(self):
        for corruption in ("rail", "horizontal", "vertical", "opaque"):
            with self.subTest(corruption=corruption), self.assertRaises(LayoutError):
                image, rect = self.two_halo_scene()
                if corruption == "rail":
                    image.paste("black", (rect.left+4*65, rect.top+2*65+12,
                                           rect.left+4*65+3, rect.top+2*65+48))
                elif corruption == "horizontal":
                    image.paste("black", (rect.left, rect.top+60, rect.left+65, rect.top+65))
                elif corruption == "vertical":
                    image.paste("black", (rect.left+60, rect.top+15, rect.left+65, rect.top+44))
                else:
                    image.paste((209, 127, 130), (rect.left+4*65-20, rect.top+2*65-4,
                                                 rect.left+4*65+64, rect.top+2*65+80))
                self.prove(image, rect)

    def test_two_halo_candidate_cannot_repair_wrong_pitch_grid(self):
        image, rect = self.two_halo_scene()
        for changed in (Rect(rect.left, rect.top, rect.right-13, rect.bottom),
                        Rect(rect.left, rect.top, rect.right, rect.bottom-25)):
            with self.subTest(rect=changed), self.assertRaises(LayoutError):
                self.prove(image, changed)

    def test_raw_guard_still_rejects_halo_and_owned_frame_plus_gutters_proves_same_grid(self):
        image, rect, _ = self.scene()
        with self.assertRaises(LayoutError):
            measure_grid(image, rect, 8, 5, GAP, 1.)
        evidence = self.prove(image, rect)
        self.assertEqual(len(evidence.vertical_stripes), 5)
        self.assertEqual(len(evidence.horizontal_stripes), 8)
        self.assertAlmostEqual(evidence.pitch_y, 65., delta=1.)

    def test_additional_bundled_raster_keeps_original_lattice_and_requires_opaque_rails(self):
        # Any newly approved border-only raster uses the SAME proof contract;
        # this test has no native capture path or private provenance filename.
        image, rect, origin = self.scene(model_index=self.primary_full_count-1)
        with self.assertRaises(LayoutError):
            measure_grid(image, rect, 8, 5, GAP, 1.)
        evidence = self.prove(image, rect)
        self.assertEqual(len(evidence.vertical_stripes), 5)
        self.assertEqual(len(evidence.horizontal_stripes), 8)
        image.paste("black", (origin[0]-1, origin[1]+12, origin[0]+4, origin[1]+48))
        with self.assertRaises(LayoutError):
            self.prove(image, rect)

    def test_additional_raster_does_not_accept_flat_pink_or_an_unrelated_occlusion(self):
        image, rect, origin = self.scene(model_index=-1)
        # Halo-colored regions without the simultaneously matched opaque
        # frame rails remain arbitrary overlays, even using genuine RGB.
        image.paste((209, 127, 130), (origin[0]-20, origin[1]-4, origin[0]+64, origin[1]+80))
        with self.assertRaises(LayoutError):
            self.prove(image, rect)
        image, rect, _ = self.scene(model_index=-1)
        image.paste("black", (rect.left, rect.top+60, rect.left+65, rect.top+65))
        with self.assertRaises(LayoutError):
            self.prove(image, rect)

    def test_plain_grid_uses_identical_raw_proof_without_loading_halo_resources(self):
        (image,), rect = generated_frames(offsets=(0,))
        original = measure_grid(image, rect, 8, 5, GAP, 1.)
        with patch("inventory_halo.default_models", side_effect=AssertionError("Raw proof is sufficient")):
            self.assertEqual(self.prove(image, rect), original)

    def test_shifted_halo_uses_complete_rows_after_independent_whole_viewport_phase(self):
        for offset in (13, 31):
            with self.subTest(offset=offset):
                image, rect, _ = self.scene(offset=offset)
                viewport = recognize_inventory_viewport(image, rect, 8, 5, GAP, 1.)
                self.assertEqual(viewport.grid.rows, 7)
                self.assertGreater(viewport.grid.rect.top, rect.top)
                self.assertLessEqual(viewport.grid.rect.bottom, rect.bottom)
                self.assertEqual(measure_grid(image, viewport.grid.rect, 7, 5, GAP, 1.,
                    allow_inventory_halo=True), viewport.evidence)

    def tight_pitch_scene(self):
        # Independent synthetic raster with a fractional repeated pitch; its
        # complete seven-row crop has less spare neutral gutter area.
        rect = Rect(71, 53, 396, 573)
        image = Image.new("RGB", (850, 850), (85, 83, 78))
        for a, b in ((60,64), (125,129), (189,193), (254,258), (319,324)):
            image.paste(GAP, (rect.left+a, rect.top, rect.left+b, rect.bottom))
        for a, b in ((59,63), (124,128), (188,192), (253,257), (317,321),
                     (382,386), (446,450), (511,515)):
            image.paste(GAP, (rect.left, rect.top+a, rect.right, rect.top+b))
        for kind, origin, offsets in (("full", (rect.left+259,rect.top+257), FULL_OFFSETS),
                                      ("cap", (rect.left+259,rect.top+515), CAP_OFFSETS)):
            for label, sample in self.models.models[kind][0].items():
                x,y,_,_ = offsets[label]
                image.paste(sample, (origin[0]+x,origin[1]+y))
        return image, rect

    def test_short_shifted_crop_uses_intact_column_segments_then_every_repaired_segment(self):
        original, rect = self.tight_pitch_scene()
        for offset in (10,20,32):
            with self.subTest(offset=offset):
                image = original.copy()
                pixels = np.roll(np.asarray(original.crop(rect.as_box())), -offset, axis=0)
                image.paste(Image.fromarray(pixels), (rect.left,rect.top))
                viewport = recognize_inventory_viewport(image, rect, 8, 5, GAP, 1.)
                self.assertEqual(viewport.grid.rows, 7)
                # Ordinary whole-column proof fails; observed untouched rows
                # establish positions, and only matched frame lanes repair it.
                with self.assertRaises(LayoutError):
                    measure_grid(image, viewport.grid.rect, 7, 5, GAP, 1.)
                self.assertEqual(measure_grid(image, viewport.grid.rect, 7, 5, GAP, 1.,
                    allow_inventory_halo=True), viewport.evidence)

    def test_three_intact_rows_cannot_excuse_an_unrelated_long_column_occlusion(self):
        image, rect, _ = self.scene()
        image.paste("black", (rect.left+59,rect.top,rect.left+66,rect.top+3*65))
        with self.assertRaises(LayoutError):
            self.prove(image,rect)

    def test_matching_halo_colors_without_opaque_frame_rails_are_not_proof(self):
        image, rect, _ = self.scene(with_frame=False)
        with self.assertRaises(LayoutError):
            self.prove(image, rect)

    def test_broad_uniform_block_with_the_exact_approved_pink_rgb_is_not_proof(self):
        image, rect, origin = self.scene()
        rgb = self.models.models["full"][0]["halo_top"].getpixel((40, 1))
        image.paste(rgb, (origin[0]-20, origin[1]-12, origin[0]+64, origin[1]+74))
        with self.assertRaises(LayoutError):
            self.prove(image, rect)

    def test_unrelated_horizontal_occlusion_cannot_hide_behind_an_authentic_halo(self):
        image, rect, _ = self.scene()
        image.paste("black", (rect.left, rect.top+59, rect.left+65, rect.top+66))
        with self.assertRaises(LayoutError):
            self.prove(image, rect)

    def test_unrelated_vertical_occlusion_cannot_hide_behind_an_authentic_halo(self):
        image, rect, _ = self.scene()
        image.paste("black", (rect.left+59, rect.top+15, rect.left+66, rect.top+44))
        with self.assertRaises(LayoutError):
            self.prove(image, rect)

    def test_corrupted_opaque_frame_rail_rejects_the_gutter_exception(self):
        image, rect, origin = self.scene()
        image.paste("black", (origin[0]-1, origin[1]+12, origin[0]+4, origin[1]+48))
        with self.assertRaises(LayoutError):
            self.prove(image, rect)

    def test_uniform_and_wrong_pitch_panels_do_not_gain_a_grid(self):
        image, rect, _ = self.scene()
        image.paste(GAP, rect.as_box())
        with self.assertRaises(LayoutError):
            self.prove(image, rect)
        (wrong,), rect = generated_frames(offsets=(0,), pitch=58)
        with self.assertRaises(LayoutError):
            self.prove(wrong, rect)

    def test_changed_scale_or_physical_rectangle_cannot_reuse_the_halo_proof(self):
        image, rect, _ = self.scene()
        with self.assertRaises(LayoutError):
            measure_grid(image, rect, 8, 5, GAP, .5, allow_inventory_halo=True)
        with self.assertRaises(LayoutError):
            self.prove(image, Rect(rect.left+8, rect.top, rect.right+8, rect.bottom))

    def test_halo_exception_is_not_available_to_storage_or_other_grid_shapes(self):
        image, rect, _ = self.scene()
        for rows, cols in ((2, 2), (7, 6), (6, 5), (9, 5)):
            with self.subTest(rows=rows, cols=cols), self.assertRaises(LayoutError):
                measure_grid(image, rect, rows, cols, GAP, 1., allow_inventory_halo=True)

    def test_every_bundled_lane_authenticates_without_a_user_profile(self):
        names = verify_inventory_halo_resources(self.document, lambda name: (ASSETS/name).read_bytes())
        self.assertEqual(names, set(self.models.samples))
        self.assertEqual(len(names), len(self.document["records"]))
        self.assertTrue(all(min(sample.size) <= 4 and max(sample.size) <= 100
                            for sample in self.models.samples.values()))

    def test_manifest_bad_types_geometry_names_dimensions_and_hash_are_rejected(self):
        mutations = []
        for key, value in (("full_offsets", {}), ("max_patch_rmse", 11)):
            item = copy.deepcopy(self.document); item[key] = value; mutations.append(item)
        for key, value in (("image", "../inventory_halo_T4_000.png"),
                           ("image", "inventory_halo_T4_000:stream.png"),
                           ("dimensions", [100, 100]), ("sha256", "0"*64)):
            item = copy.deepcopy(self.document); item["records"][0][key] = value; mutations.append(item)
        item = copy.deepcopy(self.document); item["records"][0] = None; mutations.append(item)
        item = copy.deepcopy(self.document); item["models"][0] = None; mutations.append(item)
        item = copy.deepcopy(self.document); item["models"][0]["canonical_scale"] = True; mutations.append(item)
        for data in [None, [], *mutations]:
            with self.subTest(data_type=type(data).__name__), self.assertRaises(ValueError):
                verify_inventory_halo_resources(data, lambda name: (ASSETS/name).read_bytes())

    def test_non_png_data_cannot_be_accepted_with_an_updated_file_checksum(self):
        import hashlib
        item = copy.deepcopy(self.document)
        name = item["records"][0]["image"]
        stream = BytesIO(); self.models.samples[name].save(stream, format="BMP")
        content = stream.getvalue(); item["records"][0]["sha256"] = hashlib.sha256(content).hexdigest()
        with self.assertRaises(ValueError):
            verify_inventory_halo_resources(item,
                lambda path: content if path == name else (ASSETS/path).read_bytes())

    def one_full_cue(self, models=None):
        # A deliberately isolated authenticated cue prevents an unrelated
        # sample from hiding which gate established this synthetic proof.
        clone = InventoryHaloModels.__new__(InventoryHaloModels)
        clone.models = {"full": models or [self.models.models["full"][-1]]}
        clone._rendered = {}; clone._normalized = {}
        return clone

    def changed_patch_pixels(self, label, *, constant=None):
        image, rect, origin = self.scene(model_index=-1)
        pixels = np.asarray(image, dtype=np.float32).copy()
        a,b,c,d = FULL_OFFSETS[label]
        observed = pixels[origin[1]+b:origin[1]+d,origin[0]+a:origin[0]+c]
        if constant is None:
            observed += np.where(np.arange(observed.shape[1])%2,18.,-18.)[None,:,None]
        else:
            observed += constant
        return pixels, rect, origin

    def test_symmetric_raster_normalization_requires_both_raw_rails_then_every_patch(self):
        pixels, rect, origin = self.changed_patch_pixels("frame_top")
        cue = self.one_full_cue()
        # A fine synthetic raster phase exceeds RAW10, but symmetric bounded
        # normalization establishes the SAME cue's every tiny RGB patch.
        with patch("inventory_halo._halo_raster", side_effect=lambda array: array):
            self.assertIsNone(cue.match(pixels,rect,"full",origin,1.))
        cue = self.one_full_cue()
        self.assertIsNotNone(cue.match(pixels,rect,"full",origin,1.))
        pixels, rect, origin = self.changed_patch_pixels("halo_top",constant=35.)
        self.assertIsNone(self.one_full_cue().match(pixels,rect,"full",origin,1.))

    def test_normalization_cannot_hide_a_raw_opposite_rail_mismatch(self):
        pixels, rect, origin = self.changed_patch_pixels("frame_left")
        with patch("inventory_halo._halo_raster", side_effect=AssertionError("Raw veto must happen first")):
            self.assertIsNone(self.one_full_cue().match(pixels,rect,"full",origin,1.))

    def test_opposite_rails_from_different_cues_cannot_be_combined(self):
        image,rect,origin = self.scene(model_index=-1)
        a = dict(self.models.models["full"][-1]); b = dict(a)
        a["frame_right"] = Image.new("RGB",a["frame_right"].size,"black")
        b["frame_left"] = Image.new("RGB",b["frame_left"].size,"black")
        with patch("inventory_halo._halo_raster",side_effect=AssertionError("Same cue bilateral veto")):
            self.assertIsNone(self.one_full_cue([a,b]).match(np.asarray(image,dtype=np.float32),
                                                          rect,"full",origin,1.))

    def test_one_skipped_band_only_suggests_a_regular_candidate_and_default_proof_rejects(self):
        runs = [(60+65*i,65+65*i) for i in range(8) if i != 3]
        with self.assertRaises(LayoutError):
            _lattice(runs,520,65.,1.)
        pitch,offset = _lattice(runs,520,65.,1.,candidate_gaps=True)
        self.assertAlmostEqual(pitch,65.)
        self.assertAlmostEqual(offset,62.)
        # Candidate inference itself never supplies the missing actual pixels.
        (image,),rect = generated_frames(offsets=(0,))
        image.paste("black",(rect.left,rect.top+3*65+60,rect.right,rect.top+4*65))
        with self.assertRaises(LayoutError):
            self.prove(image,rect)

    def test_gap_candidate_requires_spanning_raw_bands_correct_pitch_and_at_most_one_gap(self):
        cases = [([(60+65*i,65+65*i) for i in range(8) if i not in (2,4)],520,65.),
                 ([(60+58*i,65+58*i) for i in range(8)],520,65.),
                 ([(60+65*i,65+65*i) for i in range(4)],520,65.)]
        for runs,length,pitch in cases:
            with self.subTest(runs=runs),self.assertRaises(LayoutError):
                _lattice(runs,length,pitch,1.,candidate_gaps=True)

    def test_generalized_tiers_keep_full_same_cue_raw_opposite_rail_veto(self):
        for index in range(self.primary_full_count,len(self.models.models["full"])):
            with self.subTest(index=index):
                image,rect,origin=self.scene(model_index=index)
                cue=self.one_full_cue([self.models.models["full"][index]])
                pixels=np.asarray(image,dtype=np.float32)
                self.assertIsNotNone(cue.match(pixels,rect,"full",origin,1.))
                # Unknown/neutral rails cannot become an approved colored
                # frame merely because exact genuine gutter pixels surround it.
                pixels=pixels.copy()
                for label in ("frame_left","frame_right"):
                    a,b,c,d=FULL_OFFSETS[label]
                    pixels[origin[1]+b:origin[1]+d,origin[0]+a:origin[0]+c]=GAP
                with patch("inventory_halo._halo_raster",side_effect=AssertionError("Raw bilateral veto first")):
                    self.assertIsNone(cue.match(pixels,rect,"full",origin,1.))

    def test_generalized_manifest_types_tier_names_classifier_and_same_source_geometry_are_strict(self):
        mutations=[]
        for value in ([True,6],[5,"6"],[5,5],[4,5,6],None):
            item=copy.deepcopy(self.document);item["supplemental_tiers"]=value;mutations.append(item)
        for key,value in (("tier",True),("tier",4),("tier",7),("source_body_rect",[0,0,61,60]),
                          ("source_classification",{"tier":6,"rmse":15.,"margin":10.}),
                          ("source_classification",{"tier":6,"rmse":0.,"margin":4.}),
                          ("source_image","wrong-source.png")):
            item=copy.deepcopy(self.document);item["supplemental_models"][0][key]=value;mutations.append(item)
        item=copy.deepcopy(self.document)
        item["supplemental_models"][0]["patches"]["frame_left"]=item["models"][0]["patches"]["frame_left"]
        mutations.append(item)
        item=copy.deepcopy(self.document);item["schema"]=True;mutations.append(item)
        item=copy.deepcopy(self.document);item["schema"]=2.;mutations.append(item)
        for item in mutations:
            with self.assertRaises(ValueError):
                verify_inventory_halo_resources(item,lambda name:(ASSETS/name).read_bytes())

    def test_legacy_t4_catalog_remains_compatible_without_generalized_fields(self):
        legacy=copy.deepcopy(self.document)
        legacy["schema"]=1
        legacy.pop("supplemental_models");legacy.pop("supplemental_tiers")
        legacy["records"]=[r for r in legacy["records"] if r["image"].startswith("inventory_halo_T4_")]
        names=verify_inventory_halo_resources(legacy,lambda name:(ASSETS/name).read_bytes())
        self.assertEqual(len(names),532)

    def test_one_pixel_split_midpoint_only_proposes_cues_and_unknown_strip_still_rejects(self):
        (image,),rect=generated_frames(offsets=(0,))
        image.paste("black",(rect.left,rect.top+62,rect.left+3*65,rect.top+63))
        mask=np.max(abs(np.asarray(image.crop(rect.as_box()),dtype=float)-GAP),axis=2)<=6
        from unittest.mock import Mock
        unavailable=Mock();unavailable.match.return_value=None
        with patch("inventory_halo.default_models",return_value=unavailable):
            with self.assertRaisesRegex(LayoutError,"authenticated"):
                prove_inventory_halo_mask(image,rect,8,5,GAP,1.,mask)
        # The raw split has a bounded candidate lattice, not an invented row:
        # no exact nine-patch frame cue exists for this unrelated black strip.
        self.assertGreater(unavailable.match.call_count,0)

    def test_two_unsupported_missing_bands_cannot_even_request_a_halo_model(self):
        (image,),rect=generated_frames(offsets=(0,))
        for row in (1,3):
            image.paste("black",(rect.left,rect.top+row*65+60,rect.right,rect.top+(row+1)*65))
        with patch("inventory_halo.default_models",side_effect=AssertionError("Insufficient raw row evidence")):
            with self.assertRaises(LayoutError):
                self.prove(image,rect)

    def test_gold_and_purple_opposite_rails_do_not_form_one_cue(self):
        image,rect,origin=self.scene(model_index=-1)
        purple=self.models.models["full"][-1]
        gold=self.models.models["full"][self.primary_full_count]
        a,b,_,_=FULL_OFFSETS["frame_left"]
        image.paste(gold["frame_left"],(origin[0]+a,origin[1]+b))
        with patch("inventory_halo._halo_raster",side_effect=AssertionError("Mixed tier raw veto")):
            self.assertIsNone(self.one_full_cue([purple,gold]).match(
                np.asarray(image,dtype=np.float32),rect,"full",origin,1.))


if __name__ == "__main__":
    unittest.main()
