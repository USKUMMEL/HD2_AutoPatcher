import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from hd2_patch_fixer import slim  # noqa: E402


class SlimMemoryTests(unittest.TestCase):
    def test_resource_iterator_does_not_keep_or_overwrite_past_requested_size(self):
        with patch.object(
            slim,
            "get_resource_from_bundle",
            side_effect=[b"abc", b"def"],
        ):
            resources = list(slim.iter_resources_from_bundle("bundle", 100, 5))

        self.assertEqual(resources, [b"abc", b"de"])

    def test_reconstruction_writes_resources_directly_into_destination(self):
        package = slim.Package(
            size=5,
            name="test_package",
            entries=[slim.BundleEntry(start_offset=100, bundle_index=0, original_archive_offset=0)],
        )
        original_packages = slim.package_contents
        original_folder = slim.game_data_folder
        try:
            slim.package_contents = {"test_package": package}
            slim.game_data_folder = "game"
            with patch.object(
                slim,
                "iter_resources_from_bundle",
                return_value=iter([b"abc", b"de"]),
            ):
                data = slim.reconstruct_package_from_bundles("test_package")
        finally:
            slim.package_contents = original_packages
            slim.game_data_folder = original_folder

        self.assertEqual(data, bytearray(b"abcde"))


if __name__ == "__main__":
    unittest.main()
