"""基于 continuity.analyze 的逐户决策、批次冲突检测与报告生成。"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from continuity import (  # noqa: E402
    DATA, analyze, _parse_day, overlaps, slot_serves, load,
    feasible_slots, latest_consent,
)
from batch import batch_assessment  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"


def _cont_summary(item: dict) -> dict:
    c = item["continuity"]
    return {
        "scope": c.scope, "max_gap": c.max_gap,
        "last_countable_day": c.last_countable_day.isoformat() if c.last_countable_day else None,
        "need_by": c.need_by.isoformat() if c.need_by else None,
        "historical_gaps": c.gaps,
        "historical_breaches": c.breaches,
        "substitute_covered_gaps": c.sub_covered_gaps,
        "substitute_over_sub_days": c.sub_over_limit,
        "unverified_not_counted": c.unverified_days,
        "countable_visits": len(c.countable),
    }


def decision_for(scope: str, item: dict) -> dict:
    plan = item["plan"]
    c = item["continuity"]
    need_by = c.need_by
    paths = []
    for prov, info in item["candidates"].items():
        ev = info["evidence"]
        paths.append({
            "provider": prov,
            "consent": info["consent_latest"],
            "earliest_regular_slot": info["earliest_regular"],
            "meets_need_by": bool(info["timely_slots"]),
            "comparable_trial_n": ev["n"],
            "trial_gap_max": ev["gap_max"],
            "noncomparable_or_unverified": ev["noncomparable"] + ev["unverified"],
            "bridge_slots": info["bridge_slots"],
            "regular_slots": info["regular_slots"],
            "timely_slots": info["timely_slots"],
            "slots_voided_by_missed": info.get("slots_voided_by_missed", []),
        })
    backstop = item["extension_backstop"]
    missed = item.get("missed_events", [])
    # 最早实际缺口：need_by 当日仍未完成，则次日起出现实际缺口
    gap_day = (need_by + timedelta(days=1)).isoformat() if need_by else None
    # 旧岗延长必须真正覆盖需要日：from <= need_by <= to（仅额度存在不够）
    backstop_covers = bool(
        backstop and need_by
        and _parse_day(backstop["from"]) <= need_by <= _parse_day(backstop["to"]))
    served = item["served_in_handover"]
    grouped = bool(plan["group"])
    return {
        "scope": scope, "household": plan["household"], "care": plan["care"],
        "group": plan["group"] or None, "condition": plan["condition"],
        "continuity": _cont_summary(item),
        "earliest_gap_day": gap_day,
        "candidate_paths": paths,
        "extension_backstop": backstop,
        "backstop_covers_need_by": backstop_covers,
        "handover_proofs": item["handover_kinds"],
        "missed_events": missed,
        "valid_served_recorded": served,
        "grouped": grouped,
    }


def find_slot_conflicts(tables: dict) -> list[dict]:
    """共享班组同日重叠窗口：不可同时分配给两个项目。"""
    slots = tables["candidate_slots"]
    conflicts = []
    for i, a in enumerate(slots):
        for b in slots[i + 1:]:
            if overlaps(a, b):
                conflicts.append({
                    "team": a["team"], "day": a["day"],
                    "slots": [a["slot"], b["slot"]],
                    "cares": [a["care"], b["care"]],
                    "served_scopes": [a["scope"], b["scope"]],
                })
    return conflicts


def build(as_of: date) -> dict:
    result = analyze(as_of)
    decisions = [decision_for(s, item) for s, item in result["per_scope"].items()]
    conflicts = find_slot_conflicts(result["tables"])
    led = result["ledger"]
    ledger_view = {oid: {"offer": led.offers[oid], "active_reserves": rs}
                   for oid, rs in sorted(led.reserves.items())}
    return {"as_of": as_of.isoformat(), "decisions": decisions,
            "slot_conflicts": conflicts, "extension_ledger": ledger_view,
            "trial_groups": result["trial_groups"],
            "batch": batch_assessment(as_of)}


def verdict(d: dict) -> str:
    """给出每户每项的处置结论。"""
    paths_ok = [p for p in d["candidate_paths"] if p["meets_need_by"]]
    bridge = next((p for p in d["candidate_paths"] if p["bridge_slots"]), None)
    # 先判断本项目是否存在可行承接路径；成组约束只在有路径/已履约时限制释放
    if d["valid_served_recorded"] and not d["grouped"]:
        return ("新方已有效履约且非成组：具备释放旧保障条件，"
                "待主管按户按项核准日期与接收范围后释放，并继续连续性复盘")
    if d["valid_served_recorded"] and d["grouped"]:
        return ("本项已有有效签收，但同组试接未整组完成：旧保障暂不释放，"
                "待同组其余项目在试接期内取得有效签收、整组正式交接确认")
    if paths_ok:
        p = paths_ok[0]
        tail = ("（无本户直接试办样本，履约能力仅靠同类组合外推，"
                "证据范围限于同 care/同条件记录）") if p["comparable_trial_n"] == 0 else ""
        group_note = "；成组项目须整组确认，单项签收不得释放同组其余服务" if d["grouped"] else ""
        return f'可在需要日前由 {p["provider"]} 常规承接{tail}{group_note}'
    # 无准时常规路径
    if d["backstop_covers_need_by"]:
        base = "候选窗口赶不上最长间隔：先用已保留旧岗窗口兜底"
        if bridge:
            base += f"，并可由 {bridge['provider']} 的限时桥接（{','.join(bridge['bridge_slots'])}）补位"
        return base + "；届时仍需联系家庭确认改服务安排"
    if d["extension_backstop"]:
        return ("旧岗延长窗口覆盖不到需要日：预计出现实际服务缺口，"
                "只能用已保留未释放的旧岗窗口尽量重排，须立即联系家庭改服务")
    if bridge:
        return (f"无准时常规承接且无旧岗兜底：仅 {bridge['provider']} 限时桥接"
                f"（{','.join(bridge['bridge_slots'])}，可能只覆盖本项）可短期补位，"
                "须立即联系家庭确定正式承接安排")
    return "无可用延长且候选不达标：预计实际中断，须立即联系家庭并重置排期"


def render_markdown(report: dict) -> str:
    lines = [f"# 承接变更连续性与切换核准（证据截止 {report['as_of']}）", ""]
    lines.append("## 一、逐户逐项连续性与处置")
    for d in report["decisions"]:
        cc = d["continuity"]
        lines += [
            "", f"### {d['scope']}｜家庭 {d['household']}｜{d['care']}"
            + (f"｜成组 {d['group']}" if d["group"] else "")
            + f"｜条件：{d['condition']}",
            f"- 最长间隔 {cc['max_gap']} 天；最近可认可服务 {cc['last_countable_day']}；"
            f"须在 **{cc['need_by']}** 前再次完成，否则次日（{d['earliest_gap_day']}）起出现实际缺口。",
            f"- 历史间隔 {cc['historical_gaps']}；超限 {cc['historical_breaches'] or '无'}；"
            f"替代覆盖间隔 {cc['substitute_covered_gaps'] or '无'}"
            f"（超 sub_days 的 {cc['substitute_over_sub_days'] or '无'}）；"
            f"未计入口径的不可核验记录 {cc['unverified_not_counted'] or '无'}。",
        ]
        for p in d["candidate_paths"]:
            lines.append(
                f"- 候选 {p['provider']}：同意 {p['consent']['status'] if p['consent'] else '无'}"
                f"（记录日 {p['consent']['recorded'] if p['consent'] else '—'}）；"
                f"最早常规窗口 {p['earliest_regular_slot'] or '无'}，"
                f"满足需要日：{'是' if p['meets_need_by'] else '否'}；"
                f"可比试办样本 n={p['comparable_trial_n']}，试办最大间隔 {p['trial_gap_max']}；"
                f"桥接窗口 {p['bridge_slots'] or '无'}；"
                f"因 missed 作废的窗口 {p['slots_voided_by_missed'] or '无'}；"
                f"剔除的不可比/不可核验记录 {p['noncomparable_or_unverified'] or '无'}。")
        bs = d["extension_backstop"]
        lines.append(
            "- 旧岗兜底：" + (f"{bs['offer']} 窗口 {bs['from']}..{bs['to']}（确认截止 {bs['confirm_until']}），"
                            f"覆盖需要日：{'是' if d['backstop_covers_need_by'] else '否'}"
                            if bs else "无已实际保留且未释放的延长窗口"))
        missed_txt = "、".join(f"{m['event']}（{m['provider']} {m['effective']}）"
                               for m in d["missed_events"]) or "无"
        lines.append(f"- 交接凭证：{d['handover_proofs'] or '无'}；"
                     f"未接住记录 {missed_txt}；"
                     f"新方有效履约签收：{'有' if d['valid_served_recorded'] else '无'}")
        lines.append(f"- **结论：{verdict(d)}**")
    lines += ["", "## 二、共享班组窗口冲突（不能重复排班）"]
    for cf in report["slot_conflicts"]:
        lines.append(f"- {cf['day']} 班组 {cf['team']}：{cf['slots'][0]}({cf['cares'][0]}) 与 "
                     f"{cf['slots'][1]}({cf['cares'][1]}) 时段重叠，适用范围 {cf['served_scopes']}，只能二选一。")
    lines += ["", "## 三、延长台账（截止日后只认已保留未释放额度）"]
    for oid, v in report["extension_ledger"].items():
        o = v["offer"]
        lines.append(f"- {oid}（{o['provider']} {o['scope']}）：上限 {o['max_uses']}，"
                     f"确认截止 {o['confirm_until']}，当前有效保留 {v['active_reserves']}")
    lines += ["", "## 四、成组试接确认"]
    for g in report["trial_groups"]:
        lines.append(f"- {g['group']}（{g['window']}）：{g['scopes']}，有效签收 {g['valid_served']}；{g['note']}")
    b = report["batch"]
    served_map = {d["scope"]: d["valid_served_recorded"] for d in report["decisions"]}
    ready = b["categories"]["switch_ready"]
    lines += ["", "## 五、批次可承诺性（逐户可行 ≠ 整批可切）"]
    lines.append(f"- 已有效签收、主管核准后即可释放旧保障："
                 f"{[s for s in ready if served_map[s]] or '无'}")
    lines.append(f"- 可安排切换但须先取得有效签收（旧保障暂留）："
                 f"{[s for s in ready if not served_map[s]] or '无'}")
    lines.append(f"- 有路径但暂留（成组未整组确认）：{b['categories']['conditional_hold'] or '无'}")
    lines.append(f"- 先续用旧保障兜底：{b['categories']['backstop_first'] or '无'}")
    lines.append(f"- 预计中断、须联系家庭改服务：{b['categories']['stranded_contact_family'] or '无'}")
    lines.append("")
    lines.append("### 实施计划中的班组冲突")
    for cf in b["team_conflicts_in_live_plan"]:
        lines.append(f"- {cf['day']} 班组 {cf['team']}：{cf['slots']} 涉及 {cf['scopes']} —— {cf['type']}")
    lines.append("")
    lines.append("### 成组整批判断")
    for g in b["group_assessment"]:
        lines.append(f"- {g['group']} {g['scopes']}：各户均有路径={g['each_scope_has_viable_path']}，"
                     f"整组有效签收={g['all_scopes_valid_served']}，允许整组切换={g['batch_switch_allowed']}（{g['reason']}）")
    lines.append("")
    lines.append("### 延长额度按需要日先后的使用建议")
    for a in b["extension_advice"]:
        lines.append(f"- {a['offer']}（{a['provider']}，上限 {a['max_uses']}，确认截止 {a['confirm_until']}）："
                     f"需要日顺序 {a['need_by_order']}；当前保留 {a['active_reserves']}；{a['capacity_note']}")
    lines += ["", "## 六、主管核准建议（作用到每户每项的日期与接收范围）"]
    for d in report["decisions"]:
        cc = d["continuity"]
        path_txt = "；".join(
            f"{p['provider']}（窗口 {p['regular_slots'] or '无'}，协议 "
            f"{(p['consent']['from'] + '..' + p['consent']['to']) if p['consent'] else '无'}，"
            f"可比样本 n={p['comparable_trial_n']}，桥接 {p['bridge_slots'] or '无'}）"
            for p in d["candidate_paths"])
        limit = ("证据仅限同 care/同条件的可比试办记录，本户无直接样本，"
                 "不能外推到其他条件或组合") if all(
            p["comparable_trial_n"] == 0 for p in d["candidate_paths"]) else "有同类可比试办样本支撑"
        lines.append(
            f"- {d['scope']}（{d['household']}/{d['care']}"
            + (f"/{d['group']}" if d["group"] else "")
            + f"）：需要日 {cc['need_by']}，缺口日 {d['earliest_gap_day']}；路径：{path_txt}；"
            f"证据边界：{limit}；处置：{verdict(d)}")
    lines += [
        "",
        "## 七、口径与复用说明",
        "- 已实际服务的日子（含旧队 served 与新方 served）全部保留在连续性序列中用于复盘；",
        "  但只有新承接方的有效签收才能作为释放旧保障的依据，且成组服务须整组签收。",
        "- unverified 记录缺少到屋凭证，一律不计可认可履约；substitute 仅按 plans 指定认可，",
        "  且相邻间隔超过 sub_days 的替代标记为超限。",
        "- 延长报价过确认截止日后，只承认已 reserve 且未 release 的额度；台账外的重排诉求无法支持。",
        "- 更换数据后重跑 `python3 -I report.py` 即可按新照护节奏与团队资料重新输出风险家庭、",
        "  核准进度与无法按期覆盖的实际原因；测试：`python3 -I -m unittest discover -s tests`。",
    ]
    return "\n".join(lines) + "\n"


def main():
    as_of = date(2026, 10, 4)  # 交换资料中最新事实日
    report = build(as_of)
    OUT.mkdir(exist_ok=True)
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                                default=str), encoding="utf-8")
    (OUT / "report.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"as_of": as_of.isoformat(),
                      "scopes": len(report["decisions"]),
                      "conflicts": len(report["slot_conflicts"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
