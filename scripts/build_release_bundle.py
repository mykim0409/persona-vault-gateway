#!/usr/bin/env python3
"""Build the PersonaVault Gateway install bundle for one release.

Standard library only. Files come from the explicit ALLOWLIST (never a directory walk), so .env, secrets/ and
personal notes cannot enter the archive. The Compose PVG_IMAGE default is pinned to an exact GHCR digest, and the
archive and checksum bytes are deterministic for the same inputs. Tag, package version and image are validated
before anything is written.
"""
import argparse
import gzip
import hashlib
import io
import re
import sys
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NUM = r"(?:0|[1-9][0-9]*)"
TAG_RE = re.compile(rf"gateway-v({NUM}\.{NUM}\.{NUM})")
NAME = r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
IMAGE_RE = re.compile(rf"ghcr\.io/{NAME}(?:/{NAME})+@sha256:[0-9a-f]{{64}}")
UNPINNED = "${PVG_IMAGE:-persona-vault-gateway:local}"

# Everything that ships, relative to the repo root. docs/setup.md links the other docs.
ALLOWLIST = (
    "compose.yml",
    ".env.example",
    "LICENSE",
    "SECURITY.md",
    "docs/setup.md",
    "docs/operations.md",
    "docs/CURATOR.md",
    "docs/metadata.md",
    "docs/evaluation-error-book.md",
)


class ReleaseError(ValueError):
    pass


def validate_release(tag, root=ROOT):
    """Return the version for a gateway-vX.Y.Z tag that matches the package version in pyproject.toml."""
    match = TAG_RE.fullmatch(tag)
    if not match:
        raise ReleaseError(f"tag must look like gateway-vX.Y.Z, got {tag!r}")
    version = match.group(1)
    try:
        package = tomllib.loads((Path(root) / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    except (OSError, ValueError, KeyError) as exc:
        raise ReleaseError(f"cannot read the package version from pyproject.toml: {exc}") from exc
    if package != version:
        raise ReleaseError(f"tag version {version} does not match package version {package}")
    return version


def validate_image(image):
    if not IMAGE_RE.fullmatch(image):
        raise ReleaseError(f"image must be an exact ghcr.io/OWNER/NAME@sha256:<64 hex> reference, got {image!r}")
    return image


def pin_compose(text, image):
    """Replace the PVG_IMAGE default (gateway and init) with the digest reference."""
    count = text.count(UNPINNED)
    if count != 2:
        raise ReleaseError(f"compose.yml must contain the PVG_IMAGE default exactly twice (gateway, init), found {count}")
    pinned = text.replace(UNPINNED, "${PVG_IMAGE:-" + image + "}")
    unpinned = [l.strip() for l in pinned.splitlines() if l.strip().startswith("image:") and "@sha256:" not in l]
    if unpinned:
        raise ReleaseError(f"compose.yml still has an image without a digest: {unpinned[0]}")
    return pinned


def read_allowed(root, rel):
    root = Path(root).resolve()
    path = root / rel
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
        raise ReleaseError(f"allowlisted file is missing or not a regular file inside the repo: {rel}")
    return path.read_bytes()


def make_archive(base, files):
    """Deterministic tar.gz of {relative path: bytes} under base/: sorted, fixed mtime/owner/mode, no gzip name."""
    dirs = sorted({base} | {f"{base}/{rel.rsplit('/', 1)[0]}" for rel in files if "/" in rel})
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, compresslevel=9, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for name in dirs:
                info = tarfile.TarInfo(name)
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            for rel in sorted(files):
                info = tarfile.TarInfo(f"{base}/{rel}")
                info.size, info.mode = len(files[rel]), 0o644
                tar.addfile(info, io.BytesIO(files[rel]))
    return buf.getvalue()


def build_bundle(root, tag, image, output_dir):
    """Validate, read the allowlist, then write the archive and its .sha256. Returns both paths."""
    version = validate_release(tag, root)
    validate_image(image)
    files = {rel: read_allowed(root, rel) for rel in ALLOWLIST}
    files["compose.yml"] = pin_compose(files["compose.yml"].decode("utf-8"), image).encode("utf-8")
    base = f"persona-vault-gateway-{version}"
    data = make_archive(base, files)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"{base}-install.tar.gz"
    checksum = output_dir / f"{archive.name}.sha256"
    archive.write_bytes(data)
    checksum.write_text(f"{hashlib.sha256(data).hexdigest()}  {archive.name}\n", encoding="utf-8")
    return archive, checksum


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the PersonaVault Gateway install bundle.")
    parser.add_argument("--tag", required=True, help="gateway-vX.Y.Z; must match the package version in pyproject.toml")
    parser.add_argument("--image", help="exact reference ghcr.io/OWNER/NAME@sha256:<64 hex>")
    parser.add_argument("--output-dir", type=Path, default=Path("dist/release"))
    parser.add_argument("--source-root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--check-only", action="store_true", help="validate tag and package version, write nothing")
    args = parser.parse_args(argv)
    try:
        if args.check_only:
            print(validate_release(args.tag, args.source_root))
            return 0
        if not args.image:
            raise ReleaseError("--image is required unless --check-only")
        archive, checksum = build_bundle(args.source_root, args.tag, args.image, args.output_dir)
    except ReleaseError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(archive)
    print(checksum)
    return 0


if __name__ == "__main__":
    sys.exit(main())
