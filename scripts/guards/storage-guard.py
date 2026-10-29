#!/usr/bin/env python3
"""Check free storage before a large write; this command never deletes files."""
import argparse
import math
import os
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--require-gib", type=float, default=0, help="Peak additional storage needed")
parser.add_argument("--volume", type=Path, help="Destination directory (default: internal and mounted external disks)")
parser.add_argument("--authorized-reserve-gib", type=float,
                    help="Explicit user-authorized reserve for this invocation and named volume only")
args = parser.parse_args()
if not math.isfinite(args.require_gib) or args.require_gib < 0:
    parser.error("--require-gib must be finite and nonnegative")
if args.volume is not None and not args.volume.is_dir():
    parser.error("--volume must name an existing directory")
if args.authorized_reserve_gib is not None and (args.volume is None
        or not math.isfinite(args.authorized_reserve_gib) or args.authorized_reserve_gib < 0):
    parser.error("--authorized-reserve-gib requires a named volume and a finite nonnegative reserve")
GIB = 1024 ** 3
internal_devices = {os.stat(p).st_dev for p in ("/", "/System/Volumes/Data") if os.path.exists(p)}
volumes = [args.volume] if args.volume is not None else [Path("/")] + [
    p for p in Path("/Volumes").iterdir()
    if p.is_dir() and not p.is_symlink() and os.path.ismount(p)
]
failed = False
for volume in volumes:
    disk = os.statvfs(volume)
    free = disk.f_bavail * disk.f_frsize / GIB
    capacity = disk.f_blocks * disk.f_frsize / GIB
    reserve = max(capacity * 0.10, 50 if os.stat(volume).st_dev in internal_devices else 20)
    if args.authorized_reserve_gib is not None:
        minimum = 50 if os.stat(volume).st_dev in internal_devices else 20
        if args.authorized_reserve_gib < minimum:
            parser.error(f"An explicit reserve must still keep at least {minimum} GiB on this volume")
        reserve = args.authorized_reserve_gib
    remaining = free - args.require_gib
    ok = remaining >= reserve
    failed |= not ok
    print(f"{'OK' if ok else 'STOP'} {volume}: {free:.1f} GiB free; reserve {reserve:.1f}; planned peak {args.require_gib:.1f}; remaining {remaining:.1f}")
raise SystemExit(2 if failed else 0)
