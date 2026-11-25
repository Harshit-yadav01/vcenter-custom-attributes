# Windows PowerShell command guide and execution record

This guide records the commands used during testing and explains what each does.
It distinguishes confirmed results from steps that have not yet been verified.
Run commands individually and stop to investigate errors before continuing.

The commands use the virtual environment directly; activation is not required.
Python 3.10 or newer is required. Do not copy PowerShell prompt text such as
`PS ...>` or `>>`, Linux line-continuation backslashes, or angle-bracket placeholders.

## Confirmed execution results

| Step | Observed result |
|---|---|
| Clone project and create virtual environment | Succeeded |
| Install project using `pip install -e .` | Failed because pip could not verify the PyPI certificate |
| Install `pyvmomi` using temporary trusted-host exceptions | Succeeded; version 9.1.1.0 was installed |
| Import `SmartConnect` | Succeeded |
| Back up source attributes | Succeeded |
| Inspect the backup with `show` | Succeeded |
| Repeat backup with `--types vm` | Succeeded; 4 definitions and 4 values on one VM |
| Apply to the initially attempted target | Failed authentication; no changes |
| Apply to the subsequently selected target | Created 4 definitions, set 0 values; source VM was unmatched |
| Preview the target again | Confirmed 4 existing definitions; source VM still unmatched |
| Create a name-map CSV | Prompt was started; completion was not confirmed |
| Recover the VM through SRM and apply its values | Not yet confirmed |

No commands in this guide automatically recover VMs through SRM.

## Single-VM backup, failover, and failback

Use this workflow when values should be copied only from and to VMs you select.
These commands describe the new workflow, not the site-wide lab runs recorded
above. Use the updated tool on both sites. It still reads older site-wide
backups, but you must create a new backup with `--vm` for single-VM operations.

