#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# Script Metadata
###############################################################################
SCRIPT_VERSION="v0.0.47"

# Single run timestamp (used for BOTH ophub-release and filename)
RUN_TS="${RUN_TS:-$(date +'%F %H:%M')}"          # e.g. 2026-02-17 12:34
RUN_TS_FN="${RUN_TS// /_}"                       #        2026-02-17_12:34
RUN_TS_FN="${RUN_TS_FN//:/-}"                    #        2026-02-17_12-34

###############################################################################
# Description
# -----------------------------------------------------------------------------
# Updates a factory recovery .img by:
#  - Preserving bootloader and recovery logic
#  - Growing root partition if required (MBR/DOS, root is last)
#  - Resizing ext4 safely
#  - Mirroring root filesystem via rsync
#  - Preserving original image root UUID
#  - Updating fstab to match preserved UUID
#
# v0.0.27 additions:
#  - Enforce filesystem labels: BOOT (vfat) and ROOTFS (ext4)
#  - Ensure /etc/fstab aligns with BOOT label + preserved root UUID
#  - Comment out NFS mount line (192.168.0.2:/media/raid1) to avoid boot issues
#  - Exclude/remove /etc/.writelg from the resulting image
#  - Disable ChromaScreen.service (remove enablement symlinks + systemctl --root disable)
#
# v0.0.28 additions:
#  - Copy uEnv.txt from SRC_BOOT into the image boot partition
#  - Patch APPEND= line in that uEnv.txt:
#     * force root=UUID=<preserved image UUID>
#     * ensure exactly one 'autoflash=301' token, appended at end
#     * preserve the factory superstition spacing: two spaces before autoflash
#
# v0.0.29 additions:
#  - Stronger root sizing policy to avoid living at high utilization:
#     * guarantee MIN_FREE_MIB free after restore
#     * guarantee MIN_FREE_PCT free after restore
#     * round required size up to ROUND_UP_MIB quantum to reduce frequent resizes
#  - Post-rsync sanity warning if free space falls below policy floor
#
# v0.0.31 additions:
#  - After rsync completes, update /etc/ophub-release PACKAGED_DATE='YYYY-MM-DD HH:MM'
#    inside the image root (if the file + line exist), using current system time.
#
# v0.0.34 additions:
#  - Rename the resulting image file to include the same RUN_TS used for PACKAGED_DATE
#    and keep IMG_NAME as a stable anchor symlink pointing at the timestamped file.
#
# v0.0.36 additions:
#  - Preflight now checks for mv(1) + head(1) since the script uses them.
#
# v0.0.37 cleanup:
#  - Replace awk-based numeric compare with bash integer math for NEED_GROW
#  - Make /etc/fstab verification logic clearer (same behavior)
#  - Make uEnv APPEND patch less invasive (avoid global whitespace normalization)
#
# v0.0.38 cleanup:
#  - Restore descriptive comment blocks (no functional changes vs v0.0.37).
#  - Keep PACKAGED_DATE comment version consistent (v0.0.31+).
#
# v0.0.39 additions:
#  - uEnv.txt: if kernel cmdline contains exact token 'console=0', rewrite to 'console=tty0'
#
# v0.0.40 additions:
#  - uEnv.txt quick sanity check: include console verification alongside UUID and autoflash
#
# v0.0.41 fixes:
#  - uEnv.txt quick sanity check updated for dual console directives:
#      * allow console=ttyAML0,115200n8 (leave untouched)
#      * require console=tty0 to exist
#      * require console=0 to be absent
#
# v0.0.43 fix:
#  - VFAT /boot may mount rw with option errors=remount-ro; read-only detection must
#    only trigger on actual ro mount flag.
#
# v0.0.44+ refactor:
#  - Identify boot/root solely from sfdisk --json (no probe loops).
#  - Create persistent offset loops (boot/root) exactly once per table state.
#  - Pre-mount fsck.vfat + post-run fsck.vfat sanity check.
#
# v0.0.46 additions:
#  - Loop RW sanity check after creating each loop (base/boot/root) using losetup RO column.
#
# v0.0.47 change: stricter post-run VFAT sanity check (after sync + brief settle)
#
# IMPORTANT IMPLEMENTATION NOTE:
#  - Partition table is parsed and updated via sfdisk on the base loop device.
#  - Filesystems are mounted and resized using explicit offset-based loop
#    devices (offset + sizelimit).
#  - We do NOT rely on kernel-created /dev/loopXpY partition nodes.
###############################################################################

###############################################################################
# User-configurable variables
###############################################################################
IMG_DIR="/media/raid1/coprint"
IMG_NAME="coprint.img"   # stable anchor name (will become symlink in v0.0.34)

# Where to read source partitions from:
SRC_ROOT="/"
SRC_BOOT="/boot"

# Buffer policy when growing image root partition (legacy layer; still used):
BUFFER_FIXED_MIB=1024     # +1GiB safety buffer
BUFFER_PCT=10             # +10% of used bytes on source root
SECTOR_SIZE=512           # DOS/MBR typically 512

# v0.0.29 capacity policy (stop living on the edge)
MIN_FREE_MIB=1024         # guarantee at least this much free space on / after restore
MIN_FREE_PCT=10           # also guarantee at least this percent free space
ROUND_UP_MIB=512          # round required size up to nearest MiB set here.

# Temporary workspace
WORKDIR_BASE="/tmp/img_update"
KEEP_WORKDIR="0"          # set to 1 for debugging

# Rsync behavior
RSYNC_COMMON=(-aHAX --numeric-ids --one-file-system)
RSYNC_VERBOSITY=(--info=stats2,progress2)

# Debugging (set to 1 to enable verbose diagnostics)
DEBUG="0"

###############################################################################
# Helpers
###############################################################################
die() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "[+] $*" >&2; }
debug() { [[ "${DEBUG}" == "1" ]] && echo "[DEBUG] $*" >&2 || true; }

need_bin() { command -v "$1" >/dev/null 2>&1 || die "Missing required tool: $1"; }

run_dbg_cmd() {
  # run_dbg_cmd "Label" cmd args...
  local label="$1"; shift
  [[ "${DEBUG}" == "1" ]] || return 0
  echo "[DEBUG] ---- ${label} ----" >&2
  ( "$@" ) 2>&1 | sed 's/^/[DEBUG] /' >&2 || true
}

# v0.0.46: sanity-check loop devices are RW (RO=0)
loop_assert_rw() {
  local dev="$1"
  [[ -n "$dev" ]] || die "loop_assert_rw: empty device"
  # RO column is 0 (rw) or 1 (ro)
  local ro
  ro="$(losetup -l -O NAME,RO 2>/dev/null | awk -v d="$dev" '$1==d {print $2; exit}')" || true
  if [[ -z "${ro:-}" ]]; then
    # If losetup output format changes, fail safe
    run_dbg_cmd "losetup -l (NAME,RO)" losetup -l -O NAME,RO
    die "Loop sanity: could not determine RO state for $dev"
  fi
  debug "Loop sanity: $dev RO=$ro"
  if [[ "$ro" != "0" ]]; then
    die "Loop device $dev is read-only (RO=$ro) unexpected"
  fi
}

