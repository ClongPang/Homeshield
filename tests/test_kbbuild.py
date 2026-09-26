"""kbbuild 离线库:导入幂等性、类目映射、导出口径。"""
import json
from pathlib import Path

import pytest

from homeshield.kbbuild.db import connect, init_schema
from homeshield.kbbuild.export import export_eval
from homeshield.kbbuild.importer import import_official
from homeshield.kbbuild.mapping import map_scam_type

POLICE_SUB = "public security, prosecution, judiciary, and government agencies"


def _base_items() -> list[dict]:
    return [
        {"id": 1, "category": "impersonation", "subcategory": POLICE_SUB,
         "data_type": "message", "raw_data": "seed-1",
         "generated text": "我是公安局办案民警,案件涉密,别告诉家人,立即把资金转入安全账户配合清查,否则逮捕。"},
        {"id": 2, "category": "fraudulent service", "subcategory": "e-commerce logistics and shopping",
         "data_type": "message", "raw_data": "seed-2",
         "generated text": "您的包裹在运输中丢失,我们提供双倍赔偿,请点击链接办理退款理赔并填写银行卡信息。"},
        {"id": 3, "category": "fake job posting", "subcategory": "fake job posting",
         "data_type": "job posting", "raw_data": "seed-3",
         "generated text": "日结兼职点赞员,做任务赚佣金,入职先垫付198元开通会员权限,完成后佣金本金一起返。"},
        {"id": 4, "category": "phishing", "subcategory": "fraud email",
         "data_type": "email", "raw_data": "seed-4",
         "generated text": "恭喜您被抽中为幸运用户获得现金奖励,请点击链接领取奖品并填写个人身份信息用于核验发放。"},
        {"id": 5, "category": "network friendship", "subcategory": "network friendship",
         "data_type": "message", "raw_data": "seed-5", "generated text": "交个朋友"},
        {"id": 6, "category": "network friendship", "subcategory": "network friendship",
         "data_type": "message", "raw_data": "seed-6",
         "generated text": "网上认识的男朋友带我赚钱,说有内部渠道老师带单,小额能提现,投得越多返得越多,别告诉家人。"},
        {"id": 7, "category": "phishing", "subcategory": "commercial spam",
         "data_type": "email", "raw_data": "seed-7",
         "generated text": "请及时确认您的订阅信息以继续享受相关的内容更新与账户服务安排,详情参见附件中的说明文档。"},
    ]


def _levelup_items(base: list[dict]) -> list[dict]:
    items = []
    for b in base:
        rounds = [{"round": i, "generated_data": b["generated text"] if i == 1
                   else f"【增强{i - 1}】{b['generated text']}"}
                  for i in (1, 2, 3, 4)]
        items.append({**b, "role_bg": ["求职者"], "multi-rounds fraud": rounds})
    return items


@pytest.fixture()
def raw_dir(tmp_path: Path) -> Path:
    base, levelup = _base_items(), _levelup_items(_base_items())
    d = tmp_path / "raw"
    d.mkdir()
    (d / "FP-base-Chinese.json").write_text(
        json.dumps(base, ensure_ascii=False), encoding="utf-8")
    (d / "FP-levelup-Chinese.json").write_text(
        json.dumps(levelup, ensure_ascii=False), encoding="utf-8")
    return d


def _import(tmp_path: Path, raw_dir: Path):
    db = tmp_path / "kb.db"
    conn = connect(db)
    init_schema(conn)
    stats = import_official(
        conn, raw_dir / "FP-base-Chinese.json",
        raw_dir / "FP-levelup-Chinese.json", lang="zh")
    return conn, stats


