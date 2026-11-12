"""Maintain, or check an install against, the dependency lock files in `packaging/`.

    python scripts/build_lock.py build      # add what pyproject.toml now needs; keep every pin
    python scripts/build_lock.py upgrade    # move every pin to the newest release
    python scripts/build_lock.py check      # CI: do the committed locks still fit pyproject.toml?
    python scripts/build_lock.py outdated   # is anything pinned behind its newest release?
    <venv-python> scripts/build_lock.py verify <lock-file>

The two files are **committed**, and a release ships them as they are:

    packaging/requirements-lock.txt            the desktop install: [ui] + [openrgb]
    packaging/requirements-headless-lock.txt   `install.sh --headless`: [openrgb] only

Committed rather than generated at release time, because the pins are what every user's
update installs: a lock regenerated per release moved to the newest PySide6 whenever one
had come out, and an update that should replace one 0.7 MB wheel re-downloaded about
260 MB of Qt. Now a pin moves only when `upgrade` is run on purpose and its diff is
committed — which is also the moment to look at what is being pulled in.

Pinned must not come to mean stale: an old Qt is an unpatched Qt. `outdated` asks PyPI
for each pinned package's newest release and exits 1 when a pin is behind; the weekly
`deps.yml` workflow runs it, and `pip-audit` against both locks, so a newer release or a
published vulnerability turns a run red without anyone having to remember to look.

Each pins every dependency to one version and lists the SHA-256 of every file published
for it (all platforms, all Python versions), which is what lets one file made on Linux
install on Windows and macOS. `install.sh` / `install.ps1` hand it to
`pip install --require-hashes`. `build`, `upgrade` and `check` need `pip-tools`
(`pip install pip-tools`) and the network.

`verify`, run with an installed venv's own Python, fails unless that venv was installed
*from* the lock: every pinned package present at exactly that version and recorded by
pip as explicitly requested. A dependency pulled in by the unpinned fallback has the
right version on the day the lock is made, but never that record — so this is what
tells "the locked path ran" from "it quietly fell back".
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from importlib import metadata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK_DIR = REPO_ROOT / "packaging"

#: file name -> the extras it covers.
LOCKS = {
    "requirements-lock.txt": ("ui", "openrgb"),
    "requirements-headless-lock.txt": ("openrgb",),
}

#: The release build's own tools: source list -> pinned, hashed file. Installed by
#: build.yml and by nothing else — users never get these. Kept out of `outdated` (they
#: are not shipped, and setuptools releases weekly) but audited with the rest.
BUILD_TOOLS_IN = "build-requirements.in"
BUILD_TOOLS = "build-requirements.txt"

_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\;]+)")


def build(out_dir: Path, upgrade: bool = False) -> int:
    """Write both locks into `out_dir`. An existing file's pins are kept (pip-compile
    reuses whatever its output file already pins and still satisfies) unless `upgrade`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, extras in LOCKS.items():
        cmd = [sys.executable, "-m", "piptools", "compile", "--quiet", "--generate-hashes",
               "--strip-extras", "--no-header", "--no-annotate",
               "--output-file", str(out_dir / name)]
        if upgrade:
            cmd.append("--upgrade")
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
    return _build_tools(out_dir, upgrade)


def _build_tools(out_dir: Path, upgrade: bool) -> int:
    cmd = [sys.executable, "-m", "piptools", "compile", "--quiet", "--generate-hashes",
           "--allow-unsafe", "--no-header", "--no-annotate",
           "--output-file", str(out_dir / BUILD_TOOLS)]
    if upgrade:
        cmd.append("--upgrade")
    cmd.append(str(LOCK_DIR / BUILD_TOOLS_IN))
    done = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
    if done.returncode != 0:
        print(f"pip-compile failed for {BUILD_TOOLS}", file=sys.stderr)
        return done.returncode
    pins = pinned(out_dir / BUILD_TOOLS)
    print(f"{BUILD_TOOLS}: {', '.join(f'{n}=={v}' for n, v in pins)}")
    return 0 if pins else 1


def check(lock_dir: Path) -> int:
    """Fail when a committed lock no longer fits `pyproject.toml` — a dependency added,
    removed, or its allowed range moved past the pin. Recompiles copies (keeping pins)
    and compares what is pinned; the hash lists are not compared, since PyPI gains files
    for an already-pinned version (a wheel for a new Python) without anything changing."""
    with tempfile.TemporaryDirectory(prefix="tintaview-lock-") as tmp:
        scratch = Path(tmp)
        for name in (*LOCKS, BUILD_TOOLS):
            if not (lock_dir / name).is_file():
                print(f"{lock_dir / name} is missing — run `python scripts/build_lock.py build`",
                      file=sys.stderr)
                return 1
            shutil.copyfile(lock_dir / name, scratch / name)
        code = build(scratch)
        if code != 0:
            return code
        stale = [name for name in (*LOCKS, BUILD_TOOLS)
                 if pinned(scratch / name) != pinned(lock_dir / name)]
    for name in stale:
        print(f"packaging/{name} no longer matches pyproject.toml — run "
              "`python scripts/build_lock.py build` and commit the result", file=sys.stderr)
    return 1 if stale else 0


def newest_release(name: str, timeout: float = 20.0) -> str:
    """The newest non-yanked release PyPI lists for `name`. Raises OSError/ValueError."""
    req = urllib.request.Request(f"https://pypi.org/pypi/{name}/json",
                                 headers={"User-Agent": "TintaView-lock-check"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
        return str(json.loads(resp.read())["info"]["version"])


def outdated(lock_dir: Path, newest=newest_release) -> int:
    """Exit 1 when any pin is behind the newest release (or PyPI cannot be asked — an
    unanswered check must not pass as "up to date")."""
    pins = sorted({pin for name in LOCKS for pin in pinned(lock_dir / name)})
    behind, failed = [], []
    for name, version in pins:
        try:
            latest = newest(name)
        except (OSError, ValueError, KeyError) as exc:
            failed.append(f"{name}: could not ask PyPI ({exc})")
            continue
        marker = "" if latest == version else f"   <- newest is {latest}"
        print(f"{name}=={version}{marker}")
        if latest != version:
            behind.append(name)
    for line in failed:
        print(line, file=sys.stderr)
    if behind:
        print(f"\n{len(behind)} pinned package(s) behind their newest release. Bump them with "
              "`python scripts/build_lock.py upgrade`, run the tests, and commit the locks.",
              file=sys.stderr)
    return 1 if behind or failed else 0


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
    if argv == ["build"]:
        return build(LOCK_DIR)
    if argv == ["upgrade"]:
        return build(LOCK_DIR, upgrade=True)
    if argv == ["check"]:
        return check(LOCK_DIR)
    if argv == ["outdated"]:
        return outdated(LOCK_DIR)
    if len(argv) == 2 and argv[0] == "verify":
        return verify(Path(argv[1]))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