cleanup() {
  set +e

  # Unmount image mountpoints
  [[ -n "${MNT_BOOT:-}" ]] && mountpoint -q "$MNT_BOOT" && umount "$MNT_BOOT" || true
  [[ -n "${MNT_ROOT:-}" ]] && mountpoint -q "$MNT_ROOT" && umount "$MNT_ROOT" || true
  
  # Detach partition loop devices first
  [[ -n "${LOOP_BOOT:-}" ]] && losetup -d "$LOOP_BOOT" >/dev/null 2>&1 || true
  [[ -n "${LOOP_ROOT:-}" ]] && losetup -d "$LOOP_ROOT" >/dev/null 2>&1 || true

  # Detach base disk loop
  [[ -n "${LOOPDEV:-}" ]] && losetup -d "$LOOPDEV" >/dev/null 2>&1 || true

  # Cleanup workdir
  if [[ "${KEEP_WORKDIR}" != "1" ]]; then
    [[ -n "${WORKDIR:-}" ]] && rm -rf "$WORKDIR" || true
  fi
}
trap cleanup EXIT

###############################################################################
# Integer-safe helpers (NO awk float rounding)
###############################################################################
bytes_from_mib() { echo $(( $1 * 1024 * 1024 )); }
bytes_for_sectors() { local s="$1"; echo $(( s * SECTOR_SIZE )); }
sectors_for_bytes_ceil() { local b="$1"; echo $(( (b + SECTOR_SIZE - 1) / SECTOR_SIZE )); }

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }

round_up_bytes() {
  # round_up_bytes <bytes> <quantum_bytes>
  local bytes="$1"
  local quantum="$2"
  is_uint "$bytes" || die "round_up_bytes: bytes not numeric: $bytes"
  is_uint "$quantum" || die "round_up_bytes: quantum not numeric: $quantum"
  echo $(( ((bytes + quantum - 1) / quantum) * quantum ))
}

pct_total_target_bytes() {
  # pct_total_target_bytes <base_bytes> <pct_free_int>
  # returns smallest T such that free_pct >= pct_free_int
  # free_pct = (T - base)/T >= p/100  => T >= base*100/(100-p)
  local base="$1"
  local pct="$2"
  is_uint "$base" || die "pct_total_target_bytes: base not numeric: $base"
  is_uint "$pct" || die "pct_total_target_bytes: pct not numeric: $pct"
  (( pct >= 0 && pct < 100 )) || die "MIN_FREE_PCT must be 0..99"
  echo $(( (base * 100 + (100 - pct) - 1) / (100 - pct) ))
}

###############################################################################
# Preflight checks
###############################################################################
need_bin losetup
need_bin sfdisk
need_bin truncate
need_bin stat
need_bin awk
need_bin blkid
need_bin e2fsck
need_bin resize2fs
need_bin rsync
need_bin mount
need_bin fsck.vfat
need_bin umount
need_bin findmnt
need_bin du
need_bin grep
need_bin mkdir
need_bin mktemp
need_bin rm
need_bin xargs
need_bin sync
need_bin python3
need_bin sed
need_bin file
need_bin fdisk
need_bin mountpoint
need_bin seq
need_bin fatlabel
need_bin e2label
need_bin systemctl
need_bin find
need_bin readlink
need_bin cp
need_bin tr
need_bin tail
need_bin df
need_bin date
need_bin ln
need_bin mv
need_bin head
need_bin sleep

[[ $EUID -eq 0 ]] || die "Run as root."
findmnt -n "$SRC_BOOT" >/dev/null 2>&1 || die "SRC_BOOT ($SRC_BOOT) is not a mountpoint."
findmnt -n "$SRC_ROOT" >/dev/null 2>&1 || die "SRC_ROOT ($SRC_ROOT) is not a mountpoint."

IMG_ANCHOR="${IMG_DIR%/}/${IMG_NAME}"
[[ -e "$IMG_ANCHOR" ]] || die "Image not found: $IMG_ANCHOR"

# Work on the real file (handles IMG_ANCHOR being a symlink)
IMG_PATH="$(readlink -f "$IMG_ANCHOR")"
[[ -f "$IMG_PATH" ]] || die "Image anchor does not resolve to a regular file: $IMG_ANCHOR -> $IMG_PATH"

WORKDIR="$(mktemp -d "${WORKDIR_BASE}.XXXXXX")"
MNT_BOOT="${WORKDIR}/mnt_boot"
MNT_ROOT="${WORKDIR}/mnt_root"
mkdir -p "$MNT_BOOT" "$MNT_ROOT"

log "Running $(basename "$(readlink -f "$0")") version $SCRIPT_VERSION"
log "Run timestamp: $RUN_TS"
log "Using image anchor: $IMG_ANCHOR"
log "Using image target: $IMG_PATH"
log "Workdir: $WORKDIR"
debug "DEBUG mode is ON"

run_dbg_cmd "file(1) on IMG" file "$IMG_PATH"
run_dbg_cmd "fdisk -l on IMG" fdisk -l "$IMG_PATH"
run_dbg_cmd "sfdisk --version" sfdisk --version

###############################################################################
# Loop management
###############################################################################
attach_base_loop() {
  # Base loop as “whole disk”; we will NOT rely on /dev/loopXpY nodes.
  LOOPDEV="$(losetup --find --show "$IMG_PATH")"
  loop_assert_rw "$LOOPDEV"
  log "Attached base loop: $LOOPDEV"
}

detach_partition_loops() {
  [[ -n "${LOOP_BOOT:-}" ]] && losetup -d "$LOOP_BOOT" >/dev/null 2>&1 || true
  [[ -n "${LOOP_ROOT:-}" ]] && losetup -d "$LOOP_ROOT" >/dev/null 2>&1 || true
  LOOP_BOOT=""
  LOOP_ROOT=""
}

make_part_loop() {
  # Create an offset-based loop device for a partition described only by:
  #   start sector + size in sectors
  # This avoids reliance on /dev/loopXpY partition nodes and makes behavior
  # independent of kernel loop partition scanning settings.
  local start_sectors="$1"
  local size_sectors="$2"
  local offset_bytes size_bytes

  is_uint "$start_sectors" || die "make_part_loop: start_sectors not numeric: $start_sectors"
  is_uint "$size_sectors"  || die "make_part_loop: size_sectors not numeric: $size_sectors"

  offset_bytes=$(( start_sectors * SECTOR_SIZE ))
  size_bytes=$(( size_sectors  * SECTOR_SIZE ))
  debug "Creating offset loop: start_sectors=$start_sectors size_sectors=$size_sectors offset_bytes=$offset_bytes size_bytes=$size_bytes"
  losetup --find --show --offset "$offset_bytes" --sizelimit "$size_bytes" "$IMG_PATH"
}

