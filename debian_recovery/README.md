# Recovery builder and touchscreen source

This directory preserves the source used by the configured Debian ChromaPad's
rolling backup. The controller at the repository root calls the installed
copy at `/usr/local/lib/chromapad-recovery-builder`. Pulling source changes
does not replace that installed copy or change the current recovery image.

The main components are:

| File | Purpose |
| --- | --- |
| `rolling_recovery.py` | Update the saved filesystem in place with rsync and build the recovery environment. |
| `chromapad_recovery.py` | Validate the matching target, restore the saved system and verify readback. |
| `recovery_ram_launcher.py` | Start recovery with its required tools loaded into RAM. |
| `recovery_ui.py` | Draw the progress and result screens with Python/Pillow directly to the Linux framebuffer. |
| `recovery_shutdown.py` | Read touchscreen taps and request shutdown after verified recovery completion. |
| `build-recovery-config.py` | Assemble guarded boot files, service configuration and the recovery manifest. |
| `recovery_artifacts.py` | Reuse content-pinned artifacts and publish new cache entries only after validation. |
| `recovery_initramfs.py` | Resolve guarded-initramfs inputs and build or reuse the verified artifact. |

The earlier generation builder scripts remain because they supply shared
helpers and preserve compatibility with earlier compressed recovery payloads.
The normal rolling command does not create those older dated releases.

## Private build inputs

Three files are deliberately excluded from Git:

| Local file | Contents |
| --- | --- |
| `emmc-readiness-evidence.json` | Machine readiness evidence and the saved target MBR. |
| `protected-reference.json` | Checksums of the configured pad's protected firmware regions. |
| `assets/usb-prefix.bin` | The raw disk-prefix asset retained for the configured recovery image. |

They remain in the owner's local workspace and verified installed builder.
`tool-manifest.json` retains their exact hashes, alongside the hashes of the
published source files. A fresh clone is therefore source for review and unit
testing, not a complete deployable builder. For a rebuild of this same pad,
use the matching private inputs from the verified installed builder and run
its normal `verify_tools` validation before deployment. Do not substitute
placeholders or remove the checks to make an incomplete clone run.

The source retains explicit device identifiers, partition geometry and NAS
paths for the configured pad. It is not a generic installer for another pad.
Provisioning and validation for a different device are separate work.

## Validation

From the repository root on Linux, with Python 3, Pillow, rsync and DejaVu Sans
installed, run:

```sh
python3 tests/run_unit_tests.py
```

These tests use synthetic data, temporary files and mocked hardware/service
operations. They cover the launcher, printer lifecycle, image ownership,
restore payload checks, touchscreen rendering and shutdown gating. They do
not need the three private build inputs.

Additional explicit tests have different prerequisites and are not run by
that command:

- `test-probe-guard.py` needs the private readiness input; it uses mocked tools
  and regular files to exercise the boot guard.
- `test-recovery-arm-cli.py` needs an existing build stage/release.
- `test-rolling-filesystems.py` needs the private disk prefix, root privileges,
  filesystem tools, loop devices and space under `/var/tmp`. It creates
  disposable image files, checks two updates reuse one image, and validates
  restoration of content, ownership, modes, links and extended attributes.

The installed version passed the native unit suites, file-backed integration,
and a complete verified rolling backup on October 25, 2026. Physical boot and
restore of the new rolling image were not performed. Publishing this source
does not change that validation status.
