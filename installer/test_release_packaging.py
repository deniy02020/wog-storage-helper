"""Local ZIP packaging with a tiny executable fixture, genuine model/licence data.

No actual payload, program build, launcher, GUI or OS integration is invoked.
"""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import package_release
from release_identity import release_version
from installer.core import InstallError
from installer.license_resources import licensing_files, licensing_hashes

PROJECT = Path(__file__).resolve().parents[1]


class ReleasePackagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix=".test-release-", dir=PROJECT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / "dist" / "WoGStorageHelper"
        self.bundle.mkdir(parents=True)
        (self.bundle / "WoGStorageHelper.exe").write_bytes(b"Non-executable packaging test fixture.")
        for folder in ("preset_assets", "assets"):
            for source in (PROJECT / folder).iterdir():
                if source.is_file() and source.suffix.lower() in {".png", ".ico", ".json"}:
                    relative = source.relative_to(PROJECT)
                    for destination in (self.root / relative, self.bundle / "_internal" / relative):
                        destination.parent.mkdir(parents=True,exist_ok=True)
                        shutil.copyfile(source,destination)
        for destination in (self.root / "locales.json", self.bundle / "_internal" / "locales.json"):
            shutil.copyfile(PROJECT / "locales.json",destination)
        for name in package_release.DOCUMENTS:
            destination = self.root / name
            destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_text("Synthetic documentation fixture.\n",encoding="utf-8")
        for name, source in licensing_files(PROJECT).items():
            for destination in (self.root / name, self.bundle / "_internal" / name):
                destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(source,destination)
        releases = self.root / "releases"
        releases.mkdir()
        self.previous = releases / "preserved-prior.zip"
        self.previous.write_bytes(b"Prior snapshot must stay byte for byte.")

    def create(self):
        with patch.object(package_release,"ROOT",self.root):
            return package_release.create_release()

    def test_release_contains_checked_licences_and_keeps_the_previous_archive(self):
        before = self.previous.read_bytes()
        result = self.create()
        expected = licensing_hashes(self.root)
        metadata = json.loads(Path(result["metadata"]).read_text(encoding="utf-8"))
        self.assertEqual(metadata["version"],release_version())
        self.assertEqual(metadata["channel"],"local-prerelease" if "-beta." in release_version() else "local-release")
        self.assertEqual(metadata["license_sha256"],expected)
        self.assertEqual(metadata["authentic_license_resources"],len(expected))
        parking = json.loads((self.root / "preset_assets/header_parking.json").read_text())
        self.assertEqual(metadata["headerparking_sample_count"],6)
        self.assertEqual(metadata["headerparking_model_sha256"],package_release.sha256(
            self.root / "preset_assets/header_parking.json"))
        with ZipFile(result["archive"]) as archive:
            self.assertIsNone(archive.testzip())
            for name,digest in expected.items():
                self.assertEqual(hashlib.sha256(archive.read("WoGStorageHelper/_internal/"+name)).hexdigest(),digest)
            self.assertEqual(hashlib.sha256(archive.read("WoGStorageHelper/THIRD_PARTY_NOTICES.txt")).hexdigest(),expected["THIRD_PARTY_NOTICES.txt"])
            for record in parking["records"]:
                relative = "WoGStorageHelper/_internal/preset_assets/"+record["image"]
                self.assertEqual(hashlib.sha256(archive.read(relative)).hexdigest(),record["sample_sha256"])
                self.assertFalse(any(Path(name).name == record["source_image"] for name in archive.namelist()))
        self.assertEqual(self.previous.read_bytes(),before)

    def test_missing_or_stale_built_licence_stops_before_a_release_archive_exists(self):
        source = self.bundle / "_internal" / "licenses" / "python" / "LICENSE.txt"
        original = source.read_bytes()
        for changed in (None,b"Changed authentic licence."):
            if changed is None:
                source.unlink()
            else:
                source.write_bytes(changed)
            with self.subTest(changed=changed),self.assertRaisesRegex(ValueError,"authentic licence"):
                self.create()
            self.assertEqual(list((self.root / "releases").glob("*.zip")),[self.previous])
            source.write_bytes(original)

    def test_tampered_source_notice_cannot_be_published_as_a_new_doc_snapshot(self):
        (self.root / "THIRD_PARTY_NOTICES.txt").write_bytes(b"Invented or stale notice.")
        with self.assertRaisesRegex(InstallError,"notices checksum"):
            self.create()
        self.assertEqual(list((self.root / "releases").glob("*.zip")),[self.previous])

    def test_missing_or_stale_header_parking_model_stops_before_archive_creation(self):
        path = self.bundle / "_internal/preset_assets/header_parking.json"
        original = path.read_bytes()
        for changed in (None,b"Stale JSON model"):
            if changed is None:
                path.unlink()
            else:
                path.write_bytes(changed)
            with self.subTest(changed=changed),self.assertRaisesRegex(ValueError,"parking model missing or stale"):
                self.create()
            self.assertEqual(list((self.root / "releases").glob("*.zip")),[self.previous])
            path.write_bytes(original)

    def test_each_header_parking_sample_is_required_and_checksum_checked_before_archive(self):
        document = json.loads((self.root / "preset_assets/header_parking.json").read_text())
        for record in document["records"]:
            path = self.bundle / "_internal/preset_assets" / record["image"]
            original = path.read_bytes()
            path.unlink()
            with self.subTest(name=record["image"],case="missing"),self.assertRaisesRegex(InstallError,"parking asset is missing"):
                self.create()
            # Keep valid PNG geometry so the global leak guard reaches the SHA gate.
            from PIL import Image
            with Image.open(self.root / "preset_assets" / record["image"]) as image:
                changed = image.copy()
            pixel = changed.getpixel((0,0))
            changed.putpixel((0,0),(pixel[0]^1,pixel[1],pixel[2]))
            changed.save(path)
            with self.subTest(name=record["image"],case="changed"),self.assertRaisesRegex(InstallError,"parking sample checksum"):
                self.create()
            self.assertEqual(list((self.root / "releases").glob("*.zip")),[self.previous])
            path.write_bytes(original)

    def test_product_spec_collects_model_and_six_assets_without_building_or_private_frames(self):
        captures = []
        def analysis(_scripts,**kwargs):
            captures.append(kwargs)
            return type("AnalysisFixture",(),{"pure":None,"scripts":[],"binaries":[],"datas":[]})()
        namespace = {"SPECPATH":str(PROJECT),"Analysis":analysis,"PYZ":lambda *_:None,
                     "EXE":lambda *_args,**_kwargs:None,"COLLECT":lambda *_args,**_kwargs:None}
        native_manifest = json.loads((PROJECT/'ocr_native_manifest.json').read_bytes())
        planned_native = {'ocr_native/'+record['path']:record['sha256'] for record in native_manifest['files']}
        # This test plans Analysis without downloading/running a native build.
        # The real spec retains verify_native's mandatory byte checks.
        with patch('ocr_resources.verify_native',return_value=planned_native) as native_check:
            exec(compile((PROJECT / "WoGStorageHelper.spec").read_text(),"product-spec","exec"),namespace)
        native_check.assert_called_once_with(PROJECT.resolve())
        delivered = {str(Path(destination) / Path(source).name).replace("\\","/"):Path(source)
                     for source,destination in captures[0]['datas']}
        self.assertIn("preset_assets/header_parking.json",delivered)
        document = json.loads(delivered["preset_assets/header_parking.json"].read_text())
        for record in document["records"]:
            self.assertEqual(hashlib.sha256(delivered["preset_assets/"+record["image"]].read_bytes()).hexdigest(),record["sample_sha256"])
            self.assertNotIn("preset_assets/"+record["source_image"],delivered)
        models = json.loads(delivered['ocr_models/manifest.json'].read_bytes())
        self.assertEqual(len(models['models']),17)
        for record in models['models']:
            self.assertEqual(hashlib.sha256(delivered['ocr_models/'+record['path']].read_bytes()).hexdigest(),record['sha256'])
        native_names = {str(Path(destination)/Path(source).name).replace('\\','/')
                        for source,destination in captures[0]['binaries']}
        self.assertEqual(native_names,set(planned_native))
        self.assertEqual(len(native_names),11)


if __name__ == "__main__":
    unittest.main()
