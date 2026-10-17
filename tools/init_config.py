#!/usr/bin/env python3
"""Create local configuration once; never print or replace existing secrets."""
import argparse
import os
from pathlib import Path
import secrets


ROOT = Path(__file__).resolve().parents[1]


def initialize(directory):
    directory = Path(directory)
    destination = directory / '.env'
    if destination.exists() or destination.is_symlink():
        raise FileExistsError('Existing .env left unchanged; edit it directly')
    content = (directory / '.env.example').read_text(encoding='utf-8')
    content = content.replace('APP_PASSWORD=CHANGE_ME', 'APP_PASSWORD=' + secrets.token_urlsafe(24))
    content = content.replace('SESSION_SECRET=CHANGE_ME', 'SESSION_SECRET=' + secrets.token_hex(32))
    if hasattr(os, 'getuid') and os.getuid() != 0:
        content = content.replace('APP_UID=1000', 'APP_UID=' + str(os.getuid()))
        content = content.replace('APP_GID=1000', 'APP_GID=' + str(os.getgid()))
    descriptor = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as output:
        output.write(content)
    values = dict(line.split('=', 1) for line in content.splitlines()
                  if line and not line.startswith('#'))
    for name in ('data', 'models'):
        folder = directory / name
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            if not folder.is_dir() or folder.is_symlink():
                raise ValueError('Runtime path must be a real directory: ' + name)
        else:
            # Root is common on NAS hosts. A new0700 root-owned bind directory
            # cannot be used by the non-root container specified in the template.
            # Never recursively change existing library/data ownership.
            if hasattr(os, 'getuid') and os.getuid() == 0:
                os.chown(folder, int(values['APP_UID']), int(values['APP_GID']))
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        path = initialize(args.directory)
    except FileExistsError as error:
        parser.exit(1, str(error) + '\n')
    print(str(path) + ' created. Edit the recording path, UID/GID and password before starting.')


if __name__ == '__main__':
    main()
