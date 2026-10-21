"""Small, atomic worker heartbeat files suitable for a shared RAM directory."""
import math
import os
import tempfile
from pathlib import Path


def _timestamp(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('heartbeat timestamp must be finite positive seconds')
    try:
        value = float(value)
    except OverflowError:
        raise ValueError('heartbeat timestamp must be finite positive seconds') from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError('heartbeat timestamp must be finite positive seconds')
    return value


def write_heartbeat(path, timestamp):
    """Replace a heartbeat atomically; the destination directory must exist."""
    timestamp = _timestamp(timestamp)
    target = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='ascii', dir=target.parent,
                                         prefix=f'.{target.name}.', delete=False) as stream:
            temporary = stream.name
            stream.write(f'{timestamp!r}\n')
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def read_heartbeat(path):
    """Return a valid timestamp, or None for missing, unreadable or invalid data."""
    try:
        with Path(path).open('rb') as stream:
            data = stream.read(65)
        if len(data) > 64:
            return None
        return _timestamp(float(data.decode('ascii')))
    except (OSError, TypeError, ValueError):
        return None
