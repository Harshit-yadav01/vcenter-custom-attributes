from __future__ import annotations

from types import SimpleNamespace

import pytest

from vcattr import vcenter
from vcattr.cli import build_parser, cmd_apply, main
from vcattr.core import (
    CONFLICT,
    CREATE,
    EXISTS,
    SET,
    SKIP,
    UNCHANGED,
    EntityRecord,
    FieldDef,
    Snapshot,
    build_plan,
    field_applies,
    load_name_map,
    resolve_types,
)


def snap(host, fields, objects):
    return Snapshot(vcenter=host, taken_at="t", fields=fields, objects=objects)


def vm(name, values=None, path=None, ref=None):
    return EntityRecord("VirtualMachine", name, path or f"DC/vm/{name}", dict(values or {}), ref=ref)


def by_field(plan):
    return {(v.target.name, v.field): v for v in plan.values}


def test_new_target_gets_fields_and_values():
    backup = snap("vc-a", [FieldDef("Owner", "VirtualMachine"), FieldDef("App", None)],
                  [vm("web01", {"Owner": "ops", "App": "shop"})])
    target = snap("vc-b", [], [vm("web01")])
    plan = build_plan(backup, target)
    assert [(f.name, f.action) for f in plan.fields] == [("Owner", CREATE), ("App", CREATE)]
    assert {k: v.action for k, v in by_field(plan).items()} == {
        ("web01", "Owner"): SET, ("web01", "App"): SET,
    }
    assert plan.has_changes and not plan.has_problems


def test_rerun_after_apply_is_a_no_op():
    backup = snap("vc-a", [FieldDef("Owner", "VirtualMachine")], [vm("web01", {"Owner": "ops"})])
    target = snap("vc-b", [FieldDef("Owner", "VirtualMachine", key=7)], [vm("web01", {"Owner": "ops"})])
    plan = build_plan(backup, target)
    assert plan.fields[0].action == EXISTS
    assert plan.values[0].action == UNCHANGED
    assert not plan.has_changes


def test_different_value_is_a_conflict_unless_overwrite():
    backup = snap("vc-a", [FieldDef("Owner", None)], [vm("web01", {"Owner": "ops"})])
    target = snap("vc-b", [FieldDef("Owner", None)], [vm("web01", {"Owner": "dev"})])
    assert build_plan(backup, target).values[0].action == CONFLICT
    v = build_plan(backup, target, overwrite=True).values[0]
    assert (v.action, v.old, v.new) == (SET, "dev", "ops")


def test_values_only_on_target_are_left_alone():
    backup = snap("vc-a", [FieldDef("Owner", None)], [vm("web01", {"Owner": "ops"})])
    target = snap("vc-b", [FieldDef("Owner", None), FieldDef("Local", None)],
                  [vm("web01", {"Local": "keep"})])
    plan = build_plan(backup, target)
    assert [(v.field, v.action) for v in plan.values] == [("Owner", SET)]


def test_missing_and_duplicate_objects_are_reported_not_guessed():
    backup = snap("vc-a", [FieldDef("Owner", None)], [
        vm("gone", {"Owner": "x"}),
        vm("twin", {"Owner": "y"}),
        vm("dup", {"Owner": "1"}, path="DC/vm/a/dup"),
        vm("dup", {"Owner": "2"}, path="DC/vm/b/dup"),
    ])
    target = snap("vc-b", [], [vm("twin", path="DC/vm/a/twin"), vm("twin", path="DC/vm/b/twin"),
                               vm("dup")])
    plan = build_plan(backup, target)
    assert plan.values == []
    reasons = {(u.name, u.path): u.reason for u in plan.unmatched}
    assert "no VirtualMachine named 'gone'" in reasons[("gone", "DC/vm/gone")]
    assert "2 objects named 'twin' on the target" in reasons[("twin", "DC/vm/twin")]
    assert "share this name in the backup" in reasons[("dup", "DC/vm/a/dup")]
    assert plan.has_problems


