"""构建 FGRC-SCD 外部评测集(风险短信+对话 → Sample schema)。

来源:HuggingFace Abooooo/FGRC-SCD(MIT;基于 CCL23-Eval 任务6 电信诈骗案件数据合成,
上游 CCL 许可仅限科研——故产物按 data/README.md 纪律放 derived/,不入版本库)。

用法:
    uv run python scripts/build_fgrc_scd.py             # 用本地 zip,缺则尝试下载
    uv run python scripts/build_fgrc_scd.py --download  # 下载(HF 直连失败自动回落 hf-mirror)
    网络受限时挂代理:HTTPS_PROXY=http://... uv run python scripts/build_fgrc_scd.py --download

产出:
    data/datasets/derived/fgrc_scd/fgrc_scd_sms.jsonl
    data/datasets/derived/fgrc_scd/fgrc_scd_dialogues.jsonl
    终端打印 riskType→scam_type 映射与未映射报告(映射外不导出,同 kbbuild 纪律)。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

RAW_DIR = Path("data/raw/fgrc-scd")
OUT_DIR = Path("data/datasets/derived/fgrc_scd")
FILES = {  # zip 名 → (产物名, 样本 id 前缀)
    "FGRC-SCD-sms.zip": ("fgrc_scd_sms.jsonl", "FGSMS"),
    "FGRC-SCD-dialog.zip": ("fgrc_scd_dialogues.jsonl", "FGDLG"),
}
SOURCES = [  # 下载源,依次回落
    "https://huggingface.co/datasets/Abooooo/FGRC-SCD/resolve/main/{name}",
    "https://hf-mirror.com/datasets/Abooooo/FGRC-SCD/resolve/main/{name}",
]

# FGRC riskType → 项目 16 类分类学;映射外不导出(同 kbbuild 纪律)
RISK_TYPE_MAP: dict[str, str] = {
    "虚假网络投资理财类": "fake_investment",
    "虚假信用服务类": "fake_loan",
    "冒充电商物流客服类": "fake_refund",  # 含快递语境时细化为 express_insurance
    "虚假购物、服务类": "fake_shopping",
    "冒充公检法及政府机关类": "impersonate_police",
    "冒充领导、熟人类": "impersonate_relative",  # 含领导语境时细化为 impersonate_boss
    "网络婚恋、交友类": "romance_pigbutchering",
    "无风险": "",  # benign,无 scam_type
}
UNMAPPED = {"冒充军警购物类诈骗", "网黑案件"}  # 分类学无对应类目,报告后丢弃
EXPRESS_HINT = re.compile(r"快递|包裹|运单|理赔|派送")
BOSS_HINT = re.compile(r"领导|老板|董事长|总经理|王总|李总|张总")
TURN_SPLIT = re.compile(r"【坐席】|【客户】|坐席[:：]|客户[:：]")
MIN_TEXT_CHARS = 10

KEY_ALIASES = {  # 已知列名 → 统一名(HF 卡提示两种 schema 并存:中文列与英文列)
    "text": "text", "文本": "text", "content": "text", "内容": "text",
    "riskType": "risk_type", "风险类别": "risk_type", "风险类型": "risk_type",
    "riskPoint": "risk_point", "风险点": "risk_point", "风险要点": "risk_point",
    "f_index": "case_id", "案件编号": "case_id", "case_id": "case_id", "index": "row_id",
}


def download(name: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 1000:
        print(f"使用本地 {dest}")
        return dest
    last_err: Exception | None = None
    for tpl in SOURCES:
        url = tpl.format(name=name)
        try:
            print(f"下载 {url} …")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as fh:
                while chunk := resp.read(1 << 20):
                    fh.write(chunk)
            return dest
        except Exception as e:  # noqa: BLE001 —— 逐源回落,最后统一报错
            last_err = e
            print(f"  失败:{e}")
    raise SystemExit(f"下载失败({name});请挂代理重试或手动下载放入 {RAW_DIR}/:{last_err}")


def parse_zip(zippath: Path) -> list[dict]:
    """递归解析 zip 内 json/jsonl/csv,按 KEY_ALIASES 归一列名。"""
    rows: list[dict] = []
    with zipfile.ZipFile(zippath) as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            suffix = Path(member).suffix.lower()
            raw = zf.read(member)
            try:
                if suffix == ".jsonl":
                    rows += [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
                elif suffix == ".json":
                    data = json.loads(raw.decode("utf-8"))
                    items = data if isinstance(data, list) else next(
                        (v for v in data.values() if isinstance(v, list)), [data])
                    rows += [r for r in items if isinstance(r, dict)]
                elif suffix == ".csv":
                    rows += list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
            except (json.JSONDecodeError, UnicodeDecodeError, csv.Error) as e:
                print(f"  跳过 {member}:{e}")
    return [_normalize(r) for r in rows]


def _normalize(row: dict) -> dict:
    out: dict = {}
    for key, value in row.items():
        unified = KEY_ALIASES.get(str(key).strip())
        if unified and value not in (None, ""):
            out.setdefault(unified, str(value).strip())
    return out


def to_sample(row: dict, prefix: str, idx: int, stats: dict) -> dict | None:
    text = row.get("text", "")
    risk_type = row.get("risk_type", "")
    if len(text) < MIN_TEXT_CHARS:
        stats["过短跳过"] += 1
        return None
    if risk_type in UNMAPPED:
        stats[f"未映射丢弃:{risk_type}"] += 1
        return None
    if risk_type not in RISK_TYPE_MAP:
        stats[f"未知riskType丢弃:{risk_type}"] += 1
        return None
    scam_type = RISK_TYPE_MAP[risk_type]
    label = "benign" if risk_type == "无风险" else "scam"
    if scam_type == "fake_refund" and EXPRESS_HINT.search(text):
        scam_type = "express_insurance"
    if scam_type == "impersonate_relative" and BOSS_HINT.search(text):
        scam_type = "impersonate_boss"
    if label == "benign" and scam_type:
        stats["异常:无风险却映射出类型"] += 1
        return None
    stats[f"{label}:{scam_type or '-'}"] += 1

    parts = [p.strip() for p in TURN_SPLIT.split(text) if p.strip()]
    notes_bits = ["待人工过筛"]
    if risk_point := row.get("risk_point"):
        notes_bits.append(f"riskPoint={risk_point}")
    if case_id := row.get("case_id"):
        notes_bits.append(f"案件编号={case_id}")
    sample: dict = {
        "id": f"{prefix}-{idx:04d}",
        "text": text,
        "label": label,
        "source": "synthetic:llm:fgrc-scd",
        "notes": ";".join(notes_bits),
    }
    if scam_type and label == "scam":
        sample["scam_type"] = scam_type
    if len(parts) > 1:
        sample["turns"] = parts
    return sample


def build(zippath: Path, out_name: str, prefix: str, out_dir: Path) -> None:
    rows = parse_zip(zippath)
    print(f"{zippath.name}: 解析 {len(rows)} 行")
    stats: Counter = Counter()
    samples, dropped = [], 0
    for row in rows:
        sample = to_sample(row, prefix, len(samples) + 1, stats)
        if sample:
            samples.append(sample)
        else:
            dropped += 1
    outpath = out_dir / out_name
    with outpath.open("w", encoding="utf-8") as fh:
        for sample in samples:
            fh.write(json.dumps(sample, ensure_ascii=False) + "\n")
    print(f"  → {outpath} {len(samples)} 条(丢弃 {dropped})")
    for key, count in sorted(stats.items()):
        print(f"    {key}: {count}")


def main() -> None:
    ap = argparse.ArgumentParser("build_fgrc_scd")
    ap.add_argument("--download", action="store_true", help="本地缺 zip 时允许下载")
    ap.add_argument("--raw-dir", default=str(RAW_DIR))
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    args = ap.parse_args()
    raw_dir, out_dir = Path(args.raw_dir), Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    for name, (out_name, prefix) in FILES.items():
        zippath = raw_dir / name
        if not zippath.exists():
            if not args.download:
                print(f"{zippath} 不存在,尝试下载(加 --download 显式允许)")
            download(name, zippath)
        build(zippath, out_name, prefix, out_dir)


if __name__ == "__main__":
    main()
