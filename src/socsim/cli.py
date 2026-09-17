"""命令行入口。

::

    socsim run <config> [--out DIR] [--no-plots]
    socsim validate <config>
    socsim sweep <config> --vary <路径>=<值,...> [--watch <主设备>] [--out DIR]
    socsim schema [--out FILE]

``sweep`` 是产出「能力边界」的地方：扫一个旋钮、记录每个点的达成带宽与延迟、
自动标出拐点，并区分是**并发受限**（改配置能救）还是**器件受限**（只能重流片）。
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
from pathlib import Path

from .analysis import build_waterfall, diagnose_run
from .config import SocConfig, load_config
from .soc import Soc
from .units import PS_PER_S


# --- 配置路径操作 ---


def set_by_path(cfg: SocConfig, path: str, value) -> None:
    """按点分路径设置配置字段，如 ``masters.npu.port.target_gbps``。

    用 pydantic 的模型结构做校验，所以路径写错会立刻报错而不是静默忽略。
    """
    parts = path.split(".")
    obj = cfg
    for p in parts[:-1]:
        if isinstance(obj, dict):
            if p not in obj:
                raise KeyError(f"配置路径 {path!r} 中的 {p!r} 不存在")
            obj = obj[p]
        else:
            if not hasattr(obj, p):
                raise KeyError(f"配置路径 {path!r} 中的 {p!r} 不存在")
            obj = getattr(obj, p)
    last = parts[-1]
    # 保持原类型（pydantic 会做转换，但显式转换能给更好的报错）
    try:
        if isinstance(obj, dict):
            old = obj[last]
        else:
            old = getattr(obj, last, None)
        if isinstance(old, bool):
            value = str(value).lower() in ("1", "true", "yes", "on")
        elif isinstance(old, int) and not isinstance(old, bool):
            value = int(float(value))
        elif isinstance(old, float):
            value = float(value)
    except Exception:
        pass
    if isinstance(obj, dict):
        obj[last] = value
    else:
        setattr(obj, last, value)


def _revalidate(cfg: SocConfig) -> SocConfig:
    """改过字段后重新走一遍校验。

    绕过校验直接跑是危险的：比如把 ``target_gbps`` 设成 0 但不改 pattern，
    模型会静默当成饱和源，跑出一根满带宽的曲线让人追一个不存在的问题。
    """
    return SocConfig.model_validate(cfg.model_dump())


# --- run ---


def cmd_run(args) -> int:
    from .report import build_run_summary, make_all_plots, write_run_json
    from .report.summary import format_text_report

    cfg = load_config(args.config)
    if args.name:
        cfg.run.name = args.name

    soc = Soc(cfg)
    res = soc.run()
    summary = build_run_summary(res)

    print(format_text_report(summary))

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        write_run_json(summary, out / "result.json")
        print(f"\n结果已写入 {out / 'result.json'}")
        if not args.no_plots:
            try:
                made = make_all_plots(res, summary, out)
                print(f"图表已写入 {out}/：")
                for name, p in made.items():
                    print(f"  {p.name}")
            except Exception as e:  # pragma: no cover
                print(f"⚠️  图表生成失败（结果 JSON 不受影响）：{type(e).__name__}: {e}")

    # 自校验失败时返回非零码，便于 CI
    v = summary["validation"]
    if not (v["conservation"]["ok"] and v["bandwidth_upper_bound"]["ok"] and v["little_law"]["ok"]):
        print("\n⚠️  三重自校验未全部通过——以上结论不可用于决策", file=sys.stderr)
        return 2
    return 0


# --- validate ---


def cmd_validate(args) -> int:
    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"配置校验失败：\n{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"配置 {args.config} 校验通过。")
    print(f"  主设备 {len(cfg.masters)}：{', '.join(cfg.masters)}")
    print(f"  从设备 {len(cfg.slaves)}：{', '.join(cfg.slaves)}")
    print(f"  包络   {len(cfg.envelopes)}：{', '.join(cfg.envelopes)}")
    print(f"  互联   {cfg.interconnect.name}  策略 {cfg.interconnect.policy}")
    print(f"  窗口   {cfg.run.duration_us}µs（预热 {cfg.run.warmup_us}µs）")

    # 装配期诊断不需要真跑，直接构造模型即可
    soc = Soc(cfg)
    if soc.warnings:
        print("\n装配期警告：")
        for w in soc.warnings:
            print(f"  [!] {w}")
    return 0


# --- sweep ---


TOTAL_LOAD_KEY = "@total_load"
"""特殊扫掠变量：把所有主设备的目标带宽**等比缩放**，使总供载等于给定值。

