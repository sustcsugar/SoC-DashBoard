"""SoC 装配与运行。

把配置变成模型、接线、跑起来、收结果。

**装配阶段会做几项主动检查并产出警告**（见 ``_diagnose``）。一个架构工具如果只在
"结果里"体现问题，使用者很容易把模型假设造成的现象当成硬件行为。所以凡是会导致
结论失真的前置条件，装配时就报出来。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .axi.port import MasterPort
from .axi.transaction import AxiBurst
from .config import SocConfig
from .interconnect.crossbar import Crossbar
from .kernel.rng import RngPool
from .kernel.scheduler import Scheduler, Stopwatch
from .masters.master import Master
from .monitor.monitor import Monitor
from .slaves.base import SlaveDevice
from .slaves.hardmac import HardMacSlave
from .slaves.sram import BankedSramSlave
from .units import PS_PER_S


@dataclass
class RunResult:
    """一次仿真的全部产出。"""

    cfg: SocConfig
    t_end_ps: int
    """仿真结束时刻。"""
    t_measured_ps: int
    """测量窗口长度（总时长 − 预热）。所有速率的分母。"""
    wall_time_s: float
    events: int
    monitor: Monitor
    slaves: dict[str, SlaveDevice]
    ports: dict[str, MasterPort]
    masters: dict[str, Master]
    crossbar: Crossbar
    scheduler: Scheduler
    inflight_at_warmup_bytes: int = 0
    """预热结束时刻的在途字节数。守恒校验的基准。"""
    inflight_now_bytes: int = 0
    """仿真结束时刻的在途字节数。"""
    warnings: list[str] = field(default_factory=list)

    @property
    def events_per_second(self) -> float:
        return self.events / self.wall_time_s if self.wall_time_s > 0 else 0.0

    @property
    def conservation(self) -> dict[str, object]:
        """★ 三重自校验之一：守恒校验。

        恒等式：``发起 − 完成 == 当前在途 − 预热时在途``

        为什么要减预热时的在途量：预热会清零统计计数器，但当时**已经发出、尚未完成**
        的事务不在清零范围内——它们完成后会正常计入 ``completed_bytes``。不记下基准，
        预热就会让守恒恒等式永远差一个固定量，看起来像丢包。

        不成立意味着丢包、重复计数或时间戳错误——最严重的模型 bug。
        """
        residual = self.monitor.conservation_residual
        expected = self.inflight_now_bytes - self.inflight_at_warmup_bytes
        return {
            "ok": residual == expected,
            "issued_bytes": self.monitor.issued_bytes,
            "completed_bytes": self.monitor.completed_bytes,
            "residual_bytes": residual,
            "inflight_now_bytes": self.inflight_now_bytes,
            "inflight_at_warmup_bytes": self.inflight_at_warmup_bytes,
            "expected_residual_bytes": expected,
            "mismatch_bytes": residual - expected,
        }

    def little_law(self) -> dict[str, dict[str, float]]:
        """★ 三重自校验之一：Little's Law 校验。"""
        return self.monitor.little_law_check(self.t_end_ps, self.t_measured_ps)

    def little_law_worst_error_pct(self) -> float:
        return self.monitor.little_law_worst_error_pct(self.t_end_ps, self.t_measured_ps)

    def bandwidth_check(self) -> list[str]:
        """★ 三重自校验之一：上界校验。达成带宽不得超过从设备峰值。"""
        caps: dict[str, float] = {}
        for name, sc in self.cfg.slaves.items():
            if sc.kind == "hardmac":
                caps[name] = self.cfg.envelopes[sc.envelope].peak_gbps
        return self.monitor.upper_bound_check(caps, self.t_measured_ps)