###############################################################################
# Partition table handling via sfdisk --json
###############################################################################
get_sfdisk_json() {
  # Capture both stdout/stderr because some sfdisk builds emit warnings to stderr,
  # even though they still output valid JSON to stdout.
  run_dbg_cmd "sfdisk -d LOOPDEV (human)" sfdisk -d "$LOOPDEV"

  # Capture everything (some builds may emit warnings on stderr)
  local out json
  out="$(sfdisk --json "$LOOPDEV" 2>&1)" || {
    debug "sfdisk --json failed; output was:"
    printf '%s\n' "$out" | sed 's/^/[DEBUG] /' >&2
    return 1
  }

  # Extract JSON starting at the first '{' (in case warnings precede it).
  json="$(printf '%s\n' "$out" | awk 'BEGIN{seen=0} /^[[:space:]]*\{/ {seen=1} seen {print}')"

  # Validate non-empty
  if [[ -z "${json}" ]]; then
    debug "sfdisk --json produced no JSON. Raw output was:"
    printf '%s\n' "$out" | sed 's/^/[DEBUG] /' >&2
    return 1
  fi

  printf '%s\n' "$json"
}

# Populates:
#  LABEL_ID  (e.g. 0x1234abcd)
#  PART_COUNT
#  arrays: P_START[i], P_SIZE[i], P_TYPE[i], P_BOOT[i]
read_partitions_from_json() {
  local json parsed

  json="$(get_sfdisk_json)" || die "sfdisk --json failed on $LOOPDEV"
  debug "sfdisk JSON first 80 chars: '$(printf '%s' "$json" | head -c 80)'"

  if [[ "${DEBUG}" == "1" ]]; then
    printf "%s\n" "$json" > "${WORKDIR}/sfdisk.${LOOPDEV##*/}.json"
    debug "Saved sfdisk --json to ${WORKDIR}/sfdisk.${LOOPDEV##*/}.json"
  fi

  printf '%s\n' "$json" | grep -q '^[[:space:]]*{' || die "No JSON object found in sfdisk output"

  parsed="$(
    printf "%s\n" "$json" | python3 -c '
import json, sys
data = json.load(sys.stdin)
pt = data.get("partitiontable", {})
label = pt.get("label")
if label != "dos":
    sys.exit(2)

label_id = pt.get("id") or pt.get("label_id") or ""
parts = pt.get("partitions", [])
if not parts:
    sys.exit(3)

print(f"LABEL_ID={label_id}")
print(f"PART_COUNT={len(parts)}")
for i, p in enumerate(parts):
    start = int(p["start"])
    size  = int(p["size"])
    ptype = str(p["type"])
    bootable = 1 if p.get("bootable", False) else 0
    print(f"P{i}_START={start}")
    print(f"P{i}_SIZE={size}")
    print(f"P{i}_TYPE={ptype}")
    print(f"P{i}_BOOT={bootable}")
'
  )" || die "Failed parsing sfdisk JSON"

  eval "$parsed"
  [[ -n "${PART_COUNT:-}" ]] || die "read_partitions_from_json: PART_COUNT still unset (parsed output was empty)"
  is_uint "$PART_COUNT" || die "read_partitions_from_json: PART_COUNT not numeric: $PART_COUNT"

  log "✅ Parsed partition table via sfdisk --json (PART_COUNT=$PART_COUNT, LABEL_ID=${LABEL_ID:-?})"
}

# Identify boot/root partitions using ONLY the partition table (no probe loops).
# Root must be the last partition.
#
# We still validate the actual filesystem types later using blkid on the
# persistent boot/root loops (created exactly once per table state).
identify_boot_root_from_table() {
  [[ -n "${PART_COUNT:-}" ]] || die "PART_COUNT not set"
  [[ "$PART_COUNT" -ge 2 ]] || die "Expected at least 2 partitions, got $PART_COUNT"

  ROOT_INDEX="$((PART_COUNT-1))"  # root last per requirement
  BOOT_INDEX=""

  # Find the first FAT32-ish partition by MBR type.
  # Common values: 'c' (W95 FAT32 LBA), 'b' (W95 FAT32), occasionally 'e' (W95 FAT16 LBA)
  for i in $(seq 0 $((PART_COUNT-1))); do
    eval "tp=\${P${i}_TYPE}"
    tp="${tp,,}"
    if [[ "$tp" == "c" || "$tp" == "0xc" || "$tp" == "0c" || "$tp" == "b" || "$tp" == "0xb" || "$tp" == "0b" || "$tp" == "e" || "$tp" == "0xe" || "$tp" == "0e" ]]; then
      BOOT_INDEX="$i"
      break
    fi
  done

  [[ -n "$BOOT_INDEX" ]] || die "Could not identify boot partition by type (expected FAT32-ish type c/b/e)."

  eval "BOOT_START=\${P${BOOT_INDEX}_START}"
  eval "BOOT_SIZE=\${P${BOOT_INDEX}_SIZE}"
  eval "ROOT_START=\${P${ROOT_INDEX}_START}"
  eval "ROOT_SIZE=\${P${ROOT_INDEX}_SIZE}"
  eval "ROOT_TYPE=\${P${ROOT_INDEX}_TYPE}"

  # Sanity checks: root must be '83' (Linux) in our images, and must be last.
  local rt="${ROOT_TYPE,,}"
  if [[ "$rt" != "83" && "$rt" != "0x83" ]]; then
    die "Last partition type is not 83/Linux (got '$ROOT_TYPE'). Root must be last."
  fi

  # Avoid eval in a log line (so the type prints cleanly)
  local boot_type
  eval "boot_type=\${P${BOOT_INDEX}_TYPE}"

  log "Boot partition (from table): index=$BOOT_INDEX start=$BOOT_START size(sectors)=$BOOT_SIZE type=$boot_type"
  log "Root partition (from table): index=$ROOT_INDEX start=$ROOT_START size(sectors)=$ROOT_SIZE type=$ROOT_TYPE"
}

###############################################################################
# Create persistent boot/root offset loops from current partition table
###############################################################################
create_partition_loops() {
  detach_partition_loops

  LOOP_BOOT="$(make_part_loop "$BOOT_START" "$BOOT_SIZE")"
  loop_assert_rw "$LOOP_BOOT"

  LOOP_ROOT="$(make_part_loop "$ROOT_START" "$ROOT_SIZE")"
  loop_assert_rw "$LOOP_ROOT"

  log "Attached boot loop (offset, rw): $LOOP_BOOT"
  log "Attached root loop (offset, rw): $LOOP_ROOT"

  # Validate filesystem types using the persistent loops (no probe loops).
  local boot_fs root_fs
  boot_fs="$(blkid -s TYPE -o value "$LOOP_BOOT" 2>/dev/null || true)"
  root_fs="$(blkid -s TYPE -o value "$LOOP_ROOT" 2>/dev/null || true)"
  debug "Persistent loop fstype: boot='$boot_fs' root='$root_fs'"

  if [[ "$boot_fs" != "vfat" && "$boot_fs" != "fat" && "$boot_fs" != "msdos" ]]; then
    die "Boot loop $LOOP_BOOT is not VFAT (got '$boot_fs')"
  fi
  if [[ "$root_fs" != "ext4" ]]; then
    die "Root loop $LOOP_ROOT is not ext4 (got '$root_fs')"
  fi
}


