"""
Single-instance guard.

Prevents two copies of the bot from running at once — which doubles every alert
and doubles Google Sheets write volume (the cause of the 429 quota errors). Uses
an OS advisory lock on a file; the lock is released automatically when the
process exits or crashes, so a stale lock never blocks the next start.
"""
import logging
import os
import sys

logger = logging.getLogger(__name__)

# Module-level handle: keeping the file open is what holds the lock for the
# lifetime of the process.
_lock_handle = None


def acquire(path: str) -> bool:
    """
    Try to acquire the single-instance lock.

    Returns True if the lock was acquired (held until the process exits), or
    False if another instance already holds it.
    """
    global _lock_handle
    try:
        f = open(path, 'a+')
    except OSError as exc:
        # If we can't even open the lock file, don't block startup — log and run.
        logger.warning("Single-instance lock file %s unavailable (%s) — proceeding", path, exc)
        return True

    try:
        if sys.platform == 'win32':
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False

    # Record our PID (informational); keep the handle open to hold the lock.
    try:
        f.seek(0)
        f.write(str(os.getpid()))
        f.truncate()
        f.flush()
    except OSError:
        pass

    _lock_handle = f
    return True