def test_name_map_renames_objects():
    backup = snap("vc-a", [FieldDef("Rack", "HostSystem")],
                  [EntityRecord("HostSystem", "esx01.site-a", values={"Rack": "R1"})])
    target = snap("vc-b", [], [EntityRecord("HostSystem", "esx01.site-b")])
    plan = build_plan(backup, target, name_map={("HostSystem", "esx01.site-a"): "esx01.site-b"})
    assert plan.values[0].action == SET and plan.values[0].target.name == "esx01.site-b"
    assert "backup name esx01.site-a" in plan.to_text()


def test_field_defined_for_other_type_on_target_is_a_conflict():
    backup = snap("vc-a", [FieldDef("Owner", "VirtualMachine")], [vm("web01", {"Owner": "ops"})])
    target = snap("vc-b", [FieldDef("Owner", "HostSystem")], [vm("web01")])
    plan = build_plan(backup, target)
    assert plan.fields[0].action == CONFLICT
    assert plan.values[0].action == SKIP


def test_field_type_rules():
    assert field_applies(None, "Datastore")
    assert field_applies("VirtualMachine", "VirtualMachine")
    assert field_applies("ComputeResource", "ClusterComputeResource")
    assert not field_applies("HostSystem", "VirtualMachine")


def test_resolve_types_aliases():
    assert resolve_types("vm, host,Datastore") == {"VirtualMachine", "HostSystem", "Datastore"}
    assert resolve_types("") is None


def test_backup_round_trip_drops_refs_and_empty_objects(tmp_path):
    s = snap("vc-a", [FieldDef("Owner", None, key=5)],
             [vm("web01", {"Owner": "ops"}, ref=object()), vm("empty")])
    path = tmp_path / "b.json"
    s.save(path)
    text = path.read_text()
    assert "ref" not in text and "empty" not in text and '"key"' not in text
    loaded = Snapshot.load(path)
    assert loaded.fields[0].name == "Owner" and loaded.fields[0].key is None
    assert [o.name for o in loaded.objects] == ["web01"]


def test_unknown_format_is_refused():
    with pytest.raises(ValueError):
        Snapshot.from_dict({"format": 99})


def test_name_map_csv(tmp_path):
    p = tmp_path / "map.csv"
    p.write_text("type,source_name,target_name\nhost,esx-a,esx-b\n*,ds-a,ds-b\n,,\n")
    assert load_name_map(p) == {("HostSystem", "esx-a"): "esx-b", ("*", "ds-a"): "ds-b"}


def test_apply_accepts_live_source_vcenter():
    args = build_parser().parse_args([
        "restore", "--source-vcenter", "vc-source", "--source-user", "source-user",
        "--vcenter", "vc-target", "--user", "target-user",
    ])
    assert args.source_vcenter == "vc-source"
    assert args.source_user == "source-user"
    assert args.backup is None


def test_apply_reads_live_source_and_uses_separate_credentials(monkeypatch, capsys):
    source = snap("vc-source", [FieldDef("Owner", None)], [vm("web01", {"Owner": "ops"})])
    target = snap(
        "vc-target", [FieldDef("Owner", None, key=7)], [vm("web01", {"Owner": "ops"})],
    )
    connections = []
    disconnected = []

    def connect(host, user, password, port, insecure):
        connections.append((host, user, password, port, insecure))
        return host

    def read_snapshot(si, host, types):
        return source if si == "vc-source" else target

    monkeypatch.setenv("VCENTER_SOURCE_PASSWORD", "source-secret")
    monkeypatch.setenv("VCENTER_PASSWORD", "target-secret")
    monkeypatch.setattr(vcenter, "connect", connect)
    monkeypatch.setattr(vcenter, "read_snapshot", read_snapshot)
    monkeypatch.setattr(vcenter, "disconnect", disconnected.append)

    args = build_parser().parse_args([
        "restore", "--source-vcenter", "vc-source", "--source-user", "source-user",
        "--vcenter", "vc-target", "--user", "target-user",
    ])
    assert cmd_apply(args) == 0
    assert connections == [
        ("vc-source", "source-user", "source-secret", 443, False),
        ("vc-target", "target-user", "target-secret", 443, False),
    ]
    assert disconnected == ["vc-source", "vc-target"]
    assert "Source: vc-source (read live" in capsys.readouterr().out