为什么需要它：拐点曲线要的是「总负载 → 达成带宽」的关系。单独扫一个主设备往往
看不出拐点——当其余主设备已经把从设备打满时，改那一个的速率根本不影响结果
（实测过：npu 从 0.2 扫到 3.0 GB/s，总带宽始终在 2.1~2.4 GB/s 之间，全程器件受限）。
"""


def _scale_masters_to(cfg: SocConfig, total_gbps: float) -> None:
    """把所有启用主设备的目标带宽等比缩放到合计 ``total_gbps``。"""
    active = [m for m in cfg.masters.values() if m.port.enabled]
    if not active:
        raise ValueError("没有启用的主设备，无法缩放总负载")
    fixed = [m for m in active if m.port.pattern == "saturating"]
    adjustable = [m for m in active if m.port.pattern != "saturating"]
    if not adjustable:
        raise ValueError(
            "所有主设备都是 saturating 源，没有可缩放的目标带宽。"
            "要用 @total_load 请把主设备设成有 target_gbps 的速率受限源。"
        )
    if fixed:
        raise ValueError(
            "混用 saturating 源与速率受限源时无法保证总负载等于给定值——"
            "saturating 源会无限供给。请统一改成速率受限源。"
        )
    current = sum(m.port.target_gbps for m in adjustable)
    if current <= 0:
        raise ValueError("当前主设备的目标带宽合计为 0，无法缩放")
    scale = total_gbps / current
    for m in adjustable:
        m.port.target_gbps = round(m.port.target_gbps * scale, 6)


def _detect_knee(loads: list[float], achieved: list[float], latencies: list[float]) -> int | None:
    """自动识别拐点。

    判据：达成带宽的边际增益已降到很小（< 上一点增益的 15%），而延迟已经显著上升
    （> 首点的 1.5 倍）。两个条件同时成立才算——只看带宽会把早饱和的曲线误判成
    器件受限，只看延迟会把噪声当拐点。

    返回拐点的下标；找不到返回 None。
    """
    n = len(loads)
    if n < 3:
        return None
    base_lat = latencies[0] if latencies[0] > 0 else 1.0
    for i in range(1, n):
        gain = achieved[i] - achieved[i - 1]
        prev_gain = achieved[i - 1] - achieved[i - 2] if i >= 2 else achieved[i - 1]
        gain_ratio = gain / prev_gain if prev_gain > 1e-9 else 0.0
        lat_ratio = latencies[i] / base_lat
        if gain_ratio < 0.15 and lat_ratio > 1.5:
            return i
    return None


def cmd_sweep(args) -> int:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from .report.plots import setup_cjk, t

    setup_cjk()
    base = load_config(args.config)

    spec = args.vary
    if "=" not in spec:
        print("--vary 格式应为 <路径>=<值1>,<值2>,...", file=sys.stderr)
        return 1
    path, values_s = spec.split("=", 1)
    raw_values = [v.strip() for v in values_s.split(",") if v.strip()]

    watch = args.watch
    if watch is None:
        if path == TOTAL_LOAD_KEY:
            # 总负载模式下默认观察目标带宽最高的主设备（最容易先感受到拥塞的那个）
            cand = [n for n, m in base.masters.items() if m.port.enabled]
            watch = max(cand, key=lambda n: base.masters[n].port.target_gbps) if cand else None
        elif path.startswith("masters."):
            watch = path.split(".")[1]

    rows: list[dict] = []
    for v in raw_values:
        cfg = copy.deepcopy(base)
        if path == TOTAL_LOAD_KEY:
            _scale_masters_to(cfg, float(v))
        else:
            set_by_path(cfg, path, v)
        cfg = _revalidate(cfg)

        res = Soc(cfg).run()
        T = res.t_measured_ps
        mon = res.monitor

        row = {
            "vary_value": float(v),
            "total_gbps": mon.bandwidth_gbps(mon.completed_bytes, T),
            "ll_error_pct": res.little_law_worst_error_pct(),
            "conservation_ok": res.conservation["ok"],
            "events": res.events,
        }

        # 被观察的主设备
        if watch and watch in res.masters:
            m = res.masters[watch]
            lat = mon.lat_master[watch]
            aps = res.slaves  # noqa: F841
            row.update({
                "watch_master": watch,
                "watch_achieved_gbps": mon.bandwidth_gbps(mon.bytes_by_master.get(watch, 0), T),
                "watch_offered_gbps": m.cfg.target_gbps,
                "watch_mean_latency_ns": lat.mean / 1000.0 if len(lat) else 0.0,
                "watch_p99_latency_ns": lat.percentile(99) / 1000.0 if len(lat) else 0.0,
                "watch_outstanding_avg": mon.outstanding[watch].time_avg(res.t_end_ps),
                "watch_outstanding_limit": res.ports[watch].max_outstanding,
                "watch_stall_frac": m.stall_ps / T if T > 0 else 0.0,
            })

        # 每个从设备的分类与空闲率——拐点性质的判据
        diag = diagnose_run(res)
        for d in diag.slaves:
            row[f"{d.slave}_class"] = d.classification
            row[f"{d.slave}_idle_frac"] = round(d.bus_idle_frac, 4)
            row[f"{d.slave}_turnaround_frac"] = round(d.bus_turnaround_frac, 4)
            row[f"{d.slave}_busy_frac"] = round(d.bus_occupied_frac, 4)
            row[f"{d.slave}_gbps"] = round(d.effective_gbps, 4)

        if args.verbose:
            print(f"  {path}={v}: 总 {row['total_gbps']:.3f} GB/s"
                  + (f"  {watch} 达成 {row.get('watch_achieved_gbps', 0):.3f} GB/s"
                     f"  延迟 {row.get('watch_mean_latency_ns', 0):.0f}ns"
                     if watch else ""))
        rows.append(row)

    # --- 输出 ---
    out = Path(args.out) if args.out else Path("out") / f"sweep_{path.replace('.', '_').lstrip('@')}"
    out.mkdir(parents=True, exist_ok=True)

    with open(out / "sweep.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    if watch and all(f"watch_{k}" in rows[0] for k in ("achieved_gbps", "mean_latency_ns")):
        loads = [r["watch_offered_gbps"] for r in rows]
        achieved = [r["watch_achieved_gbps"] for r in rows]
        lat = [r["watch_mean_latency_ns"] for r in rows]
        p99 = [r["watch_p99_latency_ns"] for r in rows]

        knee = _detect_knee(loads, achieved, lat)

        fig, ax = plt.subplots(figsize=(9, 5.5), dpi=110)
        ax.plot(loads, achieved, "o-", color="#4c72b0", label=t("达成带宽", "Achieved BW"))
        ax.plot(loads, loads, "--", color="#aaa", lw=1,
                label=t("理想（无竞争）", "Ideal (no contention)"))
        ax.set_xlabel(t(f"{watch} 供载速率 (GB/s)", f"{watch} offered rate (GB/s)"))
        ax.set_ylabel(t("达成带宽 (GB/s)", "Achieved (GB/s)"), color="#4c72b0")
        ax.tick_params(axis="y", labelcolor="#4c72b0")
        ax.grid(alpha=0.25)
        ax.set_axisbelow(True)

        ax2 = ax.twinx()
        ax2.plot(loads, lat, "s-", color="#c44e52", label=t("平均延迟", "Mean latency"))
        ax2.plot(loads, p99, "^:", color="#dd8452", label=t("p99 延迟", "p99 latency"))
        ax2.set_ylabel(t("延迟 (ns)", "Latency (ns)"), color="#c44e52")
        ax2.tick_params(axis="y", labelcolor="#c44e52")

        if knee is not None:
            ax.axvline(loads[knee], color="#55a868", ls="--", lw=1.6)
            cls = rows[knee].get("psram0_class", "?")
            ax.annotate(
                t(f"拐点 @ {loads[knee]:.2f} GB/s\n（{cls}）",
                  f"Knee @ {loads[knee]:.2f} GB/s\n({cls})"),
                xy=(loads[knee], achieved[knee]),
                xytext=(loads[knee] * 0.45, max(achieved) * 0.55),
                arrowprops=dict(arrowstyle="->", color="#55a868"),
                fontsize=9, color="#2d6a4f",
            )

        lines, labels = ax.get_legend_handles_labels()
        l2, lab2 = ax2.get_legend_handles_labels()
        ax.legend(lines + l2, labels + lab2, loc="upper left", fontsize=9)
        ax.set_title(t(f"带宽-延迟拐点曲线：{watch}", f"Bandwidth-latency knee: {watch}"),
                     fontsize=12)
        fig.tight_layout()
        fig.savefig(out / "knee_curve.png")
        plt.close(fig)

        print(f"\n拐点曲线已写入 {out / 'knee_curve.png'}")
        if knee is not None:
            r = rows[knee]
            print(f"  拐点：供载 {loads[knee]:.3f} GB/s → 达成 {achieved[knee]:.3f} GB/s，"
                  f"平均延迟 {lat[knee]:.0f}ns（首点 {lat[0]:.0f}ns）")
            print(f"  性质：{r.get('psram0_class', '?')}")
            if r.get("psram0_class") == "concurrency_limited":
                print("  ★ 并发受限 —— 改配置能救（加 outstanding / 加 ID / 加深缓冲）")
            elif r.get("psram0_class") == "device_limited":
                print("  ★ 器件受限 —— 只能换器件或重新流片")
        else:
            print("  未在扫描范围内找到明显拐点（可能整个区间都未饱和，或已全程饱和）")

    print(f"\n扫掠数据已写入 {out / 'sweep.csv'}")
    return 0


# --- schema ---


def cmd_schema(args) -> int:
    from .config import dump_schema

    target = args.out or "socsim-schema.json"
    dump_schema(target)
    print(f"JSON Schema 已写入 {target}")
    print("在 VS Code 的 YAML 扩展里配置 yaml.schemas 指向它即可获得配置补全与校验。")
    return 0


# --- 入口 ---


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="socsim",
        description="SoC 总线带宽 / 拥塞 / 互锁的交易级离散事件仿真器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="跑一个配置并生成报告")
    r.add_argument("config", help="YAML 配置路径")
    r.add_argument("--out", help="输出目录（结果 JSON 与图表）")
    r.add_argument("--name", help="覆盖配置里的 run.name")
    r.add_argument("--no-plots", action="store_true", help="只出 JSON，不画图")
    r.set_defaults(func=cmd_run)

    v = sub.add_parser("validate", help="只校验配置并打印装配期诊断")
    v.add_argument("config")
    v.set_defaults(func=cmd_validate)

    s = sub.add_parser("sweep", help="扫掠一个旋钮，产出带宽-延迟拐点曲线")
    s.add_argument("config")
    s.add_argument("--vary", required=True,
                   help="点分路径=逗号分隔的值，如 masters.npu.port.target_gbps=0.5,1.0,2.0；"
                        "或用 @total_load=1,2,3 扫总负载（所有主设备等比缩放，"
                        "用于画带宽-延迟拐点曲线）")
    s.add_argument("--watch", help="观察哪个主设备的延迟（默认取 --vary 里的主设备）")
    s.add_argument("--out", help="输出目录")
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_sweep)

    sc = sub.add_parser("schema", help="导出 JSON Schema（供编辑器补全）")
    sc.add_argument("--out")
    sc.set_defaults(func=cmd_schema)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
