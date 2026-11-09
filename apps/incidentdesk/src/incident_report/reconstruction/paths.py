"""Only configured fixed-local export bundles can enter the workspace."""

import os
from pathlib import Path

from incident_report.evidence_import.models import MAX_FILES
from incident_report.evidence_import.reader import InvalidBundle, safe_path


def local_root(root: Path) -> Path:
    if str(root).replace("\\", "/").startswith("//"):
        raise InvalidBundle("exports root must be local")
    root = root.absolute()
    if os.name == "nt":
        import ctypes

        if ctypes.windll.kernel32.GetDriveTypeW(root.anchor) != 3:
            raise InvalidBundle("exports root must be a fixed local drive")
    for path in (root, *root.parents):
        if path.is_symlink() or path.is_junction():
            raise InvalidBundle("exports root must not traverse links")
    if not root.is_dir():
        raise InvalidBundle("exports root is not available")
    return root.resolve(strict=True)


def bundle_path(root: Path, relative: str) -> Path:
    parts = safe_path(relative)
    resolved_root = local_root(root)
    target = resolved_root.joinpath(*parts.parts)
    for path in (target, *target.parents):
        if path.is_symlink() or path.is_junction():
            raise InvalidBundle("bundle path must not traverse links")
        if path == resolved_root:
            break
    resolved = target.resolve(strict=True)
    if not resolved.is_relative_to(resolved_root) or resolved == resolved_root:
        raise InvalidBundle("bundle is outside exports root")
    if not resolved.is_dir():
        raise InvalidBundle("bundle must be a directory")
    return resolved


def list_bundles(root: Path) -> tuple[str, ...]:
    resolved = local_root(root)
    found = []
    seen = 0
    for directory, children, files in os.walk(resolved, followlinks=False):
        seen += 1
        if seen > MAX_FILES:
            break
        parent = Path(directory)
        children[:] = [
            name for name in children if not (parent / name).is_symlink() and not (parent / name).is_junction()
        ]
        if "bundle.json" in files and parent != resolved:
            relative = parent.relative_to(resolved).as_posix()
            try:
                bundle_path(resolved, relative)
            except (ValueError, OSError):
                continue
            found.append(relative)
    return tuple(sorted(found))
