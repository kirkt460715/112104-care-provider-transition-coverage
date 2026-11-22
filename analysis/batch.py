"""切换批次可承诺性：逐户可行不等于整批可行。

约束：
1. 共享班组（team）同日重叠 slot 只能分配一次；
2. 成组（group）服务必须整组具备可行路径且试接期内整组有效签收后才能确认；
3. 只能使用在需要日（need_by）之前、当日资质+同意齐备、未被 missed 作废的窗口；
4. 旧岗延长额度按 shared_group 共享、受 max_uses 与确认截止日约束。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from continuity import analyze, _parse_day, overlaps  # noqa: E402


def _live_timely_slots(result: dict, scope: str, provider: str) -> list[dict]:
    item = result["per_scope"][scope]
    info = item["candidates"][provider]
    need_by = item["continuity"].need_by
    slots = result["tables"]["candidate_slots"]
    out = []
    for s in slots:
        if s["slot"] not in info["timely_slots"]:
            continue
        if s["slot"] in info.get("slots_voided_by_missed", []):
            continue
        out.append(s)
    return out


def batch_assessment(as_of: date) -> dict:
    result = analyze(as_of)
    tables = result["tables"]
    plans_by_scope = {p["scope"]: p for p in tables["plans"]}

    # 1) 每个项目的准时可行承接路径（新方窗口）
    viable = {}
    for scope, item in result["per_scope"].items():
        paths = []
        for prov, info in item["candidates"].items():
            if info["timely_slots"]:
                paths.append({"provider": prov,
                              "slots": _live_timely_slots(result, scope, prov)})
        viable[scope] = paths

    # 2) 共享班组冲突：把所有“准时且可用”的 slot 两两检查重叠
    all_live = [(scope, s) for scope, paths in viable.items()
                for p in paths for s in p["slots"]]
    seen_pairs = set()
    hard_conflicts = []
    for i, (sa, a) in enumerate(all_live):
        for sb, b in all_live[i + 1:]:
            if sa == sb:
                continue
            key = tuple(sorted((a["slot"], b["slot"]))) + (a["team"], a["day"])
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            same_slot = a["slot"] == b["slot"]
            if same_slot or overlaps(a, b):
                hard_conflicts.append({
                    "team": a["team"], "day": a["day"],
                    "slots": [a["slot"], b["slot"]], "scopes": [sa, sb],
                    "type": "同一窗口被两户共用，只能排给一户" if same_slot
                            else "时段重叠，班组人手不可重复使用",
                })

    # 3) 成组约束：组内每个项目都要有可行路径，且试接期内整组有效签收
    groups = {}
    for p in tables["plans"]:
        if p["group"]:
            groups.setdefault(p["group"], []).append(p["scope"])
    group_assessment = []
    served_new = {h["scope"] for h in tables["handover_proofs"]
                  if h["kind"] == "served" and h["provider"] not in {"A", "B"}}
    for g, scopes in sorted(groups.items()):
        each_viable = all(viable[s] for s in scopes)
        served_all = all(s in served_new for s in scopes)
        group_assessment.append({
            "group": g, "scopes": scopes,
            "each_scope_has_viable_path": each_viable,
            "all_scopes_valid_served": served_all,
            "batch_switch_allowed": each_viable and served_all,
            "reason": ("整组路径齐备且整组有效签收，可整组交接"
                       if each_viable and served_all else
                       "单项可行/单项签收不构成整组承诺，旧保障不得释放"),
        })

    # 4) 逐户逐项最终批次分类
    group_lookup = {g["group"]: g for g in group_assessment}
    categories = {"switch_ready": [], "conditional_hold": [],
                  "backstop_first": [], "stranded_contact_family": []}
    for scope, item in result["per_scope"].items():
        plan = item["plan"]
        has_path = bool(viable[scope])
        backstop_covers = (item["extension_backstop"] and item["continuity"].need_by
                           and _parse_day(item["extension_backstop"]["from"])
                           <= item["continuity"].need_by
                           <= _parse_day(item["extension_backstop"]["to"]))
        served = scope in served_new
        grouped = bool(plan["group"])
        group_ok = group_lookup[plan["group"]]["batch_switch_allowed"] if grouped else False
        if (served and not grouped) or (has_path and (not grouped or group_ok)):
            categories["switch_ready"].append(scope)
        elif has_path:
            categories["conditional_hold"].append(scope)  # 路径在但成组未完成
        elif backstop_covers:
            categories["backstop_first"].append(scope)
        else:
            categories["stranded_contact_family"].append(scope)

    # 5) 延长额度使用建议：按最早 need_by 排序分配共享额度
    extension_advice = []
    led = result["ledger"]
    for oid, offer in led.offers.items():
        eligible = offer["scope"].split("|")
        ranked = sorted(
            eligible,
            key=lambda s: result["per_scope"][s]["continuity"].need_by or date.max)
        extension_advice.append({
            "offer": oid, "provider": offer["provider"],
            "eligible_scopes": eligible,
            "need_by_order": [(s, result["per_scope"][s]["continuity"].need_by.isoformat())
                              for s in ranked],
            "max_uses": int(offer["max_uses"]),
            "active_reserves": led.reserves.get(oid, []),
            "confirm_until": offer["confirm_until"],
            "capacity_note": ("确认截止日后不得新增保留；释放的额度只能在截止日前重新指配"
                              if as_of > _parse_day(offer["confirm_until"])
                              else "仍在确认期内，可按 need_by 先后调整保留对象"),
        })

    return {"as_of": as_of.isoformat(), "viable_paths":
            {s: [{"provider": p["provider"], "slots": [x["slot"] for x in p["slots"]]}
                for p in ps] for s, ps in viable.items()},
            "team_conflicts_in_live_plan": hard_conflicts,
            "group_assessment": group_assessment,
            "categories": categories,
            "extension_advice": extension_advice}


if __name__ == "__main__":
    import json
    print(json.dumps(batch_assessment(date(2026, 10, 4)), ensure_ascii=False,
                     indent=2, default=str))
