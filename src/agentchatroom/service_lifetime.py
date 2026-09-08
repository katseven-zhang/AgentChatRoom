"""OS-owned local service lifetime marker; stale files do not prove liveness."""
from contextlib import contextmanager
import errno
import os
import uuid

from .errors import DomainError


def _lock(handle, *, release=False):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def running_service(settings):
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(settings.data_dir / "service.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b"1")
            handle.flush()
        _lock(handle)
        try:
            # The generation changes on every explicit service start.
            handle.seek(1)
            handle.write(uuid.uuid4().hex.encode("ascii"))
            handle.truncate()
            handle.flush()
            yield
        finally:
            _lock(handle, release=True)


def require_running_service(settings):
    try:
        with (settings.data_dir / "service.lock").open("r+b") as handle:
            try:
                _lock(handle)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(error, "winerror", None) == 33:
                    handle.seek(1)
                    generation = handle.read(32).decode("ascii")
                    if len(generation) == 32 and all(c in "0123456789abcdef" for c in generation):
                        return generation
                    raise ValueError("Invalid service generation")
                raise
            else:
                _lock(handle, release=True)
    except (OSError, ValueError):
        pass
    raise DomainError(
        "service_unavailable", "Start the configured AgentChatRoom service explicitly before connecting MCP",
        status_code=503, details={"required_action": "start_configured_service"},
    )