# Write back full MBR table (minimal changes: only root partition size changed)
write_mbr_table_with_new_root_size() {
  local new_root_size="$1"
  is_uint "$new_root_size" || die "write_mbr_table_with_new_root_size: new_root_size not numeric: $new_root_size"

  local tmp="${WORKDIR}/sfdisk.new"
  : > "$tmp"

  {
    echo "label: dos"
    [[ -n "${LABEL_ID:-}" ]] && echo "label-id: ${LABEL_ID}"
    echo "unit: sectors"
    echo ""

    for i in $(seq 0 $((PART_COUNT-1))); do
      eval "st=\${P${i}_START}"
      eval "sz=\${P${i}_SIZE}"
      eval "tp=\${P${i}_TYPE}"
      eval "bt=\${P${i}_BOOT}"

      if [[ "$i" -eq "$((PART_COUNT-1))" ]]; then
        sz="$new_root_size"
      fi

      if [[ "$bt" -eq 1 ]]; then
        echo "start=${st}, size=${sz}, type=${tp}, bootable"
      else
        echo "start=${st}, size=${sz}, type=${tp}"
      fi
    done
  } >> "$tmp"

  debug "Prepared new sfdisk table at $tmp:"
  if [[ "${DEBUG}" == "1" ]]; then
    sed 's/^/[DEBUG] /' "$tmp" >&2 || true
  fi

  # --no-reread: we will recreate offset loops ourselves.
  # Some kernels/drivers refuse BLKRRPART on a busy loop device; that's OK as long
  # as the on-disk table is updated and we recreate our loops from the new values.
  local sf_out sf_rc
  set +e
  sf_out="$(sfdisk --no-reread --force --wipe=never --wipe-partitions=never "$LOOPDEV" < "$tmp" 2>&1)"
  sf_rc=$?
  set -e

  # Don’t hide sfdisk output when it succeeds (debug-only)
  if [[ "${DEBUG}" == "1" && -n "${sf_out}" ]]; then
    debug "sfdisk output:"
    printf '%s\n' "$sf_out" | sed 's/^/[DEBUG]   /' >&2
  fi

  if (( sf_rc != 0 )); then
    # Tolerate the common loop reread ioctl failure as long as the table is actually updated.
    if printf '%s\n' "$sf_out" | grep -qiE 'Re-reading the partition table failed|Re[- ]?read.*partition table failed|ioctl.*reread|BLKRRPART'; then
      debug "sfdisk reported reread failure (expected on busy loop devices); continuing."
    else
      printf '%s\n' "$sf_out" >&2
      die "sfdisk failed to write partition table"
    fi
  fi

  sync
  losetup -c "$LOOPDEV" || true

  run_dbg_cmd "sfdisk -d LOOPDEV after write" sfdisk -d "$LOOPDEV"
  run_dbg_cmd "sfdisk --json LOOPDEV after write" sfdisk --json "$LOOPDEV"

  read_partitions_from_json
  identify_boot_root_from_table
  [[ "$ROOT_SIZE" -eq "$new_root_size" ]] || die "sfdisk write did not apply (ROOT_SIZE=$ROOT_SIZE expected=$new_root_size)"

  # Extra safety: confirm end-of-disk math (root partition fits within image)
  local img_bytes img_sectors root_end
  img_bytes="$(stat -c%s "$IMG_PATH")"
  is_uint "$img_bytes" || die "stat returned non-numeric image size: $img_bytes"
  img_sectors="$(sectors_for_bytes_ceil "$img_bytes")"
  is_uint "$img_sectors" || die "img_sectors not numeric: $img_sectors"

  root_end=$(( ROOT_START + ROOT_SIZE ))
  if (( root_end > img_sectors )); then
    die "Root partition exceeds image size: end=$root_end sectors, image=$img_sectors sectors (start=$ROOT_START size=$ROOT_SIZE)"
  fi
}

###############################################################################
# Labels + service + fstab helpers
###############################################################################
ensure_boot_root_labels() {
  local want_boot="BOOT"
  local want_root="ROOTFS"

  local cur_boot cur_root
  cur_boot="$(blkid -s LABEL -o value "$LOOP_BOOT" 2>/dev/null || true)"
  cur_root="$(blkid -s LABEL -o value "$LOOP_ROOT" 2>/dev/null || true)"

  log "Ensuring filesystem labels: boot='${want_boot}', root='${want_root}'"
  debug "Current labels: boot='${cur_boot:-}', root='${cur_root:-}'"

  if [[ "${cur_boot:-}" != "${want_boot}" ]]; then
    log "Renaming VFAT label: '${cur_boot:-<none>}' -> '${want_boot}'"
    fatlabel "$LOOP_BOOT" "$want_boot"
  fi

  if [[ "${cur_root:-}" != "${want_root}" ]]; then
    log "Renaming ext4 label: '${cur_root:-<none>}' -> '${want_root}'"
    e2label "$LOOP_ROOT" "$want_root"
  fi

  run_dbg_cmd "blkid boot after label ensure" blkid "$LOOP_BOOT"
  run_dbg_cmd "blkid root after label ensure" blkid "$LOOP_ROOT"
}

disable_chromascreen_in_image() {
  # We *disable* the unit by removing wants symlinks, and also try systemctl --root disable.
  # Keep the unit file itself intact so sscpo can enable it later.
  local unit="ChromaScreen.service"

  if [[ ! -d "${MNT_ROOT%/}/etc/systemd/system" ]]; then
    debug "No systemd root at ${MNT_ROOT%/}/etc/systemd/system; skipping ChromaScreen disable."
    return 0
  fi

  log "Disabling ${unit} in image (remove enablement symlinks; keep unit file)..."

  # Best-effort: systemctl offline disable (may fail in some stripped images, so tolerate).
  if systemctl --root="${MNT_ROOT%/}" is-enabled "$unit" >/dev/null 2>&1; then
    debug "${unit} appears enabled per systemctl --root; attempting disable."
    systemctl --root="${MNT_ROOT%/}" disable "$unit" >/dev/null 2>&1 || true
  else
    debug "${unit} not reported enabled (or systemctl can't determine); still scrubbing wants symlinks."
  fi

  # Hard scrub: remove any wants symlinks across targets.
  rm -f "${MNT_ROOT%/}/etc/systemd/system/"*".wants/${unit}" 2>/dev/null || true
  rm -f "${MNT_ROOT%/}/etc/systemd/system/multi-user.target.wants/${unit}" 2>/dev/null || true
  rm -f "${MNT_ROOT%/}/etc/systemd/system/graphical.target.wants/${unit}" 2>/dev/null || true
  rm -f "${MNT_ROOT%/}/etc/systemd/system/default.target.wants/${unit}" 2>/dev/null || true

  # Verify
  if find "${MNT_ROOT%/}/etc/systemd/system" -type l -name "*${unit}*" -print -quit 2>/dev/null | grep -q .; then
    debug "ChromaScreen symlink(s) still found after scrub:"
    find "${MNT_ROOT%/}/etc/systemd/system" -type l -name "*${unit}*" -print -exec ls -la {} \; 2>/dev/null || true
  else
    log "ChromaScreen enablement symlinks removed (disabled)."
  fi
}

