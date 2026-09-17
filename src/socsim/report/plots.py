"""图表。

**中文显示是一个实际障碍**：matplotlib 默认字体不含 CJK，中文标签会渲染成方框。
这里主动探测系统可用的中文字体；一个都找不到时自动退化为英文标签，而不是画出一堆
豆腐块让人困惑。

四张图回答四个不同的问题：

- **损耗瀑布**：有效带宽缺的那一块花在哪了，哪些是可优化的
- **主从热力图**：谁在打谁，热点是否集中
- **时序图**：拥塞是什么时候开始积累的
- **延迟 CDF**：尾延迟有多长（实时性分析的依据）
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # 无显示环境

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

# --- 中文字体探测 ---

_CJK_CANDIDATES = [
    "Microsoft YaHei", "SimHei", "SimSun", "Noto Sans CJK SC",
    "Source Han Sans SC", "PingFang SC", "WenQuanYi Micro Hei",
]

_CJK_OK = False


def setup_cjk() -> bool:
    """探测并设置可用的中文字体。返回是否有中文可用。"""
    global _CJK_OK
    if _CJK_OK:
        return True
    from matplotlib import font_manager

    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in _CJK_CANDIDATES:
        if name in available:
            plt.rcParams["font.sans-serif"] = [name] + plt.rcParams["font.sans-serif"]
            plt.rcParams["axes.unicode_minus"] = False
            _CJK_OK = True
            return True
    return False


def t(zh: str, en: str) -> str:
    """有中文字体用中文，没有就退化为英文——不要画方框。"""
    return zh if setup_cjk() else en


def _new_fig(w: float = 10.0, h: float = 6.0):
    fig, ax = plt.subplots(figsize=(w, h), dpi=110)
    return fig, ax


# --- 1. 带宽损耗瀑布 ---


def plot_waterfall(wf: dict, out_path: Path) -> Path:
    """损耗瀑布图：从峰值逐级扣到有效带宽。

    每级标注是**实测**还是**推算**——推算的部分不该被当成可优化的空间，
    这个区分在评审时很重要。
    """
    labels = [t("理论峰值", "Peak")]
    values = [wf["peak_gbps"]]
    colors = ["#4c72b0"]
    notes = [""]

    remaining = wf["peak_gbps"]
    for st in wf["steps"]:
        remaining -= st["loss_gbps"]
        labels.append(st["label"])
        values.append(remaining)
        colors.append("#dd8452" if st["actionable"] else "#c44e52")
        notes.append(f"-{st['loss_pct_of_peak']:.1f}%")

    labels.append(t("有效带宽", "Effective"))
    values.append(wf["effective_gbps"])
    colors.append("#55a868")
    notes.append(f"{wf['efficiency_vs_peak']*100:.1f}%")

    fig, ax = _new_fig(11, 6)
    xs = np.arange(len(values))
    ax.bar(xs, values, color=colors, width=0.62)

    # 损失量用箭头标注出来
    # 舍入噪声不标注：低于峰值的 0.5% 的差值画出来只会干扰阅读
    noise_floor = wf["peak_gbps"] * 0.005
    for i in range(1, len(values)):
        drop = values[i - 1] - values[i]
        if drop > noise_floor:
            ax.annotate(
                "", xy=(i, values[i]), xytext=(i, values[i - 1]),
                arrowprops=dict(arrowstyle="->", color="#333", lw=1.4),
            )
            ax.text(i + 0.34, values[i] + drop / 2,
                    f"−{drop:.3f}\n({drop / wf['peak_gbps'] * 100:.1f}%)",
                    fontsize=8, va="center", color="#333")

    ax.set_xticks(xs)
    ax.set_xticklabels(labels, rotation=22, ha="right", fontsize=9)
    ax.set_ylabel(t("带宽 (GB/s)", "Bandwidth (GB/s)"))
    ax.set_title(
        t(f"{wf['slave']} 有效带宽损耗分解", f"{wf['slave']} bandwidth loss breakdown"),
        fontsize=12,
    )
    for i, (v, n) in enumerate(zip(values, notes)):
        if n:
            ax.text(i, v + wf["peak_gbps"] * 0.015, n, ha="center", fontsize=8)
    ax.grid(axis="y", alpha=0.25)
    ax.set_axisbelow(True)

    # 图例：实测 vs 推算 vs 可优化
    from matplotlib.patches import Patch

    ax.legend(
        handles=[
            Patch(color="#4c72b0", label=t("基准", "Baseline")),
            Patch(color="#c44e52", label=t("器件固有（不可优化）", "Inherent (not actionable)")),
            Patch(color="#dd8452", label=t("系统侧（可优化）", "System-side (actionable)")),
            Patch(color="#55a868", label=t("有效带宽", "Effective")),
        ],
        loc="upper right", fontsize=8,
    )
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


# --- 2. 主设备 × 从设备热力图 ---


def plot_matrix(summary: dict, out_path: Path) -> Path | None:
    matrix = summary["aggregate"]["matrix_gbps"]
    masters = [m for m in matrix if matrix[m]]
    if not masters:
        return None
    slaves = sorted({s for row in matrix.values() for s in row})

    data = np.zeros((len(masters), len(slaves)))
    for i, m in enumerate(masters):
        for j, s in enumerate(slaves):
            data[i, j] = matrix[m].get(s, 0.0)

    fig, ax = _new_fig(max(5.5, 2.2 * len(slaves)), max(4.0, 0.7 * len(masters) + 2.2))
    im = ax.imshow(data, cmap="YlOrRd", aspect="auto")
    ax.set_xticks(range(len(slaves)))
    ax.set_xticklabels(slaves, fontsize=9)
    ax.set_yticks(range(len(masters)))
    ax.set_yticklabels(masters, fontsize=9)
    ax.set_xlabel(t("从设备", "Slave"))
    ax.set_ylabel(t("主设备", "Master"))
    ax.set_title(t("主设备 × 从设备 带宽矩阵 (GB/s)", "Master x Slave bandwidth (GB/s)"), fontsize=11)

    for i in range(len(masters)):
        for j in range(len(slaves)):
            v = data[i, j]
            if v > 0:
                ax.text(j, i, f"{v:.3f}", ha="center", va="center", fontsize=8,
                        color="white" if v > data.max() * 0.55 else "#222")
    fig.colorbar(im, ax=ax, label="GB/s")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


# --- 3. 时序：带宽与队列 ---


def plot_timeline(result, out_path: Path) -> Path:
    """上下两栏：总带宽时序 + 从设备队列深度时序。

    要回答的是「拥塞什么时候开始积累」——所以队列深度必须和带宽画在同一个
    时间轴上，才能看出因果关系。
    """
    mon = result.monitor
    bucket_ps = mon.bucket_ps
    ts = mon.ts_total_rd.values()
    n = len(ts)
    if n == 0:
        n = 1
    times_us = np.arange(n) * bucket_ps / 1e6
    bucket_s = bucket_ps / 1e12

    fig, axes = plt.subplots(2, 1, figsize=(11, 6.5), dpi=110, sharex=True)

    ax = axes[0]
    rd = np.array(mon.ts_total_rd.values(), dtype=float) / bucket_s / 1e9
    wr = np.array(mon.ts_total_wr.values(), dtype=float) / bucket_s / 1e9
    ax.stackplot(times_us, rd, wr,
                 labels=[t("读", "Read"), t("写", "Write")],
                 colors=["#4c72b0", "#dd8452"], alpha=0.85)
    ax.set_ylabel(t("带宽 (GB/s)", "Bandwidth (GB/s)"))
    ax.set_title(t("带宽与队列深度时序", "Bandwidth and queue depth over time"), fontsize=11)
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(alpha=0.25)
    ax.set_axisbelow(True)

    ax = axes[1]
    for name, g in mon.queue_depth.items():
        series = _gauge_series(g, bucket_ps, n)
        ax.plot(times_us, series, label=name, lw=1.4)
    ax.set_xlabel(t("时间 (µs)", "Time (us)"))
    ax.set_ylabel(t("队列深度 (事务)", "Queue depth (txn)"))
    ax.legend(loc="upper right", fontsize=9)
    ax.grid(alpha=0.25)
    ax.set_axisbelow(True)

    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def _gauge_series(g, bucket_ps: int, n: int) -> np.ndarray:
    """把 Gauge 的观测点重采样成等比时间轴，便于画图。

    严格做法是记录完整的时间加权轨迹；这里用观测点插值近似，对趋势判断足够，
    而且内存开销与观测点数成正比而不是时间。
    """
    pts = g.trace()
    if not pts:
        return np.zeros(n)
    times = np.array([p[0] for p in pts], dtype=float) / bucket_ps
    vals = np.array([p[1] for p in pts], dtype=float)
    grid = np.arange(n)
    return np.interp(grid, times, vals, left=vals[0], right=vals[-1])


# --- 4. 延迟 CDF ---


def plot_latency_cdf(result, out_path: Path, max_curves: int = 8) -> Path | None:
    """延迟累积分布。尾延迟是实时性分析的核心，所以 CDF 比直方图有用。

    同时在图上标出 p50 / p99，便于直接读数。
    """
    mon = result.monitor
    series = [(name, sc) for name, sc in mon.lat_master.items() if len(sc)]
    if not series:
        return None
    series.sort(key=lambda kv: -kv[1].percentile(50))
    series = series[:max_curves]

    fig, ax = _new_fig(10, 6)
    for name, sc in series:
        arr = np.sort(np.frombuffer(sc._data, dtype=np.int64)) / 1000.0  # → ns
        cdf = np.arange(1, len(arr) + 1) / len(arr)
        # 大样本抽稀，避免图文件过大
        step = max(1, len(arr) // 4000)
        ax.plot(arr[::step], cdf[::step], label=name, lw=1.4)

    ax.axhline(0.5, color="#999", ls=":", lw=1)
    ax.axhline(0.99, color="#c44e52", ls=":", lw=1)
    ax.text(ax.get_xlim()[1], 0.99, " p99", color="#c44e52", fontsize=8, va="bottom", ha="right")
    ax.text(ax.get_xlim()[1], 0.5, " p50", color="#999", fontsize=8, va="bottom", ha="right")

    ax.set_xlabel(t("延迟 (ns)", "Latency (ns)"))
    ax.set_ylabel(t("累积概率", "CDF"))
    ax.set_title(t("各主设备延迟分布", "Per-master latency distribution"), fontsize=11)
    ax.legend(fontsize=9)
    ax.grid(alpha=0.25)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


# --- 5. 仲裁份额 ---


def plot_arbitration(summary: dict, out_path: Path) -> Path | None:
    """仲裁字节份额。回答「谁拿多了、谁被饿死」。"""
    arb = summary["arbitration"]
    if not arb:
        return None
    slaves = list(arb)
    masters = sorted({m for a in arb.values() for m in a["bytes_share"]})
    if not masters:
        return None

    fig, ax = _new_fig(max(6, 2.0 * len(slaves) + 2), 5)
    width = 0.8 / len(masters)
    xs = np.arange(len(slaves))
    for k, m in enumerate(masters):
        vals = [arb[s]["bytes_share"].get(m, 0.0) * 100 for s in slaves]
        ax.bar(xs + k * width - 0.4 + width / 2, vals, width, label=m)

    ax.set_xticks(xs)
    ax.set_xticklabels([f"{s}\n({arb[s]['policy']})" for s in slaves], fontsize=9)
    ax.set_ylabel(t("字节份额 (%)", "Byte share (%)"))
    ax.set_title(t("仲裁字节份额", "Arbitration byte share"), fontsize=11)
    ax.legend(fontsize=9, ncol=min(4, len(masters)))
    ax.grid(axis="y", alpha=0.25)
    ax.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


# --- 汇总入口 ---


def make_all_plots(result, summary: dict, out_dir: Path) -> dict[str, Path]:
    """生成全部图表。返回 ``{名字: 路径}``。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    made: dict[str, Path] = {}

    for name, s in summary["slaves"].items():
        made[f"waterfall_{name}"] = plot_waterfall(
            s["waterfall"], out_dir / f"waterfall_{name}.png"
        )

    p = plot_matrix(summary, out_dir / "matrix.png")
    if p:
        made["matrix"] = p

    made["timeline"] = plot_timeline(result, out_dir / "timeline.png")

    p = plot_latency_cdf(result, out_dir / "latency_cdf.png")
    if p:
        made["latency_cdf"] = p

    p = plot_arbitration(summary, out_dir / "arbitration.png")
    if p:
        made["arbitration"] = p

    return made
