import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("streets", Path(__file__).with_name("streets.py"))
streets = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(streets)


class StreetEncodingTest(unittest.TestCase):
    def test_polyline6_reference_vector(self):
        self.assertEqual(
            streets.encode_polyline([[-120.2, 38.5], [-120.95, 40.7], [-126.453, 43.252]]),
            "_izlhA~rlgdF_{geC~ywl@_kwzCn`{nI",
        )

    def test_names_unicode_aliases_and_delimiters(self):
        feature = {
            "properties": {"highway": "residential", "name": "Straße", "name:en": "Street",
                           "alt_name": "Straße; Other\nname\0x"},
            "geometry": {"type": "LineString", "coordinates": [[1, 2], [1.1, 2.1]]},
        }
        row, = streets.street_rows(feature)
        self.assertEqual(row.decode().split("\0")[1:], ["Straße", "Street", "Other name x\n"])

    def test_not_a_named_highway_is_excluded(self):
        self.assertEqual(list(streets.street_rows({"properties": {"name": "Boundary"}})), [])
        self.assertEqual(list(streets.street_rows({"properties": {"highway": "primary"}})), [])

    def test_invalid_coordinates_rejected(self):
        for coordinates in ([[181, 0]], [[0, float("nan")]]):
            with self.assertRaises(ValueError):
                streets.encode_polyline(coordinates)

    @unittest.skipUnless(shutil.which("osmium"), "requires runtime osmium-tool")
    def test_real_pbf_disk_index_and_native_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            xml = root / "fixture.osm"
            xml.write_text("""<osm version="0.6">
<node id="1" lat="52.5" lon="13.4" version="1"/>
<node id="2" lat="52.5001" lon="13.4001" version="1"/>
<way id="3" version="1"><nd ref="1"/><nd ref="2"/>
<tag k="highway" v="residential"/><tag k="name" v="Teststraße"/></way>
<way id="4" version="1"><nd ref="1"/><nd ref="999"/>
<tag k="highway" v="residential"/><tag k="name" v="Incomplete"/></way></osm>""")
            pbf = root / "fixture.osm.pbf"
            subprocess.run(["osmium", "cat", str(xml), "-o", str(pbf)], check=True)
            output = root / "extract.0sv"
            self.assertEqual(streets.extract(pbf, output, root / "scratch"), 1)
            self.assertTrue(output.read_bytes().endswith("\0Teststraße\n".encode()))
            self.assertEqual(list((root / "scratch").iterdir()), [])
            metadata = json.loads(subprocess.check_output(["osmium", "fileinfo", "-j", str(pbf)]))
            self.assertEqual(metadata["file"]["format"], "PBF")

    @unittest.skipUnless(shutil.which("osmium"), "requires runtime osmium-tool")
    def test_bad_source_leaves_previous_output_and_cleans_scratch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "extract.0sv"
            output.write_bytes(b"previous")
            with self.assertRaises(RuntimeError):
                streets.extract(root / "missing.osm.pbf", output, root / "scratch")
            self.assertEqual(output.read_bytes(), b"previous")
            self.assertFalse((root / "extract.0sv.next").exists())
            self.assertEqual(list((root / "scratch").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
