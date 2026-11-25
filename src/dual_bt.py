#!/usr/bin/env python3
"""
dual-bt: copy Bluetooth pairing keys from a Windows SYSTEM hive into BlueZ (Linux),
so devices paired in Windows also work in Linux on a dual-boot machine.

Safety:
  * Windows files are opened read-only. Copies go into a work directory.
  * Nothing is deleted. Linux files are only changed with --apply, after a
    full tar.gz backup of the BlueZ directory and a y/N prompt per device.
"""
import argparse
import configparser
import datetime as dt
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

try:
    from regipy.registry import RegistryHive
    from regipy.recovery import apply_transaction_logs
except ImportError:
    sys.exit("regipy is not installed. Run this through the ./dual-bt launcher.")

HEX12 = re.compile(r"^[0-9a-fA-F]{12}$")
MAC = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


# ----------------------------------------------------------------- helpers
def mac_colon(h):
    h = h.upper()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2))


def as_bytes(v):
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    if isinstance(v, str):  # regipy may hand back hex strings
        return bytes.fromhex(v.replace(",", "").replace(" ", ""))
    raise TypeError(f"unexpected value type {type(v)}")


def as_int(v):
    if isinstance(v, int):
        return v
    b = as_bytes(v)
    return int.from_bytes(b, "little")


def hexs(b):
    return b.hex().upper()


def find_ci(directory, name):
    """Case-insensitive file lookup (NTFS mounts can be case-sensitive)."""
    try:
        for entry in os.listdir(directory):
            if entry.lower() == name.lower():
                return os.path.join(directory, entry)
    except FileNotFoundError:
        pass
    return None


def say(msg=""):
    print(msg, flush=True)


# ------------------------------------------------------- windows side
SEARCH_DIRS = ("/mnt", "/media", "/run/media")


def windows_config_dir(root):
    """Return <root>/Windows/System32/config if it holds a SYSTEM hive, else None."""
    p = root
    for part in ("Windows", "System32", "config"):
        p = find_ci(p, part)
        if not p:
            return None
    return p if find_ci(p, "SYSTEM") else None


def find_windows_roots():
    """Find mounted Windows installs (up to 3 levels below the usual mount dirs)."""
    found = []
    for base in SEARCH_DIRS:
        for root, dirs, _ in os.walk(base):
            depth = root[len(base):].count(os.sep)
            if windows_config_dir(root):
                found.append(root)
                dirs[:] = []
                continue
            if depth >= 3:
                dirs[:] = []
    return sorted(set(found))


def pick_windows_root(given):
    if given:
        return given
    roots = find_windows_roots()
    if not roots:
        sys.exit("No mounted Windows install found in /mnt, /media or /run/media.\n"
                 "Mount the Windows partition (open it in your file manager), or pass --windows-root PATH.")
    if len(roots) == 1:
        say(f"* Found Windows install: {roots[0]}")
        return roots[0]
    say("* Several Windows installs found:")
    for i, r in enumerate(roots, 1):
        say(f"  {i}) {r}")
    while True:
        c = input("  choose> ").strip()
        if c.isdigit() and 1 <= int(c) <= len(roots):
            return roots[int(c) - 1]


def load_hives(windows_root, work_dir):
    """Copy SYSTEM (+ logs) to work_dir, try to replay the logs.
    Returns list of (label, path) to read, best first."""
    cfg = windows_config_dir(windows_root)
    if not cfg:
        sys.exit(f"No Windows/System32/config/SYSTEM under {windows_root}")

    say(f"* Windows config dir: {cfg}")
    copies = {}
    for name in ("SYSTEM", "SYSTEM.LOG1", "SYSTEM.LOG2"):
        src = find_ci(cfg, name)
        if src and os.path.getsize(src) > 0:
            dst = os.path.join(work_dir, name)
            shutil.copyfile(src, dst)  # read-only on the source
            copies[name] = dst
            say(f"  copied {name} ({os.path.getsize(src)} bytes)")
        else:
            say(f"  {name}: not present or empty")

    if find_ci(windows_root, "hiberfil.sys"):
        say("  note: hiberfil.sys exists (Windows Fast Startup/hibernate may hold newer data)")

    hives = []
    hive = RegistryHive(copies["SYSTEM"])
    dirty = hive.header.primary_sequence_num != hive.header.secondary_sequence_num
    say(f"  hive header is {'DIRTY (unflushed changes in logs)' if dirty else 'clean'}")

    logs = [copies.get("SYSTEM.LOG1"), copies.get("SYSTEM.LOG2")]
    logs = [l for l in logs if l]
    if logs:
        restored = os.path.join(work_dir, "SYSTEM.recovered")
        attempts = []
        if len(logs) == 2:
            attempts.append((logs[0], logs[1]))
            attempts.append((logs[1], logs[0]))
        attempts += [(l, None) for l in logs]
        for primary, secondary in attempts:
            try:
                _, pages = apply_transaction_logs(copies["SYSTEM"], primary, secondary_log_path=secondary,
                                                  restored_hive_path=restored)
                RegistryHive(restored)  # make sure it parses
                say(f"  replayed transaction logs: {pages} dirty page(s) recovered")
                hives.append(("SYSTEM + transaction logs", restored))
                break
            except Exception as e:  # noqa: BLE001
                say(f"  log replay ({os.path.basename(primary)}"
                    f"{' + ' + os.path.basename(secondary) if secondary else ''}) failed: {e}")
    hives.append(("SYSTEM (as on disk)", copies["SYSTEM"]))
    return hives


