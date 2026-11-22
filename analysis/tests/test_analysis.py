"""分析口径的单元测试，只用标准库；以 python -I 运行。"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from continuity import (  # noqa: E402
    load, analyze, _parse_day, cert_covers, latest_consent,
    visit_is_countable, build_extension_ledger, overlaps,
    eligible_regular, trial_evidence,
)
from report import build, verdict  # noqa: E402
from batch import batch_assessment  # noqa: E402

AS_OF = date(2026, 10, 4)


class ContinuityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tables = load()
        cls.report = build(AS_OF)
        cls.result = analyze(AS_OF)
        cls.decisions = {d["scope"]: d for d in cls.report["decisions"]}

    def test_unverified_never_countable(self):
        # 用 visit 编号核对：同一天可能有另一机构的可认可记录，不能只比日期
        for scope, item in self.result["per_scope"].items():
            counted = {r["visit"] for r in item["continuity"].countable}
            unverified_ids = {r["visit"] for r in self.tables["visit_history"]
                              if r["scope"] == scope and r["outcome"] == "unverified"}
            self.assertFalse(counted & unverified_ids)

    def test_need_by_examples(self):
        d = self.decisions
        # X01 最近可认可为交接台 2026-10-04 的 C 实际履约
        self.assertEqual(d["X01"]["continuity"]["last_countable_day"], "2026-10-04")
        self.assertEqual(d["X01"]["continuity"]["need_by"], "2026-10-12")
        # X02 最近可认可含交接台 B 的 2026-09-29 served，need_by = 10-07
        self.assertEqual(d["X02"]["continuity"]["last_countable_day"], "2026-09-29")
        self.assertEqual(d["X02"]["continuity"]["need_by"], "2026-10-07")
        # X06 交接 served 2026-10-03，max_gap 14
        self.assertEqual(d["X06"]["continuity"]["need_by"], "2026-10-17")
        # 最早实际缺口日 = need_by 次日
        self.assertEqual(d["X06"]["earliest_gap_day"], "2026-10-18")

    def test_substitute_rules(self):
        # X01 的 substitute 间隔 14 天 > sub_days=7，应被标记
        self.assertIn(14, self.decisions["X01"]["continuity"]["substitute_over_sub_days"])
        # X07 的 substitute 间隔 7 天 == sub_days=7，不超限
        self.assertEqual(self.decisions["X07"]["continuity"]["substitute_over_sub_days"], [])

    def test_cert_end_exclusive(self):
        term = {"care": "bath", "cert_from": "2026-09-01", "cert_to": "2026-12-01",
                "condition": "楼梯协助|单项陪同"}
        self.assertTrue(cert_covers(term, "bath", date(2026, 11, 30), "楼梯协助"))
        self.assertFalse(cert_covers(term, "bath", date(2026, 12, 1), "楼梯协助"))
        self.assertFalse(cert_covers(term, "bath", date(2026, 10, 1), "需要乐龄车"))

    def test_consent_latest_record_wins(self):
        # latest_consent 取全局最新记录（同日两条时后者覆盖前者）
        c = latest_consent(self.tables["family_consents"], "X07", "D",
                           "regular", date(2026, 10, 6))
        self.assertIsNotNone(c)
        self.assertEqual(c["recorded"], "2026-10-06")
        # 时点有效性由 consent_effective_on 负责（见下例）

    def test_point_in_time_consent_blocks_early_slot(self):
        from continuity import consent_effective_on
        consents = self.tables["family_consents"]
        # T05 在 2026-10-03，当日 X07/D 只有 pending 记录 -> 窗口不可用
        self.assertIsNone(consent_effective_on(consents, "X07", "D",
                                               "regular", date(2026, 10, 3)))
        self.assertIsNotNone(consent_effective_on(consents, "X07", "D",
                                                  "regular", date(2026, 10, 6)))
        d = self.decisions
        # 因此 X07 的 D 没有任何可行常规窗口
        d_path = next(p for p in d["X07"]["candidate_paths"] if p["provider"] == "D")
        self.assertEqual(d_path["regular_slots"], [])
        # E 仅持 bridge 同意：wipe 桥接 T09(10-03)、T12(10-08) 均在同意窗口与资质期内
        e_path = next(p for p in d["X07"]["candidate_paths"] if p["provider"] == "E")
        self.assertEqual(e_path["regular_slots"], [])
        self.assertEqual(e_path["bridge_slots"], ["T09", "T12"])

    def test_trial_comparability(self):
        pt = self.tables["provider_terms"]
        vh = self.tables["visit_history"]
        # C 对 bath/楼梯协助的试办证据跨户聚合：X01 的 Q00/Q04 条件=基础不可比，
        # X07 的 5 条 Q 条件为单项陪同同样不可比；同条件 done 仅 X01 的 3 条
        ev = trial_evidence(vh, pt, "C", "bath", "楼梯协助")
        self.assertEqual(ev["n"], 3)
        self.assertEqual(sorted(ev["noncomparable"]),
                         sorted(["Q00", "Q04", "Q60", "Q61", "Q62", "Q63", "Q64"]))
        self.assertTrue(ev["regular_cert_condition_ok"])
        # D 对 bath 无任何 Q 样本（X07 无证据可外推），但常规资质条件本身覆盖单项陪同
        no_sample = trial_evidence(vh, pt, "D", "bath", "单项陪同")
        self.assertEqual(no_sample["n"], 0)
        self.assertTrue(no_sample["regular_cert_condition_ok"])
        # D 对 escort 没有任何 Q 试办记录（escort 的 Q 属于 C/X04 与 E/X09）：
        # X04、X09 拟由 D 承接完全缺少可比证据，只能守住资质+同意层面的判断
        ev2 = trial_evidence(vh, pt, "D", "escort", "需要乐龄车")
        self.assertEqual(ev2["n"], 0)
        self.assertEqual(ev2["sample_visits"], [])
        # C 在 escort/需要乐龄车：Q31 条件=基础不可比，Q34 unverified，余 3 条可比
        ev3 = trial_evidence(vh, pt, "C", "escort", "需要乐龄车")
        self.assertEqual(ev3["n"], 3)
        self.assertEqual(ev3["noncomparable"], ["Q31"])
        self.assertEqual(ev3["unverified"], ["Q34"])

    def test_slot_team_conflict(self):
        slots = {s["slot"]: s for s in self.tables["candidate_slots"]}
        self.assertTrue(overlaps(slots["T01"], slots["T02"]))   # Q 班组 10-03 同时段
        self.assertFalse(overlaps(slots["T06"], slots["T07"]))  # 不同 team 可并行

    def test_extension_ledger(self):
        led = build_extension_ledger(self.tables, AS_OF)
        # L1 上限 2：X01、X03 保留；X07 未保留
        self.assertEqual(sorted(led.reserves["L1"]), ["X01", "X03"])
        # L3：X06 reserve 后 release，额度已释放但截止日后不能再给 X10
        self.assertEqual(led.reserves["L3"], [])
        # L4：X02、X08 保留，X05 未保留
        self.assertEqual(sorted(led.reserves["L4"]), ["X02", "X08"])

    def test_backstop_availability(self):
        d = self.decisions
        self.assertIsNone(d["X07"]["extension_backstop"])   # L1 已满且无其保留
        self.assertIsNone(d["X04"]["extension_backstop"])   # L2 从未保留
        self.assertIsNone(d["X10"]["extension_backstop"])   # L3 已释放且截止
        self.assertEqual(d["X08"]["extension_backstop"]["offer"], "L4")

    def test_timely_slot_judgement(self):
        d = self.decisions
        # X04 need_by 2026-10-09，D 最早 10-10 -> 不准时
        x04 = d["X04"]["candidate_paths"][0]
        self.assertFalse(x04["meets_need_by"])
        self.assertEqual(x04["earliest_regular_slot"], "2026-10-10")
        # X02 need_by 2026-10-07：H5 missed 生效日 2026-10-04 -> T03 作废，T02(10-03) 准时
        x02 = d["X02"]["candidate_paths"][0]
        self.assertEqual(x02["slots_voided_by_missed"], ["T03"])
        self.assertTrue(x02["meets_need_by"])
        self.assertEqual(x02["regular_slots"], ["T02"])
        self.assertEqual(x02["timely_slots"], ["T02"])
        # X10 need_by 2026-10-07，E 窗口 10-05 准时
        self.assertTrue(any(p["meets_need_by"] for p in d["X10"]["candidate_paths"]))

    def test_group_trial_not_releasable_on_single_served(self):
        groups = {g["group"]: g for g in self.report["trial_groups"]}
        self.assertFalse(groups["P1"]["complete"])  # 仅 X01 有 served，X02 missed
        self.assertFalse(groups["P2"]["complete"])  # 尚无 served

    def test_verdicts_mention_contact_for_stranded(self):
        text = {s: verdict(d) for s, d in self.decisions.items()}
        # 无准时可行路径、必须联系家庭改服务的项目
        for stranded in ("X04", "X05", "X07", "X08", "X09"):
            self.assertIn("联系家庭", text[stranded], stranded)
        # X03、X10 有准时常规窗口；X06 已有效履约
        self.assertIn("具备释放", text["X06"])
        self.assertNotIn("联系家庭", text["X10"])
        # X02 虽有 T03 准时窗口，但 T02 已 missed 且成组 P1 未整组完成：路径可行但不得释放
        self.assertIn("整组确认", text["X02"])


class BatchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.batch = batch_assessment(AS_OF)

    def test_categories_match_evidence(self):
        c = self.batch["categories"]
        # X06 已由 E 有效履约；X03、X10 有准时窗口且非成组
        self.assertEqual(sorted(c["switch_ready"]), ["X03", "X06", "X10"])
        # P1 两户各有窗口但未整组签收
        self.assertEqual(sorted(c["conditional_hold"]), ["X01", "X02"])
        # X08 仅靠 L4 旧岗延长覆盖需要日
        self.assertEqual(c["backstop_first"], ["X08"])
        # 无路径且无兜底：必须联系家庭
        self.assertEqual(sorted(c["stranded_contact_family"]),
                         ["X04", "X05", "X07", "X09"])

    def test_team_conflicts_block_whole_batch_promise(self):
        conflicts = self.batch["team_conflicts_in_live_plan"]
        # Q 班组 T01/T02 同时段分属 X01/X02，不能同时兑现
        q = [x for x in conflicts if x["team"] == "Q"]
        self.assertEqual(len(q), 1)
        self.assertEqual(sorted(q[0]["scopes"]), ["X01", "X02"])
        # T08/T11 是 X06 与 X10 共用的同一窗口
        w_slots = {tuple(x["slots"]) for x in conflicts if x["team"] == "W"}
        self.assertIn(("T08", "T08"), w_slots)
        self.assertIn(("T11", "T11"), w_slots)

    def test_groups_not_switchable(self):
        ga = {g["group"]: g for g in self.batch["group_assessment"]}
        self.assertFalse(ga["P1"]["batch_switch_allowed"])
        self.assertFalse(ga["P2"]["batch_switch_allowed"])
        self.assertFalse(ga["P2"]["each_scope_has_viable_path"])

    def test_extension_advice_need_by_order(self):
        l4 = next(a for a in self.batch["extension_advice"] if a["offer"] == "L4")
        # X05/X08 need_by 10-05 早于 X02 10-07；但 X05 未保留、额度已给 X02/X08
        self.assertEqual([s for s, _ in l4["need_by_order"]], ["X05", "X08", "X02"])
        self.assertEqual(sorted(l4["active_reserves"]), ["X02", "X08"])
        # 截止日后不可新增
        self.assertIn("截止日后", l4["capacity_note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
