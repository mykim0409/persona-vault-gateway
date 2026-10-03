"""Managed browser-first onboarding: claim, deploy key, clone, and in-process Git sync.

Active when the Gateway is started with `python -m gateway.server` (PVG_ONBOARDING=managed), the product entrypoint.
Direct `uvicorn gateway.app:app` stays an internal/test/backward-compatible mode: there `vault_ready()` is always
true and the ADMIN_PASSWORD flow is untouched.

Persistent layout under one data root (PVG_DATA_DIR): gateway.db, vault/, and setup/ (private: claim record, setup
code, deploy key, pinned known_hosts, vault state). Setup state lives in files, never in the SQLite schema.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import bootstrap

logger = logging.getLogger(__name__)

MODE_ENV = "PVG_ONBOARDING"
MANAGED = "managed"
SETUP_TOKEN_ENV = "PVG_SETUP_TOKEN"
SETUP_TOKEN_MIN, SETUP_TOKEN_MAX = 20, 200
CODE_FILE, CLAIM_FILE, VAULT_FILE = "setup_code", "claim.json", "vault.json"
SCRYPT_PARAMS = (2**14, 8, 1)  # n, r, p: 16 MiB, within OpenSSL's default scrypt memory limit
SYNC_INTERVAL_DEFAULT = 300
SYNC_BACKOFF_MAX_SECONDS = 3600
CLONE_TIMEOUT_SECONDS = 600
GIT_TIMEOUT_SECONDS = 120
PULL_TIMEOUT_SECONDS = 300
SYNC_STOP_GRACE_SECONDS = 4  # a sync command may finish this long after stop() before it is asked to terminate
TERMINATE_WAIT_SECONDS = 5
# Overridden only by tests, which replace the SSH transport with a synthetic local bare repository.
ALLOWED_PROTOCOLS = "ssh"
SYNC_AUTHOR = ("PersonaVault Sync", "persona-vault-sync@localhost")
BLOCKING_MARKERS = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD")

# Fixed, user-facing texts keyed by error code. Git output is classified, never stored or rendered.
ERROR_TEXT = {
    "auth": "Git rejected the deploy key. Check that it is registered on the repository, with write access enabled.",
    "host-key": "The SSH host key did not match the pinned GitHub key.",
    "not-found": "The repository was not found. Check the URL and that the deploy key is registered on it.",
    "network": "The Git host could not be reached.",
    "timeout": "The Git operation timed out.",
    "empty": "The repository has no commits yet. Create an initial commit (for example a README), then retry.",
    "destination": "The Vault location is not empty or cannot be replaced safely, so it was left untouched.",
    "storage": "The Vault storage could not be written.",
    "push-rejected": "The push was rejected. The deploy key probably lacks write access, or the branch is protected.",
    "missing": "The Vault directory is missing or unusable.",
    "blocked": "Unresolved Git state in the Vault (a rebase or merge in progress, or unmerged files). Sync is paused "
               "until it is resolved manually; it resumes on its own afterwards.",
    "failed": "The Git operation failed.",
    "stopped": "The Gateway was shutting down.",
}


class SetupError(Exception):
    """A user-facing failure; the message is fixed text and never contains secret values."""

    status = 400


class InvalidSetupCode(SetupError):
    status = 403


class AlreadyClaimed(SetupError):
    status = 409


class GitFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def managed() -> bool:
    return os.getenv(MODE_ENV) == MANAGED


def data_dir() -> Path:
    return Path(os.environ.get("PVG_DATA_DIR") or "/data")


def setup_dir() -> Path:
    return data_dir() / "setup"


def vault_dir() -> Path:
    # Unresolved on purpose: a symlinked Vault path must be seen as a symlink, not followed.
    return Path(os.environ.get("VAULT_DIR") or data_dir() / "vault").absolute()


def apply_defaults(env=os.environ) -> None:
    """Fill the managed layout and the keyword-only default; explicit (legacy) variables always win."""
    root = os.path.abspath(env.get("PVG_DATA_DIR") or "/data")
    env["PVG_DATA_DIR"] = root
    env[MODE_ENV] = MANAGED
    if not env.get("VAULT_DIR"):
        env["VAULT_DIR"] = os.path.join(root, "vault")
    if not env.get("DB_PATH"):
        env["DB_PATH"] = os.path.join(root, "gateway.db")
    if "EMBEDDING_PROVIDER" not in env:
        env["EMBEDDING_PROVIDER"] = "none"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- private files -------------------------------------------------------------------------------------------

def ensure_private_dir(path: Path) -> None:
    if path.is_symlink():
        raise SetupError("A setup path is a symlink; refusing to follow it.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise SetupError("A setup path is not a directory.")
    os.chmod(path, 0o700)


def write_private(path: Path, text: str, *, exclusive: bool = False) -> None:
    """Write a 0600 file atomically (temp + rename, or temp + hard link when it must not already exist)."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            os.link(tmp, path)  # atomic and complete on appearance; fails if the target exists
        else:
            os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def read_private(path: Path) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, encoding="utf-8", errors="replace") as handle:
        return handle.read(65536)


