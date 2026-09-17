#!/usr/bin/env python3
"""Replay the supported llama.cpp patch set without changing Git metadata."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import os

ROOT = Path(__file__).resolve().parents[1]
LLAMA = Path(os.environ.get('KVMEM_LLAMA_DIR', ROOT / 'llama.cpp')).resolve()
PATCHES = [
    ROOT / 'patches' / 'llama-kvmem-current.patch',
    ROOT / 'patches' / 'reasoning-budget-upgrade.patch',
    ROOT / 'patches' / 'replayssm-upgrade.patch',
    ROOT / 'patches' / 'multimodal-upgrade.patch',
]


def git(*args, check=True, cwd=LLAMA, **kwargs):
    return subprocess.run(['git', *args], cwd=cwd, check=check,
                          capture_output=True, text=True, **kwargs)


def can_upgrade(upgrade):
    if git('apply', '--check', str(upgrade), check=False).returncode != 0:
        return False
    with tempfile.TemporaryDirectory(prefix='kvmem-patch-check-') as temp:
        check_dir = Path(temp)
        numstat = git('apply', '--numstat', str(PATCHES[0])).stdout.splitlines()
        for row in numstat:
            fields = row.split('\t', 2)
            if len(fields) != 3:
                continue
            source = LLAMA / fields[2]
            if source.is_file():
                target = check_dir / fields[2]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        if subprocess.run(['git', 'apply', str(upgrade)], cwd=check_dir,
                          capture_output=True).returncode:
            return False
        return subprocess.run(['git', 'apply', '--reverse', '--check', str(PATCHES[0])],
                              cwd=check_dir, capture_output=True).returncode == 0


def main():
    if not LLAMA.is_dir():
        raise SystemExit(f'missing llama.cpp checkout: {LLAMA}')
    current, *upgrades = PATCHES
    if git('apply', '--reverse', '--check', str(current), check=False).returncode == 0:
        print('KVMem patches already applied')
        return
    if git('apply', '--check', str(current), check=False).returncode == 0:
        git('apply', str(current))
        print('applied current KVMem patch to pinned llama.cpp')
        return
    for upgrade in upgrades:
        if can_upgrade(upgrade):
            git('apply', str(upgrade))
            print(f'upgraded existing KVMem tree with {upgrade.stem.replace("-", " ")}')
            return
    raise SystemExit('llama.cpp differs from the supported pin or KVMem baseline; no files changed')


if __name__ == '__main__':
    main()
