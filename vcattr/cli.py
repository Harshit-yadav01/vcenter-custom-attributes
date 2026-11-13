"""Command line: backup, failover, restore, show."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime
from pathlib import Path

from vcattr.core import Snapshot, build_plan, load_name_map, resolve_types

DESCRIPTION = """\
Back up vCenter custom attributes and put them back on another vCenter.

  backup    read attribute definitions and values from a vCenter into a JSON file
  failover  apply a backup of the primary vCenter to the secondary vCenter
  restore   apply a backup to a vCenter again (back to the primary after failback,
            or the same vCenter after attributes were lost)
  show      print what a backup file contains (no vCenter needed)

failover and restore only show the plan unless you add --apply.
"""


def _add_connection(p: argparse.ArgumentParser) -> None:
    p.add_argument("--vcenter", required=True, help="vCenter host name or IP")
    p.add_argument("--user", required=True, help="for example administrator@vsphere.local")
    p.add_argument("--port", type=int, default=443)
    p.add_argument(
        "--password-env", default="VCENTER_PASSWORD", metavar="VAR",
        help="environment variable that holds the password (default VCENTER_PASSWORD); "
             "if it is not set you are prompted",
    )
    p.add_argument("--insecure", action="store_true", help="do not verify the vCenter certificate")
    p.add_argument(
        "--types", help="only these object types, comma separated, for example vm,host,cluster",
    )


def _add_apply(p: argparse.ArgumentParser) -> None:
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument("--backup", type=Path, help="backup file from the backup command")
    source.add_argument(
        "--source-vcenter", help="read the current attributes directly from this source vCenter",
    )
    p.add_argument("--source-user", help="username for --source-vcenter")
    p.add_argument("--source-port", type=int, default=443, help="source vCenter port (default 443)")
    p.add_argument(
        "--source-password-env", default="VCENTER_SOURCE_PASSWORD", metavar="VAR",
        help="environment variable for the source password (default VCENTER_SOURCE_PASSWORD)",
    )
    p.add_argument(
        "--source-insecure", action="store_true",
        help="do not verify the source vCenter certificate",
    )
    p.add_argument("--apply", action="store_true", help="make the changes (default: plan only)")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation with --apply")
    p.add_argument(
        "--overwrite", action="store_true",
        help="replace values that already exist on the target with the backup value",
    )
    p.add_argument(
        "--name-map", type=Path, metavar="CSV",
        help="CSV with columns type,source_name,target_name for objects renamed on the target",
    )
    p.add_argument(
        "--backup-dir", type=Path, default=Path("backups"),
        help="where the safety backup of the target is written before --apply (default backups)",
    )
    p.add_argument("--verbose", action="store_true", help="also list values that are already correct")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vcattr", description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backup", help="back up custom attributes from a vCenter")
    _add_connection(p)
    p.add_argument("--out", type=Path, help="output file (default backups/<vcenter>-<time>.json)")

    p = sub.add_parser("failover", help="apply a primary backup to the secondary vCenter")
    _add_connection(p)
    _add_apply(p)

    p = sub.add_parser("restore", help="apply a backup to a vCenter (for example after failback)")
    _add_connection(p)
    _add_apply(p)

    p = sub.add_parser("show", help="print what a backup file contains")
    p.add_argument("backup", type=Path)
    return parser


def _password(env_name: str, user: str, host: str) -> str:
    pw = os.environ.get(env_name)
    if pw:
        return pw
    if not sys.stdin.isatty():
        raise SystemExit(f"Set {env_name} or run in a terminal to be prompted for the password.")
    return getpass.getpass(f"Password for {user} on {host}: ")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _safe(host: str) -> str:
    return "".join(c if c.isalnum() or c in "-." else "_" for c in host)


def cmd_backup(args) -> int:
    from vcattr import vcenter

    types = resolve_types(args.types)
    si = vcenter.connect(
        args.vcenter, args.user, _password(args.password_env, args.user, args.vcenter),
        args.port, args.insecure,
    )
    try:
        snap = vcenter.read_snapshot(si, args.vcenter, types).filtered(types)
    finally:
        vcenter.disconnect(si)
    out = args.out or Path("backups") / f"{_safe(args.vcenter)}-{_stamp()}.json"
    snap.save(out)
    with_values = [o for o in snap.objects if o.values]
    count = sum(len(o.values) for o in with_values)
    print(f"Backed up {len(snap.fields)} attribute definition(s) and {count} value(s) "
          f"on {len(with_values)} object(s) from {args.vcenter}.")
    print(f"Saved to {out}")
    return 0


def cmd_apply(args) -> int:
    from vcattr import vcenter

    types = resolve_types(args.types)
    if args.source_vcenter:
        if not args.source_user:
            raise ValueError("--source-user is required when using --source-vcenter")
        source_si = vcenter.connect(
            args.source_vcenter, args.source_user,
            _password(args.source_password_env, args.source_user, args.source_vcenter),
            args.source_port, args.source_insecure,
        )
        try:
            backup = vcenter.read_snapshot(source_si, args.source_vcenter, types).filtered(types)
        finally:
            vcenter.disconnect(source_si)
        source_description = (
            f"Source: {args.source_vcenter} (read live at {backup.taken_at})"
        )
    else:
        backup = Snapshot.load(args.backup).filtered(types)
        source_description = f"Backup: {args.backup} (taken {backup.taken_at} from {backup.vcenter})"
    name_map = load_name_map(args.name_map)

    si = vcenter.connect(
        args.vcenter, args.user, _password(args.password_env, args.user, args.vcenter),
        args.port, args.insecure,
    )
    try:
        target = vcenter.read_snapshot(si, args.vcenter, types)
        plan = build_plan(backup, target, overwrite=args.overwrite, name_map=name_map)
        print(source_description)
        print(plan.to_text(verbose=args.verbose))
        print()

        if not plan.has_changes:
            if plan.has_problems:
                print("Nothing can be changed automatically. Check the conflicts and unmatched "
                      "objects above (--overwrite, --name-map).")
                return 3
            print(f"{args.vcenter} already carries every attribute in the backup. Nothing to do.")
            return 0
        if not args.apply:
            print(f"This was a plan only. Run again with --apply to make the {args.command} changes.")
            return 0

        if not args.yes:
            if not sys.stdin.isatty():
                print("Refusing to apply without a terminal. Add --yes to confirm.")
                return 1
            try:
                answer = input(f"Type the target vCenter name ({args.vcenter}) to apply: ").strip()
            except EOFError:
                answer = ""
            if answer != args.vcenter:
                print("Not confirmed. Nothing was changed.")
                return 1

        safety = args.backup_dir / f"{_safe(args.vcenter)}-before-{args.command}-{_stamp()}.json"
        target.save(safety)
        print(f"Saved the target's current attributes to {safety} (use it with restore to undo).")

        result = vcenter.apply_plan(si, plan, target)
    finally:
        vcenter.disconnect(si)

    if result.errors:
        print(f"\nFinished with {len(result.errors)} error(s):")
        for e in result.errors:
            print(f"  {e}")
        return 2
    print(f"\nDone. Run the same command without --apply to check that {args.vcenter} now matches.")
    if plan.has_problems:
        print("Some items were not changed: see the conflicts and unmatched objects above.")
        return 3
    return 0


def cmd_show(args) -> int:
    snap = Snapshot.load(args.backup)
    print(f"vCenter:  {snap.vcenter} ({snap.about})")
    print(f"Taken:    {snap.taken_at}")
    print(f"Attributes ({len(snap.fields)}):")
    for f in snap.fields:
        used = sum(1 for o in snap.objects if f.name in o.values)
        print(f"  {f.name}  [{f.type or 'Global'}]  on {used} object(s)")
    print(f"Objects with values ({len(snap.objects)}):")
    for o in snap.objects:
        vals = ", ".join(f"{k}={v}" for k, v in sorted(o.values.items()))
        print(f"  {o.type} {o.path}: {vals}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from vcattr.vcenter import VCenterError

    try:
        if args.command == "backup":
            return cmd_backup(args)
        if args.command in ("failover", "restore"):
            return cmd_apply(args)
        return cmd_show(args)
    except VCenterError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
