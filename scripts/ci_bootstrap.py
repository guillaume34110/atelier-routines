"""Fetch only pinned development tools, verify SHA256, extract only their binary."""
import argparse
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import platform
import tarfile
import urllib.request

RTK = {
    ('Darwin', 'arm64'): ('aarch64-apple-darwin', '1623590dd8d65374f61589c599c764c08ba9ce993ebe6b601312a89a3c874558'),
    ('Darwin', 'x86_64'): ('x86_64-apple-darwin', '559156238dd3e148c84c88c3a62a7594b874f68793208994e1b02e77a9d9abf9'),
    ('Linux', 'aarch64'): ('aarch64-unknown-linux-gnu', '57a4fac13d18da77f0577ea8b68def7934b934bf76d0a6e161f0bfd06b1e1078'),
    ('Linux', 'x86_64'): ('x86_64-unknown-linux-musl', '4dec94a64035e08189ae82fd5408fe4ae9ae27a665fc07e3e3ec3c4c2d8e8a3e'),
}
GITLEAKS = {
    ('Darwin', 'arm64'): ('darwin_arm64', 'b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5'),
    ('Darwin', 'x86_64'): ('darwin_x64', 'dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709'),
    ('Linux', 'aarch64'): ('linux_arm64', 'e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080'),
    ('Linux', 'x86_64'): ('linux_x64', '551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb'),
}
RG = {
    ('Darwin', 'arm64'): ('aarch64-apple-darwin', '378e973289176ca0c6054054ee7f631a065874a352bf43f0fa60ef079b6ba715'),
    ('Darwin', 'x86_64'): ('x86_64-apple-darwin', '64811cb24e77cac3057d6c40b63ac9becf9082eedd54ca411b475b755d334882'),
    ('Linux', 'aarch64'): ('aarch64-unknown-linux-gnu', '2b661c6ef508e902f388e9098d9c4c5aca72c87b55922d94abdba830b4dc885e'),
    ('Linux', 'x86_64'): ('x86_64-unknown-linux-musl', '1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599'),
}


def install(tool, destination):
    key = (platform.system(), platform.machine())
    tag, expected = {'rtk': RTK, 'rg': RG, 'gitleaks': GITLEAKS}[tool][key]
    url = {'rtk': f'https://github.com/rtk-ai/rtk/releases/download/v1.1.0/rtk-oss-{tag}.tar.gz',
           'rg': f'https://github.com/BurntSushi/ripgrep/releases/download/15.1.0/ripgrep-15.1.0-{tag}.tar.gz',
           'gitleaks': f'https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_{tag}.tar.gz'}[tool]
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError('Development tool checksum mismatch.')
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = [m for m in archive.getmembers() if PurePosixPath(m.name).name in (tool, tool + '-oss')
                   and m.isfile() and m.size <= 50 * 1024 * 1024]
        if len(members) != 1:
            raise RuntimeError('Unexpected development archive.')
        with (destination / tool).open('xb') as target:
            target.write(archive.extractfile(members[0]).read())
    (destination / tool).chmod(0o700)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tool', choices=('rtk', 'rg', 'gitleaks', 'both', 'verify'), default='both')
    args = parser.parse_args()
    base = os.environ.get('TASK_WORKSPACE') or os.environ.get('RUNNER_TEMP')
    if not base:
        raise SystemExit('Run inside the task guard or GitHub Actions.')
    destination = Path(base) / 'routines-tools'
    destination.mkdir(mode=0o700, exist_ok=False)
    for tool in {'both': ('rtk', 'gitleaks'), 'verify': ('rtk', 'rg')}.get(args.tool, (args.tool,)):
        install(tool, destination)
    if os.environ.get('GITHUB_PATH'):
        with open(os.environ['GITHUB_PATH'], 'a') as paths:
            paths.write(str(destination) + '\n')
    print(str(destination))
