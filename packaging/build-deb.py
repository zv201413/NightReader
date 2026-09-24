"""Build the Ubuntu 24.04 amd64 package and publishable dependency sources.

Run with system Python 3.12 on Ubuntu 24.04. Only build/cache and dist are written.
Downloads are pinned by SHA-256; pip is never run on the end user's machine.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def download(item, cache):
    target = cache / item['filename']
    if target.exists() and sha256(target) == item['sha256']:
        return target
    part = target.with_suffix(target.suffix + '.part')
    print('Downloading', item['filename'], flush=True)
    with urllib.request.urlopen(item['url'], timeout=120) as source, part.open('wb') as out:
        shutil.copyfileobj(source, out)
    if sha256(part) != item['sha256']:
        part.unlink()
        raise RuntimeError(f"SHA-256 mismatch: {item['filename']}")
    part.replace(target)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=ROOT / 'build/cache')
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12) or platform.machine() != 'x86_64':
        parser.error('Build on Ubuntu 24.04 amd64 with Python 3.12')
    os_release = platform.freedesktop_os_release()
    if os_release.get('ID') != 'ubuntu' or os_release.get('VERSION_ID') != '24.04':
        parser.error('This binary package is tested on Ubuntu 24.04 only')
    version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
    locked = json.loads((ROOT / 'packaging/dependencies.json').read_text())
    args.cache.mkdir(parents=True, exist_ok=True)
    cache = args.cache.resolve()
    files = [p[k] for p in locked['packages'] for k in ('wheel', 'source')]
    files.extend(locked['native_sources'])
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda item: download(item, cache), files))
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='nightreader-deb-') as tmp:
        stage = Path(tmp)
        app = stage / 'opt/nightreader'
        app.mkdir(parents=True)
        shutil.copytree(ROOT / 'nightread', app / 'nightread',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        wheels = [str(cache / p['wheel']['filename']) for p in locked['packages']]
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-index',
                        '--no-deps', '--no-compile', '--target', str(app / 'lib'),
                        *wheels], check=True)
        # Installed files must not contain a build machine's temporary paths.
        for direct_url in (app / 'lib').glob('*.dist-info/direct_url.json'):
            direct_url.unlink()
        shutil.copy2(ROOT / 'packaging/nightreader', app / 'nightreader')
        (app / 'nightreader').chmod(0o755)
        bindir = stage / 'usr/bin'
        bindir.mkdir(parents=True)
        (bindir / 'nightreader').symlink_to('/opt/nightreader/nightreader')
        for source, destination in (
            ('assets/nightreader.desktop', 'usr/share/applications/nightreader.desktop'),
            ('assets/nightreader.svg', 'usr/share/icons/hicolor/scalable/apps/nightreader.svg'),
            ('LICENSE', 'usr/share/doc/nightreader/copyright'),
            ('README.md', 'usr/share/doc/nightreader/README.md'),
            ('THIRD_PARTY_NOTICES.md', 'usr/share/doc/nightreader/THIRD_PARTY_NOTICES.md'),
            ('packaging/dependencies.json', 'usr/share/doc/nightreader/dependencies.json'),
        ):
            target = stage / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / source, target)
        control = stage / 'DEBIAN'
        control.mkdir()
        size = sum(p.stat().st_size for p in stage.rglob('*') if p.is_file() and not p.is_symlink())
        (control / 'control').write_text(
            f'Package: nightreader\nVersion: {version}\nArchitecture: amd64\n'
            'Section: graphics\nPriority: optional\n'
            'Maintainer: NightReader contributors <zv201413@users.noreply.github.com>\n'
            'Homepage: https://github.com/zv201413/NightReader\n'
            f'Installed-Size: {(size + 1023) // 1024}\n'
            'Depends: python3 (>= 3.12), python3 (<< 3.13), python3-gi, python3-gi-cairo, '
            'python3-cairo, gir1.2-gtk-3.0, libc6 (>= 2.28), libstdc++6\n'
            'Recommends: fonts-noto-cjk\n'
            'Description: Offline PDF reader with night modes and editable bookmarks\n'
            ' Search text, copy selections and add highlights in independent GTK windows.\n'
            ' Built for Ubuntu 24.04 LTS amd64. Python PDF/image dependencies are bundled.\n')
        for name in ('postinst', 'postrm'):
            script = control / name
            script.write_text('#!/bin/sh\nset -e\n'
                'if command -v update-desktop-database >/dev/null 2>&1; then\n'
                '    update-desktop-database /usr/share/applications || true\nfi\n'
                'if command -v gtk-update-icon-cache >/dev/null 2>&1; then\n'
                '    gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true\nfi\n'
                'exit 0\n')
            script.chmod(0o755)
        # dpkg-deb records root ownership without requiring a root build process.
        artifact = dist / f'NightReader_{version}_ubuntu24.04_amd64.deb'
        subprocess.run(['dpkg-deb', '--root-owner-group', '-Zxz', '-z6', '--build',
                        str(stage), str(artifact)], check=True)
    sources = dist / f'NightReader-{version}-dependency-sources.tar.gz'
    with tarfile.open(sources, 'w:gz', compresslevel=1) as archive:
        archive.add(ROOT / 'packaging/dependencies.json', arcname='dependencies.json')
        archive.add(ROOT / 'THIRD_PARTY_NOTICES.md', arcname='THIRD_PARTY_NOTICES.md')
        for package in locked['packages']:
            name = package['source']['filename']
            archive.add(cache / name, arcname=name)
        for source in locked['native_sources']:
            archive.add(cache / source['filename'], arcname=source['filename'])
    (dist / 'SHA256SUMS').write_text(''.join(
        f'{sha256(path)}  {path.name}\n' for path in (artifact, sources)))
    print('Built:', artifact.name, sources.name, 'SHA256SUMS', sep='\n  ')


if __name__ == '__main__':
    main()
