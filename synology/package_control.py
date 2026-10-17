#!/usr/bin/python3
"""Adopt only the existing camera Compose services; never remove library data."""
import argparse
import datetime
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request


PREFIX = '/webman/3rdparty/XiaomiCameraReview/index.cgi'
DOCKER = '/var/packages/ContainerManager/target/usr/bin/docker'
APP = Path('/volume1/docker/xiaomi-review')
SERVICES = ('web', 'worker')
CONTAINERS = ('xiaomi-review-web', 'xiaomi-review-worker')


def run(command):
    result = subprocess.run([str(item) for item in command], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            universal_newlines=True)
    return result.stdout


def write_json(path, value, mode=0o600):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write('\n')
    os.chmod(str(temporary), mode)
    os.replace(str(temporary), str(path))


def protect_secret(path):
    """Only root can traverse the parent; uid1031 reads the read-only bind mount."""
    os.chown(str(path.parent), 0, 0)
    os.chmod(str(path.parent), 0o700)
    os.chown(str(path), 1031, 0)
    os.chmod(str(path), 0o400)


class Manager:
    """Optional root bootstrap/recovery helper; never invoked by package hooks."""
    def __init__(self, app=APP, target=None, runner=run, protect_secret=protect_secret):
        self.app = Path(app)
        self.target = Path(target or os.environ.get('SYNOPKG_PKGDEST', '/var/packages/XiaomiCameraReview/target'))
        self.state = self.app / '.dsm-package'
        self.runner = runner
        self.protect_secret = protect_secret

    def check(self):
        if not (self.app / '.codex-camera-review').is_file():
            raise RuntimeError('Existing application adoption marker is missing; refusing to manage services')
        if not (self.app / 'compose.yaml').is_file() or not (self.app / '.env').is_file():
            raise RuntimeError('Existing Compose configuration and environment are required')

    def compose(self, *arguments, override=True):
        command = [DOCKER, 'compose', '--project-directory', str(self.app),
                   '--project-name', 'xiaomi-review', '-f', str(self.app / 'compose.yaml')]
        if override and (self.state / 'compose.override.json').exists():
            command += ['-f', str(self.state / 'compose.override.json')]
        images = self.state / 'rollback.images.json'
        if images.exists():
            command += ['-f', str(images)]
        return self.runner(command + list(arguments))

    def snapshot(self):
        self.check()
        self.state.mkdir(mode=0o700, exist_ok=True)
        os.chmod(str(self.state), 0o700)
        backups = self.state / 'backups'
        backups.mkdir(mode=0o700, exist_ok=True)
        backup = backups / (datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%S%fZ'))
        backup.mkdir(mode=0o700)
        files = ['compose.yaml', '.env', 'start-after-network.sh']
        for name in files:
            source = self.app / name
            if source.exists():
                shutil.copy2(str(source), str(backup / name))
                os.chmod(str(backup / name), 0o600)
        for name in ('compose.override.json', 'installed.json'):
            if (self.state / name).exists():
                shutil.copy2(str(self.state / name), str(backup / name))
        config = self.target / 'ui/gateway.json'
        if config.exists():
            shutil.copy2(str(config), str(backup / 'gateway.json'))
        containers = {}
        for name in CONTAINERS:
            containers[name] = json.loads(self.runner([DOCKER, 'inspect', name]))[0]
        write_json(backup / 'containers.json', containers)
        write_json(backup / 'rollback.images.json', {'services': {
            service: {'image': containers[name]['Image']}
            for service, name in zip(SERVICES, CONTAINERS)}})
        # Compose config can contain existing credentials, so backup stays root-only.
        resolved = self.compose('config', '--format', 'json')
        (backup / 'compose.resolved.json').write_text(resolved, encoding='utf-8')
        os.chmod(str(backup / 'compose.resolved.json'), 0o600)
        database = self.app / 'data/catalog.sqlite3'
        if database.exists():
            # SQLite's backup API includes committed WAL pages without copying a live WAL.
            with sqlite3.connect(str(database), timeout=60) as source:
                with sqlite3.connect(str(backup / 'catalog.sqlite3')) as destination:
                    source.backup(destination)
            os.chmod(str(backup / 'catalog.sqlite3'), 0o600)
        write_json(self.state / 'pending.json', {'backup': str(backup)})
        return backup

    def install(self, backup=None):
        self.check()
        # Snapshot immediately before any mutation, including direct postinst retries.
        backup = Path(backup) if backup else self.snapshot()
        try:
            images = self.state / 'rollback.images.json'
            if images.exists():
                images.unlink()
            secret = self.state / 'gateway.secret'
            if not secret.exists():
                descriptor = os.open(str(secret), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, 'w') as output:
                    output.write(secrets.token_hex(32) + '\n')
            value = secret.read_text(encoding='utf-8').strip()
            if len(value) != 64 or len(bytes.fromhex(value)) != 32:
                raise RuntimeError('Existing gateway secret is invalid')
            self.protect_secret(secret)
            web = {
                'environment': {'DSM_GATEWAY_SECRET_FILE': '/run/camera-review-gateway.secret',
                                'DSM_APP_PREFIX': PREFIX},
                'volumes': [{'type': 'bind', 'source': str(secret),
                             'target': '/run/camera-review-gateway.secret', 'read_only': True}],
            }
            write_json(self.state / 'compose.override.json', {'services': {'web': web}})
            write_json(self.target / 'ui/gateway.json', {
                'prefix': PREFIX, 'secret_file': str(secret),
                'upstream': 'http://127.0.0.1:8090',
                'authenticate_cgi': '/usr/syno/synoman/webman/modules/authenticate.cgi',
            }, 0o600)
            boot = self.app / 'start-after-network.sh'
            if boot.exists():
                boot.write_text('#!/bin/sh\n# Lifecycle adopted by XiaomiCameraReview SPK.\nexit 0\n', encoding='utf-8')
                os.chmod(str(boot), 0o755)
            self.compose('config', '--quiet')
            self.compose('up', '-d', '--no-deps', *SERVICES)
            if self.status() != 0:
                raise RuntimeError('Adopted containers did not remain running')
            write_json(self.state / 'installed.json', {'package': 'XiaomiCameraReview',
                        'backup': str(backup), 'target': str(self.target)})
            pending = self.state / 'pending.json'
            if pending.exists():
                pending.unlink()
        except Exception:
            self.rollback(backup)
            raise

    def rollback(self, backup):
        for name in ('compose.yaml', '.env', 'start-after-network.sh'):
            if (backup / name).exists():
                shutil.copy2(str(backup / name), str(self.app / name))
        boot = self.app / 'start-after-network.sh'
        if boot.exists():
            os.chmod(str(boot), 0o755)
        for name in ('compose.override.json', 'installed.json'):
            destination = self.state / name
            if (backup / name).exists():
                shutil.copy2(str(backup / name), str(destination))
            elif destination.exists():
                destination.unlink()
        config = self.target / 'ui/gateway.json'
        if (backup / 'gateway.json').exists():
            shutil.copy2(str(backup / 'gateway.json'), str(config))
        elif config.exists():
            config.unlink()
        # Do not overwrite a live catalog: package changes never alter its schema/data.
        shutil.copy2(str(backup / 'rollback.images.json'), str(self.state / 'rollback.images.json'))
        self.compose('up', '-d', '--no-deps', *SERVICES)
        containers = json.loads((backup / 'containers.json').read_text(encoding='utf-8'))
        stopped = [name for name in CONTAINERS if not containers[name]['State']['Running']]
        if stopped:
            self.runner([DOCKER, 'stop'] + stopped)

    def start(self):
        self.check()
        if not (self.state / 'installed.json').exists():
            raise RuntimeError('Package installation has not completed')
        self.compose('up', '-d', '--no-deps', *SERVICES)

    def stop(self):
        self.check()
        self.runner([DOCKER, 'stop'] + list(CONTAINERS))

    def status(self):
        self.check()
        for name in CONTAINERS:
            state = json.loads(self.runner([DOCKER, 'inspect', name]))[0]['State']
            if not state['Running']:
                return 1
        return 0

    def uninstall(self):
        self.stop()
        # Keep backups, secret and override for a future reinstall. No data removal.


def healthy():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:8090/', timeout=5) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


class PackageManager:
    """Prepare configuration as the package user; DSM's worker owns Docker access."""
    def __init__(self, app=APP, target=None, runtime=None, health=healthy, pause=time.sleep):
        self.app = Path(app)
        self.target = Path(target or os.environ.get('SYNOPKG_PKGDEST',
                                                  '/var/packages/XiaomiCameraReview/target'))
        # DSM has not created target/var during preinst; root bootstrap prepares this.
        self.runtime = Path(runtime or self.app / '.dsm-package/runtime')
        self.health = health
        self.pause = pause

    def prepare(self):
        if not (self.app / '.codex-camera-review').is_file():
            raise RuntimeError('Existing application adoption marker is missing')
        template = self.app / 'dsm-project-template.json'
        document = json.loads(template.read_text(encoding='utf-8'))
        services = document.get('services', {})
        if set(services) != set(SERVICES):
            raise RuntimeError('Prepared project must contain only web and worker')
        if document.get('name', 'xiaomi-review') != 'xiaomi-review':
            raise RuntimeError('Prepared Compose project name is invalid')
        for service, container in zip(SERVICES, CONTAINERS):
            if services[service].get('container_name') != container:
                raise RuntimeError('Prepared container name is invalid: ' + service)
        if not self.runtime.is_dir():
            raise RuntimeError('Root bootstrap must prepare the package runtime directory')
        os.chmod(str(self.runtime), 0o700)
        secret = self.runtime / 'gateway.secret'
        if not secret.exists():
            descriptor = os.open(str(secret), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
            with os.fdopen(descriptor, 'w') as output:
                output.write(secrets.token_hex(32) + '\n')
        value = secret.read_text(encoding='utf-8').strip()
        if len(value) != 64 or len(bytes.fromhex(value)) != 32:
            raise RuntimeError('Existing gateway secret is invalid')
        # Host runtime0700 blocks traversal; web reads only the read-only file bind.
        os.chmod(str(secret), 0o444)
        web = services['web']
        web.setdefault('environment', {}).update({
            'DSM_GATEWAY_SECRET_FILE': '/run/camera-review-gateway.secret',
            'DSM_APP_PREFIX': PREFIX,
        })
        mounts = web.setdefault('volumes', [])
        mounts[:] = [mount for mount in mounts
                     if not isinstance(mount, dict) or mount.get('target') != '/run/camera-review-gateway.secret']
        mounts.append({'type': 'bind', 'source': str(secret),
                       'target': '/run/camera-review-gateway.secret', 'read_only': True})
        project = self.runtime / 'project'
        project.mkdir(mode=0o700, exist_ok=True)
        previous = project / 'compose.yaml'
        pending = self.runtime / 'pending-project.json'
        if previous.exists() and not pending.exists():
            shutil.copy2(str(previous), str(self.runtime / 'previous.compose.yaml'))
            write_json(pending, {'previous': 'previous.compose.yaml'})
        write_json(project / 'compose.yaml', document)

    def install(self):
        if not (self.runtime / 'project/compose.yaml').is_file():
            raise RuntimeError('Native Docker project was not prepared before resource acquisition')
        write_json(self.target / 'ui/gateway.json', {
            'prefix': PREFIX, 'secret_file': str(self.runtime / 'gateway.secret'),
            'upstream': 'http://127.0.0.1:8090',
            'authenticate_cgi': '/usr/syno/synoman/webman/modules/authenticate.cgi',
        })

    def start(self):
        for attempt in range(20):
            if self.health():
                write_json(self.runtime / 'running.json', {'native_resource': 'xiaomi-review'})
                pending = self.runtime / 'pending-project.json'
                if pending.exists():
                    pending.unlink()
                return
            self.pause(0.25)
        self.restore_project()
        raise RuntimeError('Native Docker project web service is unreachable')

    def restore_project(self):
        pending = self.runtime / 'pending-project.json'
        previous = self.runtime / 'previous.compose.yaml'
        if pending.exists() and previous.exists():
            shutil.copy2(str(previous), str(self.runtime / 'project/compose.yaml'))
            pending.unlink()
        self.stop()

    def stop(self):
        marker = self.runtime / 'running.json'
        if marker.exists():
            marker.unlink()

    def status(self):
        return 0 if (self.runtime / 'running.json').exists() and self.health() else 1

    def uninstall(self):
        self.stop()
        # The native worker releases its project; existing bind-mounted data is untouched.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['preinst', 'preupgrade', 'postinst', 'postupgrade',
                                          'preuninst', 'postuninst', 'start', 'stop', 'status', 'recover'])
    args = parser.parse_args()
    manager = PackageManager()
    if args.action in ('preinst', 'preupgrade'):
        manager.prepare()
    elif args.action in ('postinst', 'postupgrade'):
        manager.install()
    elif args.action == 'preuninst':
        manager.uninstall()
    elif args.action == 'status':
        return manager.status()
    elif args.action == 'recover':
        manager.restore_project()
    elif args.action in ('start', 'stop'):
        getattr(manager, args.action)()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('Camera review package: ' + str(error), file=sys.stderr)
        sys.exit(1)
