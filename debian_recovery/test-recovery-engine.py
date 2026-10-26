import gzip,hashlib,json,struct,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
import chromapad_recovery as r

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.m={'build_id':'debian-recovery-20261024T185713Z'};self.pin='a'*64
    def tearDown(self):self.tmp.cleanup()
    def arm(self):return {'schema':1,'action':'restore-emmc','cid':r.CID,'build_id':self.m['build_id'],'manifest_sha256':self.pin,'confirmation':r.arm_token(self.m,self.pin)}
    def test_exact_arm(self):r.validate_arm(self.arm(),self.m,self.pin)
    def test_wrong_arm(self):
        for key in self.arm():
            value=self.arm();value[key]='wrong'
            with self.subTest(field=key),self.assertRaises(RuntimeError):r.validate_arm(value,self.m,self.pin)
    def test_extra_arm_key(self):
        value=self.arm();value['ignore_checks']=True
        with self.assertRaises(RuntimeError):r.validate_arm(value,self.m,self.pin)
    def test_exclusive_state(self):
        p=self.root/'attempt.json';r.save(p,{'first':True},True)
        with self.assertRaises(FileExistsError):r.save(p,{'second':True},True)
        self.assertEqual(json.loads(p.read_text()),{'first':True})
    def test_state_symlink(self):
        p=self.root/'attempt.json';p.symlink_to(self.root/'target')
        with self.assertRaises(RuntimeError):r.save(p,{})
    def payload(self):
        data=bytearray(8192);struct.pack_into('<I',data,1028,8)
        struct.pack_into('<HH',data,1080,0xef53,1);data[1128:1144]=uuid.UUID(r.ROOT_UUID).bytes
        return data
    def stream(self,data,expected=None):
        compressed=gzip.compress(data)
        (self.root/'root.ext4.gz').write_bytes(compressed)
        m={'root_payload':{'file':'root.ext4.gz','compressed_bytes':len(compressed),'compressed_sha256':hashlib.sha256(compressed).hexdigest(),'raw_sha256':hashlib.sha256(expected if expected is not None else data).hexdigest()}}
        with patch.object(r,'RAW_BYTES',8192):return r.stream_payload(self.root,m)
    def test_valid_payload(self):self.assertEqual(self.stream(self.payload()),hashlib.sha256(self.payload()).hexdigest())
    def test_short_payload(self):
        with self.assertRaises(RuntimeError):self.stream(self.payload()[:-1])
    def test_long_payload(self):
        with self.assertRaises(RuntimeError):self.stream(self.payload()+b'x')
    def test_wrong_payload_hash(self):
        with self.assertRaises(RuntimeError):self.stream(self.payload(),b'wrong')
    def test_dirty_payload(self):
        data=self.payload();struct.pack_into('<H',data,1082,0)
        with self.assertRaises(RuntimeError):self.stream(data)
    def test_wrong_uuid(self):
        data=self.payload();data[1128:1144]=bytes(16)
        with self.assertRaises(RuntimeError):self.stream(data)
    def test_wrong_geometry(self):
        data=self.payload();struct.pack_into('<I',data,1028,7)
        with self.assertRaises(RuntimeError):self.stream(data)
    def test_bad_gzip(self):
        data=bytearray(gzip.compress(self.payload()));data[-8]^=1;(self.root/'root.ext4.gz').write_bytes(data)
        with patch.object(r,'RAW_BYTES',8192),self.assertRaises(gzip.BadGzipFile):r.stream_payload(self.root,{'root_payload':{'file':'root.ext4.gz','compressed_bytes':len(data),'compressed_sha256':'x','raw_sha256':'x'}})
    def test_partial_boot_generation(self):
        m={'boot_generation':'debian-restored-20261024T185713Z','boot_files':{'Image':{'bytes':5,'sha256':'a'*64}}}
        (self.root/'debian'/m['boot_generation']).mkdir(parents=True)
        with self.assertRaises(RuntimeError):r.boot_destination(self.root,self.root,m)
    def test_bad_legacy(self):
        p=self.root/'uInitrd';p.write_bytes(bytes(100))
        with self.assertRaises(RuntimeError):r.legacy(p,3)

if __name__=='__main__':unittest.main(verbosity=2)
