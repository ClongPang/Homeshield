"""评测数据集:JSONL,每行一条。"""
import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel


class Label(StrEnum):
    SCAM = "scam"
    EDGE = "edge"
    BENIGN = "benign"


class Sample(BaseModel):
    id: str
    text: str = ""  # 会话体样本(turns 非空)可为空
    label: Label
    scam_type: str | None = None  # 诈骗类才有,对齐分类学
    source: str  # adapted:<出处> | synthetic:llm:<model> | correction:<版本>
    notes: str = ""
    turns: list[str] | None = None  # 重构四:会话体样本(轮次文本);None=单条


def load_dataset(path: str | Path) -> list[Sample]:
    samples: list[Sample] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            samples.append(Sample(**json.loads(line)))
    return samples