def read_windows(hive_path):
    """Return {controller_hex: {device_hex: info}}"""
    hive = RegistryHive(hive_path)
    current = 1
    try:
        current = as_int(hive.get_key("\\Select").get_value("Current")) or 1
    except Exception:  # noqa: BLE001
        pass
    cs = f"ControlSet{current:03d}"
    base = f"\\{cs}\\Services\\BTHPORT\\Parameters"
    out = {}
    try:
        keys = hive.get_key(base + "\\Keys")
    except Exception:  # noqa: BLE001
        return cs, out

    names = {}
    try:
        for dev in hive.get_key(base + "\\Devices").iter_subkeys():
            n = dev.get_value("Name")
            if n is not None:
                try:
                    names[dev.name.lower()] = as_bytes(n).split(b"\0")[0].decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    names[dev.name.lower()] = str(n)
    except Exception:  # noqa: BLE001
        pass

    for ctrl in keys.iter_subkeys():
        if not HEX12.match(ctrl.name):
            continue
        devs = {}
        for v in ctrl.iter_values(trim_values=False):
            if HEX12.match(v.name):
                devs[v.name.lower()] = {"type": "classic", "lk": as_bytes(v.value)}
        for sub in ctrl.iter_subkeys():
            if not HEX12.match(sub.name):
                continue
            vals = {v.name: v.value for v in sub.iter_values(trim_values=False)}
            d = {"type": "ble"}
            if "LTK" in vals:
                d["ltk"] = as_bytes(vals["LTK"])
            if "IRK" in vals:
                d["irk"] = as_bytes(vals["IRK"])
            if "CSRK" in vals:
                d["csrk"] = as_bytes(vals["CSRK"])
            d["keylen"] = as_int(vals.get("KeyLength", 16))
            d["ediv"] = as_int(vals.get("EDIV", 0))
            d["rand"] = as_int(vals.get("ERand", 0))
            d["authreq"] = as_int(vals.get("AuthReq", 0))
            d["addrtype"] = as_int(vals.get("AddressType", 0))
            if "Address" in vals:
                a = as_int(vals["Address"])
                d["address"] = f"{a:012x}"
            devs[sub.name.lower()] = d
        for k, d in devs.items():
            d["name"] = names.get(k, "")
        out[ctrl.name.lower()] = devs
    return cs, out


# --------------------------------------------------------- linux side
def read_info(path):
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    cp.optionxform = str
    try:
        cp.read(path)
    except configparser.Error:
        pass
    return cp


def set_key_in_section(text, section, key, value):
    """Change one key inside one [section] of an ini text, keep everything else."""
    lines = text.splitlines(keepends=True)
    cur = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            cur = s[1:-1]
            continue
        if cur == section and s.split("=", 1)[0].strip() == key:
            nl = "\n" if line.endswith("\n") else ""
            lines[i] = f"{key}={value}{nl}"
            return "".join(lines), True
    return text, False


def write_preserving(path, content, mode=0o600, uid=0, gid=0):
    """Write via a temp file in the same dir, then atomically rename over."""
    st = None
    if os.path.exists(path):
        st = os.stat(path)
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".info.", dir=d)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    os.chmod(tmp, st.st_mode & 0o7777 if st else mode)
    try:
        os.chown(tmp, st.st_uid if st else uid, st.st_gid if st else gid)
    except PermissionError:
        pass
    os.replace(tmp, path)


