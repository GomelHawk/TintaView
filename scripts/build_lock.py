"""Build, or check an install against, the release's dependency lock files.

    python scripts/build_lock.py build <out-dir>
    <venv-python> scripts/build_lock.py verify <lock-file>

`build` writes the two files a release ships next to the wheel (build.yml runs it; the
smoke-install jobs in ci.yml run it too, so the installers' locked path is exercised on
every OS before a release ever depends on it):

    requirements-lock.txt            the desktop install: [ui] + [openrgb]
    requirements-headless-lock.txt   `install.sh --headless`: [openrgb] only

Each pins every dependency to one version and lists the SHA-256 of every file published
for it (all platforms, all Python versions), which is what lets one file made on Linux
install on Windows and macOS. `install.sh` / `install.ps1` hand it to
`pip install --require-hashes`. Needs `pip-tools` (`pip install pip-tools`).

`verify`, run with an installed venv's own Python, fails unless that venv was installed
*from* the lock: every pinned package present at exactly that version and recorded by
pip as explicitly requested. A dependency pulled in by the unpinned fallback has the
right version on the day the lock is made, but never that record — so this is what
tells "the locked path ran" from "it quietly fell back".
"""

from __future__ import annotations

import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: file name -> the extras it covers.
LOCKS = {
    "requirements-lock.txt": ("ui", "openrgb"),
    "requirements-headless-lock.txt": ("openrgb",),
}

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\;]+)")


def build(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, extras in LOCKS.items():
        cmd = [sys.executable, "-m", "piptools", "compile", "--quiet", "--generate-hashes",
               "--strip-extras", "--no-header", "--output-file", str(out_dir / name)]
        for extra in extras:
            cmd += ["--extra", extra]
        cmd.append(str(REPO_ROOT / "pyproject.toml"))
        done = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
        if done.returncode != 0:
            print(f"pip-compile failed for {name}", file=sys.stderr)
            return done.returncode
        pins = pinned(out_dir / name)
        if not pins:
            print(f"{name} pins nothing — refusing to ship an empty lock", file=sys.stderr)
            return 1
        print(f"{name}: {', '.join(f'{n}=={v}' for n, v in pins)}")
    return 0


def pinned(lock: Path) -> list[tuple[str, str]]:
    """Every ``name==version`` the lock file pins, in file order."""
    out = []
    for line in lock.read_text(encoding="utf-8").splitlines():
        match = _PIN.match(line)
        if match:
            out.append((match.group(1), match.group(2)))
    return out


def verify(lock: Path) -> int:
    problems = []
    for name, version in pinned(lock):
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            problems.append(f"{name} is not installed")
            continue
        if dist.version != version:
            problems.append(f"{name} is {dist.version}, the lock pins {version}")
        elif dist.read_text("REQUESTED") is None:
            problems.append(f"{name} was not installed from the lock (pulled in as a "
                            "dependency — the unpinned fallback ran)")
    for problem in problems:
        print(problem, file=sys.stderr)
    if not problems:
        print(f"installed from {lock.name}: {len(pinned(lock))} pinned packages")
    return 1 if problems else 0


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "build":
        return build(Path(argv[1]))
    if len(argv) == 2 and argv[0] == "verify":
        return verify(Path(argv[1]))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