patch_fstab_alignment() {
  local fstab="${MNT_ROOT%/}/etc/fstab"
  [[ -f "$fstab" ]] || die "fstab not found in image root: $fstab"

  log "Patching /etc/fstab in image for root UUID, BOOT label, and commenting NFS mount..."

  # 1) Set '/' device field to preserved UUID only on the '/' line
  awk -v new="UUID=${ROOT_FS_UUID_IMG}" '
    /^[[:space:]]*#/ {print; next}
    NF < 2 {print; next}
    $2 == "/" {$1=new}
    {print}
  ' "$fstab" > "${fstab}.tmp" && mv "${fstab}.tmp" "$fstab"

  # 2) Force /boot to LABEL=BOOT if a /boot line exists
  awk -v new="LABEL=BOOT" '
    /^[[:space:]]*#/ {print; next}
    NF < 2 {print; next}
    $2 == "/boot" {$1=new}
    {print}
  ' "$fstab" > "${fstab}.tmp" && mv "${fstab}.tmp" "$fstab"

  # 3) Comment out the NFS mount line (exact prefix match requirement)
  sed -i 's|^[[:space:]]*192\.168\.0\.2:/media/raid1|# &|' "$fstab"

  # Verify root line
  grep -Eq '^[[:space:]]*UUID='"${ROOT_FS_UUID_IMG}"'[[:space:]]+/[[:space:]]' "$fstab" \
    || die "fstab patch verification failed: / entry does not reference image UUID."

  # Verify /boot line uses LABEL=BOOT (only if an active /boot line exists)
  if grep -Eq '^[[:space:]]*[^#[:space:]][^[:space:]]*[[:space:]]+/boot[[:space:]]' "$fstab"; then
    grep -Eq '^[[:space:]]*LABEL=BOOT[[:space:]]+/boot[[:space:]]' "$fstab" \
      || die "fstab patch verification failed: active /boot line is not mounted via LABEL=BOOT"
  fi

  # Verify NFS line is commented (if present at all)
  if grep -Eq '^[[:space:]]*192\.168\.0\.2:/media/raid1' "$fstab"; then
    die "fstab patch verification failed: NFS mount line still active (not commented)."
  fi
}

purge_writelg() {
  # Requirement: /etc/.writelg must not exist in final image
  if [[ -e "${MNT_ROOT%/}/etc/.writelg" ]]; then
    log "Removing /etc/.writelg from image root..."
    rm -f "${MNT_ROOT%/}/etc/.writelg" || true
  fi
}

###############################################################################
# v0.0.31+: update PACKAGED_DATE in /etc/ophub-release (image root)
###############################################################################
update_ophub_packaged_date() {
  local rel="${MNT_ROOT%/}/etc/ophub-release"
  [[ -f "$rel" ]] || { debug "No /etc/ophub-release in image; skipping PACKAGED_DATE update."; return 0; }

  if ! grep -qE '^[[:space:]]*PACKAGED_DATE[[:space:]]*=' "$rel"; then
    debug "No PACKAGED_DATE= line in /etc/ophub-release; skipping."
    return 0
  fi

  local now
  now="${RUN_TS}"   # shared run timestamp

  log "Updating /etc/ophub-release PACKAGED_DATE to '${now}'..."

  python3 - <<PY
import re, pathlib
p = pathlib.Path(r"$rel")
now = r"$now"

lines = p.read_text(errors="replace").splitlines(True)
out = []
changed = False

for line in lines:
    if not changed:
        m = re.match(r'^(\s*PACKAGED_DATE\s*=\s*)(.*?)(\s*)$', line.rstrip('\r\n'))
        if m:
            prefix = m.group(1)
            trailing_ws = m.group(3) or ""
            newline = "\n" if line.endswith("\n") else ""
            out.append(f"{prefix}'{now}'{trailing_ws}{newline}")
            changed = True
            continue
    out.append(line)

if changed:
    p.write_text(''.join(out))
else:
    raise SystemExit("PACKAGED_DATE line was detected by grep but not rewritten (unexpected).")
PY

  run_dbg_cmd "ophub-release (PACKAGED_DATE post patch)" sh -c "grep -nE '^[[:space:]]*PACKAGED_DATE[[:space:]]*=' '$rel' || true"
}

###############################################################################
# v0.0.34: finalize image filename + stable anchor symlink
###############################################################################
finalize_image_filename() {
  # Rename the real image file to include RUN_TS_FN, then repoint IMG_ANCHOR symlink.
  # IMG_ANCHOR stays constant (e.g. coprint.img) so next run always finds it.

  local dir base ext stem new_base new_path n

  dir="${IMG_DIR%/}"
  base="$IMG_NAME"
  stem="${base%.*}"
  ext="${base##*.}"

  # Handle "no extension" gracefully
  if [[ "$ext" == "$base" ]]; then
    ext=""
  fi

  if [[ -n "$ext" ]]; then
    new_base="${stem}_${RUN_TS_FN}.${ext}"
  else
    new_base="${stem}_${RUN_TS_FN}"
  fi

  new_path="${dir}/${new_base}"

  # If the current target already has the desired name, just ensure symlink is correct.
  if [[ -e "$IMG_ANCHOR" ]]; then
    if [[ "$(readlink -f "$IMG_ANCHOR")" == "$new_path" && -f "$new_path" ]]; then
      log "Image already named with current timestamp: $new_base"
      ( cd "$dir" && ln -sfn "$new_base" "$base" )
      IMG_PATH="$new_path"
      return 0
    fi
  fi

  # Collision handling: if a file with that name exists, append _1, _2, ...
  if [[ -e "$new_path" ]]; then
    n=1
    while :; do
      if [[ -n "$ext" ]]; then
        new_base="${stem}_${RUN_TS_FN}_${n}.${ext}"
      else
        new_base="${stem}_${RUN_TS_FN}_${n}"
      fi
      new_path="${dir}/${new_base}"
      [[ ! -e "$new_path" ]] && break
      n=$((n+1))
    done
    log "Filename collision detected; using: $new_base"
  fi

  log "Renaming image target -> $new_base"
  mv -f -- "$IMG_PATH" "$new_path"

  # Repoint anchor to the new basename (relative symlink inside IMG_DIR)
  log "Updating anchor symlink: $(basename "$IMG_ANCHOR") -> $new_base"
  ( cd "$dir" && ln -sfn "$new_base" "$base" )

  # Update IMG_PATH to reflect the new real path for final logs
  IMG_PATH="$new_path"
}