def ble_info_text(name, d):
    sc = d["ediv"] == 0 and d["rand"] == 0  # LE Secure Connections
    mitm = bool(d["authreq"] & 0x04)
    if sc:
        auth = 3 if mitm else 2  # MGMT_LTK_P256_AUTH / P256_UNAUTH
    else:
        auth = 1 if mitm else 0
    addrtype = "static" if d["addrtype"] else "public"
    out = io.StringIO()
    out.write("[General]\n")
    if name:
        out.write(f"Name={name}\n")
    out.write(f"AddressType={addrtype}\n")
    out.write("SupportedTechnologies=LE;\n")
    out.write("Trusted=true\nBlocked=false\n\n")
    if "irk" in d:
        out.write("[IdentityResolvingKey]\n")
        out.write(f"Key={hexs(d['irk'][::-1])}\n\n")  # Windows stores IRK reversed
    if "csrk" in d:
        out.write("[LocalSignatureKey]\n")
        out.write(f"Key={hexs(d['csrk'])}\nCounter=0\nAuthenticated=false\n\n")
    groups = ["LongTermKey"] if sc else ["LongTermKey", "PeripheralLongTermKey"]
    for g in groups:
        out.write(f"[{g}]\n")
        out.write(f"Key={hexs(d['ltk'])}\n")
        out.write(f"Authenticated={auth}\n")
        out.write(f"EncSize={d['keylen']}\n")
        out.write(f"EDiv={d['ediv']}\n")
        out.write(f"Rand={d['rand']}\n\n")
    return out.getvalue()


