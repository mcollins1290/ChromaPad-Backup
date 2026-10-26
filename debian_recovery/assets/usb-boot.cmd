# ChromaPad Debian USB boot. All setenv changes below are RAM-only.
# Block the observed caller's fallback paths even if this script is interrupted.
setenv start_mmc_autoscript 'echo ChromaPad: SD fallback blocked'
setenv start_emmc_autoscript 'echo ChromaPad: internal-storage fallback blocked'
setenv storeboot 'echo ChromaPad: vendor fallback blocked'
setenv update 'echo ChromaPad: update fallback blocked'
setenv recovery_from_flash 'echo ChromaPad: recovery fallback blocked'
setenv recovery_from_udisk 'echo ChromaPad: recovery fallback blocked'
setenv recovery_from_sdcard 'echo ChromaPad: recovery fallback blocked'
setenv bootcmd 'echo ChromaPad: USB boot stopped. Remove USB and power-cycle to restore normal boot'
setenv verify yes
setenv kernel_addr_r 0x11000000
setenv fdt_addr_r 0x10000000
setenv ramdisk_addr_r 0x20000000
setenv fdt_high 0x30000000
setenv initrd_high 0x38000000
setenv bootargs 'root=UUID=38327d56-529b-498f-8b1a-e81b534bfeb9 rootfstype=ext4 rootwait rw fsck.repair=yes console=ttyAML0,115200 console=tty1 net.ifnames=0 panic=0'
if fatload usb ${usbdev}:1 ${kernel_addr_r} Image 23fafc0; then
 if test ${filesize} = 23fafc0; then
  if fatload usb ${usbdev}:1 ${ramdisk_addr_r} uInitrd 2587b16; then
   if test ${filesize} = 2587b16; then
    if fatload usb ${usbdev}:1 ${fdt_addr_r} meson-sm1-chromapad.dtb 13009; then
     if test ${filesize} = 13009; then
      echo Starting ChromaPad Debian from USB
      booti ${kernel_addr_r} ${ramdisk_addr_r} ${fdt_addr_r}
     fi
    fi
   fi
  fi
 fi
fi
echo ChromaPad USB boot failed. Internal-storage and recovery fallbacks are blocked.
echo Remove USB and power-cycle to return to the original installation.
while true; do sleep 1; done
