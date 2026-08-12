"""Adapter for the community Helldivers 2 audio migration engine.

The community audio modder already contains the version-aware Wwise/HIRC
import logic required to move a sound mod onto the current game archives.  It
also includes a GUI and audio-preview dependencies that are not appropriate
for the patch fixer.  This module loads only that supplied engine at runtime,
stubs its optional preview dependencies, and exposes a small headless API for
the patcher's always-on, community-compatible semantic merge mode.

This is deliberately *not* a byte-preserving repair path.  The community
engine overlays selected HIRC fields from the mod onto the current game's
Bank hierarchy. The ordinary fixer uses raw audio preservation only when a
Bank cannot be matched to a current game archive.

The community source remains a data directory rather than an ordinary package
on purpose: it keeps the original sources intact and makes updates from the
community project easy to audit.  ``build.bat`` bundles that directory under
``community_audio`` for PyInstaller builds.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterable, Iterator


COMMUNITY_AUDIO_ENV_VAR = "HD2_PATCH_FIXER_COMMUNITY_AUDIO_DIR"
_COMMUNITY_SOURCE_LEAF = (
    "External source",
    "audio modding tool",
    "hd2-audio-modder-main",
)
_BUNDLED_SOURCE_LEAF = "community_audio"
_REQUIRED_COMMUNITY_FILES = (
    "core.py",
    "slim.py",
    "util.py",
    "wwise_hierarchy_140.py",
    "wwise_hierarchy_154.py",
    "backend/db.py",
)


class CommunityAudioAdapterError(RuntimeError):
    """Raised when the optional community audio engine cannot migrate a patch."""


@dataclass(frozen=True)
class CommunityAudioMigrationResult:
    """Summary of one explicitly requested community-style semantic merge."""

    output_path: Path
    base_archives: tuple[Path, ...]
    modified_bank_ids: tuple[int, ...]
    modified_stream_ids: tuple[int, ...]

    @property
    def changed_resource_count(self) -> int:
        return len(self.modified_bank_ids) + len(self.modified_stream_ids)


class _CommunityLoggerBridge:
    """Minimal logger surface used by the community modules.

    Loading the upstream ``log.py`` creates rotating files in the current
    directory.  The fixer already has a log pane, so forwarding those messages
    to the caller is both cleaner and more useful.
    """

    def __init__(self) -> None:
        self._callback: Callable[[str], None] | None = None

    @contextmanager
    def use_callback(self, callback: Callable[[str], None] | None) -> Iterator[None]:
        old_callback = self._callback
        self._callback = callback
        try:
            yield
        finally:
            self._callback = old_callback

    def _emit(self, level: str, message: object, *args: object, **_kwargs: object) -> None:
        if self._callback is None:
            return
        try:
            rendered = str(message)
            if args:
                try:
                    rendered = rendered % args
                except (TypeError, ValueError):
                    rendered = " ".join((rendered, *(str(arg) for arg in args)))
            self._callback(f"COMMUNITY AUDIO [{level}]: {rendered}")
        except Exception:
            # A GUI callback must never make the migration engine fail.
            pass

    def debug(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit("debug", message, *args, **kwargs)

    def info(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit("info", message, *args, **kwargs)

    def warning(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit("warning", message, *args, **kwargs)

    warn = warning

    def error(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit("error", message, *args, **kwargs)

    def exception(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit("exception", message, *args, **kwargs)


_IMPORT_LOCK = threading.RLock()
_COMMUNITY_LOGGER = _CommunityLoggerBridge()
_LOADED_CORE: ModuleType | None = None
_LOADED_SOURCE: Path | None = None
_MISSING = object()


def _emit(log: Callable[[str], None] | None, message: str) -> None:
    if log is not None:
        log(message)


def _is_community_source(path: Path) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in _REQUIRED_COMMUNITY_FILES)


def discover_community_audio_source(
    source_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Return the directory that contains the supplied community audio source.

    Resolution is deterministic: explicit argument, environment override,
    PyInstaller extraction directory, then the normal workspace layout.  The
    explicit override is useful when a maintainer wants to test a newer
    community revision before replacing the bundled copy.
    """

    candidates: list[Path] = []
    if source_dir is not None:
        candidates.append(Path(source_dir))

    environment_override = os.environ.get(COMMUNITY_AUDIO_ENV_VAR)
    if environment_override:
        candidates.append(Path(environment_override))

    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidates.append(Path(frozen_root) / _BUNDLED_SOURCE_LEAF)

    # When launched from source, find the workspace without depending on the
    # current working directory (the GUI may be launched from a shortcut).
    module_path = Path(__file__).resolve()
    for parent in (module_path.parent, *module_path.parents):
        candidates.append(parent.joinpath(*_COMMUNITY_SOURCE_LEAF))
        candidates.append(parent / _BUNDLED_SOURCE_LEAF)

    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if _is_community_source(resolved):
            return resolved

    hint = (
        f"Set {COMMUNITY_AUDIO_ENV_VAR} to the folder containing core.py, or "
        "restore External source/audio modding tool/hd2-audio-modder-main."
    )
    raise CommunityAudioAdapterError(f"Community audio engine source was not found. {hint}")