# --- setup code, claim, password -----------------------------------------------------------------------------

def env_setup_token() -> str | None:
    value = (os.environ.get(SETUP_TOKEN_ENV) or "").strip()
    ok = SETUP_TOKEN_MIN <= len(value) <= SETUP_TOKEN_MAX and value.isascii() and value.isprintable() and " " not in value
    return value if ok else None


def claim_path() -> Path:
    return setup_dir() / CLAIM_FILE


def claimed() -> bool:
    return os.path.lexists(claim_path())


def current_setup_code() -> str | None:
    if claimed():
        return None
    return env_setup_token() or (read_private(setup_dir() / CODE_FILE) or "").strip() or None


def start_banner(out=None) -> None:
    """Tell the operator (stdout only) how to claim; a generated code is printed once, when it is created."""
    out = out or sys.stdout
    if claimed():
        return
    if env_setup_token():
        print(f"PersonaVault setup is waiting for the first administrator. Open /setup and enter the value of "
              f"{SETUP_TOKEN_ENV}.", file=out, flush=True)
        return
    if os.environ.get(SETUP_TOKEN_ENV):
        print(f"{SETUP_TOKEN_ENV} is ignored: it must be {SETUP_TOKEN_MIN}-{SETUP_TOKEN_MAX} printable ASCII "
              "characters without spaces.", file=out, flush=True)
    code = secrets.token_urlsafe(24)
    try:
        write_private(setup_dir() / CODE_FILE, code + "\n", exclusive=True)
    except FileExistsError:
        print(f"PersonaVault setup is unclaimed. The setup code was printed at the first start of this volume; set "
              f"{SETUP_TOKEN_ENV} to choose a new one.", file=out, flush=True)
        return
    print("=" * 64, f"PersonaVault first-run setup code (shown once): {code}",
          "Open /setup in a browser and enter it with a new admin password.", "=" * 64, sep="\n", file=out, flush=True)


def validate_new_password(password: str, confirm: str) -> None:
    if password != confirm:
        raise SetupError("The passwords do not match.")
    low, high = bootstrap.PASSWORD_MIN, bootstrap.PASSWORD_MAX
    if not low <= len(password) <= high:
        raise SetupError(f"Password must be {low}-{high} characters.")
    if password != password.strip() or any(ord(c) < 32 or ord(c) == 127 for c in password):
        raise SetupError("Password must not start or end with whitespace or contain control characters.")
    if len(set(password)) < 8:
        raise SetupError("Password is too repetitive; use at least 8 different characters.")


def hash_password(password: str) -> str:
    n, r, p = SCRYPT_PARAMS
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8", "replace"), salt=salt, n=n, r=r, p=p, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"


def read_claim() -> dict | None:
    try:
        data = json.loads(read_private(claim_path()) or "")
    except (OSError, ValueError):
        return None
    ok = isinstance(data, dict) and isinstance(data.get("password_hash"), str) \
        and isinstance(data.get("signing_secret"), str) and len(data["signing_secret"]) >= 32
    return data if ok else None


def signing_secret() -> str | None:
    record = read_claim()
    return record["signing_secret"] if record else None


