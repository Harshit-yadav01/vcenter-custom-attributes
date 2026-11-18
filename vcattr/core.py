"""Backup format and restore planning. No vCenter I/O here, so it can be tested with plain data."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
VM_FORMAT_VERSION = 2

# Friendly names accepted by --types, mapped to vSphere managed object type names.
TYPE_ALIASES = {
    "vm": "VirtualMachine",
    "host": "HostSystem",
    "cluster": "ClusterComputeResource",
    "computeresource": "ComputeResource",
    "datastore": "Datastore",
    "datastorecluster": "StoragePod",
    "datacenter": "Datacenter",
    "folder": "Folder",
    "resourcepool": "ResourcePool",
    "vapp": "VirtualApp",
    "network": "Network",
    "portgroup": "DistributedVirtualPortgroup",
    "dvswitch": "VmwareDistributedVirtualSwitch",
}

# A field defined for a parent type also applies to these subtypes.
_PARENT_TYPES = {
    "ClusterComputeResource": {"ComputeResource"},
    "VirtualApp": {"ResourcePool"},
    "DistributedVirtualPortgroup": {"Network"},
    "OpaqueNetwork": {"Network"},
    "VmwareDistributedVirtualSwitch": {"DistributedVirtualSwitch"},
}


def resolve_types(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    out = set()
    for item in raw.split(","):
        item = item.strip()
        if item:
            out.add(TYPE_ALIASES.get(item.lower(), item))
    return out or None


def field_applies(field_type: str | None, object_type: str) -> bool:
    """True when a field of `field_type` (None means global) can be set on `object_type`."""
    if field_type is None:
        return True
    return field_type == object_type or field_type in _PARENT_TYPES.get(object_type, set())


@dataclass
class FieldDef:
    name: str
    type: str | None  # None: a global attribute, valid on every object type
    key: int | None = None  # vCenter-local id, never written to a backup


@dataclass
class EntityRecord:
    type: str
    name: str
    path: str = ""
    values: dict[str, str] = field(default_factory=dict)
    bios_uuid: str = ""
    instance_uuid: str = ""
    ref: Any = None  # the live managed object on the vCenter this was read from


@dataclass
class Snapshot:
    vcenter: str
    taken_at: str
    fields: list[FieldDef]
    objects: list[EntityRecord]
    about: str = ""
    vm_scoped: bool = False

    def to_dict(self) -> dict:
        return {
            "format": VM_FORMAT_VERSION if self.vm_scoped else FORMAT_VERSION,
            "vcenter": self.vcenter,
            "about": self.about,
            "taken_at": self.taken_at,
            "fields": [{"name": f.name, "type": f.type} for f in self.fields],
            "objects": [
                {k: v for k, v in asdict(o).items() if k != "ref"}
                for o in self.objects
                if o.values or self.vm_scoped
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Snapshot:
        version = data.get("format")
        if version not in (FORMAT_VERSION, VM_FORMAT_VERSION):
            raise ValueError(
                f"Unsupported backup format {version!r}; expected {FORMAT_VERSION} or {VM_FORMAT_VERSION}."
            )
        snapshot = cls(
            vcenter=data.get("vcenter", ""),
            about=data.get("about", ""),
            taken_at=data.get("taken_at", ""),
            fields=[FieldDef(name=f["name"], type=f.get("type")) for f in data.get("fields", [])],
            objects=[
                EntityRecord(
                    type=o["type"],
                    name=o["name"],
                    path=o.get("path", ""),
                    values=dict(o.get("values", {})),
                    bios_uuid=o.get("bios_uuid", ""),
                    instance_uuid=o.get("instance_uuid", ""),
                )
                for o in data.get("objects", [])
            ],
            vm_scoped=version == VM_FORMAT_VERSION,
        )
        if snapshot.vm_scoped and (
            len(snapshot.objects) != 1 or snapshot.objects[0].type != "VirtualMachine"
        ):
            raise ValueError("A single-VM backup must contain exactly one VM.")
        return snapshot

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Snapshot:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def filtered(self, types: set[str] | None) -> Snapshot:
        if not types:
            return self
        return Snapshot(
            vcenter=self.vcenter,
            about=self.about,
            taken_at=self.taken_at,
            fields=[f for f in self.fields if f.type is None or f.type in types],
            objects=[o for o in self.objects if o.type in types],
            vm_scoped=self.vm_scoped,
        )

    def select_vm(self, selector: str, *, path_only: bool = False) -> EntityRecord:
        matches = [
            obj for obj in self.objects
            if obj.type == "VirtualMachine"
            and (obj.path == selector or (not path_only and obj.name == selector))
        ]
        if not matches:
            raise ValueError(f"{self.vcenter}: no VM matches {selector!r}.")
        if len(matches) > 1:
            paths = ", ".join(obj.path for obj in matches)
            raise ValueError(
                f"{self.vcenter}: VM selector {selector!r} is ambiguous ({paths}); "
                "use the full inventory path."
            )
        return matches[0]

    def for_vm(self, vm: EntityRecord) -> Snapshot:
        return Snapshot(
            vcenter=self.vcenter,
            taken_at=self.taken_at,
            fields=[f for f in self.fields if f.name in vm.values],
            objects=[vm],
            about=self.about,
            vm_scoped=True,
        )


def load_name_map(path: Path | None) -> dict[tuple[str, str], str]:
    """CSV with columns type,source_name,target_name. Type may be blank or * for any type."""
    if path is None:
        return {}
    out: dict[tuple[str, str], str] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            src = (row.get("source_name") or "").strip()
            dst = (row.get("target_name") or "").strip()
            if not src or not dst:
                continue
            typ = (row.get("type") or "").strip()
            typ = TYPE_ALIASES.get(typ.lower(), typ) if typ and typ != "*" else "*"
            out[(typ, src)] = dst
    return out


CREATE, EXISTS, CONFLICT, SET, UNCHANGED, SKIP = (
    "create", "exists", "conflict", "set", "unchanged", "skip",
)


@dataclass
class FieldAction:
    name: str
    type: str | None
    action: str
    detail: str = ""


@dataclass
class ValueAction:
    type: str
    source_name: str
    target: EntityRecord
    field: str
    old: str
    new: str
    action: str
    detail: str = ""


@dataclass
class Unmatched:
    type: str
    name: str
    path: str
    reason: str


@dataclass
class Plan:
    source: str
    target: str
    fields: list[FieldAction] = field(default_factory=list)
    values: list[ValueAction] = field(default_factory=list)
    unmatched: list[Unmatched] = field(default_factory=list)

    def count(self, action: str) -> int:
        return sum(1 for v in self.values if v.action == action)

    @property
    def fields_to_create(self) -> list[FieldAction]:
        return [f for f in self.fields if f.action == CREATE]

    @property
    def has_changes(self) -> bool:
        return bool(self.fields_to_create) or self.count(SET) > 0

    @property
    def has_problems(self) -> bool:
        return (
            bool(self.unmatched)
            or self.count(CONFLICT) > 0
            or self.count(SKIP) > 0
            or any(f.action == CONFLICT for f in self.fields)
        )

    def to_text(self, verbose: bool = False) -> str:
        lines = [f"Plan: copy custom attributes from {self.source} to {self.target}", ""]
        lines.append("Attribute definitions:")
        for f in self.fields:
            note = f" ({f.detail})" if f.detail else ""
            lines.append(f"  [{f.action:9}] {f.name}  [{f.type or 'Global'}]{note}")
        if not self.fields:
            lines.append("  (none in the backup)")
        lines.append("")
        lines.append("Values:")
        shown = [v for v in self.values if verbose or v.action != UNCHANGED]
        for v in shown:
            obj = f"{v.type} {v.target.name}"
            if v.target.name != v.source_name:
                obj += f" (backup name {v.source_name})"
            if v.action == SET:
                change = f"'{v.old}' -> '{v.new}'" if v.old else f"'{v.new}'"
                lines.append(f"  [set      ] {obj}: {v.field} = {change}")
            else:
                note = f" ({v.detail})" if v.detail else ""
                lines.append(f"  [{v.action:9}] {obj}: {v.field} = '{v.new}'{note}")
        if not shown:
            lines.append("  (nothing to change)")
        if self.unmatched:
            lines.append("")
            lines.append("Objects in the backup that were not matched on the target:")
            for u in self.unmatched:
                lines.append(f"  {u.type} {u.name} ({u.path}): {u.reason}")
        lines.append("")
        lines.append(
            "Summary: "
            f"{len(self.fields_to_create)} attribute(s) to create, "
            f"{self.count(SET)} value(s) to set, {self.count(UNCHANGED)} already correct, "
            f"{self.count(CONFLICT)} conflict(s), {self.count(SKIP)} skipped, "
            f"{len(self.unmatched)} object(s) not matched."
        )
        return "\n".join(lines)


def build_plan(
    backup: Snapshot,
    target: Snapshot,
    overwrite: bool = False,
    name_map: dict[tuple[str, str], str] | None = None,
) -> Plan:
    """Work out what must change on `target` so it carries the attributes in `backup`.

    Objects are matched by type and name (after `name_map`). Values on the target that the backup
    does not mention are left alone. A value that differs is only replaced with `overwrite`.
    """
    name_map = name_map or {}
    plan = Plan(source=backup.vcenter, target=target.vcenter)

    target_fields = {f.name: f for f in target.fields}
    usable_fields: dict[str, str | None] = {}
    conflicting_fields = set()
    for f in backup.fields:
        existing = target_fields.get(f.name)
        if existing is None:
            plan.fields.append(FieldAction(f.name, f.type, CREATE))
            usable_fields[f.name] = f.type
        elif existing.type == f.type:
            plan.fields.append(FieldAction(f.name, f.type, EXISTS))
            usable_fields[f.name] = f.type
        else:
            plan.fields.append(FieldAction(
                f.name, f.type, CONFLICT, f"target defines it for {existing.type or 'Global'}",
            ))
            if backup.vm_scoped:
                conflicting_fields.add(f.name)
            else:
                usable_fields[f.name] = existing.type

    by_name: dict[tuple[str, str], list[EntityRecord]] = {}
    for obj in target.objects:
        by_name.setdefault((obj.type, obj.name), []).append(obj)

    backup_counts: dict[tuple[str, str], int] = {}
    for obj in backup.objects:
        backup_counts[(obj.type, obj.name)] = backup_counts.get((obj.type, obj.name), 0) + 1

    for obj in backup.objects:
        dupes = backup_counts[(obj.type, obj.name)]
        if dupes > 1:
            plan.unmatched.append(Unmatched(
                obj.type, obj.name, obj.path,
                f"{dupes} objects share this name in the backup; rename them or use a name map",
            ))
            continue
        target_name = (
            name_map.get((obj.type, obj.name)) or name_map.get(("*", obj.name)) or obj.name
        )
        candidates = by_name.get((obj.type, target_name), [])
        if not candidates:
            plan.unmatched.append(Unmatched(
                obj.type, obj.name, obj.path, f"no {obj.type} named '{target_name}' on the target",
            ))
            continue
        if len(candidates) > 1:
            paths = ", ".join(c.path for c in candidates)
            plan.unmatched.append(Unmatched(
                obj.type, obj.name, obj.path,
                f"{len(candidates)} objects named '{target_name}' on the target ({paths}); "
                "use a name map",
            ))
            continue
        tgt = candidates[0]
        for fname, value in sorted(obj.values.items()):
            old = tgt.values.get(fname, "")
            if fname in conflicting_fields:
                action, detail = SKIP, "attribute definition conflicts with the target"
            elif fname not in usable_fields:
                action, detail = SKIP, "attribute is not defined in the backup"
            elif not field_applies(usable_fields[fname], obj.type):
                action, detail = SKIP, f"attribute on the target is for {usable_fields[fname]}"
            elif old == value:
                action, detail = UNCHANGED, ""
            elif old and not overwrite:
                action, detail = CONFLICT, f"target has '{old}'; use --overwrite to replace"
            else:
                action, detail = SET, ""
            plan.values.append(ValueAction(obj.type, obj.name, tgt, fname, old, value, action, detail))
    return plan
