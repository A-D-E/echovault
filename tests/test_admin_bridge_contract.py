import copy
import json
from pathlib import Path

import pytest

from memory.cli import AdminMutationValidationError, parse_admin_request


GOLDEN_PATH = Path(__file__).parent / "fixtures" / "dashboard-mutations-v1.json"


def _cases() -> list[dict[str, object]]:
    payload = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert isinstance(payload, list)
    return payload


def _create_request() -> dict[str, object]:
    case = next(item for item in _cases() if item["name"] == "create")
    request = case["request"]
    assert isinstance(request, dict)
    return copy.deepcopy(request)


def test_admin_request_parser_accepts_shared_golden_corpus() -> None:
    parsed_actions = [parse_admin_request(case["request"]).action for case in _cases()]
    assert parsed_actions == ["create", "update", "archive", "restore", "merge", "delete"]


def test_admin_request_parser_rejects_unknown_key() -> None:
    request = _create_request()
    request["unexpected"] = True
    with pytest.raises(AdminMutationValidationError, match="unknown field"):
        parse_admin_request(request)


def test_admin_request_parser_rejects_caller_supplied_create_id() -> None:
    request = _create_request()
    request["memory_id"] = "33333333-3333-4333-8333-333333333333"
    with pytest.raises(AdminMutationValidationError, match="memory_id"):
        parse_admin_request(request)


def test_admin_request_parser_rejects_shell_fields() -> None:
    request = _create_request()
    request["command"] = "sh -c ignored"
    with pytest.raises(AdminMutationValidationError, match="command"):
        parse_admin_request(request)


def test_admin_request_parser_requires_actor() -> None:
    request = _create_request()
    del request["actor"]
    with pytest.raises(AdminMutationValidationError, match="actor"):
        parse_admin_request(request)
