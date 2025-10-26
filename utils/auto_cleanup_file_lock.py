import os
from filelock import FileLock, Timeout


class AutoCleanupFileLock(FileLock):
    """A FileLock that deletes its lock file once no process holds it."""

    def release(self, force: bool = False):
        """Release the lock and remove the file if no one else is using it."""
        super().release(force=force)

        # Try to re-acquire the lock instantly — if we succeed, no one else holds it.
        try:
            with FileLock(self.lock_file, timeout=0):
                # No other process holds this lock → safe to remove.
                os.remove(self.lock_file)
        except Timeout:
            # Another process still holds it → skip cleanup.
            pass
        except OSError:
            # Race condition or already deleted → ignore.
            pass
