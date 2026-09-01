"""
output.py
=========
Writing artifacts, safely.

Ported from ``causalia-final/extractor/core/output.py``. Three of its four
invariants are unchanged and are the reason this module exists at all:

* ARTIFACT ALLOWLIST - the writer walks a list of names it is permitted to
  write. A name it does not recognise is refused, not silently skipped.
* STAT FENCE - the .wacz's (size, mtime_ns, inode) are captured before the read
  and re-checked after. If the archive changed under us the extraction is void.
  Content hashing is deliberately avoided: it would mean a second full read of
  every archive, ~2.1 TB across ripost alone.
* ATOMIC WRITES - every artifact is written to a temp file in the same directory
  and moved into place with os.replace, so a killed process never leaves a
  half-written JSON behind. Ownership is handed back to the corpus user when
  running as root.

CHANGED FROM THE PORT: the allowlist covers the new artifact set, and
``screenshot.*`` is now WRITABLE. Upstream it was a reserved name because that
extractor wrote into the corpus article directory, beside a screenshot the
backfill owned. This extractor writes to its own --output tree and never into
the corpus, so writing the screenshot there is exactly what it should do.
``page.wacz`` and every other archive suffix stay refused.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------
# What may be written
# --------------------------------------------------------------------

#: Exact artifact filenames this extractor produces in an article dir.
ARTIFACT_FILES = frozenset({
    "article.json", "content.json", "images.json", "videos.json",
    "links.json", "extraction.json", "readability.html", "original.html",
})

#: The subdirectories we create, and the only filenames allowed in them.
IMAGES_DIR = "images"
IMAGE_NAME_RE = re.compile(r"^image_\d{3}\.[A-Za-z0-9]{1,6}$")

#: Video localised out of the capture. A SEPARATE directory from images/
#: for two reasons: the review tooling copies artifacts without media by
#: naming one directory, and the --force sweep can then target exactly
#: what this extractor wrote.
#:
#: The name is POSITIONAL - video_001 is "the first video in this
#: article" - so it is re-derived on every run and is therefore ours to
#: delete. Anything else found in videos/ was put there by something
#: else and is left strictly alone (see remove_previous_artifacts). A
#: later backfill stage writing identity-keyed names must widen the
#: allowlist deliberately, not inherit permission from this one.
SCREENSHOT_NAME_RE = re.compile(r"^screenshot\.(png|jpg|jpeg|webp)$")

VIDEOS_DIR = "videos"
OWN_VIDEO_RE = re.compile(r"^video_\d{3}\.[A-Za-z0-9]{1,5}$")

#: Refused no matter what the allowlist says. This is the belt to the
#: allowlist's braces: widening ARTIFACT_FILES by mistake still cannot
#: reach the raw capture.
NEVER_WRITE_SUFFIXES = (".wacz", ".warc", ".warc.gz", ".cdx", ".cdxj")
NEVER_WRITE_STEMS = ("page", "datapackage", "datapackage-digest")


class UnsafeArtifact(Exception):
    """A write was attempted for a path that is not a known artifact."""


class ArchiveMutated(Exception):
    """page.wacz changed while we were reading it. Fatal for the batch."""


def _check_artifact_name(relative_name: str) -> None:
    """Raise unless ``relative_name`` is an artifact we are allowed to write.

    ``relative_name`` is relative to the article directory and may be
    either a bare filename or ``images/<file>``.
    """
    if not relative_name or relative_name != relative_name.strip():
        raise UnsafeArtifact(f"suspicious artifact name: {relative_name!r}")

    parts = Path(relative_name).parts
    if any(part in ("..", "") for part in parts) or Path(relative_name).is_absolute():
        raise UnsafeArtifact(f"path traversal or absolute path: {relative_name!r}")

    lowered = relative_name.lower()
    if lowered.endswith(NEVER_WRITE_SUFFIXES):
        raise UnsafeArtifact(f"refusing to write archive data: {relative_name!r}")
    if Path(lowered).stem in NEVER_WRITE_STEMS:
        raise UnsafeArtifact(f"refusing to write reserved name: {relative_name!r}")

    if len(parts) == 1:
        if SCREENSHOT_NAME_RE.match(relative_name):
            return
        if relative_name not in ARTIFACT_FILES:
            raise UnsafeArtifact(f"not a known artifact: {relative_name!r}")
        return

    if len(parts) == 2 and parts[0] == IMAGES_DIR:
        if not IMAGE_NAME_RE.match(parts[1]):
            raise UnsafeArtifact(f"not a valid image artifact: {relative_name!r}")
        return

    if len(parts) == 2 and parts[0] == VIDEOS_DIR:
        if not OWN_VIDEO_RE.match(parts[1]):
            raise UnsafeArtifact(f"not a valid video artifact: {relative_name!r}")
        return

    raise UnsafeArtifact(f"artifact outside the permitted layout: {relative_name!r}")


# --------------------------------------------------------------------
# The stat fence
# --------------------------------------------------------------------

@dataclass(frozen=True)
class ArchiveFingerprint:
    """Enough of page.wacz's identity to prove we did not modify it.

    Content hashing is deliberately NOT used: it would mean a second full
    read of every archive (~2.1 TB across ripost) to defend against a
    threat - us silently rewriting a file we only ever open read-only -
    that (size, mtime, inode) already detects.
    """
    size: int
    mtime_ns: int
    inode: int

    @classmethod
    def of(cls, path: Path) -> "ArchiveFingerprint":
        info = path.stat()
        return cls(size=info.st_size, mtime_ns=info.st_mtime_ns, inode=info.st_ino)


def verify_unchanged(path: Path, before: ArchiveFingerprint) -> None:
    """Raise ``ArchiveMutated`` if ``path`` differs from its snapshot."""
    after = ArchiveFingerprint.of(path)
    if after != before:
        raise ArchiveMutated(
            f"{path} changed during extraction: "
            f"{before} -> {after}. Aborting before more damage is possible."
        )


# --------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------

def _artifact_owner() -> tuple[int, int] | None:
    """(uid, gid) to hand artifacts back to, or None to leave them alone.

    Only meaningful when we are root. Running unprivileged (tests, the
    laptop) leaves ownership as-is, which is already correct there.
    """
    if os.geteuid() != 0:
        return None
    uid = int(os.environ.get("CAUSALIA_ARTIFACT_UID", "1009"))
    gid = int(os.environ.get("CAUSALIA_ARTIFACT_GID", "1010"))
    if uid < 0 or gid < 0:
        return None
    return uid, gid


FILE_MODE = 0o644     # matches page.wacz
DIR_MODE = 0o755


class ArtifactWriter:
    """Writes one article's artifacts, safely.

    Instantiate per article. ``dry_run=True`` makes every write a no-op
    that still runs the full safety check, so a dry run genuinely
    exercises the allowlist rather than bypassing it.
    """

    def __init__(self, article_dir: Path, *, dry_run: bool = False):
        self.article_dir = Path(article_dir)
        self.dry_run = dry_run
        self.written: list[str] = []
        self._owner = _artifact_owner()

    # -- internals ---------------------------------------------------

    def _finalise(self, fd: int, tmp_path: str, target: Path) -> None:
        """chmod/chown the temp file, then atomically move it into place."""
        os.fchmod(fd, FILE_MODE)
        if self._owner is not None:
            os.fchown(fd, *self._owner)
        os.close(fd)
        os.replace(tmp_path, target)

    def _ensure_dir(self, directory: Path) -> None:
        if directory.exists():
            return
        if self.dry_run:
            return
        directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(directory, DIR_MODE)
            if self._owner is not None:
                os.chown(directory, *self._owner)
        except OSError:
            # A pre-existing directory we do not own is fine; we only
            # need to be able to create files inside it.
            pass

    # -- public API --------------------------------------------------

    def write_bytes(self, relative_name: str, data: bytes) -> Path:
        """Write one artifact. Always safety-checked, even in a dry run."""
        _check_artifact_name(relative_name)
        target = self.article_dir / relative_name

        if self.dry_run:
            self.written.append(relative_name)
            return target

        self._ensure_dir(target.parent)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(target.parent), prefix=".causalia-tmp-", suffix=".part")
        try:
            os.write(fd, data)
            self._finalise(fd, tmp_path, target)
        except BaseException:
            # Leave nothing behind on any failure, including KeyboardInterrupt.
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        self.written.append(relative_name)
        return target

    def write_text(self, relative_name: str, text: str) -> Path:
        return self.write_bytes(relative_name, text.encode("utf-8"))

    def write_json(self, relative_name: str, payload) -> Path:
        # ensure_ascii=False keeps Hungarian readable in the artifacts;
        # the trailing newline makes them behave in a terminal and in git.
        body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        return self.write_text(relative_name, body)

    def remove_previous_artifacts(self) -> list[str]:
        """Delete this extractor's own prior output. Used only by --force.

        Walks the allowlist rather than the directory, so a file we do not
        recognise is not merely skipped - it is never even considered.
        """
        removed: list[str] = []

        for name in sorted(ARTIFACT_FILES):
            path = self.article_dir / name
            if path.is_file():
                _check_artifact_name(name)      # paranoia: re-verify before unlink
                if not self.dry_run:
                    path.unlink()
                removed.append(name)

        for directory in (IMAGES_DIR, VIDEOS_DIR):
            media = self.article_dir / directory
            if not media.is_dir():
                continue
            for path in sorted(media.iterdir()):
                if not path.is_file():
                    continue
                relative = f"{directory}/{path.name}"
                try:
                    _check_artifact_name(relative)
                except UnsafeArtifact:
                    # Something we did not write. Leave it strictly alone.
                    # This is what stops a --force re-extraction destroying
                    # media that cost a network fetch to obtain.
                    continue
                if not self.dry_run:
                    path.unlink()
                removed.append(relative)

        return removed
