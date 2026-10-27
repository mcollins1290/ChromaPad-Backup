import hashlib,json,os,signal,subprocess,sys,tempfile,unittest
from pathlib import Path
import chromapad_recovery_backup as workflow

class WorkflowTests(unittest.TestCase):
    def test_changed_tool_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'tool.py').write_text('original')
            manifest={'kind':'chromapad-recovery-builder-tools','files':{'tool.py':hashlib.sha256(b'original').hexdigest()}}
            (root/'tool-manifest.json').write_text(json.dumps(manifest))
            workflow.verify_tools(root)
            (root/'tool.py').write_text('changed')
            with self.assertRaisesRegex(RuntimeError,'Builder file changed'):workflow.verify_tools(root)

    def test_tool_path_escape_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'tool-manifest.json').write_text(json.dumps({'kind':'chromapad-recovery-builder-tools','files':{'../outside':'0'*64}}))
            with self.assertRaisesRegex(RuntimeError,'Unsafe builder file'):workflow.verify_tools(root)

    def test_sigterm_runs_cleanup(self):
        with tempfile.TemporaryDirectory() as d:
            marker=Path(d)/'cleanup'
            # Keep the readiness handshake inside try/finally, and use a
            # bounded wait so a signal arriving just before the wait cannot
            # strand the child in signal.pause().
            script="import sys,signal,time;from pathlib import Path;import recovery_build_common as c;c.install_signal_handlers()\ntry:\n print('ready',flush=True)\n while True:time.sleep(0.05)\nfinally:Path(sys.argv[1]).write_text('cleanup ran')\n"
            child=subprocess.Popen([sys.executable,'-c',script,str(marker)],cwd=Path(__file__).parent,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(),'ready')
                child.send_signal(signal.SIGTERM)
                child.communicate(timeout=10)
                self.assertNotEqual(child.returncode,0)
                self.assertEqual(marker.read_text(),'cleanup ran')
            finally:
                if child.poll() is None:child.kill();child.wait()

if __name__=='__main__':unittest.main(verbosity=2)
