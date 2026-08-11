"""Non-destructive validation for Wwise resources stored in HD2 patches.

The community audio modder can perform semantic Wwise edits, but that requires
parsing a large and version-sensitive HIRC hierarchy.  This fixer has a
different job: migrate a patch container to the current game build without
changing the mod author's audio.  The helpers below therefore inspect the
stable outer envelopes only and leave every audio byte untouched.

Keeping this parser deliberately shallow is important.  It can flag malformed
Bank/Stream/Dep records and show which Bank/Dep records belong together, while
remaining forward-compatible with hierarchy records the tool does not know.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass

from .constants import (
    WwiseBankID,
    WwiseDepID,
    WwiseMetaDataID,
    WwiseStreamID,
)


AUDIO_TYPE_IDS = frozenset(
    {
        WwiseBankID,
        WwiseDepID,
        WwiseStreamID,
        WwiseMetaDataID,
    }
)

# This is the resource wrapper used by the community audio modder before a
# bank payload.  It is not a Wwise chunk tag (the inner payload starts BKHD).
WWISE_RESOURCE_MAGIC = bytes.fromhex("D82F7678")
BANK_VERSION_KEY = 0x9211BCAC


@dataclass(frozen=True)
class AudioPayloadInspection:
    """Result of inspecting one audio archive entry without changing it."""

    file_id: int
    type_id: int
    kind: str
    valid: bool
    notes: tuple[str, ...] = ()
    bank_version: int | None = None
    embedded_bank_id: int | None = None
    declared_stream_size: int | None = None
    hirc_entry_count: int | None = None
    didx_entry_count: int | None = None


@dataclass(frozen=True)
class AudioDependencyGroup:
    """The stable Bank <-> Dep relationship, keyed by their shared file ID."""

    file_id: int
    has_bank: bool
    has_dep: bool
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AudioValidationReport:
    """A non-fatal validation report for all audio resources in one patch."""

    entries: tuple[AudioPayloadInspection, ...]
    groups: tuple[AudioDependencyGroup, ...]
    stream_count: int
    metadata_count: int

    @property
    def has_audio(self) -> bool:
        return bool(self.entries)

    @property
    def invalid_entries(self) -> tuple[AudioPayloadInspection, ...]:
        return tuple(entry for entry in self.entries if not entry.valid)

    def log_lines(self) -> tuple[str, ...]:
        """Return concise user-facing diagnostics for the fixer log."""

        if not self.entries:
            return ()

        bank_count = sum(1 for entry in self.entries if entry.type_id == WwiseBankID)
        dep_count = sum(1 for entry in self.entries if entry.type_id == WwiseDepID)
        lines = [
            "AUDIO VALIDATION: "
            f"{bank_count} bank(s), {dep_count} dep(s), {self.stream_count} stream(s), "
            f"{self.metadata_count} metadata record(s); all audio payloads will be preserved byte-for-byte."
        ]
        for group in self.groups:
            for note in group.notes:
                lines.append(f"AUDIO GROUP {group.file_id}: {note}")
        for entry in self.invalid_entries:
            detail = "; ".join(entry.notes) or "unknown malformed resource"
            lines.append(
                f"AUDIO WARNING {entry.kind} {entry.file_id}: {detail}; preserving original bytes."
            )
        return tuple(lines)


def inspect_audio_entry(entry) -> AudioPayloadInspection:
    """Inspect a ``TocEntry``-like object without mutating its payload.

    The outer HD2 archive stores Bank and Dep data in ``toc_data`` and Stream
    media in ``stream_data``.  Entries from future game versions may have
    unfamiliar internals, so unexpected content is reported rather than
    rewritten or rejected.
    """

    file_id = int(entry.file_id)
    type_id = int(entry.type_id)
    toc_data = bytes(entry.toc_data)
    stream_data = bytes(entry.stream_data)

    if type_id == WwiseBankID:
        return _inspect_bank(file_id, toc_data)
    if type_id == WwiseDepID:
        return _inspect_dep(file_id, toc_data)
    if type_id == WwiseStreamID:
        return _inspect_stream(file_id, toc_data, stream_data)
    if type_id == WwiseMetaDataID:
        return AudioPayloadInspection(
            file_id=file_id,
            type_id=type_id,
            kind="Wwise Metadata",
            valid=True,
            notes=(
                "No stable public metadata schema is assumed; payload is intentionally pass-through.",
            ),
        )
    return AudioPayloadInspection(
        file_id=file_id,
        type_id=type_id,
        kind=f"Unknown audio type ({type_id})",
        valid=True,
        notes=("Unknown audio type is intentionally pass-through.",),
    )


def inspect_audio_collection(toc_dict: Mapping[int, Mapping[int, object]]) -> AudioValidationReport:
    """Validate audio entries and group matching Wwise Bank/Dep records.

    A Wwise dependency uses the same file ID as its Bank in the community
    tool.  Streams are intentionally not assigned to a bank here: doing so
    requires semantic HIRC parsing and would make a migration unsafe for newer
    Wwise hierarchy variants.
    """

    entries: list[AudioPayloadInspection] = []
    bank_ids: set[int] = set()
    dep_ids: set[int] = set()
    stream_count = 0
    metadata_count = 0

    for type_id in AUDIO_TYPE_IDS:
        for entry in toc_dict.get(type_id, {}).values():
            inspection = inspect_audio_entry(entry)
            entries.append(inspection)
            if type_id == WwiseBankID:
                bank_ids.add(inspection.file_id)
            elif type_id == WwiseDepID:
                dep_ids.add(inspection.file_id)
            elif type_id == WwiseStreamID:
                stream_count += 1
            elif type_id == WwiseMetaDataID:
                metadata_count += 1

    groups = []
    for file_id in sorted(bank_ids | dep_ids):
        has_bank = file_id in bank_ids
        has_dep = file_id in dep_ids
        notes = []
        if has_bank and not has_dep:
            notes.append(
                "Bank has no matching Dep in this patch; this is valid when it relies on the base game's dependency record."
            )
        elif has_dep and not has_bank:
            notes.append(
                "Dep has no matching Bank in this patch; this is valid when it accompanies a base-game bank."
            )
        groups.append(
            AudioDependencyGroup(
                file_id=file_id,
                has_bank=has_bank,
                has_dep=has_dep,
                notes=tuple(notes),
            )
        )

    return AudioValidationReport(
        entries=tuple(sorted(entries, key=lambda item: (item.type_id, item.file_id))),
        groups=tuple(groups),
        stream_count=stream_count,
        metadata_count=metadata_count,
    )


def _inspect_bank(file_id: int, toc_data: bytes) -> AudioPayloadInspection:
    notes: list[str] = []
    valid = True
    embedded_bank_id = None
    bank_version = None
    hirc_entry_count = None
    didx_entry_count = None

    if len(toc_data) < 16:
        return AudioPayloadInspection(
            file_id=file_id,
            type_id=WwiseBankID,
            kind="Wwise Bank",
            valid=False,
            notes=("Bank payload is smaller than the 16-byte HD2 resource wrapper.",),
        )

    if toc_data[:4] != WWISE_RESOURCE_MAGIC:
        valid = False
        notes.append("Bank does not start with the expected HD2 resource wrapper.")

    declared_size = struct.unpack_from("<I", toc_data, 4)[0]
    embedded_bank_id = struct.unpack_from("<Q", toc_data, 8)[0]
    bank_data_end = 16 + declared_size
    if bank_data_end > len(toc_data):
        valid = False
        notes.append(
            f"Bank wrapper declares {declared_size} bytes, but only {len(toc_data) - 16} byte(s) are present."
        )
        bank_data = toc_data[16:]
    else:
        bank_data = toc_data[16:bank_data_end]
        padding = toc_data[bank_data_end:]
        if padding and any(padding):
            notes.append("Bank has non-zero data after its declared wrapper size.")

    if embedded_bank_id != file_id:
        notes.append(
            f"Embedded bank ID {embedded_bank_id} differs from archive file ID {file_id}."
        )

    chunks, chunk_notes, chunk_valid = _parse_wwise_chunks(bank_data)
    notes.extend(chunk_notes)
    valid = valid and chunk_valid

    bkhd = chunks.get("BKHD")
    if bkhd is None:
        valid = False
        notes.append("Bank has no BKHD chunk.")
    elif len(bkhd) < 4:
        valid = False
        notes.append("BKHD chunk is smaller than its version field.")
    else:
        bank_version = struct.unpack_from("<I", bkhd)[0] ^ BANK_VERSION_KEY

    hirc = chunks.get("HIRC")
    if hirc is not None:
        hirc_entry_count, hirc_notes, hirc_valid = _inspect_hirc_envelope(hirc)
        notes.extend(hirc_notes)
        valid = valid and hirc_valid

    didx = chunks.get("DIDX")
    if didx is not None:
        didx_entry_count, didx_notes, didx_valid = _inspect_didx(didx, chunks.get("DATA"))
        notes.extend(didx_notes)
        valid = valid and didx_valid

    return AudioPayloadInspection(
        file_id=file_id,
        type_id=WwiseBankID,
        kind="Wwise Bank",
        valid=valid,
        notes=tuple(notes),
        bank_version=bank_version,
        embedded_bank_id=embedded_bank_id,
        hirc_entry_count=hirc_entry_count,
        didx_entry_count=didx_entry_count,
    )


def _inspect_dep(file_id: int, toc_data: bytes) -> AudioPayloadInspection:
    notes: list[str] = []
    valid = True
    if len(toc_data) < 8:
        valid = False
        notes.append("Dependency payload is smaller than its tag and size fields.")
    else:
        declared_size = struct.unpack_from("<I", toc_data, 4)[0]
        available_size = len(toc_data) - 8
        if declared_size > available_size:
            valid = False
            notes.append(
                f"Dependency declares {declared_size} bytes, but only {available_size} byte(s) are present."
            )
        else:
            name_bytes = toc_data[8:8 + declared_size]
            try:
                name_bytes.decode("utf-8")
            except UnicodeDecodeError:
                valid = False
                notes.append("Dependency path is not valid UTF-8.")
            trailing = toc_data[8 + declared_size:]
            if trailing and any(trailing):
                notes.append("Dependency has non-zero data after its declared path.")

    return AudioPayloadInspection(
        file_id=file_id,
        type_id=WwiseDepID,
        kind="Wwise Dep",
        valid=valid,
        notes=tuple(notes),
    )


def _inspect_stream(file_id: int, toc_data: bytes, stream_data: bytes) -> AudioPayloadInspection:
    notes: list[str] = []
    valid = True
    declared_stream_size = None

    if len(toc_data) < 12:
        valid = False
        notes.append("Stream TOC payload is smaller than the 12-byte HD2 resource record.")
    else:
        if toc_data[:4] != WWISE_RESOURCE_MAGIC:
            valid = False
            notes.append("Stream does not start with the expected HD2 resource wrapper.")

        # The community writer historically emits 16 bytes of header while
        # declaring a 12-byte TOC record.  StreamToc therefore sees only the
        # first 12 bytes when loading those patches.  The low 32 bits at +8
        # are still sufficient for HD2 stream payloads, so recognize that
        # layout as valid and never invent the missing four bytes.
        if len(toc_data) == 12:
            declared_stream_size = struct.unpack_from("<I", toc_data, 8)[0]
            if declared_stream_size != len(stream_data):
                valid = False
                notes.append(
                    f"Legacy stream record declares {declared_stream_size} byte(s), but archive entry contains {len(stream_data)} byte(s)."
                )
        else:
            declared_stream_size = struct.unpack_from("<Q", toc_data, 8)[0]
            if declared_stream_size != len(stream_data):
                valid = False
                notes.append(
                    f"Stream wrapper declares {declared_stream_size} byte(s), but archive entry contains {len(stream_data)} byte(s)."
                )
            trailing = toc_data[16:]
            if trailing and any(trailing):
                notes.append("Stream TOC payload has non-zero data after its resource wrapper.")

    return AudioPayloadInspection(
        file_id=file_id,
        type_id=WwiseStreamID,
        kind="Wwise Stream",
        valid=valid,
        notes=tuple(notes),
        declared_stream_size=declared_stream_size,
    )


def _parse_wwise_chunks(data: bytes) -> tuple[dict[str, bytes], tuple[str, ...], bool]:
    chunks: dict[str, bytes] = {}
    notes: list[str] = []
    offset = 0
    valid = True

    while offset < len(data):
        remaining = data[offset:]
        if not any(remaining):
            break
        if len(remaining) < 8:
            valid = False
            notes.append("Trailing Wwise chunk header is incomplete.")
            break

        raw_tag = remaining[:4]
        try:
            tag = raw_tag.decode("ascii")
        except UnicodeDecodeError:
            valid = False
            notes.append("Wwise chunk tag is not ASCII.")
            break
        size = struct.unpack_from("<I", data, offset + 4)[0]
        payload_start = offset + 8
        payload_end = payload_start + size
        if payload_end > len(data):
            valid = False
            notes.append(f"Wwise chunk {tag!r} exceeds the declared Bank payload.")
            break
        if tag in chunks:
            notes.append(f"Wwise Bank contains duplicate {tag!r} chunks; preserving both raw bytes.")
        else:
            chunks[tag] = data[payload_start:payload_end]
        offset = payload_end

    return chunks, tuple(notes), valid


def _inspect_hirc_envelope(data: bytes) -> tuple[int | None, tuple[str, ...], bool]:
    notes: list[str] = []
    if len(data) < 4:
        return None, ("HIRC chunk is smaller than its item-count field.",), False

    count = struct.unpack_from("<I", data)[0]
    offset = 4
    for _ in range(count):
        if offset + 5 > len(data):
            return count, ("HIRC item header exceeds the HIRC chunk.",), False
        item_size = struct.unpack_from("<I", data, offset + 1)[0]
        offset += 5
        if offset + item_size > len(data):
            return count, ("HIRC item payload exceeds the HIRC chunk.",), False
        offset += item_size

    trailing = data[offset:]
    if trailing and any(trailing):
        notes.append("HIRC chunk has non-zero bytes after its declared item list.")
    return count, tuple(notes), True


def _inspect_didx(data: bytes, media_data: bytes | None) -> tuple[int, tuple[str, ...], bool]:
    notes: list[str] = []
    if len(data) % 12:
        return 0, ("DIDX chunk size is not divisible by its 12-byte entry size.",), False

    count = len(data) // 12
    valid = True
    if count and media_data is None:
        valid = False
        notes.append("DIDX lists embedded media but the Bank has no DATA chunk.")
    if media_data is not None:
        for index in range(count):
            _source_id, offset, size = struct.unpack_from("<III", data, index * 12)
            if offset + size > len(media_data):
                valid = False
                notes.append(f"DIDX item {index} exceeds the DATA chunk.")
                break
    return count, tuple(notes), valid
