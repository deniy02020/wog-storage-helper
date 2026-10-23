"""Local diagnostic output is bounded to grids; no capture/input/transmission."""
import json
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import Mock
import unittest
from PIL import Image
from auto_layout import ScaleMatch
from core import Rect
from layout_diagnostics import save_grid_failure


class LocalGridDiagnosticTests(unittest.TestCase):
    def test_only_proved_bounded_grid_crops_are_saved_and_replaced(self):
        image = Image.new('RGB', (500, 500), (1, 2, 3))
        profile = {'inventory': {'rect': [50, 80, 150, 180]},
                   'storage': {'rect': [250, 80, 350, 180]},
                   'anchors': [{'rect': [50, 40, 150, 60]}, {'rect': [250, 40, 350, 60]}]}
        layout = SimpleNamespace(profile=profile, locate_headers=Mock(return_value=[
            ScaleMatch(Rect(50, 40, 150, 60), 1., 0., 10.),
            ScaleMatch(Rect(250, 40, 350, 60), 1., 0., 10.)]))
        with TemporaryDirectory() as directory:
            for _ in range(2):
                save_grid_failure(directory, layout, image)
            files = sorted((Path(directory)/'diagnostics').iterdir())
            self.assertEqual([file.name for file in files],
                ['grid.json', 'inventory-grid.png', 'storage-grid.png'])
            metadata = json.loads(files[0].read_text())
            self.assertEqual(metadata['inventory']['crop'], [42, 72, 158, 188])
            with Image.open(files[1]) as crop:
                self.assertEqual(crop.size, (116, 116))
            self.assertNotIn('500', files[0].read_text())

    def test_missing_headers_or_io_failure_do_not_change_the_workflow(self):
        layout = SimpleNamespace(locate_headers=Mock(return_value=[None, None]))
        with TemporaryDirectory() as directory:
            save_grid_failure(directory, layout, Image.new('RGB', (10, 10)))
            self.assertFalse((Path(directory)/'diagnostics').exists())


if __name__ == '__main__':
    unittest.main()
