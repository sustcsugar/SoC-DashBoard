"""存储 IP 的性能包络。

**这是有硬核 IP 时唯一正确的建模层次。** vendor 提供 controller + PHY，内部不可改，
所以建模它的 AXI 侧可观测特性，而不是内部 bank/row 时序。厂商数据手册给的正是这些量。

为什么要有这个抽象，而不是给 PSRAM 写一个专用类：

1. 硬核不可改 → 建模内部没有决策价值。真正有决策价值的是系统侧（outstanding 配置、
   仲裁、ID 分配、流量调度），那些作为变量去扫描。
2. 换一颗 PSRAM、或者将来加 DDR，只是换一份包络参数，模型结构不变。
3. 它把"厂商给的数据"和"我们的架构选择"明确分开——这个界限在评审时很重要。

**关键区分：延迟 ≠ 服务时间。**

- **延迟**（``latency``）：命令发出到数据开始返回。它**可以被流水掩盖**——多个事务
  同时在途时，各自的延迟重叠。
- **服务时间**（``service``）：数据占用总线的时长。它**不可掩盖**，是串行的物理资源。

把这两者混为一谈，就会得出"延迟高所以带宽低"的错误结论。真实关系是：
**延迟高本身不降低带宽，延迟高加上 outstanding 不足才降低带宽**（Little's Law）。
这正是本工具要区分的东西。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..units import PS_PER_S, ns


@dataclass(frozen=True)
class MemoryEnvelope:
    """存储硬核的 AXI 侧性能包络。

    单位约定：配置里用 GB/s 和 ns（贴合手册），内部算出字节/皮秒和皮秒。
    """

    name: str

    # --- 带宽 ---
    peak_gbps: float
    """理论峰值。**只是瀑布图的 100% 基准，不是设计目标。**"""

    sustained_rd_gbps: float
    """纯读持续带宽。扣掉所有固有开销后实际可达。"""

    sustained_wr_gbps: float
    """纯写持续带宽。通常略低于读（写还要等响应）。"""

    # --- 延迟 ---
    latency_idle_ns: float
    """空载往返延迟。注意：这是**可被流水掩盖**的量。"""

    # --- 并发 ---
    max_outstanding_rd: int
    """最大在读事务数。**PSRAM 系统里决定有效带宽的头号旋钮。**"""

    max_outstanding_wr: int
    """最大在写事务数。"""

    # --- 开销 ---
    rd_wr_turnaround_ns: float = 0.0
    """读写方向切换惩罚。共享双向数据总线上这是最大的一类可控损耗。
    同 rank 内写→读惩罚最大，跨 rank 几乎无惩罚——所以这个值应填**同一目标**的数字。"""

    cmd_overhead_ns: float = 0.0
    """命令/地址相位开销。每事务一次，短 burst 上占比更高。"""

    refresh_overhead: float = 0.0
    """刷新/维护的直接开销比例（0~1），如 0.017 表示 1.7%。
    注意隐藏代价（刷新后所有 bank 关闭）不在此项，它体现在延迟上。"""

    # --- 几何约束 ---
    max_burst_bytes: int = 256
    data_width_b: int = 64

    # --- 可选：实测延迟曲线 ---
    latency_vs_outstanding: tuple[tuple[int, float], ...] = ()
    """``((outstanding, latency_ns), ...)``。厂商提供实测曲线时填这里，
    模型会线性插值，比单一空载延迟准确得多。空则退化为恒定的空载延迟。"""

    # --- 行为选项 ---
    direction_batching: bool = True
    """是否启用读写方向批处理。真实控制器一定会做（写缓冲攒批）；
    关掉它会让翻转损耗被严重高估，只能作为对照实验。"""

    lookahead_depth: int = 32
    """命令调度器一次能看到多少个待处理事务。

    真实控制器的命令调度器每周期扫描**整个**待处理队列（FR-FCFS 就是这么工作的），
    所以这个值应当覆盖队列深度。调小它等于人为限制调度器的视野。"""

    write_buffer_bytes: int = 4096
    """写缓冲容量。写请求先落进这里，攒够一批再排到总线上。

    **写缓冲是批处理的来源。** 没有它，每个到来的写请求都会被立即服务，方向切换
    就会发生在每个事务上，翻转开销会吃掉大部分带宽。"""

    write_drain_high_frac: float = 0.75
    """写缓冲占用率超过这个比例就开始排空。"""

    write_drain_low_frac: float = 0.25
    """排空到这个比例以下就切回读方向。

    高低水位之间的**滞回**是关键：没有它，方向会在水位附近反复横跳，
    每次横跳都付一次翻转代价。"""

    min_batch_bursts: int = 4
    """★ **换向前至少要服务多少个事务。**

    这是真实控制器必有的参数，也是本模型里最关键的一个调优点。没有它，
    "读饥饿保护"会把批处理彻底吃掉：满载时读队列总是有积压（最老的读可能已经等了
    十几微秒），于是"有读在等"这个条件**永远为真**，每完成一个写就切回读，
    方向连续长度塌到 2，翻转开销吃掉 40% 的带宽。

    它体现的是一个真实的架构取舍：

    - 批越大 → 翻转代价摊得越薄 → 总线效率高
    - 批越大 → 读的等待上限越长 → 实时性变差

    所以这个值应该由**读延迟预算**反推，而不是拍脑袋。工具的价值就在于把这条取舍
    量化出来。"""

    write_starve_ns: float = 10000.0
    """写请求最长等待。超过就强制切到写方向，防止读流量永不间断时写被无限期推迟。

    写不着急（发起方丢进缓冲就走了），所以这个阈值可以放得很宽。"""

    read_starve_ns: float = 4000.0
    """读请求最长等待的**硬上限**。读是延迟敏感的（发起方在等数据），

    注意它是硬上限而非常规触发条件：正常情况下应该由水位线和 ``min_batch_bursts``
    决定换向时机。只有读到这个时限还没被服务，才强制打断当前批。

    设得太小会让批处理失效（见 ``min_batch_bursts`` 的说明）。"""

    notes: str = field(default="", compare=False)

    # --- 派生量（构造后计算，避免在热路径里做除法）---

    def __post_init__(self) -> None:
        if self.sustained_rd_gbps > self.peak_gbps:
            raise ValueError(
                f"[{self.name}] 读持续带宽 {self.sustained_rd_gbps} GB/s "
                f"超过了峰值 {self.peak_gbps} GB/s。请检查手册数据。"
            )
        if self.sustained_wr_gbps > self.peak_gbps:
            raise ValueError(
                f"[{self.name}] 写持续带宽 {self.sustained_wr_gbps} GB/s "
                f"超过了峰值 {self.peak_gbps} GB/s。"
            )
        if self.max_outstanding_rd < 1 or self.max_outstanding_wr < 1:
            raise ValueError(f"[{self.name}] outstanding 必须 ≥1")
        if not 0.0 <= self.refresh_overhead < 1.0:
            raise ValueError(f"[{self.name}] refresh_overhead 必须在 [0,1)")
        if not 0.0 < self.write_drain_low_frac < self.write_drain_high_frac <= 1.0:
            raise ValueError(
                f"[{self.name}] 水位线必须满足 0 < low < high <= 1，"
                f"收到 low={self.write_drain_low_frac} high={self.write_drain_high_frac}。"
                f"写缓冲的滞回区间是防止方向反复横跳的关键。"
            )

    @property
    def write_drain_high_bytes(self) -> int:
        return int(self.write_buffer_bytes * self.write_drain_high_frac)

    @property
    def write_drain_low_bytes(self) -> int:
        return int(self.write_buffer_bytes * self.write_drain_low_frac)

    # --- 速率 ---

    @property
    def rd_bytes_per_ps(self) -> float:
        return self.sustained_rd_gbps * 1e9 / PS_PER_S

    @property
    def wr_bytes_per_ps(self) -> float:
        return self.sustained_wr_gbps * 1e9 / PS_PER_S

    @property
    def peak_bytes_per_ps(self) -> float:
        return self.peak_gbps * 1e9 / PS_PER_S

    def bytes_per_ps(self, is_write: bool) -> float:
        return self.wr_bytes_per_ps if is_write else self.rd_bytes_per_ps

    def data_ps(self, nbytes: int, is_write: bool) -> int:
        """一个事务的数据占线时长。这是**不可重叠的串行资源**。"""
        rate = self.bytes_per_ps(is_write)
        if rate <= 0:
            raise ValueError(f"[{self.name}] 带宽为 0，无法服务")
        return int(nbytes / rate)

    # --- 延迟 ---

    def latency_ps(self, outstanding: int) -> int:
        """给定在途事务数下的延迟。

        有实测曲线时线性插值；否则返回空载延迟。注意返回的是**可被掩盖的延迟**，
        不是服务时间。
        """
        if not self.latency_vs_outstanding:
            return ns(self.latency_idle_ns)
        pts = sorted(self.latency_vs_outstanding, key=lambda p: p[0])
        if outstanding <= pts[0][0]:
            return ns(pts[0][1])
        if outstanding >= pts[-1][0]:
            return ns(pts[-1][1])
        for (o0, l0), (o1, l1) in zip(pts, pts[1:]):
            if o0 <= outstanding <= o1:
                if o1 == o0:
                    return ns(l1)
                frac = (outstanding - o0) / (o1 - o0)
                return ns(l0 + frac * (l1 - l0))
        return ns(self.latency_idle_ns)

    # --- 诊断 ---

    @property
    def turnaround_ps(self) -> int:
        return ns(self.rd_wr_turnaround_ns)

    @property
    def read_starve_ps(self) -> int:
        return ns(self.read_starve_ns)

    @property
    def write_starve_ps(self) -> int:
        return ns(self.write_starve_ns)

    @property
    def cmd_ps(self) -> int:
        return ns(self.cmd_overhead_ns)

    def max_outstanding(self, is_write: bool) -> int:
        return self.max_outstanding_wr if is_write else self.max_outstanding_rd

    def bandwidth_delay_product_bytes(self, is_write: bool = False) -> float:
        """带宽延迟积：**达到 sustained 带宽所需的最小在途字节数**。

        这是定 outstanding 容量的下限依据。若包络的 outstanding 上限 × 平均 burst
        字节数小于这个值，那么**永远达不到 sustained 带宽**——瓶颈在并发，不在带宽。

        这是 PSRAM 系统最容易被误诊的一种情况，所以单独做成一个显式方法。
        """
        return self.bytes_per_ps(is_write) * self.latency_ps(1)

    def required_outstanding(self, burst_bytes: int, is_write: bool = False) -> float:
        """达到 sustained 带宽所需的最小 outstanding 事务数。"""
        per = max(1, burst_bytes)
        return self.bandwidth_delay_product_bytes(is_write) / per

    def headroom_vs_outstanding(self, burst_bytes: int, is_write: bool = False) -> dict:
        """并发能力的诊断摘要。报告里直接用。"""
        need = self.required_outstanding(burst_bytes, is_write)
        have = self.max_outstanding(is_write)
        return {
            "name": self.name,
            "direction": "wr" if is_write else "rd",
            "burst_bytes": burst_bytes,
            "bdp_bytes": self.bandwidth_delay_product_bytes(is_write),
            "outstanding_required": need,
            "outstanding_available": have,
            "concurrency_ratio": have / need if need > 0 else float("inf"),
            "concurrency_limited": have < need,
        }

    def summary(self) -> dict[str, object]:
        return {
            "name": self.name,
            "peak_gbps": self.peak_gbps,
            "sustained_rd_gbps": self.sustained_rd_gbps,
            "sustained_wr_gbps": self.sustained_wr_gbps,
            "latency_idle_ns": self.latency_idle_ns,
            "max_outstanding_rd": self.max_outstanding_rd,
            "max_outstanding_wr": self.max_outstanding_wr,
            "rd_wr_turnaround_ns": self.rd_wr_turnaround_ns,
            "bdp_rd_bytes": round(self.bandwidth_delay_product_bytes(False), 1),
            "bdp_wr_bytes": round(self.bandwidth_delay_product_bytes(True), 1),
        }


# --- 一批典型包络，用于打通流程和做敏感性分析 ---
#
# ⚠️ 这些是**占位值**，取自公开数据手册的典型量级。
# 真实决策必须换成厂商给的数字。见 ADR-0000 第 1.4 节。

def psram_xccela_placeholder() -> MemoryEnvelope:
    """x8 DDR @ 266MHz 的单颗 PSRAM 量级（占位）。"""
    return MemoryEnvelope(
        name="psram_xccela_placeholder",
        peak_gbps=4.256,          # 8bit × 266MHz × 2 = 4.256 Gb/s
        sustained_rd_gbps=3.20,
        sustained_wr_gbps=2.60,
        latency_idle_ns=180.0,
        max_outstanding_rd=16,
        max_outstanding_wr=16,
        rd_wr_turnaround_ns=120.0,
        cmd_overhead_ns=15.0,
        refresh_overhead=0.017,
        max_burst_bytes=256,
        data_width_b=8,
        latency_vs_outstanding=(
            (1, 180.0), (2, 190.0), (4, 215.0), (8, 260.0), (16, 340.0),
        ),
        notes="占位值：公开手册典型量级，仅用于打通流程，不可用于真实决策",
    )


def psram_conservative_placeholder() -> MemoryEnvelope:
    """更保守的 PSRAM 配置，用于对比（占位）。"""
    return MemoryEnvelope(
        name="psram_conservative_placeholder",
        peak_gbps=4.256,
        sustained_rd_gbps=2.60,
        sustained_wr_gbps=2.10,
        latency_idle_ns=220.0,
        max_outstanding_rd=8,
        max_outstanding_wr=8,
        rd_wr_turnaround_ns=150.0,
        cmd_overhead_ns=20.0,
        refresh_overhead=0.017,
        max_burst_bytes=128,
        data_width_b=8,
        notes="占位值：更少 outstanding、更高延迟，用于观察并发受限拐点",
    )
