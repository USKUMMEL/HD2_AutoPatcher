import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from hd2_patch_fixer.archive import create_fixed_mod_archive  # noqa: E402


class ParallelPatchTests(unittest.TestCase):
    def test_compressed_mod_runs_requested_patch_jobs_in_parallel(self):
        """The outer archive creates one Slim map, then parallel patch jobs."""
        barrier = threading.Barrier(2)
        calls = []
        calls_lock = threading.Lock()

        def fake_patch_fix(**kwargs):
            # A sequential implementation times out here, proving both jobs
            # actually entered the worker pool together.
            barrier.wait(timeout=1)
            with calls_lock:
                calls.append(kwargs)
            return {
                "output_path": kwargs["output_patch_path"],
                "kept_entries": 3,
                "skipped_entries": 0,
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            input_archive = temp_path / "input.zip"
            input_archive.write_bytes(b"placeholder")
            output_archive = temp_path / "output.zip"

            def fake_groups(extract_dir):
                return (
                    [
                        str(Path(extract_dir) / "B" / "second.patch_0"),
                        str(Path(extract_dir) / "A" / "first.patch_0"),
                    ],
                    [],
                )

            with (
                patch("hd2_patch_fixer.archive.slim_init") as slim_init,
                patch("hd2_patch_fixer.archive.StreamToc.from_file", return_value=True),
                patch("hd2_patch_fixer.archive.extract_archive_file"),
                patch("hd2_patch_fixer.archive.find_patch_groups", side_effect=fake_groups),
                patch("hd2_patch_fixer.archive.create_fixed_patch", side_effect=fake_patch_fix),
                patch("hd2_patch_fixer.archive.create_zip_from_directory"),
            ):
                result = create_fixed_mod_archive(
                    game_data_folder=temp_dir,
                    input_archive_path=str(input_archive),
                    output_zip_path=str(output_archive),
                    keep_type_ids={1},
                    max_workers=2,
                )

        slim_init.assert_called_once()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["slim_initialized"] for call in calls))
        self.assertEqual(result["fixed_patch_count"], 2)
        self.assertEqual(
            [item["relative_path"] for item in result["patch_results"]],
            ["A/first.patch_0", "B/second.patch_0"],
        )


if __name__ == "__main__":
    unittest.main()
