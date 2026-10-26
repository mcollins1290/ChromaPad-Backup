import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('update_factory_recovery_img.sh')


class LauncherTests(unittest.TestCase):
    def invoke(self, active, *args):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            log = root / 'calls'
            fake = root / 'systemctl'
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n'
                            'if [ "$1" = is-active ]; then\n'
                            '  [ "$ACTIVE" = yes ] && [ "$3" = klipper-1.service ] && exit 0\n'
                            '  exit 3\nfi\nexit 0\n')
            fake.chmod(0o755)
            viewer = root / 'journalctl'
            viewer.write_text('#!/bin/sh\nprintf "journalctl %s\\n" "$*" >> "$CALLS"\n')
            viewer.chmod(0o755)
            (root / 'id').write_text('#!/bin/sh\necho 0\n')
            (root / 'id').chmod(0o755)
            worker = root / 'check.py'
            worker.write_text('import os,sys\nif os.environ["ACTIVE"]=="printing":\n print("Printer is printing",file=sys.stderr);sys.exit(1)\n')
            script = root / 'entry.sh'
            script.write_text(SCRIPT.read_text().replace('/usr/local/lib/chromapad-backup-promoter/rolling_backup.py', str(worker)))
            env = dict(os.environ, PATH=d + ':' + os.environ['PATH'], CALLS=str(log), ACTIVE=active)
            result = subprocess.run(['sh', str(script), *args], capture_output=True, text=True, env=env)
            return result, log.read_text() if log.exists() else ''

    def test_busy_printer_prevents_service_start(self):
        result, calls = self.invoke('printing')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('printing', result.stderr)
        self.assertNotIn('start --no-block', calls)

    def test_idle_running_printer_can_start_background_backup(self):
        result, calls = self.invoke('yes')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls.splitlines(), ['start --no-block chromapad-backup.service',
                                            'journalctl -fu chromapad-backup.service'])
        self.assertIn('background', result.stdout)

    def test_logs_only_does_not_start_backup(self):
        result, calls = self.invoke('yes', '--logs')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls.splitlines(), ['journalctl -fu chromapad-backup.service'])

    def test_invalid_argument_does_not_start_anything(self):
        result, calls = self.invoke('no', '--unknown')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, '')


if __name__ == '__main__':
    unittest.main(verbosity=2)
