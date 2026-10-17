"""Public archives must exclude operational data even in a populated checkout."""
import importlib.util
from pathlib import Path
import shutil
import secrets
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('build_release', ROOT / 'tools/build_release.py')
builder = None
if (ROOT / 'tools/build_release.py').is_file():
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)


class ReleaseArchiveTests(unittest.TestCase):
    def test_repeatable_archive_excludes_private_files_and_contains_install_inputs(self):
        self.assertIsNotNone(builder, 'source release builder must exist')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'source'
            shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns(
                '.git', '.venv', 'data', 'models', 'output', '.superpowers', '__pycache__', '.env'))
            sentinel = secrets.token_bytes(48)
            for relative in ('.env', '.env.production', 'models/private.onnx', 'data/catalog.sqlite3',
                             'docs/private-acceptance.md', 'output/credentials.json',
                             'static/session.json', 'tools/production-password.py',
                             'docs/superpowers/private-report.md'):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(sentinel)
            first = builder.build(root, Path(temporary) / 'one.zip')
            second = builder.build(root, Path(temporary) / 'two.zip')
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                prefix = 'shiguang-camera-review-1.0.0/'
                members = {name.removeprefix(prefix) for name in archive.namelist()}
                for name in ('README.md', 'LICENSE', '.env.example', 'Dockerfile', 'compose.yaml',
                             'tools/init_config.py', 'tools/download_model.py', 'tools/build_release.py',
                             'third_party/model-manifest.json', '.github/workflows/ci.yml'):
                    self.assertIn(name, members)
                self.assertNotIn('Dockerfile.base', members)
                self.assertNotIn('start-after-network.sh', members)
                self.assertTrue(all('..' not in Path(name).parts for name in archive.namelist()))
                for name in archive.namelist():
                    self.assertNotIn(sentinel, archive.read(name), name)

    def test_missing_license_refuses_to_create_an_incomplete_release(self):
        self.assertIsNotNone(builder, 'source release builder must exist')
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(FileNotFoundError):
                builder.build(Path(temporary), Path(temporary) / 'release.zip')
            self.assertFalse((Path(temporary) / 'release.zip').exists())

    def test_selected_symbolic_link_is_refused(self):
        self.assertIsNotNone(builder, 'source release builder must exist')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'source'
            shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns(
                '.git', '.venv', 'data', 'models', 'output', '.superpowers', '__pycache__', '.env'))
            readme = root / 'README.md'
            readme.unlink()
            readme.symlink_to(ROOT / 'README.md')
            with self.assertRaises(ValueError):
                builder.build(root, Path(temporary) / 'release.zip')
            self.assertFalse((Path(temporary) / 'release.zip').exists())


if __name__ == '__main__':
    unittest.main()
