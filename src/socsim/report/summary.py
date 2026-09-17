"""把一次仿真的产出整理成可序列化的结构。

**这是数据契约。** 报告层、Dashboard（Stage 4）、扫掠引擎都消费这个结构，
所以它的字段名一旦定下来就不该随意改。需要加字段就加，不要改语义。

设计原则：JSON 里只放**结论和支撑结论的数字**，不放原始样本（那些太大）。
需要下钻分析时重新跑仿真，或者单独导出。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..analysis import build_waterfall, diagnose_run
from ..units import PS_PER_S


def build_run_summary(result) -> dict[str, Any]:
    """把 ``RunResult`` 整理成完整的结果摘要。"""
    T = result.t_measured_ps
    mon = result.monitor
    cfg = result.cfg

    # --- 三重自校验 ---
    checks = {
        "conservation": result.conservation,
        "bandwidth_upper_bound": {
            "ok": not result.bandwidth_check(),
            "violations": result.bandwidth_check(),
        },
        "little_law": {
            "worst_error_pct": round(result.little_law_worst_error_pct(), 3),
            "tolerance_pct": 2.0,
            "ok": result.little_law_worst_error_pct() < 2.0,
            "detail": result.little_law(),
        },
    }

    # --- 诊断 ---
    diag = diagnose_run(result)

    # --- 从设备：带宽 + 瀑布 ---
    slaves: dict[str, Any] = {}
    for name, dev in result.slaves.items():
        eff = mon.bandwidth_gbps(mon.bytes_by_slave.get(name, 0), T)
        wf = build_waterfall(dev, T, eff, mon.bursts_by_master and 0)
        slaves[name] = {
            "service": dev.service_stats(),
            "effective_gbps": round(eff, 4),
            "waterfall": wf.to_dict(),
            "diagnosis": next((d.to_dict() for d in diag.slaves if d.slave == name), {}),
        }

    # --- 主设备 ---
    masters: dict[str, Any] = {}
    for name, m in result.masters.items():
        lat = mon.lat_master.get(name)
        port = result.ports[name]
        masters[name] = {
            "summary": m.summary(T),
            "port": port.summary(),
            "outstanding_avg": round(mon.outstanding[name].time_avg(result.t_end_ps), 3),
            "outstanding_peak": mon.outstanding[name].peak,
            "outstanding_limit": port.max_outstanding,
            "latency_ps": lat.summary() if len(lat) else {"count": 0},
            "wait_master_ps": mon.wait_master[name].summary()
            if len(mon.wait_master[name]) else {"count": 0},
            "diagnosis": next((d.to_dict() for d in diag.masters if d.master == name), {}),
        }

    return {
        "schema_version": "1.0",
        "config_name": cfg.run.name,
        "window": {
            "t_end_ps": result.t_end_ps,
            "measured_ps": T,
            "warmup_ps": cfg.run.warmup_ps,
            "measured_us": round(T / 1e6, 3),
        },
        "performance": {
            "wall_time_s": round(result.wall_time_s, 4),
            "events": result.events,
            "events_per_second": round(result.events_per_second, 1),
        },
        "validation": checks,
        "diagnosis": diag.to_dict(),
        "aggregate": {
            "total_gbps": round(mon.bandwidth_gbps(mon.completed_bytes, T), 4),
            "rd_gbps": round(mon.bandwidth_gbps(mon.bytes_by_access.get("rd", 0), T), 4),
            "wr_gbps": round(mon.bandwidth_gbps(mon.bytes_by_access.get("wr", 0), T), 4),
            "bytes_by_master": {
                k: round(mon.bandwidth_gbps(v, T), 4)
                for k, v in mon.bytes_by_master.items()
            },
            "bytes_by_slave": {
                k: round(mon.bandwidth_gbps(v, T), 4)
                for k, v in mon.bytes_by_slave.items()
            },
            "matrix_gbps": {
                m: {s: round(v, 4) for s, v in row.items()}
                for m, row in mon.matrix_gbps(T).items()
            },
            "access_split": {
                k: round(mon.bandwidth_gbps(v, T), 4)
                for k, v in mon.bytes_by_access.items()
            },
        },
        "arbitration": result.crossbar.arbiter_summary(),
        "starvation": {
            s: rep
            for s, rep in result.crossbar.starvation_report(
                result.t_end_ps, int(cfg.run.duration_ps * 0.5)
            ).items()
            if rep
        },
        "masters": masters,
        "slaves": slaves,
        "envelopes": {
            name: env.to_envelope().summary() for name, env in cfg.envelopes.items()
        },
        "warnings": result.warnings,
    }


def write_run_json(summary: dict[str, Any], path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    return p


def format_text_report(summary: dict[str, Any]) -> str:
    """终端友好的文本报告。给的是结论，不是数据倾倒。"""
    L: list[str] = []
    w = summary["window"]
    agg = summary["aggregate"]
    val = summary["validation"]

    L.append("=" * 78)
    L.append(f"SoC 带宽分析报告  —  {summary['config_name']}")
    L.append("=" * 78)
    L.append(
        f"测量窗口 {w['measured_us']:.1f}µs（预热 {w['warmup_ps']/1e6:.1f}µs）   "
        f"{summary['performance']['events']:,} 事件  "
        f"{summary['performance']['wall_time_s']:.2f}s  "
        f"{summary['performance']['events_per_second']:,.0f} 事件/秒"
    )
    L.append("")

    # --- 自校验 ---
    ok_c = val["conservation"]["ok"]
    ok_b = val["bandwidth_upper_bound"]["ok"]
    ll = val["little_law"]
    L.append("── 三重自校验 ──")
    L.append(f"  守恒      {'PASS' if ok_c else 'FAIL'}"
             + ("" if ok_c else f"  偏差 {val['conservation']['mismatch_bytes']} 字节"))
    L.append(f"  上界      {'PASS' if ok_b else 'FAIL'}")
    L.append(f"  利特尔    {'PASS' if ll['ok'] else 'FAIL'}  最大误差 {ll['worst_error_pct']}%"
             f"（容差 {ll['tolerance_pct']}%）")
    if not (ok_c and ok_b and ll["ok"]):
        L.append("  ⚠️  校验未全部通过——下面的结论不可用于决策")
    L.append("")

    # --- 结论 ---
    L.append("── 诊断结论 ──")
    L.append(f"  {summary['diagnosis']['headline']}")
    for c in summary["diagnosis"]["critical"]:
        L.append(f"  ! {c}")
    L.append("")

    # --- 带宽 ---
    L.append("── 带宽 ──")
    L.append(f"  总计 {agg['total_gbps']:.3f} GB/s   "
             f"(读 {agg['rd_gbps']:.3f} / 写 {agg['wr_gbps']:.3f})")
    L.append("")
    L.append("  各从设备：")
    for name, s in summary["slaves"].items():
        d = s["diagnosis"]
        L.append(f"    {name:12s} {s['effective_gbps']:7.3f} GB/s   "
                 f"[{d.get('classification','?')}]  {d.get('cost','')}")
        for st in s["waterfall"]["steps"]:
            tag = "  <<< 可优化" if st["actionable"] else ""
            L.append(f"        -{st['loss_pct_of_peak']:6.2f}%  {st['label']}"
                     f" ({'实测' if st['measured'] else '推算'}){tag}")
        wf = s["waterfall"]
        L.append(f"        {'=' * 40}")
        L.append(f"        有效 {wf['effective_gbps']:.3f} GB/s = "
                 f"{wf['efficiency_vs_peak']*100:.1f}% of peak, "
                 f"{wf['efficiency_vs_sustained']*100:.1f}% of sustained")
    L.append("")

    # --- 主设备 ---
    L.append("── 主设备 ──")
    L.append(f"  {'名称':<10} {'达成/目标':>14} {'在途':>10} {'停滞':>6} "
             f"{'延迟':>9} {'p99':>9}  诊断")
    for name, m in summary["masters"].items():
        sm = m["summary"]
        d = m["diagnosis"]
        tgt = sm["offered_gbps"]
        ratio = f"{sm['achieved_gbps']:.3f}/{tgt:.2f}" if tgt > 0 else f"{sm['achieved_gbps']:.3f}/  -"
        lat = m["latency_ps"]
        L.append(
            f"  {name:<10} {ratio:>14} "
            f"{m['outstanding_avg']:5.1f}/{m['outstanding_limit']:<4d} "
            f"{sm['stall_ratio']*100:5.1f}% "
            f"{lat.get('mean', 0)/1000:8.0f}n "
            f"{lat.get('p99', 0)/1000:8.0f}n  {d.get('classification','')}"
        )
    L.append("")

    # --- 仲裁 ---
    L.append("── 仲裁字节份额 ──")
    for slave, a in summary["arbitration"].items():
        L.append(f"  {slave} (策略 {a['policy']})")
        for m, share in sorted(a["bytes_share"].items(), key=lambda kv: -kv[1]):
            L.append(f"    {m:<10} {share*100:5.1f}%   {a['granted_bytes'][m]:,} 字节")
    L.append("")

    if summary["starvation"]:
        L.append("── 饿死检测 ──")
        for slave, reps in summary["starvation"].items():
            for r in reps:
                L.append(f"  [{slave}] {r['flow']} 已有 {r['gap_ps']/1000:.0f}ns 未获得授权"
                         f"（累计授权 {r['grants']} 次）")
        L.append("")

    if summary["warnings"]:
        L.append("── 装配期警告 ──")
        for warn in summary["warnings"]:
            L.append(f"  [!] {warn}")
        L.append("")

    return "\n".join(L)
