"""指标公式对拍与阈值扫描。"""
from homeshield.eval.metrics import confusion3x3, fpr, user_visible_false_positive_rate, latency_percentiles, recall, threshold_sweep


def test_confusion_recall_fpr():
    y = ["scam", "scam", "edge", "benign"]
    p = ["dangerous", "safe", "suspicious", "safe"]
    assert recall(y, p) == 0.5
    assert fpr(y, p) == 0.0  # edge→suspicious 不算规格口径 FP
    assert user_visible_false_positive_rate(y, p) == 0.5  # 用户感知口径
    m = confusion3x3(y, p)
    assert m["scam"]["dangerous"] == 1 and m["scam"]["safe"] == 1
    assert m["edge"]["suspicious"] == 1 and m["benign"]["safe"] == 1


def test_latency_percentiles():
    assert latency_percentiles([]) == {"p50": 0, "p95": 0}
    assert latency_percentiles([10, 20, 100]) == {"p50": 20, "p95": 100}


def test_threshold_sweep_recall_monotonic():
    y = ["scam", "scam", "benign"]
    scores = [80, 60, 60]
    pts = threshold_sweep(y, scores)
    recalls = [p["recall"] for p in pts]
    assert recalls == sorted(recalls, reverse=True) or recalls == sorted(recalls)


def test_report_labels_mock_as_plumbing_only():
    """报告标注数据构成,并声明 mock/合成口径仅验证管道。"""
    from homeshield.eval.report import render_report

    text = render_report(
        {},
        mode="mock",
        dataset="data/samples/samples.jsonl",
        n=1,
        data_profile={"synthetic": 1},
    )
    assert "synthetic:1" in text
    assert "验证管道正确性" in text and "不是产品质量" in text
