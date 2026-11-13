from __future__ import annotations

from types import SimpleNamespace

import pytest

from vcattr import vcenter
from vcattr.cli import build_parser, cmd_apply
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
        self.fail_on = fail_on

    def AddCustomFieldDef(self, name, moType):
        f = SimpleNamespace(name=name, key=100 + len(self.field), managedObjectType=moType)
        self.field.append(f)
        return f

    def SetField(self, entity, key, value):
        if entity == self.fail_on:
            raise RuntimeError("boom")
        self.set_calls.append((entity, key, value))


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
