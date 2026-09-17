"""时钟域。

仿真内部时间单位是皮秒（见 ``units`` 模块）。时钟域的作用是**把传输时长对齐到时钟
边沿**——现实中数据只在时钟沿推进，一次 3 拍的传输不会因为"3.7 拍就够了"而只花 3.7 拍。

不对齐的后果是模型会高估带宽：连续传输的零头会累积成凭空的收益。对齐之后，
``n`` 拍的传输严格占 ``n × period`` 皮秒。

跨时钟域（CDC）用固定的同步器延迟建模（典型 2~3 个目的时钟周期），这是现实里无法
消除的成本，也是"为什么不要把太多东西挂在不同时钟域上"的定量依据。
"""

from __future__ import annotations

from ..units import PS_PER_NS, mhz_to_period_ps


class ClockDomain:
    """一个时钟域。

    >>> clk = ClockDomain("axi_hp", 400)
    >>> clk.period_ps
    2500
    >>> clk.align_up(3000)      # 下一个上升沿
    5000
    >>> clk.cycles_ps(3)        # 3 拍占多久
    7500
    """

    __slots__ = ("name", "freq_mhz", "period_ps")

    def __init__(self, name: str, freq_mhz: float):
        self.name = name
        self.freq_mhz = freq_mhz
        self.period_ps = mhz_to_period_ps(freq_mhz)

    def cycles_ps(self, n_cycles: float) -> int:
        """``n`` 个周期对应的皮秒数（向上取整到整周期）。

        >>> ClockDomain("c", 400).cycles_ps(2.1)
        7500
        """
        whole = int(n_cycles)
        if n_cycles > whole:
            whole += 1
        return whole * self.period_ps

    def to_cycles(self, duration_ps: int) -> int:
        """时长折算成周期数（向上取整）——传输至少占这么多拍。"""
        if duration_ps <= 0:
            return 0
        return -(-duration_ps // self.period_ps)  # 向上取整

    def align_up(self, time_ps: int) -> int:
        """对齐到下一个（含当前的）上升沿。"""
        p = self.period_ps
        return -(-time_ps // p) * p

    def align_down(self, time_ps: int) -> int:
        """对齐到当前或上一个上升沿。"""
        return (time_ps // self.period_ps) * self.period_ps

    def __repr__(self) -> str:
        return f"ClockDomain({self.name!r}, {self.freq_mhz}MHz, {self.period_ps}ps)"


def cdc_latency_ps(src: ClockDomain, dst: ClockDomain, sync_stages: int = 2) -> int:
    """跨时钟域的同步延迟。

    典型的两级同步器 = 目的域的 2 个周期。这不是可以忽略的成本：在 200MHz 的目的域上
    就是 10ns，与 PSRAM 空载延迟同量级。

    同一时钟域内返回 0。
    """
    if src.period_ps == dst.period_ps:
        return 0
    return dst.cycles_ps(sync_stages)


def describe_frequency_ratio(a: ClockDomain, b: ClockDomain) -> str:
    """人类可读的频率比，用于报告。"""
    from math import gcd

    g = gcd(a.period_ps, b.period_ps)
    return (
        f"{a.name}:{b.name} = {b.period_ps // g}:{a.period_ps // g}"
        f"  ({a.freq_mhz}MHz : {b.freq_mhz}MHz)"
    )


# 常用频率的便捷构造（便于在配置和测试里引用）
def clk_100() -> ClockDomain:
    return ClockDomain("100MHz", 100)


def clk_200() -> ClockDomain:
    return ClockDomain("200MHz", 200)


def clk_400() -> ClockDomain:
    return ClockDomain("400MHz", 400)


def clk_800() -> ClockDomain:
    return ClockDomain("800MHz", 800)


__all__ = [
    "ClockDomain",
    "cdc_latency_ps",
    "describe_frequency_ratio",
    "clk_100",
    "clk_200",
    "clk_400",
    "clk_800",
    "PS_PER_NS",
]
