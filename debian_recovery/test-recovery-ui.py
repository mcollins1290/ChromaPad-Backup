import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import recovery_ui as ui
import chromapad_recovery as engine

class DisplayTests(unittest.TestCase):
    def test_all_screens_render_without_devices(self):
        with tempfile.TemporaryDirectory() as d:
            ui.preview(d)
            from PIL import Image
            for p in Path(d).glob('*.png'):
                with Image.open(p) as image:self.assertEqual(image.size,(1024,600))
            self.assertEqual(len(list(Path(d).glob('*.png'))),5)

    def test_missing_framebuffer_does_not_block_progress(self):
        with tempfile.TemporaryDirectory() as d, patch.object(ui,'STATE',Path(d)/'progress.json'), patch.object(ui,'Framebuffer',side_effect=OSError('no display')):
            screen=ui.RecoveryScreen()
            screen.update('Restoring Debian',4,50,100,'Copying')
            screen.finish()
            state=json.loads(ui.STATE.read_text())
            self.assertEqual(state['done'],50)
            self.assertEqual(state['status'],'working')

    def test_framebuffer_write_failure_does_not_block_progress(self):
        class Broken:
            def draw(self,image):raise OSError('display unplugged')
            def close(self):pass
        with tempfile.TemporaryDirectory() as d, patch.object(ui,'STATE',Path(d)/'progress.json'), patch.object(ui,'Framebuffer',return_value=Broken()):
            screen=ui.RecoveryScreen()
            screen.update('Final checks',6,detail='Checking firmware')
            screen.finish()
            self.assertEqual(json.loads(ui.STATE.read_text())['status'],'working')

    def test_full_byte_progress_is_not_completion(self):
        screen=ui.RecoveryScreen(enabled=False)
        screen.update('Restoring Debian',4,100,100)
        self.assertEqual(screen.state['status'],'working')
        screen.update('Recovery complete',status='complete')
        self.assertEqual(screen.state['status'],'complete')

    def test_hash_progress_has_actual_byte_counts(self):
        with tempfile.TemporaryDirectory() as d:
            file=Path(d)/'data';file.write_bytes(b'payload'*100)
            screen=ui.RecoveryScreen(enabled=False)
            with patch.object(engine,'DISPLAY',screen):engine.region_hash(file,0,700,progress=True)
            self.assertEqual((screen.state['step'],screen.state['done'],screen.state['total']),(5,700,700))

if __name__=='__main__':unittest.main(verbosity=2)
