"""Three-layer secret redaction pipeline for memory system.

This module implements a comprehensive redaction system to ensure secrets
are never stored in agent memories:

1. Layer 1: Explicit <redacted> tags - User-marked sensitive content
2. Layer 2: Automatic pattern detection - Known secret formats (API keys, tokens, etc.)
3. Layer 3: Custom patterns from .memoryignore - Project-specific sensitive data
"""

import copy
from dataclasses import fields
import re
from typing import Optional

from memory.models import RawMemoryInput

# Regex patterns for known sensitive data formats
SENSITIVE_PATTERNS = [
    r"sk_live_[a-zA-Z0-9]+",                      # Stripe live keys
    r"sk_test_[a-zA-Z0-9]+",                      # Stripe test keys
    r"ghp_[a-zA-Z0-9]+",                          # GitHub personal access tokens
    r"AKIA[0-9A-Z]{16}",                          # AWS access key IDs
    r"xoxb-[a-zA-Z0-9-]+",                        # Slack bot tokens
    r"-----BEGIN (?:RSA )?PRIVATE KEY-----",      # Private keys (RSA and generic)
    r"eyJ[a-zA-Z0-9_-]+\.eyJ[a-zA-Z0-9_-]+",     # JWT tokens
    r"password\s*[:=]\s*[\"']?.+",                # Password fields
    r"secret\s*[:=]\s*[\"']?.+",                  # Secret fields
    r"api[_-]?key\s*[:=]\s*[\"']?.+",            # API key fields
]

# Pattern to match explicit <redacted> tags (including nested/multiline)
REDACTED_TAG_PATTERN = re.compile(r"<redacted>.*?</redacted>", re.DOTALL)


def redact(text: str, extra_patterns: Optional[list[str]] = None) -> str:
    """Redact sensitive information from text using three-layer approach.

    Args:
        text: The text to redact
        extra_patterns: Optional list of additional regex patterns to redact
                       (typically from .memoryignore file)

    Returns:
        Text with all sensitive information replaced with [REDACTED]

    Examples:
        >>> redact("My key is <redacted>secret</redacted>")
        'My key is [REDACTED]'
        >>> redact("API key: sk_live_abc123")
        'API key: [REDACTED]'
    """
    # Layer 1: Explicit <redacted> tags
    # Handle nested tags by repeatedly substituting until no more matches found
    while True:
        prev_text = text
        text = REDACTED_TAG_PATTERN.sub("[REDACTED]", text)
        # If no substitution was made, no more matched pairs exist
        if prev_text == text:
            break

    # Clean up any remaining orphaned tags
    text = text.replace("<redacted>", "").replace("</redacted>", "")

    # Layer 2: Automatic pattern detection
    all_patterns = SENSITIVE_PATTERNS + (extra_patterns or [])
    for pattern in all_patterns:
        text = re.sub(pattern, "[REDACTED]", text, flags=re.IGNORECASE)

    return text


def redact_memory_input(
    raw: RawMemoryInput,
    patterns: Optional[list[str]] = None,
) -> RawMemoryInput:
    """Return a deeply copied input with every nested text value redacted."""
    redacted = copy.deepcopy(raw)
    for field_info in fields(redacted):
        value = getattr(redacted, field_info.name)
        if isinstance(value, str):
            setattr(redacted, field_info.name, redact(value, patterns))
        elif isinstance(value, list):
            setattr(
                redacted,
                field_info.name,
                [redact(item, patterns) if isinstance(item, str) else item for item in value],
            )
    return redacted


def load_memoryignore(path: str) -> list[str]:
    """Load custom redaction patterns from a .memoryignore file.

    The file format supports:
    - One regex pattern per line
    - Comments starting with #
    - Empty lines (ignored)

    Args:
        path: Path to the .memoryignore file

    Returns:
        List of regex patterns to use for redaction.
        Returns empty list if file doesn't exist.

    Examples:
        With .memoryignore containing:
            # SSN pattern
            \\d{3}-\\d{2}-\\d{4}

        >>> patterns = load_memoryignore(".memoryignore")
        >>> patterns
        ['\\\\d{3}-\\\\d{2}-\\\\d{4}']
    """
    try:
        with open(path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []

    patterns = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(line)

    return patterns
