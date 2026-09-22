"""Shared helpers: fixtures on disk, a fake HTTP session, and a shape check
for programs so every importer's output is held to the same standard."""

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(*parts):
    return json.loads((FIXTURES.joinpath(*parts)).read_text())


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Stands in for requests.Session: answers by URL path, records calls."""

    def __init__(self, routes):
        self.routes = routes  # path fragment -> payload or callable(params)
        self.calls = []

    def get(self, url, params=None, timeout=None, **_kw):
        self.calls.append((url, dict(params or {})))
        for fragment, payload in self.routes.items():
            if fragment in url:
                if callable(payload):
                    payload = payload(params or {})
                return FakeResponse(payload)
        return FakeResponse({"error": "no route"}, status=404)


def assert_program_shape(program):
    """What every importer must produce: the fields the validator needs,
    unique step ids, chained tracks, and a resource constraint for every
    task a step uses."""
    assert program["programId"] and program["name"]
    assert isinstance(program["tracks"], list) and program["tracks"]
    step_ids = []
    tasks = set()
    for track in program["tracks"]:
        assert track["trackId"] and track["name"]
        assert track["steps"], f"track {track['trackId']} has no steps"
        for step in track["steps"]:
            assert step["stepId"] and step["name"]
            step_ids.append(step["stepId"])
            duration = step["duration"]
            assert duration["type"] in ("fixed", "variable", "indefinite")
            if duration["type"] == "fixed":
                assert duration["seconds"] > 0
            assert step["startTrigger"]["type"] in (
                "programStart", "programStartOffset", "afterStep", "afterStepWithBuffer", "manual",
            )
            if step.get("task"):
                tasks.add(step["task"])
    assert len(step_ids) == len(set(step_ids)), "duplicate step ids"
    declared = {rc["task"] for rc in program.get("resourceConstraints", [])}
    assert tasks <= declared, f"tasks without a resource constraint: {tasks - declared}"
    referenced = {s["startTrigger"].get("stepId") for t in program["tracks"] for s in t["steps"]} - {None}
    assert referenced <= set(step_ids), f"dangling afterStep: {referenced - set(step_ids)}"
