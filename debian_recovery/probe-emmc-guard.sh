#!/bin/sh
# Runs only inside the separately built probe initramfs. Never install as a service.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
probe_mode=${1:-}
case "$probe_mode" in protect|verify) ;; *) exit 1;; esac
probe_log=/run/initramfs/chromapad-emmc-probe.log
mkdir -p /run/initramfs
note() {
    printf '%s\n' "$*" >> "$probe_log"
    printf '<5>ChromaPad eMMC probe: %s\n' "$*" > /dev/kmsg 2>/dev/null || true
}
fail() { note "FAIL: $*"; exit 1; }
probe_root_tokens=0
probe_enabled=0
for probe_arg in $(cat /proc/cmdline); do
    case "$probe_arg" in
        root=UUID=38327d56-529b-498f-8b1a-e81b534bfeb9) probe_root_tokens=$((probe_root_tokens+1));;
        root=*) fail 'Unexpected root device';;
        chromapad.emmc_probe=readonly) probe_enabled=1;;
        autoflash=*) fail 'Recovery trigger forbidden';;
    esac
done
[ "$probe_root_tokens" = 1 ] && [ "$probe_enabled" = 1 ] || fail 'Probe/root boot arguments missing'

protect_all() {
    for probe_sys in /sys/class/block/mmcblk*; do
        [ -e "$probe_sys/dev" ] || continue
        probe_node=/dev/${probe_sys##*/}
        [ -b "$probe_node" ] || continue
        blockdev --setro "$probe_node" || fail "Cannot protect $probe_node"
    done
}

if [ "$probe_mode" = protect ]; then
    # This is called before init-top/udev and before generic module loading.
    # Only the supplies, clocks and MMC host needed for enumeration are loaded.
    for probe_module in fixed clk-phase sclk-div meson_gx_mmc; do
        modprobe "$probe_module" || fail "Cannot load $probe_module"
    done
fi

probe_wait=0
while :; do
    [ "$probe_mode" != protect ] || protect_all
    probe_count=0
    probe_disk=
    for probe_sys in /sys/block/mmcblk*; do
        [ -e "$probe_sys/device/type" ] || continue
        case "${probe_sys##*/}" in *boot*) continue;; esac
        probe_count=$((probe_count+1))
        probe_disk=${probe_sys##*/}
    done
    [ "$probe_count" -le 1 ] || fail 'Unexpected additional MMC/SD disk'
    if [ "$probe_count" = 1 ] && [ -b "/dev/${probe_disk}p1" ] &&
       [ -b "/dev/${probe_disk}p2" ] && [ -b "/dev/${probe_disk}boot0" ] &&
       [ -b "/dev/${probe_disk}boot1" ]; then break; fi
    [ "$probe_mode" = protect ] || fail 'MMC disappeared or incomplete at handoff'
    [ "$probe_wait" -lt 45 ] || fail 'MMC discovery timed out'
    sleep 1
    probe_wait=$((probe_wait+1))
done
[ "$probe_mode" != protect ] || protect_all

probe_device=/sys/class/block/$probe_disk/device
[ "$(cat "$probe_device/type")" = MMC ] || fail 'Target is not MMC'
[ "$(cat "$probe_device/name")" = AJNB4R ] || fail 'Target name mismatch'
[ "$(cat "$probe_device/serial")" = 0x35a5fac6 ] || fail 'Target serial mismatch'
[ "$(blockdev --getsize64 "/dev/$probe_disk")" = 15634268160 ] || fail 'Target capacity mismatch'
[ "$(blockdev --getss "/dev/$probe_disk")" = 512 ] || fail 'Target sector-size mismatch'
probe_cid=$(cat "$probe_device/cid")
[ "${#probe_cid}" = 32 ] || fail 'Invalid CID length'
case "$probe_cid" in *[!0-9a-fA-F]*) fail 'Invalid CID';; esac

probe_surfaces=0
for probe_sys in /sys/class/block/mmcblk*; do
    [ -e "$probe_sys/dev" ] || continue
    probe_node=/dev/${probe_sys##*/}
    [ -b "$probe_node" ] || fail "Missing block node $probe_node"
    [ "$(blockdev --getro "$probe_node")" = 1 ] || fail "Writable surface $probe_node"
    case "${probe_sys##*/}" in
        "$probe_disk"|"${probe_disk}p1"|"${probe_disk}p2"|"${probe_disk}boot0"|"${probe_disk}boot1") ;;
        *) fail "Unexpected block surface $probe_node";;
    esac
    probe_surfaces=$((probe_surfaces+1))
done
[ "$probe_surfaces" = 5 ] || fail 'Incomplete protection coverage'
for probe_boot in 0 1; do
    [ "$(cat "/sys/class/block/${probe_disk}boot${probe_boot}/force_ro")" = 1 ] || fail 'Hardware boot-area force_ro is not set'
done
[ "$(cat "/sys/class/block/${probe_disk}p1/start")" = 139264 ] || fail 'p1 start mismatch'
[ "$(cat "/sys/class/block/${probe_disk}p1/size")" = 1046528 ] || fail 'p1 size mismatch'
[ "$(cat "/sys/class/block/${probe_disk}p2/start")" = 3289088 ] || fail 'p2 start mismatch'
[ "$(cat "/sys/class/block/${probe_disk}p2/size")" = 27246592 ] || fail 'p2 size mismatch'
dd if="/dev/$probe_disk" of=/run/initramfs/chromapad-emmc-mbr.bin bs=512 count=1 2>/dev/null || fail 'MBR read failed'
probe_hash=$(sha256sum /run/initramfs/chromapad-emmc-mbr.bin)
probe_hash=${probe_hash%% *}
[ "$probe_hash" = e7ac296d23ef572ae908c377af0289ef0a7a355bd4603e0fd66e85d1fee2b60b ] || fail 'MBR differs from saved original'

if [ "$probe_mode" = protect ]; then
    mkdir -p /run/udev/rules.d
    cat > /run/udev/rules.d/99-chromapad-emmc-probe.rules <<'RULE'
ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="mmcblk[0-9]*", ENV{UDISKS_IGNORE}="1", ENV{UDISKS_AUTO}="0", OPTIONS+="nowatch", RUN+="/usr/sbin/blockdev --setro /dev/%k"
RULE
    printf '%s\n' "$probe_cid" > /run/initramfs/chromapad-emmc-cid
else
    [ "$(cat /run/initramfs/chromapad-emmc-cid)" = "$probe_cid" ] || fail 'CID changed during boot'
    probe_usb_root=
    while read -r probe_source probe_mount probe_rest; do
        case "$probe_source" in /dev/mmcblk*) fail 'MMC filesystem was mounted';; esac
        if [ "$probe_mount" = /root ]; then probe_usb_root=$probe_source; fi
    done < /proc/mounts
    [ -n "$probe_usb_root" ] || fail 'Mounted USB root missing'
    [ "$(blkid -s UUID -o value "$probe_usb_root")" = 38327d56-529b-498f-8b1a-e81b534bfeb9 ] || fail 'Mounted root is not the USB filesystem'
fi
note "PASS $probe_mode disk=$probe_disk cid=$probe_cid surfaces=$probe_surfaces mbr=$probe_hash"