def _unavailable_dependency_module(module_name: str, package_name: str) -> ModuleType:
    """Create a module placeholder for an upstream preview-only dependency."""

    module = ModuleType(module_name)

    class _UnavailableFeature:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise CommunityAudioAdapterError(
                f"{package_name} is unavailable in HD2 Patch Fixer. "
                "Audio preview is not part of patch migration."
            )

    if module_name == "pyaudio":
        module.PyAudio = _UnavailableFeature  # type: ignore[attr-defined]
        module.paComplete = 1  # type: ignore[attr-defined]
        module.paContinue = 0  # type: ignore[attr-defined]
    return module


@contextmanager
def _temporary_module(name: str, module: ModuleType) -> Iterator[None]:
    previous = sys.modules.get(name, _MISSING)
    sys.modules[name] = module
    try:
        yield
    finally:
        if previous is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous  # type: ignore[assignment]


@contextmanager
def _community_import_environment(source: Path) -> Iterator[None]:
    """Expose the upstream flat-module layout only while importing ``core``."""

    bridge_log_module = ModuleType("log")
    bridge_log_module.logger = _COMMUNITY_LOGGER  # type: ignore[attr-defined]

    replacements: list[tuple[str, ModuleType]] = [("log", bridge_log_module)]
    if "pyaudio" not in sys.modules:
        replacements.append(("pyaudio", _unavailable_dependency_module("pyaudio", "PyAudio")))
    if "numpy" not in sys.modules:
        # Numpy is used exclusively by the preview player in the community
        # code.  A blank placeholder is sufficient until that unused feature
        # is called, at which point Python naturally reports the absence.
        replacements.append(("numpy", _unavailable_dependency_module("numpy", "NumPy")))

    source_string = str(source)
    sys.path.insert(0, source_string)
    stack = []
    try:
        for name, module in replacements:
            manager = _temporary_module(name, module)
            manager.__enter__()
            stack.append(manager)
        yield
    finally:
        while stack:
            stack.pop().__exit__(None, None, None)
        try:
            sys.path.remove(source_string)
        except ValueError:
            pass


def load_community_audio_core(
    source_dir: str | os.PathLike[str] | None = None,
) -> ModuleType:
    """Load the upstream ``core.py`` without its GUI/audio-preview runtime.

    The community project uses flat absolute imports (``import core``,
    ``import wwise_hierarchy_154``).  The adapter preserves that layout during
    import because rewriting the source would make every community upgrade a
    risky fork.  Once imported, all needed references are held by the core
    module, so the temporary import path can be removed.
    """

    source = discover_community_audio_source(source_dir)
    with _IMPORT_LOCK:
        global _LOADED_CORE, _LOADED_SOURCE
        if _LOADED_CORE is not None:
            if _LOADED_SOURCE != source:
                raise CommunityAudioAdapterError(
                    "The community audio engine is already loaded from "
                    f"{_LOADED_SOURCE}; restart the tool before selecting a different source."
                )
            return _LOADED_CORE

        existing_core = sys.modules.get("core")
        if existing_core is not None:
            existing_file = getattr(existing_core, "__file__", None)
            if existing_file is None or Path(existing_file).resolve().parent != source:
                raise CommunityAudioAdapterError(
                    "A different module named 'core' is already loaded, so the "
                    "community audio engine cannot be imported safely in this process."
                )
            _LOADED_CORE = existing_core
            _LOADED_SOURCE = source
            return existing_core

        try:
            with _community_import_environment(source):
                core = importlib.import_module("core")
        except CommunityAudioAdapterError:
            raise
        except Exception as exc:
            raise CommunityAudioAdapterError(
                f"Failed to load the community audio engine from {source}: {exc}"
            ) from exc

        _LOADED_CORE = core
        _LOADED_SOURCE = source
        return core


def _normalize_archive_paths(base_archive_paths: Iterable[str | os.PathLike[str]]) -> tuple[Path, ...]:
    normalized: list[Path] = []
    seen: set[Path] = set()
    for archive_path in base_archive_paths:
        path = Path(archive_path).expanduser()
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path.absolute()
        if resolved in seen:
            continue
        seen.add(resolved)
        normalized.append(resolved)
    return tuple(normalized)


