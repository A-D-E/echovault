"""CLI commands for the memory system.

This module provides the command-line interface for managing memories.
All commands use the MemoryService for business logic.
"""

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, cast

import yaml

import click

from memory.config import (
    clear_persisted_memory_home,
    get_memory_home,
    load_config,
    resolve_memory_home,
    set_persisted_memory_home,
    resolve_context_mode,
)
from memory.core import MemoryService
from memory.integrations.registry import get_adapter
from memory.integrations.types import (
    InstallMode,
    InstallScope,
    IntegrationOptions,
)
from memory.models import RawMemoryInput
from memory.persistence import MemoryPatch
from memory.projects import (
    ProjectRegistry,
    ProjectResolutionError,
    build_project_identity,
    discover_project_root,
)
from memory.safe_io import LockTimeoutError

DETAILS_TEMPLATE = """\
Context:

Options considered:
- Option A:
- Option B:

Decision:

Tradeoffs:

Follow-up:
"""


AdminAction = Literal["create", "update", "archive", "restore", "merge", "delete"]


@dataclass(frozen=True)
class AdminMutationRequest:
    """Validated local mutation request for the hidden dashboard bridge."""

    action: AdminAction
    payload: dict[str, object]


class AdminMutationValidationError(ValueError):
    """Raised when a local admin mutation does not match the closed schema."""


_ADMIN_ALLOWED_FIELDS: dict[AdminAction, frozenset[str]] = {
    "create": frozenset(
        {
            "action",
            "title",
            "what",
            "why",
            "impact",
            "category",
            "tags",
            "source",
            "project",
            "details",
            "actor",
        }
    ),
    "update": frozenset(
        {
            "action",
            "memory_id",
            "title",
            "what",
            "why",
            "impact",
            "category",
            "tags",
            "details",
            "actor",
        }
    ),
    "archive": frozenset({"action", "memory_id", "reason", "actor"}),
    "restore": frozenset({"action", "memory_id", "actor"}),
    "merge": frozenset({"action", "canonical_id", "source_ids", "actor"}),
    "delete": frozenset({"action", "memory_id", "actor"}),
}

_ADMIN_REQUIRED_FIELDS: dict[AdminAction, frozenset[str]] = {
    "create": frozenset({"action", "title", "what", "project", "actor"}),
    "update": frozenset({"action", "memory_id", "actor"}),
    "archive": frozenset({"action", "memory_id", "reason", "actor"}),
    "restore": frozenset({"action", "memory_id", "actor"}),
    "merge": frozenset({"action", "canonical_id", "source_ids", "actor"}),
    "delete": frozenset({"action", "memory_id", "actor"}),
}

_ADMIN_REQUIRED_STRINGS: dict[AdminAction, frozenset[str]] = {
    "create": frozenset({"title", "what", "project"}),
    "update": frozenset({"memory_id"}),
    "archive": frozenset({"memory_id", "reason"}),
    "restore": frozenset({"memory_id"}),
    "merge": frozenset({"canonical_id"}),
    "delete": frozenset({"memory_id"}),
}

_ADMIN_NULLABLE_STRINGS: dict[AdminAction, frozenset[str]] = {
    "create": frozenset({"why", "impact", "category", "source", "details"}),
    "update": frozenset({"why", "impact", "category", "details"}),
    "archive": frozenset(),
    "restore": frozenset(),
    "merge": frozenset(),
    "delete": frozenset(),
}


def parse_admin_request(payload: object) -> AdminMutationRequest:
    """Validate one admin request without opening storage or running commands."""
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) for key in payload
    ):
        raise AdminMutationValidationError("request must be one JSON object")
    request_payload = cast(dict[str, object], payload)
    raw_action = request_payload.get("action")
    if not isinstance(raw_action, str) or raw_action not in _ADMIN_ALLOWED_FIELDS:
        raise AdminMutationValidationError(
            "action must be create, update, archive, restore, merge, or delete"
        )
    action = cast(AdminAction, raw_action)

    unknown = sorted(set(request_payload) - _ADMIN_ALLOWED_FIELDS[action])
    if unknown:
        raise AdminMutationValidationError(f"unknown field: {unknown[0]}")
    missing = sorted(_ADMIN_REQUIRED_FIELDS[action] - set(request_payload))
    if missing:
        raise AdminMutationValidationError(f"missing required field: {missing[0]}")

    actor = request_payload["actor"]
    if not isinstance(actor, str) or not actor.strip():
        raise AdminMutationValidationError("actor must be a non-empty string")

    for field in _ADMIN_REQUIRED_STRINGS[action]:
        value = request_payload[field]
        if not isinstance(value, str) or not value.strip():
            raise AdminMutationValidationError(
                f"{field} must be a non-empty string"
            )

    if action == "update":
        for field in ("title", "what"):
            if field in request_payload:
                value = request_payload[field]
                if not isinstance(value, str) or not value.strip():
                    raise AdminMutationValidationError(
                        f"{field} must be a non-empty string"
                    )

    for field in _ADMIN_NULLABLE_STRINGS[action]:
        if field not in request_payload:
            continue
        value = request_payload[field]
        if value is not None and not isinstance(value, str):
            raise AdminMutationValidationError(f"{field} must be a string or null")

    if "tags" in request_payload:
        tags = request_payload["tags"]
        if not isinstance(tags, list) or not all(
            isinstance(tag, str) for tag in tags
        ):
            raise AdminMutationValidationError("tags must be a list of strings")

    if action == "merge":
        source_ids = request_payload["source_ids"]
        if (
            not isinstance(source_ids, list)
            or not source_ids
            or not all(
                isinstance(source_id, str) and source_id.strip()
                for source_id in source_ids
            )
        ):
            raise AdminMutationValidationError(
                "source_ids must be a non-empty list of strings"
            )
        if len(set(source_ids)) != len(source_ids):
            raise AdminMutationValidationError("source_ids must be unique")
        if request_payload["canonical_id"] in source_ids:
            raise AdminMutationValidationError(
                "canonical_id cannot also be a source_id"
            )

    return AdminMutationRequest(action=action, payload=dict(request_payload))


