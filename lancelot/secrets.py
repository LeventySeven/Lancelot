"""M7 T3.2 — lazy secrets provider with SIGHUP-rotate semantics.

Infisical pattern (TRANSCRIPTS_YC_ROOT_ACCESS.md:14963-15111):
"injected at startup (not hardcoded); rotated without service downtime;
auditable".

What ships:
  - Pluggable backends: env (default), file, Vault, Infisical, AWS-SM.
    Each lazy-loaded — `import lancelot` does NOT import any of them.
  - `lancelot.secrets.get('NAME')` is the user-facing API.
  - `lancelot.secrets.rotate()` re-reads (SIGHUP handler can call this).
  - Audit hook: callable invoked on every access (defaults to no-op).
  - Cardinal rule: NO new runtime deps. Vault/Infisical/AWS-SM
    backends try-import their SDK on first use; AbsentBackend if
    not installed.

Usage:
    # 1) env backend (default — works out of the box)
    api_key = lancelot.secrets.get("OPENAI_API_KEY")

    # 2) explicit backend
    lancelot.secrets.configure(VaultBackend(url=..., token=...))
    api_key = lancelot.secrets.get("openai/api_key")

    # 3) SIGHUP rotation (operator runs `kill -HUP <pid>`)
    lancelot.secrets.install_sighup_handler()

The audit hook is the operator's place to wire "this PID just read
GPT4_API_KEY" → SIEM / Splunk / etc.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
from typing import Callable, Optional, Protocol


_log = logging.getLogger("lancelot.secrets")


# ----- Backend protocol --------------------------------------------------

class SecretsBackend(Protocol):
    """A secrets backend implements `get(name) -> str | None` and an
    optional `refresh()` to invalidate caches on SIGHUP rotation."""

    def get(self, name: str) -> str | None: ...

    def refresh(self) -> None: ...


# ----- env backend (default) ----------------------------------------------

class EnvBackend:
    """Reads from os.environ. Default backend — works on every machine
    out of the box. No SDK, no SIGHUP needed (env vars don't change
    in a running process unless re-set externally, which env can't see
    anyway). `refresh()` is a no-op."""

    def get(self, name: str) -> str | None:
        return os.environ.get(name)

    def refresh(self) -> None:
        pass


# ----- file backend (one-secret-per-file pattern) -------------------------

class FileBackend:
    """Reads from a directory of one-file-per-secret. Common pattern
    when secrets are mounted via k8s `secret` volumes or Docker
    `--secret-file`. SIGHUP refresh re-reads from disk (the mount path
    may have been updated externally)."""

    def __init__(self, base_dir: str, *, encoding: str = "utf-8"):
        # realpath the base so a symlinked base can't defeat the containment
        # check below (same guard pattern as staticfiles.StaticFiles).
        self.base = os.path.realpath(base_dir)
        self.encoding = encoding
        self._cache: dict[str, str] = {}
        self._cached_keys_loaded = False

    def get(self, name: str) -> str | None:
        # Cache per-name; refresh() invalidates.
        if name in self._cache:
            return self._cache[name]
        # SEC: keep the resolved path inside base. `name` may be attacker-
        # controlled (e.g. wired from a request); os.path.join discards base on
        # an absolute name, and ".." escapes — realpath+commonpath blocks both.
        try:
            p = os.path.realpath(os.path.join(self.base, name))
            if os.path.commonpath([p, self.base]) != self.base:
                _log.warning("FileBackend: path-traversal attempt for %r", name)
                return None
        except (ValueError, OSError):
            return None
        try:
            with open(p, "r", encoding=self.encoding) as f:
                v = f.read().rstrip("\n")
            self._cache[name] = v
            return v
        except FileNotFoundError:
            return None
        except OSError as e:
            _log.warning("FileBackend get(%s) failed: %s", name, e)
            return None

    def refresh(self) -> None:
        self._cache.clear()


# ----- Vault backend (lazy SDK) -------------------------------------------

class VaultBackend:
    """HashiCorp Vault KV v2 backend. Lazy-imports `hvac`; raises
    RuntimeError if hvac isn't installed.

    Args:
        url: Vault URL (e.g. https://vault.internal:8200).
        token: Vault auth token. (For prod, prefer K8s/AWS auth methods
            via the hvac client directly and pass `client=...` to us.)
        mount_point: KV v2 mount (default 'secret').
        client: optional pre-constructed hvac client (lets the user
            handle exotic auth methods).
    """

    def __init__(self, url: str | None = None, token: str | None = None,
                 *, mount_point: str = "secret", client=None):
        try:
            import hvac  # noqa: F401
        except ImportError as e:
            raise RuntimeError("VaultBackend requires `hvac` to be "
                               "installed: pip install hvac") from e
        if client is None:
            import hvac
            client = hvac.Client(url=url, token=token)
        self._client = client
        self._mount = mount_point
        self._cache: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        if name in self._cache:
            return self._cache[name]
        try:
            resp = self._client.secrets.kv.v2.read_secret_version(
                path=name, mount_point=self._mount)
            # KV v2 wraps under data.data
            value = resp["data"]["data"].get("value")
            if value is not None:
                self._cache[name] = str(value)
            return value
        except Exception as e:  # noqa: BLE001
            _log.warning("VaultBackend get(%s) failed: %s", name, e)
            return None

    def refresh(self) -> None:
        self._cache.clear()


# ----- Infisical backend (lazy SDK) ---------------------------------------

class InfisicalBackend:
    """Infisical SDK backend. Lazy-imports `infisical_sdk`; raises if
    not installed.

    Args:
        client: pre-constructed Infisical SDK client (auth varies by
            install — service tokens, machine identities, etc.).
        project_id: Infisical project id.
        environment: environment slug ("prod", "dev", ...).
    """

    def __init__(self, *, client=None, project_id: str | None = None,
                 environment: str = "prod"):
        try:
            import infisical_sdk  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "InfisicalBackend requires `infisical_sdk`: "
                "pip install infisical-python") from e
        if client is None:
            raise ValueError(
                "InfisicalBackend requires an explicit `client=` "
                "(Infisical auth methods vary; construct outside).")
        self._client = client
        self._project = project_id
        self._env = environment
        self._cache: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        if name in self._cache:
            return self._cache[name]
        try:
            # Infisical SDK shape (project-id, env, secret-name).
            secret = self._client.secrets.get(
                project_id=self._project, environment=self._env,
                secret_name=name)
            value = getattr(secret, "secret_value", None) \
                or (secret.get("secretValue") if isinstance(secret, dict) else None)
            if value is not None:
                self._cache[name] = str(value)
            return value
        except Exception as e:  # noqa: BLE001
            _log.warning("InfisicalBackend get(%s) failed: %s", name, e)
            return None

    def refresh(self) -> None:
        self._cache.clear()


# ----- AWS Secrets Manager backend (lazy SDK) ----------------------------

class AwsSecretsManagerBackend:
    """AWS Secrets Manager backend. Lazy-imports boto3."""

    def __init__(self, *, client=None, region_name: str | None = None):
        try:
            import boto3  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "AwsSecretsManagerBackend requires `boto3`: "
                "pip install boto3") from e
        if client is None:
            import boto3
            client = boto3.client("secretsmanager",
                                  region_name=region_name)
        self._client = client
        self._cache: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        if name in self._cache:
            return self._cache[name]
        try:
            r = self._client.get_secret_value(SecretId=name)
            value = r.get("SecretString")
            if value is not None:
                self._cache[name] = str(value)
            return value
        except Exception as e:  # noqa: BLE001
            _log.warning("AwsSecretsManagerBackend get(%s) failed: %s", name, e)
            return None

    def refresh(self) -> None:
        self._cache.clear()


# ----- Global provider state ---------------------------------------------

_LOCK = threading.Lock()
_BACKEND: SecretsBackend = EnvBackend()
_AUDIT: Optional[Callable[[str, bool], None]] = None
_SIGHUP_INSTALLED = False


def configure(backend: SecretsBackend) -> None:
    """Replace the active backend. Call once at startup, before any
    `get()` calls (existing callers won't be re-routed to the new
    backend's cache, which is the point — secrets are sticky)."""
    global _BACKEND
    with _LOCK:
        _BACKEND = backend


def set_audit_hook(fn: Callable[[str, bool], None] | None) -> None:
    """Install an audit hook: called on every `get()` as
    `fn(name, found)`. `None` disables. Operator wires this into
    SIEM/Splunk. Synchronous — keep it FAST (a slow audit hook slows
    every secret access)."""
    global _AUDIT
    _AUDIT = fn


def get(name: str) -> str | None:
    """Read a secret. Returns None if not found. Calls the audit hook
    (if set) after the lookup with `(name, found_bool)` — the operator
    wires this into their SIEM."""
    backend = _BACKEND  # local capture so configure() during get() is safe
    value = backend.get(name)
    audit = _AUDIT
    if audit is not None:
        try:
            audit(name, value is not None)
        except Exception:  # noqa: BLE001
            # Audit MUST NOT break the caller — silently swallow.
            _log.warning("audit hook raised; suppressed", exc_info=True)
    return value


def rotate() -> None:
    """Invalidate the active backend's caches. SIGHUP handler should
    call this. Subsequent `get()` calls re-fetch from the backing
    store. No-op for env backend (env is read fresh each time)."""
    backend = _BACKEND
    try:
        backend.refresh()
    except Exception as e:  # noqa: BLE001
        _log.warning("rotate failed: %s", e)


def install_sighup_handler() -> bool:
    """Install a SIGHUP handler that calls `rotate()`. Only works
    on Unix (signal.SIGHUP doesn't exist on Windows). Returns True
    if installed, False otherwise.

    NOTE: Python's signal-handler runs in the main thread, so the
    rotate() call competes with the main loop. The work is small
    (a dict.clear()) so this is fine; the actual re-fetch happens
    lazily on the next `get()`."""
    global _SIGHUP_INSTALLED
    if not hasattr(signal, "SIGHUP"):
        return False
    if _SIGHUP_INSTALLED:
        return True

    def _handler(signum, frame):  # noqa: ARG001
        _log.info("SIGHUP — rotating secrets")
        rotate()

    try:
        signal.signal(signal.SIGHUP, _handler)
        _SIGHUP_INSTALLED = True
        return True
    except (OSError, ValueError) as e:
        # Non-main thread can't install signal handlers.
        _log.warning("could not install SIGHUP handler: %s", e)
        return False


def reset_for_tests() -> None:
    """Test-only: restore the default env backend + clear hooks."""
    global _BACKEND, _AUDIT, _SIGHUP_INSTALLED
    _BACKEND = EnvBackend()
    _AUDIT = None
    _SIGHUP_INSTALLED = False


__all__ = [
    "SecretsBackend", "EnvBackend", "FileBackend",
    "VaultBackend", "InfisicalBackend", "AwsSecretsManagerBackend",
    "configure", "set_audit_hook", "get", "rotate",
    "install_sighup_handler", "reset_for_tests",
]
