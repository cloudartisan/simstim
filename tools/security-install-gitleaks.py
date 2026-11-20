#!/usr/bin/env python3
"""Install the repository-pinned Gitleaks binary with checksum verification."""
from __future__ import annotations

import hashlib
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

VERSION = "8.30.0"
ASSETS = {
    "linux_arm64": "b4cbbb6ddf7d1b2a603088cd03a4e3f7ce48ee7fd449b51f7de6ee2906f5fa2f",
    "linux_x64": "79a3ab579b53f71efd634f3aaf7e04a0fa0cf206b7ed434638d1547a2470a66e",
    "darwin_arm64": "b251ab2bcd4cd8ba9e56ff37698c033ebf38582b477d21ebd86586d927cf87e7",
    "darwin_x64": "ca221d012d247080c2f6f61f4b7a83bffa2453806b0c195c795bbe9a8c775ed5",
}


def main() -> int:
    system = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(platform.machine().lower())
    key = f"{system}_{arch}"
    if key not in ASSETS:
        raise SystemExit(f"Unsupported platform for pinned Gitleaks: {platform.system()}/{platform.machine()}")

    repo = Path(__file__).resolve().parents[1]
    git_path = subprocess.check_output(
        ["git", "rev-parse", "--git-path", "simstim-security/gitleaks"],
        cwd=repo, text=True,
    ).strip()
    destination = (repo / git_path).resolve()
    if destination.exists():
        try:
            output = subprocess.check_output([str(destination), "version"], text=True).strip()
            if output == VERSION:
                print(f"Gitleaks {VERSION} already installed at {destination}")
                return 0
        except (OSError, subprocess.CalledProcessError):
            pass

    asset = f"gitleaks_{VERSION}_{key}.tar.gz"
    url = f"https://github.com/gitleaks/gitleaks/releases/download/v{VERSION}/{asset}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="simstim-gitleaks-") as tmp:
        archive = Path(tmp) / asset
        request = urllib.request.Request(url, headers={"User-Agent": "simstim-security-setup"})
        with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as out:
            shutil.copyfileobj(response, out)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != ASSETS[key]:
            raise SystemExit(f"Gitleaks archive checksum mismatch for {asset}")
        with tarfile.open(archive, "r:gz") as bundle:
            member = bundle.extractfile("gitleaks")
            if member is None:
                raise SystemExit("Pinned Gitleaks archive has no gitleaks executable")
            temporary = destination.with_suffix(".new")
            temporary.write_bytes(member.read())
        temporary.chmod(0o755)
        temporary.replace(destination)
    print(f"Installed checksum-verified Gitleaks {VERSION} at {destination}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, tarfile.TarError) as exc:
        print(f"security dependency installation failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