def _parse_admin_json(payload: str) -> AdminMutationRequest:
    def reject_constant(value: str) -> object:
        raise AdminMutationValidationError(f"invalid JSON constant: {value}")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AdminMutationValidationError(f"duplicate field: {key}")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            payload,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except (json.JSONDecodeError, AdminMutationValidationError) as error:
        raise AdminMutationValidationError("request must be valid JSON") from error
    return parse_admin_request(decoded)


def _admin_result(
    result: object,
    *,
    fallback_id: str | None,
    default_status: str,
) -> tuple[str, str]:
    if result is False or result is None:
        raise RuntimeError("Mutation did not report success")
    status = default_status
    memory_id = fallback_id
    if isinstance(result, dict):
        raw_status = result.get("action")
        raw_memory_id = result.get("id")
        if isinstance(raw_status, str) and raw_status:
            status = raw_status
        if isinstance(raw_memory_id, str) and raw_memory_id:
            memory_id = raw_memory_id
    if not isinstance(memory_id, str) or not memory_id:
        raise RuntimeError("Mutation did not return a memory ID")
    return status, memory_id


def _apply_admin_request(
    service: MemoryService,
    request: AdminMutationRequest,
) -> tuple[str, str]:
    payload = request.payload
    actor = cast(str, payload["actor"])

    if request.action == "create":
        raw = RawMemoryInput(
            title=cast(str, payload["title"]),
            what=cast(str, payload["what"]),
            why=cast(str | None, payload.get("why")),
            impact=cast(str | None, payload.get("impact")),
            category=cast(str | None, payload.get("category")),
            tags=list(cast(list[str], payload.get("tags", []))),
            source=cast(str | None, payload.get("source")),
            details=cast(str | None, payload.get("details")),
        )
        result = service.save(
            raw,
            cast(str, payload["project"]),
            authoritative_source=actor,
        )
        return _admin_result(
            result,
            fallback_id=None,
            default_status="created",
        )

    if request.action == "update":
        patch_fields = {
            field: payload[field]
            for field in (
                "title",
                "what",
                "why",
                "impact",
                "category",
                "tags",
                "details",
            )
            if field in payload
        }
        memory_id = cast(str, payload["memory_id"])
        result = service.update_memory_record(
            memory_id,
            patch=MemoryPatch(**patch_fields),
            actor=actor,
        )
        return _admin_result(
            result,
            fallback_id=memory_id,
            default_status="updated",
        )

    if request.action == "archive":
        memory_id = cast(str, payload["memory_id"])
        result = service.archive_memory(
            memory_id,
            reason=cast(str, payload["reason"]),
            actor=actor,
        )
        return _admin_result(
            result,
            fallback_id=memory_id,
            default_status="archived",
        )

    if request.action == "restore":
        memory_id = cast(str, payload["memory_id"])
        result = service.restore_memory(memory_id, actor=actor)
        return _admin_result(
            result,
            fallback_id=memory_id,
            default_status="restored",
        )

    if request.action == "merge":
        canonical_id = cast(str, payload["canonical_id"])
        result = service.merge_memories(
            canonical_id,
            list(cast(list[str], payload["source_ids"])),
            actor=actor,
        )
        return _admin_result(
            result,
            fallback_id=canonical_id,
            default_status="merged",
        )

    memory_id = cast(str, payload["memory_id"])
    canonical_id = service.resolve_memory_id(memory_id)
    result = service.delete(canonical_id, actor=actor)
    return _admin_result(
        result,
        fallback_id=canonical_id,
        default_status="deleted",
    )


def _redact_api_keys(data: dict) -> dict:
    for section in ("embedding",):
        config = data.get(section)
        if isinstance(config, dict) and config.get("api_key"):
            config["api_key"] = "<redacted>"
    return data


@click.group()
@click.version_option(package_name="echovault", prog_name="echovault")
def main():
    """Memory — local memory for coding agents."""
    pass


@main.group(hidden=True)
def admin():
    """Trusted local mutation bridge."""
    pass


