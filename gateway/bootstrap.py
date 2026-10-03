"""First-run setup that runs inside the Gateway image (no host Python, Git or ssh-keygen needed).

    docker compose run --rm persona-vault-init            # create .env, deploy key, pinned known_hosts
    docker compose run --rm persona-vault-init --check    # read-only `git ls-remote` with that key

The project directory is mounted at /setup. Nothing existing is overwritten or sourced, secrets are never
printed (only the public deploy key is). It configures no reverse proxy, TLS or public exposure.
"""
import argparse
import base64
import getpass
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
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
CHECK_TIMEOUT_SECONDS = 30

OWNER = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})"
GITHUB_URL = re.compile(rf"(?:git@github\.com:|ssh://git@github\.com/)(?P<owner>{OWNER})/(?P<repo>[A-Za-z0-9._-]{{1,100}})")
MANUAL_GUIDANCE = (
    "Automatic setup supports only GitHub SSH URLs such as git@github.com:OWNER/REPO.git. For another Git host, "
    "create secrets/persona_vault_sync and secrets/github_known_hosts yourself (verify the host key out of band) "
    "and put VAULT_REPO_SSH_URL in .env."
)


class BootstrapError(Exception):
    """A user-facing failure; the message never contains secret values."""


@dataclass
class Paths:
    root: Path

    def __post_init__(self):
        self.env = self.root / ".env"
        self.secrets = self.root / "secrets"
        self.key = self.secrets / "persona_vault_sync"
        self.pub = self.secrets / "persona_vault_sync.pub"
        self.known_hosts = self.secrets / "github_known_hosts"

    def targets(self):
        return [self.env, self.secrets, self.key, self.pub, self.known_hosts]


def validate_vault_url(text):
    text = (text or "").strip()
    match = GITHUB_URL.fullmatch(text)
    if match:
        repo = match["repo"][:-4] if match["repo"].endswith(".git") else match["repo"]
        if repo not in ("", ".", ".."):
            return text
    raise BootstrapError(f"Not an accepted Vault URL. {MANUAL_GUIDANCE}")


def validate_password(password):
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise BootstrapError(f"Password must be {PASSWORD_MIN}-{PASSWORD_MAX} characters.")
    if password != password.strip():
        raise BootstrapError("Password must not start or end with whitespace.")
    if any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise BootstrapError("Password must not contain control characters or line breaks.")
    if "'" in password:
        raise BootstrapError("Password must not contain a single quote (it is stored single-quoted in .env).")
    if len(set(password)) < 8:
        raise BootstrapError("Password is too repetitive; use at least 8 different characters.")
    return password


def env_quote(value):
    """Single-quote a .env value: Compose treats it literally (no $ interpolation, no escapes)."""
    if "'" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise BootstrapError("Value cannot be stored safely in .env.")
    return f"'{value}'"


