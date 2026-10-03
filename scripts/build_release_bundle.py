#!/usr/bin/env python3
"""Build the PersonaVault Gateway install bundle for one release.

Standard library only. Files come from the explicit ALLOWLIST (never a directory walk), so .env, secrets/ and
personal notes cannot enter the archive. The PVG_IMAGE default of compose.yml (its single Gateway service) is pinned
to an exact GHCR digest, and so is the tag reference in the Render and Railway recipes. Every pin must match exactly
the expected number of times, and the archive and checksum bytes are deterministic for the same inputs. The pinned
compose.yml is also written next to the archive as a standalone download with its own checksum. Tag, package
version and image are validated before anything is written.
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
STANDALONE_COMPOSE = "compose.yml"
# How many PVG_IMAGE defaults each shipped Compose file must contain. Never relax a count to make a file pass.
COMPOSE_IMAGE_COUNTS = {STANDALONE_COMPOSE: 1}
# Provider recipes reference the released image by tag in the repository; the bundle copy gets the digest instead.
PROVIDER_IMAGE_FILES = ("render.yaml", ".railway/railway.ts")

# Everything that ships, relative to the repo root. docs/setup.md links the other docs. Every shipped Markdown doc ships
# with its .ko.md reader translation; docs/WORKING_AGREEMENT.md is deliberately not shipped (and neither is its translation).
ALLOWLIST = (
    "compose.yml",
    "LICENSE",
    "SECURITY.md",
    "SECURITY.ko.md",
    "docs/setup.md",
    "docs/setup.ko.md",
    "docs/hosting.md",
    "docs/hosting.ko.md",
    "docs/operations.md",
    "docs/operations.ko.md",
    "docs/CURATOR.md",
    "docs/CURATOR.ko.md",
    "docs/metadata.md",
    "docs/metadata.ko.md",
    # Provider recipes with the image pinned to the release digest. Railway needs its whole (optional) tooling folder
    # to be usable: `npm ci --ignore-scripts && npm run typecheck` inside .railway.
    "render.yaml",
    ".railway/railway.ts",
    ".railway/package.json",
    ".railway/package-lock.json",
    ".railway/tsconfig.json",
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


def pin_compose(text, image, expected=1, name="compose.yml"):
    """Replace the PVG_IMAGE default with the digest reference; `expected` is the exact number of occurrences."""
    count = text.count(UNPINNED)
    if count != expected:
        raise ReleaseError(f"{name} must contain the PVG_IMAGE default exactly {expected} time(s), found {count}")
    pinned = text.replace(UNPINNED, "${PVG_IMAGE:-" + image + "}")
    unpinned = [l.strip() for l in pinned.splitlines() if l.strip().startswith("image:") and "@sha256:" not in l]
    if unpinned:
        raise ReleaseError(f"{name} still has an image without a digest: {unpinned[0]}")
    return pinned


def pin_provider_image(text, image, tag, name):
    """Replace the one `<image repository>:<release tag>` reference with the digest reference, exactly once."""
    tagged = f"{image.split('@', 1)[0]}:{tag}"
    count = text.count(tagged)
    if count != 1:
        raise ReleaseError(f"{name} must contain the image reference {tagged} exactly once, found {count}")
    return text.replace(tagged, image)


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


def standalone_paths(output_dir):
    """The pinned single-file Compose download and its checksum, next to the archive."""
    output_dir = Path(output_dir)
    return output_dir / STANDALONE_COMPOSE, output_dir / f"{STANDALONE_COMPOSE}.sha256"


def build_bundle(root, tag, image, output_dir):
    """Validate, read the allowlist, then write the archive and its .sha256 (returns both paths) plus the standalone
    pinned compose.yml and its .sha256 (see standalone_paths)."""
    version = validate_release(tag, root)
    validate_image(image)
    files = {rel: read_allowed(root, rel) for rel in ALLOWLIST}
    for rel, expected in COMPOSE_IMAGE_COUNTS.items():
        files[rel] = pin_compose(files[rel].decode("utf-8"), image, expected, rel).encode("utf-8")
    for rel in PROVIDER_IMAGE_FILES:
        files[rel] = pin_provider_image(files[rel].decode("utf-8"), image, tag, rel).encode("utf-8")
    base = f"persona-vault-gateway-{version}"
    data = make_archive(base, files)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"{base}-install.tar.gz"
    checksum = output_dir / f"{archive.name}.sha256"
    archive.write_bytes(data)
    checksum.write_text(f"{hashlib.sha256(data).hexdigest()}  {archive.name}\n", encoding="utf-8")
    standalone, standalone_checksum = standalone_paths(output_dir)
    standalone.write_bytes(files[STANDALONE_COMPOSE])
    standalone_checksum.write_text(
        f"{hashlib.sha256(files[STANDALONE_COMPOSE]).hexdigest()}  {standalone.name}\n", encoding="utf-8")
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
    for path in (archive, checksum, *standalone_paths(args.output_dir)):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