@admin.command("apply", hidden=True)
@click.option("--json-stdin", is_flag=True, required=True, hidden=True)
def admin_apply(json_stdin):
    """Apply one strictly validated JSON mutation from stdin."""
    if not json_stdin:  # pragma: no cover - enforced by Click
        raise click.ClickException("Invalid admin mutation request.")
    try:
        request = _parse_admin_json(click.get_text_stream("stdin").read())
    except AdminMutationValidationError:
        raise click.ClickException("Invalid admin mutation request.") from None

    try:
        service = MemoryService()
        try:
            status, memory_id = _apply_admin_request(service, request)
        finally:
            service.close()
    except Exception:
        raise click.ClickException("Admin mutation failed.") from None

    click.echo(
        json.dumps(
            {"status": status, "memory_id": memory_id},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


@main.command()
def init():
    """Initialize the memory vault."""
    home = get_memory_home()
    vault_dir = os.path.join(home, "vault")
    os.makedirs(vault_dir, exist_ok=True)
    click.echo(f"Memory vault initialized at {home}")


@main.group()
def project():
    """Manage collision-safe project identities."""
    pass


@project.command("adopt-legacy")
@click.argument("legacy_key")
@click.option(
    "--project-root",
    required=True,
    type=click.Path(path_type=Path, exists=True, file_okay=False),
    help="Project directory that should own the legacy alias.",
)
@click.option(
    "--force-reassign",
    is_flag=True,
    default=False,
    help="Reassign an alias that belongs to another project.",
)
def project_adopt_legacy(legacy_key, project_root, force_reassign):
    """Assign LEGACY_KEY to a collision-safe project identity."""
    memory_home, _ = resolve_memory_home()
    try:
        discovered = discover_project_root(project_root)
        identity = build_project_identity(*discovered)
        scope = ProjectRegistry(Path(memory_home)).adopt_legacy(
            legacy_key,
            identity,
            force_reassign=force_reassign,
        )
    except (ProjectResolutionError, LockTimeoutError, OSError) as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"Adopted legacy alias {legacy_key} for {scope.identity.key}")


@main.group(invoke_without_command=True)
@click.pass_context
def config(ctx):
    """Show or manage configuration."""
    if ctx.invoked_subcommand is None:
        home, source = resolve_memory_home()
        cfg = load_config(os.path.join(home, "config.yaml"))
        data = _redact_api_keys(asdict(cfg))
        data["memory_home"] = home
        data["memory_home_source"] = source
        click.echo(yaml.safe_dump(data, sort_keys=False))


@config.command("set-home")
@click.argument("path")
def config_set_home(path):
    """Persist memory home location (used when MEMORY_HOME is unset)."""
    resolved = set_persisted_memory_home(path)
    os.makedirs(resolved, exist_ok=True)
    os.makedirs(os.path.join(resolved, "vault"), exist_ok=True)
    click.echo(f"Persisted memory home: {resolved}")
    click.echo("Override anytime with MEMORY_HOME.")


@config.command("clear-home")
def config_clear_home():
    """Remove persisted memory home location from global config."""
    changed = clear_persisted_memory_home()
    if changed:
        click.echo("Cleared persisted memory home setting.")
    else:
        click.echo("No persisted memory home setting was found.")


_CONFIG_TEMPLATE = """\
# EchoVault configuration
# Docs: https://github.com/mraza007/echovault#configure-embeddings-optional

# Embedding provider for semantic search.
# Without this, keyword search (FTS5) still works.
embedding:
  provider: ollama              # ollama | openai
  model: nomic-embed-text
  # base_url: http://localhost:11434   # ollama default; for openai: https://api.openai.com/v1
  # api_key: sk-...            # required for openai

# How memories are retrieved at session start.
# "auto" uses vectors when available, falls back to keywords.
context:
  mode: auto                    # on | off | auto
  semantic: auto                # auto | always | never
  topup_recent: true            # also include recent memories
  token_budget: 1200            # approximate injected-context budget
  min_relevance: 0.70           # real-world nomic benchmark default
  min_vector_similarity: 0.05   # nomic-embed-text default; calibrate per model
  agent_modes: {}               # e.g. {codex: on, claude-code: off}
"""


def _write_context_mode(mode: str, agent: str | None = None) -> None:
    home = get_memory_home()
    path = os.path.join(home, "config.yaml")
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}
    context_data = data.setdefault("context", {})
    if agent:
        context_data.setdefault("agent_modes", {})[agent] = mode
    else:
        context_data["mode"] = mode
    os.makedirs(home, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)


@config.command("context")
@click.argument("mode", required=False, type=click.Choice(["on", "off", "auto"]))
@click.option("--agent", default=None, help="Set or inspect an agent-specific policy")
def config_context(mode, agent):
    """Show or set automatic agent-context policy."""
    if mode:
        _write_context_mode(mode, agent)
    cfg = load_config(os.path.join(get_memory_home(), "config.yaml"))
    effective, source = resolve_context_mode(cfg, agent)
    click.echo(f"context: {effective} ({source})")


@config.command("init")
@click.option("--force", is_flag=True, default=False, help="Overwrite existing config")
def config_init(force):
    """Generate a starter config.yaml."""
    home = get_memory_home()
    config_path = os.path.join(home, "config.yaml")

    if os.path.exists(config_path) and not force:
        click.echo(f"Config already exists at {config_path}")
        click.echo("Use --force to overwrite.")
        return

    os.makedirs(home, exist_ok=True)
    with open(config_path, "w") as f:
        f.write(_CONFIG_TEMPLATE)

    click.echo(f"Created {config_path}")
    click.echo("Edit the file to configure your embedding provider.")


