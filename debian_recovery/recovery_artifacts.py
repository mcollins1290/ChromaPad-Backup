"""Two bounded, disposable local artifact slots, published only after validation."""
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time

CACHE = Path('/var/tmp/chromapad-recovery-artifacts')
KINDS = {'initramfs', 'runtime'}


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(4 * 1024 ** 2):
            value.update(block)
    return value.hexdigest()


def file_record(path):
    path = Path(path)
    info = path.lstat()
    record = {'mode': info.st_mode, 'uid': info.st_uid, 'gid': info.st_gid}
    if stat.S_ISLNK(info.st_mode):
        record['link'] = os.readlink(path)
    elif stat.S_ISREG(info.st_mode):
        record.update(bytes=info.st_size, sha256=digest(path))
    else:
        raise RuntimeError('Not an artifact input file: ' + str(path))
    return record


def root_records(root, names):
    """Fingerprint selected files, resolving absolute links inside the chroot."""
    root = Path(root).resolve()
    names = sorted(set(str(name).lstrip('/') for name in names))
    records = {}
    for name in names:
        pending, current, links = deque(Path(name).parts), root, 0
        while pending:
            part = pending.popleft()
            if part == '..':
                current = current.parent
            elif part != '.':
                current = current / part
            if not current.is_relative_to(root):
                raise RuntimeError('Artifact input escapes recovery root')
            if current.is_symlink():
                links += 1
                if links > 40:
                    raise RuntimeError('Artifact input symlink loop')
                key = str(current.relative_to(root))
                records[key] = file_record(current)
                target = os.readlink(current)
                current = root if target.startswith('/') else current.parent
                pending.extendleft(reversed(Path(target.lstrip('/')).parts))
        key = str(current.relative_to(root))
        if key not in records:
            records[key] = file_record(current)
    return {'selected': names, 'files': records}


def valid_metadata(kind, metadata):
    if not isinstance(metadata, dict):
        return False
    if kind == 'initramfs':
        return metadata.get('status') == 'PASS' and metadata.get('repack_inventory_identical') is True
    fields = ('uncompressed_file_bytes', 'elf_files', 'files')
    return (all(type(metadata.get(name)) is int and metadata[name] >= 0 for name in fields)
            and metadata['uncompressed_file_bytes'] <= 350 * 1024 ** 2)


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise RuntimeError('Unsafe artifact cache directory: ' + str(path))


def atomic_file(target, source=None, record=None):
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.pending-', delete=False) as stream:
            pending = Path(stream.name)
            if source is not None:
                with Path(source).open('rb') as src:
                    shutil.copyfileobj(src, stream)
            else:
                stream.write((json.dumps(record, sort_keys=True) + '\n').encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, target)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


class ArtifactCache:
    def __init__(self, stage, enabled=True):
        self.stage = Path(stage)
        self.enabled = enabled
        self.pending = []
        self.events = []

    def get(self, kind, inputs, target, build, started=None):
        if kind not in KINDS:
            raise ValueError('Unknown artifact kind')
        started = time.monotonic() if started is None else started
        key = hashlib.sha256(json.dumps({'implementation': digest(__file__), 'inputs': inputs}, sort_keys=True).encode()).hexdigest()
        hit, reason, metadata = False, 'disabled', None
        slot = CACHE / kind
        if self.enabled:
            try:
                private_directory(CACHE)
                private_directory(slot)
                receipt = json.loads((slot / 'receipt.json').read_text())
                artifact = slot / 'artifact.bin'
                info = artifact.lstat()
                if (receipt['schema'] == 1 and receipt['key'] == key
                        and valid_metadata(kind, receipt['metadata'])
                        and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                        and digest(artifact) == receipt['sha256']):
                    shutil.copyfile(artifact, target)
                    if digest(target) == receipt['sha256']:
                        hit, metadata = True, receipt['metadata']
                reason = 'input or artifact mismatch'
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                reason = type(exc).__name__
        if not hit:
            Path(target).unlink(missing_ok=True)
            metadata = build()
            if self.enabled:
                self.pending.append({'kind': kind, 'source': str(Path(target).relative_to(self.stage)),
                                     'schema': 1, 'key': key, 'sha256': digest(target), 'metadata': metadata})
                (self.stage / 'artifact-cache-pending.json').write_text(json.dumps(self.pending))
        self.events.append({'artifact': kind, 'status': 'HIT' if hit else 'BUILT',
                            'reason': None if hit else reason,
                            'seconds': round(time.monotonic() - started, 3)})
        (self.stage / 'reports/artifact-cache.json').write_text(json.dumps({'artifacts': self.events}, indent=2) + '\n')
        print('ARTIFACT_CACHE ' + kind + ' ' + self.events[-1]['status'], flush=True)
        return metadata


def publish_artifacts(stage):
    """Called only after image validation and launcher smoke testing succeed."""
    stage = Path(stage)
    pending = stage / 'artifact-cache-pending.json'
    if not pending.exists():
        return
    try:
        private_directory(CACHE)
        for item in json.loads(pending.read_text()):
            if item['kind'] not in KINDS:
                raise RuntimeError('Invalid pending artifact kind')
            source = stage / item['source']
            if not source.resolve().is_relative_to(stage.resolve()) or source.is_symlink():
                raise RuntimeError('Unsafe pending artifact path')
            if digest(source) != item['sha256']:
                raise RuntimeError('Pending artifact changed after validation')
            slot = CACHE / item['kind']
            private_directory(slot)
            atomic_file(slot / 'artifact.bin', source=source)
            atomic_file(slot / 'receipt.json', record={k: v for k, v in item.items() if k not in ('source', 'kind')})
        print('ARTIFACT_CACHE published validated artifacts', flush=True)
    except Exception as exc:
        # A disposable cache must not turn a verified backup into a failed one.
        print('ARTIFACT_CACHE publication skipped: ' + str(exc), flush=True)
