"""Markdown rendering and session file writing for memories."""

from __future__ import annotations

import locale
import json
import math
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from memory.models import CATEGORY_HEADINGS, VALID_CATEGORIES, Memory, MemoryOperation

ARCHIVED_HEADING = "Archived"
MEMORY_ID_PREFIX = "<!-- memory-id:"
METADATA_V2_PREFIX = "<!-- echovault-metadata-v2:"
SCHEMA_V2_METADATA_KEYS = frozenset({
    "project",
    "tags",
    "category",
    "related_files",
    "section_anchor",
    "created_at",
    "updated_at",
    "status",
    "archived_at",
    "archive_reason",
    "superseded_by",
    "structured_data",
    "confidence",
    "valid_from",
    "valid_until",
    "commit_sha",
    "branch",
    "links",
    "last_verified",
    "creator_source",
    "last_updated_by",
    "contributors",
    "operations",
    "content_fingerprint",
    "history_complete",
    "updated_count",
})
MEMORY_OPERATION_KEYS = frozenset({
    "operation_id",
    "source",
    "action",
    "request_fingerprint",
    "timestamp",
    "branch",
    "commit_sha",
})
SCHEMA_V1_MIGRATION_COMMAND = "memory migrate vault-metadata"


class LegacySchemaWriteError(ValueError):
    """Raised when a write targets a schema-v1 session file."""

    def __init__(self) -> None:
        super().__init__(
            "Schema-v1 session files are read-only; run "
            f"'{SCHEMA_V1_MIGRATION_COMMAND}' before writing."
        )


@dataclass
class SessionEntry:
    """Parsed memory section from a session markdown file."""

    id: Optional[str]
    title: str
    what: str
    why: Optional[str]
    impact: Optional[str]
    source: Optional[str]
    details: Optional[str]
    category: Optional[str]
    status: str = "active"
    archived_at: Optional[str] = None
    archive_reason: Optional[str] = None
    superseded_by: Optional[str] = None
    section_anchor: Optional[str] = None
    living_data: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    metadata_complete: bool = False

    def to_memory(self, file_path: str) -> Memory:
        """Rebuild a Memory from a complete schema-v2 session entry."""
        if not self.metadata_complete:
            raise ValueError("Cannot rebuild Memory from incomplete schema-v2 metadata")
        metadata = _validate_v2_metadata(self.metadata)
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("Schema-v2 entry requires a stable memory ID")
        if not isinstance(self.title, str) or not isinstance(self.what, str):
            raise ValueError("Schema-v2 readable title and what fields must be strings")
        for name, value in (
            ("why", self.why),
            ("impact", self.impact),
            ("source", self.source),
        ):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Schema-v2 readable {name} field must be a string or null")

        return Memory(
            id=self.id,
            title=self.title,
            what=self.what,
            why=self.why,
            impact=self.impact,
            tags=list(metadata["tags"]),
            category=metadata["category"],
            project=metadata["project"],
            source=self.source,
            related_files=list(metadata["related_files"]),
            file_path=file_path,
            section_anchor=metadata["section_anchor"],
            created_at=metadata["created_at"],
            updated_at=metadata["updated_at"],
            status=metadata["status"],
            archived_at=metadata["archived_at"],
            archive_reason=metadata["archive_reason"],
            superseded_by=metadata["superseded_by"],
            structured_data=deepcopy(metadata["structured_data"]),
            confidence=metadata["confidence"],
            valid_from=metadata["valid_from"],
            valid_until=metadata["valid_until"],
            commit_sha=metadata["commit_sha"],
            branch=metadata["branch"],
            links=list(metadata["links"]),
            last_verified=metadata["last_verified"],
            creator_source=metadata["creator_source"],
            last_updated_by=metadata["last_updated_by"],
            contributors=list(metadata["contributors"]),
            operations=[MemoryOperation(**operation) for operation in metadata["operations"]],
            content_fingerprint=metadata["content_fingerprint"],
            history_complete=metadata["history_complete"],
            updated_count=metadata["updated_count"],
        )


