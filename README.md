# ChromaPad rolling Debian backup

These files are the pad-side launcher and backup controller for the configured
Debian ChromaPad. Recovery-builder and touchscreen sources are maintained in
[`debian_recovery/`](debian_recovery/README.md). The running controller uses
the separately installed recovery builder at
`/usr/local/lib/chromapad-recovery-builder` and the installed controller/service.
Cloning this repository alone does not install those components. The NAS paths
and device checks are specific to this configured pad.

Run the familiar command on the pad. Windows is not needed:

```sh
sudo /root/Git/ChromaPad-Backup/update_factory_recovery_img.sh
```

Finish printing and turn off heater targets first. The command checks printer
readiness, starts a background service, and opens its live logs. Ctrl+C closes
the viewer; the backup continues. Use `--logs` to reconnect, `--status` for
service status, or `--plan` to check readiness without starting a backup.

The first run creates `/media/raid1/coprint/coprint-debian.img` and selects it
through `coprint.img` only after verification. The existing dated stock image
is preserved. Subsequent runs update the same Debian image in place. There are
no dated Debian images or separate full source backups.

The image contains a sparse filesystem holding the saved Debian system and a
smaller recovery environment. Updates preserve software, printer settings and
user files, excluding transient files, mounted drives and routine logs.
Rsync uses file size and modification time to skip unchanged files, like the
original updater. One copy runs with application writers paused, followed
by a full comparison using the same quick check. Files changed while deliberately preserving both size and timestamps
are not detected by that check. The finished saved filesystem is hashed once;
recovery verifies that hash before writing and checks the restored bytes.
The checksum runs locally on the NAS after the pad fully unmounts the image,
avoiding a full payload read back over the network. The first run also populates the recovery environment;
later runs reuse its unchanged files.
Only previously active printer services are stopped and restarted. Other
application writers pause for the consistent copy and comparison. Printer services
return before offline recovery packaging. Rehome before printing afterward.

The image is incomplete during an update. An interruption can invalidate it;
check the job result before copying it to USB. No previous Debian generation
is retained for rollback. The stock fallback is not modified.

Success requires `BACKUP_COMPLETE_PRINTERS_RESTORED` in the log and:

```sh
sudo systemctl show chromapad-backup.service -p ActiveState -p Result -p ExecMainStatus
```

These must show `ActiveState=inactive`, `Result=success` and `ExecMainStatus=0`.
The compact current
record is `/media/raid1/coprint/coprint-debian.json`; successful state is `READY`.
The latest bounded diagnostic reports are in `coprint/backup-info`.

The image is about 24.50 GiB logically. NAS space checks reserve only its
possible remaining allocation plus 512 MiB, rather than a fixed 45 GiB.
The first image therefore needs about 25 GiB free on the NAS. Local disposable
boot/runtime work requires 1.5 GiB free on the pad. The image also needs room
internally for the saved system, recovery environment, and recovery logs.
Temporary work is cleaned after success or a handled failure; work with a
surviving mount is retained for inspection rather than removed unsafely.

Write the completed `coprint.img` to USB with an image writer. Booting that USB
automatically restores the matching pad. The touchscreen shows progress and
offers shutdown only after successful verification. Shut down, remove USB,
then cold-boot eMMC. Backup does not flash a USB or perform a physical restore.

The implementation keeps target identity checks, firmware protection, source
comparison, filesystem checks, payload checksums, guarded boot, restoration
readback and cleanup. The recovery engine reads both earlier compressed
payloads and the new raw sparse payload. No backup schedule is installed.

The older generation builder remains available for history/rollback, but the
rolling command does not use `coprint_2_debian` or retain its old build products.

For source development, run `python3 tests/run_unit_tests.py` on Linux with
Python 3, Pillow, rsync and DejaVu Sans installed. This runs the unit suites
using temporary files and mocked device/service operations. It does not start
a backup, restore the pad or power it off. Private-input integration tests are
described in the [builder notes](debian_recovery/README.md).

## Backup phase timing

Rolling runs emit `TIMING_START` and `TIMING_END` lines in the service log,
including elapsed seconds and `OK` or `FAILED`. The latest run also saves
`/media/raid1/coprint/backup-info/phase-timings.json`, even after a handled
failure. Timings use a monotonic clock; UTC start times help correlate logs.
Nested phase durations overlap and must not be summed. `backup_work_total`
covers image checks/initialization, capture and recovery finishing, including
mount cleanup; it excludes readiness, report export and final activation.
Capture details include firmware checks, rsync, the paused copy and comparison,
printer stop/capture/restart, trimming, runtime refresh, filesystem checks and
payload hashing. Finish details separate packaging, validation and smoke tests.
Abrupt power loss may prevent report export; timing lines already written to
the journal may still be available. Reports are replaced by each run.

These changes must be installed into both the controller and recovery builder
before the normal launcher emits timings. Updating this checkout alone does
not deploy them. The builder's tool manifest must accompany its changed files.

The recovery data partition is now 24 GiB. USB media must have at least
24.5 GiB actual capacity (normally sold as 32 GB or larger). Existing legacy
12.5 GiB rolling images require the explicit `enlarge_recovery_image.py`
migration after deploying the matching controller/builder. The migration
checks ownership, old partition geometry and filesystem UUID, grows only
the image file and its outer ext4 filesystem, and leaves the image FAILED
until a complete backup succeeds. It retains migration diagnostics under
`/var/tmp/chromapad-image-expansion-*`. It never writes a physical disk.

