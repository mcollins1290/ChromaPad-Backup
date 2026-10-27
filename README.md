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
original updater. The final paused copy and comparison use the same quick
check. Files changed while deliberately preserving both size and timestamps
are not detected by that check. The finished saved filesystem is hashed once;
recovery verifies that hash before writing and checks the restored bytes.
This full checksum still takes time over the NAS connection, even when rsync
has few files to update. The first run also populates the recovery environment;
later runs reuse its unchanged files.
Only previously active printer services are stopped and restarted. Other
application writers pause for the final consistent copy. Printer services
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
Python 3, Pillow, rsync and DejaVu Sans installed. This runs the 102 unit tests
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
Capture details include firmware checks, rsync, the paused final copy,
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

## Sparse checksum experiment

`sudo python3 -B benchmark_payload_hash.py` compares sequential and sparse-aware
hashing of a READY image, mounted read-only. It takes roughly two checksum
passes and does not stop printers or run a backup. Both digests must match
the verified image. Results are saved to
`/var/tmp/chromapad-payload-hash-benchmark.json`.

The October 27 experiment measured 537.4 seconds sequential versus 524.6
seconds sparse-aware (2.4% faster), with identical SHA-256 values. CPU time
fell from 42.4 to 32.6 seconds. Sparse-aware hashing generated 5.58 GB of hole
bytes locally, but Linux already handles holes efficiently during normal
reads; the remaining time is dominated by reading stored data. This small
single-pair difference does not establish a reliable speedup, particularly
because outer/NAS caches can remain warm between passes. Recorded evidence is
in `benchmarks/sparse-hash-2026-10-27.json`.

Sparse hashing remains opt-in (`hash_payload(..., sparse=True)`) and is not
selected by normal backups. No installed builder was changed for this
experiment. The restore format and full SHA-256 verification remain intact.
