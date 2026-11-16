# vcattr: vCenter custom attribute backup, failover and restore

vCenter custom attributes (Owner, Application, Rack, ...) live in the vCenter database, not in the
VM. When you fail VMs over to a secondary site, the VMs come up there **without** their attributes.
`vcattr` backs the attributes up from the primary vCenter and puts them back on the secondary, and
again on the primary after failback.

It works on every object type that can carry an attribute: VMs, hosts, clusters, datastores,
datastore clusters, datacenters, folders, resource pools, vApps, networks, distributed switches
and port groups, plus global attributes.

## Install

Python 3.10 or newer.

```bash
python -m venv .venv
.venv/bin/pip install -e .          # Windows: .venv\Scripts\pip install -e .
```

This gives you the `vcattr` command (or run `python -m vcattr`).

For the Windows lab command history, explanations, and pending recovery steps,
see the [PowerShell command guide](./POWERSHELL-COMMANDS.md).

## Password

The password is never taken on the command line. Set `VCENTER_PASSWORD` (or the variable named by
`--password-env`), or leave it unset and you are prompted.

Use `--insecure` if the vCenter has a self-signed certificate.

## Disaster recovery workflow

### 1. Take backups regularly, while the primary is up

```bash
vcattr backup --vcenter vc-primary.example.local --user administrator@vsphere.local
# Saved to backups/vc-primary.example.local-20261113-101500.json
```

Run it on a schedule (Task Scheduler or cron) and keep the files somewhere the secondary site can
reach. The primary is usually down when you need them. `vcattr show <file>` prints what a backup
holds.

### 2. Failover: apply the primary's backup to the secondary

After the VMs are recovered on the secondary site (SRM, Remote Copy, or another tool):

```bash
# See the plan first. Nothing is changed.
vcattr failover --vcenter vc-secondary.example.local --user administrator@vsphere.local \
    --backup backups/vc-primary.example.local-20261113-101500.json

# Make the changes
vcattr failover ... --apply

# Run the plan again to check: it should say there is nothing to do
vcattr failover ...
```

`--apply` asks you to type the target vCenter name. Use `--yes` to skip that in a script.

If the primary vCenter is reachable, you can read its current attributes directly instead of
using a saved backup file:

```bash
vcattr failover --source-vcenter vc-primary.example.local --source-user administrator@vsphere.local \
    --vcenter vc-secondary.example.local --user administrator@vsphere.local
vcattr failover --source-vcenter vc-primary.example.local --source-user administrator@vsphere.local \
    --vcenter vc-secondary.example.local --user administrator@vsphere.local --apply
```

The source and target can use different credentials. Set `VCENTER_SOURCE_PASSWORD` for the
source and `VCENTER_PASSWORD` for the target, or leave them unset to be prompted securely.
Use `--source-password-env` to choose another source password variable. The live source is
read-only; all changes are made only on the target.

### 3. Restore: after failback, put the attributes back on the primary

Attributes may have changed while you ran at the secondary site, so back up the secondary first,
then restore that backup to the primary:

```bash
vcattr backup  --vcenter vc-secondary.example.local --user administrator@vsphere.local \
    --out backups/secondary-before-failback.json
vcattr restore --vcenter vc-primary.example.local --user administrator@vsphere.local \
    --backup backups/secondary-before-failback.json --overwrite --apply
```

`restore` also repairs a vCenter that lost attributes (rebuild, reinstall, accidental delete):
restore one of its own backups to it.
As with `failover`, `restore` can use `--source-vcenter` and `--source-user` instead of `--backup`
to read from a reachable source vCenter.

## How it decides what to change

- **Attribute definitions** missing on the target are created with the same name and object type.
  If the target already has the name for a different object type, that is reported as a
  conflict and its values are skipped.
- **Objects are matched by type and name.** A VM called `web01` in the backup gets the values of
  `web01` on the target. If a name is missing on the target, or used by more than one object of
  that type, the object is listed as not matched and left alone. The tool never guesses.
- **Renamed objects** (for example hosts and datastores, which usually have different names on
  each site): pass `--name-map map.csv`:

  ```csv
  type,source_name,target_name
  host,esx01.site-a.local,esx01.site-b.local
  datastore,DS-A-01,DS-B-01
  *,shared-name-a,shared-name-b
  ```

- **Values that already exist** on the target with a different value are reported as conflicts and
  kept. Add `--overwrite` to replace them with the backup value.
- **Values on the target that are not in the backup are never removed.**
- `--types vm,host,cluster` limits any command to some object types. Accepted names: `vm`, `host`,
  `cluster`, `computeresource`, `datastore`, `datastorecluster`, `datacenter`, `folder`,
  `resourcepool`, `vapp`, `network`, `portgroup`, `dvswitch`, or a vSphere type name such as
  `VirtualMachine`.

## Safety

- `backup` and the plan only read from vCenter.
- With `--apply`, the target's current attributes are first saved to
  `backups/<vcenter>-before-<command>-<time>.json`. Restoring that file with `--overwrite` puts
  back any value that was overwritten. Attribute definitions that were created, and values added
  where there was none, stay: the tool never deletes.
- One failed object does not stop the run. Every error is listed at the end.

## Permissions

The vCenter user needs **Global > Manage custom attributes** (to create definitions) and
**Global > Set custom attribute** (to set values). `backup` needs read access only.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Done, or the target already matches the backup |
| 1 | Not confirmed, or refused to apply without a terminal and without `--yes` |
| 2 | Error: connection, login, bad backup file, or at least one change failed |
| 3 | Done, but some conflicts, skipped values or unmatched objects need attention |

## Not covered

- vSphere **tags** (Tags & Custom Attributes > Tags) are a different feature with their own API.
  This tool handles custom attributes only.
- It does not fail VMs over. Run it after your failover tool has recovered them.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

The tests use fakes only. For an end-to-end run without real vCenters, use the govmomi simulator
([vcsim](https://github.com/vmware/govmomi/tree/main/vcsim)): start two copies on different ports
and use `--port` and `--insecure` (any user name and password works). vcsim cannot create global
attributes (it drops their data type), so test those on a real vCenter.
