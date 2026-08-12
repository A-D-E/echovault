# Cross-agent migration to 0.6

This guide covers the unreleased 0.6 storage and client integration changes.
Make a backup of `~/.memory/` plus only the affected EchoVault-owned client
manifests/configuration before changing an installation. Never copy credentials
into a migration report.

## 1. Inspect before changing

```bash
memory --version
memory doctor
memory doctor --agent cursor
memory doctor --agent gemini-cli
```

For each active repository, repeat both agent doctors with
`--project-root /absolute/project`. Cursor reports global/project state; Gemini
reports the native/user/project **N/U/P** state and any same-scope conflict.

## 2. Migrate vault metadata explicitly

EchoVault continues to read **schema v1** Markdown. Before a write intended to
enrich a v1 session with canonical v2 provenance, preview and then run the
lossless metadata migration:

```bash
memory migrate vault-metadata --dry-run
memory migrate vault-metadata
# or one logical project only
memory migrate vault-metadata --project my-project
```

The migration enriches only unambiguous records using local SQLite metadata.
It preserves undecidable content for review. Run `memory doctor`, then
`memory import` only when doctor reports index drift. Reconciliation is
crash-safe and preserves conflicts rather than guessing.

## 3. Upgrade client integrations

Rerun the selected setup command. Cursor migrates only the exact recognized
legacy EchoVault MCP entry; it does not rewrite a custom same-named server.
Gemini first inspects the complete N/U/P state and refuses native plus
user-direct coexistence in the same scope.

```bash
memory setup cursor
memory setup cursor --project
memory setup gemini
# or the deliberately selected fallback:
memory setup gemini --direct
memory setup gemini --project
```

If doctor reports a modified *managed* asset and the backup confirms it should
be replaced, rerun the matching command with `--force-managed`. Force is
restricted to EchoVault's ownership manifest; it never overwrites malformed,
custom, unowned, or symlink-escaped content.

## 4. Verify and roll back by scope

Restart the client, rerun both doctors, and verify the four MCP tools. Rollback
uses the matching scope-exact uninstall command:

```bash
memory uninstall cursor
memory uninstall cursor --project
memory uninstall gemini
memory uninstall gemini --direct
memory uninstall gemini --project
```

Uninstall removes only verified owned entries, files, and marked blocks. It
preserves unrelated configuration and the memory vault. If an owned artifact
was edited, uninstall refuses it unless `--force-managed` is explicitly used.
Restore a backed-up client file only after uninstall and only for that scope;
do not replace the canonical vault with a client-configuration backup.
