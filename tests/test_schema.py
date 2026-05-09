"""Validate that the playbook + opponent schemas are well-formed and that
every JSON file we ship under playbook/ matches its schema."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

PLAYBOOK_DIR = Path(__file__).resolve().parent.parent / "playbook"


def _load(p: Path) -> dict:
    with p.open(encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def playbook_schema() -> dict:
    return _load(PLAYBOOK_DIR / "schema.json")


@pytest.fixture(scope="module")
def opponent_schema() -> dict:
    return _load(PLAYBOOK_DIR / "opponent_schema.json")


def test_schemas_are_valid_json_schema(playbook_schema, opponent_schema):
    Draft202012Validator.check_schema(playbook_schema)
    Draft202012Validator.check_schema(opponent_schema)


def test_tvz_playbook_validates(playbook_schema):
    pb = _load(PLAYBOOK_DIR / "tvz.json")
    Draft202012Validator(playbook_schema).validate(pb)


def test_opponents_example_validates(opponent_schema):
    example = _load(PLAYBOOK_DIR / "opponents.example.json")
    Draft202012Validator(opponent_schema).validate(example)