@main.command()
@click.option("--title", required=True, help="Title of the memory")
@click.option("--what", required=True, help="What happened or was learned")
@click.option("--why", default=None, help="Why it matters")
@click.option("--impact", default=None, help="Impact or consequences")
@click.option("--tags", default="", help="Comma-separated tags")
@click.option(
    "--category",
    type=click.Choice([
        "decision", "pattern", "bug", "context", "learning", "playbook",
        "known_fix", "constraint", "project_state", "active_work",
    ]),
    default=None,
    help="Category of the memory",
)
@click.option("--related-files", default="", help="Comma-separated file paths")
@click.option("--details", default=None, help="Extended details or context")
@click.option("--details-file", default=None, help="Path to a file containing extended details")
@click.option("--details-template", is_flag=True, default=False, help="Use a structured details template")
@click.option("--source", default=None, help="Source of the memory")
@click.option("--project", default=None, help="Project name")
@click.option("--triggers", default="", help="Comma-separated playbook triggers")
@click.option("--prerequisites", default="", help="Comma-separated prerequisites")
@click.option("--steps", default="", help="Pipe-separated procedure steps")
@click.option("--verification", default="", help="Pipe-separated verification commands")
@click.option("--follow-ups", default="", help="Pipe-separated follow-ups")
@click.option("--constraints", default="", help="Pipe-separated constraints")
@click.option("--alternatives-rejected", default="", help="Pipe-separated rejected alternatives")
@click.option("--open-questions", default="", help="Pipe-separated open questions")
@click.option("--confidence", type=click.FloatRange(0.0, 1.0), default=None)
@click.option("--valid-from", default=None, help="ISO date/time when valid")
@click.option("--valid-until", default=None, help="ISO date/time expiry")
@click.option("--commit-sha", default=None)
@click.option("--branch", default=None)
@click.option("--links", default="", help="Comma-separated provenance links")
@click.option("--last-verified", default=None, help="ISO date/time last verified")
def save(
    title,
    what,
    why,
    impact,
    tags,
    category,
    related_files,
    details,
    details_file,
    details_template,
    source,
    project,
    triggers, prerequisites, steps, verification, follow_ups, constraints, alternatives_rejected,
    open_questions, confidence, valid_from, valid_until, commit_sha, branch,
    links, last_verified,
):
    """Save a memory to the current session."""
    project = project or os.path.basename(os.getcwd())
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    file_list = [f.strip() for f in related_files.split(",") if f.strip()] if related_files else []

    if details and details_file:
        raise click.UsageError("Use either --details or --details-file, not both.")

    resolved_details = details
    if details_file:
        try:
            with open(details_file) as f:
                resolved_details = f.read()
        except OSError as e:
            raise click.ClickException(f"Failed to read details file '{details_file}': {e}") from e

    if details_template and not (resolved_details or "").strip():
        resolved_details = DETAILS_TEMPLATE

    raw = RawMemoryInput(
        title=title,
        what=what,
        why=why,
        impact=impact,
        tags=tag_list,
        category=category,
        related_files=file_list,
        details=resolved_details,
        source=source,
        triggers=[v.strip() for v in triggers.split(",") if v.strip()],
        prerequisites=[v.strip() for v in prerequisites.split(",") if v.strip()],
        steps=[v.strip() for v in steps.split("|") if v.strip()],
        verification=[v.strip() for v in verification.split("|") if v.strip()],
        follow_ups=[v.strip() for v in follow_ups.split("|") if v.strip()],
        constraints=[v.strip() for v in constraints.split("|") if v.strip()],
        alternatives_rejected=[v.strip() for v in alternatives_rejected.split("|") if v.strip()],
        open_questions=[v.strip() for v in open_questions.split("|") if v.strip()],
        confidence=confidence, valid_from=valid_from, valid_until=valid_until,
        commit_sha=commit_sha, branch=branch,
        links=[v.strip() for v in links.split(",") if v.strip()],
        last_verified=last_verified,
    )

    svc = MemoryService()
    result = svc.save(raw, project=project)
    svc.close()

    click.echo(f"Saved: {title} (id: {result['id']})")
    click.echo(f"File: {result['file_path']}")
    for warning in result.get("warnings", []):
        click.echo(f"Warning: {warning}")


@main.command()
@click.argument("query")
@click.option("--limit", default=5, help="Maximum number of results")
@click.option(
    "--project",
    is_flag=True,
    default=False,
    help="Filter to current project (current directory name)",
)
@click.option("--source", default=None, help="Filter by source")
@click.option("--explain", is_flag=True, help="Show raw ranking diagnostics")
def search(query, limit, project, source, explain):
    """Search memories using hybrid FTS5 + semantic search."""
    project_name = os.path.basename(os.getcwd()) if project else None

    svc = MemoryService()
    results = svc.search(query, limit=limit, project=project_name, source=source)
    svc.close()

    if not results:
        click.echo("No results found.")
        return

    click.echo(f"\n Results ({len(results)} found) ")

    for i, r in enumerate(results, 1):
        score = r.get("score", 0)
        cat = r.get("category", "")
        proj = r.get("project", "")
        src = r.get("source", "")
        has_details = r.get("has_details", False)

        click.echo(f"\n [{i}] {r['title']} (score: {score:.2f})")
        click.echo(f"     {cat} | {r.get('created_at', '')[:10]} | {proj}" + (f" | {src}" if src else ""))
        click.echo(f"     What: {r['what']}")

        if r.get("why"):
            click.echo(f"     Why: {r['why']}")

        if r.get("impact"):
            click.echo(f"     Impact: {r['impact']}")

        if has_details:
            click.echo(f"     Details: available (use `memory details {r['id'][:12]}`)")
        if explain:
            click.echo("     Ranking: " + yaml.safe_dump(r.get("score_explain", {"mode": "fallback", "score": score}), default_flow_style=True).strip())


@main.command()
@click.argument("memory_id")
def details(memory_id):
    """Fetch full details for a specific memory."""
    svc = MemoryService()
    detail = svc.get_details(memory_id)
    svc.close()

    if not detail:
        click.echo(f"No details found for memory {memory_id}")
        return

    click.echo(detail.body)


