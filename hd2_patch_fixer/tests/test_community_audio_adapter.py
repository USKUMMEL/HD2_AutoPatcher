import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
COMMUNITY_SOURCE = (
    WORKSPACE_ROOT
    / "External source"
    / "audio modding tool"
    / "hd2-audio-modder-main"
)
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from hd2_patch_fixer.community_audio_adapter import (  # noqa: E402
    discover_community_audio_source,
    load_community_audio_core,
)


@unittest.skipUnless(COMMUNITY_SOURCE.is_dir(), "community audio source is not present")
class CommunityAudioAdapterTests(unittest.TestCase):
    def test_explicit_source_discovery_uses_the_supplied_community_tree(self):
        self.assertEqual(
            discover_community_audio_source(COMMUNITY_SOURCE),
            COMMUNITY_SOURCE.resolve(),
        )

    def test_community_core_loads_without_the_optional_pyaudio_package(self):
        # CI/source-test environments do not need (and commonly cannot build)
        # PortAudio/PyAudio.  The adapter provides the upstream preview module
        # only long enough to import the semantic migration engine.
        core = load_community_audio_core(COMMUNITY_SOURCE)
        self.assertTrue(callable(core.Mod))
        self.assertTrue(callable(core.GameArchive))


if __name__ == "__main__":
    unittest.main()
