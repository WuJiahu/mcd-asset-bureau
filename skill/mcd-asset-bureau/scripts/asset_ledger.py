#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
麦门资产管理局 · 清算引擎

把麦当劳会员权益（券 / 积分 / 奖品 / 机会窗口）当作会过期的资产，
计算面值、日衰减率与蒸发账单，输出按优先级排序的清算指令。

设计约束：
  - 纯标准库，无第三方依赖，可离线运行
  - 所有时间以 assets.json 的 as_of 为基准，保证可复算、可回放
  - 缺字段一律降级处理，绝不补造数据

用法：
  python3 asset_ledger.py assets.json
  python3 asset_ledger.py assets.json --html ledger.html
  python3 asset_ledger.py assets.json --allocate 3
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------- 常量

TIER_LABEL = {
    0: "T0 零成本窗口",
    1: "T1 24h内到期",
    2: "T2 7天内到期",
    3: "T3 有缓冲",
    4: "T4 期限未知",
    5: "已失效",
}

MIN_DAYS = 1.0 / 24.0          # 剩余不足 1 小时按 1 小时计，避免除零与无穷大
WEEKDAY_CN = ["一", "二", "三", "四", "五", "六", "日"]

# ---------------------------------------------------------------- 数据结构


@dataclass
class Asset:
    id: str
    name: str
    kind: str                      # coupon | points | prize | window
    value: float | None = None     # 面值（元）；None = 待核价
    expires_at: datetime | None = None
    channels: list[str] = field(default_factory=list)
    time_window: str = ""
    weekdays: list[int] = field(default_factory=list)
    note: str = ""
    value_source: str = ""

    # 派生字段（由 compute 填充）
    days_left: float | None = None
    theta: float | None = None     # 元/天
    annual_rate: float | None = None
    tier: int = 3

    def usable_on(self, when: datetime) -> bool:
        """该资产在某个时刻是否可执行。"""
        if self.expires_at and when > self.expires_at:
            return False
        if self.kind == "window" and self.expires_at and when < (self.starts_at or self.expires_at - timedelta(days=1)):
            return False
        if self.weekdays and when.isoweekday() not in self.weekdays:
            return False
        return True

    starts_at: datetime | None = None


# ---------------------------------------------------------------- 时间


