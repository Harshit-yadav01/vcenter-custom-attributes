"""The only module that talks to vCenter (pyVmomi)."""

from __future__ import annotations

import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import VmomiSupport, vim, vmodl

from vcattr.core import CREATE, SET, EntityRecord, FieldDef, Plan, Snapshot


class VCenterError(Exception):
    pass


def connect(host: str, user: str, password: str, port: int = 443, insecure: bool = False):
    """Open a session. With `insecure` the certificate is not checked (lab or self-signed certs)."""
    context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    try:
        si = SmartConnect(host=host, user=user, pwd=password, port=port, sslContext=context)
    except vim.fault.InvalidLogin as exc:
        raise VCenterError(f"{host}: user name or password was rejected.") from exc
    except ssl.SSLError as exc:
        raise VCenterError(
            f"{host}: the certificate could not be verified ({exc}). "
            "Use --insecure if this vCenter uses a self-signed certificate."
        ) from exc
    except Exception as exc:
        raise VCenterError(f"{host}: could not connect ({exc}).") from exc
    content = si.RetrieveContent()
    if content.customFieldsManager is None:
        Disconnect(si)
        raise VCenterError(f"{host} is not a vCenter (it has no custom attributes manager).")
    return si


def disconnect(si) -> None:
    try:
        Disconnect(si)
    except Exception:  # noqa: BLE001, S110 - best-effort logout
        pass


def _type_name(cls) -> str:
    return getattr(cls, "_wsdlName", None) or cls.__name__.rsplit(".", 1)[-1]


def _vim_type(name: str):
    try:
        return VmomiSupport.GetWsdlType("urn:vim25", name)
    except Exception as exc:
        raise VCenterError(f"Unknown vSphere object type '{name}'.") from exc


def _retrieve(content, obj_type, props: list[str]) -> list:
    """Read `props` for every object of `obj_type` in one paged property collector call."""
    view = content.viewManager.CreateContainerView(content.rootFolder, [obj_type], True)
    pc = vmodl.query.PropertyCollector
    try:
        spec = pc.FilterSpec(
            objectSet=[pc.ObjectSpec(
                obj=view, skip=True,
                selectSet=[pc.TraversalSpec(name="view", type=vim.view.ContainerView, path="view")],
            )],
            propSet=[pc.PropertySpec(type=obj_type, pathSet=props, all=False)],
        )
        collector = content.propertyCollector
        out = []
        result = collector.RetrievePropertiesEx([spec], pc.RetrieveOptions())
        while result:
            out.extend(result.objects)
            if not result.token:
                break
            result = collector.ContinueRetrievePropertiesEx(result.token)
        return out
    finally:
        view.Destroy()


def read_snapshot(si, host: str, types: set[str] | None = None) -> Snapshot:
    """Read every attribute definition and every object's values. Changes nothing."""
    content = si.RetrieveContent()
    fields = [
        FieldDef(
            name=f.name,
            type=_type_name(f.managedObjectType) if f.managedObjectType else None,
            key=f.key,
        )
        for f in (content.customFieldsManager.field or [])
    ]
    key_to_name = {f.key: f.name for f in fields}

    raw: dict[str, dict] = {}
    for oc in _retrieve(content, vim.ManagedEntity, ["name", "parent", "customValue"]):
        props = {p.name: p.val for p in (oc.propSet or [])}
        raw[oc.obj._moId] = {"obj": oc.obj, **props}

    vm_ids: dict[str, dict] = {}
    if not types or "VirtualMachine" in types:
        for oc in _retrieve(content, vim.VirtualMachine, ["config.uuid", "config.instanceUuid"]):
            vm_ids[oc.obj._moId] = {p.name: p.val for p in (oc.propSet or [])}

    def path_of(moid: str) -> str:
        parts, seen = [], set()
        while moid in raw and moid not in seen:
            seen.add(moid)
            parts.append(raw[moid].get("name", ""))
            parent = raw[moid].get("parent")
            moid = parent._moId if parent is not None else ""
        return "/".join(reversed(parts))

    objects = []
    for moid, item in raw.items():
        obj_type = _type_name(type(item["obj"]))
        if types and obj_type not in types:
            continue
        values = {}
        for cv in item.get("customValue") or []:
            value = getattr(cv, "value", None)
            name = key_to_name.get(cv.key)
            if name and value not in (None, ""):
                values[name] = str(value)
        ids = vm_ids.get(moid, {})
        objects.append(EntityRecord(
            type=obj_type,
            name=item.get("name", ""),
            path=path_of(moid),
            values=values,
            bios_uuid=ids.get("config.uuid") or "",
            instance_uuid=ids.get("config.instanceUuid") or "",
            ref=item["obj"],
        ))

    # Objects are already limited to `types`; every field stays so name clashes are still seen.
    return Snapshot(
        vcenter=host,
        about=content.about.fullName,
        taken_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        fields=fields,
        objects=sorted(objects, key=lambda o: (o.type, o.path)),
    )


