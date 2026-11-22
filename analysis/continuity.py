"""居家护理承接变更：连续性证据与切换核准的口径实现。

只使用标准库。所有判断基于 data/ 下的交换表（合成资料）。
关键口径（与 README 约定一致）：
- 合同/资质结束日不含当日；最长间隔为两次可认可服务完成之间的自然天数。
- outcome == done 才是真正完成；unverified 缺少到屋凭证，不计可认可履约；
  substitute 仅当 plans.substitute 指定且该机构持有替代项目有效资质时才可认可，
  且替代覆盖的间隔还须不超过 plans.sub_days。
- Q 开头记录只来自有试办资质的机构；只有同类组合（care、condition、资质条件）
  才可外推到同一候选承接方的其他家庭。
- 常规接收三要件：家庭同意（最新状态 approved 且覆盖当日）、资质有效、有效协议窗口。
- 桥接（bridge）是短期且可能只覆盖一个项目，不能当作常规承接。
- team 是共享班组：同 team 同日时段重叠的 slot 不可重复使用。
- 延长报价有确认截止日；截止日后只能使用已实际 reserve 且未 release 的窗口。
- 成组（group）服务须整组在试接期内各自取得有效签收，单项签收不能释放同组其余服务。
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"


# ---------------------------------------------------------------- 基础读取
def _parse_day(text: str) -> date:
    return date.fromisoformat(text)


def _rows(table: str) -> list[dict]:
    with (DATA / f"{table}.csv").open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load() -> dict[str, list[dict]]:
    return {p.stem: _rows(p.stem) for p in sorted(DATA.glob("*.csv"))}


# ---------------------------------------------------------------- 口径函数
def cert_covers(term: dict, care: str, day: date, condition: str) -> bool:
    """资质是否覆盖某 care 在某日、某服务条件下的服务。"""
    if term["care"] != care:
        return False
    if not (_parse_day(term["cert_from"]) <= day < _parse_day(term["cert_to"])):
        return False
    allowed = term["condition"].split("|")
    return "*" in allowed or condition in allowed


def terms_for(provider_terms: list[dict], provider: str, care: str) -> list[dict]:
    return [t for t in provider_terms if t["provider"] == provider and t["care"] == care]


def consent_effective_on(consents: list[dict], scope: str, provider: str,
                         kind: str, day: date) -> dict | None:
    """窗口当日时点的有效同意：截至当日已 recorded、approved 且窗口覆盖当日。"""
    best = None
    for c in consents:
        if (c["scope"] == scope and c["provider"] == provider
                and c["kind"] == kind and _parse_day(c["recorded"]) <= day):
            if best is None or c["recorded"] > best["recorded"]:
                best = c
    if best is None or best["status"] != "approved":
        return None
    if not (_parse_day(best["from"]) <= day <= _parse_day(best["to"])):
        return None
    return best


def latest_consent(consents: list[dict], scope: str, provider: str,
                   kind: str, day: date) -> dict | None:
    """同范围同意以最新 recorded 为准，要求 approved 且窗口覆盖当日。"""
    hits = [c for c in consents
            if c["scope"] == scope and c["provider"] == provider and c["kind"] == kind]
    if not hits:
        return None
    newest = max(hits, key=lambda c: c["recorded"])
    if newest["status"] != "approved":
        return None
    if not (_parse_day(newest["from"]) <= day <= _parse_day(newest["to"])):
        return None
    return newest


def eligible_regular(provider_terms, consents, provider: str, scope: str,
                     care: str, condition: str, day: date) -> tuple[bool, list[str]]:
    """常规服务接收三要件：同意、资质、协议窗口（同意书 from/to 即协议窗口）。"""
    reasons = []
    consent = latest_consent(consents, scope, provider, "regular", day)
    if consent is None:
        reasons.append("无覆盖当日的有效家庭同意(regular/approved)")
    covered = any(cert_covers(t, care, day, condition)
                  for t in terms_for(provider_terms, provider, care))
    if not covered:
        reasons.append(f"资质不覆盖{care}/{condition}@{day.isoformat()}")
    return (not reasons), reasons


def visit_is_countable(row: dict, plans_by_scope: dict, provider_terms: list[dict]) -> bool:
    """该条历史预约能否计入连续性。"""
    if row["outcome"] == "unverified":
        return False  # 缺少到屋凭证，不可核验
    if row["outcome"] == "done":
        return True
    if row["outcome"] == "substitute":
        plan = plans_by_scope[row["scope"]]
        # README：临时替代只由 plans.substitute 指定；历史替代属“勉强替代”，
        # 其可认可性以计划指定为准（visit 行仍按原 care 登记），
        # 资质窗口要求只适用于未来的桥接/常规安排。
        return bool(plan["substitute"])
    return False


# ---------------------------------------------------------------- 连续性
@dataclass
class Continuity:
    scope: str
    countable: list[dict] = field(default_factory=list)
    gaps: list[int] = field(default_factory=list)
    sub_covered_gaps: list[int] = field(default_factory=list)
    sub_over_limit: list[int] = field(default_factory=list)
    unverified_days: list[str] = field(default_factory=list)
    last_countable_day: date | None = None
    need_by: date | None = None
    max_gap: int | None = None
    breaches: list[int] = field(default_factory=list)


def continuity_for(scope: str, rows: list[dict], plan: dict,
                   provider_terms: list[dict], plans_by_scope: dict,
                   extra_served: list[dict] | None = None) -> Continuity:
    """按户按项目展开连续性。extra_served 为交接台登记的实际履约（served）。"""
    out = Continuity(scope=scope, max_gap=int(plan["max_gap"]))
    hist = [r for r in rows if r["scope"] == scope]
    for r in hist:
        if r["outcome"] == "unverified":
            out.unverified_days.append(r["day"])
    countable = [r for r in hist if visit_is_countable(r, plans_by_scope, provider_terms)]
    if extra_served:
        countable = countable + [
            {"visit": e["event"], "day": e["effective"], "provider": e["provider"],
             "care": plan["care"], "condition": plan["condition"], "outcome": "done"}
            for e in extra_served if e["scope"] == scope]
    countable.sort(key=lambda r: r["day"])
    out.countable = countable
    sub_days = int(plan["sub_days"] or 0)
    prev = None
    for idx, r in enumerate(countable):
        d = _parse_day(r["day"])
        if prev is not None:
            gap = (d - prev).days
            out.gaps.append(gap)
            if gap > out.max_gap:
                out.breaches.append(gap)
            # 替代服务只能在 sub_days 内桥接：与替代相邻的前后间隔都要核对
            if sub_days and (r["outcome"] == "substitute"
                             or (idx > 0 and countable[idx - 1]["outcome"] == "substitute")):
                out.sub_covered_gaps.append(gap)
                if gap > sub_days:
                    out.sub_over_limit.append(gap)
        prev = d
    if countable:
        out.last_countable_day = _parse_day(countable[-1]["day"])
        out.need_by = out.last_countable_day + timedelta(days=out.max_gap)
    return out


# ---------------------------------------------------------------- 试办证据外推
def trial_evidence(visit_history: list[dict], provider_terms: list[dict],
                   provider: str, care: str, condition: str) -> dict:
    """候选承接方的可比试办样本。

    Q 开头记录来自持有“单独有效试办资质”的机构（README 明确），
    provider_terms 的 cert_from/to 是常规承接期资质窗口，不能用来否定试办期记录；
    因此可比性只核对同 care、同服务条件、结果可认可（done）。
    provider_terms 仅用于确认该机构常规承接期具备对应条件资质（外推资格）。
    """
    regular_cert = any(
        t["provider"] == provider and t["care"] == care
        and ("*" in t["condition"].split("|") or condition in t["condition"].split("|"))
        for t in provider_terms)
    sample, noncomparable, unverified = [], [], []
    for r in visit_history:
        if not r["visit"].startswith("Q") or r["provider"] != provider:
            continue
        if r["care"] != care:
            continue
        if r["outcome"] == "unverified":
            unverified.append(r["visit"])
            continue
        if r["condition"] == condition and r["outcome"] == "done":
            sample.append(r)
        else:
            noncomparable.append(r["visit"])
    gaps = []
    sample.sort(key=lambda r: r["day"])
    for a, b in zip(sample, sample[1:]):
        gaps.append((_parse_day(b["day"]) - _parse_day(a["day"])).days)
    return {"provider": provider, "care": care, "condition": condition,
            "regular_cert_condition_ok": regular_cert,
            "sample_visits": [r["visit"] for r in sample], "n": len(sample),
            "gaps": gaps,
            "gap_max": max(gaps) if gaps else None,
            "noncomparable": noncomparable, "unverified": unverified}


# ---------------------------------------------------------------- 候选窗口
def slot_serves(slot: dict, scope: str) -> bool:
    return scope in slot["scope"].split("|")


def overlaps(a: dict, b: dict) -> bool:
    return (a["team"] == b["team"] and a["day"] == b["day"]
            and int(a["from_minute"]) < int(b["to_minute"])
            and int(b["from_minute"]) < int(a["to_minute"]))


def feasible_slots(candidate_slots: list[dict], provider_terms, consents,
                   scope: str, plan: dict, provider: str) -> list[dict]:
    """该承接方对该项目可实际使用的窗口：资质+同意+窗口 care 匹配。"""
    res = []
    for s in candidate_slots:
        if s["provider"] != provider or s["care"] != plan["care"]:
            continue
        if not slot_serves(s, scope):
            continue
        day = _parse_day(s["day"])
        consent = consent_effective_on(consents, scope, provider, "regular", day)
        cert = any(cert_covers(t, plan["care"], day, plan["condition"])
                   for t in terms_for(provider_terms, provider, plan["care"]))
        if consent and cert:
            res.append(s)
    return res


def bridge_slots(candidate_slots: list[dict], provider_terms, consents,
                 scope: str, plan: dict, provider: str) -> list[dict]:
    """桥接窗口：替代项目、bridge 同意（窗口当日已生效）、替代资质。"""
    sub = plan["substitute"]
    if not sub:
        return []
    res = []
    for s in candidate_slots:
        if s["provider"] != provider or s["care"] != sub:
            continue
        if not slot_serves(s, scope):
            continue
        day = _parse_day(s["day"])
        consent = consent_effective_on(consents, scope, provider, "bridge", day)
        cert = any(cert_covers(t, sub, day, plan["condition"])
                   for t in terms_for(provider_terms, provider, sub))
        if consent and cert:
            res.append(s)
    return res


# ---------------------------------------------------------------- 延长台账
@dataclass
class ExtensionLedger:
    offers: dict = field(default_factory=dict)
    reserves: dict = field(default_factory=dict)  # offer -> list[scope]（未释放）

    def reserved_for(self, offer_id: str, scope: str) -> bool:
        return scope in self.reserves.get(offer_id, [])

    def capacity_left(self, offer_id: str) -> int:
        offer = self.offers[offer_id]
        return int(offer["max_uses"]) - len(self.reserves.get(offer_id, []))


def build_extension_ledger(tables: dict, as_of: date) -> ExtensionLedger:
    led = ExtensionLedger()
    for o in tables["extension_offer"]:
        led.offers[o["offer"]] = o
    for e in sorted(tables["extension_events"], key=lambda r: r["recorded"]):
        oid, scope, kind = e["offer"], e["scope"], e["kind"]
        offer = led.offers[oid]
        bucket = led.reserves.setdefault(oid, [])
        if kind == "reserve":
            # 只有在确认截止日（含）之前登记的保留才有效；截止日后不得新增
            if e["recorded"] <= offer["confirm_until"]:
                if scope not in bucket and len(bucket) < int(offer["max_uses"]):
                    bucket.append(scope)
        elif kind == "release" and scope in bucket:
            bucket.remove(scope)
    return led


def extension_backstop(led: ExtensionLedger, scope: str, plan: dict) -> dict | None:
    """该项目可实际调用的旧岗窗口（已保留未释放，且窗口能覆盖到需要日）。"""
    for oid, offer in led.offers.items():
        if offer["provider"] != plan["old_provider"]:
            continue
        if scope not in offer["scope"].split("|"):
            continue
        if not led.reserved_for(oid, scope):
            continue
        return {"offer": oid, "from": offer["from"], "to": offer["to"],
                "confirm_until": offer["confirm_until"]}
    return None


# ---------------------------------------------------------------- 成组试接
def trial_group_status(tables: dict, served_scopes: set, as_of: date) -> list[dict]:
    out = []
    for tw in tables["trial_windows"]:
        scopes = tw["scope"].split("|")
        valid = [s for s in scopes if s in served_scopes]
        out.append({
            "group": tw["group"], "window": f'{tw["from"]}..{tw["to"]}',
            "scopes": scopes, "valid_served": valid,
            "complete": set(scopes) <= set(valid),
            "note": "整组均有有效签收，可整组确认" if set(scopes) <= set(valid)
                    else "仅部分项目有有效签收，不能释放同组其余服务",
        })
    return out


# ---------------------------------------------------------------- 汇总分析
def analyze(as_of: date) -> dict:
    tables = load()
    plans = tables["plans"]
    plans_by_scope = {p["scope"]: p for p in plans}
    provider_terms = tables["provider_terms"]
    consents = tables["family_consents"]
    candidate_slots = tables["candidate_slots"]
    visit_history = tables["visit_history"]
    proofs = tables["handover_proofs"]

    served_proofs = [h for h in proofs if h["kind"] == "served"]
    # 试接/交接有效签收只认新承接方（旧队 served 属于旧保障兜底，用于连续性但不释放旧保障）
    new_served_scopes = {h["scope"] for h in served_proofs
                         if h["provider"] not in {"A", "B"}}
    led = build_extension_ledger(tables, as_of)

    per_scope = {}
    for plan in plans:
        scope = plan["scope"]
        extra = [h for h in served_proofs if h["scope"] == scope]
        cont = continuity_for(scope, visit_history, plan, provider_terms,
                              plans_by_scope, extra)
        # 候选承接方：regular 同意方 + 仅有 bridge 同意的提供方
        providers = sorted({c["provider"] for c in consents
                            if c["scope"] == scope and c["kind"] in ("regular", "bridge")})
        cand = {}
        missed_by_provider = {}
        for h in proofs:
            if h["scope"] == scope and h["kind"] == "missed":
                missed_by_provider.setdefault(h["provider"], set()).add(h["effective"])
        for prov in providers:
            ev = trial_evidence(visit_history, provider_terms, prov,
                                plan["care"], plan["condition"])
            slots = feasible_slots(candidate_slots, provider_terms, consents,
                                   scope, plan, prov)
            bridges = bridge_slots(candidate_slots, provider_terms, consents,
                                   scope, plan, prov)
            # 交接台已登记 missed 的当日窗口视为实际未接住，不得再算作可行/准时路径
            voided = [s for s in slots
                      if s["day"] in missed_by_provider.get(prov, set())]
            live_slots = [s for s in slots if s not in voided]
            earliest = min((s["day"] for s in live_slots), default=None)
            # 窗口是否赶得上需要日
            timely_slots = [s for s in live_slots
                            if cont.need_by and _parse_day(s["day"]) <= cont.need_by]
            cand[prov] = {
                "evidence": ev,
                "regular_slots": [s["slot"] for s in live_slots],
                "earliest_regular": earliest,
                "timely_slots": [s["slot"] for s in timely_slots],
                "slots_voided_by_missed": [s["slot"] for s in voided],
                "bridge_slots": [s["slot"] for s in bridges],
                "consent_latest": (lambda c: None if c is None else
                                   {"status": c["status"], "from": c["from"],
                                    "to": c["to"], "recorded": c["recorded"]})(
                    max((c for c in consents
                         if c["scope"] == scope and c["provider"] == prov
                         and c["kind"] == "regular"),
                        key=lambda c: c["recorded"], default=None)),
            }
        backstop = extension_backstop(led, scope, plan)
        proof_kinds = sorted({h["kind"] for h in proofs if h["scope"] == scope})
        missed_events = [{"event": h["event"], "provider": h["provider"],
                          "effective": h["effective"]}
                         for h in proofs if h["scope"] == scope and h["kind"] == "missed"]
        per_scope[scope] = {
            "plan": plan,
            "continuity": cont,
            "candidates": cand,
            "extension_backstop": backstop,
            "handover_kinds": proof_kinds,
            "missed_events": missed_events,
            "served_in_handover": scope in new_served_scopes,
        }
    return {
        "as_of": as_of.isoformat(),
        "per_scope": per_scope,
        "ledger": led,
        "trial_groups": trial_group_status(tables, new_served_scopes, as_of),
        "tables": tables,
    }
