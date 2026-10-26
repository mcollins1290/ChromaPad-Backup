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

The image is about 12.50 GiB logically. NAS space checks reserve only its
possible remaining allocation plus 512 MiB, rather than a fixed 45 GiB.
The first image therefore needs about 13 GiB free on the NAS. Local disposable
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
