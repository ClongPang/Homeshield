"""指标定义与阈值扫描。

fpr:FP = benign/edge 且判为 dangerous。
fpr_strict 为用户感知口径,suspicious 亦计入。
"""

def confusion3x3(y_true: list[str], y_pred: list[str]) -> dict:
    matrix: dict[str, dict[str, int]] = {
        t: {p: 0 for p in ("dangerous", "suspicious", "safe")}
        for t in ("scam", "edge", "benign")
    }
    for t, p in zip(y_true, y_pred):
        matrix[t][p] += 1
    return matrix


def recall(y_true: list[str], y_pred: list[str]) -> float:
    """TP = 标注 scam 且判为 dangerous+suspicious(dangerous 单列见混淆矩阵)。"""
    tp = sum(1 for t, p in zip(y_true, y_pred) if t == "scam" and p in ("dangerous", "suspicious"))
    fn = sum(1 for t, p in zip(y_true, y_pred) if t == "scam" and p == "safe")
    return tp / (tp + fn) if tp + fn else 0.0


def fpr(y_true: list[str], y_pred: list[str]) -> float:
    fp = sum(1 for t, p in zip(y_true, y_pred) if t in ("edge", "benign") and p == "dangerous")
    tn = sum(1 for t, p in zip(y_true, y_pred) if t in ("edge", "benign") and p != "dangerous")
    return fp / (fp + tn) if fp + tn else 0.0


def fpr_strict(y_true: list[str], y_pred: list[str]) -> float:
    fp = sum(1 for t, p in zip(y_true, y_pred) if t in ("edge", "benign") and p != "safe")
    tn = sum(1 for t, p in zip(y_true, y_pred) if t in ("edge", "benign") and p == "safe")
    return fp / (fp + tn) if fp + tn else 0.0


def latency_percentiles(latencies_ms: list[int]) -> dict:
    if not latencies_ms:
        return {"p50": 0, "p95": 0}
    s = sorted(latencies_ms)

    def pct(p: int) -> int:
        return s[max(0, round(p / 100 * (len(s) - 1)))]

    return {"p50": pct(50), "p95": pct(95)}


def threshold_sweep(y_true: list[str], scores: list[int]) -> list[dict]:
    """判定置信分 → P/R 曲线点。score=0 视为判非 scam。"""
    points = []
    positives = sum(1 for t in y_true if t == "scam")
    for th in sorted(set(scores), reverse=True):
        pred = ["scam" if s >= th > 0 else "non" for s in scores]
        tp = sum(1 for t, p in zip(y_true, pred) if t == "scam" and p == "scam")
        fp = sum(1 for t, p in zip(y_true, pred) if t != "scam" and p == "scam")
        points.append(
            {
                "threshold": th,
                "precision": round(tp / (tp + fp), 3) if tp + fp else 1.0,
                "recall": round(tp / positives, 3) if positives else 0.0,
            }
        )
    return points
