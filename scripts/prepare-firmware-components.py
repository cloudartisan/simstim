#!/usr/bin/env python3
"""Export the exact IDF components in k151-standalone/dependencies.lock."""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

COMPONENTS = (
    "cmake_utilities",
    "esp_cam_sensor",
    "esp_sccb_intf",
    "esp_video",
    "usb_host_uvc",
)


def pinned_hashes(lock_text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for component in COMPONENTS:
        pattern = (
            rf"^  espressif/{re.escape(component)}:\s*\n"
            rf"    component_hash: ([0-9a-f]{{64}})$"
        )
        match = re.search(pattern, lock_text, re.MULTILINE)
        if not match:
            raise ValueError(f"missing/invalid lock hash for espressif/{component}")
        result[component] = match.group(1)
    return result


def _native_hash_validator():
    """Load the verifier shipped with the active ESP-IDF Python environment."""
    try:
        from idf_component_tools.hash_tools.validate import validate_hash_eq_hashdir
    except ImportError as exc:
        raise RuntimeError(
            "ESP-IDF Component Manager verifier is unavailable; source the ESP-IDF 5.5.6 environment first"
        ) from exc
    return validate_hash_eq_hashdir


def _reject_unsafe_symlinks(component: Path) -> None:
    root = component.resolve(strict=True)
    for path in component.rglob("*"):
        if not path.is_symlink():
            continue
        link = Path(path.readlink())
        if link.is_absolute():
            raise ValueError(f"absolute symlink is not allowed: {path}")
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"broken or looping symlink is not allowed: {path}") from exc
        if not resolved.is_relative_to(root):
            raise ValueError(f"symlink escapes component directory: {path} -> {link}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path,
                        help="ESP-IDF managed_components from the locked resolver build")
    parser.add_argument("--destination", required=True, type=Path,
                        help="new mirror directory to create")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    lock = repo / "k151-standalone" / "dependencies.lock"
    try:
        pins = pinned_hashes(lock.read_text(encoding="utf-8"))
        if not args.source.is_dir():
            raise ValueError(f"component source directory does not exist: {args.source}")
        if args.destination.exists() and any(args.destination.iterdir()):
            raise ValueError(f"destination must be absent or empty: {args.destination}")
        validator = _native_hash_validator()
        verified: list[tuple[Path, Path, str]] = []
        for name, expected_hash in pins.items():
            source = args.source / f"espressif__{name}"
            marker = source / ".component_hash"
            if source.is_symlink() or not source.is_dir() or not marker.is_file():
                raise ValueError(f"missing extracted component or .component_hash: {source}")
            _reject_unsafe_symlinks(source)
            try:
                # Recompute against the pinned lock hash. This is the same
                # content-hash implementation used by IDF Component Manager;
                # trusting the marker alone would miss modified source files.
                validator(source, expected_hash)
            except Exception as exc:
                raise ValueError(f"content hash mismatch for espressif/{name}: {exc}") from exc
            verified.append((source, args.destination / f"espressif__{name}", expected_hash))
        args.destination.mkdir(parents=True, exist_ok=True)
        for source, target, _ in verified:
            shutil.copytree(source, target, symlinks=True)
        for _, target, expected_hash in verified:
            try:
                validator(target, expected_hash)
            except Exception as exc:
                raise ValueError(f"post-copy content hash verification failed: {target}: {exc}") from exc
        print(f"Exported and verified {len(verified)} locked components to {args.destination}")
        return 0
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        print(f"prepare-firmware-components: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
