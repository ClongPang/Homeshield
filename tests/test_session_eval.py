import asyncio
import json
import pytest

from homeshield.core.config import Settings
from homeshield.eval.session import evaluate_pairs, run_arm
from homeshield.kbbuild.db import connect
from homeshield.kbbuild.export import export_cross_message


def test_cross_message_export_counts(tmp_path):
    # 使用现有离线库时验证分层下限;空库由导出器明确报错。
    from pathlib import Path
    source = Path("data/kb_build.db")
    if not source.exists():
        pytest.skip("local restricted Fraud-R1 offline database unavailable")
    out = tmp_path / "arcs.jsonl"
    counts = export_cross_message(connect(source), out)
    assert counts == {
        "trust_same_incident": 52, "trust_linked": 12, "trust_unlinked": 12,
        "fear": 12, "overwindow": 8, "mismerge_benign": 30, "mismerge_risky": 30,
    }
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 156
    assert all(row["messages"][0]["delay_seconds"] == 0 for row in rows)


def test_session_pair_gate_detects_mismerge(tmp_path):
    arcs = [
        {"arc_id": "fear-1", "stratum": "fear", "messages": [
            {"text": "案件保密", "delay_seconds": 0},
            {"text": "请转账", "delay_seconds": 300}],
         "final_label": "scam", "source": "author_draft:unreviewed"},
        {"arc_id": "benign-1", "stratum": "mismerge_risky", "messages": [
            {"text": "案件保密", "delay_seconds": 0},
            {"text": "给孩子转账生活费", "delay_seconds": 300}],
         "final_label": "benign", "source": "author_draft:unreviewed"},
    ]
    settings = Settings(mode="mock", db_path=":memory:")

    async def paired():
        return await run_arm(arcs, settings, False), await run_arm(arcs, settings, True)

    off, on = asyncio.run(paired())
    metrics, _ = evaluate_pairs(arcs, off, on, "mock")
    assert not metrics["default_enable"]
    assert metrics["gates"]["monotonic"]
    assert metrics["gates"]["judge_calls"]
    assert on[0]["level"] == "dangerous"
