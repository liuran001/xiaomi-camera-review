"""Release tooling must preserve local configuration and reject corrupt models."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, patch

from tools import download_model, init_config


class ModelDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.payload = b'validated-model-bytes'
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps({'files': [{
            'file': 'detector.onnx', 'bytes': len(self.payload),
            'sha256': hashlib.sha256(self.payload).hexdigest(),
            'source': 'https://models.example/detector.onnx',
        }]}), encoding='utf-8')
        self.models = self.root / 'models'

    def test_valid_download_publishes_verified_bytes_and_leaves_no_partial(self):
        result = download_model.download(self.manifest, self.models,
                                         opener=lambda *a, **k: io.BytesIO(self.payload))
        self.assertEqual(result.read_bytes(), self.payload)
        self.assertEqual([p.name for p in self.models.iterdir()], ['detector.onnx'])

    def test_already_valid_model_needs_no_network(self):
        self.models.mkdir()
        target = self.models / 'detector.onnx'
        target.write_bytes(self.payload)
        def unavailable(*args, **kwargs):
            self.fail('valid model must work offline')
        self.assertEqual(download_model.download(self.manifest, self.models, opener=unavailable), target)

    def test_wrong_hash_or_size_never_replaces_existing_model(self):
        self.models.mkdir()
        target = self.models / 'detector.onnx'
        target.write_bytes(b'old-model')
        for payload in (b'x' * len(self.payload), b'short', self.payload + b'oversized'):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    download_model.download(self.manifest, self.models,
                                            opener=lambda *a, **k: io.BytesIO(payload))
                self.assertEqual(target.read_bytes(), b'old-model')
                self.assertEqual([p.name for p in self.models.iterdir()], ['detector.onnx'])

    def test_interrupted_download_removes_only_its_temporary_file(self):
        self.models.mkdir()
        unrelated = self.models / '.unrelated.part'
        unrelated.write_bytes(b'keep')
        class Broken(io.BytesIO):
            def read(self, *args):
                raise OSError('connection interrupted')
        with self.assertRaises(OSError):
            download_model.download(self.manifest, self.models, opener=lambda *a, **k: Broken())
        self.assertEqual(list(self.models.iterdir()), [unrelated])
        self.assertEqual(unrelated.read_bytes(), b'keep')

    def test_manifest_cannot_write_outside_model_directory(self):
        spec = json.loads(self.manifest.read_text(encoding='utf-8'))
        for name in ('../outside.onnx', '/outside.onnx', 'nested/model.onnx', '..\\outside.onnx'):
            spec['files'][0]['file'] = name
            self.manifest.write_text(json.dumps(spec), encoding='utf-8')
            with self.subTest(name=name), self.assertRaises(ValueError):
                download_model.download(self.manifest, self.models,
                                        opener=lambda *a, **k: io.BytesIO(self.payload))


class ConfigurationTests(unittest.TestCase):
    def test_root_initializer_assigns_new_private_directories_to_container_user(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = Path(__file__).resolve().parents[1] / '.env.example'
            (root / '.env.example').write_bytes(template.read_bytes())
            with patch.object(init_config.os, 'getuid', return_value=0, create=True), \
                    patch.object(init_config.os, 'chown', create=True) as chown:
                init_config.initialize(root)
            self.assertEqual(chown.call_args_list, [call(root / 'data', 1000, 1000),
                                                   call(root / 'models', 1000, 1000)])

    def test_root_initializer_leaves_existing_directory_ownership_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = Path(__file__).resolve().parents[1] / '.env.example'
            (root / '.env.example').write_bytes(template.read_bytes())
            (root / 'data').mkdir()
            with patch.object(init_config.os, 'getuid', return_value=0, create=True), \
                    patch.object(init_config.os, 'chown', create=True) as chown:
                init_config.initialize(root)
            self.assertEqual(chown.call_args_list, [call(root / 'models', 1000, 1000)])

    def test_fresh_configuration_has_unique_secrets_and_private_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            template = Path(__file__).resolve().parents[1] / '.env.example'
            first, second = root / 'first', root / 'second'
            for folder in (first, second):
                folder.mkdir()
                (folder / '.env.example').write_bytes(template.read_bytes())
                init_config.initialize(folder)
                self.assertTrue((folder / 'data').is_dir())
                self.assertTrue((folder / 'models').is_dir())
                content = (folder / '.env').read_text(encoding='utf-8')
                self.assertNotIn('CHANGE_ME', content)
                values = dict(line.split('=', 1) for line in content.splitlines()
                              if line and not line.startswith('#'))
                self.assertGreaterEqual(len(values['APP_PASSWORD']), 20)
                self.assertGreaterEqual(len(values['SESSION_SECRET']), 64)
            self.assertNotEqual((first / '.env').read_bytes(), (second / '.env').read_bytes())

    def test_existing_environment_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / '.env').write_bytes(b'private-existing-settings\n')
            with self.assertRaises(FileExistsError):
                init_config.initialize(root)
            self.assertEqual((root / '.env').read_bytes(), b'private-existing-settings\n')


if __name__ == '__main__':
    unittest.main()