def parse_dt(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if not s:
        return None
    # 先试 ISO
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def align_tz(dt: datetime | None, ref: datetime) -> datetime | None:
    """无时区的时间按 as_of 的时区解释，保证可比。"""
    if dt is None:
        return None
    if dt.tzinfo is None and ref.tzinfo is not None:
        return dt.replace(tzinfo=ref.tzinfo)
    if dt.tzinfo is not None and ref.tzinfo is None:
        return dt.replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------- 构建资产


def end_of_month(ref: datetime) -> datetime:
    """当月最后一日的 23:59:59。

    注意：datetime.replace(day=1) 会保留时分秒，因此必须先定位到次月首日、
    减一天、再显式把时间拨到日终，否则会得到"次月 1 日 16:10"这种错误边界。
    """
    next_first = (ref.replace(day=1) + timedelta(days=32)).replace(day=1)
    last_day = next_first - timedelta(days=1)          # 仍是 ref 的时分秒
    return last_day.replace(hour=23, minute=59, second=59, microsecond=0,
                            tzinfo=ref.tzinfo)


def build_assets(data: dict, as_of: datetime) -> list[Asset]:
    assets: list[Asset] = []

    for i, c in enumerate(data.get("coupons") or []):
        value = c.get("value")
        src = "explicit"
        if value is None:
            pw, pwo = c.get("price_with"), c.get("price_without")
            if isinstance(pw, (int, float)) and isinstance(pwo, (int, float)) and pwo > pw:
                value = round(float(pwo) - float(pw), 2)
                src = "price_without - price_with"
        assets.append(Asset(
            id=c.get("id") or f"coupon-{i+1}",
            name=c.get("name") or f"未命名券 {i+1}",
            kind="coupon",
            value=value,
            expires_at=align_tz(parse_dt(c.get("expires_at")), as_of),
            channels=c.get("channels") or [],
            time_window=c.get("time_window") or "",
            weekdays=c.get("weekdays") or [],
            note=c.get("note") or "",
            value_source=src if value is not None else "待核价",
        ))

    pts = data.get("points") or {}
    if pts:
        uv = pts.get("unit_value")
        cm = pts.get("current_month_expire")
        if isinstance(cm, (int, float)) and cm > 0:
            v = round(float(cm) * uv, 2) if isinstance(uv, (int, float)) else None
            assets.append(Asset(
                id="points-expiring",
                name=f"本月将过期积分 {cm}",
                kind="points",
                value=v,
                expires_at=end_of_month(as_of),
                note="到期日按当月月末估算，实际以官方规则为准",
                value_source=f"{cm} × {uv} 元/积分" if v is not None else "兑换率未知",
            ))

    for i, p in enumerate(data.get("prizes") or []):
        if p.get("status") and p.get("status") not in ("可用",):
            continue
        assets.append(Asset(
            id=p.get("id") or f"prize-{i+1}",
            name=p.get("name") or f"未命名奖品 {i+1}",
            kind="prize",
            value=None,
            expires_at=align_tz(parse_dt(p.get("expires_at")), as_of),
            note=("非货币资产 · " + (p.get("kind") or "未分类")) if p.get("kind") else "非货币资产",
            value_source="不折算",
        ))

    for i, w in enumerate(data.get("windows") or []):
        assets.append(Asset(
            id=w.get("id") or f"window-{i+1}",
            name=w.get("name") or f"机会窗口 {i+1}",
            kind="window",
            value=(float(w["expected_value"]) if isinstance(w.get("expected_value"), (int, float)) and w["expected_value"] > 0 else None),
            expires_at=align_tz(parse_dt(w.get("ends_at")), as_of),
            starts_at=align_tz(parse_dt(w.get("starts_at")), as_of),
            note=w.get("action") or "",
            value_source="零成本" if not w.get("cost") else "有成本",
        ))

    return assets


def compute(assets: list[Asset], as_of: datetime) -> None:
    for a in assets:
        if a.expires_at is None:
            a.days_left, a.theta, a.annual_rate = None, None, None
            a.tier = 0 if a.kind == "window" else 4
            continue
        seconds = (a.expires_at - as_of).total_seconds()
        if seconds <= 0:
            a.days_left = 0.0
            a.theta = None
            a.annual_rate = None
            a.tier = 5  # 已失效
            continue
        d = max(seconds / 86400.0, MIN_DAYS)
        a.days_left = d
        if a.value is not None:
            a.theta = a.value / d
            a.annual_rate = (365.0 / d) * 100.0
        else:
            a.theta, a.annual_rate = None, None
        if a.kind == "window":
            a.tier = 0
        elif d <= 1.0:
            a.tier = 1
        elif d <= 7.0:
            a.tier = 2
        else:
            a.tier = 3


def sort_assets(assets: list[Asset]) -> list[Asset]:
    """tier 为主键，θ 为次键；θ 未知的排在已知之后。"""
    def key(a: Asset):
        t = a.theta if a.theta is not None else -1.0
        return (a.tier, -t, a.name)
    return sorted(assets, key=key)


# ---------------------------------------------------------------- 分析


def sunk_cost(data: dict) -> dict:
    pts = data.get("points") or {}
    uv = pts.get("unit_value")
    acc = pts.get("accumulative")
    exp = pts.get("expired")
    out = {
        "accumulative": acc,
        "expired": exp,
        "available": pts.get("available"),
        "unit_value": uv,
        "rate": None,
        "amount": None,
    }
    if isinstance(acc, (int, float)) and acc > 0 and isinstance(exp, (int, float)):
        out["rate"] = exp / acc * 100.0
        if isinstance(uv, (int, float)):
            out["amount"] = exp * uv
    return out


def find_duplicates(assets: list[Asset]) -> list[list[Asset]]:
    buckets: dict[tuple, list[Asset]] = {}
    for a in assets:
        if a.kind != "coupon":
            continue
        sig = (a.name.strip(), a.value, a.expires_at)
        buckets.setdefault(sig, []).append(a)
    return [g for g in buckets.values() if len(g) > 1]


def risk_exposure(assets: list[Asset], days: float = 7.0) -> float:
    total = 0.0
    for a in assets:
        if a.value is None or a.days_left is None:
            continue
        if 0 < a.days_left <= days:
            total += a.value
    return round(total, 2)


def same_day_conflicts(assets: list[Asset]) -> list[tuple]:
    """同一自然日内有多张券到期：一单只能用一张，必须拆单才可能全部兑现。

    返回 [(日期, 券组, 若只下 N 单的必然损失)]，按日期升序。
    """
    groups: dict = {}
    for a in assets:
        if a.kind != "coupon" or a.expires_at is None:
            continue
        if a.days_left is not None and a.days_left <= 0:
            continue
        groups.setdefault(a.expires_at.date(), []).append(a)

    out = []
    for d in sorted(groups):
        g = groups[d]
        if len(g) < 2:
            continue
        # 完全相同的券已在"重复券"中提示过，不再重复占用一行
        sig = {(x.name, x.value, x.expires_at) for x in g}
        if len(sig) == 1:
            continue
        vals = [x.value for x in g if x.value is not None]
        loss = round(sum(vals) - max(vals), 2) if len(vals) >= 2 else None
        out.append((d, g, loss))
    return out


def recoverable(assets: list[Asset]) -> float:
    return round(sum(a.value for a in assets
                     if a.value is not None and (a.days_left is None or a.days_left > 0)), 2)


# ---------------------------------------------------------------- 分配


def allocate(assets: list[Asset], meals: int, as_of: datetime, meal_dates: list[datetime] | None = None):
    """N 个餐次 × M 张券 的最大节省匹配。

    一单只能用一张券 → 把券分配给互不相同的餐次，使总面值最大。
    用位掩码 DP 求精确最优（M ≤ 16），超出则退化为贪心。
    """
    pool = [a for a in assets if a.kind == "coupon" and a.value is not None and a.days_left and a.days_left > 0]
    if not pool or meals <= 0:
        return [], pool

    dates = meal_dates or [as_of + timedelta(days=i) for i in range(meals)]
    dates = dates[:meals]

    if len(pool) > 16:
        pool.sort(key=lambda a: -a.value)
        used, plan = set(), []
        for d in dates:
            for a in pool:
                if id(a) in used:
                    continue
                if a.usable_on(d):
                    plan.append((d, a)); used.add(id(a)); break
        remaining = [a for a in pool if id(a) not in used]
        return plan, remaining

    m = len(pool)
    NEG = float("-inf")
    # dp[i][mask] = 前 i 个餐次、使用 mask 券集时的最大总面值
    dp = [[NEG] * (1 << m) for _ in range(len(dates) + 1)]
    choice = [[-1] * (1 << m) for _ in range(len(dates) + 1)]
    dp[0][0] = 0.0

    for i in range(len(dates)):
        d = dates[i]
        for mask in range(1 << m):
            if dp[i][mask] == NEG:
                continue
            # 不用券
            if dp[i][mask] > dp[i + 1][mask]:
                dp[i + 1][mask] = dp[i][mask]
                choice[i + 1][mask] = -1
            # 用第 j 张
            for j in range(m):
                if mask >> j & 1:
                    continue
                if not pool[j].usable_on(d):
                    continue
                nm = mask | (1 << j)
                val = dp[i][mask] + (pool[j].value or 0)
                if val > dp[i + 1][nm]:
                    dp[i + 1][nm] = val
                    choice[i + 1][nm] = j

    best_mask, best_val = 0, NEG
    for mask in range(1 << m):
        if dp[len(dates)][mask] > best_val:
            best_val, best_mask = dp[len(dates)][mask], mask

    # 回溯
    plan = []
    mask = best_mask
    for i in range(len(dates), 0, -1):
        j = choice[i][mask]
        if j >= 0:
            plan.append((dates[i - 1], pool[j]))
            mask ^= (1 << j)
    plan.reverse()
    remaining = [a for i, a in enumerate(pool) if not (best_mask >> i & 1)]
    return plan, remaining


# ---------------------------------------------------------------- 输出


def money(v, unknown="待核价") -> str:
    if v is None:
        return unknown
    return f"¥{v:,.2f}"


def show_value(a: Asset) -> str:
    """窗口类资产的"面值"是零成本而非待核价，两者含义完全不同。"""
    if a.kind == "window" and a.value is None:
        return "零成本"
    if a.kind == "prize" and a.value is None:
        return "不折算"
    return money(a.value)


def fmt_left(days: float | None) -> str:
    if days is None:
        return "—"
    if days == 0:
        return "已失效"
    if days < 1:
        return f"{days*24:.1f} 小时"
    return f"{days:.1f} 天"


def render_text(data: dict, assets: list[Asset], as_of: datetime, show_alloc: int = 0) -> str:
    L: list[str] = []
    bar = "─" * 68
    L.append("")
    L.append("  麦门资产管理局 · 清算报告")
    L.append(f"  快照时刻：{as_of.strftime('%Y-%m-%d %H:%M')}")
    L.append(bar)

    # 1. 蒸发账单
    sc = sunk_cost(data)
    L.append("")
    L.append("【一】蒸发账单")
    if sc["accumulative"] is not None:
        line = f"  累计积分 {sc['accumulative']}"
        if sc["expired"] is not None:
            line += f"｜已过期 {sc['expired']}"
        if sc["rate"] is not None:
            line += f"｜蒸发率 {sc['rate']:.1f}%"
        L.append(line)
        if sc["amount"] is not None:
            L.append(f"  折合已蒸发金额：{money(sc['amount'])}（按 {sc['unit_value']} 元/积分）")
        if sc["rate"] is not None and sc["rate"] >= 50:
            L.append("  ⚠ 超过一半的历史积分从未被兑现——这不是优惠问题，是调度问题。")
    else:
        L.append("  积分数据未采集")

    exp = risk_exposure(assets)
    rec = recoverable(assets)
    L.append("")
    L.append(f"  在险敞口（7 天内到期）：{money(exp)}")
    L.append(f"  可回收价值（全部未失效）：{money(rec)}")

    # 2. 台账
    L.append("")
    L.append("【二】资产台账（按 tier → 日衰减率 排序）")
    ordered = sort_assets(assets)
    if not ordered:
        L.append("  无资产记录")
    else:
        L.append(f"  {'#':<3}{'资产':<24}{'面值':>10}{'剩余':>10}{'θ(元/天)':>11}  优先级")
        L.append("  " + "─" * 64)
        for i, a in enumerate(ordered, 1):
            name = a.name if len(a.name) <= 22 else a.name[:21] + "…"
            theta = f"{a.theta:.1f}" if a.theta is not None else "—"
            L.append(f"  {i:<3}{name:<24}{show_value(a):>10}{fmt_left(a.days_left):>10}{theta:>11}  {TIER_LABEL.get(a.tier,'')}")

    # 3. 清算指令
    L.append("")
    L.append("【三】今日清算指令")
    actionable = [a for a in ordered if a.tier in (0, 1, 2)]
    if not actionable:
        L.append("  当前无 7 天内到期资产。")
    for i, a in enumerate(actionable[:3], 1):
        when = a.expires_at.strftime("%m-%d %H:%M") if a.expires_at else "尽快确认"
        act = a.note or ("立即预约" if a.kind == "window" else "安排消费")
        L.append(f"  {i}. [{TIER_LABEL[a.tier]}] {act}：{a.name}")
        detail = f"     截止 {when}｜剩余 {fmt_left(a.days_left)}"
        if a.value is not None:
            detail += f"｜面值 {money(a.value)}"
            if a.theta is not None:
                detail += f"｜θ {a.theta:.1f} 元/天"
                # 年化仅在剩余 ≥1 天时有意义；小时级资产的年化会放大到荒谬量级
                if a.days_left and a.days_left >= 1 and a.annual_rate:
                    detail += f"｜年化损耗 {a.annual_rate:,.0f}%"
        elif a.kind == "window":
            detail += "｜成本 0，纯正期望"
        L.append(detail)
        if a.channels:
            L.append(f"     渠道：{'、'.join(a.channels)}" + (f"｜时段 {a.time_window}" if a.time_window else ""))

    # 4. 冗余与风险
    L.append("")
    L.append("【四】冗余与风险")
    dups = find_duplicates(assets)
    if dups:
        for g in dups:
            L.append(f"  · 重复券「{g[0].name}」×{len(g)} 张，一单仅可用一张，需拆 {len(g)} 次消费；"
                     f"否则未用的一张将随同批到期")
    conflicts = same_day_conflicts(assets)
    for d, g, loss in conflicts:
        names = "、".join(a.name for a in g)
        L.append(f"  · 同日到期冲突 {d.strftime('%m-%d')}：{len(g)} 张券同日失效（{names}）")
        L.append(f"    一单只能用一张 → 需当天拆 {len(g)} 单才能全部兑现"
                 + (f"；否则必然损失 {money(loss)}" if loss else ""))
    if not dups and not conflicts:
        L.append("  · 未发现重复券或同日到期冲突")
    unknown = [a for a in assets if a.value is None and a.kind in ("coupon", "points")]
    if unknown:
        L.append(f"  · {len(unknown)} 项面值待核价（未计入合计）：" + "、".join(a.name for a in unknown[:5]))
    dead = [a for a in assets if a.tier == 5]
    if dead:
        lost = sum(a.value for a in dead if a.value is not None)
        L.append(f"  · 已失效 {len(dead)} 项，沉没成本 {money(lost)}："
                 + "、".join(a.name for a in dead[:5]))
    undated = [a for a in assets if a.tier == 4]
    if undated:
        L.append(f"  · {len(undated)} 项有效期未知，建议尽快确认：" + "、".join(a.name for a in undated[:5]))

    # 5. 分配
    if show_alloc > 0:
        L.append("")
        L.append(f"【五】{show_alloc} 餐次联合配置")
        plan, remaining = allocate(assets, show_alloc, as_of)
        if not plan:
            L.append("  无可用于分配的券")
        else:
            total = sum(a.value for _, a in plan)
            for d, a in plan:
                L.append(f"  {d.strftime('%m-%d 周')}{WEEKDAY_CN[d.isoweekday()-1]}  {a.name}  [{money(a.value)}]")
            L.append(f"  合计节省 {money(total)}")
        if remaining:
            last_day = (as_of + timedelta(days=show_alloc - 1)).date()
            wasted = [a for a in remaining if a.expires_at and a.expires_at.date() <= last_day]
            # 用 id() 而非值比较：两张完全相同的券若用 == 会被误判为同一对象
            wasted_ids = {id(a) for a in wasted}
            keepable = [a for a in remaining if id(a) not in wasted_ids]
            if wasted:
                lost = sum(a.value for a in wasted if a.value is not None)
                L.append(f"  ⚠ 方案期内即失效、将被浪费 {len(wasted)} 张："
                         + "、".join(a.name for a in wasted[:4])
                         + (f"（合计 {money(lost)}）" if lost else ""))
            if keepable:
                L.append(f"  可留待下周：{len(keepable)} 张 —— "
                         + "、".join(a.name for a in keepable[:4]))

    L.append("")
    L.append(bar)
    L.append("  注：面值与金额为基于当前快照的估算，价格、库存、活动以麦当劳官方实时结果为准。")
    L.append("")
    return "\n".join(L)


def render_html(data: dict, assets: list[Asset], as_of: datetime) -> str:
    sc = sunk_cost(data)
    ordered = sort_assets(assets)
    exp = risk_exposure(assets)
    rec = recoverable(assets)
    actionable = [a for a in ordered if a.tier in (0, 1, 2)][:3]
    max_theta = max([a.theta for a in assets if a.theta] or [1.0])

    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    KIND_CN = {"coupon": "券", "points": "积分", "prize": "奖品", "window": "窗口"}

    rows = []
    for a in ordered:
        pct = (a.theta / max_theta * 100) if (a.theta is not None and max_theta) else 0
        badge = {0: ("#0b6b3a", "#e6f4ea"), 1: ("#a4161a", "#fdecea"),
                 2: ("#8a5a00", "#fff4e0"), 3: ("#4a4a4a", "#f0f0f0"),
                 4: ("#5a5a5a", "#f5f5f5"), 5: ("#8a8a8a", "#ececec")}.get(a.tier, ("#4a4a4a", "#f0f0f0"))
        rows.append(f"""
        <tr>
          <td class="nm">{esc(a.name)}<span class="kind">{KIND_CN.get(a.kind, a.kind)}</span></td>
          <td class="num">{show_value(a)}</td>
          <td class="num">{fmt_left(a.days_left)}</td>
          <td>
            <div class="barrow">
              <div class="barwrap"><div class="bar" style="width:{pct:.0f}%"></div></div>
              <span class="bval">{f"{a.theta:.1f}" if a.theta is not None else "—"}</span>
            </div>
          </td>
          <td><span class="tier" style="color:{badge[0]};background:{badge[1]}">{TIER_LABEL.get(a.tier,'')}</span></td>
        </tr>""")

    acts = []
    for i, a in enumerate(actionable, 1):
        when = a.expires_at.strftime("%m-%d %H:%M") if a.expires_at else "尽快确认"
        if a.value is not None:
            val_txt = f" · 面值 {money(a.value)}"
        elif a.kind == "window":
            val_txt = " · 成本 0"
        else:
            val_txt = ""
        theta_txt = f" · θ {a.theta:.1f} 元/天" if a.theta else ""
        acts.append(f"""
        <div class="act">
          <div class="idx">{i}</div>
          <div>
            <div class="act-t">{esc(a.note or ('立即预约' if a.kind == 'window' else '安排消费'))}：{esc(a.name)}</div>
            <div class="act-s">截止 {when} · 剩余 {fmt_left(a.days_left)}{val_txt}{theta_txt}</div>
          </div>
        </div>""")

    sunk_html = ""
    if sc["rate"] is not None:
        sunk_html = f"""
        <div class="sunk">
          <div class="sunk-n">{sc['rate']:.1f}%</div>
          <div class="sunk-l">累计积分蒸发率<br><b>{sc['expired']}</b> / {sc['accumulative']}
          {f"<br>折合 {money(sc['amount'])}" if sc["amount"] else ""}</div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>麦门资产台账 · {as_of.strftime('%Y-%m-%d')}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
background:#f7f6f3;color:#1d1d1f;padding:40px 20px;line-height:1.6}}
.card{{max-width:960px;margin:0 auto;background:#fff;border-radius:18px;
box-shadow:0 8px 40px rgba(0,0,0,.07);overflow:hidden}}
.hd{{background:linear-gradient(135deg,#2b1055 0%,#5a1a2b 55%,#7a2f1f 100%);color:#fff;padding:32px 36px}}
.hd h1{{font-size:23px;font-weight:600;letter-spacing:.5px}}
.hd p{{opacity:.72;font-size:13px;margin-top:6px}}
.stats{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;border-bottom:1px solid #eee}}
.stats>div{{padding:22px 26px;border-right:1px solid #eee}}
.stats>div:last-child{{border-right:none}}
.stats .lb{{font-size:12px;color:#888;letter-spacing:.5px}}
.stats .vl{{font-size:24px;font-weight:600;margin-top:4px;font-variant-numeric:tabular-nums}}
.sunk{{display:flex;align-items:center;gap:20px;padding:24px 30px;background:#fff8f6;
border-bottom:1px solid #f2e2dd}}
.sunk-n{{font-size:38px;font-weight:700;color:#b3202b;font-variant-numeric:tabular-nums}}
.sunk-l{{font-size:13px;color:#6b4a44}}
.sec{{padding:26px 30px}}
.sec h2{{font-size:14px;font-weight:600;color:#333;margin-bottom:14px;letter-spacing:.8px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;font-weight:500;color:#999;font-size:11px;letter-spacing:.8px;
padding:0 8px 10px;border-bottom:1px solid #eee}}
td{{padding:12px 8px;border-bottom:1px solid #f5f5f5;vertical-align:middle}}
.nm{{font-weight:500}}
.kind{{display:inline-block;margin-left:8px;font-size:10px;color:#aaa;letter-spacing:.5px}}
.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
.barrow{{display:flex;align-items:center;gap:8px;justify-content:flex-end;min-width:150px}}
.barwrap{{flex:1;height:8px;background:#f2f2f2;border-radius:4px;overflow:hidden;min-width:56px}}
.bar{{height:100%;background:linear-gradient(90deg,#e8a33d,#c2452d);border-radius:4px}}
.bval{{font-size:11px;font-weight:600;color:#444;width:36px;text-align:right;font-variant-numeric:tabular-nums}}
.tier{{font-size:10px;padding:2px 8px;border-radius:10px;white-space:nowrap}}
.act{{display:flex;gap:14px;padding:14px 0;border-bottom:1px solid #f5f5f5}}
.act:last-child{{border-bottom:none}}
.idx{{width:24px;height:24px;flex:0 0 24px;border-radius:50%;background:#1d1d1f;color:#fff;
font-size:12px;display:flex;align-items:center;justify-content:center;font-weight:600}}
.act-t{{font-size:14px;font-weight:500}}
.act-s{{font-size:12px;color:#888;margin-top:3px}}
.ft{{padding:18px 30px;background:#fafafa;font-size:11px;color:#999;border-top:1px solid #eee}}
</style></head><body><div class="card">
  <div class="hd"><h1>麦门资产台账</h1>
  <p>快照 {as_of.strftime('%Y-%m-%d %H:%M')} · 本地清算生成 · 非麦当劳官方产品</p></div>
  {sunk_html}
  <div class="stats">
    <div><div class="lb">在险敞口 · 7天内</div><div class="vl">{money(exp)}</div></div>
    <div><div class="lb">可回收价值</div><div class="vl">{money(rec)}</div></div>
    <div><div class="lb">资产条目</div><div class="vl">{len(assets)}</div></div>
  </div>
  <div class="sec"><h2>资 产 台 账</h2>
    <table><thead><tr><th>资产</th><th style="text-align:right">面值</th>
    <th style="text-align:right">剩余</th><th style="text-align:right">日衰减率 θ</th><th>优先级</th></tr></thead>
    <tbody>{''.join(rows)}</tbody></table>
  </div>
  <div class="sec"><h2>今 日 清 算 指 令</h2>{''.join(acts) or '<p style="color:#999;font-size:13px">当前无 7 天内到期资产</p>'}</div>
  <div class="ft">面值与金额为基于当前快照的估算，价格、库存、活动以麦当劳官方实时结果为准。
  本工具不构成消费或营养建议。</div>
</div></body></html>"""


# ---------------------------------------------------------------- 入口


def main() -> int:
    ap = argparse.ArgumentParser(description="麦门资产清算引擎")
    ap.add_argument("input", help="assets.json 路径")
    ap.add_argument("--html", metavar="PATH", help="另出一份 HTML 台账")
    ap.add_argument("--allocate", type=int, default=0, metavar="N", help="按 N 个餐次做券的最优分配")
    args = ap.parse_args()

    p = Path(args.input)
    if not p.exists():
        print(f"输入文件不存在：{p}", file=sys.stderr)
        return 2
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"JSON 解析失败：{e}", file=sys.stderr)
        return 2

    as_of = parse_dt(data.get("as_of")) or datetime.now().astimezone()
    if as_of.tzinfo is None:
        as_of = as_of.astimezone()

    assets = build_assets(data, as_of)
    compute(assets, as_of)

    print(render_text(data, assets, as_of, show_alloc=args.allocate))

    if args.html:
        out = Path(args.html)
        out.write_text(render_html(data, assets, as_of), encoding="utf-8")
        print(f"HTML 台账已生成：{out.resolve()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