@main.command()
@click.argument("memory_id")
def delete(memory_id):
    """Delete a memory by ID or prefix."""
    svc = MemoryService()
    deleted = svc.delete(memory_id)
    svc.close()

    if deleted:
        click.echo(f"Deleted memory {memory_id}")
    else:
        click.echo(f"No memory found for {memory_id}")


@main.command()
@click.option(
    "--project",
    is_flag=True,
    default=False,
    help="Filter to current project (current directory name)",
)
@click.option("--source", default=None, help="Filter by source")
@click.option("--limit", default=10, help="Maximum number of pointers")
@click.option("--query", default=None, help="Semantic search query for filtering")
@click.option("--agent", default=None, help="Agent name for policy override")
@click.option("--token-budget", type=int, default=None, help="Approximate context token budget")
@click.option(
    "--semantic",
    "semantic_mode",
    flag_value="always",
    default=None,
    help="Force semantic search (embeddings)",
)
@click.option(
    "--fts-only",
    "semantic_mode",
    flag_value="never",
    help="Disable embeddings and use FTS-only",
)
@click.option(
    "--show-config",
    is_flag=True,
    default=False,
    help="Show effective configuration and exit",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["hook", "agents-md"]),
    default="hook",
    help="Output format",
)
def context(project, source, limit, query, agent, token_budget, semantic_mode, show_config, output_format):
    """Output memory pointers for agent context injection."""
    import json

    if show_config:
        home = get_memory_home()
        cfg = load_config(os.path.join(home, "config.yaml"))
        data = _redact_api_keys(asdict(cfg))
        data["memory_home"] = home
        click.echo(yaml.safe_dump(data, sort_keys=False))
        return

    project_name = os.path.basename(os.getcwd()) if project else None

    svc = MemoryService()
    policy = svc.context_policy(agent)
    if not policy["enabled"]:
        click.echo(f"Automatic memory context is disabled ({policy['source']}).")
        svc.close()
        return
    results, total = svc.get_context(
        limit=limit,
        project=project_name,
        source=source,
        query=query,
        semantic_mode=semantic_mode,
        agent=agent,
        token_budget=token_budget,
    )
    svc.close()

    if not results:
        click.echo("No memories found.")
        return

    showing = len(results)

    if output_format == "agents-md":
        click.echo("## Memory Context\n")

    click.echo(f"Available memories ({total} total, showing {showing}):")

    for r in results:
        date_str = r.get("created_at", "")[:10]
        # Format date as "Mon DD" if possible
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(date_str)
            date_display = dt.strftime("%b %d")
        except (ValueError, TypeError):
            date_display = date_str

        title = r.get("title", "Untitled")
        cat = r.get("category", "")
        tags_raw = r.get("tags", "")
        if isinstance(tags_raw, str) and tags_raw:
            try:
                tags_list = json.loads(tags_raw)
            except (json.JSONDecodeError, TypeError):
                tags_list = []
        elif isinstance(tags_raw, list):
            tags_list = tags_raw
        else:
            tags_list = []

        cat_part = f" [{cat}]" if cat else ""
        tags_part = f" [{','.join(tags_list)}]" if tags_list else ""

        click.echo(f"- [{date_display}] {title}{cat_part}{tags_part}")
        if query and r.get("what"):
            click.echo(f"  {r['what']}")

    if output_format == "agents-md":
        click.echo("")
    click.echo('Use `memory search <query>` for full details on any memory.')


@main.command("evaluate")
@click.argument("golden_set", type=click.Path(exists=True, dir_okay=False))
@click.option("--limit", default=5)
@click.option("--project", default=None)
@click.option("--sweep", is_flag=True, help="Calibrate relevance thresholds over a standard grid")
@click.option("--min-recall", default=1.0, type=click.FloatRange(0.0, 1.0), help="Minimum recall required for sweep recommendations")
@click.option("--max-negative-fpr", default=0.0, type=click.FloatRange(0.0, 1.0), help="Maximum unrelated-query false-positive rate")
@click.option("--min-relevance", default=None, type=click.FloatRange(0.0, 1.0), help="Temporarily override ranked-result threshold")
@click.option("--min-vector-similarity", default=None, type=click.FloatRange(0.0, 1.0), help="Temporarily override vector threshold")
def evaluate_cmd(golden_set, limit, project, sweep, min_recall, max_negative_fpr, min_relevance, min_vector_similarity):
    """Evaluate retrieval against a redacted YAML golden set."""
    from memory.evaluation import evaluate, load_golden_set, sweep_thresholds
    svc = MemoryService()
    cases = load_golden_set(golden_set)
    if min_relevance is not None:
        svc.config.context.min_relevance = min_relevance
    if min_vector_similarity is not None:
        svc.config.context.min_vector_similarity = min_vector_similarity
    report = (
        sweep_thresholds(
            svc, cases, limit=limit, project=project,
            min_recall=min_recall, max_negative_fpr=max_negative_fpr,
        )
        if sweep else evaluate(svc, cases, limit=limit, project=project)
    )
    svc.close()
    click.echo(yaml.safe_dump(report, sort_keys=False))


@main.command("feedback")
@click.argument("event", type=click.Choice(["referenced", "dismissed"]))
@click.argument("memory_ids", nargs=-1, required=True)
def feedback_cmd(event, memory_ids):
    """Record local retrieval feedback for one or more memories."""
    svc = MemoryService()
    count = svc.db.record_feedback(list(memory_ids), event)
    svc.close()
    click.echo(f"Recorded {event} for {count} memories.")


