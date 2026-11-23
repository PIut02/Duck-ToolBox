#!/system/bin/sh
# Sourced by the KernelSU/APatch/Magisk installer, in BusyBox ash standalone mode. Only the
# installer helpers (ui_print, abort, set_perm*) and its variables are guaranteed here.


# Nothing is mounted, so a metamodule that supports hot install (e.g. mountify) may bring the
# module up without a reboot and run hotinstall.sh.
export MODULE_HOT_INSTALL_REQUEST="true"
export MODULE_HOT_RUN_SCRIPT="hotinstall.sh"

ui_print " "
ui_print "- Duck ToolBox installer"

# KernelSU sets MAGISK_VER_CODE too (always 25200), so check it last.
if [ "$KSU" = "true" ]; then
  ui_print "- Root manager: KernelSU $KSU_VER ($KSU_VER_CODE)"
elif [ "$APATCH" = "true" ]; then
  ui_print "- Root manager: APatch $APATCH_VER ($APATCH_VER_CODE)"
elif [ -n "$MAGISK_VER_CODE" ]; then
  ui_print "- Root manager: Magisk $MAGISK_VER ($MAGISK_VER_CODE)"
else
  abort "! Unsupported environment; install from KernelSU, APatch or Magisk."
fi

case "$ARCH" in
  arm64) ui_print "- Device platform: $ARCH" ;;
  x64|x86_64)
    ui_print "- Device platform: $ARCH (x86_64)"
    [ -f "$MODPATH/bin/duckd-x86_64" ] ||
      abort "! Unsupported platform: $ARCH (x86_64 backend missing from this package)."
    cp "$MODPATH/bin/duckd-x86_64" "$MODPATH/bin/duckd" ||
      abort "! Could not install the x86_64 backend."
    chmod 0755 "$MODPATH/bin/duckd" ||
      abort "! Could not mark the x86_64 backend executable."
    rm -f "$MODPATH/bin/duckd-x86_64"
    # These helpers contain ARM64-only code and would only produce exec errors on x86_64.
    rm -f "$MODPATH/bin/tini-arm64" \
      "$MODPATH/play-integrity/pif-engine" \
      "$MODPATH/device-faker/device-faker-engine"
    ;;
  *) abort "! Unsupported platform: $ARCH (this package requires arm64 or x86_64)" ;;
esac

if [ -z "$API" ] || [ "$API" -lt 26 ]; then
  abort "! Android 8.0 (API 26) or newer is required."
fi

# shellcheck source=util_functions.sh
. "$MODPATH/util_functions.sh" || abort "! Could not verify the module edition."

if conflicting_installation "$MODULE_ID" /data/adb; then
  abort "! Full and Lite cannot coexist. Uninstall the other edition and reboot before installing."
fi

if [ ! -d "/data/adb/modules/tricky_store" ] &&
  [ ! -d "/data/adb/modules/oh_my_keymint" ] &&
  [ ! -d "/data/adb/modules/teesim" ]; then
  ui_print "! No keystore module found (Tricky Store, OhMyKeymint or TEESimulator)."
  ui_print "  The Tricky Store manager stays idle until one is installed."
fi

# KernelSU and APatch show a WebUI button; the action button only helps Magisk users.
if [ "$KSU" = "true" ] || [ "$APATCH" = "true" ]; then
  rm -f "$MODPATH/action.sh"
fi

ui_print "- Preparing runtime directory at $DATA_ROOT"
umask 077
repair_runtime
normalize_boot_hash

if [ -f "$MODPATH/play-integrity/pif-engine" ]; then
  chmod 0755 "$MODPATH/play-integrity/pif-engine"
  PIF_ENGINE_MODULE="$MODPATH/play-integrity" \
    "$MODPATH/play-integrity/pif-engine" stage setup >/dev/null 2>&1 \
    || abort "! Play Integrity engine setup failed."
fi

ui_print " "
ui_print "! Duck ToolBox is not part of Tricky Store, TEESimulator or OhMyKeymint."
ui_print "  Do not report its issues to those projects."
if [ "$MODULE_ID" = "duck-toolbox" ] && [ -f "$MODPATH/device-faker/device-faker-engine" ]; then
  # shellcheck source=device-faker.sh
  . "$MODPATH/device-faker.sh"
  setup_device_faker || abort "! Device Faker payload setup failed."
  ui_print "- Embedded Device Faker prepared; reboot before enabling it in the WebUI."
fi
ui_print "- Installation complete."
ui_print " "
