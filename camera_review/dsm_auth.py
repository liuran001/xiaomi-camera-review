"""Authenticate request-bound identities from the native DSM CGI gateway."""
import hashlib
import hmac
import re
import time
from pathlib import Path
from urllib.parse import unquote


def has_identity(headers):
    """Partial or unknown DSM headers must never select password authentication."""
    return any(name.lower().startswith('x-camera-dsm-') for name in headers.keys())


def authenticate(request, secret_file, prefix):
    """Return the signed DSM user, or None; no browser session is created."""
    encoded_user = request.headers.get('X-Camera-DSM-User', '')
    if not encoded_user.isascii() or re.search(r'%(?![0-9a-fA-F]{2})', encoded_user):
        return None
    try:
        # The HTTP header is ASCII; the signed identity is decoded UTF-8 exactly once.
        user = unquote(encoded_user, encoding='utf-8', errors='strict')
    except UnicodeError:
        return None
    timestamp = request.headers.get('X-Camera-DSM-Time', '')
    signature = request.headers.get('X-Camera-DSM-Signature', '')
    supplied_prefix = request.headers.get('X-Camera-DSM-Prefix', '')
    if (not secret_file or not prefix or not prefix.startswith('/') or prefix.endswith('/')
            or supplied_prefix != prefix or not user or len(user) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in user)
            or not re.fullmatch(r'[0-9]{1,12}', timestamp)
            or not re.fullmatch(r'[0-9a-f]{64}', signature)):
        return None
    if abs(time.time() - int(timestamp)) > 30:
        return None
    try:
        # Read on every request: removed/rotated credentials take effect immediately.
        secret = Path(secret_file).read_text(encoding='utf-8').strip()
        if not re.fullmatch(r'[0-9a-fA-F]{64}', secret):
            return None
        query = request.query_string.decode('ascii')
    except (OSError, UnicodeError):
        return None
    message = '\n'.join((timestamp, user, request.method, request.path, query, prefix, request.host))
    expected = hmac.new(secret.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()
    return user if hmac.compare_digest(signature, expected) else None