@main.command("review")
@click.option("--project", default=None)
def review_cmd(project):
    """Propose lifecycle cleanup without changing memories."""
    from memory.health import lifecycle_review
    svc = MemoryService()
    report = lifecycle_review(svc.db, project)
    svc.close()
    click.echo(yaml.safe_dump(report, sort_keys=False))


@main.command("doctor")
@click.option("--project", default=None)
@click.option("--agent", default=None, help="Inspect one agent integration")
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Project root for agent integration diagnostics",
)
def doctor_cmd(project, agent, project_root):
    """Check vault, index, vectors, references, and lifecycle health."""
    from memory.health import doctor_home

    report = doctor_home(
        Path(get_memory_home()),
        project,
        agent=agent,
        project_root=project_root,
    )
    click.echo(yaml.safe_dump(report, sort_keys=False))


@main.group()
def migrate():
    """Run explicit, lossless storage migrations."""
    pass


@migrate.command("vault-metadata")
@click.option("--project", default=None, help="Migrate one project scope")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report migratable files without changing storage",
)
def migrate_vault_metadata_cmd(project, dry_run):
    """Enrich schema-v1 sessions from unambiguous SQLite metadata."""
    svc = (
        MemoryService(recover_pending=False, read_only=True)
        if dry_run
        else MemoryService()
    )
    try:
        report = svc.migrate_vault_metadata(project=project, dry_run=dry_run)
    finally:
        svc.close()
    click.echo(yaml.safe_dump(report, sort_keys=False))


@main.command("import")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would be imported without changing anything")
@click.option("--reindex", "do_reindex", is_flag=True, default=False, help="Run reindex after importing")
@click.option(
    "--reconcile",
    is_flag=True,
    default=False,
    help="Repair the derived index from complete canonical v2 Markdown",
)
@click.option(
    "--project",
    type=str,
    default=None,
    help="Limit reconciliation to one project",
)
def import_vault(dry_run, do_reindex, reconcile, project):
    """Import memories from vault markdown files into the local index.

    Scans all .md files in vault/ sub-directories, parses H3 memory
    sections, and inserts any that are missing from the local SQLite
    database.  Useful in multi-agent setups where new files arrive
    via file-sync (e.g. Syncthing) but are not yet indexed.

    Deduplication is by (project, file_path, section_anchor) — existing memories are skipped.
    """
    if dry_run and reconcile:
        raise click.UsageError("--dry-run cannot be combined with --reconcile")
    if project is not None and not reconcile:
        raise click.UsageError("--project requires --reconcile")
    if do_reindex and reconcile:
        raise click.UsageError("--reindex cannot be combined with --reconcile")

    svc = MemoryService(recover_pending=not reconcile)

    if dry_run:
        click.echo("Dry run — no changes will be made.\n")

    def progress(imported, skipped, project, title):
        if dry_run:
            click.echo(f"  [new] {project}/{title}")

    try:
        result = svc.import_from_vault(
            dry_run=dry_run,
            progress_callback=progress,
            reconcile=reconcile,
            project=project,
        )

        if reconcile:
            click.echo(yaml.safe_dump(result, sort_keys=False).rstrip())
            return

        click.echo(f"\nImported: {result['imported']}, Skipped (already exists): {result['skipped']}")
        if result["projects"]:
            click.echo(f"Projects with new imports: {', '.join(result['projects'])}")

        if do_reindex and result["imported"] > 0 and not dry_run:
            total = svc.db.count_memories()
            click.echo(f"\nReindexing {total} memories with {svc.config.embedding.provider}/{svc.config.embedding.model}...")

            def reindex_progress(current, count):
                click.echo(f"  {current}/{count}", nl=(current == count))
                if current < count:
                    click.echo("\r", nl=False)

            reindex_result = svc.reindex(progress_callback=reindex_progress)
            click.echo(
                f"Re-indexed {reindex_result['count']} memories with "
                f"{reindex_result['model']} ({reindex_result['dim']} dims)"
            )
    finally:
        svc.close()


@main.command()
def reindex():
    """Rebuild vector index with current embedding provider."""
    svc = MemoryService()

    total = svc.db.count_memories()
    if total == 0:
        click.echo("No memories to reindex.")
        svc.close()
        return

    click.echo(f"Reindexing {total} memories with {svc.config.embedding.provider}/{svc.config.embedding.model}...")

    def progress(current, count):
        click.echo(f"  {current}/{count}", nl=(current == count))
        if current < count:
            click.echo("\r", nl=False)

    result = svc.reindex(progress_callback=progress)
    svc.close()

    click.echo(
        f"Re-indexed {result['count']} memories with "
        f"{result['model']} ({result['dim']} dims)"
    )


@main.command()
@click.option("--limit", default=10, help="Maximum number of sessions to show")
@click.option("--project", default=None, help="Filter by project name")
def sessions(limit, project):
    """List recent sessions."""
    svc = MemoryService()
    vault = svc.vault_dir
    session_files = []

    if os.path.exists(vault):
        for proj_dir in sorted(os.listdir(vault)):
            proj_path = os.path.join(vault, proj_dir)
            if not os.path.isdir(proj_path) or proj_dir.startswith("."):
                continue
            if project and proj_dir != project:
                continue

            for f in sorted(os.listdir(proj_path), reverse=True):
                if f.endswith("-session.md"):
                    session_files.append((proj_dir, f))

    svc.close()

    if not session_files:
        click.echo("No sessions found.")
        return

    click.echo("\nSessions:")
    for proj, fname in session_files[:limit]:
        date_str = fname.replace("-session.md", "")
        click.echo(f"  {date_str} | {proj}")


