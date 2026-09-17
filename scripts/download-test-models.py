#!/usr/bin/env python3
"""Download the documented test models from ModelScope on any OS."""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS = (
    ('ci', 'unsloth/Qwen3-0.6B-GGUF', 'Qwen3-0.6B-Q8_0.gguf'),
    ('ci', 'unsloth/Qwen3.5-0.8B-GGUF', 'Qwen3.5-0.8B-Q8_0.gguf'),
    ('mtp', 'unsloth/Qwen3.5-0.8B-MTP-GGUF', 'Qwen3.5-0.8B-Q8_0.gguf'),
    ('all', 'unsloth/Qwen3-1.7B-GGUF', 'Qwen3-1.7B-Q4_K_M.gguf'),
    ('all', 'unsloth/Qwen3-4B-GGUF', 'Qwen3-4B-Q4_K_M.gguf'),
    ('all', 'unsloth/Qwen3.5-4B-GGUF', 'Qwen3.5-4B-Q4_K_M.gguf'),
    ('all', 'unsloth/Qwen3.8-27B-GGUF', 'Qwen3.8-27B-UD-Q4_K_M.gguf'),
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=('ci', 'mtp', 'all'), nargs='?', default='ci')
    parser.add_argument('--output', type=Path, default=ROOT / 'models')
    args = parser.parse_args()
    try:
        from modelscope import snapshot_download
    except ImportError as exc:
        raise SystemExit('missing modelscope; install it in the active Python environment') from exc
    for minimum, repo, filename in MODELS:
        if minimum == 'mtp' and args.stage == 'ci':
            continue
        if minimum == 'all' and args.stage != 'all':
            continue
        destination = args.output / repo
        print(f'==> ModelScope {repo} / {filename}', flush=True)
        snapshot_download(repo, allow_patterns=[filename], local_dir=str(destination))
        print(f'ok {destination / filename}', flush=True)


if __name__ == '__main__':
    main()
