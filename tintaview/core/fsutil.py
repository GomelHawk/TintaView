"""Writing files other things read while we write them — the one atomic-write helper.

Every file TintaView rewrites is read by something that does not wait for it: an agent's
own config, the hook script an agent may run mid-install, `config.toml` under a tray that
polls it. So a write is always "a complete temp file beside the target, then a rename",
and the temp file is created with its final permissions rather than given them
afterwards: `config.toml` holds whatever token the user put in a notify command and
`~/.claude.json` is 0600, and a copy that is world-readable until a later `chmod` is
readable in between.

Stdlib only, like the rest of `core/`.
"""

from __future__ import annotations

import contextlib
import os
import stat
import sys
import threading
from pathlib import Path

#: Owner-only: what a file holding the user's commands, tokens or usage gets.
PRIVATE_FILE = 0o600
PRIVATE_DIR = 0o700


def file_mode(path: Path) -> int | None:
    """`path`'s permission bits, or None when it does not exist or cannot be read."""
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def atomic_write(path: Path, data: str | bytes, *, mode: int | None = None,
                 encoding: str = "utf-8") -> None:
    """Replace `path` with `data` in one rename; a reader sees the old file or the new one.

    `mode` is the permission the file ends up with. Left None, an existing file keeps its
    own and a new one gets the umask's default — except that the temp file is never more
    readable than the result. Text is written the way `Path.write_text` writes it (so
    `\\n` still becomes `\\r\\n` on Windows); bytes are written untouched.

    Raises OSError like any write; the temp file is removed on the way out.
    """
    path = Path(path)
    if path.is_symlink():
        # Write the file the link points at. Renaming over the link itself replaced a
        # `settings.json` symlinked into a dotfiles repository with a regular file, and
        # the repository's copy never got the change.
        path = path.resolve()
    final = mode if mode is not None else file_mode(path)
    # Unique per writer: two threads, or a stale second process, must not share a temp.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tintaview-tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    with contextlib.suppress(OSError):
        tmp.unlink()  # a leftover from a crashed writer with the same ids
    fd = os.open(tmp, flags, final if final is not None else 0o666)
    try:
        if isinstance(data, bytes):
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        else:
            with os.fdopen(fd, "w", encoding=encoding) as fh:
                fh.write(data)
        if final is not None and sys.platform != "win32":
            # `os.open`'s mode is masked by the umask; this makes it exact.
            os.chmod(tmp, final)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def ensure_private_dir(path: Path) -> None:
    """Create `path` if needed and make it owner-only. Never raises.

    A no-op for permissions on Windows, where the directory sits under the user's own
    profile and `chmod` means nothing.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            os.chmod(path, PRIVATE_DIR)
    except OSError:
        pass
