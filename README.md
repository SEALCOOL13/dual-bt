# dual-bt

Use the same Bluetooth devices in Windows and Linux on a dual-boot PC without re-pairing every time you switch.

Each time you pair a device, it gets a new secret key. If you pair in Linux and then in Windows, only Windows has the current key, so the device won't connect in Linux. `dual-bt` reads the key from your Windows install and copies it into Linux.

Example output (shortened):

```
$ ./dual-bt
* Found Windows install: /mnt/Windows
  replayed transaction logs: 3 dirty page(s) recovered
- controller AA:BB:CC:00:00:01
    11:22:33:44:55:02  Example Earbuds
        linux  LK 0123456789ABCDEF0123456789ABCDEF
        windowsLK CAFEBABECAFEBABECAFEBABECAFEBABE
    11:22:33:44:55:04  Test Gamepad
        linux: not paired -> will create a new BlueZ entry
Planned changes:
  * 11:22:33:44:55:02 Example Earbuds: set LinkKey to Windows key
  * 11:22:33:44:55:04 Test Gamepad: create BLE pairing from Windows keys
Dry run only. Re-run with --apply to make these changes.
```

## Quick start

1. **Pair in Linux, then in Windows.** Pair every device in Linux first. Reboot into Windows and pair them there too, then check they work.
2. **Shut Windows down fully.** In an admin terminal, run `shutdown /s /t 0`. Turning off Fast Startup is recommended; see [Troubleshooting](#troubleshooting).
3. **Boot Linux and mount the Windows partition.** Opening it in your file manager is enough.
4. **Run:**

   ```bash
   git clone https://github.com/SEALCOOL13/dual-bt
   cd dual-bt
   ./dual-bt            # dry run: shows what would change
   ./dual-bt --apply    # backs up, asks y/N per device, applies
   ```

5. Connect, e.g. `bluetoothctl connect 11:22:33:44:55:02`.

Don't pair those devices in Linux again afterwards. Re-pairing creates a new key and breaks them in Windows.

**Requirements:** Linux with BlueZ (standard on most distros), `python3` with `venv`, and `sudo`. The first run installs [regipy](https://github.com/mkorman90/regipy) into a private folder, `~/.cache/dual-bt/venv`. Nothing is installed system-wide.

## Options

| Option | Meaning |
|---|---|
| *(none)* | Dry run. Shows each device and what would change. |
| `--apply` | Make the changes (backup first, then y/N per device). |
| `--yes` | Don't ask per device (with `--apply`). |
| `--windows-root PATH` | Windows partition mount point. Auto-detected in `/mnt`, `/media`, `/run/media` if omitted. |
| `--bluez-root PATH` | BlueZ storage folder. Default `/var/lib/bluetooth`. |
| `--no-service` | Don't stop/start `bluetooth.service` around the changes. |

## Safety

- **The Windows drive is only read.** `SYSTEM` and its logs are copied into `~/dual-bt-backups/<date>/` and processed there.
- **Nothing is deleted.**
- **Backup first.** Before any change, `/var/lib/bluetooth` is saved to `~/dual-bt-backups/<date>/bluez-backup-*.tar.gz`. The exact undo command is printed at the end:

  ```bash
  sudo systemctl stop bluetooth && sudo tar -xzf ~/dual-bt-backups/<date>/bluez-backup-<date>.tar.gz -C / && sudo systemctl start bluetooth
  ```

  Restoring doesn't remove newly created BLE device folders. Remove those from your Bluetooth settings if you don't want them.
- **Small edits only.** For classic devices, only the `Key=` line under `[LinkKey]` changes. File ownership and permissions are kept.

## How it works

Windows keeps Bluetooth keys in the registry, at `HKLM\SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Keys\<controller>`. Linux (BlueZ) keeps them in `/var/lib/bluetooth/<controller>/<device>/info`.

1. **Replays the registry change logs.** Windows often holds recent changes in `SYSTEM.LOG1`/`SYSTEM.LOG2` before merging them into the main `SYSTEM` file. Tools that read only `SYSTEM` (e.g. `chntpw`/`reged`) then report recently paired devices as "not found in Windows". dual-bt replays the logs on the copies first, falls back to the plain file if that fails, and combines the results.
2. **Reads the active control set** (`Select\Current`) instead of assuming `ControlSet001`.
3. **Converts the keys:**

   | Device type | Windows | BlueZ |
   |---|---|---|
   | Classic (headphones, speakers) | value `<device mac>` | `[LinkKey] Key` (same bytes) |
   | BLE | `LTK` | `[LongTermKey] Key` (same bytes) |
   | BLE | `KeyLength` | `EncSize` |
   | BLE | `EDIV` | `EDiv` (decimal) |
   | BLE | `ERand` | `Rand` (decimal, 64-bit) |
   | BLE | `IRK` | `[IdentityResolvingKey] Key` (byte order **reversed**) |
   | BLE | `CSRK` | `[LocalSignatureKey] Key` |
   | BLE | `AuthReq` bit 2, plus whether `EDIV`/`ERand` are 0 (Secure Connections) | `Authenticated` (0–3) |

## Troubleshooting

**"windows: not found" for a device you paired in Windows.** Windows hasn't saved that pairing to disk yet; the newest state can be held back by Fast Startup. In Windows, go to Control Panel → Power Options → *Choose what the power buttons do* and uncheck *Turn on fast startup*. Run `shutdown /s /t 0` from an admin terminal, then run dual-bt again.

**"linux: no [LinkKey]".** The device was never paired in Linux. Pair it once in Linux, then once in Windows, then run dual-bt again.

**BLE device (mouse, gamepad) doesn't connect after `--apply`.** BLE support is newer and harder to get right than classic. The `Authenticated` and `AddressType` values are inferred from the Windows data, which may not match your device. Undo with the backup, or remove the device and pair it once in Linux. Then pair it in Windows again and re-run.

**Several Windows installs or mount points.** dual-bt lists them and asks you to pick, or you can pass `--windows-root`.

## What about Linux → Windows?

dual-bt only copies **Windows → Linux**, on purpose:

- Linux → Windows means writing to the Windows registry from outside Windows. If Windows has unsaved changes in its logs, it applies them at next boot and can overwrite your edit, or leave the registry inconsistent. Writing safely needs the logs merged first, and an offline edit gone wrong can stop Windows from booting.
- You never need that direction. Pair in Linux first, Windows last, then run dual-bt. Pairing in Windows is always the final step.

To do it anyway, [bt-keys-sync](https://github.com/KeyofBlueS/bt-keys-sync) can write keys into the Windows hive. Shut Windows down fully (Fast Startup off) first, and back up `Windows/System32/config/SYSTEM` before you try.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover tests -v
```

The tests use a made-up hive at `tests/fixtures/SYSTEM` (rebuild it with `tests/fixtures/build_fixture.sh`) and a temporary fake `/var/lib/bluetooth`. They never touch real system files.

## Credits

Inspired by [bt-keys-sync](https://github.com/KeyofBlueS/bt-keys-sync) by KeyofBlueS. The BLE key mapping follows the [Arch Wiki dual-boot pairing guide](https://wiki.archlinux.org/title/Bluetooth#Dual_boot_pairing).

## License

GPL-2.0. See [LICENSE](LICENSE).