###############################################################################
# v0.0.28+: uEnv sync + patch helper
###############################################################################
sync_and_patch_uenv_from_source() {
  local src_uenv="${SRC_BOOT%/}/uEnv.txt"
  local dst_uenv="${MNT_BOOT%/}/uEnv.txt"

  [[ -f "$src_uenv" ]] || die "SRC_BOOT uEnv.txt not found: $src_uenv"

  log "Copying uEnv.txt from source boot into image boot..."
  cp -a "$src_uenv" "$dst_uenv"

  log "Patching uEnv.txt APPEND: set root UUID + console=tty0 + ensure trailing '  autoflash=301'..."

  python3 - <<PY
import re, pathlib

dst = pathlib.Path(r"$dst_uenv")
root_uuid = r"$ROOT_FS_UUID_IMG"

lines = dst.read_text(errors="replace").splitlines(True)
out = []
patched = False

for line in lines:
    if line.startswith("APPEND="):
        cmd = line[len("APPEND="):]
        # Preserve original newline
        nl = "\n" if cmd.endswith("\n") else ""
        cmd = cmd.rstrip("\n")

        # Remove any existing autoflash=... tokens (anywhere), minimally invasive.
        # Remove token plus any *preceding* whitespace to avoid leaving dangling gaps.
        cmd = re.sub(r'\s*autoflash=\S+', '', cmd)

        # v0.0.39: If cmdline contains exact token 'console=0' then rewrite to 'console=tty0'.
        # (Only matches token boundary: start/whitespace before and whitespace/end after.)
        cmd = re.sub(r'(^|\s)console=0(?=\s|$)', r'\1console=tty0', cmd)

        # Force root=UUID=<preserved>
        if re.search(r'(^|\s)root=(UUID|PARTUUID|LABEL)=\S+', cmd):
            cmd = re.sub(
                r'(^|\s)root=(UUID|PARTUUID|LABEL)=\S+',
                lambda m: (m.group(1) + f"root=UUID={root_uuid}"),
                cmd,
                count=1
            )
        else:
            # If no root= exists, prepend with a single trailing space.
            cmd = f"root=UUID={root_uuid} " + cmd

        # Preserve internal spacing; strip trailing whitespace then append exactly once
        # with TWO spaces before it.
        cmd = cmd.rstrip() + "  autoflash=301"

        out.append("APPEND=" + cmd + nl)
        patched = True
    else:
        out.append(line)

if not patched:
    raise SystemExit("ERROR: uEnv.txt has no APPEND= line to patch")

dst.write_text(''.join(out))
PY

  # Quick sanity:
  #   - root UUID must be present
  #   - trailing '  autoflash=301' must be present
  #   - dual console directives allowed:
  #       * allow console=ttyAML0,115200n8 (leave untouched)
  #       * require console=tty0 to exist
  #       * require console=0 to be absent
  if ! grep -Eq '^APPEND=.*root=UUID='"${ROOT_FS_UUID_IMG}"'.*  autoflash=301[[:space:]]*$' "$dst_uenv"; then
    run_dbg_cmd "uEnv.txt (post patch)" sed -n '1,120p' "$dst_uenv"
    die "uEnv.txt patch verification failed (root UUID and/or trailing autoflash missing)."
  fi

  if grep -Eq '^APPEND=.*(^|[[:space:]])console=0([[:space:]]|$)' "$dst_uenv"; then
    run_dbg_cmd "uEnv.txt (post patch)" sed -n '1,120p' "$dst_uenv"
    die "uEnv.txt patch verification failed: console=0 still present."
  fi

  if ! grep -Eq '^APPEND=.*(^|[[:space:]])console=tty0([[:space:]]|$)' "$dst_uenv"; then
    run_dbg_cmd "uEnv.txt (post patch)" sed -n '1,120p' "$dst_uenv"
    die "uEnv.txt patch verification failed: console=tty0 not present."
  fi

  run_dbg_cmd "uEnv.txt APPEND (post patch)" sh -c "grep -n '^APPEND=' '$dst_uenv' || true"
}

###############################################################################
# Main flow
###############################################################################
attach_base_loop

# Read partitions via sfdisk --json
read_partitions_from_json || die "Failed to parse sfdisk --json output; see DEBUG logs above."
identify_boot_root_from_table
create_partition_loops
run_dbg_cmd "blkid on initial root loop" blkid "$LOOP_ROOT"

###############################################################################
# Step 1a: Capture image root filesystem UUID (MUST be preserved & used later)
###############################################################################
ROOT_FS_UUID_IMG="$(blkid -s UUID -o value "$LOOP_ROOT" || true)"
[[ -n "$ROOT_FS_UUID_IMG" ]] || die "Could not read filesystem UUID from image root partition."
log "Image root FS UUID (saved): $ROOT_FS_UUID_IMG"

###############################################################################
# Step 2/3: Compute if the image root partition is large enough
###############################################################################
log "Calculating used bytes on source root ($SRC_ROOT) ..."
SOURCE_USED_BYTES="$(
  du -x -B1 -s \
    --exclude="$SRC_BOOT" \
    --exclude='/proc' --exclude='/sys' --exclude='/dev' --exclude='/run' \
    --exclude='/tmp' --exclude='/mnt' --exclude='/media' --exclude='/lost+found' \
    "$SRC_ROOT" | awk '{print $1}'
)"
is_uint "$SOURCE_USED_BYTES" || die "Failed to compute SOURCE_USED_BYTES"

BUFFER_FIXED_BYTES="$(bytes_from_mib "$BUFFER_FIXED_MIB")"
BUFFER_PCT_BYTES=$(( SOURCE_USED_BYTES * BUFFER_PCT / 100 ))

if (( BUFFER_FIXED_BYTES > BUFFER_PCT_BYTES )); then
  BUFFER_BYTES="$BUFFER_FIXED_BYTES"
else
  BUFFER_BYTES="$BUFFER_PCT_BYTES"
fi

# v0.0.29: require "used + buffer + headroom", not just "used + buffer"
BASE_BYTES=$(( SOURCE_USED_BYTES + BUFFER_BYTES ))

MIN_FREE_BYTES="$(bytes_from_mib "$MIN_FREE_MIB")"
TARGET_BY_FIXED_FREE=$(( BASE_BYTES + MIN_FREE_BYTES ))
TARGET_BY_PCT="$(pct_total_target_bytes "$BASE_BYTES" "$MIN_FREE_PCT")"