def verify_password(candidate: str) -> bool:
    record = read_claim()
    if not record:
        return False
    try:
        scheme, n, r, p, salt, expected = record["password_hash"].split("$")
        digest = hashlib.scrypt(candidate.encode("utf-8", "replace"), salt=bytes.fromhex(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(expected)))
    except (ValueError, OverflowError):
        return False
    return scheme == "scrypt" and hmac.compare_digest(digest.hex(), expected)


def claim(code: str, password: str, confirm: str) -> str:
    """Claim the Gateway; the first valid claimant wins. Returns the signing secret for the admin session."""
    validate_new_password(password, confirm)  # independent of the code, so a bad password never confirms it
    if claimed():
        raise AlreadyClaimed("This Gateway has already been claimed.")
    expected = current_setup_code()
    if not expected or not hmac.compare_digest(code.strip().encode("utf-8", "replace"), expected.encode("utf-8")):
        raise InvalidSetupCode("The setup code is not valid.")
    record = {"version": 1, "password_hash": hash_password(password), "signing_secret": secrets.token_hex(32),
              "claimed_at": utc_now()}
    ensure_private_dir(setup_dir())
    try:
        write_private(claim_path(), json.dumps(record), exclusive=True)
    except FileExistsError:
        raise AlreadyClaimed("This Gateway has already been claimed.") from None
    with contextlib.suppress(FileNotFoundError):
        os.unlink(setup_dir() / CODE_FILE)
    return record["signing_secret"]


# --- vault state and readiness -------------------------------------------------------------------------------

_state_lock = threading.Lock()


def read_vault_state() -> dict:
    try:
        data = json.loads(read_private(setup_dir() / VAULT_FILE) or "")
    except (OSError, ValueError):
        data = None
    state = {"state": "unconfigured", "repo_url": None, "error": None}
    if isinstance(data, dict):
        state.update({key: data.get(key) for key in state if key in data})
    return state


def write_vault_state(**fields) -> dict:
    with _state_lock:
        state = {**read_vault_state(), **fields, "updated": utc_now()}
        write_private(setup_dir() / VAULT_FILE, json.dumps(state))
        return state


def vault_ready() -> bool:
    """True when vault reads, writes and indexing may run: always outside managed mode, else after a published clone."""
    if not managed():
        return True
    if not claimed() or read_vault_state()["state"] != "ready":
        return False
    vault = vault_dir()
    git = vault / ".git"
    return not vault.is_symlink() and git.is_dir() and not git.is_symlink()


def setup_phase() -> str:
    return "claim" if not claimed() else "vault"


def read_public_key() -> str | None:
    line = (read_private(bootstrap.Paths(setup_dir()).pub) or "").strip()
    return line if line.startswith("ssh-") and "\n" not in line else None


def registration_link(url: str | None) -> str | None:
    match = bootstrap.GITHUB_URL.fullmatch(url or "")
    if not match:
        return None
    repo = match["repo"][:-4] if match["repo"].endswith(".git") else match["repo"]
    return f"https://github.com/{match['owner']}/{repo}/settings/keys/new"


def ensure_deploy_key() -> None:
    """Generate the private deploy key and pinned known_hosts once, with the first-run initializer's helpers."""
    ensure_private_dir(setup_dir())
    paths = bootstrap.Paths(setup_dir())
    try:
        bootstrap.preflight(paths)
        known_hosts = bootstrap.github_known_hosts_line()
        paths.secrets.mkdir(mode=0o700, exist_ok=True)
        paths.secrets.chmod(0o700)
        if not paths.key.exists():
            bootstrap.generate_private_key(paths, subprocess.run)
        if not paths.pub.exists():
            bootstrap.derive_public_key(paths, subprocess.run)
        if not paths.known_hosts.exists():
            bootstrap.create_file(paths, paths.known_hosts, known_hosts, 0o644)
        elif paths.known_hosts.read_text(encoding="utf-8", errors="replace") != known_hosts:
            raise SetupError("The pinned host key file was modified; refusing to use it.")
    except bootstrap.BootstrapError as exc:
        raise SetupError(str(exc)) from None
    except OSError:
        raise SetupError("The deploy key could not be written.") from None


# --- git -----------------------------------------------------------------------------------------------------

def remote_url(url: str) -> str:
    """The URL Git actually uses. Identity in production; tests point it at a synthetic local bare repository."""
    return url


