import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    if spec is None or spec.loader is None or not (ROOT / relative).exists():
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = load('build_spk', 'tools/build_spk.py')
control = load('package_control', 'synology/package_control.py')


class ArchiveTests(unittest.TestCase):
    def test_reproducible_archive_has_dsm_contract_and_modes(self):
        self.assertIsNotNone(build, 'deterministic SPK builder must exist')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            first = build.build(ROOT, path / 'one.spk')
            second = build.build(ROOT, path / 'two.spk')
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with tarfile.open(first) as archive:
                info = archive.extractfile('INFO').read().decode()
                self.assertIn('package="XiaomiCameraReview"', info)
                self.assertIn('version="1.0.0-1006"', info)
                self.assertIn('ContainerManager', info)
                self.assertIn('arch="x86_64"', info)
                privilege = json.load(archive.extractfile('conf/privilege'))
                self.assertEqual(privilege['defaults']['run-as'], 'package')
                resource = json.load(archive.extractfile('conf/resource'))['docker-project']['projects']
                self.assertEqual(resource, [{'name': 'xiaomi-review', 'path': 'project',
                    'build_params': {'build': False, 'force_pull': False, 'force_recreate': False}}])
                self.assertEqual(archive.getmember('scripts/start-stop-status').mode, 0o755)
                with tarfile.open(fileobj=io.BytesIO(archive.extractfile('package.tgz').read())) as package:
                    self.assertEqual(package.getmember('ui/index.cgi').mode, 0o755)
                    self.assertIn('gateway.py', package.getnames())
                    self.assertEqual(package.extractfile('bootstrap.js').read(),
                                     (ROOT / 'synology/bootstrap.js').read_bytes())
                    self.assertIn('package_control.py', package.getnames())
                    self.assertIn(b'MIT License', package.extractfile('LICENSE').read())
                    self.assertIn('THIRD_PARTY_NOTICES.md', package.getnames())
                    self.assertNotIn('ui/gateway.json', package.getnames())
                    self.assertTrue(package.getmember('project').issym())
                    self.assertEqual(package.getmember('project').linkname,
                                     '/volume1/docker/xiaomi-review/.dsm-package/runtime/project')
                    desktop = json.load(package.extractfile('ui/config'))['.url']['com.xiaomi.camera.review']
                    # DSM's generic URL launcher blocks third-party QuickConnect entries.
                    # The supported legacy window launcher opens this authenticated same-origin route.
                    self.assertEqual(desktop['type'], 'legacy')
                    self.assertTrue(desktop['allUsers'])
                    self.assertEqual(desktop['url'], 'webman/3rdparty/XiaomiCameraReview/index.cgi/')
                    self.assertEqual(desktop['urlTarget'], '_blank')
                    for member in package:
                        self.assertFalse(member.name.startswith('/') or '..' in Path(member.name).parts)
                        self.assertEqual(member.mtime, 0)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(control, 'safe package lifecycle controller must exist')
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / 'app'
        self.target = self.root / 'target'
        self.app.mkdir()
        (self.target / 'ui').mkdir(parents=True)
        (self.app / 'data').mkdir()
        (self.app / 'models').mkdir()
        (self.app / '.codex-camera-review').write_text('owned')
        (self.app / '.env').write_text('APP_PASSWORD_HASH=preserved\n')
        (self.app / 'compose.yaml').write_bytes((ROOT / 'compose.yaml').read_bytes())
        (self.app / 'start-after-network.sh').write_text('#!/bin/sh\ndocker start xiaomi-review-web xiaomi-review-worker\n')
        with sqlite3.connect(str(self.app / 'data/catalog.sqlite3')) as db:
            db.execute('CREATE TABLE progress (value INTEGER)')
            db.execute('INSERT INTO progress VALUES (42)')
        self.calls = []
        self.permissions = []
        def runner(command):
            self.calls.append(command)
            if command[1:3] == ['inspect', 'xiaomi-review-web']:
                return json.dumps([{'State': {'Running': True}, 'Image': 'sha256:oldweb', 'Config': {'Image': 'old:image'}}])
            if command[1:3] == ['inspect', 'xiaomi-review-worker']:
                return json.dumps([{'State': {'Running': True}, 'Image': 'sha256:oldworker', 'Config': {'Image': 'old:image'}}])
            return '[]'
        self.runner = runner
        self.manager = control.Manager(self.app, self.target, runner=runner,
                                       protect_secret=lambda path: self.permissions.append(path))

    def test_install_preserves_environment_and_backs_up_sqlite_and_boot_hook(self):
        before = (self.app / '.env').read_bytes()
        self.manager.install()
        self.assertEqual((self.app / '.env').read_bytes(), before)
        state = self.app / '.dsm-package'
        secret = (state / 'gateway.secret').read_text().strip()
        self.assertEqual(len(bytes.fromhex(secret)), 32)
        self.assertEqual(self.permissions, [state / 'gateway.secret'])
        config = json.loads((self.target / 'ui/gateway.json').read_text())
        self.assertEqual(config['secret_file'], str(state / 'gateway.secret'))
        override = json.loads((state / 'compose.override.json').read_text())
        self.assertEqual(set(override['services']), {'web'})
        self.assertEqual(override['services']['web']['environment']['DSM_APP_PREFIX'], control.PREFIX)
        self.assertTrue(override['services']['web']['volumes'][0]['read_only'])
        backup = next((state / 'backups').iterdir())
        with sqlite3.connect(str(backup / 'catalog.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT value FROM progress').fetchone()[0], 42)
        self.assertIn('exit 0', (self.app / 'start-after-network.sh').read_text())
        self.assertEqual((backup / '.env').read_bytes(), before)
        self.assertTrue((backup / 'containers.json').exists())
        self.assertTrue((backup / 'compose.resolved.json').exists())
        up = [call for call in self.calls if 'up' in call]
        self.assertEqual(up[-1][-2:], ['web', 'worker'])
        self.assertIn('--no-deps', up[-1])

    def test_failed_install_restores_original_boot_hook_and_legacy_services(self):
        original = (self.app / 'start-after-network.sh').read_bytes()
        def failing(command):
            if 'up' in command and 'compose.override.json' in ' '.join(command):
                raise RuntimeError('candidate rejected')
            return self.runner(command)
        self.manager.runner = failing
        with self.assertRaisesRegex(RuntimeError, 'candidate rejected'):
            self.manager.install()
        self.assertEqual((self.app / 'start-after-network.sh').read_bytes(), original)
        self.assertFalse((self.app / '.dsm-package/installed.json').exists())
        self.assertFalse((self.target / 'ui/gateway.json').exists())
        rollback = [call for call in self.calls if 'up' in call]
        self.assertNotIn('compose.override.json', ' '.join(rollback[-1]))
        self.assertEqual(rollback[-1][-2:], ['web', 'worker'])
        self.assertIn('rollback.images.json', ' '.join(rollback[-1]))
        backup = next((self.app / '.dsm-package/backups').iterdir())
        images = json.loads((backup / 'rollback.images.json').read_text())
        self.assertEqual(images['services']['worker']['image'], 'sha256:oldworker')

    def test_sqlite_snapshot_includes_committed_wal_pages(self):
        database = self.app / 'data/catalog.sqlite3'
        with sqlite3.connect(str(database)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('INSERT INTO progress VALUES (84)')
            db.commit()
            backup = self.manager.snapshot()
            with sqlite3.connect(str(backup / 'catalog.sqlite3')) as saved:
                self.assertEqual(saved.execute('SELECT SUM(value) FROM progress').fetchone()[0], 126)

    def test_existing_stopped_worker_is_restored_stopped_on_failed_install(self):
        def failing(command):
            if 'up' in command and 'compose.override.json' in ' '.join(command):
                raise RuntimeError('candidate rejected')
            if command[1:3] == ['inspect', 'xiaomi-review-worker']:
                return json.dumps([{'State': {'Running': False}, 'Image': 'sha256:oldworker'}])
            return self.runner(command)
        self.manager.runner = failing
        with self.assertRaisesRegex(RuntimeError, 'candidate rejected'):
            self.manager.install()
        self.assertEqual(self.calls[-1], [control.DOCKER, 'stop', 'xiaomi-review-worker'])

    def test_failed_upgrade_pins_previous_images_until_successful_upgrade(self):
        self.manager.install()
        failed = False
        def fail_once(command):
            nonlocal failed
            if 'up' in command and not failed:
                failed = True
                raise RuntimeError('candidate rejected')
            return self.runner(command)
        self.manager.runner = fail_once
        with self.assertRaisesRegex(RuntimeError, 'candidate rejected'):
            self.manager.install()
        self.manager.start()
        restarted = [call for call in self.calls if 'up' in call][-1]
        self.assertIn('rollback.images.json', ' '.join(restarted))
        self.manager.install()
        upgraded = [call for call in self.calls if 'up' in call][-1]
        self.assertNotIn('rollback.images.json', ' '.join(upgraded))

    def test_stop_and_uninstall_preserve_data_and_do_not_run_compose_down(self):
        self.manager.install()
        self.manager.stop()
        self.manager.uninstall()
        self.assertTrue((self.app / 'data/catalog.sqlite3').exists())
        self.assertTrue((self.app / 'models').exists())
        self.assertTrue((self.app / '.env').exists())
        self.assertFalse(any('down' in call or 'rm' in call for call in self.calls))
        stopped = [call for call in self.calls if 'stop' in call]
        self.assertTrue(stopped)
        self.assertTrue(all(call[-2:] == ['xiaomi-review-web', 'xiaomi-review-worker'] for call in stopped))

    def test_reinstall_keeps_secret_and_declines_unmarked_directory(self):
        self.manager.install()
        secret = (self.app / '.dsm-package/gateway.secret').read_bytes()
        self.manager.install()
        self.assertEqual((self.app / '.dsm-package/gateway.secret').read_bytes(), secret)
        (self.app / '.codex-camera-review').unlink()
        with self.assertRaisesRegex(RuntimeError, 'adoption marker'):
            self.manager.start()


class PackageLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(control, 'PackageManager'), 'nonroot native resource lifecycle must exist')
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / 'app'
        self.target = self.root / 'target'
        self.var = self.root / 'var'
        self.app.mkdir()
        (self.target / 'ui').mkdir(parents=True)
        self.var.mkdir()
        (self.app / '.codex-camera-review').write_text('owned')
        self.template = self.app / 'dsm-project-template.json'
        self.document = {'name': 'xiaomi-review', 'services': {
            'web': {'image': 'xiaomi-review:candidate', 'container_name': 'xiaomi-review-web',
                    'user': '1031:100', 'environment': {'DATA_ROOT': '/data'},
                    'env_file': ['/volume1/docker/xiaomi-review/.env'],
                    'volumes': [{'type': 'bind', 'source': '/volume1/docker/xiaomi-review/data',
                                 'target': '/data'}]},
            'worker': {'image': 'xiaomi-review:candidate', 'container_name': 'xiaomi-review-worker',
                       'env_file': ['/volume1/docker/xiaomi-review/.env']}}}
        self.template.write_text(json.dumps(self.document))
        self.manager = control.PackageManager(app=self.app, target=self.target,
            runtime=self.var, health=lambda: True)
        self.manager.pause = lambda _: None

    def test_preinst_prepares_native_resource_before_postinst_without_docker_access(self):
        before = self.template.read_bytes()
        with patch.object(control.os, 'chmod', wraps=os.chmod) as chmod:
            self.manager.prepare()
        self.assertTrue(any(call.args == (str(self.var / 'gateway.secret'), 0o444)
                            for call in chmod.call_args_list), 'mounted secret must be mode0444')
        self.assertEqual(self.template.read_bytes(), before)
        secret = self.var / 'gateway.secret'
        self.assertEqual(len(bytes.fromhex(secret.read_text().strip())), 32)
        project = json.loads((self.var / 'project/compose.yaml').read_text())
        self.assertEqual(set(project['services']), {'web', 'worker'})
        web = project['services']['web']
        self.assertNotIn('group_add', web)
        self.assertEqual(web['environment']['DSM_APP_PREFIX'], control.PREFIX)
        self.assertEqual(web['environment']['DATA_ROOT'], '/data')
        self.assertEqual(web['env_file'], self.document['services']['web']['env_file'])
        self.assertTrue(web['volumes'][-1]['read_only'])
        self.assertEqual(project['services']['worker'], self.document['services']['worker'])
        self.assertFalse((self.target / 'ui/gateway.json').exists())
        self.manager.install()
        gateway = json.loads((self.target / 'ui/gateway.json').read_text())
        self.assertEqual(gateway['secret_file'], str(secret))

    def test_existing_template_groups_are_preserved_without_adding_package_group(self):
        self.document['services']['web']['group_add'] = ['existing-group']
        self.template.write_text(json.dumps(self.document))
        self.manager.prepare()
        project = json.loads((self.var / 'project/compose.yaml').read_text())
        self.assertEqual(project['services']['web']['group_add'], ['existing-group'])

    def test_upgrade_retains_secret_and_uninstall_leaves_existing_library_untouched(self):
        original = self.template.read_bytes()
        self.manager.prepare()
        secret = (self.var / 'gateway.secret').read_bytes()
        self.manager.prepare()
        self.assertEqual((self.var / 'gateway.secret').read_bytes(), secret)
        self.manager.install()
        self.manager.start()
        self.assertEqual(self.manager.status(), 0)
        self.manager.stop()
        self.assertEqual(self.manager.status(), 1)
        self.manager.uninstall()
        self.assertEqual(self.template.read_bytes(), original)

    def test_start_and_status_fail_when_native_project_is_unreachable(self):
        self.manager.prepare()
        self.manager.install()
        self.manager.health = lambda: False
        with self.assertRaisesRegex(RuntimeError, 'unreachable'):
            self.manager.start()
        self.assertEqual(self.manager.status(), 1)

    def test_start_waits_for_native_container_http_readiness(self):
        self.manager.prepare()
        self.manager.install()
        responses = iter((False, False, True))
        self.manager.health = lambda: next(responses)
        try:
            self.manager.start()
        except RuntimeError as error:
            self.fail('start must tolerate native container warmup: ' + str(error))
        self.assertTrue((self.var / 'running.json').exists())

    def test_template_must_only_adopt_the_two_named_services(self):
        self.document['services']['unrelated'] = {'image': 'foreign'}
        self.template.write_text(json.dumps(self.document))
        with self.assertRaisesRegex(RuntimeError, 'web and worker'):
            self.manager.prepare()
        self.assertFalse((self.var / 'project/compose.yaml').exists())

    def test_failed_candidate_readiness_restores_previous_project_for_next_native_start(self):
        self.manager.prepare()
        self.manager.install()
        original = (self.var / 'project/compose.yaml').read_bytes()
        self.document['services']['web']['image'] = 'xiaomi-review:rejected'
        self.template.write_text(json.dumps(self.document))
        self.manager.prepare()
        self.manager.install()
        self.manager.health = lambda: False
        with self.assertRaisesRegex(RuntimeError, 'unreachable'):
            self.manager.start()
        self.assertEqual((self.var / 'project/compose.yaml').read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