# ----------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(prog="dual-bt", description="Copy Bluetooth pairing keys from Windows to Linux.")
    ap.add_argument("--windows-root", help="mount point of the Windows partition (auto-detected if omitted)")
    ap.add_argument("--bluez-root", default="/var/lib/bluetooth")
    ap.add_argument("--work-dir", required=True, help="where hive copies and backups go")
    ap.add_argument("--apply", action="store_true", help="actually change the Linux pairing files (default: dry run)")
    ap.add_argument("--yes", action="store_true", help="don't prompt per device")
    ap.add_argument("--no-service", action="store_true", help="don't stop/start bluetooth.service")
    a = ap.parse_args()

    os.makedirs(a.work_dir, exist_ok=True)
    hives = load_hives(pick_windows_root(a.windows_root), a.work_dir)

    # Merge: prefer the first hive (recovered) per device, fill gaps from others.
    win = {}
    for label, path in hives:
        cs, data = read_windows(path)
        total = sum(len(v) for v in data.values())
        say(f"  {label}: {cs}, {len(data)} controller(s), {total} device(s)")
        for c, devs in data.items():
            for m, d in devs.items():
                if m not in win.setdefault(c, {}):
                    d["source"] = label
                    win[c][m] = d

    if not os.path.isdir(a.bluez_root):
        sys.exit(f"{a.bluez_root} not found")
    linux_ctrls = [e for e in os.listdir(a.bluez_root) if MAC.match(e)]

    actions = []  # (description, callable)
    missing_in_windows = []
    say()
    for lc in sorted(linux_ctrls):
        chex = lc.replace(":", "").lower()
        say(f"- controller {lc}" + ("" if chex in win else "   (NOT in Windows registry)"))
        wdevs = win.get(chex, {})
        ldevs = sorted(e for e in os.listdir(os.path.join(a.bluez_root, lc)) if MAC.match(e))
        allmacs = sorted(set(m.replace(":", "").lower() for m in ldevs) | set(wdevs))
        for m in allmacs:
            mc = mac_colon(m)
            info_path = os.path.join(a.bluez_root, lc, mc, "info")
            lin = read_info(info_path) if os.path.exists(info_path) else None
            w = wdevs.get(m)
            name = (lin.get("General", "Alias", fallback=None) or lin.get("General", "Name", fallback="")) if lin else ""
            name = name or (w or {}).get("name", "") or "?"
            say(f"    {mc}  {name}")
            if not w:
                say("        windows: not found")
                missing_in_windows.append(f"{mc} {name}")
                continue
            say(f"        windows: {w['type']} key (from {w['source']})")
            if w["type"] == "classic":
                if lin is None or not lin.has_section("LinkKey"):
                    say("        linux:   no [LinkKey] -> pair it once in Linux first, then re-run")
                    continue
                cur = lin.get("LinkKey", "Key", fallback="").upper()
                new = hexs(w["lk"])
                say(f"        linux  LK {cur}\n        windowsLK {new}")
                if cur == new:
                    say("        already in sync")
                    continue

                def do(info_path=info_path, new=new):
                    with open(info_path) as f:
                        txt = f.read()
                    txt2, ok = set_key_in_section(txt, "LinkKey", "Key", new)
                    if not ok:
                        raise RuntimeError("Key= line not found in [LinkKey]")
                    write_preserving(info_path, txt2)
                actions.append((f"{mc} {name}: set LinkKey to Windows key", do))
            else:
                if "ltk" not in w:
                    say("        windows entry has no LTK, skipping")
                    continue
                say(f"        LTK {hexs(w['ltk'])}  EDIV {w['ediv']}  Rand {w['rand']}"
                    f"  IRK {'yes' if 'irk' in w else 'no'}")
                if w.get("address") and w["address"] != m:
                    say(f"        note: Windows identity address is {mac_colon(w['address'])}")
                if lin is not None:
                    cur = lin.get("LongTermKey", "Key", fallback="").upper()
                    if cur == hexs(w["ltk"]):
                        say("        already in sync")
                        continue
                    say("        linux: existing entry with a different LTK")

                    def do(info_path=info_path, w=w):
                        with open(info_path) as f:
                            txt = f.read()
                        for sec, key, val in (("LongTermKey", "Key", hexs(w["ltk"])),
                                              ("LongTermKey", "EDiv", str(w["ediv"])),
                                              ("LongTermKey", "Rand", str(w["rand"])),
                                              ("LongTermKey", "EncSize", str(w["keylen"])),
                                              ("PeripheralLongTermKey", "Key", hexs(w["ltk"])),
                                              ("PeripheralLongTermKey", "EDiv", str(w["ediv"])),
                                              ("PeripheralLongTermKey", "Rand", str(w["rand"])),
                                              ("IdentityResolvingKey", "Key",
                                               hexs(w["irk"][::-1]) if "irk" in w else None)):
                            if val is not None:
                                txt, _ = set_key_in_section(txt, sec, key, val)
                        write_preserving(info_path, txt)
                    actions.append((f"{mc} {name}: update BLE keys from Windows", do))
                else:
                    say("        linux: not paired -> will create a new BlueZ entry")

                    def do(lc=lc, mc=mc, name=name, w=w):
                        d = os.path.join(a.bluez_root, lc, mc)
                        os.makedirs(d, mode=0o700, exist_ok=True)
                        write_preserving(os.path.join(d, "info"), ble_info_text(name, w))
                    actions.append((f"{mc} {name}: create BLE pairing from Windows keys", do))

    say()
    if missing_in_windows:
        say("! Still missing from the Windows registry: " + ", ".join(missing_in_windows))
        say("  Windows has not written these pairings to disk. In Windows: pair them, turn off")
        say("  Fast Startup (Control Panel > Power Options), run `shutdown /s /t 0`, then re-run.")
        say()

    if not actions:
        say("Nothing to change.")
        return
    say("Planned changes:")
    for desc, _ in actions:
        say(f"  * {desc}")
    if not a.apply:
        say("\nDry run only. Re-run with --apply to make these changes.")
        return

    # Backup first, always.
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(a.work_dir, f"bluez-backup-{stamp}.tar.gz")
    with tarfile.open(backup, "w:gz") as t:
        t.add(a.bluez_root, arcname=a.bluez_root.lstrip("/"))
    say(f"\nBackup of {a.bluez_root}: {backup}")

    chosen = []
    for desc, fn in actions:
        if a.yes or input(f"Apply: {desc}? [y/N] ").strip().lower() == "y":
            chosen.append((desc, fn))
    if not chosen:
        say("Nothing applied.")
        return

    if not a.no_service:
        subprocess.run(["systemctl", "stop", "bluetooth"], check=False)
    try:
        for desc, fn in chosen:
            fn()
            say(f"  done: {desc}")
    finally:
        if not a.no_service:
            subprocess.run(["systemctl", "start", "bluetooth"], check=False)

    say(f"\nTo undo: sudo systemctl stop bluetooth && sudo tar -xzf '{backup}' -C / "
        f"&& sudo systemctl start bluetooth")
    say("Then connect, e.g.:  bluetoothctl connect <MAC>")


if __name__ == "__main__":
    main()