@dataclass
class ApplyResult:
    created: list[str] = field(default_factory=list)
    set: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class DefinitionRemovalResult:
    removed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def apply_plan(si, plan: Plan, target: Snapshot, progress=print) -> ApplyResult:
    """Create the missing attribute definitions, then set each planned value. Keeps going on errors."""
    manager = si.RetrieveContent().customFieldsManager
    result = ApplyResult()
    keys = {f.name: f.key for f in target.fields}

    for f in plan.fields:
        if f.action != CREATE:
            continue
        try:
            mo_type = _vim_type(f.type) if f.type else None
            new = manager.AddCustomFieldDef(name=f.name, moType=mo_type)
            keys[f.name] = new.key
            result.created.append(f.name)
            progress(f"  created attribute {f.name} [{f.type or 'Global'}]")
        except vim.fault.DuplicateName:
            # Someone created it between the plan and now; reuse it.
            existing = next((x for x in manager.field or [] if x.name == f.name), None)
            if existing is not None:
                keys[f.name] = existing.key
                progress(f"  attribute {f.name} already exists, reusing it")
            else:
                result.errors.append(f"create attribute {f.name}: duplicate name but not found")
        except Exception as exc:  # noqa: BLE001 - record and continue with the next item
            result.errors.append(f"create attribute {f.name}: {_fault_text(exc)}")

    for v in plan.values:
        if v.action != SET:
            continue
        key = keys.get(v.field)
        if key is None:
            result.errors.append(f"{v.type} {v.target.name}: {v.field} was not created, value skipped")
            continue
        try:
            manager.SetField(entity=v.target.ref, key=key, value=v.new)
            result.set += 1
        except Exception as exc:  # noqa: BLE001 - record and continue with the next item
            result.errors.append(f"{v.type} {v.target.name}: set {v.field}: {_fault_text(exc)}")
    progress(f"  set {result.set} value(s)")
    return result


def remove_definitions(
    si, fields: list[FieldDef], safety_snapshot: Snapshot, progress=print,
) -> DefinitionRemovalResult:
    """Remove requested definitions only if their type and saved identity still match."""
    manager = si.RetrieveContent().customFieldsManager
    result = DefinitionRemovalResult()
    definitions = {f.name: f for f in manager.field or []}
    saved = {f.name: f for f in safety_snapshot.fields}
    to_remove = []
    for expected in fields:
        definition = definitions.get(expected.name)
        if definition is None:
            result.missing.append(expected.name)
            continue
        actual_type = _type_name(definition.managedObjectType) if definition.managedObjectType else None
        if actual_type != expected.type:
            result.errors.append(
                f"remove attribute {expected.name}: target type is {actual_type or 'Global'}, "
                f"expected {expected.type or 'Global'}"
            )
            continue
        saved_field = saved.get(expected.name)
        if saved_field is None or saved_field.key != definition.key or saved_field.type != actual_type:
            result.errors.append(
                f"remove attribute {expected.name}: definition changed since the safety backup"
            )
            continue
        to_remove.append(definition)
    if result.errors:
        return result
    for name in result.missing:
        progress(f"  attribute {name} is already absent")
    for definition in to_remove:
        try:
            manager.RemoveCustomFieldDef(key=definition.key)
            result.removed.append(definition.name)
            progress(f"  deleted attribute {definition.name}")
        except Exception as exc:  # noqa: BLE001 - record and continue with remaining definitions
            result.errors.append(f"remove attribute {definition.name}: {_fault_text(exc)}")
    return result


def _fault_text(exc: Exception) -> str:
    msg = getattr(exc, "msg", None) or str(exc)
    if isinstance(exc, vim.fault.NoPermission):
        return f"permission denied ({msg}); the user needs Global > Manage/Set custom attributes"
    return msg
