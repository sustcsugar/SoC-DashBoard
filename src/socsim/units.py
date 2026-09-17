"""时间与容量单位。

全局约定：**仿真内部的唯一时间单位是皮秒（int）**。

为什么不用纳秒浮点：400MHz 的时钟周期是 2.5ns，1ns 分辨率下无法整数表示；而浮点
比较会让同刻事件的排序变得不确定，破坏可复现性。用皮秒后所有常见时钟频率都是整数
周期，且 Python 的任意精度整数不会溢出（100ms 窗口 = 10^11 ps，远在安全范围内）。

取整误差：非整数周期（如 166MHz → 6024.096ps）会取整到 6024ps，相对误差 ~0.0016%，
对架构级结论无影响。
"""

from __future__ import annotations

# --- 时间 ---

PS_PER_NS = 1_000
PS_PER_US = 1_000_000
PS_PER_MS = 1_000_000_000
PS_PER_S = 1_000_000_000_000


def ns(value: float) -> int:
    """纳秒 → 皮秒。"""
    return int(round(value * PS_PER_NS))


def us(value: float) -> int:
    """微秒 → 皮秒。"""
    return int(round(value * PS_PER_US))


def ms(value: float) -> int:
    """毫秒 → 皮秒。"""
    return int(round(value * PS_PER_MS))


def as_ns(value_ps: int) -> float:
    """皮秒 → 纳秒（仅用于显示，不参与仿真计算）。"""
    return value_ps / PS_PER_NS


def as_us(value_ps: int) -> float:
    """皮秒 → 微秒（仅用于显示）。"""
    return value_ps / PS_PER_US


def as_ms(value_ps: int) -> float:
    """皮秒 → 毫秒（仅用于显示）。"""
    return value_ps / PS_PER_MS


def mhz_to_period_ps(freq_mhz: float) -> int:
    """时钟频率（MHz）→ 周期（皮秒）。

    >>> mhz_to_period_ps(400)
    2500
    """
    if freq_mhz <= 0:
        raise ValueError(f"时钟频率必须为正，收到 {freq_mhz}")
    return int(round(1_000_000 / freq_mhz))


# --- 带宽 ---

def gbps_to_bytes_per_ps(gbps: float) -> float:
    """GB/s → 字节/皮秒。"""
    return gbps * 1e9 / PS_PER_S


def bytes_per_ps_to_gbps(bpp: float) -> float:
    """字节/皮秒 → GB/s。"""
    return bpp * PS_PER_S / 1e9


def bus_peak_gbps(width_bytes: int, freq_mhz: float, ddr: bool = True) -> float:
    """总线的理论峰值带宽（GB/s）。

    这是瀑布图的 100% 基准，**不是设计目标**——架构决策必须用 sustained BW。

    >>> bus_peak_gbps(8, 400)   # 64bit @ 400MHz
    6.4
    """
    multiplier = 2 if ddr else 1
    return width_bytes * freq_mhz * 1e6 * multiplier / 1e9
