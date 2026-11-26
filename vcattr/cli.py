"""Command line: backup, failover, restore, show."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from vcattr.core import Snapshot, build_plan, load_name_map, resolve_types

VM_TYPES = {"VirtualMachine"}

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
    p.add_argument("--vm", help="back up only this VM (exact name or full inventory path)")

    p = sub.add_parser("failover", help="apply a primary backup to the secondary vCenter")
    _add_connection(p)
    _add_apply(p)
    p.add_argument("--vm", help="apply a single-VM backup to this exact VM name or inventory path")

    p = sub.add_parser("restore", help="apply a backup to a vCenter (for example after failback)")
    _add_connection(p)
    _add_apply(p)
    destination = p.add_mutually_exclusive_group()
    destination.add_argument("--vm", help="apply a single-VM backup to this exact VM name or inventory path")
    destination.add_argument(
        "--original-backup", type=Path,
        help="single-VM backup identifying the original VM to restore to",
    )
    p.add_argument(
        "--delete-target-definitions", action="store_true",
        help="after a successful single-VM restore, delete definitions in either backup from "
             "the cleanup vCenter (removes their values from all applicable objects there)",
    )
    p.add_argument(
        "--cleanup-vcenter",
        help="target vCenter to clean up; must match the source recorded in --backup",
    )
    p.add_argument("--cleanup-user", help="username for the target definition cleanup connection")
    p.add_argument("--cleanup-port", type=int, default=443, help="cleanup target vCenter port")
    p.add_argument(
        "--cleanup-password-env", default="VCENTER_CLEANUP_PASSWORD", metavar="VAR",
        help="environment variable for cleanup target password (default VCENTER_CLEANUP_PASSWORD)",
    )
    p.add_argument(
        "--cleanup-insecure", action="store_true",
        help="do not verify the cleanup target vCenter certificate",
    )

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


def _vm_types(types: set[str] | None) -> set[str]:
    if types is not None and types != VM_TYPES:
        raise ValueError("Single-VM operations only support --types vm.")
    return VM_TYPES


def cmd_backup(args) -> int:
    from vcattr import vcenter

    types = resolve_types(args.types)
    if args.vm is not None:
        types = _vm_types(types)
    si = vcenter.connect(
        args.vcenter, args.user, _password(args.password_env, args.user, args.vcenter),
        args.port, args.insecure,
    )
    try:
        snap = vcenter.read_snapshot(si, args.vcenter, types).filtered(types)
        if args.vm is not None:
            snap = snap.for_vm(snap.select_vm(args.vm))
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

    cleanup_enabled = getattr(args, "delete_target_definitions", False)
    if cleanup_enabled and args.command != "restore":
        raise ValueError("--delete-target-definitions is only available with restore.")
    if cleanup_enabled and (
        not getattr(args, "original_backup", None)
        or not getattr(args, "cleanup_vcenter", None)
        or not getattr(args, "cleanup_user", None)
    ):
        raise ValueError(
            "--delete-target-definitions requires --original-backup, --cleanup-vcenter, "
            "and --cleanup-user."
        )
    if cleanup_enabled and args.apply:
        if not args.yes and not sys.stdin.isatty():
            print(
                "Refusing failback with target cleanup without a terminal. "
                "Run interactively or add --yes."
            )
            return 1
        if not sys.stdin.isatty() and not os.environ.get(args.cleanup_password_env):
            print(
                f"Set {args.cleanup_password_env} before failback cleanup when running "
                "without a terminal."
            )
            return 1

    types = resolve_types(args.types)
    original_path = getattr(args, "original_backup", None)
    vm_mode = args.vm is not None or original_path is not None
    original_vm = None
    if vm_mode:
        types = _vm_types(types)
        if args.name_map is not None or args.backup is None:
            raise ValueError("Single-VM operations require --backup and cannot use --name-map.")
    if original_path is not None:
        original = Snapshot.load(original_path)
        if not original.vm_scoped:
            raise ValueError("--original-backup must point to a backup created with backup --vm.")
        if args.vcenter.casefold() != original.vcenter.casefold():
            raise ValueError(f"--original-backup identifies vCenter {original.vcenter!r}, not {args.vcenter!r}.")
        original_vm = original.objects[0]
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
        backup = Snapshot.load(args.backup)
        if backup.vm_scoped != vm_mode:
            raise ValueError(
                "A single-VM backup requires --vm (or restore --original-backup)."
                if backup.vm_scoped else
                "Back up the selected VM with backup --vm before using single-VM mode."
            )
        backup = backup.filtered(types)
        source_description = f"Backup: {args.backup} (taken {backup.taken_at} from {backup.vcenter})"
    if cleanup_enabled:
        if not backup.vm_scoped:
            raise ValueError("--delete-target-definitions requires a single-VM backup.")
        if original is None:
            raise ValueError("--delete-target-definitions requires --original-backup.")
        if args.cleanup_vcenter.casefold() != backup.vcenter.casefold():
            raise ValueError(
                f"--cleanup-vcenter must match the backup source {backup.vcenter!r}."
            )
        if args.cleanup_vcenter.casefold() == args.vcenter.casefold():
            raise ValueError("Cleanup target must be different from the restore destination.")
        cleanup_fields = {field.name: field for field in original.fields}
        for field in backup.fields:
            existing = cleanup_fields.get(field.name)
            if existing is not None and existing.type != field.type:
                raise ValueError(
                    f"Cannot clean up {field.name!r}: definition types differ between backups."
                )
            cleanup_fields[field.name] = field
        if any(field.type not in (None, "VirtualMachine") for field in cleanup_fields.values()):
            raise ValueError("Cleanup supports only Global and VirtualMachine definitions.")
    name_map = load_name_map(args.name_map)

    si = vcenter.connect(
        args.vcenter, args.user, _password(args.password_env, args.user, args.vcenter),
        args.port, args.insecure,
    )
    try:
        target = vcenter.read_snapshot(si, args.vcenter, types)
        safety_snapshot = target
        if vm_mode:
            if original_vm is not None:
                selected = target.select_vm(original_vm.path, path_only=True)
                if original_vm.instance_uuid and selected.instance_uuid != original_vm.instance_uuid:
                    raise ValueError(
                        f"VM at {selected.path!r} does not have the original instance UUID. "
                        "Verify its identity before selecting it explicitly with --vm."
                    )
            else:
                selected = target.select_vm(args.vm)
            print(f"Backup VM: {backup.objects[0].path}")
            print(f"Destination VM: {selected.path}")
            name_map = {("VirtualMachine", backup.objects[0].name): selected.name}
            target = replace(target, objects=[selected])
            safety_snapshot = target.for_vm(selected)
        plan = build_plan(backup, target, overwrite=args.overwrite, name_map=name_map)
        print(source_description)
        print(plan.to_text(verbose=args.verbose))
        print()
        if cleanup_enabled:
            print(
                f"After a successful restore, cleanup will delete these definitions "
                f"from {args.cleanup_vcenter}:"
            )
            for field in cleanup_fields.values():
                print(f"  {field.name} [{field.type or 'Global'}]")
            missing_values = original.objects[0].values.keys() - backup.objects[0].values.keys()
            if missing_values:
                print(
                    "WARNING: these original VM attributes are not in the restore backup "
                    f"and will not be restored: {', '.join(sorted(missing_values))}"
                )
            print(
                "WARNING: deleting a definition removes that attribute and its values "
                "from all applicable objects on the cleanup vCenter, including values that existed "
                "before this tool was used."
            )

        if not plan.has_changes:
            if plan.has_problems:
                print("Nothing can be changed automatically. Check the conflicts and unmatched "
                      "objects above (--overwrite, --name-map).")
                return 3
            print(f"{args.vcenter} already carries every attribute in the backup. Nothing to restore.")
            if not cleanup_enabled:
                return 0
        if not args.apply:
            print(f"This was a plan only. Run again with --apply to make the {args.command} changes.")
            return 0

        if plan.has_changes and not args.yes:
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

        if plan.has_changes:
            safety = args.backup_dir / f"{_safe(args.vcenter)}-before-{args.command}-{_stamp()}.json"
            safety_snapshot.save(safety)
            print(f"Saved the target's current attributes to {safety} (use it with restore to undo).")
            result = vcenter.apply_plan(si, plan, target)
        else:
            result = vcenter.ApplyResult()
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
    if cleanup_enabled:
        try:
            cleanup_si = vcenter.connect(
                args.cleanup_vcenter, args.cleanup_user,
                _password(
                    args.cleanup_password_env, args.cleanup_user, args.cleanup_vcenter,
                ),
                args.cleanup_port, args.cleanup_insecure,
            )
        except vcenter.VCenterError as exc:
            print(
                f"\nSource restore succeeded, but target cleanup could not connect to "
                f"{args.cleanup_vcenter}; its definitions remain: {exc}",
                file=sys.stderr,
            )
            return 2
        try:
            try:
                cleanup_snapshot = vcenter.read_snapshot(cleanup_si, args.cleanup_vcenter, None)
                cleanup_backup = (
                    args.backup_dir
                    / f"{_safe(args.cleanup_vcenter)}-before-definition-cleanup-{_stamp()}.json"
                )
                cleanup_snapshot.save(cleanup_backup)
            except (OSError, ValueError, vcenter.VCenterError, vcenter.vmodl.MethodFault) as exc:
                print(
                    f"\nSource restore succeeded, but target cleanup stopped before deletion: {exc}",
                    file=sys.stderr,
                )
                return 2
            print(f"Saved a full cleanup-target backup to {cleanup_backup}.")
            if not args.yes:
                if not sys.stdin.isatty():
                    print(
                        "Refusing to delete definitions without a terminal. "
                        "Run interactively or add --yes to confirm."
                    )
                    return 1
                try:
                    answer = input(
                        f"Type the cleanup target vCenter name ({args.cleanup_vcenter}) "
                        "to delete the definitions across the vCenter: "
                    ).strip()
                except EOFError:
                    answer = ""
                if answer != args.cleanup_vcenter:
                    print("Not confirmed. The restored source was unchanged; target definitions remain.")
                    return 1
            removal = vcenter.remove_definitions(
                cleanup_si, list(cleanup_fields.values()), cleanup_snapshot,
            )
        finally:
            vcenter.disconnect(cleanup_si)
        if removal.errors:
            print(f"\nRestore succeeded but cleanup had {len(removal.errors)} error(s):")
            for error in removal.errors:
                print(f"  {error}")
            print(f"Full cleanup-target backup: {cleanup_backup}")
            return 2
        print(
            f"Cleanup complete: deleted {len(removal.removed)} definition(s) "
            f"from {args.cleanup_vcenter}."
        )
        if removal.missing:
            print(f"{len(removal.missing)} definition(s) were already absent.")
    return 0


def cmd_show(args) -> int:
    snap = Snapshot.load(args.backup)
    print(f"vCenter:  {snap.vcenter} ({snap.about})")
    print(f"Taken:    {snap.taken_at}")
    if snap.vm_scoped:
        print(f"VM scope: {snap.objects[0].path}")
    print(f"Attributes ({len(snap.fields)}):")
    for f in snap.fields:
        used = sum(1 for o in snap.objects if f.name in o.values)
        print(f"  {f.name}  [{f.type or 'Global'}]  on {used} object(s)")
    with_values = [o for o in snap.objects if o.values]
    print(f"Objects with values ({len(with_values)}):")
    for o in with_values:
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