def git_environment() -> dict[str, str]:
    """A fresh environment per command: pinned SSH, no prompts, no global config; os.environ is never mutated."""
    env = bootstrap.git_env(bootstrap.Paths(setup_dir()))
    name, email = SYNC_AUTHOR
    env.update(GIT_ALLOW_PROTOCOL=ALLOWED_PROTOCOLS, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
               GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
    return env


# Worker threads of a Manager set `stop` (its stop event) and `grace` here, so a running Git command can end on shutdown.
_worker = threading.local()


def terminate(proc: subprocess.Popen) -> None:
    """End a child this process started, and only that child's own process group (ssh included)."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            proc.communicate(timeout=TERMINATE_WAIT_SECONDS)
            return
        except subprocess.TimeoutExpired:
            continue


def run_git(args: list[str], *, timeout: int) -> tuple[int, str, str]:
    """Run git without a shell, in its own session. It ends on timeout or, after a grace period, on shutdown."""
    stop, grace = getattr(_worker, "stop", None), getattr(_worker, "grace", 0)
    proc = subprocess.Popen(["git", *args], env=git_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, errors="replace", start_new_session=True)
    deadline, stop_deadline = time.monotonic() + timeout, None
    while True:
        try:
            out, err = proc.communicate(timeout=0.2)
            return proc.returncode, out, err
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            if now >= deadline:
                terminate(proc)
                raise GitFailure("timeout") from None
            if stop is not None and stop.is_set():
                stop_deadline = stop_deadline or now + grace
                if now >= stop_deadline:
                    terminate(proc)  # a half-finished rebase is left as is: sync reports BLOCKED for manual recovery
                    raise GitFailure("stopped") from None


def git_in(repo: Path, *args: str, timeout: int = GIT_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    return run_git(["-c", f"safe.directory={repo}", "-C", str(repo), *args], timeout=timeout)


def classify_git_error(stderr: str) -> str:
    text = stderr.lower()
    for needles, code in (
        (("host key verification failed",), "host-key"),
        (("permission denied", "publickey"), "auth"),
        (("read only", "read-only", "remote rejected", "protected branch", "non-fast-forward"), "push-rejected"),
        (("not found", "does not exist", "does not appear to be a git repository"), "not-found"),
        (("could not resolve", "connection", "timed out", "network"), "network"),
    ):
        if any(needle in text for needle in needles):
            return code
    return "failed"


def blocked(repo: Path) -> bool:
    """The sidecar's BLOCKED guard: an unfinished rebase/merge/cherry-pick/revert, or unmerged paths."""
    for marker in BLOCKING_MARKERS:
        code, out, _ = git_in(repo, "rev-parse", "--git-path", marker)
        if code:
            raise GitFailure("failed")
        path = Path(out.strip())
        if os.path.lexists(path if path.is_absolute() else repo / path):
            return True
    code, out, _ = git_in(repo, "ls-files", "--unmerged")
    if code:
        raise GitFailure("failed")
    return bool(out.strip())


def sync_once(repo: Path) -> str:
    """One sync cycle in the sidecar's order: add/commit, pull --rebase --autostash, push. Returns 'ok' or 'blocked'.

    Never forces, resets or resolves anything; a conflict stops the cycle and leaves the Vault as Git left it.
    """
    if blocked(repo):
        return "blocked"
    code, _, err = git_in(repo, "add", ".")
    if code:
        raise GitFailure(classify_git_error(err))
    code, _, _ = git_in(repo, "diff", "--cached", "--quiet")
    if code == 1:
        code, _, err = git_in(repo, "commit", "-m", f"Sync vault {utc_now()}")
        if code:
            raise GitFailure(classify_git_error(err))
    elif code:
        raise GitFailure("failed")
    code, _, err = git_in(repo, "pull", "--rebase", "--autostash", timeout=PULL_TIMEOUT_SECONDS)
    if blocked(repo):
        return "blocked"
    if code:
        raise GitFailure(classify_git_error(err))
    code, _, err = git_in(repo, "push", timeout=PULL_TIMEOUT_SECONDS)
    if code:
        reason = classify_git_error(err)
        raise GitFailure("push-rejected" if reason == "failed" else reason)
    return "ok"


def sync_interval() -> int:
    try:
        return min(86400, max(1, int(os.environ.get("VAULT_SYNC_INTERVAL_SECONDS", SYNC_INTERVAL_DEFAULT))))
    except ValueError:
        return SYNC_INTERVAL_DEFAULT


def sync_delay(interval: int, failures: int) -> int:
    """Seconds until the next attempt: the interval, doubling per consecutive failure up to a fixed cap."""
    if failures <= 0:
        return interval
    return min(interval * 2 ** min(failures, 12), max(interval, SYNC_BACKOFF_MAX_SECONDS))


def adoptable(vault: Path, remote: str) -> bool:
    """An existing Vault that already is a clone of this repository is kept as is (never overwritten)."""
    git = vault / ".git"
    if git.is_symlink() or not git.is_dir():
        return False
    try:
        inside = git_in(vault, "rev-parse", "--is-inside-work-tree")
        top = git_in(vault, "rev-parse", "--show-cdup")
        origin = git_in(vault, "config", "--get", "remote.origin.url")
        head = git_in(vault, "rev-parse", "--verify", "--quiet", "HEAD")
    except GitFailure:
        return False
    return (inside[0] == 0 and inside[1].strip() == "true" and top[0] == 0 and not top[1].strip()
            and origin[0] == 0 and origin[1].strip() == remote and head[0] == 0)


# --- manager -------------------------------------------------------------------------------------------------

class Manager:
    """Owns the background clone job and the periodic sync thread of this process."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.clone_thread: threading.Thread | None = None
        self.sync_thread: threading.Thread | None = None
        self.sync = {"state": "idle", "error": None, "last_attempt": None, "last_ok": None, "failures": 0,
                     "next_delay": None}

    def clone_running(self) -> bool:
        return bool(self.clone_thread and self.clone_thread.is_alive())

    def status(self) -> dict:
        state = read_vault_state()
        with self.lock:
            sync = dict(self.sync)
        sync["running"] = bool(self.sync_thread and self.sync_thread.is_alive())
        return {**state, "public_key": read_public_key() if state["repo_url"] else None,
                "registration_link": registration_link(state["repo_url"]), "sync": sync,
                "cloning": self.clone_running()}

    def configure(self, url: str) -> None:
        try:
            url = bootstrap.validate_vault_url(url)
        except bootstrap.BootstrapError:
            raise SetupError("Enter a GitHub SSH URL such as git@github.com:OWNER/REPO.git.") from None
        with self.lock:
            if read_vault_state()["state"] in ("cloning", "ready") or self.clone_running():
                raise SetupError("The Vault is already connected or being connected.")
            ensure_deploy_key()
            write_vault_state(state="key-ready", repo_url=url, error=None)

    def connect(self) -> None:
        with self.lock:
            if self.stop_event.is_set():
                raise SetupError("The Gateway is shutting down.")
            state = read_vault_state()
            if state["state"] == "ready" or not state["repo_url"]:
                raise SetupError("Enter the repository URL first.")
            if self.clone_running():
                return
            write_vault_state(state="cloning", error=None)
            self.clone_thread = threading.Thread(target=self.clone_job, daemon=True, name="pvg-vault-clone")
            self.clone_thread.start()

    def clone_job(self) -> None:
        _worker.stop, _worker.grace = self.stop_event, 0
        code = None
        try:
            self.clone()
        except GitFailure as exc:
            code = exc.code
        except OSError:
            code = "storage"
        except Exception as exc:  # the job must always end in a visible state
            logger.error("Vault clone crashed (%s)", type(exc).__name__)
            code = "failed"
        if code == "stopped" or (not code and self.stop_event.is_set()):
            return  # shutting down: state stays "cloning" and the next start resumes it
        if code:
            logger.warning("Vault clone failed (%s)", code)
            write_vault_state(state="failed", error=code)
            return
        write_vault_state(state="ready", error=None)
        self.start_sync()

    def clone(self) -> None:
        """Clone into a new private sibling directory and publish it by one rename; only a complete clone is published."""
        remote = remote_url(read_vault_state()["repo_url"])
        vault = vault_dir()
        if vault.is_symlink() or (os.path.lexists(vault) and not vault.is_dir()):
            raise GitFailure("destination")
        if vault.is_dir() and any(vault.iterdir()):
            if adoptable(vault, remote):
                return
            raise GitFailure("destination")
        vault.parent.mkdir(parents=True, exist_ok=True)
        # Created by this attempt (random name, mode 0700), so cleaning it up never touches anything pre-existing.
        staging = Path(tempfile.mkdtemp(prefix=f".{vault.name}.pvg-", dir=vault.parent))
        try:
            code, _, err = run_git(["-C", str(vault.parent), "clone", "--quiet", "--", remote, str(staging)],
                                   timeout=CLONE_TIMEOUT_SECONDS)
            if code:
                raise GitFailure(classify_git_error(err))
            if staging.is_symlink() or (staging / ".git").is_symlink() or not (staging / ".git").is_dir():
                raise GitFailure("failed")
            if git_in(staging, "rev-parse", "--verify", "--quiet", "HEAD")[0]:
                raise GitFailure("empty")
            if self.stop_event.is_set():
                raise GitFailure("stopped")
            try:
                os.rename(staging, vault)  # fails if the target is not an empty directory
            except OSError:
                raise GitFailure("destination") from None
        finally:
            if staging.is_dir() and not staging.is_symlink():
                shutil.rmtree(staging, ignore_errors=True)

    def start_sync(self) -> None:
        with self.lock:
            if self.stop_event.is_set() or (self.sync_thread and self.sync_thread.is_alive()):
                return
            self.sync_thread = threading.Thread(target=self.sync_loop, daemon=True, name="pvg-vault-sync")
            self.sync_thread.start()

    def sync_now(self) -> None:
        self.wake.set()

    def sync_loop(self) -> None:
        _worker.stop, _worker.grace = self.stop_event, SYNC_STOP_GRACE_SECONDS
        while not self.stop_event.is_set():
            with self.lock:
                self.sync.update(state="running", last_attempt=utc_now())
            outcome, error = "ok", None
            try:
                if not vault_ready():
                    raise GitFailure("missing")
                outcome = sync_once(vault_dir())
            except GitFailure as exc:
                if exc.code == "stopped":
                    break
                outcome, error = "error", exc.code
            except Exception as exc:
                logger.error("Vault sync crashed (%s)", type(exc).__name__)
                outcome, error = "error", "failed"
            if outcome != "ok":
                logger.warning("Vault sync %s (%s)", outcome, error or "blocked")
            with self.lock:
                failures = self.sync["failures"] + 1 if outcome == "error" else 0
                delay = sync_delay(sync_interval(), failures)
                self.sync.update(state=outcome, error=error or ("blocked" if outcome == "blocked" else None),
                                 failures=failures, next_delay=delay,
                                 last_ok=utc_now() if outcome == "ok" else self.sync["last_ok"])
            self.wake.wait(delay)
            self.wake.clear()

    def resume(self) -> None:
        state = read_vault_state()["state"]
        if state == "ready":
            self.start_sync()
        elif state == "cloning":
            self.connect()  # the previous process stopped mid-clone; run it again

    def stop(self) -> bool:
        """Stop publishing and syncing, wait for this manager's own threads and Git children; True when all ended.

        A command still running after its grace period gets SIGTERM (then SIGKILL) for its own process group only.
        Nothing is reset or cleaned up: an interrupted rebase is left for manual recovery.
        """
        self.stop_event.set()
        self.wake.set()
        for thread in (self.sync_thread, self.clone_thread):
            if thread and thread.is_alive():
                thread.join(timeout=SYNC_STOP_GRACE_SECONDS + 3 * TERMINATE_WAIT_SECONDS)
        return not any(t and t.is_alive() for t in (self.sync_thread, self.clone_thread))


_manager: Manager | None = None
_manager_lock = threading.Lock()


def manager() -> Manager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = Manager()
        return _manager


def start() -> None:
    """Application startup in managed mode: private dirs, operator banner, resume the configured Vault."""
    apply_defaults()
    ensure_private_dir(setup_dir())
    start_banner()
    manager().resume()


def stop() -> None:
    global _manager
    with _manager_lock:
        current = _manager
    if current and current.stop():
        with _manager_lock:
            if _manager is current:
                _manager = None  # a manager whose threads are still alive is kept, so two never run side by side
