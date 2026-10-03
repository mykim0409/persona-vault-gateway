"""SSH and URL helpers for browser onboarding (see gateway.onboarding).

Provides the GitHub SSH URL check, the deploy key generated with `ssh-keygen`, the pinned GitHub host key and the
Git-over-SSH environment. Nothing here prompts, reads dotenv files or prints; secrets are never part of an error.
"""
import base64
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Official GitHub SSH host key for github.com (ED25519), from
# https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
# The key is bundled and re-verified against the pinned fingerprint on every use; there is no keyscan.
GITHUB_ED25519_SHA256 = "SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU"
GITHUB_ED25519_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"

KEY_COMMENT = "persona-vault-sync"
PASSWORD_MIN, PASSWORD_MAX = 16, 128

OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
GITHUB_URL = re.compile(rf"(?:git@github\.com:|ssh://git@github\.com/)(?P<owner>{OWNER})/(?P<repo>[A-Za-z0-9._-]{{1,100}})")


class BootstrapError(Exception):
    """A user-facing failure; the message never contains secret values."""


@dataclass
class Paths:
    """Key material inside a private directory (the onboarding setup directory)."""

    root: Path

    def __post_init__(self):
        self.secrets = self.root / "secrets"
        self.key = self.secrets / "persona_vault_sync"
        self.pub = self.secrets / "persona_vault_sync.pub"
        self.known_hosts = self.secrets / "github_known_hosts"

    def targets(self):
        return [self.secrets, self.key, self.pub, self.known_hosts]


def validate_vault_url(text):
    text = (text or "").strip()
    match = GITHUB_URL.fullmatch(text)
    if match:
        repo = match["repo"][:-4] if match["repo"].endswith(".git") else match["repo"]
        if repo not in ("", ".", ".."):
            return text
    raise BootstrapError("Not an accepted Vault URL; only GitHub SSH URLs such as git@github.com:OWNER/REPO.git are supported.")


def github_known_hosts_line():
    blob = base64.b64decode(GITHUB_ED25519_KEY)
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    if fingerprint != GITHUB_ED25519_SHA256:
        raise BootstrapError("Bundled GitHub host key does not match the pinned fingerprint; refusing to use it.")
    return f"github.com ssh-ed25519 {GITHUB_ED25519_KEY}\n"


def preflight(paths):
    if not paths.root.is_dir():
        raise BootstrapError(f"{paths.root} is not a directory.")
    for target in paths.targets():
        if target.is_symlink():
            raise BootstrapError(f"{target.relative_to(paths.root)} is a symlink; refusing to follow it.")
    if paths.secrets.exists() and not paths.secrets.is_dir():
        raise BootstrapError("secrets exists but is not a directory.")


def adopt(paths, path):
    """Hand a new file to the owner of the setup dir so that dir's user can read it (we may run as root)."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        st = paths.root.stat()
        if st.st_uid != 0:
            os.chown(path, st.st_uid, st.st_gid, follow_symlinks=False)


def create_file(paths, path, content, mode):
    """Create a new file; never truncates or follows an existing path."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(content)
    adopt(paths, path)


def keygen(args, run):
    try:
        result = run(["ssh-keygen", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        raise BootstrapError("ssh-keygen is not usable in this image.") from None
    if result.returncode:
        raise BootstrapError("ssh-keygen failed (is the existing private key passphrase-protected or damaged?).")
    return result.stdout


def generate_private_key(paths, run):
    # Generate in a private temp dir, then hard-link into place: the link fails if the target appeared meanwhile.
    scratch = Path(tempfile.mkdtemp(dir=paths.secrets))
    try:
        keygen(["-q", "-t", "ed25519", "-N", "", "-C", KEY_COMMENT, "-f", str(scratch / "key")], run)
        os.link(scratch / "key", paths.key)
    except FileExistsError:
        raise BootstrapError("Private key appeared during setup; not overwriting it.") from None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    adopt(paths, paths.key)


def derive_public_key(paths, run):
    fields = keygen(["-y", "-P", "", "-f", str(paths.key)], run).split()
    if len(fields) < 2 or not fields[0].startswith(("ssh-", "ecdsa-", "sk-")):
        raise BootstrapError("Could not derive a public key from the existing private key.")
    create_file(paths, paths.pub, f"{fields[0]} {fields[1]} {KEY_COMMENT}\n", 0o644)


def git_env(paths):
    """Environment for Git over SSH: the generated key, the pinned known_hosts, no prompts, no global config."""
    ssh = ["ssh", "-F", "/dev/null", "-i", str(paths.key), "-o", "IdentitiesOnly=yes",
           "-o", f"UserKnownHostsFile={paths.known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
           "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    return {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": "/tmp", "LC_ALL": "C",
            "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_SSH_COMMAND": shlex.join(ssh)}