def migrate_audio_patch_with_community_engine(
    *,
    base_archive_paths: Iterable[str | os.PathLike[str]],
    patch_path: str | os.PathLike[str],
    output_patch_path: str | os.PathLike[str],
    game_data_folder: str | os.PathLike[str] | None = None,
    import_hierarchy: bool = True,
    source_dir: str | os.PathLike[str] | None = None,
    log: Callable[[str], None] | None = None,
) -> CommunityAudioMigrationResult:
    """Perform an explicit community-style semantic merge for a mod's audio.

    ``base_archive_paths`` must include every current game archive that owns a
    Bank referenced by the mod patch.  The community engine uses Wwise source
    IDs to apply mod media to the corresponding current Bank/Stream, then
    overlays the HIRC fields it supports before writing a new standalone
    patch.  This is the community tool's aggressive migration behaviour, not
    a proof that only author-modified fields were changed.

    This function intentionally fails when no Bank or Stream becomes modified.
    An empty output patch would hide a missing base archive mapping and is
    worse than preserving the original audio entry as a fallback.
    """

    source_patch = Path(patch_path).expanduser().resolve()
    target_patch = Path(output_patch_path).expanduser().resolve()
    base_archives = _normalize_archive_paths(base_archive_paths)

    if not source_patch.is_file():
        raise CommunityAudioAdapterError(f"Audio patch file does not exist: {source_patch}")
    if source_patch == target_patch:
        raise CommunityAudioAdapterError(
            "Community audio migration needs a separate output path so the original patch remains recoverable."
        )
    if not base_archives:
        raise CommunityAudioAdapterError(
            "No current game archives were supplied for community audio migration."
        )
    if game_data_folder is not None and not Path(game_data_folder).expanduser().is_dir():
        raise CommunityAudioAdapterError(f"Game data folder does not exist: {game_data_folder}")

    # The community engine supports bundled game data after its own slim
    # module is initialized.  Legacy archives remain valid with this call too.
    target_patch.parent.mkdir(parents=True, exist_ok=True)

    with _IMPORT_LOCK:
        core = load_community_audio_core(source_dir)
        slim_module = sys.modules.get("slim")
        if game_data_folder is not None:
            if slim_module is None or not hasattr(slim_module, "slim_init"):
                raise CommunityAudioAdapterError("Community slim loader was not initialized correctly.")
            try:
                slim_module.slim_init(str(Path(game_data_folder).expanduser().resolve()))
            except Exception as exc:
                raise CommunityAudioAdapterError(
                    f"Failed to initialize the community loader for {game_data_folder}: {exc}"
                ) from exc

        with _COMMUNITY_LOGGER.use_callback(log):
            try:
                mod = core.Mod("HD2 Patch Fixer audio migration", None)
                for archive_path in base_archives:
                    _emit(log, f"COMMUNITY AUDIO: Loading current archive {archive_path.name}")
                    if not mod.load_archive_file(str(archive_path)):
                        raise CommunityAudioAdapterError(
                            f"Community engine could not load current archive: {archive_path}"
                        )

                _emit(log, f"COMMUNITY AUDIO: Importing mod audio from {source_patch.name}")
                if not mod.import_patch(str(source_patch), import_hierarchy=import_hierarchy):
                    raise CommunityAudioAdapterError(
                        "Community engine rejected the audio patch; inspect the preceding community log lines."
                    )

                modified_bank_ids = tuple(
                    sorted(int(bank_id) for bank_id, bank in mod.get_wwise_banks().items() if bank.modified)
                )
                modified_stream_ids = tuple(
                    sorted(int(stream_id) for stream_id, stream in mod.get_wwise_streams().items() if stream.modified)
                )
                if not modified_bank_ids and not modified_stream_ids:
                    raise CommunityAudioAdapterError(
                        "Community engine found no matching modified Bank or Stream. "
                        "The relevant current game archive may be missing."
                    )

                _emit(
                    log,
                    "COMMUNITY AUDIO: Writing community-compatible semantic merge "
                    f"({len(modified_bank_ids)} bank(s), {len(modified_stream_ids)} stream(s))",
                )
                mod.write_patch(
                    output_folder=str(target_patch.parent),
                    output_filename=target_patch.name,
                )
            except CommunityAudioAdapterError:
                raise
            except Exception as exc:
                raise CommunityAudioAdapterError(
                    f"Community audio migration failed: {exc}"
                ) from exc

    if not target_patch.is_file():
        raise CommunityAudioAdapterError(
            f"Community engine reported success but did not create {target_patch}."
        )

    return CommunityAudioMigrationResult(
        output_path=target_patch,
        base_archives=base_archives,
        modified_bank_ids=modified_bank_ids,
        modified_stream_ids=modified_stream_ids,
    )
