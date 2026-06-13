"""Merge a maturin-built native wheel into the pure wheel of this project.

The PyPI project ships a single distribution: platform wheels carry the
compiled native module alongside the Python package, and the pure
py3-none-any wheel remains as the fallback pip selects on platforms
without a matching wheel. This script takes both wheels, copies the
native extension into the pure wheel tree, retags it with the native
wheel's platform tags, and repacks.

Usage:
    python scripts/repack_native_wheel.py \
        --pure-wheel dist/pkg-0.2.1-py3-none-any.whl \
        --native-wheel rust/<crate>/dist/<native>-0.2.1-cp311-abi3-....whl \
        --out dist/
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _unpack(wheel: Path, dest: Path) -> Path:
    subprocess.run(
        [sys.executable, "-m", "wheel", "unpack", str(wheel), "-d", str(dest)],
        check=True,
    )
    trees = [p for p in dest.iterdir() if p.is_dir()]
    if len(trees) != 1:
        raise SystemExit(f"expected one unpacked tree in {dest}, found {len(trees)}")
    return trees[0]


def _dist_info(tree: Path) -> Path:
    infos = list(tree.glob("*.dist-info"))
    if len(infos) != 1:
        raise SystemExit(f"expected one dist-info in {tree}, found {len(infos)}")
    return infos[0]


def _native_tags(native_tree: Path) -> list[str]:
    wheel_meta = (_dist_info(native_tree) / "WHEEL").read_text(encoding="utf-8")
    tags = [line.split(":", 1)[1].strip() for line in wheel_meta.splitlines() if line.startswith("Tag:")]
    if not tags:
        raise SystemExit("native wheel has no Tag entries")
    return tags


def merge(pure_wheel: Path, native_wheel: Path, out_dir: Path) -> Path:
    with tempfile.TemporaryDirectory() as scratch:
        scratch_path = Path(scratch)
        pure_tree = _unpack(pure_wheel, scratch_path / "pure")
        native_tree = _unpack(native_wheel, scratch_path / "native")

        # Copy everything except packaging metadata: for an abi3 cdylib
        # module that is the single compiled extension (.so / .pyd).
        copied = 0
        for item in native_tree.iterdir():
            if item.name.endswith(".dist-info"):
                continue
            target = pure_tree / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
            copied += 1
        if copied == 0:
            raise SystemExit("native wheel contained no payload to merge")

        wheel_file = _dist_info(pure_tree) / "WHEEL"
        lines = [
            line
            for line in wheel_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith(("Tag:", "Root-Is-Purelib:"))
        ]
        lines.append("Root-Is-Purelib: false")
        lines.extend(f"Tag: {tag}" for tag in _native_tags(native_tree))
        wheel_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

        out_dir.mkdir(parents=True, exist_ok=True)
        before = set(out_dir.glob("*.whl"))
        subprocess.run(
            [sys.executable, "-m", "wheel", "pack", str(pure_tree), "-d", str(out_dir)],
            check=True,
        )
        produced = set(out_dir.glob("*.whl")) - before
        if len(produced) != 1:
            raise SystemExit(f"expected one repacked wheel, found {len(produced)}")
        return produced.pop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pure-wheel", type=Path, required=True)
    parser.add_argument("--native-wheel", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    merged = merge(args.pure_wheel, args.native_wheel, args.out)
    print(merged)


if __name__ == "__main__":
    main()
