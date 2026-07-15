# Local client dogfood procedure

This procedure produces sanitized, machine-specific evidence under
`build/dogfood/`. That directory is ignored and must not be committed. The JSON
schema cannot store credentials, prompts, responses, transcripts, raw client
output, or environment dumps. A missing account, binary, exact pinned version,
or observation remains `not_verified`.

## Required order

1. Build the source distribution and wheel, inspect both archives, install the
   wheel into an isolated environment, and pass `verify_installed_tool.py`.
2. Install that exact verified wheel as the local `memory` tool only after all
   deterministic source, Rust, package, Cursor, and Gemini-render gates pass.
3. Run `memory setup cursor` and `memory setup gemini`. Allow Cursor/Gemini to
   display their normal reload, trust, and consent UI; never bypass it. If the
   exact client or an interactive terminal is unavailable, record
   `not_verified` and use project-direct setup only for diagnostics.
4. Through a Cursor-bound `memory_save`, seed one nonsecret `CURSOR-<UUID>`
   marker with a distinct operation UUID; repeat the identical call and require
   `replayed`. Do the same through a Gemini-bound server using a different
   `GEMINI-<UUID>` marker and operation UUID. Create the two closed marker files
   with `record-bound-marker`; enter only the returned memory UUIDs.
5. Start Cursor IDE 3.11.19 in a fresh single-root task. Observe whether it can
   retrieve the Cursor marker and append the manual closed evidence codes. Do
   not copy the prompt or response.
6. Run Cursor Agent CLI build `2026.07.09-a3815c0` once interactively in the
   same disposable workspace. Record that manual cell separately, then run the
   automated `probe` and `headless` commands.
7. With Gemini CLI 0.50.0, run extension discovery and the automated headless
   smoke so `BeforeAgent` behavior is observed at the pinned version.
8. Retrieve both resulting memory IDs through Cursor, Gemini, Claude Code, and
   Codex wherever those bound clients are actually available. Missing clients
   are not verified; a generic unbound save cannot replace either bound marker.
9. In isolated temporary homes, exercise missing executable, missing MCP,
   embedding failure, and abrupt client exit. Never damage active user
   configuration to simulate a failure.
10. Keep every unavailable cell `not_verified`. Create and validate a
    `client_verified` final report only when every pinned automated/manual cell
    and both replayed bound markers satisfy the strict gate. Otherwise retain
    the partial report or assemble only an explicitly `not_verified` final.

## Sanitized command surface

Create one stable partial report. Every append requires the same validated Git
commit and rejects a duplicate client/command cell:

```bash
COMMIT="$(git rev-parse --short HEAD)"
mkdir -p build/dogfood

uv run python scripts/smoke_clients.py probe \
  --client cursor-cli --workspace . \
  --output build/dogfood/partial-smoke.json \
  --expected-version 2026.07.09-a3815c0 --commit "$COMMIT"

uv run python scripts/smoke_clients.py probe \
  --client gemini-cli --workspace . \
  --output build/dogfood/partial-smoke.json \
  --expected-version 0.50.0 --commit "$COMMIT"
```

`headless` additionally requires a nonsecret marker UUID. `manual` accepts only
the Cursor IDE/manual and Cursor CLI/interactive pairs plus closed version,
status, reason, and evidence enums; it does not launch a client. Use
`record-bound-marker` only after an identical bound save replay returned the
same memory UUID.

Finalize with exactly one Cursor and one Gemini marker file. The two marker,
operation, and memory IDs must all be distinct:

```bash
uv run python scripts/smoke_clients.py finalize \
  --partial build/dogfood/partial-smoke.json \
  --bound-marker build/dogfood/cursor-bound-marker.json \
  --bound-marker build/dogfood/gemini-bound-marker.json \
  --claim not_verified \
  --output build/dogfood/not-verified-final.json
```

Only a report built with `--claim client_verified` and successfully reparsed by
this exact command supports that claim:

```bash
uv run python scripts/smoke_clients.py validate-final \
  --report build/dogfood/client-verified-final.json \
  --require-claim client_verified
```

Generated evidence remains local until a separate user decision explicitly
authorizes attaching or publishing it.
