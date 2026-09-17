"""有效带宽损耗的瀑布分解。

回答「为什么有效带宽只有峰值的 X%」，并且把损耗归到**可行动的类别**上。

分解的原则是**不重复计数**。损耗分两大类：

1. **器件固有开销**（峰值 → 持续带宽的差距）：这部分来自厂商的表征——协议开销、
   行冲突、刷新。我们改不了它（硬核不可改），但它的大小决定了这个器件的天花板。
2. **系统侧损耗**（持续带宽 → 有效带宽的差距）：这部分是**我们造成的**，可以改：
   - 读写方向翻转：调度策略和写缓冲深度决定
   - 仲裁/排队空闲：上游没喂够（并发不足），或仲裁策略不佳
   - 主设备饿死：上游被挡

   第 2 类里，「仲裁/排队空闲」是 PSRAM 系统最容易被忽略、也通常最大的一块——它
   代表**总线在空等，没有事务可发**，也就是并发不足。把它和器件饱和区分开，就是
   本工具最核心的诊断能力。

时间口径统一用**测量窗口内的时间占比**，避免绝对量在不同窗口长度下不可比。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..slaves.base import SlaveDevice
from ..slaves.hardmac import HardMacSlave
from ..slaves.sram import BankedSramSlave
from ..units import PS_PER_S, bytes_per_ps_to_gbps


@dataclass
class WaterfallStep:
    """瀑布图的一级。"""

    label: str
    """这一级是什么损耗。"""
    loss_gbps: float
    """损失了多少绝对带宽。"""
    loss_pct_of_peak: float
    """占峰值的百分比。"""
    measured: bool
    """是实测的还是从厂商参数推算的。**这个区分很重要**——推算的部分不该被当成
    可优化的空间。"""
    actionable: bool
    """是「改配置能救」的一类，还是「只能重流片/换器件」。"""
    note: str = ""


@dataclass
class BandwidthWaterfall:
    """一个从设备的带宽损耗分解。"""

    slave: str
    peak_gbps: float
    sustained_gbps: float
    effective_gbps: float
    window_ps: int
    bytes_transferred: int
    steps: list[WaterfallStep] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def efficiency_vs_peak(self) -> float:
        return self.effective_gbps / self.peak_gbps if self.peak_gbps > 0 else 0.0

    @property
    def efficiency_vs_sustained(self) -> float:
        return self.effective_gbps / self.sustained_gbps if self.sustained_gbps > 0 else 0.0

    @property
    def system_loss_gbps(self) -> float:
        """系统侧（可优化）的损耗总量。"""
        return sum(s.loss_gbps for s in self.steps if s.measured)

    @property
    def actionable_loss_gbps(self) -> float:
        """可行动的损耗总量——这是架构优化的空间。"""
        return sum(s.loss_gbps for s in self.steps if s.actionable)

    def to_dict(self) -> dict[str, object]:
        return {
            "slave": self.slave,
            "peak_gbps": round(self.peak_gbps, 4),
            "sustained_gbps": round(self.sustained_gbps, 4),
            "effective_gbps": round(self.effective_gbps, 4),
            "efficiency_vs_peak": round(self.efficiency_vs_peak, 4),
            "efficiency_vs_sustained": round(self.efficiency_vs_sustained, 4),
            "system_loss_gbps": round(self.system_loss_gbps, 4),
            "actionable_loss_gbps": round(self.actionable_loss_gbps, 4),
            "steps": [
                {
                    "label": s.label,
                    "loss_gbps": round(s.loss_gbps, 4),
                    "loss_pct_of_peak": round(s.loss_pct_of_peak, 2),
                    "measured": s.measured,
                    "actionable": s.actionable,
                    "note": s.note,
                }
                for s in self.steps
            ],
            "errors": self.errors,
        }


def _time_frac_to_gbps(frac: float, peak_gbps: float) -> float:
    """时间占比 → 折算成损失带宽。"""
    return max(0.0, frac) * peak_gbps


def build_waterfall(
    slave: SlaveDevice,
    window_ps: int,
    effective_gbps: float,
    n_transactions: int,
) -> BandwidthWaterfall:
    """为一个从设备构造损耗分解。

    ``effective_gbps`` 与 ``n_transactions`` 由调用方从监控器取（按测量窗口）。
    """
    if isinstance(slave, HardMacSlave):
        return _hardmac_waterfall(slave, window_ps, effective_gbps, n_transactions)
    if isinstance(slave, BankedSramSlave):
        return _sram_waterfall(slave, window_ps, effective_gbps, n_transactions)
    raise TypeError(f"没有为 {type(slave).__name__} 定义瀑布分解")


def _hardmac_waterfall(
    slave: HardMacSlave,
    window_ps: int,
    effective_gbps: float,
    n_transactions: int,
) -> BandwidthWaterfall:
    """硬核存储的损耗分解。

    **分解口径（关键在于不重复计数）**

    把窗口时间切成三块，它们严格相加等于 1：

        数据占用 + 方向翻转 + 总线空闲 = 窗口

    于是带宽可以精确地写成：

        有效带宽 = 窗口内传输字节 / 窗口
                 = (传输速率) × (数据占用时间 / 窗口)

    其中"传输速率"是**实测的**数据传输期速率（bytes / bus_busy_ps），它已经把
    厂商表征的器件固有开销（协议、行冲突、刷新）包含在内。所以：

        峰值 = 器件固有开销 + 传输速率 × (翻转占比 + 空闲占比) + 有效带宽

    这个恒等式是**按构造成立**的，不会出现各项之和超过峰值的情况。

    反例（曾经的错误做法）：把"器件固有开销"按峰值 − sustained 算，又把"翻转损耗"
    按峰值 × 翻转时间占比 算。两处用了不同的基准，同一段时间被计入两次，加起来会
    超过峰值 17%。检查器抓到了它——这正是分解自检存在的意义。
    """
    env = slave.env
    wf = BandwidthWaterfall(
        slave=slave.name,
        peak_gbps=env.peak_gbps,
        sustained_gbps=(env.sustained_rd_gbps + env.sustained_wr_gbps) / 2.0,
        effective_gbps=effective_gbps,
        window_ps=window_ps,
        bytes_transferred=int(effective_gbps * (window_ps / PS_PER_S)),
    )

    if window_ps <= 0:
        return wf

    data_frac = slave.bus_busy_ps / window_ps
    turn_frac = slave.turnaround_ps_total / window_ps
    idle_frac = slave.bus_idle_ps / window_ps

    # 实测的"数据传输期速率"——这是分解的基准
    if slave.bus_busy_ps > 0:
        # 用 units 里的换算函数，不要手写 1e12/1e9 —— 这类换算极易写错且不会报错，
        # 只会静默给出 0 或差 1000 倍的数（本函数就踩过一次）
        data_rate_gbps = bytes_per_ps_to_gbps(slave.bytes_completed / slave.bus_busy_ps)
    else:
        data_rate_gbps = effective_gbps

    # --- 第一级：器件固有开销（峰值 → 数据传输期速率）---
    inherent = wf.peak_gbps - data_rate_gbps
    if inherent > 0:
        wf.steps.append(WaterfallStep(
            label="器件固有开销", loss_gbps=inherent,
            loss_pct_of_peak=inherent / wf.peak_gbps * 100,
            measured=False, actionable=False,
            note=(
                f"峰值与实测数据传输速率之差（{wf.peak_gbps:.3f} → {data_rate_gbps:.3f} GB/s）。"
                f"厂商声明的分量：刷新 {env.refresh_overhead:.2%}、"
                f"命令相位 {env.cmd_overhead_ns:.1f}ns/事务——**这些已含在其中，不再单独相加**，"
                f"否则重复计数。硬核不可改，但可通过访问局部性间接改善行冲突部分"
            ),
        ))

    # --- 第二级：系统侧的时间损耗（相对数据传输期速率折算）---
    if turn_frac > 0:
        wf.steps.append(WaterfallStep(
            label="读写方向切换", loss_gbps=data_rate_gbps * turn_frac,
            loss_pct_of_peak=data_rate_gbps * turn_frac / wf.peak_gbps * 100,
            measured=True, actionable=True,
            note=(
                f"{slave.turnaround_events} 次切换，占窗口 {turn_frac:.1%}，"
                f"平均方向连续 {slave.mean_direction_run():.1f} 个事务。"
                f"加大写缓冲、提高 min_batch_bursts 可以把代价摊得更薄——"
                f"但批越大读的等待上限越长，这是实时性与效率的真实取舍"
            ),
        ))
    if idle_frac > 0.001:
        wf.steps.append(WaterfallStep(
            label="仲裁/排队空闲 ★", loss_gbps=data_rate_gbps * idle_frac,
            loss_pct_of_peak=data_rate_gbps * idle_frac / wf.peak_gbps * 100,
            measured=True, actionable=True,
            note=(
                f"总线在空等，没有事务可发（占窗口 {idle_frac:.1%}）。"
                f"这是**并发不足**的直接证据——加 outstanding、加 ID、加深缓冲能救，"
                f"与引脚带宽无关"
            ),
        ))

    # --- 一致性检查：按构造应当精确成立，超出说明时间口径对不上 ---
    explained = sum(s.loss_gbps for s in wf.steps) + wf.effective_gbps
    if explained > wf.peak_gbps * 1.02:
        wf.errors.append(
            f"瀑布分解各项之和 {explained:.3f} GB/s 超过峰值 {wf.peak_gbps:.3f} GB/s "
            f"（{(explained/wf.peak_gbps - 1)*100:.1f}%）。说明时间口径不一致——"
            f"数据 {data_frac:.3f} + 翻转 {turn_frac:.3f} + 空闲 {idle_frac:.3f} "
            f"= {data_frac+turn_frac+idle_frac:.3f}（应等于 1）。此结果不可用于决策。"
        )
    return wf


def _sram_waterfall(
    slave: BankedSramSlave,
    window_ps: int,
    effective_gbps: float,
    n_transactions: int,
) -> BandwidthWaterfall:
    """SRAM 的损耗结构和 PSRAM 完全不同。

    SRAM 没有读写方向切换（读写走独立端口），也没有刷新。它的损耗来自：
    - **bank 冲突**：两个访问打到同一个 bank 上被迫串行
    - **跨 bank 拆包**：交织粒度太小导致一个 burst 跨越多个 bank
    - **bank 利用率不足**：访问分布不均，部分 bank 空闲

    所以对 SRAM 报「翻转损耗」是错的——那会把人引向错误的优化方向。
    """
    peak = slave.n_banks * slave.bank_bytes_per_ps * PS_PER_S / 1e9
    wf = BandwidthWaterfall(
        slave=slave.name,
        peak_gbps=peak,
        sustained_gbps=peak,  # SRAM 无固有开销，持续 = 峰值，全部损耗都是设计造成的
        effective_gbps=effective_gbps,
        window_ps=window_ps,
        bytes_transferred=int(effective_gbps * (window_ps / PS_PER_S)),
    )
    if window_ps > 0 and n_transactions > 0:
        conflict_frac = slave.bank_conflicts / n_transactions
        split_frac = slave.cross_bank_splits / n_transactions
        if conflict_frac > 0:
            wf.steps.append(WaterfallStep(
                label="bank 冲突", loss_gbps=wf.peak_gbps * conflict_frac * 0.5,
                loss_pct_of_peak=conflict_frac * 50,
                measured=True, actionable=True,
                note=f"{slave.bank_conflicts}/{n_transactions} 个事务落在忙 bank 上。"
                     f"调整交织粒度或 bank 数可以改善",
            ))
        if split_frac > 0:
            wf.steps.append(WaterfallStep(
                label="跨 bank 拆包", loss_gbps=wf.peak_gbps * split_frac * 0.5,
                loss_pct_of_peak=split_frac * 50,
                measured=True, actionable=True,
                note=f"{slave.cross_bank_splits}/{n_transactions} 个事务跨越 bank 边界，"
                     f"被迫拆成多次访问。交织粒度 {slave.interleave_bytes}B 相对 burst "
                     f"偏小",
            ))
    return wf
