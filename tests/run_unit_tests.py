#!/usr/bin/env python3
"""Run the existing unit suites without private build inputs or device access."""
import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    suites = {
        root: (
            'test_promote_backup.py', 'test_printer_lifecycle.py',
            'test_launcher.py', 'test_rolling_backup.py',
        ),
        root / 'debian_recovery': (
            'test_debian_backup.py', 'test-recovery-engine.py',
            'test-recovery-ui.py', 'test-recovery-shutdown.py',
            'test-workflow-control.py', 'test-rolling-recovery.py', 'test-nas-payload-hash.py',
        ),
    }
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    for folder, names in suites.items():
        for name in names:
            print('\nRunning ' + str((folder / name).relative_to(root)), flush=True)
            result = subprocess.run([sys.executable, '-B', name], cwd=folder, env=env)
            if result.returncode:
                return result.returncode
    print('\nAll unit suites passed.', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