@main.command()
@click.option("--project", default=None, help="Initial project filter for the dashboard")
@click.option("--include-archived", is_flag=True, default=False, help="Show archived memories on launch")
def dashboard(project, include_archived):
    """Launch the EchoVault terminal dashboard."""
    import shutil

    binary = shutil.which("memory-dashboard")
    if binary is None:
        click.echo("Error: memory-dashboard binary not found on PATH.")
        click.echo("Build it: cd dashboard && cargo build --release")
        click.echo("Install it: cp dashboard/target/release/memory-dashboard ~/.local/bin/")
        raise SystemExit(1)
    memory_executable = shutil.which("memory")
    if memory_executable is None:
        click.echo("Error: memory console script not found on PATH.")
        raise SystemExit(1)
    memory_executable = str(Path(memory_executable).resolve())

    cmd = [binary]
    if project:
        cmd.extend(["--project", project])
    if include_archived:
        cmd.append("--include-archived")

    memory_home = get_memory_home()
    os.environ["MEMORY_HOME"] = memory_home
    os.environ["ECHOVAULT_MEMORY_EXECUTABLE"] = memory_executable
    os.execvp(binary, cmd)


def _resolve_config_dir(agent_dot_dir: str, config_dir: str | None, project: bool) -> str:
    """Resolve the config directory for an agent.

    Args:
        agent_dot_dir: The dot-directory name (e.g. ".claude", ".cursor", ".codex").
        config_dir: Explicit --config-dir override (takes priority).
        project: If True, use cwd; if False, use home directory.
    """
    if config_dir:
        return config_dir
    if project:
        return os.path.join(os.getcwd(), agent_dot_dir)
    return os.path.join(os.path.expanduser("~"), agent_dot_dir)


@main.group()
def setup():
    """Install EchoVault hooks for an agent."""
    pass


@setup.command("claude-code")
@click.option("--config-dir", default=None, help="Path to .claude directory")
@click.option("--project", is_flag=True, default=False, help="Install in current project instead of globally")
def setup_claude_code_cmd(config_dir, project):
    """Install hooks into Claude Code settings."""
    from memory.setup import setup_claude_code

    target = _resolve_config_dir(".claude", config_dir, project)
    result = setup_claude_code(target, project=project)
    click.echo(result["message"])


@setup.command("cursor")
@click.option("--config-dir", default=None, help="Path to .cursor directory")
@click.option("--project", is_flag=True, default=False, help="Install in current project instead of globally")
@click.option(
    "--command",
    default=None,
    help="Exact EchoVault command (portable for project, executable for user)",
)
@click.option(
    "--force-managed",
    is_flag=True,
    default=False,
    help="Replace modified EchoVault-managed assets",
)
def setup_cursor_cmd(config_dir, project, command, force_managed):
    """Install curated EchoVault memory into Cursor."""
    explicit_root = config_dir is not None
    result = get_adapter("cursor").setup(
        IntegrationOptions(
            scope=(InstallScope.PROJECT if project else InstallScope.USER),
            mode=(InstallMode.DIRECT if project else InstallMode.NATIVE),
            config_root=(
                Path(config_dir)
                if config_dir is not None
                else (None if project else Path.home() / ".cursor")
            ),
            project_root=Path.cwd() if project else None,
            command=command,
            force_managed=force_managed,
            config_root_explicit=explicit_root,
        )
    )
    click.echo(result.message)
    for warning in result.warnings:
        click.echo(f"Warning: {warning}")


def _gemini_integration_options(
    *,
    config_dir: Path | None,
    direct: bool,
    project: bool,
    command: str | None,
    force_managed: bool,
) -> IntegrationOptions:
    if config_dir is not None and not (direct or project):
        raise click.UsageError(
            "--config-dir requires --direct or --project"
        )
    scope = InstallScope.PROJECT if project else InstallScope.USER
    mode = InstallMode.DIRECT if direct or project else InstallMode.NATIVE
    if config_dir is not None:
        config_root = config_dir.expanduser().resolve()
    elif project:
        config_root = None
    else:
        config_root = Path.home() / ".gemini"
    return IntegrationOptions(
        scope=scope,
        mode=mode,
        config_root=config_root,
        project_root=Path.cwd().resolve() if project else None,
        command=command,
        force_managed=force_managed,
        config_root_explicit=config_dir is not None,
    )


@setup.command("gemini")
@click.option(
    "--config-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Exact .gemini directory for a direct target",
)
@click.option(
    "--direct",
    is_flag=True,
    default=False,
    help="Install user-direct instead of the native extension",
)
@click.option(
    "--project",
    is_flag=True,
    default=False,
    help="Install direct integration in the current project",
)
@click.option(
    "--command",
    default=None,
    help="Exact EchoVault command",
)
@click.option(
    "--force-managed",
    is_flag=True,
    default=False,
    help="Replace modified EchoVault-managed assets",
)
def setup_gemini_cmd(
    config_dir: Path | None,
    direct: bool,
    project: bool,
    command: str | None,
    force_managed: bool,
) -> None:
    """Install curated EchoVault memory into Gemini CLI."""
    options = _gemini_integration_options(
        config_dir=config_dir,
        direct=direct,
        project=project,
        command=command,
        force_managed=force_managed,
    )
    result = get_adapter("gemini").setup(options)
    click.echo(result.message)
    for warning in result.warnings:
        click.echo(f"Warning: {warning}")