After setup, enter the source/target vCenter and username variables as described
in [section 3](#3-enter-connection-details), then select the original VM:

```powershell
$sourceVm = Read-Host "Enter the original VM's exact name or full inventory path"
.\.venv\Scripts\python.exe -m vcattr backup --vcenter $sourceVcenter --user $sourceUser --vm $sourceVm --out .\backups\original-vm.json
.\.venv\Scripts\python.exe -m vcattr show .\backups\original-vm.json
```

The JSON contains only the selected VM and definitions for its populated values.
Keep this original file at the recovery site; do not overwrite it with a later
target backup. If multiple VMs share a name, use the full inventory path shown
in the error, such as `DC/vm/Folder/RHEL8`.

Once SRM has recovered the VM at the target, select that VM explicitly and preview:

```powershell
$targetVm = Read-Host "Enter the recovered VM's exact name or full inventory path"
.\.venv\Scripts\python.exe -m vcattr failover --vcenter $targetVcenter --user $targetUser --backup .\backups\original-vm.json --vm $targetVm
```

If `Destination VM` is correct, repeat with `--apply` and confirm when prompted.
A missing or ambiguous destination is rejected before any definitions or values
are written. Global definition names can still appear on other VMs; **only the
selected destination VM receives values**. No name-map CSV is needed.

For failback, choose which values to restore:

- **Original values:** use the original source backup.
- **Latest target values:** before taking the recovered VM offline or moving it
  back, save its current values to a separate file.

```powershell
# Only for latest target values
.\.venv\Scripts\python.exe -m vcattr backup --vcenter $targetVcenter --user $targetUser --vm $targetVm --out .\backups\target-vm-latest.json
```

When the original VM is available again, set `$valuesBackup` to one of the
two files, then preview the restore:

```powershell
$valuesBackup = ".\backups\original-vm.json"
# To restore latest target values, set $valuesBackup to ".\backups\target-vm-latest.json" instead.
.\.venv\Scripts\python.exe -m vcattr restore --vcenter $sourceVcenter --user $sourceUser --backup $valuesBackup --original-backup .\backups\original-vm.json
```

`--original-backup` checks the original vCenter address, VM inventory path, and
instance UUID when present in the backup. Use the same vCenter address you used
to take the original backup. If SRM changed the original VM's path or UUID,
verify the recovered VM's identity in vSphere Client, then use `--vm` with its
verified exact inventory path **instead of** `--original-backup`.

After reviewing the restore preview, repeat with `--apply`. To replace differing
values, include `--overwrite` in both the preview and apply. Restoring a backup
does not delete values or definitions that are absent from it. These examples
verify vCenter certificates by default; see section 3 for the lab certificate option.

If policy requires the failover target's Global definitions to be removed after
failback succeeds, add the explicit cleanup options to the restore command:

```powershell
.\.venv\Scripts\python.exe -m vcattr restore --vcenter $sourceVcenter --user $sourceUser --backup $valuesBackup --original-backup .\backups\original-vm.json --insecure --apply --overwrite --delete-target-definitions --cleanup-vcenter $targetVcenter --cleanup-user $targetUser --cleanup-insecure
```

Omit `--overwrite` if the reviewed plan does not require replacing source values.
The cleanup backup/password use the `backups` folder and `VCENTER_CLEANUP_PASSWORD`
by default; the CLI securely prompts if the variable is unset in an interactive
PowerShell session. After the source restore succeeds, the tool saves a full
backup of the target and requires a **second** confirmation: type the exact target
vCenter address. Only then does it delete Global definitions whose names appear
in the original VM backup.

This deletion is site-wide, not VM-scoped: vCenter removes each Global
definition and its values from every target object, including values that
existed before failover. Definitions are not tracked by ownership, so matching
pre-existing definitions are deleted too. The safety backup contains the target
inventory values and definitions, but restoration is a separate manual operation.
Do not enable this cleanup if any target VM or administrator still needs those
attributes. If restore has unresolved conflicts/unmatched objects, target
connection fails, safety backup cannot be saved, or cleanup is not confirmed,
definitions are left in place. If deletion partially fails, the command reports
each error and the saved target backup path.

## 1. Download the project and create the environment

Run once from a folder where you want the project stored:

```powershell
cd $HOME
git clone https://github.com/Harshit-yadav01/vcenter-custom-attributes.git
cd vcenter-custom-attributes
python -m venv .venv
```

- `cd $HOME` changes to your Windows user directory.
- `git clone` downloads the project into a new `vcenter-custom-attributes` folder.
  Do not repeat it into an existing checkout.
- `cd vcenter-custom-attributes` enters the project folder.
- `python -m venv .venv` creates an isolated Python environment.

For an existing checkout under your user directory, reopen it with:

```powershell
Set-Location (Join-Path $HOME "vcenter-custom-attributes")
```

## 2. Install and verify dependencies

The normal installation was attempted but failed due to certificate verification:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

This normally installs the project in editable mode and its `pyvmomi` dependency.
With that installation blocked, the following lab workaround succeeded:

```powershell
.\.venv\Scripts\python.exe -m pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org "pyvmomi>=8.0.1"
```

**Security warning:** these options bypass certificate verification for the named
package hosts during this command, reducing download security. Use only if your
organization permits it. An approved CA bundle or internal package index is
preferable. This workaround is not required on machines with working certificate
trust and does not change pip configuration permanently.

Verify the dependency:

```powershell
.\.venv\Scripts\python.exe -c "from pyVim.connect import SmartConnect; print('Dependency installed successfully')"
```

This imports the vCenter connection helper from `pyvmomi`; it does not connect to
vCenter. Installing `pyvmomi` allowed `python -m vcattr` to run directly from the
project checkout even though editable installation had failed.

## 3. Enter connection details

These are convenience variables for the commands below, not an additional
previously executed operation. Enter the source and the actual intended target.
Addresses and backup contents are omitted from this guide to avoid publishing
internal inventory details.

```powershell
$sourceVcenter = Read-Host "Enter source vCenter IP or hostname"
$targetVcenter = Read-Host "Enter intended target vCenter IP or hostname"
$sourceUser = Read-Host "Enter source vCenter username"
$targetUser = Read-Host "Enter target vCenter username"
```

Passwords are prompted securely by the CLI unless `VCENTER_PASSWORD` is already
set. Different vCenters can use different passwords; do not put passwords into
commands, this document, or Git.

The vCenter commands below include `--insecure` because that was needed in the lab.
It disables vCenter certificate verification while retaining encryption. Use it
only for a trusted endpoint; omit it when certificate trust is configured.
This option is separate from pip's package-download certificate settings.

## 4. Back up the source custom attributes

The initial backup included all supported object types:

```powershell
.\.venv\Scripts\python.exe -m vcattr backup --vcenter $sourceVcenter --user $sourceUser --out .\backups\source-custom-attributes.json --insecure
```

It saved 10 definitions and 4 values on one VM in the observed lab run.
The later VM-focused backup used:

```powershell
.\.venv\Scripts\python.exe -m vcattr backup --vcenter $sourceVcenter --user $sourceUser --out .\backups\source-custom-attributes.json --types vm --insecure
```

How it works:

1. Connects to the source with the supplied username and prompted password.
2. Reads definitions and per-object values without modifying vCenter.
3. `--types vm` keeps VM-specific and Global definitions, and VM objects.
4. `--out` saves the JSON on the computer running the command, not inside vCenter.
   The backup directory is created if needed. Reusing the same filename replaces
   the previous file.

The VM-focused lab backup contained 4 Global definitions and 4 values on one VM.
Store a protected copy at the secondary site before a source-site outage.
The JSON contains inventory and attribute data; do not commit it to Git.

## 5. Inspect the saved backup

```powershell
.\.venv\Scripts\python.exe -m vcattr show .\backups\source-custom-attributes.json
```

Reads the local JSON and prints its source, timestamp, definitions, objects, and
values. No vCenter connection or password is needed. It does not refresh the
backup from the source.

To open the local backup folder for copying through an approved transfer method:

```powershell
explorer.exe .\backups
```

This helper was suggested; its execution was not confirmed.

## 6. Preview the target changes

```powershell
.\.venv\Scripts\python.exe -m vcattr failover --vcenter $targetVcenter --user $targetUser --backup .\backups\source-custom-attributes.json --types vm --insecure
```

- Reads the saved source backup and the target's current inventory.
- Matches VMs by type and exact name, not their source inventory folder.
- Reports missing definitions, values to set, conflicts, and unmatched objects.
- Does not modify the target because `--apply` is absent.
- Does not need the source vCenter to be available.

The latest confirmed preview showed 4 existing definitions and one unmatched VM.
`--overwrite` cannot resolve a missing or ambiguous VM match.

## 7. Apply definitions and matched VM values

```powershell
.\.venv\Scripts\python.exe -m vcattr failover --vcenter $targetVcenter --user $targetUser --backup .\backups\source-custom-attributes.json --types vm --insecure --apply
```

Review the plan, then type the exact target hostname/IP when prompted.
Before modifying the target, the tool saves its current attributes under
`backups/<target>-before-failover-<timestamp>.json`.

The confirmed run created 4 definitions but set 0 values because the source VM
was not found on the target. It completed with unresolved items, not a fully
successful value restore. Exit code 3 indicates items need attention.

**Global definitions versus values:** Global attribute names are available across
eligible target objects, so other VMs can display the names with blank values.
That is expected and does not mean values were copied to every VM. Values are
assigned only to matched objects.

Existing differing values are preserved and reported as conflicts. Add
`--overwrite` only after reviewing and intentionally approving their replacement.
Values absent from the source backup are not deleted.

## 8. After VM recovery: optional name mapping

This step is pending verification. Recover the VM through the configured SRM
workflow first. `vcattr failover` only restores attributes; it cannot recover,
register, or power on a VM.

If the recovered VM has the same name, rerun the preview in section 6.
If its name differs, create a mapping:

```powershell
$sourceVmName = Read-Host "Enter the exact source VM name in the backup"
$targetVmName = Read-Host "Enter the exact recovered VM name on the target"
@([pscustomobject]@{type="vm"; source_name=$sourceVmName; target_name=$targetVmName}) | Export-Csv -Path .\name-map.csv -NoTypeInformation -Encoding UTF8
```

This writes a local CSV mapping the source VM name to the target VM name.
It replaces an existing mapping file, does not rename a VM, and does not modify
vCenter. Only map to the intended recovered VM; do not select an unrelated VM.

Preview with the mapping:

```powershell
.\.venv\Scripts\python.exe -m vcattr failover --vcenter $targetVcenter --user $targetUser --backup .\backups\source-custom-attributes.json --types vm --name-map .\name-map.csv --insecure
```

After confirming the intended VM and proposed values, apply:

```powershell
.\.venv\Scripts\python.exe -m vcattr failover --vcenter $targetVcenter --user $targetUser --backup .\backups\source-custom-attributes.json --types vm --name-map .\name-map.csv --insecure --apply
```

Rerun the same preview afterward. A fully matched, successfully applied backup
should require no changes; `--verbose` also lists already-correct values.
Verify the values on the intended VM in the target vSphere Client.

## 9. Restore and rollback limitations

This section is explanatory; rollback has not been verified in the lab.
`restore` uses the same planning and apply behavior as `failover`.
For attribute failback, back up the active secondary site, then restore that
backup to the original site after its VMs have been recovered there. Preview first.

Restoring a target's pre-apply safety backup with `--overwrite` can restore
overwritten values. **It is not an exact rollback:** definitions created by
the operation and newly added values remain because the tool never deletes them.
VM failback, reprotection, and VM rollback are separate SRM operations.
