"""主设备与流量生成。

流量的**开环/闭环**区别是这里最需要想清楚的一件事：

- **速率受限源**（``target_gbps > 0``）：以固定速率供给，被阻塞时达成速率自然下降。
  用来问"这个主设备需要 1.5 GB/s，它拿得到吗"。
- **饱和源**（``target_gbps = 0``）：一有空就往里灌，用来问"这个架构最多能跑多少"。

**被阻塞的源不积累"信用"**（no credit banking）。否则一个被堵住 1ms 的源恢复后会瞬间
打出 1ms 的量，制造出现实中不存在的突发。真实主设备的请求缓冲是有界的，被堵住就是被
堵住，恢复后按自己的速率继续。

等时（isochronous）源的 deadline 按**帧**计算：帧边界由帧周期对齐，deadline 是帧内
允许的最大延迟。错过 deadline 在现实里产生可见/可听伪影，不是温和的性能下降——所以
它单独统计，不混进平均延迟。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..axi.port import MasterPort
from ..axi.transaction import Access, AxiBurst, TrafficClass
from ..kernel.rng import RngPool, poisson_interval_ps
from ..kernel.scheduler import PRIO_ISSUE, Scheduler
from ..units import PS_PER_S


@dataclass
class TrafficConfig:
    """一个主设备的流量描述。"""

    name: str
    traffic_class: TrafficClass = TrafficClass.BANDWIDTH
    enabled: bool = True
    """关闭这个主设备。用于对比场景（关掉某路看别人能拿多少），比删掉配置干净。"""

    # --- 速率 ---
    target_gbps: float = 0.0
    """目标带宽。0 表示饱和源（尽快灌满）。"""

    pattern: str = "poisson"
    """``periodic`` / ``poisson`` / ``bursty`` / ``saturating``。"""

    rd_ratio: float = 0.5
    """读占比。0=全写，1=全读。"""

    # --- 突发形状 ---
    burst_len: int = 7
    """AxLEN：拍数 − 1。7 表示 8 拍。"""

    burst_size: int = 3
    """AxSIZE：log2(每拍字节数)。3 表示 8 字节/拍 → 64 字节/burst。"""

    # --- 突发性（bursty 模式）---
    on_off_ratio: float = 0.5
    """on 期占比（bursty 模式）。"""

    # --- 地址 ---
    addr_mode: str = "sequential"
    """``sequential`` / ``strided`` / ``random`` / ``hotspot``。"""

    stride_bytes: int = 0
    """``strided`` 模式的步长。0 表示用 burst 大小作为步长。"""

    hotspot_ratio: float = 0.8
    """``hotspot`` 模式中落在热点区的比例。"""

    # --- 目标从设备分布 ---
    targets: dict[str, float] = field(default_factory=dict)
    """``{从设备名: 权重}``。权重会被归一化。"""

    # --- 地址空间（按从设备）---
    base_addr: int = 0
    size_bytes: int = 1 << 24  # 16MB

    # --- 等时约束 ---
    frame_period_ps: int = 0
    """帧周期（如 60fps → 16.67ms）。0 表示非等时。"""

    max_latency_ns: float = 0.0
    """帧内允许的最大延迟。超过即判定错过 deadline。"""

    qos: int = 0
    """AxQOS 值（0~15）。**这是建议权重，不是保证。**"""


class Master:
    """一个主设备：按配置生成流量并提交到自己的端口。

    停滞恢复走**事件驱动的等待者回调**，不轮询。
    """

    def __init__(
        self,
        cfg: TrafficConfig,
        sched: Scheduler,
        port: MasterPort,
        rng_pool: RngPool,
        base_addr: int = 0,
        size_bytes: int = 1 << 24,
        queue_depth: int = 8,
    ):
        self.cfg = cfg
        self.name = cfg.name
        self.sched = sched
        self.port = port
        self.rng = rng_pool.get(f"master.{cfg.name}")
        self.monitor = None  # 由 Soc 装配时注入；用来记录 outstanding 变化

        self.base_addr = base_addr
        self.size_bytes = size_bytes
        self.end_addr = base_addr + size_bytes

        # 归一化目标分布
        if cfg.targets:
            total = sum(cfg.targets.values())
            self.targets = {k: v / total for k, v in cfg.targets.items()}
        else:
            self.targets = {}
        self._target_names = list(self.targets)
        self._target_cum = []
        acc = 0.0
        for name in self._target_names:
            acc += self.targets[name]
            self._target_cum.append(acc)

        # 地址游标（按从设备分别维护）
        self._cursor: dict[str, int] = {n: base_addr for n in self._target_names}

        # 调度状态
        self._next_t = 0
        self._pending: AxiBurst | None = None
        self._seq = 0
        self._burst_bytes = (cfg.burst_len + 1) * (1 << cfg.burst_size)
        self._interval_ps = self._compute_interval()
        self._frame_start = 0

        # 统计
        self.issued = 0
        self.issued_bytes = 0
        self.stall_ps = 0
        self._stall_at = 0
        self.deadline_misses = 0
        self.deadline_samples: list[int] = []

    # --- 速率 ---

    def _compute_interval(self) -> int:
        """平均发行间隔。饱和源返回 0。"""
        if self.cfg.target_gbps <= 0 or self.cfg.pattern == "saturating":
            return 0
        rate_bpp = self.cfg.target_gbps * 1e9 / PS_PER_S
        return max(1, int(self._burst_bytes / rate_bpp))

    # --- 生命周期 ---

    def start(self, t_ps: int = 0) -> None:
        self._next_t = t_ps
        self.sched.schedule_at(t_ps, self._tick, priority=PRIO_ISSUE)

    def _tick(self) -> None:
        now = self.sched.now

        # 有未提交成功的 burst 就先重试它——不要重新生成，否则地址会意外前进
        burst = self._pending if self._pending is not None else self._make_burst(now)
        if burst is None:
            return  # 没有可用的目标从设备

        if not self.port.submit(burst):
            if self._pending is None:
                self._pending = burst
                self._stall_at = now
            self.port.when_space(burst.slave, self._resume)
            return

        if self._pending is not None:
            self.stall_ps += now - self._stall_at
            self._pending = None

        # 提交成功 ≠ 成为 outstanding。``t_issue`` 由互联在 AR/AW 握手完成时设定
        # （见 MasterPort.take_ready）——那才是 AXI 语义上事务进入在途的时刻。
        # 这里只记录"主设备想发起"，用于量端口停滞。
        if self.monitor is not None:
            self.monitor.record_created(burst, now)

        self.issued += 1
        self.issued_bytes += burst.nbytes
        self._advance_cursor(burst)

        # 开环速率控制：被阻塞期间不积累信用（no credit banking）
        if self._interval_ps <= 0:
            self.sched.schedule(0, self._tick, priority=PRIO_ISSUE)
            return
        self._next_t += self._interval_ps
        if self._next_t < now:
            self._next_t = now  # 落后了就对齐到当下，不让积压无限增长
        self.sched.schedule_at(self._next_t, self._tick, priority=PRIO_ISSUE)

    def _resume(self) -> None:
        """端口腾出空间，立刻重试。"""
        self.sched.schedule(0, self._tick, priority=PRIO_ISSUE)

    def stop(self, t_ps: int) -> None:
        if self._pending is not None and self._stall_at > 0:
            self.stall_ps += t_ps - self._stall_at

    # --- 生成 ---

    def _pick_slave(self) -> str | None:
        if not self._target_names:
            return None
        if len(self._target_names) == 1:
            return self._target_names[0]
        r = self.rng.random()
        for name, cum in zip(self._target_names, self._target_cum):
            if r <= cum:
                return name
        return self._target_names[-1]

    def _make_burst(self, t_ps: int) -> AxiBurst | None:
        slave = self._pick_slave()
        if slave is None:
            return None

        is_read = self.rng.random() < self.cfg.rd_ratio
        addr = self._align(self._cursor.get(slave, self.base_addr), self._burst_bytes)

        self._seq += 1
        deadline = 0
        if self.cfg.frame_period_ps > 0 and self.cfg.max_latency_ns > 0:
            # 帧对齐：deadline 是当前帧起点 + 帧内容许的最大延迟
            frame_start = (t_ps // self.cfg.frame_period_ps) * self.cfg.frame_period_ps
            deadline = frame_start + int(self.cfg.max_latency_ns * 1000)

        return AxiBurst(
            seq=self._seq,
            master=self.name,
            slave=slave,
            addr=addr,
            length=self.cfg.burst_len,
            size=self.cfg.burst_size,
            access=Access.READ if is_read else Access.WRITE,
            qos=self.cfg.qos,
            traffic_class=self.cfg.traffic_class,
            deadline_ps=deadline,
            t_issue=t_ps,
        )

    def _advance_cursor(self, burst: AxiBurst) -> None:
        mode = self.cfg.addr_mode
        size = self.size_bytes
        cur = self._cursor.get(burst.slave, self.base_addr)

        if mode == "random":
            cur = self.base_addr + self.rng.randrange(0, max(1, size - self._burst_bytes))
        elif mode == "hotspot":
            hotspot_end = self.base_addr + int(size * 0.1)
            if self.rng.random() < self.cfg.hotspot_ratio:
                cur = self.base_addr + self.rng.randrange(
                    0, max(1, hotspot_end - self.base_addr - self._burst_bytes)
                )
            else:
                cur = self.base_addr + self.rng.randrange(0, max(1, size - self._burst_bytes))
        elif mode == "strided":
            step = self.cfg.stride_bytes or self._burst_bytes
            cur += step
        else:  # sequential
            cur += self._burst_bytes

        # 回绕
        if cur + self._burst_bytes > self.base_addr + size:
            cur = self.base_addr
        self._cursor[burst.slave] = cur

    @staticmethod
    def _align(addr: int, size: int) -> int:
        return (addr // size) * size

    # --- 统计 ---

    @property
    def offered_gbps(self) -> float:
        """配置的目标带宽。"""
        return self.cfg.target_gbps

    def record_complete(self, burst: AxiBurst) -> None:
        """事务完成回调。等时源在这里统计 deadline 是否被满足。"""
        if burst.deadline_ps > 0:
            self.deadline_samples.append(burst.deadline_slack_ps)
            if burst.deadline_missed:
                self.deadline_misses += 1

    def summary(self, t_end_ps: int) -> dict[str, object]:
        span_s = t_end_ps / PS_PER_S if t_end_ps > 0 else 0.0
        achieved = (self.issued_bytes / span_s / 1e9) if span_s > 0 else 0.0
        out: dict[str, object] = {
            "name": self.name,
            "traffic_class": self.cfg.traffic_class.label,
            "pattern": self.cfg.pattern,
            "offered_gbps": self.cfg.target_gbps,
            "achieved_gbps": round(achieved, 4),
            "issued": self.issued,
            "issued_bytes": self.issued_bytes,
            "stall_ps": self.stall_ps,
            "stall_ratio": round(self.stall_ps / t_end_ps, 4) if t_end_ps > 0 else 0.0,
        }
        if self.cfg.frame_period_ps > 0:
            out["deadline_misses"] = self.deadline_misses
            out["deadline_samples"] = len(self.deadline_samples)
            if self.deadline_samples:
                s = sorted(self.deadline_samples)
                out["slack_min_ps"] = s[0]
                out["slack_p01_ps"] = s[max(0, int(len(s) * 0.01))]
                out["slack_p50_ps"] = s[len(s) // 2]
        return out