class FakeManager:
    def __init__(self, fail_on=None):
        self.field = []
        self.set_calls = []
        self.removed_keys = []
        self.fail_on = fail_on

    def AddCustomFieldDef(self, name, moType):
        f = SimpleNamespace(name=name, key=100 + len(self.field), managedObjectType=moType)
        self.field.append(f)
        return f

    def SetField(self, entity, key, value):
        if entity == self.fail_on:
            raise RuntimeError("boom")
        self.set_calls.append((entity, key, value))

    def RemoveCustomFieldDef(self, key):
        self.removed_keys.append(key)
        self.field = [f for f in self.field if f.key != key]


def test_apply_plan_creates_fields_then_sets_values():
    manager = FakeManager(fail_on="ref-db")
    si = SimpleNamespace(RetrieveContent=lambda: SimpleNamespace(customFieldsManager=manager))
    backup = snap("vc-a", [FieldDef("Owner", "VirtualMachine"), FieldDef("Old", None)],
                  [vm("web01", {"Owner": "ops", "Old": "x"}), vm("db01", {"Owner": "dba"})])
    target = snap("vc-b", [FieldDef("Old", None, key=9)],
                  [vm("web01", {"Old": "x"}, ref="ref-web"), vm("db01", ref="ref-db")])
    plan = build_plan(backup, target)
    result = vcenter.apply_plan(si, plan, target, progress=lambda _m: None)
    assert result.created == ["Owner"]
    assert manager.field[0].managedObjectType._wsdlName == "VirtualMachine"
    assert manager.set_calls == [("ref-web", 100, "ops")]
    assert result.set == 1
    assert result.errors == ["VirtualMachine db01: set Owner: boom"]


def test_remove_global_definitions_removes_only_requested_global_fields():
    manager = FakeManager()
    manager.field = [
        SimpleNamespace(name="Owner", key=7, managedObjectType=None),
        SimpleNamespace(name="Rack", key=8, managedObjectType=None),
        SimpleNamespace(name="HostOnly", key=9, managedObjectType=object()),
    ]
    si = SimpleNamespace(RetrieveContent=lambda: SimpleNamespace(customFieldsManager=manager))

    result = vcenter.remove_global_definitions(
        si, ["Owner", "NotPresent", "HostOnly"], progress=lambda _message: None,
    )

    assert result.removed == ["Owner"]
    assert result.missing == ["NotPresent"]
    assert result.errors == ["remove attribute HostOnly: target definition is not Global"]
    assert manager.removed_keys == [7]
    assert [f.name for f in manager.field] == ["Rack", "HostOnly"]


def test_single_vm_backup_selects_path_and_excludes_other_fields(monkeypatch, tmp_path):
    source = snap("primary", [FieldDef("Owner", None), FieldDef("Other", None)], [
        vm("RHEL8", {"Owner": "ops"}, path="DC/vm/A/RHEL8"),
        vm("RHEL8", {"Other": "keep"}, path="DC/vm/B/RHEL8"),
        vm("empty"),
    ])
    wire_snapshots(monkeypatch, {"primary": source})
    output = tmp_path / "source.json"
    args = ["backup", "--vcenter", "primary", "--user", "user", "--out", str(output)]
    assert main([*args, "--vm", "RHEL8"]) == 2
    assert main([*args, "--vm", "missing"]) == 2
    assert not output.exists()
    assert main([*args, "--vm", "DC/vm/A/RHEL8"]) == 0
    backup = Snapshot.load(output)
    assert backup.vm_scoped
    assert [f.name for f in backup.fields] == ["Owner"]
    assert [(o.path, o.values) for o in backup.objects] == [("DC/vm/A/RHEL8", {"Owner": "ops"})]
    assert main([*args, "--vm", "empty"]) == 0
    backup = Snapshot.load(output)
    assert backup.vm_scoped and backup.fields == []
    assert backup.objects[0].name == "empty" and backup.objects[0].values == {}