class Soc:
    """配置驱动的 SoC 仿真。"""

    def __init__(self, cfg: SocConfig):
        self.cfg = cfg
        self.sched = Scheduler(max_events=cfg.run.max_events)
        self.rng = RngPool(cfg.run.seed)
        self.clocks = {n: c.to_domain(n) for n, c in cfg.clocks.items()}
        self.warnings: list[str] = []

        self.slaves: dict[str, SlaveDevice] = {}
        self.ports: dict[str, MasterPort] = {}
        self.masters: dict[str, Master] = {}
        self.monitor: Monitor | None = None
        self.crossbar: Crossbar | None = None
        self._inflight_at_warmup = 0
        """预热结束时刻的在途字节数。

        守恒校验要用它：预热会清零统计计数器，但当时**已经发出、尚未完成**的事务不在
        清零范围内——它们完成后会正常计入 ``completed_bytes``。所以恒等式是

            发起 − 完成 == 当前在途 − 预热时在途

        不记下这一项，预热就会让守恒校验永远差一个固定量，看起来像丢包。
        """

        self._build()

    # --- 装配 ---

    def _build(self) -> None:
        cfg = self.cfg

        self.monitor = Monitor(
            master_names=list(cfg.masters),
            slave_names=list(cfg.slaves),
            bucket_ps=cfg.run.bucket_ps,
        )

        self._build_slaves()
        self._build_ports_and_crossbar()
        self._build_masters()
        self._diagnose()

    def _build_slaves(self) -> None:
        cfg = self.cfg
        for name, sc in cfg.slaves.items():
            if sc.kind == "hardmac":
                env = cfg.envelopes[sc.envelope].to_envelope()
                dev = HardMacSlave(
                    name=name,
                    sched=self.sched,
                    envelope=env,
                    queue_depth=sc.queue_depth,
                    on_complete=self._on_slave_complete,
                    address_range=(int(sc.base_addr), int(sc.size_bytes)),
                    write_queue_depth=sc.write_queue_depth,
                )
            elif sc.kind == "sram":
                dev = BankedSramSlave(
                    name=name,
                    sched=self.sched,
                    n_banks=sc.n_banks,
                    interleave_bytes=sc.interleave_bytes,
                    bank_gbps=sc.bank_gbps,
                    access_latency_ns=sc.access_latency_ns,
                    queue_depth=sc.queue_depth,
                    on_complete=self._on_slave_complete,
                    ports=sc.ports,
                    lookahead_depth=sc.lookahead_depth,
                    base_addr=int(sc.base_addr),
                    size_bytes=int(sc.size_bytes),
                )
            else:  # pragma: no cover - pydantic 已限定
                raise ValueError(f"未知从设备类型 {sc.kind!r}")
            self.slaves[name] = dev

    def _build_ports_and_crossbar(self) -> None:
        cfg = self.cfg
        slave_names = list(cfg.slaves)
        for name, mc in cfg.masters.items():
            self.ports[name] = MasterPort(
                name=name,
                slaves=slave_names,
                queue_depth=mc.queue_depth,
                max_outstanding=mc.max_outstanding,
                n_ids=mc.n_ids,
            )

        ic = cfg.interconnect
        self.crossbar = Crossbar(
            name=ic.name,
            sched=self.sched,
            master_ports=list(self.ports.values()),
            slave_devices=self.slaves,
            policy=ic.policy,
            arb_latency_ps=cfg.arb_latency_ps,
            max_packet_bytes=ic.max_burst_bytes,
            weights=ic.weights,
            priorities=ic.priorities,
            aging_ps=int(ic.aging_ns * 1000),
        )

        # 端口提交成功后通知交叉开关触发仲裁
        for port in self.ports.values():
            port.on_submit = self._on_port_submit
            port.on_grant = self._on_port_grant

    def _on_port_submit(self, burst: AxiBurst) -> None:
        assert self.crossbar is not None
        self.crossbar.notify(burst.slave)

    def _on_port_grant(self, burst: AxiBurst, t_ps: int) -> None:
        """AR/AW 握手完成 —— 事务此刻成为 outstanding，并发计数在这里加一。"""
        assert self.monitor is not None
        self.monitor.record_outstanding(burst, t_ps)

    def _build_masters(self) -> None:
        cfg = self.cfg
        for name, mc in cfg.masters.items():
            tc = mc.port.to_traffic_config(name)
            m = Master(
                cfg=tc,
                sched=self.sched,
                port=self.ports[name],
                rng_pool=self.rng,
                base_addr=int(mc.port.base_addr),
                size_bytes=int(mc.port.size_bytes),
                queue_depth=mc.queue_depth,
            )
            m.monitor = self.monitor
            self.masters[name] = m

    # --- 完成回调链 ---

    def _on_slave_complete(self, burst: AxiBurst) -> None:
        """从设备完成的统一出口。释放资源 + 记指标。

        时刻用 ``burst.t_done``（排程时算出的权威完成时刻），而不是 ``sched.now``。
        正常两者相等；但若仿真在事务完成前被截断，用 t_done 能保持延迟统计自洽。
        """
        t = burst.t_done
        port = self.ports.get(burst.master)
        if port is not None:
            port.complete(burst)
        assert self.monitor is not None
        slave = self.slaves.get(burst.slave)
        qdepth = sum(slave.queue_snapshot().values()) if slave is not None else 0
        self.monitor.record_complete(burst, t, queue_depth=qdepth)
        m = self.masters.get(burst.master)
        if m is not None:
            m.record_complete(burst)

    # --- 运行 ---

    def _inflight_bytes(self) -> int:
        """当前在途（已过 AR/AW 握手、尚未完成）的字节数。

        **口径必须与 ``issued_bytes`` / ``completed_bytes`` 严格一致**：那两个计数器
        只在握手完成时加、只在完成事件里减，所以这里只能数 ``port.inflight``。

        不能把 ``port.ready`` 里的请求算进来——那些还没被计数过，算进来会让守恒
        恒等式多出一个"排队中字节数"的固定偏差。

        也不用再遍历从设备队列：``take_ready`` 时事务就进了 ``inflight``，从设备队列
        里的内容已被覆盖。
        """
        return sum(b.nbytes for port in self.ports.values() for b in port.inflight.values())

    def run(self) -> RunResult:
        cfg = self.cfg
        assert self.monitor is not None and self.crossbar is not None

        warmup = cfg.run.warmup_ps
        duration = cfg.run.duration_ps

        # 主设备在 t=0 全部启动（enabled: false 的跳过）
        for m in self.masters.values():
            if m.cfg.enabled:
                m.start(0)

        # 预热：只推进不统计。不计入事件吞吐与墙钟——否则"事件/秒"这个
        # 用于验证内核性能的数字会被预热期的启动瞬态污染。
        if warmup > 0:
            self.sched.run(warmup)
            self._inflight_at_warmup = self._inflight_bytes()
            self.monitor.reset_stats(warmup)
            for dev in self.slaves.values():
                dev.reset_stats(warmup)
            # 交叉开关计数器与从设备的 bytes_accepted 是配对口径，只在一边清零会让
            # "授权字节数 == 受理字节数"这条不变量在预热场景下永远不成立
            if self.crossbar is not None:
                self.crossbar.reset_stats(warmup)
            for port in self.ports.values():
                port.reset_stats(warmup)
            for m in self.masters.values():
                m.issued = 0
                m.issued_bytes = 0
                m.stall_ps = 0
                m.deadline_misses = 0
                m.deadline_samples.clear()

        events_before = self.sched.stats["fired"]
        sw = Stopwatch().start()
        self.sched.run(warmup + duration)
        sw.stop()
        events = self.sched.stats["fired"] - events_before

        t_end = self.sched.now
        measured = max(1, t_end - warmup)
        inflight_now = self._inflight_bytes()

        # 收尾：把仍停在队列里的源的停滞时间记下来
        for m in self.masters.values():
            m.stop(t_end)

        return RunResult(
            cfg=cfg,
            t_end_ps=t_end,
            t_measured_ps=measured,
            wall_time_s=sw.elapsed,
            events=events,
            monitor=self.monitor,
            slaves=self.slaves,
            ports=self.ports,
            masters=self.masters,
            crossbar=self.crossbar,
            scheduler=self.sched,
            inflight_at_warmup_bytes=self._inflight_at_warmup,
            inflight_now_bytes=inflight_now,
            warnings=list(self.warnings),
        )

    # --- 装配期诊断 ---

    def _diagnose(self) -> None:
        """把会导致结论失真的前置条件在装配阶段就报出来。

        这些都是**方法论的坑**，不是模型 bug。如果不报，使用者很容易把模型假设造成
        的现象当作硬件行为去优化。
        """
        cfg = self.cfg

        # 1) 非平稳流量：帧同步源是周期性的，套 Jackson 网络会出错
        framed = [n for n, m in cfg.masters.items() if m.port.frame_period_us > 0]
        if framed:
            self.warnings.append(
                f"主设备 {framed} 是帧同步（周期性非平稳）流量。"
                f"不要对它们套 Jackson 网络等要求平稳性的解析模型——"
                f"用确定性样本路径版本的 Little's Law，或直接看仿真结果。"
            )

        # 2) 并发受限：outstanding 不足时，永远达不到 sustained 带宽
        for name, sc in cfg.slaves.items():
            if sc.kind != "hardmac":
                continue
            env = cfg.envelopes[sc.envelope].to_envelope()
            # 用最大的主设备 burst 几何做最保守估计
            max_burst = 0
            for mc in cfg.masters.values():
                b = (mc.port.burst_len + 1) * (1 << mc.port.burst_size)
                max_burst = max(max_burst, b)
            if max_burst == 0:
                continue
            for is_wr in (False, True):
                diag = env.headroom_vs_outstanding(max_burst, is_wr)
                if diag["concurrency_limited"]:
                    self.warnings.append(
                        f"[{name}/{diag['direction']}] **并发受限**：达到 sustained 带宽需"
                        f"{diag['outstanding_required']:.1f} 个 {max_burst}B 事务在途，"
                        f"但包络只提供 {diag['outstanding_available']} 个。"
                        f"该方向的有效带宽会被 Little's Law 封顶在约 "
                        f"{diag['concurrency_ratio'] * 100:.0f}% 的 sustained 值，"
                        f"与引脚带宽无关。——这也说明瓶颈是「改配置能救」的一类。"
                    )

        # 3) 仲裁语义：rr 在 burst 几何不一致时是错的
        if cfg.interconnect.policy == "rr":
            geoms = {(mc.port.burst_len, mc.port.burst_size) for mc in cfg.masters.values()}
            if len(geoms) > 1:
                self.warnings.append(
                    "仲裁策略是 rr（每事务公平），但各主设备的 burst 几何形状不同 "
                    f"（共 {len(geoms)} 种）。**每事务公平在 AXI 上是错的语义**——"
                    "发长 burst 的主设备每字节拿到的仲裁机会更少。建议改用 drr。"
                )

        # 4) 饱和源数量：多个饱和源会让结果由仲裁策略主导
        saturating = [
            n for n, m in cfg.masters.items()
            if m.port.enabled and (
                m.port.pattern == "saturating" or m.port.target_gbps <= 0
            )
        ]
        if len(saturating) > 2:
            self.warnings.append(
                f"有 {len(saturating)} 个饱和源 {saturating}。"
                f"多个饱和源并存时，达成带宽几乎完全由仲裁策略和 outstanding 决定，"
                f"而不是由各自的「需求」决定——解读结果时要注意这一点。"
            )

        # 5) 总供给超过总容量：先看是否物理上不可能
        total_offered = sum(
            m.port.target_gbps for m in cfg.masters.values() if m.port.enabled
        )
        total_capacity = sum(
            cfg.envelopes[s.envelope].sustained_rd_gbps
            for s in cfg.slaves.values() if s.kind == "hardmac"
        )
        if total_capacity > 0 and total_offered > total_capacity * 1.2:
            self.warnings.append(
                f"所有主设备的目标带宽之和 {total_offered:.2f} GB/s 超过了存储侧持续带宽 "
                f"{total_capacity:.2f} GB/s。系统必然达不到全部目标——"
                f"这是预期行为，用来观察「谁被牺牲」。"
            )
