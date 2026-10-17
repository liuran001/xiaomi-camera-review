#!/usr/bin/env python3
"""Build a deterministic allowlisted source ZIP without runtime data or history."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FIXED = (
    '.dockerignore', '.env.example', '.gitattributes', '.gitignore',
    '.github/workflows/ci.yml', 'API.md', 'CHANGELOG.md', 'CONTRIBUTING.md',
    'Dockerfile', 'LICENSE', 'README.md', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md',
    'VERSION', 'compose.yaml', 'requirements.txt',
    'docs/architecture.md', 'docs/configuration.md', 'docs/deployment.md',
    'docs/dsm-native-token-bridge.md', 'docs/releasing.md',
    'tools/build_release.py', 'tools/build_spk.py', 'tools/download_model.py', 'tools/init_config.py',
    'third_party/model-manifest.json', 'third_party/YOLOX-LICENSE',
    'third_party/MobileNet-SSD-LICENSE',
    'synology/INFO', 'synology/README.md', 'synology/PACKAGE_ICON.PNG',
    'synology/PACKAGE_ICON_256.PNG', 'synology/bootstrap.js', 'synology/gateway.py',
    'synology/package_control.py', 'synology/conf/privilege', 'synology/conf/resource',
    'synology/ui/config', 'synology/ui/index.cgi',
)
PATTERNS = ('camera_review/*.py', 'static/*.js', 'static/*.css', 'static/*.html',
            'tests/test_*.py', 'tests/test_*.js', 'synology/ui/images/*.png')
HOOKS = ('postinst', 'postuninst', 'postupgrade', 'preinst', 'preuninst',
         'preupgrade', 'start-stop-status')


def source_files(root):
    root = Path(root)
    names = set(FIXED) | {'synology/scripts/' + name for name in HOOKS}
    for pattern in PATTERNS:
        names.update(path.relative_to(root).as_posix() for path in root.glob(pattern))
    for name in sorted(names):
        path = root / name
        current = path
        while current != root:
            if current.is_symlink():
                raise ValueError('Refusing symbolic link in release: ' + name)
            current = current.parent
        if not path.is_file():
            raise FileNotFoundError('Required release source missing: ' + name)
    return sorted(names)


def build(root, output):
    root, output = Path(root).absolute(), Path(output).absolute()
    names = source_files(root)
    version = (root / 'VERSION').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Invalid release version')
    prefix = 'shiguang-camera-review-' + version + '/'
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.release-', suffix='.part', dir=str(output.parent))
    os.close(descriptor)
    temporary = Path(temporary)
    try:
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in names:
                info = zipfile.ZipInfo(prefix + name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                executable = name.startswith('synology/scripts/') or name.endswith('.cgi')
                info.external_attr = (0o100755 if executable else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, (root / name).read_bytes(), compresslevel=9)
        os.replace(str(temporary), str(output))
    finally:
        temporary.unlink(missing_ok=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/release')
    args = parser.parse_args()
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    output = args.output
    if output.suffix.lower() != '.zip':
        output = output / ('shiguang-camera-review-' + version + '-source.zip')
    artifact = build(ROOT, output)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    checksum = artifact.parent / (artifact.name + '.sha256')
    checksum.write_text(digest + '  ' + artifact.name + '\n', encoding='ascii')
    print(str(artifact))
    print('SHA256 ' + digest)


if __name__ == '__main__':
    main()