Recovery refresh omits `/usr/src`, `/usr/share/doc`, `/usr/share/man` and
contents of `/var/cache` from the recovery environment. Existing duplicates
there are removed on the next successful refresh. The saved Debian payload
still includes these paths, so restoration coverage is unchanged. Runtime
copy reports now include rsync statistics and itemized changes. The first
refresh after this change includes cleanup work; later runs are the useful
comparison for steady-state timings. Payload hashing remains unchanged.

Codex diagnostic databases (`/root/.codex/logs_*.sqlite*`, including SQLite
sidecars) and `/root/.codex/models_cache.json` are excluded as transient logs
and cache. Codex state databases, session history, configuration, skills and
plugins remain included. Close Codex for a consistent copy of that state;
background diagnostic/cache updates alone no longer fail the final comparison.

## NAS-side payload verification

Backup now requires noninteractive root SSH access to `192.168.0.2` from the
pad, with an already trusted host key. Preflight checks SSH, Python 3, the NAS
filesystem tools and absence of a surviving NAS checksum mount before any
image changes or printer stops. SSH failures stop the job; verification is
never skipped or silently weakened.

After capture, runtime refresh and the saved-filesystem check, the pad syncs
and unmounts all image filesystems and detaches its loops. It then sends the
pinned `nas_payload_hash.py` helper over SSH. In a private mount namespace,
the NAS checks the image identity, partition geometry and filesystem UUIDs,
mounts only the outer recovery filesystem read-only without journal replay,
and hashes every byte of the saved payload with SHA-256. It returns the result
only after unmounting and detaching its loop. Packaging then resumes on the pad.
The recovery format, pre-restore checksum and restored-byte readback are unchanged.

The NAS requires `python3`, `unshare`, `losetup`, `blkid`, `mount` and `umount`.
No helper installation on the NAS is needed. Its private mount and local lock
isolate checksum jobs. The pad retains `nas-payload-hash.txt` and
`payload-hash.json` in `backup-info`; `payload_sha256` includes SSH and remote
cleanup time. An SSH timeout or cleanup failure fails the backup; inspect the
NAS loop attachment before retrying if preflight reports it is still busy.
The full helper operation has a 30-minute timeout.

A read-only benchmark on the same verified image took about 3m40s including
SSH, versus 8m57s when hashed on the pad, with identical checksums. A complete
backup with this integration is still needed to measure total runtime.

## Single consistent system copy

Routine backup performs one system rsync with application writers paused,
then a full-tree dry-run comparison. The preliminary live copy was removed:
it scanned the whole tree even for tiny updates and printers were already
stopped during it. No source paths or metadata checks were dropped. Boot
checks, source identity checks, final sync, NAS payload hashing and restore
verification are unchanged.

This avoids one scan (about 84 seconds in the preceding full backup), but
actual savings depend on changed data and cache state. Application writers
now remain paused for the entire copy; a large update or first full capture
may pause them longer than the old two-copy approach. Only previously active
services are restarted, and printers still return before offline packaging.

New nested timings separate `consistent_rsync`, `boot_files_copy`,
`root_comparison`, `boot_validation_and_inputs` and `payload_sync`.
`application_pause_and_resume` includes service stop/start; durations overlap
and must not be summed. `rsync-final.txt` holds the sole copy report, and the
obsolete `rsync-initial.txt` report is removed on export.

## Verified recovery artifact reuse

Rolling backups can reuse the guarded initramfs and RAM tool archive. Input
fingerprints cover file contents, permissions/ownership, symlink targets,
builder code and the complete selected dependency set. RAM packaging resolves
its dependency graph again on every lookup, including new Python/Pillow files
and changes in library search precedence. Initramfs inputs include the raw
initrd, generated guard, target-tool loader resolution, tool/library contents,
initramfs hooks and package state. Re-created input timestamps alone do not
invalidate a content-equivalent artifact.

Artifacts occupy two fixed slots under the root-private disposable directory
`/var/tmp/chromapad-recovery-artifacts`, excluded from the saved system. A cache
hit verifies the artifact's SHA-256 and the copy used by the build. Missing,
changed or corrupt entries rebuild normally. New artifacts are published only
after image validation and the launcher smoke test succeed. Cache publication
errors are diagnostic and do not invalidate an otherwise verified backup.
Deleting this cache simply forces rebuilding.

The first successful backup populates the cache; the following unchanged run
can benefit. `/media/raid1/coprint/backup-info/artifact-cache.json` reports
`BUILT` or `HIT` and per-artifact seconds including input checks. New boot
arguments, manifests and restore authorization are still generated each run;
final image validation, toolchain/display checks, launcher smoke, the full
payload checksum and restore readback remain enabled.

A read-only experiment verified fresh artifacts against the last successful
image by comparing their contents, permissions, ownership and symlinks. A
second lookup in an isolated cache reused byte-identical artifacts: combined
artifact work fell from 60.7s to 15.1s. This is a component benchmark with warm
filesystem caches, not a measured full-backup saving. Normal backups still
need measurement after the production cache has been populated.
