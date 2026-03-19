import argparse
import hashlib
import json
from pathlib import Path

from hd2_patch_fixer.archive import StreamToc
from hd2_patch_fixer.constants import TYPE_NAME_MAP, UnitID


def find_base_patch(path: Path) -> Path:
    if path.is_file():
        return path
    candidates = []
    for candidate in path.iterdir():
        if candidate.is_file() and ".patch_" in candidate.name and not candidate.name.endswith(".gpu_resources") and not candidate.name.endswith(".stream"):
            candidates.append(candidate)
    if len(candidates) != 1:
        raise ValueError(f"Expected exactly one base patch file in {path}, found {len(candidates)}")
    return candidates[0]


def sha16(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def first_diff_offset(left: bytes, right: bytes):
    limit = min(len(left), len(right))
    for index in range(limit):
        if left[index] != right[index]:
            return index
    if len(left) != len(right):
        return limit
    return None


def format_hex_window(data: bytes, offset: int, radius: int = 16) -> str:
    start = max(0, offset - radius)
    end = min(len(data), offset + radius)
    return " ".join(f"{byte:02X}" for byte in data[start:end])


def dump_entry(entry, output_dir: Path, label: str):
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{entry.file_id}_{label}"
    (output_dir / f"{stem}.toc.bin").write_bytes(bytes(entry.toc_data))
    (output_dir / f"{stem}.gpu.bin").write_bytes(bytes(entry.gpu_data))
    (output_dir / f"{stem}.stream.bin").write_bytes(bytes(entry.stream_data))


def compare_entries(fixed_entry, broken_entry):
    payloads = {
        "toc": (bytes(fixed_entry.toc_data), bytes(broken_entry.toc_data)),
        "gpu": (bytes(fixed_entry.gpu_data), bytes(broken_entry.gpu_data)),
        "stream": (bytes(fixed_entry.stream_data), bytes(broken_entry.stream_data)),
    }
    result = {}
    for name, (fixed_data, broken_data) in payloads.items():
        diff_offset = first_diff_offset(fixed_data, broken_data)
        result[name] = {
            "fixed_len": len(fixed_data),
            "broken_len": len(broken_data),
            "fixed_sha16": sha16(fixed_data),
            "broken_sha16": sha16(broken_data),
            "first_diff_offset": diff_offset,
            "fixed_window": None if diff_offset is None else format_hex_window(fixed_data, diff_offset),
            "broken_window": None if diff_offset is None else format_hex_window(broken_data, diff_offset),
        }
    return result


def make_summary(patch: StreamToc):
    items = []
    for type_id, entries in sorted(patch.toc_dict.items()):
        items.append(
            {
                "type_id": type_id,
                "type_name": TYPE_NAME_MAP.get(type_id, str(type_id)),
                "count": len(entries),
                "entry_ids": sorted(entries.keys()),
            }
        )
    return items


def main():
    parser = argparse.ArgumentParser(description="Dump and compare two HD2 patch folders/files.")
    parser.add_argument("--fixed", default="patch/fixed patch", help="Working patch folder or base patch file")
    parser.add_argument("--broken", default="patch/old patch", help="Broken patch folder or base patch file")
    parser.add_argument("--out", default="patch/analysis_output", help="Output folder for dumps and report")
    args = parser.parse_args()

    fixed_base = find_base_patch(Path(args.fixed))
    broken_base = find_base_patch(Path(args.broken))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    fixed_patch = StreamToc()
    broken_patch = StreamToc()
    if not fixed_patch.from_file(str(fixed_base)):
        raise ValueError(f"Failed to load fixed patch: {fixed_base}")
    if not broken_patch.from_file(str(broken_base)):
        raise ValueError(f"Failed to load broken patch: {broken_base}")

    report = {
        "fixed_patch": str(fixed_base),
        "broken_patch": str(broken_base),
        "fixed_summary": make_summary(fixed_patch),
        "broken_summary": make_summary(broken_patch),
        "unit_comparisons": [],
    }

    fixed_units = fixed_patch.toc_dict.get(UnitID, {})
    broken_units = broken_patch.toc_dict.get(UnitID, {})
    all_unit_ids = sorted(set(fixed_units.keys()) | set(broken_units.keys()))

    unit_dump_dir = out_dir / "unit_dump"
    for unit_id in all_unit_ids:
        fixed_entry = fixed_units.get(unit_id)
        broken_entry = broken_units.get(unit_id)
        unit_item = {
            "unit_id": unit_id,
            "fixed_present": fixed_entry is not None,
            "broken_present": broken_entry is not None,
        }
        if fixed_entry is not None:
            dump_entry(fixed_entry, unit_dump_dir / "fixed", "fixed")
        if broken_entry is not None:
            dump_entry(broken_entry, unit_dump_dir / "broken", "broken")
        if fixed_entry is not None and broken_entry is not None:
            unit_item["comparison"] = compare_entries(fixed_entry, broken_entry)
        report["unit_comparisons"].append(unit_item)

    (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = []
    lines.append(f"Fixed patch: {fixed_base}")
    lines.append(f"Broken patch: {broken_base}")
    lines.append("")
    lines.append("Summary by type")
    lines.append("Fixed:")
    for item in report["fixed_summary"]:
        lines.append(f"  {item['type_name']} ({item['type_id']}): {item['count']}")
    lines.append("Broken:")
    for item in report["broken_summary"]:
        lines.append(f"  {item['type_name']} ({item['type_id']}): {item['count']}")
    lines.append("")
    lines.append("Unit comparisons")
    for item in report["unit_comparisons"]:
        lines.append(f"UNIT {item['unit_id']}")
        if not item["fixed_present"] or not item["broken_present"]:
            lines.append(f"  missing in one side: fixed={item['fixed_present']} broken={item['broken_present']}")
            continue
        for payload_name, payload_info in item["comparison"].items():
            lines.append(
                "  "
                + f"{payload_name.upper()}: "
                + f"fixed_len={payload_info['fixed_len']} broken_len={payload_info['broken_len']} "
                + f"fixed_sha={payload_info['fixed_sha16']} broken_sha={payload_info['broken_sha16']} "
                + f"first_diff={payload_info['first_diff_offset']}"
            )
            if payload_info["first_diff_offset"] is not None:
                lines.append(f"    fixed : {payload_info['fixed_window']}")
                lines.append(f"    broken: {payload_info['broken_window']}")

    (out_dir / "report.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"Report written to: {out_dir / 'report.txt'}")


if __name__ == "__main__":
    main()
