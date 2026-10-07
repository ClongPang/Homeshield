import json

import pytest

from homeshield.eval.dataset import Sample
from homeshield.eval.retrieval import validate_relevance_annotations


def _sample():
    return Sample(id="S1", text="样本", label="scam", scam_type="impersonate_police", source="test")


def test_unreviewed_annotation_must_use_null(tmp_path):
    path = tmp_path / "labels.jsonl"
    path.write_text(json.dumps({"sample_file": "samples.jsonl", "sample_id": "S1",
                               "group_id": "g1", "relevant_case_ids": [], "reviewed": False}), encoding="utf-8")
    with pytest.raises(ValueError, match="null"):
        validate_relevance_annotations(path, {"samples.jsonl": [_sample()]}, {"C1"})


def test_reviewed_annotation_requires_rationale_and_known_unique_case_ids(tmp_path):
    path = tmp_path / "labels.jsonl"
    row = {"sample_file": "samples.jsonl", "sample_id": "S1", "group_id": "g1",
           "relevant_case_ids": ["C1", "C1"], "reviewed": True,
           "reviewer": "reviewer", "rationale": "reviewed"}
    path.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate or unknown"):
        validate_relevance_annotations(path, {"samples.jsonl": [_sample()]}, {"C1"})


def test_reviewed_annotation_accepts_pending_manual_pair(tmp_path):
    path = tmp_path / "labels.jsonl"
    row = {"sample_file": "samples.jsonl", "sample_id": "S1", "group_id": "g1",
           "relevant_case_ids": ["C1"], "reviewed": True,
           "reviewer": "reviewer", "rationale": "mechanism fits"}
    path.write_text(json.dumps(row), encoding="utf-8")
    rows = validate_relevance_annotations(path, {"samples.jsonl": [_sample()]}, {"C1"})
    assert rows[0]["reviewed"] is True