REQUIRED_BYTES=$(( TARGET_BY_FIXED_FREE > TARGET_BY_PCT ? TARGET_BY_FIXED_FREE : TARGET_BY_PCT ))

ROUND_QUANTUM_BYTES="$(bytes_from_mib "$ROUND_UP_MIB")"
REQUIRED_BYTES="$(round_up_bytes "$REQUIRED_BYTES" "$ROUND_QUANTUM_BYTES")"

is_uint "$REQUIRED_BYTES" || die "REQUIRED_BYTES not numeric: $REQUIRED_BYTES"

# Current root partition bytes based on table size, not kernel partition nodes
IMG_ROOT_PART_BYTES="$(bytes_for_sectors "$ROOT_SIZE")"

log "Source used bytes:        $SOURCE_USED_BYTES"
log "Buffer bytes:             $BUFFER_BYTES (max(${BUFFER_FIXED_MIB}MiB, ${BUFFER_PCT}%))"
log "Base bytes (used+buf):    $BASE_BYTES"
log "Min free policy:          ${MIN_FREE_MIB}MiB and ${MIN_FREE_PCT}%"
log "Required root bytes:      $REQUIRED_BYTES (rounded up to ${ROUND_UP_MIB}MiB)"
log "Image root partition:     $IMG_ROOT_PART_BYTES (from partition table)"

# v0.0.37 cleanup: bash integer compare (no awk)
NEED_GROW="0"
(( REQUIRED_BYTES > IMG_ROOT_PART_BYTES )) && NEED_GROW="1" || true

log "Grow decision: required=$REQUIRED_BYTES current_part_bytes=$IMG_ROOT_PART_BYTES NEED_GROW=$NEED_GROW"

###############################################################################
# Step 4/5/6: Grow image + root partition + filesystem (MBR, root is last)
###############################################################################
if [[ "$NEED_GROW" == "1" ]]; then
  log "Image root partition is too small. Preparing to grow image + root partition..."

  IMG_BYTES_CURRENT="$(stat -c%s "$IMG_PATH")"
  is_uint "$IMG_BYTES_CURRENT" || die "stat -c%s returned non-numeric for IMG_BYTES_CURRENT: $IMG_BYTES_CURRENT"
  IMG_SECTORS_CURRENT="$(sectors_for_bytes_ceil "$IMG_BYTES_CURRENT")"
  debug "Image current bytes=$IMG_BYTES_CURRENT sectors=$IMG_SECTORS_CURRENT"

  MIN_ROOT_SECTORS="$(sectors_for_bytes_ceil "$REQUIRED_BYTES")"
  MIN_IMG_SECTORS_NEEDED="$(( ROOT_START + MIN_ROOT_SECTORS ))"
  debug "Min root sectors needed=$MIN_ROOT_SECTORS; min total image sectors needed=$MIN_IMG_SECTORS_NEEDED"

  ADD_BYTES="0"
  if (( MIN_IMG_SECTORS_NEEDED > IMG_SECTORS_CURRENT )); then
    ADD_SECTORS="$(( MIN_IMG_SECTORS_NEEDED - IMG_SECTORS_CURRENT ))"
    ADD_BYTES="$(( ADD_SECTORS * SECTOR_SIZE ))"
  fi

  # We may rewrite the partition table; drop partition loops now.
  detach_partition_loops

  if (( ADD_BYTES > 0 )); then
    log "Growing image file by $ADD_BYTES bytes (append only)..."
    truncate -s +"$ADD_BYTES" "$IMG_PATH"
    losetup -c "$LOOPDEV"
    run_dbg_cmd "losetup -l (filtered) after refresh" sh -c "losetup -l | grep -F \"$LOOPDEV\" || true"
    debug "Image new byte size after truncate: $(stat -c%s "$IMG_PATH")"
  else
    log "Image file size already sufficient; will only extend partition table entry if needed."
  fi

  IMG_BYTES_NEW="$(stat -c%s "$IMG_PATH")"
  is_uint "$IMG_BYTES_NEW" || die "stat -c%s returned non-numeric for IMG_BYTES_NEW: $IMG_BYTES_NEW"

  # Force sector alignment (avoid partial-sector edge cases)
  ALIGNED_BYTES=$(( (IMG_BYTES_NEW / SECTOR_SIZE) * SECTOR_SIZE ))
  if (( IMG_BYTES_NEW != ALIGNED_BYTES )); then
    log "Aligning image size down to sector boundary: $IMG_BYTES_NEW -> $ALIGNED_BYTES"
    truncate -s "$ALIGNED_BYTES" "$IMG_PATH"
    losetup -c "$LOOPDEV"
    IMG_BYTES_NEW="$ALIGNED_BYTES"
  fi

  IMG_SECTORS_NEW="$(sectors_for_bytes_ceil "$IMG_BYTES_NEW")"
  is_uint "$IMG_SECTORS_NEW" || die "IMG_SECTORS_NEW not numeric: $IMG_SECTORS_NEW"

  # IMPORTANT: exact integer math so we never exceed disk size
  NEW_ROOT_SIZE_SECTORS="$(( IMG_SECTORS_NEW - ROOT_START ))"

  if (( NEW_ROOT_SIZE_SECTORS <= ROOT_SIZE )); then
    log "Partition table already has root size(sectors)=$ROOT_SIZE; computed new size=$NEW_ROOT_SIZE_SECTORS (no shrink)."
    # We detached loops earlier; recreate them before continuing.
    create_partition_loops
    run_dbg_cmd "blkid on root loop after grow decision (no shrink)" blkid "$LOOP_ROOT"
  else
    log "Updating MBR root entry size(sectors)=${NEW_ROOT_SIZE_SECTORS} (start=${ROOT_START})..."
    write_mbr_table_with_new_root_size "$NEW_ROOT_SIZE_SECTORS"

    # Now recreate loops using the updated partition table state.
    create_partition_loops
    run_dbg_cmd "blkid on root loop after grow decision (grew)" blkid "$LOOP_ROOT"

    # Verify filesystem UUID unchanged
    ROOT_FS_UUID_CHECK="$(blkid -s UUID -o value "$LOOP_ROOT" || true)"
    [[ "$ROOT_FS_UUID_CHECK" == "$ROOT_FS_UUID_IMG" ]] || die "Root filesystem UUID changed unexpectedly! ($ROOT_FS_UUID_CHECK != $ROOT_FS_UUID_IMG)"

    log "Running e2fsck on image root before resize..."
    e2fsck -f -p "$LOOP_ROOT" || die "e2fsck reported issues requiring manual intervention."

    log "Resizing ext4 filesystem to fill expanded partition..."
    resize2fs "$LOOP_ROOT"

    log "Re-check filesystem..."
    e2fsck -f -p "$LOOP_ROOT" || die "Post-resize e2fsck reported issues."
  fi
else
  log "Image root partition is already large enough. No resize needed."
fi

