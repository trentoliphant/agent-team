#!/usr/bin/env python3
"""Install an immutable preview beside stable Agent Team, with isolated state."""
import argparse
from pathlib import Path
import re
import shlex
import subprocess
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ref', required=True, help='Full reviewed GitHub commit SHA')
    parser.add_argument('--prefix', type=Path, default=Path.home()/'.local/share/agent-team-preview')
    parser.add_argument('--bin-dir', type=Path, default=Path.home()/'.local/bin')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{40}', args.ref):
        parser.error('--ref must be a full lowercase commit SHA')
    prefix, bin_dir = args.prefix.expanduser().resolve(), args.bin_dir.expanduser().resolve()
    command = bin_dir/'agent-team-preview'
    marker = '# Installed by Agent Team preview installer'
    if command.exists() or command.is_symlink():
        if command.is_symlink() or marker not in command.read_text():
            parser.error(f'Refusing to replace an unrelated command: {command}')
    environment = prefix/'venv'
    venv.EnvBuilder(with_pip=True).create(environment)
    subprocess.run([str(environment/'bin/python'), '-m', 'pip', 'install', '--upgrade',
                    f'git+https://github.com/trentoliphant/agent-team.git@{args.ref}'], check=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    # Quote paths as shell code; the wrapper pins its state regardless of the caller's directory.
    command.write_text('#!/bin/sh\n' + marker + '\nexec ' + shlex.quote(str(environment/'bin/agent-team'))
                       + ' --home ' + shlex.quote(str(prefix/'state')) + ' "$@"\n')
    command.chmod(0o755)
    print(f'Installed commit {args.ref}\nCommand: {command}\nState: {prefix / "state"}')
    print('Register each target repository in only one state home; do not run stable and preview on the same repository.')


if __name__ == '__main__':
    main()
