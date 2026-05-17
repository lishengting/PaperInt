"""Shared log utilities — timestamp all stderr output automatically."""
import sys
from datetime import datetime

_orig_stderr = sys.stderr


class _TSStderr:
    """Wrap stderr so every line gets a [HH:MM:SS] timestamp prefix."""
    def __init__(self, real):
        self._real = real
        self._pending = True

    def write(self, s):
        if not s:
            return
        if self._pending:
            self._real.write(f'[{datetime.now().strftime("%H:%M:%S")}] ')
            self._pending = False
        self._real.write(s)
        if s.endswith('\n'):
            self._pending = True

    def flush(self):
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


def install():
    if not isinstance(sys.stderr, _TSStderr):
        sys.stderr = _TSStderr(_orig_stderr)