@setup.command("codex")
@click.option("--config-dir", default=None, help="Path to .codex directory")
@click.option("--project", is_flag=True, default=False, help="Install in current project instead of globally")
def setup_codex_cmd(config_dir, project):
    """Install EchoVault section into Codex AGENTS.md and config.toml."""
    from memory.setup import setup_codex

    target = _resolve_config_dir(".codex", config_dir, project)
    result = setup_codex(target)
    click.echo(result["message"])


@setup.command("opencode")
@click.option("--project", is_flag=True, default=False, help="Install in current project instead of globally")
def setup_opencode_cmd(project):
    """Install EchoVault MCP server into OpenCode."""
    from memory.setup import setup_opencode

    result = setup_opencode(project=project)
    click.echo(result["message"])


@main.group()
def uninstall():
    """Remove EchoVault hooks for an agent."""
    pass


@uninstall.command("claude-code")
@click.option("--config-dir", default=None, help="Path to .claude directory")
@click.option("--project", is_flag=True, default=False, help="Uninstall from current project instead of globally")
def uninstall_claude_code_cmd(config_dir, project):
    """Remove hooks from Claude Code settings."""
    from memory.setup import uninstall_claude_code

    target = _resolve_config_dir(".claude", config_dir, project)
    result = uninstall_claude_code(target, project=project)
    click.echo(result["message"])


@uninstall.command("cursor")
@click.option("--config-dir", default=None, help="Path to .cursor directory")
@click.option("--project", is_flag=True, default=False, help="Uninstall from current project instead of globally")
@click.option(
    "--force-managed",
    is_flag=True,
    default=False,
    help="Remove modified EchoVault-managed assets",
)
def uninstall_cursor_cmd(config_dir, project, force_managed):
    """Remove one curated EchoVault Cursor scope."""
    explicit_root = config_dir is not None
    result = get_adapter("cursor").uninstall(
        IntegrationOptions(
            scope=(InstallScope.PROJECT if project else InstallScope.USER),
            mode=(InstallMode.DIRECT if project else InstallMode.NATIVE),
            config_root=(
                Path(config_dir)
                if config_dir is not None
                else (None if project else Path.home() / ".cursor")
            ),
            project_root=Path.cwd() if project else None,
            command=None,
            force_managed=force_managed,
            config_root_explicit=explicit_root,
        )
    )
    click.echo(result.message)


@uninstall.command("gemini")
@click.option(
    "--config-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Exact .gemini directory for a direct target",
)
@click.option(
    "--direct",
    is_flag=True,
    default=False,
    help="Remove the user-direct integration",
)
@click.option(
    "--project",
    is_flag=True,
    default=False,
    help="Remove direct integration from the current project",
)
@click.option(
    "--force-managed",
    is_flag=True,
    default=False,
    help="Remove modified EchoVault-managed assets",
)
def uninstall_gemini_cmd(
    config_dir: Path | None,
    direct: bool,
    project: bool,
    force_managed: bool,
) -> None:
    """Remove one curated EchoVault Gemini scope."""
    options = _gemini_integration_options(
        config_dir=config_dir,
        direct=direct,
        project=project,
        command=None,
        force_managed=force_managed,
    )
    result = get_adapter("gemini").uninstall(options)
    click.echo(result.message)


@uninstall.command("codex")
@click.option("--config-dir", default=None, help="Path to .codex directory")
@click.option("--project", is_flag=True, default=False, help="Uninstall from current project instead of globally")
def uninstall_codex_cmd(config_dir, project):
    """Remove EchoVault from Codex AGENTS.md and config.toml."""
    from memory.setup import uninstall_codex

    target = _resolve_config_dir(".codex", config_dir, project)
    result = uninstall_codex(target)
    click.echo(result["message"])


@uninstall.command("opencode")
@click.option("--project", is_flag=True, default=False, help="Uninstall from current project instead of globally")
def uninstall_opencode_cmd(project):
    """Remove EchoVault from OpenCode."""
    from memory.setup import uninstall_opencode

    result = uninstall_opencode(project=project)
    click.echo(result["message"])


@main.group()
def hook() -> None:
    """Run supported agent lifecycle hooks."""
    pass


@hook.group("gemini")
def hook_gemini() -> None:
    """Run Gemini CLI hooks."""
    pass


@hook_gemini.command("before-agent")
def hook_gemini_before_agent_cmd() -> None:
    """Read one BeforeAgent event from stdin and emit one JSON response."""
    from memory.integrations.gemini_hook import handle_before_agent

    response: dict[str, object] = {}
    try:
        payload = json.loads(click.get_text_stream("stdin").read())
        if isinstance(payload, dict):
            observed = handle_before_agent(payload)
            if isinstance(observed, dict):
                response = observed
    except (OSError, UnicodeError, json.JSONDecodeError):
        response = {}
    click.echo(
        json.dumps(
            response,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    )


@main.command()
@click.option("--agent", envvar="MEMORY_AGENT", default=None)
@click.option(
    "--project-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
)
def mcp(agent: str | None, project_root: Path | None) -> None:
    """Start the EchoVault MCP server (stdio transport)."""
    import asyncio
    from memory.mcp_server import run_server

    asyncio.run(
        run_server(
            agent=agent,
            project_root=project_root,
            startup_cwd=Path.cwd(),
        )
    )


if __name__ == "__main__":
    main()
