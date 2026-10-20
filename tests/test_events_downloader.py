from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from geofetch.providers.events.downloader import extract_pdf_files, safe_name


class PdfExtractionTests(unittest.TestCase):
    def test_extracts_pdf_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "product.zip"
            output_dir = root / "output"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("maps/static_map.pdf", b"%PDF-1.7\ntest")
                archive.writestr("vector/result.geojson", b'{"type":"FeatureCollection"}')
                archive.writestr("raster/image.tif", b"not-a-real-tif")

            result = extract_pdf_files(archive_path, output_dir)

            self.assertEqual(result["downloaded"], 1)
            self.assertTrue((output_dir / "static_map.pdf").is_file())
            self.assertFalse((output_dir / "result.geojson").exists())
            self.assertFalse((output_dir / "image.tif").exists())

    def test_rejects_zip_without_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "product.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("data/result.geojson", b"{}")

            with self.assertRaises(RuntimeError):
                extract_pdf_files(archive_path, root / "output")

    def test_safe_name_removes_windows_invalid_characters(self) -> None:
        self.assertEqual(safe_name('Flood: AOI/01? "map".pdf'), "Flood_AOI_01_map_.pdf")


if __name__ == "__main__":
    unittest.main()