def read_env_value(path, name):
    """Read one KEY from a dotenv file without sourcing or interpolating anything; last assignment wins."""
    value = None
    pattern = re.compile(rf"\s*(?:export\s+)?{re.escape(name)}\s*=\s*(.*)")
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.fullmatch(line)
        if not match:
            continue
        raw = match[1].strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
            value = raw[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", raw)
    return value


def github_known_hosts_line():
    blob = base64.b64decode(GITHUB_ED25519_KEY)
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    if fingerprint != GITHUB_ED25519_SHA256:
        raise BootstrapError("Bundled GitHub host key does not match the pinned fingerprint; refusing to use it.")
    return f"github.com ssh-ed25519 {GITHUB_ED25519_KEY}\n"


def preflight(paths):
    if not paths.root.is_dir():
        raise BootstrapError(f"{paths.root} is not a directory; run this through `docker compose run` (project mounted at /setup).")
    for target in paths.targets():
        if target.is_symlink():
            raise BootstrapError(f"{target.relative_to(paths.root)} is a symlink; refusing to follow it. Remove it and re-run.")
    if paths.secrets.exists() and not paths.secrets.is_dir():
        raise BootstrapError("secrets exists but is not a directory.")


def adopt(paths, path):
    """Hand a new file to the owner of the mounted project dir so the host user can read it (we run as root)."""
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
        raise BootstrapError("Private key appeared during setup; not overwriting it. Re-run.") from None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    adopt(paths, paths.key)


def derive_public_key(paths, run):
    fields = keygen(["-y", "-P", "", "-f", str(paths.key)], run).split()
    if len(fields) < 2 or not fields[0].startswith(("ssh-", "ecdsa-", "sk-")):
        raise BootstrapError("Could not derive a public key from the existing private key.")
    create_file(paths, paths.pub, f"{fields[0]} {fields[1]} {KEY_COMMENT}\n", 0o644)


def render_env(url, password):
    lines = [
        "# Written once by `persona-vault-init`; later runs never modify this file.",
        "# Single-quoted values are literal (no $ interpolation).",
        f"VAULT_REPO_SSH_URL={env_quote(url)}",
        f"ADMIN_PASSWORD={env_quote(password)}",
        "HOST_ID=persona-vault-gateway",
        "# Loopback only. Changing the bind address is your decision and your responsibility (see the docs).",
        "GATEWAY_BIND_ADDR=127.0.0.1",
        "GATEWAY_HOST_PORT=18080",
        "VAULT_SYNC_INTERVAL_SECONDS=300",
        "# Keyword-only search by default. For semantic search set EMBEDDING_PROVIDER=cloudflare, fill both",
        "# Cloudflare values and add COMPOSE_PROFILES=semantic (starts Qdrant).",
        "EMBEDDING_PROVIDER=none",
        "CLOUDFLARE_ACCOUNT_ID=",
        "CLOUDFLARE_API_TOKEN=",
    ]
    return "\n".join(lines) + "\n"


def ask(prompt_fn, prompt, validate, err, tries=3):
    for _ in range(tries):
        try:
            return validate(prompt_fn(prompt))
        except BootstrapError as exc:
            print(f"  {exc}", file=err)
    raise BootstrapError("Too many invalid attempts.")


def ask_password(getpass_fn, err):
    def validate(first):
        validate_password(first)
        if getpass_fn("Confirm admin password: ") != first:
            raise BootstrapError("Passwords did not match.")
        return first

    return ask(getpass_fn, "Admin password (16+ characters, not shown): ", validate, err)


def run_init(paths, args, input_fn, getpass_fn, run, out, err):
    preflight(paths)
    known_hosts = github_known_hosts_line()
    env_exists = paths.env.exists()
    url = password = None
    if not env_exists:  # prompt before touching anything
        url = validate_vault_url(args.vault_url) if args.vault_url else ask(
            input_fn, "Vault Git SSH URL (git@github.com:OWNER/REPO.git): ", validate_vault_url, err)
        password = ask_password(getpass_fn, err)

    notes = []
    if not paths.secrets.exists():
        paths.secrets.mkdir(mode=0o700)
        adopt(paths, paths.secrets)
    paths.secrets.chmod(0o700)
    if paths.key.exists():
        notes.append("kept existing secrets/persona_vault_sync")
    else:
        generate_private_key(paths, run)
        notes.append("created secrets/persona_vault_sync (mode 600)")
    if paths.pub.exists():
        notes.append("kept existing secrets/persona_vault_sync.pub")
    else:
        derive_public_key(paths, run)
        notes.append("created secrets/persona_vault_sync.pub")
    if paths.known_hosts.exists():
        notes.append("kept existing secrets/github_known_hosts")
        if GITHUB_ED25519_KEY not in paths.known_hosts.read_text(encoding="utf-8", errors="replace"):
            notes.append("WARNING: it does not contain the pinned GitHub ed25519 host key; --check will fail until fixed")
    else:
        create_file(paths, paths.known_hosts, known_hosts, 0o644)
        notes.append("created secrets/github_known_hosts (pinned GitHub ed25519 host key)")
    if env_exists:
        notes.append("kept existing .env (not read or modified)")
    else:
        create_file(paths, paths.env, render_env(url, password), 0o600)
        notes.append("created .env (mode 600)")

    print("\n".join(notes), file=out)
    print("\nDeploy key (public, safe to share). Add it to the Vault repository on GitHub under\n"
          "Settings > Deploy keys with 'Allow write access' enabled:\n", file=out)
    print(paths.pub.read_text(encoding="utf-8").strip(), file=out)
    print("\nThe repository needs at least one commit (for example an initialized README).\n\n"
          "Next steps:\n"
          "  1. docker compose run --rm persona-vault-init --check   # read-only reachability check\n"
          "  2. docker compose up -d\n"
          "  3. The Gateway listens on 127.0.0.1:18080 only. This setup provides no remote access or encryption;\n"
          "     how you reach it from elsewhere (an existing reverse proxy, or a private encrypted network) is\n"
          "     your choice. If you change GATEWAY_BIND_ADDR in .env, secure that path first.\n"
          "  4. Optional semantic search: see the comments in .env.", file=out)
    return 0


def run_check(paths, run, out, err, timeout=CHECK_TIMEOUT_SECONDS):
    preflight(paths)  # same symlink policy as init, including the secrets directory itself
    for target in (paths.env, paths.key, paths.known_hosts):
        if not target.is_file():
            raise BootstrapError(f"{target.relative_to(paths.root)} is missing; run setup without --check first.")
    url = read_env_value(paths.env, "VAULT_REPO_SSH_URL")
    if not url:
        raise BootstrapError("VAULT_REPO_SSH_URL is not set in .env.")
    url = validate_vault_url(url)
    ssh = ["ssh", "-F", "/dev/null", "-i", str(paths.key), "-o", "IdentitiesOnly=yes",
           "-o", f"UserKnownHostsFile={paths.known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
           "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": "/tmp", "LC_ALL": "C",
           "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           "GIT_SSH_COMMAND": shlex.join(ssh)}
    try:
        result = run(["git", "ls-remote", "--heads", url], env=env, stdin=subprocess.DEVNULL,
                     capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"CHECK FAILED: no answer from {url} within {timeout}s (network, firewall or DNS).", file=err)
        return 1
    except OSError:
        raise BootstrapError("git is not usable in this image.") from None
    if result.returncode:
        stderr = (result.stderr or "").strip()
        hint = ("the deploy key is not registered for this repository, or the repository name is wrong"
                if "Permission denied" in stderr or "not found" in stderr.lower() else
                "the SSH host key did not match the pinned GitHub key" if "Host key verification failed" in stderr
                else "see the message below")
        print(f"CHECK FAILED for {url}: {hint}.", file=err)
        print("\n".join(stderr.splitlines()[-5:])[-500:], file=err)
        return 1
    heads = [line for line in result.stdout.splitlines() if line.strip()]
    if not heads:
        print(f"CHECK FAILED: {url} is reachable but EMPTY (no commits). Initialize the repository first, for "
              "example by creating a README on GitHub, then re-run --check.", file=err)
        return 1
    print(f"CHECK OK: {url} is reachable with the deploy key and has {len(heads)} branch(es).\n"
          "This read-only check cannot prove the key has write access; sync pushes need 'Allow write access'.", file=out)
    return 0


def main(argv=None, *, input_fn=input, getpass_fn=getpass.getpass, run=subprocess.run, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="python -m gateway.bootstrap", description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="read-only git ls-remote with the generated key")
    parser.add_argument("--vault-url", help="Vault Git SSH URL (skips the prompt)")
    parser.add_argument("--setup-dir", default="/setup", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.check and args.vault_url:
        parser.error("--check cannot be combined with other options")
    paths = Paths(Path(args.setup_dir))
    try:
        if args.check:
            return run_check(paths, run, out, err)
        return run_init(paths, args, input_fn, getpass_fn, run, out, err)
    except (BootstrapError, OSError, EOFError) as exc:
        print(f"ERROR: {exc if isinstance(exc, BootstrapError) else getattr(exc, 'strerror', None) or type(exc).__name__}", file=err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