def test_mapping_rules():
    assert map_scam_type("impersonation", POLICE_SUB, "x") == "impersonate_police"
    assert map_scam_type("impersonation", "acquaintances", "x") == "impersonate_relative"
    assert map_scam_type("impersonation", "acquaintances", "致张杨主管:采购框架协议") == "impersonate_boss"
    assert map_scam_type("fraudulent service", "investment and financial management", "x") == "fake_investment"
    assert map_scam_type("fraudulent service", "e-commerce logistics and shopping", "包裹丢失理赔") == "fake_refund"
    assert map_scam_type("fraudulent service", "e-commerce logistics and shopping", "低价好物") == "fake_shopping"
    assert map_scam_type("fake job posting", "fake job posting", "垫付做任务佣金") == "task_scam"
    assert map_scam_type("fake job posting", "fake job posting", "高薪文员") == "fake_parttime"
    assert map_scam_type("network friendship", "network friendship", "x") == "romance_pigbutchering"
    # phishing 内容二次分流:有内容特征进新类目,无特征留缺口
    assert map_scam_type("phishing", "fraud email", "恭喜您中奖领取奖品") == "fake_prize"
    assert map_scam_type("phishing", "phishing email", "请确认订阅信息以继续服务") is None


def test_import_levels_and_idempotent(tmp_path, raw_dir):
    conn, stats = _import(tmp_path, raw_dir)
    # base 7 条中 6 条 ≥30 字全部入库;levelup round1 与 base 重复被去重,round2~4 入库
    assert stats["base"] == 7
    assert stats["round1_duplicated"] == 7
    levels = dict(conn.execute(
        "SELECT level, COUNT(*) FROM fr_case WHERE lang='zh' GROUP BY level").fetchall())
    assert levels[0] == 7 and levels[1] == levels[2] == levels[3] == 7
    # 幂等:重复导入不产生新行
    stats2 = import_official(
        conn, raw_dir / "FP-base-Chinese.json",
        raw_dir / "FP-levelup-Chinese.json", lang="zh")
    assert stats2["base"] == 0 and stats2["levelup"] == 0
    total = conn.execute("SELECT COUNT(*) FROM fr_case").fetchone()[0]
    assert total == 28
    # provenance 落库;round1 文本与 base 一致
    row = conn.execute("SELECT provenance, license_note FROM fr_case LIMIT 1").fetchone()
    assert "arXiv:2502.12904" in row["provenance"] and "no redistribution" in row["license_note"]


def test_export_eval_schema_and_gap(tmp_path, raw_dir):
    conn, _ = _import(tmp_path, raw_dir)
    out_base = tmp_path / "base.jsonl"
    out_lvl = tmp_path / "level.jsonl"
    summary = export_eval(conn, out_base, out_lvl, lang="zh", per_class=2, seed=42)

    base_rows = [json.loads(l) for l in out_base.read_text(encoding="utf-8").splitlines()]
    # phishing(映射外)与过短文本(#5)不进导出
    assert summary["exported_base"] == 5
    assert {r["scam_type"] for r in base_rows} == {
        "impersonate_police", "fake_refund", "task_scam", "fake_prize", "romance_pigbutchering"}
    assert all(r["label"] == "scam" and r["source"] == "synthetic:llm:deepseek-r1"
               and "待人工过筛" in r["notes"] for r in base_rows)
    # 退化集与 base 配对:每个案例 4 级,level0 与 base 同文
    lvl_rows = [json.loads(l) for l in out_lvl.read_text(encoding="utf-8").splitlines()]
    assert summary["exported_levelup"] == 20
    by_case: dict[int, list[int]] = {}
    for r in lvl_rows:
        by_case.setdefault(r["case_key"], []).append(r["level"])
    assert all(v == [0, 1, 2, 3] for v in by_case.values())
    base_text = {r["id"]: r["text"] for r in base_rows}
    l0 = {r["case_key"]: r["text"] for r in lvl_rows if r["level"] == 0}
    assert base_text["FR1-0001"] == l0[1]
    # §6.1 加载器可直接消费两份文件(levelup 的扩展字段被忽略)
    from homeshield.eval.dataset import load_dataset
    assert len(load_dataset(out_base)) == 5
    assert len(load_dataset(out_lvl)) == 20


def test_export_deterministic(tmp_path, raw_dir):
    conn, _ = _import(tmp_path, raw_dir)
    export_eval(conn, tmp_path / "a.jsonl", tmp_path / "al.jsonl", per_class=1, seed=7)
    export_eval(conn, tmp_path / "b.jsonl", tmp_path / "bl.jsonl", per_class=1, seed=7)
    assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes()
