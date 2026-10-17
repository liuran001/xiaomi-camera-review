#!/usr/bin/env python3
"""Create a reproducible, credential-free DSM package using only the stdlib."""
import argparse
import gzip
import hashlib
import io
from pathlib import Path
import tarfile


ROOT = Path(__file__).resolve().parents[1]


def archive_bytes(files, compressed=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for entry in sorted(files):
            name, content, mode = entry[:3]
            if name.startswith('/') or '..' in Path(name).parts:
                raise ValueError('Unsafe archive member: ' + name)
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = mode
            member.uid = member.gid = member.mtime = 0
            member.uname = member.gname = 'root'
            if len(entry) == 4:
                # The one fixed link points into the prepared package-owned runtime.
                if name != 'project' or entry[3] != '/volume1/docker/xiaomi-review/.dsm-package/runtime/project':
                    raise ValueError('Unexpected package symbolic link')
                member.type = tarfile.SYMTYPE
                member.linkname = entry[3]
                member.size = 0
            archive.addfile(member, io.BytesIO(content))
    if not compressed:
        return output.getvalue()
    zipped = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=zipped, mtime=0) as stream:
        stream.write(output.getvalue())
    return zipped.getvalue()


def build(root, output):
    root, output = Path(root), Path(output)
    synology = root / 'synology'
    payload = []
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        payload.append((name, (root / name).read_bytes(), 0o644))
    for name in ('gateway.py', 'bootstrap.js', 'package_control.py', 'ui/index.cgi', 'ui/config'):
        source = synology / name
        if not source.is_file():
            raise ValueError('Required package source missing: ' + name)
        payload.append((name, source.read_bytes(), 0o755 if name.endswith('.cgi') else 0o644))
    for source in sorted((synology / 'ui/images').glob('*.png')):
        payload.append((source.relative_to(synology).as_posix(), source.read_bytes(), 0o644))
    payload.append(('project', b'', 0o777, '/volume1/docker/xiaomi-review/.dsm-package/runtime/project'))
    package = archive_bytes(payload, compressed=True)
    info = (synology / 'INFO').read_bytes()
    info += ('checksum="' + hashlib.md5(package).hexdigest() + '"\n').encode('ascii')
    outer = [('INFO', info, 0o644), ('package.tgz', package, 0o644)]
    for directory in ('conf', 'scripts'):
        for source in sorted((synology / directory).iterdir()):
            if source.is_file():
                outer.append((source.relative_to(synology).as_posix(), source.read_bytes(),
                              0o755 if directory == 'scripts' else 0o644))
    # Pre-install hooks need the controller before package.tgz has been unpacked.
    outer.append(('scripts/package_control.py', (synology / 'package_control.py').read_bytes(), 0o644))
    for name in ('PACKAGE_ICON.PNG', 'PACKAGE_ICON_256.PNG'):
        outer.append((name, (synology / name).read_bytes(), 0o644))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(archive_bytes(outer))
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT / 'output/synology')
    args = parser.parse_args()
    output = args.output
    if output.suffix.lower() != '.spk':
        output = output / 'XiaomiCameraReview-1.0.0-1006.spk'
    artifact = build(ROOT, output)
    print(str(artifact))
    print('SHA256 ' + hashlib.sha256(artifact.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
