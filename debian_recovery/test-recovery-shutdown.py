import json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import recovery_shutdown as s

class ShutdownTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.directory=Path(temporary.name)
        log=patch.object(s,'LOG',self.directory/'completion-events.jsonl');log.start();self.addCleanup(log.stop)
    def test_checkpoint_is_persistent_json_and_synced(self):
        with patch.object(s.os,'fsync',wraps=os.fsync) as sync:
            s.checkpoint('starting');s.checkpoint('button_ready',touch_slots=5)
        events=[json.loads(line) for line in s.LOG.read_text().splitlines()]
        self.assertEqual([x['event'] for x in events],['starting','button_ready'])
        self.assertEqual(events[1]['touch_slots'],5)
        self.assertEqual(sync.call_count,4)
        self.assertEqual(s.LOG.stat().st_mode&0o777,0o600)
    def test_startup_failure_has_durable_traceback_and_is_reraised(self):
        with patch.object(s,'main',side_effect=RuntimeError('touch device unavailable')):
            with self.assertRaisesRegex(RuntimeError,'touch device unavailable'):s.run_logged()
        event=json.loads(s.LOG.read_text())
        self.assertEqual(event['event'],'failed')
        self.assertIn('touch device unavailable',event['traceback'])
    def test_diagnostic_failure_does_not_prevent_completion(self):
        with patch.object(s,'LOG',self.directory/'missing'/'events'),patch('builtins.print') as output:
            s.checkpoint('starting')
        output.assert_called_once()
    def test_diagnostic_file_does_not_follow_symlinks(self):
        target=self.directory/'target';target.write_text('original')
        s.LOG.symlink_to(target)
        with patch('builtins.print'):s.checkpoint('starting')
        self.assertEqual(target.read_text(),'original')
    def touch(self,held=False):return s.TouchGesture([dict(id=9 if held else -1,x=500,y=370),dict(id=-1,x=100,y=100)])
    def frame(self,t,events):
        for code,value in events:self.assertIsNone(t.feed(3,code,value))
        return t.feed(0,0,0)
    def test_single_press_release(self):
        t=self.touch();self.assertIsNone(self.frame(t,[(0x39,1)]));self.assertTrue(t.pressed)
        self.assertEqual(self.frame(t,[(0x39,-1)]),'tap');self.assertIsNone(self.frame(t,[]))
    def test_existing_held_touch_does_not_shutdown(self):
        t=self.touch(True);self.assertIsNone(self.frame(t,[(0x39,-1)]))
        self.frame(t,[(0x39,2)]);self.assertEqual(self.frame(t,[(0x39,-1)]),'tap')
    def test_drag_out_and_back_is_cancelled(self):
        t=self.touch();self.frame(t,[(0x39,1)])
        self.frame(t,[(0x35,100)]);self.frame(t,[(0x35,500)])
        self.assertIsNone(self.frame(t,[(0x39,-1)]))
    def test_release_frame_outside_is_cancelled(self):
        t=self.touch();self.frame(t,[(0x39,1)])
        self.assertIsNone(self.frame(t,[(0x35,100),(0x39,-1)]))
    def test_multiple_fingers_cancel_gesture(self):
        t=self.touch();self.frame(t,[(0x39,1)])
        self.frame(t,[(0x2f,1),(0x39,2)]);self.frame(t,[(0x39,-1)])
        self.assertIsNone(self.frame(t,[(0x2f,0),(0x39,-1)]))
    def test_drop_and_invalid_slot_request_resync(self):
        t=self.touch();self.frame(t,[(0x39,1)])
        self.assertEqual(t.feed(0,3,0),'resync')
        self.assertEqual(t.feed(3,0x2f,99),'resync')
    def test_failed_or_incomplete_restore_refused(self):
        manifest={'root_payload':{'raw_sha256':'raw'}}
        valid={'status':'RESTORE_PASS','fixture':False,'manifest_sha256':'pin','root_partition_sha256':'raw','protected_regions_unchanged':True,'filesystems_clean':True,'boot_entry_verified':True,'cleanup_complete':True}
        s.verified_result(manifest,valid,'pin')
        for key,value in [('status','FAILED'),('fixture',True),('manifest_sha256','other'),('root_partition_sha256','wrong'),('cleanup_complete',False),('filesystems_clean',False),('boot_entry_verified',False),('protected_regions_unchanged',False)]:
            with self.subTest(key=key),self.assertRaises(RuntimeError):s.verified_result(manifest,{**valid,key:value},'pin')
    def test_active_restore_refused_before_any_poweroff(self):
        with patch.object(s,'run',return_value='MainPID=599\nResult=success\nSubState=start'),patch.object(s.subprocess,'run') as command:
            with self.assertRaises(RuntimeError):s.poweroff()
            command.assert_not_called()
    def test_poweroff_rechecks_completion_and_is_not_forced(self):
        with patch.object(s,'completed_recovery') as gate,patch.object(s.subprocess,'run') as command:
            s.poweroff();gate.assert_called_once();command.assert_called_once_with(['systemctl','poweroff'],check=True,timeout=20)
    def test_all_button_states_render(self):
        for args in ({},{'pressed':True},{'shutting_down':True},{'error':'test'}):self.assertEqual(s.render(**args).size,(1024,600))

if __name__=='__main__':unittest.main(verbosity=2)