@dataclass
class SessionDocument:
    """Parsed session markdown file."""

    project: str
    created: Optional[str]
    tags: list[str]
    sources: list[str]
    title: str
    entries: list[SessionEntry]
    schema_version: int = 2


def _is_optional_string(value: object) -> bool:
    return value is None or isinstance(value, str)


def _is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _validate_json_native(value: object, path: str = "metadata") -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"Schema-v2 {path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, nested in enumerate(value):
            _validate_json_native(nested, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError(f"Schema-v2 {path} contains a non-string object key")
            _validate_json_native(nested, f"{path}.{key}")
        return
    raise ValueError(
        f"Schema-v2 {path} contains non-JSON-native type {type(value).__name__}"
    )


def _validate_memory_operation(operation: object, index: int) -> dict:
    if not isinstance(operation, dict):
        raise ValueError(f"Schema-v2 operation {index} must be an object")
    if set(operation) != MEMORY_OPERATION_KEYS:
        raise ValueError(f"Schema-v2 operation {index} has invalid keys")
    for name in ("operation_id", "action", "request_fingerprint", "timestamp"):
        if not isinstance(operation[name], str):
            raise ValueError(f"Schema-v2 operation {index} field {name} must be a string")
    for name in ("source", "branch", "commit_sha"):
        if not _is_optional_string(operation[name]):
            raise ValueError(
                f"Schema-v2 operation {index} field {name} must be a string or null"
            )
    return operation


def _validate_v2_metadata(metadata: object) -> dict:
    """Validate the complete, typed schema-v2 per-entry metadata object."""
    if not isinstance(metadata, dict):
        raise ValueError("Schema-v2 metadata must be an object")
    _validate_json_native(metadata)

    keys = set(metadata)
    missing = sorted(SCHEMA_V2_METADATA_KEYS - keys)
    unexpected = sorted(keys - SCHEMA_V2_METADATA_KEYS)
    if missing or unexpected:
        parts = []
        if missing:
            parts.append(f"missing keys: {', '.join(missing)}")
        if unexpected:
            parts.append(f"unexpected keys: {', '.join(unexpected)}")
        raise ValueError("Invalid schema-v2 metadata (" + "; ".join(parts) + ")")

    for name in ("project", "section_anchor", "created_at", "updated_at", "status"):
        if not isinstance(metadata[name], str):
            raise ValueError(f"Schema-v2 metadata field {name} must be a string")
    for name in (
        "category",
        "archived_at",
        "archive_reason",
        "superseded_by",
        "valid_from",
        "valid_until",
        "commit_sha",
        "branch",
        "last_verified",
        "creator_source",
        "last_updated_by",
        "content_fingerprint",
    ):
        if not _is_optional_string(metadata[name]):
            raise ValueError(f"Schema-v2 metadata field {name} must be a string or null")
    for name in ("tags", "related_files", "links", "contributors"):
        if not _is_string_list(metadata[name]):
            raise ValueError(f"Schema-v2 metadata field {name} must be a list of strings")
    if not isinstance(metadata["structured_data"], dict):
        raise ValueError("Schema-v2 metadata field structured_data must be an object")
    confidence = metadata["confidence"]
    if confidence is not None and (
        isinstance(confidence, bool) or not isinstance(confidence, (int, float))
    ):
        raise ValueError("Schema-v2 metadata field confidence must be a number or null")
    if not isinstance(metadata["history_complete"], bool):
        raise ValueError("Schema-v2 metadata field history_complete must be a boolean")
    if isinstance(metadata["updated_count"], bool) or not isinstance(
        metadata["updated_count"], int
    ):
        raise ValueError("Schema-v2 metadata field updated_count must be an integer")
    if not isinstance(metadata["operations"], list):
        raise ValueError("Schema-v2 metadata field operations must be a list")
    for index, operation in enumerate(metadata["operations"]):
        _validate_memory_operation(operation, index)

    return metadata


def _metadata_from_memory(mem: Memory) -> dict:
    return {
        "project": mem.project,
        "tags": list(mem.tags),
        "category": mem.category,
        "related_files": list(mem.related_files),
        "section_anchor": mem.section_anchor,
        "created_at": mem.created_at,
        "updated_at": mem.updated_at,
        "status": mem.status,
        "archived_at": mem.archived_at,
        "archive_reason": mem.archive_reason,
        "superseded_by": mem.superseded_by,
        "structured_data": deepcopy(mem.structured_data),
        "confidence": mem.confidence,
        "valid_from": mem.valid_from,
        "valid_until": mem.valid_until,
        "commit_sha": mem.commit_sha,
        "branch": mem.branch,
        "links": list(mem.links),
        "last_verified": mem.last_verified,
        "creator_source": mem.creator_source,
        "last_updated_by": mem.last_updated_by,
        "contributors": list(mem.contributors),
        "operations": [asdict(operation) for operation in mem.operations],
        "content_fingerprint": mem.content_fingerprint,
        "history_complete": mem.history_complete,
        "updated_count": mem.updated_count,
    }


def _entry_from_memory(mem: Memory, details: Optional[str] = None) -> SessionEntry:
    return SessionEntry(
        id=mem.id,
        title=mem.title,
        what=mem.what,
        why=mem.why,
        impact=mem.impact,
        source=mem.source,
        details=details,
        category=mem.category,
        status=mem.status,
        archived_at=mem.archived_at,
        archive_reason=mem.archive_reason,
        superseded_by=mem.superseded_by,
        section_anchor=mem.section_anchor,
        living_data={
            "structured_data": deepcopy(mem.structured_data),
            "confidence": mem.confidence,
            "valid_from": mem.valid_from,
            "valid_until": mem.valid_until,
            "commit_sha": mem.commit_sha,
            "branch": mem.branch,
            "links": list(mem.links),
            "last_verified": mem.last_verified,
        },
        metadata=_metadata_from_memory(mem),
        metadata_complete=True,
    )


def read_markdown_text(file_path: Path) -> str:
    """Read a markdown file with encoding fallbacks."""
    encodings = ["utf-8-sig", "utf-8", locale.getpreferredencoding(False), "cp1251"]
    seen: set[str] = set()

    for encoding in encodings:
        if not encoding or encoding in seen:
            continue
        seen.add(encoding)
        try:
            return file_path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue

    return file_path.read_text(encoding="utf-8", errors="replace")


def normalize_markdown_content(content: str) -> str:
    """Normalize markdown text for line-based parsing."""
    return content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def make_section_anchor(title: str, occurrence: int = 1) -> str:
    """Create a stable section anchor, suffixing repeated titles."""
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "memory"
    if occurrence <= 1:
        return base
    return f"{base}-{occurrence}"


def render_section(mem: Memory, details: Optional[str] = None) -> str:
    """Render a single section from a Memory."""
    return render_entry(_entry_from_memory(mem, details), schema_version=2)


def _metadata_for_render(entry: SessionEntry) -> dict:
    if not entry.metadata_complete:
        raise ValueError("Cannot render schema-v2 entry with incomplete metadata")
    metadata = dict(entry.metadata)
    metadata.update({
        "category": entry.category,
        "section_anchor": entry.section_anchor,
        "status": entry.status,
        "archived_at": entry.archived_at,
        "archive_reason": entry.archive_reason,
        "superseded_by": entry.superseded_by,
    })
    return _validate_v2_metadata(metadata)


def _render_v2_json_record(label: str, value: Optional[str]) -> str:
    return (
        f"**{label}:** "
        + json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )


def _title_preview(title: str) -> str:
    return re.split(r"\r\n|\r|\n", title, maxsplit=1)[0]


def render_entry(entry: SessionEntry, *, schema_version: int = 1) -> str:
    """Render a single parsed session entry."""
    rendered_title = _title_preview(entry.title) if schema_version == 2 else entry.title
    lines = [f"### {rendered_title}"]
    if entry.id:
        lines.append(f"<!-- memory-id: {entry.id} -->")
    elif schema_version == 2:
        raise ValueError("Cannot render schema-v2 entry without a stable memory ID")
    if schema_version == 2:
        metadata_json = json.dumps(
            _metadata_for_render(entry),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).replace("--", "\\u002d\\u002d")
        lines.append(f"{METADATA_V2_PREFIX} {metadata_json} -->")
        lines.append(_render_v2_json_record("Title", entry.title))
        lines.append(_render_v2_json_record("What", entry.what))
        lines.append(_render_v2_json_record("Why", entry.why))
        lines.append(_render_v2_json_record("Impact", entry.impact))
        lines.append(_render_v2_json_record("Source", entry.source))
        lines.append(_render_v2_json_record("Details", entry.details))
    else:
        lines.append(f"**What:** {entry.what}")
        if entry.why is not None:
            lines.append(f"**Why:** {entry.why}")
        if entry.impact is not None:
            lines.append(f"**Impact:** {entry.impact}")
        if entry.source is not None:
            lines.append(f"**Source:** {entry.source}")
    living_data = {k: v for k, v in (entry.living_data or {}).items() if v not in (None, [], {}, "")}
    if living_data:
        lines.append(
            "**Living Memory:** "
            + json.dumps(living_data, sort_keys=True, allow_nan=False)
        )

    if schema_version == 1 and entry.status == "archived":
        if entry.category is not None:
            lines.append(f"**Category:** {entry.category}")
        if entry.archived_at is not None:
            lines.append(f"**Archived:** {entry.archived_at}")
        if entry.archive_reason is not None:
            lines.append(f"**Archive Reason:** {entry.archive_reason}")
        if entry.superseded_by is not None:
            lines.append(f"**Superseded By:** {entry.superseded_by}")

    if schema_version == 1 and entry.details is not None:
        lines.append("")
        lines.append("<details>")
        lines.append(entry.details)
        lines.append("</details>")

    return "\n".join(lines)


def parse_session_file(file_path: str | Path) -> SessionDocument:
    """Parse a session markdown file into structured entries."""
    path = Path(file_path)
    content = normalize_markdown_content(read_markdown_text(path))
    frontmatter, body = _split_frontmatter(content)
    frontmatter_data = _parse_frontmatter(frontmatter)
    schema_version = _parse_schema_version(frontmatter_data.get("schema_version"))
    title = _extract_session_title(body, path)
    entries = _parse_entries(body, schema_version=schema_version)

    return SessionDocument(
        project=frontmatter_data.get("project", path.parent.name),
        created=frontmatter_data.get("created"),
        tags=frontmatter_data.get("tags", []),
        sources=frontmatter_data.get("sources", []),
        title=title,
        entries=entries,
        schema_version=schema_version,
    )


def render_session_document(
    document: SessionDocument,
    tags: Optional[list[str]] = None,
    sources: Optional[list[str]] = None,
) -> str:
    """Purely render a complete session document as Markdown."""
    if document.schema_version not in (1, 2):
        raise ValueError(f"Unsupported session schema version: {document.schema_version}")

    session_title = document.title or "Session"
    created = document.created or ""
    render_tags = sorted(tags if tags is not None else document.tags)
    render_sources = sorted(sources if sources is not None else document.sources)
    rendered_tags = [tag.replace("\n", "\\n").replace("\r", "\\r") for tag in render_tags]
    rendered_sources = [
        source.replace("\n", "\\n").replace("\r", "\\r")
        for source in render_sources
    ]

    lines = ["---"]
    if document.schema_version == 2:
        lines.append("schema_version: 2")
    lines.append(f"project: {document.project}")
    lines.append(f"sources: [{', '.join(rendered_sources)}]")
    lines.append(f"created: {created}")
    lines.append(f"tags: [{', '.join(rendered_tags)}]")
    lines.append("---")
    lines.append("")
    lines.append(f"# {session_title}")
    lines.append("")

    active_entries = [entry for entry in document.entries if entry.status != "archived"]
    archived_entries = [entry for entry in document.entries if entry.status == "archived"]

    for category in VALID_CATEGORIES:
        category_entries = [entry for entry in active_entries if entry.category == category]
        if not category_entries:
            continue
        lines.append(f"## {CATEGORY_HEADINGS[category]}")
        lines.append("")
        for entry in category_entries:
            lines.append(render_entry(entry, schema_version=document.schema_version))
            lines.append("")

    uncategorized_entries = [entry for entry in active_entries if entry.category not in VALID_CATEGORIES]
    for entry in uncategorized_entries:
        lines.append(render_entry(entry, schema_version=document.schema_version))
        lines.append("")

    if archived_entries:
        lines.append(f"## {ARCHIVED_HEADING}")
        lines.append("")
        for entry in archived_entries:
            lines.append(render_entry(entry, schema_version=document.schema_version))
            lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def write_session_document(
    file_path: str | Path,
    document: SessionDocument,
    *,
    tags: Optional[list[str]] = None,
    sources: Optional[list[str]] = None,
) -> None:
    """Write a rendered session document to disk as UTF-8."""
    if document.schema_version == 1:
        raise LegacySchemaWriteError()
    content = render_session_document(document, tags=tags, sources=sources)
    Path(file_path).write_text(content, encoding="utf-8")


def assign_entry_anchors(entries: list[SessionEntry]) -> None:
    """Assign stable anchors in document order."""
    counts: dict[str, int] = {}
    for entry in entries:
        base = make_section_anchor(entry.title)
        occurrence = counts.get(base, 0) + 1
        counts[base] = occurrence
        entry.section_anchor = make_section_anchor(entry.title, occurrence)


def upsert_session_memory_entry(
    document: SessionDocument,
    mem: Memory,
    details: Optional[str] = None,
) -> None:
    """Replace or append one canonical memory entry and refresh its anchor."""
    if document.schema_version == 1:
        raise LegacySchemaWriteError()

    replacement = _entry_from_memory(mem, details)
    for index, entry in enumerate(document.entries):
        if entry.id == mem.id:
            document.entries[index] = replacement
            break
    else:
        document.entries.append(replacement)

    assign_entry_anchors(document.entries)
    if replacement.section_anchor is None:
        raise ValueError(f"Unable to assign section anchor for memory {mem.id}")
    mem.section_anchor = replacement.section_anchor
    replacement.metadata["section_anchor"] = replacement.section_anchor


def write_session_memory(
    vault_project_dir: str,
    mem: Memory,
    date_str: str,
    details: Optional[str] = None,
) -> str:
    """Create or append to a session file."""
    file_path = Path(vault_project_dir) / f"{date_str}-session.md"
    if file_path.exists():
        document = parse_session_file(file_path)
        if document.schema_version == 1:
            raise LegacySchemaWriteError()
    else:
        document = SessionDocument(
            project=mem.project,
            created=datetime.now(timezone.utc).isoformat(),
            tags=[],
            sources=[],
            title=f"{date_str} Session",
            entries=[],
            schema_version=2,
        )
    if document.created is None:
        document.created = datetime.now(timezone.utc).isoformat()

    document.entries.append(_entry_from_memory(mem, details))
    assign_entry_anchors(document.entries)
    write_session_document(
        file_path,
        document,
        tags=sorted(set(document.tags + mem.tags)),
        sources=sorted(set(document.sources + ([mem.source] if mem.source else []))),
    )
    return str(file_path)


def _split_frontmatter(content: str) -> tuple[str, str]:
    """Split content into frontmatter and body."""
    normalized = normalize_markdown_content(content)
    if not normalized.startswith("---\n"):
        return "", normalized
    parts = normalized.split("---\n", 2)
    if len(parts) < 3:
        return "", normalized
    return "---\n" + parts[1] + "---", parts[2]


def _parse_frontmatter(frontmatter: str) -> dict:
    """Parse the small YAML-ish frontmatter used by session files."""
    data: dict[str, object] = {}
    seen_schema_version = False
    if not frontmatter:
        return data
    for line in frontmatter.split("\n"):
        if ":" not in line or line == "---":
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key == "schema_version":
            if seen_schema_version:
                raise ValueError("Duplicate schema_version in frontmatter")
            seen_schema_version = True
        if value.startswith("[") and value.endswith("]"):
            items = [item.strip() for item in value[1:-1].split(",") if item.strip()]
            data[key] = items
        else:
            data[key] = value
    return data


def _parse_schema_version(value: object) -> int:
    if value is None or value == "":
        return 1
    if isinstance(value, str) and value.strip() in {"1", "2"}:
        return int(value.strip())
    if isinstance(value, int) and not isinstance(value, bool) and value in {1, 2}:
        return value
    raise ValueError(f"Unsupported session schema version: {value}")


def _extract_session_title(body: str, path: Path) -> str:
    """Extract the session H1 title."""
    for line in body.split("\n"):
        if line.startswith("# "):
            return line[2:].strip()
    match = re.match(r"(\d{4}-\d{2}-\d{2})", path.stem)
    return f"{match.group(1) if match else path.stem} Session"


def _heading_to_category(heading: str) -> Optional[str]:
    for category, display in CATEGORY_HEADINGS.items():
        if display == heading:
            return category
    return None


def _parse_entries(body: str, *, schema_version: int = 1) -> list[SessionEntry]:
    """Parse section entries from a session body."""
    if schema_version == 2:
        return _parse_entries_v2(body)
    return _parse_entries_v1(body)


def _parse_entries_v1(body: str) -> list[SessionEntry]:
    """Parse legacy schema-v1 entries with the historical permissive rules."""
    entries: list[SessionEntry] = []
    current_category: Optional[str] = None
    current_status = "active"
    lines = body.split("\n")
    i = 0

    while i < len(lines):
        line = lines[i]
        if line.startswith("## "):
            heading = line[3:].strip()
            if heading == ARCHIVED_HEADING:
                current_category = None
                current_status = "archived"
            else:
                current_category = _heading_to_category(heading)
                current_status = "active"
        elif line.startswith("### "):
            title = line[4:].strip()
            entry = SessionEntry(
                id=None,
                title=title,
                what="",
                why=None,
                impact=None,
                source=None,
                details=None,
                category=current_category,
                status=current_status,
            )
            details_lines: list[str] = []
            in_details = False

            i += 1
            while i < len(lines) and not lines[i].startswith("### ") and not lines[i].startswith("## "):
                stripped = lines[i].strip()
                if in_details:
                    if stripped == "</details>":
                        in_details = False
                    else:
                        details_lines.append(lines[i])
                elif stripped == "<details>":
                    in_details = True
                elif stripped.startswith(MEMORY_ID_PREFIX):
                    entry.id = stripped.removeprefix(MEMORY_ID_PREFIX).removesuffix("-->").strip()
                elif stripped.startswith("**What:**"):
                    entry.what = stripped[len("**What:**"):].strip()
                elif stripped.startswith("**Why:**"):
                    entry.why = stripped[len("**Why:**"):].strip()
                elif stripped.startswith("**Impact:**"):
                    entry.impact = stripped[len("**Impact:**"):].strip()
                elif stripped.startswith("**Living Memory:**"):
                    try:
                        entry.living_data = json.loads(stripped[len("**Living Memory:**"):].strip())
                    except json.JSONDecodeError:
                        entry.living_data = {}
                elif stripped.startswith("**Source:**"):
                    entry.source = stripped[len("**Source:**"):].strip()
                elif stripped.startswith("**Category:**"):
                    entry.category = stripped[len("**Category:**"):].strip() or entry.category
                elif stripped.startswith("**Archived:**"):
                    entry.archived_at = stripped[len("**Archived:**"):].strip()
                elif stripped.startswith("**Archive Reason:**"):
                    entry.archive_reason = stripped[len("**Archive Reason:**"):].strip()
                elif stripped.startswith("**Superseded By:**"):
                    entry.superseded_by = stripped[len("**Superseded By:**"):].strip()
                i += 1

            entry.details = "\n".join(details_lines).strip() or None
            entries.append(entry)
            continue
        i += 1

    assign_entry_anchors(entries)
    return entries


def _load_schema_v2_json(payload: str, context: str) -> object:
    def reject_constant(value: str) -> None:
        raise ValueError(f"Non-finite JSON constant {value}")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON object key {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate_keys,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Invalid schema-v2 JSON in {context}") from exc
    _validate_json_native(value, context)
    return value


def _parse_memory_id_line(line: str) -> str:
    if not line.startswith(MEMORY_ID_PREFIX) or not line.endswith("-->"):
        raise ValueError("Schema-v2 memory ID comment is malformed")
    memory_id = line.removeprefix(MEMORY_ID_PREFIX).removesuffix("-->").strip()
    if not memory_id:
        raise ValueError("Schema-v2 entry requires a stable memory ID")
    return memory_id


def _parse_metadata_v2_line(line: str, memory_id: str) -> dict:
    if not line.startswith(METADATA_V2_PREFIX) or not line.endswith("-->"):
        raise ValueError(f"Schema-v2 metadata placement is malformed for memory {memory_id}")
    payload = line.removeprefix(METADATA_V2_PREFIX).removesuffix("-->").strip()
    metadata = _load_schema_v2_json(payload, f"metadata for memory {memory_id}")
    return _validate_v2_metadata(metadata)


def _parse_v2_json_record(line: str, label: str, memory_id: str) -> object:
    prefix = f"**{label}:**"
    if not line.startswith(prefix):
        raise ValueError(f"Missing schema-v2 {label} record for memory {memory_id}")
    payload = line[len(prefix):]
    if not payload.startswith(" "):
        raise ValueError(f"Schema-v2 {label} record requires one delimiter space")
    return _load_schema_v2_json(payload[1:], f"{label} for memory {memory_id}")


def _validate_v2_prelude(
    lines: list[str], first_entry_start: int, section_headings: set[str]
) -> None:
    """Require the exact renderer-owned body prefix before the first v2 entry."""
    prelude = lines[:first_entry_start]
    canonical_base = (
        len(prelude) >= 3
        and prelude[0] == ""
        and prelude[1].startswith("# ")
        and len(prelude[1]) > 2
        and prelude[2] == ""
    )
    if not canonical_base:
        raise ValueError("Invalid schema-v2 session body prelude")
    if len(prelude) == 3:
        return
    if (
        first_entry_start < len(lines)
        and len(prelude) == 5
        and prelude[3] in section_headings
        and prelude[4] == ""
    ):
        return
    raise ValueError("Unexpected schema-v2 content before first entry")


def _parse_entries_v2(body: str) -> list[SessionEntry]:
    """Parse canonical v2 entries with adjacent structural comments and JSON records."""
    lines = body.split("\n")
    entry_starts: list[int] = []
    consumed_comments: set[int] = set()

    for index, line in enumerate(lines):
        if not line.startswith("### "):
            continue
        id_index = index + 1
        metadata_index = index + 2
        if id_index >= len(lines) or not lines[id_index].startswith(MEMORY_ID_PREFIX):
            raise ValueError("Every schema-v2 entry heading must be followed by a stable ID")
        if metadata_index >= len(lines) or not lines[metadata_index].startswith(
            METADATA_V2_PREFIX
        ):
            raise ValueError(
                "Schema-v2 memory ID must be immediately followed by metadata"
            )
        entry_starts.append(index)
        consumed_comments.update({id_index, metadata_index})

    for index, line in enumerate(lines):
        stripped = line.strip()
        if (
            stripped.startswith(MEMORY_ID_PREFIX)
            or stripped.startswith(METADATA_V2_PREFIX)
        ) and index not in consumed_comments:
            raise ValueError("Stray or duplicate schema-v2 ID/metadata comment")

    entries: list[SessionEntry] = []
    seen_ids: set[str] = set()
    readable_labels = ("Title", "What", "Why", "Impact", "Source", "Details")
    section_headings = {
        *(f"## {heading}" for heading in CATEGORY_HEADINGS.values()),
        "## Archived",
    }
    first_entry_start = entry_starts[0] if entry_starts else len(lines)
    _validate_v2_prelude(lines, first_entry_start, section_headings)

    for position, start in enumerate(entry_starts):
        end = entry_starts[position + 1] if position + 1 < len(entry_starts) else len(lines)
        memory_id = _parse_memory_id_line(lines[start + 1])
        if memory_id in seen_ids:
            raise ValueError(f"Duplicate schema-v2 memory ID: {memory_id}")
        seen_ids.add(memory_id)
        metadata = _parse_metadata_v2_line(lines[start + 2], memory_id)
        fields: dict[str, object] = {}
        living_data: dict = {}
        cursor = start + 3
        for label in readable_labels:
            if cursor >= end:
                raise ValueError(
                    f"Missing schema-v2 readable field {label} for memory {memory_id}"
                )
            fields[label] = _parse_v2_json_record(lines[cursor], label, memory_id)
            cursor += 1

        living_prefix = "**Living Memory:**"
        if cursor < end and lines[cursor].startswith(living_prefix):
            payload = lines[cursor][len(living_prefix):]
            if not payload.startswith(" "):
                raise ValueError(
                    f"Living data record requires one delimiter space for memory {memory_id}"
                )
            loaded_living_data = _load_schema_v2_json(
                payload[1:], f"living data for memory {memory_id}"
            )
            if not isinstance(loaded_living_data, dict):
                raise ValueError(f"Living data for memory {memory_id} must be an object")
            living_data = loaded_living_data
            cursor += 1

        suffix = lines[cursor:end]
        valid_suffix = suffix == [""]
        if position + 1 < len(entry_starts):
            valid_suffix = valid_suffix or (
                len(suffix) == 3
                and suffix[0] == ""
                and suffix[1] in section_headings
                and suffix[2] == ""
            )
        if not valid_suffix:
            raise ValueError(
                f"Invalid schema-v2 entry-boundary suffix for memory {memory_id}"
            )

        if not isinstance(fields["Title"], str) or not isinstance(fields["What"], str):
            raise ValueError(f"Schema-v2 Title and What must be strings for memory {memory_id}")
        for label in ("Why", "Impact", "Source", "Details"):
            if fields[label] is not None and not isinstance(fields[label], str):
                raise ValueError(
                    f"Schema-v2 {label} must be a string or null for memory {memory_id}"
                )
        if lines[start][4:] != _title_preview(fields["Title"]):
            raise ValueError(f"Schema-v2 title preview does not match memory {memory_id}")

        entries.append(
            SessionEntry(
                id=memory_id,
                title=fields["Title"],
                what=fields["What"],
                why=fields["Why"],
                impact=fields["Impact"],
                source=fields["Source"],
                details=fields["Details"],
                category=metadata["category"],
                status=metadata["status"],
                archived_at=metadata["archived_at"],
                archive_reason=metadata["archive_reason"],
                superseded_by=metadata["superseded_by"],
                section_anchor=metadata["section_anchor"],
                living_data=living_data,
                metadata=metadata,
                metadata_complete=True,
            )
        )

    return entries