def wire_snapshots(monkeypatch, snapshots):
    monkeypatch.setenv("VCENTER_PASSWORD", "test-password")
    managers = {host: FakeManager() for host in snapshots}
    monkeypatch.setattr(vcenter, "connect", lambda host, *_: SimpleNamespace(
        RetrieveContent=lambda: SimpleNamespace(customFieldsManager=managers[host]),
    ))
    monkeypatch.setattr(vcenter, "disconnect", lambda _: None)
    monkeypatch.setattr(vcenter, "read_snapshot", lambda _si, host, _types: snapshots[host])
    return managers


def test_single_vm_failover_previews_and_only_writes_selected_vm(monkeypatch, tmp_path, capsys):
    field = FieldDef("Owner", None, key=7)
    source_vm = vm("RHEL8", {"Owner": "ops"})
    source = snap("primary", [field], [source_vm])
    original = tmp_path / "original.json"
    source.for_vm(source_vm).save(original)
    target = snap("secondary", [field], [
        vm("RHEL8", ref="not-selected"),
        vm("recovered", {"Owner": "previous"}, path="DC/vm/A/recovered", ref="selected"),
        vm("recovered", path="DC/vm/B/recovered", ref="same-name"),
    ])
    manager = wire_snapshots(monkeypatch, {"secondary": target})["secondary"]
    args = ["failover", "--vcenter", "secondary", "--user", "user", "--backup", str(original),
            "--backup-dir", str(tmp_path), "--overwrite"]
    assert main([*args, "--apply", "--yes"]) == 2
    assert main([*args, "--vm", "recovered", "--apply", "--yes"]) == 2
    assert main([*args, "--vm", "DC/vm/A/recovered"]) == 0
    assert "Destination VM: DC/vm/A/recovered" in capsys.readouterr().out
    assert manager.set_calls == []
    assert main([*args, "--vm", "DC/vm/A/recovered", "--apply", "--yes"]) == 0
    assert manager.set_calls == [("selected", 7, "ops")]
    safety = Snapshot.load(next(tmp_path.glob("secondary-before-failover-*.json")))
    assert safety.vm_scoped
    assert [(o.path, o.values) for o in safety.objects] == [
        ("DC/vm/A/recovered", {"Owner": "previous"}),
    ]


@pytest.mark.parametrize("use_latest,expected", [(False, "ops"), (True, "updated")])
def test_single_vm_failback_only_restores_original_vm(monkeypatch, tmp_path, use_latest, expected):
    field = FieldDef("Owner", None, key=7)
    original_vm = vm("RHEL8", {"Owner": "ops"}, path="DC/vm/A/RHEL8")
    original_vm.instance_uuid = "original-id"
    original = tmp_path / "original.json"
    snap("primary", [field], [original_vm]).for_vm(original_vm).save(original)
    recovered_vm = vm("recovered", {"Owner": "updated"})
    latest = tmp_path / "latest.json"
    snap("secondary", [field], [recovered_vm]).for_vm(recovered_vm).save(latest)
    restored_vm = vm("RHEL8", {"Owner": "before"}, path=original_vm.path, ref="original")
    restored_vm.instance_uuid = "replacement-id"
    primary = snap("primary", [field], [restored_vm, vm("RHEL8", ref="same-name")])
    manager = wire_snapshots(monkeypatch, {"primary": primary})["primary"]
    args = ["restore", "--user", "user", "--backup", str(latest if use_latest else original),
            "--original-backup", str(original), "--backup-dir", str(tmp_path),
            "--overwrite", "--apply", "--yes"]
    assert main([*args, "--vcenter", "secondary"]) == 2
    assert main([*args, "--vcenter", "primary"]) == 2
    assert manager.set_calls == []
    restored_vm.instance_uuid = "original-id"
    assert main([*args, "--vcenter", "primary"]) == 0
    assert manager.set_calls == [("original", 7, expected)]


