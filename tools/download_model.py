#!/usr/bin/env python3
"""Fetch the pinned model; verify size and SHA-256 before atomic publication."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import urllib.request
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]


def verified(path, specification):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != specification['bytes']:
        return False
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest() == specification['sha256']


def download(manifest, directory, opener=urllib.request.urlopen):
    files = json.loads(Path(manifest).read_text(encoding='utf-8'))['files']
    if len(files) != 1:
        raise ValueError('Expected exactly one pinned model')
    specification = files[0]
    name = specification['file']
    if (not re.fullmatch(r'[A-Za-z0-9_-]+\.onnx', name)
            or type(specification['bytes']) is not int or specification['bytes'] <= 0
            or not re.fullmatch(r'[0-9a-f]{64}', specification['sha256'])
            or urlsplit(specification['source']).scheme != 'https'):
        raise ValueError('Invalid pinned model specification')
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('Model directory must not be a symbolic link')
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    if target.is_symlink():
        raise ValueError('Model destination must not be a symbolic link')
    if verified(target, specification):
        return target
    descriptor, temporary = tempfile.mkstemp(prefix='.model-', suffix='.part', dir=str(directory))
    temporary = Path(temporary)
    try:
        with os.fdopen(descriptor, 'wb') as output, opener(specification['source'], timeout=60) as response:
            if hasattr(response, 'geturl') and urlsplit(response.geturl()).scheme != 'https':
                raise ValueError('Model redirect must use HTTPS')
            received = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > specification['bytes']:
                    raise ValueError('Model download exceeds pinned size')
                output.write(chunk)
        if not verified(temporary, specification):
            raise ValueError('Model size or SHA-256 verification failed; previous file left unchanged')
        if target.is_symlink():
            raise ValueError('Model destination changed to a symbolic link')
        temporary.chmod(0o644)
        os.replace(str(temporary), str(target))
        return target
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT / 'models')
    args = parser.parse_args()
    path = download(ROOT / 'third_party/model-manifest.json', args.directory)
    print(str(path) + ' verified (size and SHA-256).')


if __name__ == '__main__':
    main()
