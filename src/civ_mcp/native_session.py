"""Reserve the resident tuner service during explicitly authorized acceptance."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile


SESSION_PATH = Path(tempfile.gettempdir()) / "civ6-mcp-native-acceptance.lock"


def _open(path):
    stream = open(path, "a+b")
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    return stream


def _lock(stream):
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(stream):
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def reserve_native_session(path=None):
    with _open(path or SESSION_PATH) as stream:
        try:
            _lock(stream)
        except OSError as error:
            raise ConnectionError("Native acceptance already has a resident service") from error
        try:
            yield
        finally:
            _unlock(stream)


def native_session_active(path=None):
    try:
        with reserve_native_session(path):
            return False
    except ConnectionError:
        return True