###############################################################################
# Ensure filesystem labels on the image devices before mounting
###############################################################################
ensure_boot_root_labels

###############################################################################
# Mount image partitions (offset loops)
###############################################################################
log "Mounting image boot partition..."

# VFAT can be auto-mounted read-only by the kernel if it was not cleanly
# unmounted or contains errors. Since we need to write kernel artifacts into
# /boot, always run an auto-repair fsck and then *verify* the mount is RW.
log "Checking/repairing VFAT boot filesystem before mount..."
fsck.vfat -a "$LOOP_BOOT" >/dev/null 2>&1 || {
  # Re-run verbosely so the user sees the failure context.
  fsck.vfat -a "$LOOP_BOOT" || die "fsck.vfat failed on boot partition ($LOOP_BOOT)"
}

mount -t vfat -o rw "$LOOP_BOOT" "$MNT_BOOT"

# Abort if the kernel forced a read-only mount anyway.
BOOT_OPTS="$(findmnt -no OPTIONS "$MNT_BOOT" || true)"
# Note: vfat mount options may include "errors=remount-ro" even when mounted RW.
# We only treat it as read-only if the *actual* ro flag is present.
case ",$BOOT_OPTS," in
  *,ro,*)
    run_dbg_cmd "findmnt boot (opts)" findmnt "$MNT_BOOT"
    die "Boot partition mounted read-only (opts: $BOOT_OPTS). VFAT is likely still corrupt."
    ;;
  *)
    ;;
esac

log "Mounting image root partition..."
mount -t ext4 "$LOOP_ROOT" "$MNT_ROOT"
run_dbg_cmd "df -B1 on image root" df -B1 "$MNT_ROOT"

###############################################################################
# Step 7a: Boot partition sync (zImage + uInitrd + dtb/)
###############################################################################
log "Updating zImage/uInitrd/dtb on image boot..."
BOOT_INCLUDES=(
  "/zImage"
  "/uInitrd"
  "/dtb/***"
)

RSYNC_BOOT_RULES=()
for pat in "${BOOT_INCLUDES[@]}"; do
  RSYNC_BOOT_RULES+=(--include="$pat")
done
RSYNC_BOOT_RULES+=(--include="*/")
RSYNC_BOOT_RULES+=(--exclude="*")

rsync "${RSYNC_COMMON[@]}" "${RSYNC_VERBOSITY[@]}" \
  "${RSYNC_BOOT_RULES[@]}" \
  "${SRC_BOOT%/}/" "${MNT_BOOT%/}/"

# v0.0.28+: uEnv sourced from device then patched
sync_and_patch_uenv_from_source

###############################################################################
# Step 7b: Root partition sync (mirror with delete)
###############################################################################
log "Syncing source root filesystem into image root (two-pass rsync mirror)..."

ROOT_EXCLUDES=(
  --exclude="/dev/*"
  --exclude="/proc/*"
  --exclude="/sys/*"
  --exclude="/run/*"
  --exclude="/tmp/*"
  --exclude="/mnt/*"
  --exclude="/media/*"
  --exclude="/lost+found"
  --exclude="/boot/*"
  --exclude="/etc/.writelg"
)

# Pass 1
rsync "${RSYNC_COMMON[@]}" "${RSYNC_VERBOSITY[@]}" \
  --delete --delete-delay \
  "${ROOT_EXCLUDES[@]}" \
  "${SRC_ROOT%/}/" "${MNT_ROOT%/}/"

# Pass 2
rsync "${RSYNC_COMMON[@]}" "${RSYNC_VERBOSITY[@]}" \
  --delete --delete-delay \
  "${ROOT_EXCLUDES[@]}" \
  "${SRC_ROOT%/}/" "${MNT_ROOT%/}/"

# Ensure transient dirs exist and are empty
for d in dev proc sys run tmp mnt media; do
  mkdir -p "${MNT_ROOT%/}/$d"
  rm -rf "${MNT_ROOT%/}/$d/"* 2>/dev/null || true
done

# v0.0.31+: refresh the timestamp shown on console during restore (if present)
update_ophub_packaged_date

###############################################################################
# Post-rsync cleanup and boot-safety edits
###############################################################################
purge_writelg
disable_chromascreen_in_image
patch_fstab_alignment

# v0.0.29: sanity check free space on image root after sync
FREE_BYTES="$(df -B1 --output=avail "$MNT_ROOT" | tail -n1 | tr -d ' ')"
is_uint "$FREE_BYTES" || die "Failed to read free space from df: '$FREE_BYTES'"

MIN_FREE_BYTES_CHECK="$(bytes_from_mib "$MIN_FREE_MIB")"
if (( FREE_BYTES < MIN_FREE_BYTES_CHECK )); then
  echo "[!] WARNING: Image root free space is below policy floor (${MIN_FREE_MIB}MiB)." >&2
  echo "[!]          Free bytes: $FREE_BYTES, floor: $MIN_FREE_BYTES_CHECK" >&2
  echo "[!]          Consider increasing MIN_FREE_MIB / MIN_FREE_PCT or rounding quantum." >&2
fi

sync

###############################################################################
# Finalize filename (rename target + repoint anchor symlink)
###############################################################################
debug "Finalizing: unmounting and detaching loop devices prior to rename..."

# Strict unmount (but only if mounted)
if mountpoint -q "$MNT_BOOT"; then
  umount "$MNT_BOOT" || die "Failed to unmount boot ($MNT_BOOT)"
fi
if mountpoint -q "$MNT_ROOT"; then
  umount "$MNT_ROOT" || die "Failed to unmount root ($MNT_ROOT)"
fi

# v0.0.47: stricter post-run VFAT sanity check (after sync + small settle)
if [[ -n "${LOOP_BOOT:-}" ]]; then
  log "Post-run VFAT sanity check on boot partition (strict)..."
  sync
  sleep 0.2

  if mountpoint -q "$MNT_BOOT"; then
    run_dbg_cmd "findmnt boot (still mounted!)" findmnt "$MNT_BOOT" || true
    die "Boot still mounted at $MNT_BOOT; refusing to run post-run fsck.vfat"
  fi

  losetup -c "$LOOP_BOOT" 2>/dev/null || true
  fsck.vfat -a "$LOOP_BOOT" >/dev/null 2>&1 || {
    fsck.vfat -a "$LOOP_BOOT" || die "Post-run fsck.vfat failed on boot partition ($LOOP_BOOT)"
  }
fi

# Detach loops before rename
losetup -d "$LOOP_BOOT" 2>/dev/null || true
losetup -d "$LOOP_ROOT" 2>/dev/null || true
losetup -d "$LOOPDEV" 2>/dev/null || true
LOOP_BOOT=""; LOOP_ROOT=""; LOOPDEV=""

finalize_image_filename

###############################################################################
# Done
###############################################################################
log "All operations completed successfully."
echo " "
log "Image anchor: $IMG_ANCHOR"
log "Image target: $IMG_PATH"