def test_failback_deletes_only_backup_global_definitions_after_success(
    monkeypatch, tmp_path, capsys,
):
    field = FieldDef("Owner", None, key=7)
    original_vm = vm("RHEL8", {"Owner": "old"}, path="DC/vm/A/RHEL8")
    original_vm.instance_uuid = "original-id"
    original = tmp_path / "original.json"
    snap("primary", [field], [original_vm]).for_vm(original_vm).save(original)

    recovered_vm = vm("recovered", {"Owner": "latest"})
    latest = tmp_path / "latest.json"
    snap("secondary", [field], [recovered_vm]).for_vm(recovered_vm).save(latest)

    source_vm = vm("RHEL8", {"Owner": "before"}, path=original_vm.path, ref="source-ref")
    source_vm.instance_uuid = "original-id"
    source = snap("primary", [field], [source_vm])
    target_vm = vm("recovered", {"Owner": "latest"}, path="DR/vm/recovered", ref="target-ref")
    target = snap(
        "secondary",
        [field, FieldDef("KeepMe", None, key=8)],
        [target_vm, vm("unrelated", {"Owner": "keep"}, ref="other-ref")],
    )

    snapshots = {"primary": source, "secondary": target}
    managers = wire_snapshots(monkeypatch, snapshots)
    managers["secondary"].field = [
        SimpleNamespace(name="Owner", key=7, managedObjectType=None),
        SimpleNamespace(name="KeepMe", key=8, managedObjectType=None),
    ]
    monkeypatch.setenv("VCENTER_CLEANUP_PASSWORD", "cleanup-password")

    args = [
        "restore", "--vcenter", "primary", "--user", "restore-user",
        "--backup", str(latest), "--original-backup", str(original),
        "--delete-target-definitions", "--cleanup-vcenter", "secondary",
        "--cleanup-user", "cleanup-user", "--cleanup-insecure",
        "--backup-dir", str(tmp_path), "--overwrite",
    ]
    assert main([*args]) == 0
    assert managers["primary"].set_calls == []
    assert managers["secondary"].removed_keys == []
    assert "WARNING: deleting a Global definition" in capsys.readouterr().out

    assert main([*args, "--apply", "--yes"]) == 0
    assert managers["primary"].set_calls == [("source-ref", 7, "latest")]
    assert managers["secondary"].removed_keys == [7]
    assert [f.name for f in managers["secondary"].field] == ["KeepMe"]
    saved_target = Snapshot.load(next(tmp_path.glob("secondary-before-definition-cleanup-*.json")))
    assert len(saved_target.objects) == 2
    assert saved_target.objects[1].values == {"Owner": "keep"}


def test_failback_cleanup_does_not_run_if_source_restore_has_problems(
    monkeypatch, tmp_path,
):
    field = FieldDef("Owner", None, key=7)
    original_vm = vm("RHEL8", {"Owner": "old"}, path="DC/vm/A/RHEL8")
    original = tmp_path / "original.json"
    snap("primary", [field], [original_vm]).for_vm(original_vm).save(original)
    latest = tmp_path / "latest.json"
    snap("secondary", [field], [vm("recovered", {"Owner": "latest"})]).for_vm(
        vm("recovered", {"Owner": "latest"}),
    ).save(latest)
    source = snap("primary", [field], [vm("RHEL8", {"Owner": "different"}, path=original_vm.path)])
    target = snap("secondary", [field], [vm("recovered", {"Owner": "latest"})])
    managers = wire_snapshots(monkeypatch, {"primary": source, "secondary": target})
    managers["secondary"].field = [
        SimpleNamespace(name="Owner", key=7, managedObjectType=None),
    ]
    monkeypatch.setenv("VCENTER_CLEANUP_PASSWORD", "cleanup-password")
    args = [
        "restore", "--vcenter", "primary", "--user", "restore-user",
        "--backup", str(latest), "--original-backup", str(original),
        "--delete-target-definitions", "--cleanup-vcenter", "secondary",
        "--cleanup-user", "cleanup-user", "--backup-dir", str(tmp_path),
        "--apply", "--yes",
    ]
    assert main(args) == 3
    assert managers["secondary"].removed_keys == []
