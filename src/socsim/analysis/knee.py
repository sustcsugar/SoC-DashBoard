"""双拐点分类器。

**这是本工具最核心的诊断能力。**

带宽-延迟曲线在某个负载之后会"拐"上去。但拐点有两种，成因和修复方式完全不同：

======================  ================================  ==========================
                        **并发受限拐点**                    **器件受限拐点**
======================  ================================  ==========================
成因                     在途请求打满 outstanding / MSHR    总线或存储核心真饱和
总线状态                 **存在空转**（没事务可发）          接近 100% 占用
延迟表现                 上升但带宽平台化                    上升且带宽触顶
修复                     **改配置**：加 outstanding、        **只能换器件或重流片**
                        加 ID、加深缓冲、加大写缓冲
代价                     面积 / 延迟                        立项决策
======================  ================================  ==========================

研究里的原话：「报告『你在拐点上』却不区分这两者的工具是误导性的——前者便宜可移，
后者是重新流片。」

判别依据是**总线空闲率**：
- 空闲率高 → 总线在等，上游没喂够 → 并发受限
- 空闲率≈0 且总线占用≈100% → 器件受限

这比单看"带宽上不去了"可靠得多，因为两者在带宽曲线上长得一模一样。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..slaves.base import SlaveDevice
from ..slaves.hardmac import HardMacSlave
from ..slaves.sram import BankedSramSlave
from ..units import PS_PER_S

#: 判据阈值。这些是工程默认值，不是定律——报告里会连同实测值一起给出，
#: 让读者能自己判断分类是否成立。
BUS_IDLE_FRAC_THRESHOLD = 0.05
"""总线空闲率超过这个比例就判为并发受限。"""

BUS_SATURATED_FRAC = 0.95
"""总线占用率超过这个比例就判为器件受限。"""

OUTSTANDING_SATURATION = 0.85
"""主设备在途数达到上限的这个比例，就算打满了。"""

TURNAROUND_BOUND_FRAC = 0.15
"""总线占用时间里翻转占比超过这个值，就判为「翻转受限」而非「器件受限」。

两者在总线占用率上完全一样（都是 100%），但修复代价天差地别：前者调调度参数就行，
后者要重新流片。用这个比例把它们分开。
"""


@dataclass
class KneeDiagnosis:
    """一个从设备的拐点诊断。"""

    slave: str
    classification: str
    """四种之一：

    - ``concurrency_limited``：总线**有空转** —— 没事务可发（加 outstanding / 加 ID）
    - ``turnaround_bound``：总线很忙但**忙在换向**，不是在传数据（加大写缓冲 / 调水位线）
    - ``device_limited``：总线忙着传数据，真的到顶了（只能换器件或重流片）
    - ``underloaded`` / ``mixed``：未饱和或混合区间

    区分 ``turnaround_bound`` 与 ``device_limited`` 很重要——两者在"总线占用率"
    这个指标上长得一样（都是 100%），但一个改配置就能救，另一个要重新流片。
    """

    # --- 实测证据 ---
    bus_data_frac: float = 0.0
    bus_turnaround_frac: float = 0.0
    bus_idle_frac: float = 0.0
    effective_gbps: float = 0.0
    sustained_gbps: float = 0.0
    peak_gbps: float = 0.0

    evidence: list[str] = field(default_factory=list)
    remediation: list[str] = field(default_factory=list)
    cost: str = ""
    """修复代价的定性描述。这是给立项决策用的。"""

    @property
    def efficiency_vs_sustained(self) -> float:
        return self.effective_gbps / self.sustained_gbps if self.sustained_gbps > 0 else 0.0

    @property
    def bus_occupied_frac(self) -> float:
        return self.bus_data_frac + self.bus_turnaround_frac

    def to_dict(self) -> dict[str, object]:
        return {
            "slave": self.slave,
            "classification": self.classification,
            "bus_data_frac": round(self.bus_data_frac, 4),
            "bus_turnaround_frac": round(self.bus_turnaround_frac, 4),
            "bus_idle_frac": round(self.bus_idle_frac, 4),
            "bus_occupied_frac": round(self.bus_occupied_frac, 4),
            "effective_gbps": round(self.effective_gbps, 4),
            "sustained_gbps": round(self.sustained_gbps, 4),
            "peak_gbps": round(self.peak_gbps, 4),
            "efficiency_vs_sustained": round(self.efficiency_vs_sustained, 4),
            "cost": self.cost,
            "evidence": self.evidence,
            "remediation": self.remediation,
        }


@dataclass
class MasterDiagnosis:
    """一个主设备的诊断。"""

    master: str
    classification: str
    """``own_outstanding_limited`` / ``fabric_stalled`` / ``achieving_target`` /
    ``starved`` / ``unconstrained``。"""

    achieved_gbps: float
    offered_gbps: float
    outstanding_avg: float
    outstanding_limit: int
    outstanding_saturation: float
    stall_frac: float
    mean_latency_ns: float
    p99_latency_ns: float
    evidence: list[str] = field(default_factory=list)
    remediation: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "master": self.master,
            "classification": self.classification,
            "achieved_gbps": round(self.achieved_gbps, 4),
            "offered_gbps": round(self.offered_gbps, 4),
            "achievement_ratio": round(
                self.achieved_gbps / self.offered_gbps, 4
            ) if self.offered_gbps > 0 else None,
            "outstanding_avg": round(self.outstanding_avg, 3),
            "outstanding_limit": self.outstanding_limit,
            "outstanding_saturation": round(self.outstanding_saturation, 4),
            "stall_frac": round(self.stall_frac, 4),
            "mean_latency_ns": round(self.mean_latency_ns, 1),
            "p99_latency_ns": round(self.p99_latency_ns, 1),
            "evidence": self.evidence,
            "remediation": self.remediation,
        }


@dataclass
class DiagnosisReport:
    slaves: list[KneeDiagnosis] = field(default_factory=list)
    masters: list[MasterDiagnosis] = field(default_factory=list)
    headline: str = ""
    """一句话结论。报告的第一行。"""
    critical: list[str] = field(default_factory=list)
    """需要立刻关注的问题。"""

    def to_dict(self) -> dict[str, object]:
        return {
            "headline": self.headline,
            "critical": self.critical,
            "slaves": [d.to_dict() for d in self.slaves],
            "masters": [d.to_dict() for d in self.masters],
        }


def diagnose_slave(
    slave: SlaveDevice,
    window_ps: int,
    effective_gbps: float,
) -> KneeDiagnosis:
    """对一个从设备做双拐点分类。"""
    if isinstance(slave, HardMacSlave):
        env = slave.env
        peak = env.peak_gbps
        sustained = (env.sustained_rd_gbps + env.sustained_wr_gbps) / 2.0
        diag = KneeDiagnosis(
            slave=slave.name, classification="underloaded",
            effective_gbps=effective_gbps, sustained_gbps=sustained, peak_gbps=peak,
        )
        if window_ps <= 0:
            return diag
        diag.bus_data_frac = slave.bus_busy_ps / window_ps
        diag.bus_turnaround_frac = slave.turnaround_ps_total / window_ps
        diag.bus_idle_frac = slave.bus_idle_ps / window_ps
        _classify_hardmac(slave, diag)
        return diag

    if isinstance(slave, BankedSramSlave):
        peak = slave.n_banks * slave.bank_bytes_per_ps * PS_PER_S / 1e9
        diag = KneeDiagnosis(
            slave=slave.name, classification="underloaded",
            effective_gbps=effective_gbps, sustained_gbps=peak, peak_gbps=peak,
        )
        if window_ps > 0:
            # SRAM 没有共享数据总线，所以"总线空闲"这个概念不适用。
            # 用 bank 平均利用率代替：低利用率 = 访问没摊开。
            # 注意用累计忙碌时长算，不能用 bank_*_free 的绝对时刻去推——
            # 那样会得到超过 100% 的荒谬值。
            util = slave.bank_utilization(window_ps)
            diag.bus_data_frac = util
            diag.bus_idle_frac = max(0.0, 1.0 - util)
            _classify_sram(slave, diag)
        return diag

    raise TypeError(f"没有为 {type(slave).__name__} 定义拐点诊断")


def _classify_hardmac(slave: HardMacSlave, diag: KneeDiagnosis) -> None:
    occupied = diag.bus_occupied_frac
    idle = diag.bus_idle_frac
    eff = diag.efficiency_vs_sustained
    d = diag.evidence

    d.append(
        f"总线占位：数据 {diag.bus_data_frac:.1%} + 翻转 {diag.bus_turnaround_frac:.1%} "
        f"= {occupied:.1%}；空闲 {idle:.1%}"
    )
    d.append(
        f"有效/持续带宽比 {eff:.1%}（{diag.effective_gbps:.3f} / {diag.sustained_gbps:.3f} GB/s）"
    )

    if diag.effective_gbps <= 0.0:
        diag.classification = "underloaded"
        diag.cost = "无"
        d.append("没有观测到流量，无法判断拐点类型")
        return

    if idle > BUS_IDLE_FRAC_THRESHOLD:
        # ★ 总线在空等 → 并发受限
        diag.classification = "concurrency_limited"
        diag.cost = "改配置就能推——不需要改器件"
        d.append(
            f"★ **总线空转 {idle:.1%}**：有足够的时间窗可以传数据，但没有事务可发。"
            f"这是并发不足的直接证据，不是总线饱和"
        )
        bdp_rd = slave.env.bandwidth_delay_product_bytes(False)
        d.append(
            f"带宽延迟积：达到读持续带宽需在途 {bdp_rd:.0f} 字节；"
            f"包络提供 {slave.env.max_outstanding_rd} 个读槽位"
        )
        diag.remediation = [
            f"提高硬核 outstanding 能力（当前读 {slave.env.max_outstanding_rd} / "
            f"写 {slave.env.max_outstanding_wr}）——若 IP 可配，这是最直接的杠杆",
            "提高主设备的 max_outstanding 与 n_ids（ID 数决定并发能力）",
            "加深从设备输入队列与主设备端口队列",
            "检查是否有主设备在端口停滞（见主设备诊断的 own_outstanding_limited）",
        ]
    elif occupied >= BUS_SATURATED_FRAC and diag.bus_turnaround_frac > TURNAROUND_BOUND_FRAC:
        # ★ 第三种失效模式：总线很忙，但忙在换向而不是传数据。
        # 它和 device_limited 在"总线占用率"上完全一样（都是 100%），
        # 但一个改配置就能救，另一个要重新流片——必须分开报。
        diag.classification = "turnaround_bound"
        diag.cost = "改配置能救——加大批长度，把翻转代价摊薄"
        d.append(
            f"★ **总线占用 {occupied:.1%}，但其中 {diag.bus_turnaround_frac:.1%} 花在"
            f"方向切换上**，真正传数据只有 {diag.bus_data_frac:.1%}。"
            f"换向期间总线什么都传不了，所以这是纯粹的效率损失"
        )
        d.append(
            f"有效/持续带宽比只有 {eff:.1%}——器件本身还有余量，是调度把它浪费掉了"
        )
        d.append(
            f"方向连续长度 {slave.mean_direction_run():.1f} 个事务，"
            f"共切换 {slave.turnaround_events} 次"
        )
        diag.remediation = [
            f"**加大写缓冲**（当前 {slave.env.write_buffer_bytes}B）：缓冲越深，"
            f"每次排写能带走的事务越多，翻转摊得越薄",
            f"**提高排空高水位**（当前 {slave.env.write_drain_high_frac:.0%}）："
            f"让写攒得更满再排，批更长",
            f"**提高 min_batch_bursts**（当前 {slave.env.min_batch_bursts}）："
            f"强制每方向至少服务够这么多事务才允许换向。"
            f"注意代价：批越大，读的最长等待也越长——这是实时性与效率的真实取舍",
            "检查流量方向混合比：若读写比接近 1:1，翻转无法避免，只能靠批长度摊薄",
        ]
    elif occupied >= BUS_SATURATED_FRAC:
        diag.classification = "device_limited"
        diag.cost = "只能换器件或重新流片——这一条不能靠改配置解决"
        d.append(
            f"★ **总线占用 {occupied:.1%}，其中数据 {diag.bus_data_frac:.1%}、"
            f"翻转仅 {diag.bus_turnaround_frac:.1%}**：器件确实被数据打满了，"
            f"再优化调度也拿不到更多带宽"
        )
        diag.remediation = [
            "提高有效载荷：更大的 burst、更好的访问局部性",
            "若仍不够，只能换更高带宽的器件、加通道，或把部分流量迁到片上 SRAM",
        ]
    else:
        diag.classification = "mixed"
        diag.cost = "部分可优化"
        d.append(
            f"总线占用 {occupied:.1%}、空闲 {idle:.1%}——既没饱和也没饿死，"
            f"处于混合区间。可能受仲裁策略或流量突发性影响"
        )
        diag.remediation = [
            "扫描仲裁策略（drr 对照 rr / wrr / prio）看是否由仲裁决定",
            "扫描主设备 outstanding 看是否还有余量",
        ]


def _classify_sram(slave: BankedSramSlave, diag: KneeDiagnosis) -> None:
    util = diag.bus_data_frac
    d = diag.evidence
    d.append(f"bank 平均利用率 {util:.1%}（{slave.n_banks} 个 bank，交织粒度 {slave.interleave_bytes}B）")
    d.append(
        f"bank 冲突 {slave.bank_conflicts} 次，跨 bank 拆包 {slave.cross_bank_splits} 次"
    )

    if util >= 0.8:
        diag.classification = "device_limited"
        diag.cost = "bank 已接近打满——加 bank 或调交织粒度"
        diag.remediation = [
            "增加 bank 数（线性提升峰值）",
            "调整交织粒度，让访问摊得更均匀",
        ]
    elif slave.bank_conflicts > 0 and util < 0.5:
        diag.classification = "concurrency_limited"
        diag.cost = "改配置能救——bank 空闲但访问撞在同一个 bank 上"
        diag.remediation = [
            f"调整交织粒度（当前 {slave.interleave_bytes}B）：太小会跨 bank 拆包，"
            f"太大则连续访问全落同一个 bank",
            "检查主设备的访问步长是否与交织粒度形成了共振",
            "增加 bank 数",
        ]
    else:
        diag.classification = "underloaded"
        diag.cost = "无"
        d.append("bank 未被打满，该从设备不是瓶颈")


def diagnose_master(
    name: str,
    offered_gbps: float,
    achieved_gbps: float,
    outstanding_avg: float,
    outstanding_limit: int,
    stall_frac: float,
    mean_latency_ps: float,
    p99_latency_ps: float,
    arbitration_share: float | None,
) -> MasterDiagnosis:
    """对一个主设备做诊断。"""
    sat = outstanding_avg / outstanding_limit if outstanding_limit > 0 else 0.0
    diag = MasterDiagnosis(
        master=name, classification="unconstrained",
        achieved_gbps=achieved_gbps, offered_gbps=offered_gbps,
        outstanding_avg=outstanding_avg, outstanding_limit=outstanding_limit,
        outstanding_saturation=sat, stall_frac=stall_frac,
        mean_latency_ns=mean_latency_ps / 1000.0,
        p99_latency_ns=p99_latency_ps / 1000.0,
    )
    d = diag.evidence
    d.append(
        f"在途数 {outstanding_avg:.1f}/{outstanding_limit}（{sat:.0%}），"
        f"端口停滞占比 {stall_frac:.1%}，平均延迟 {diag.mean_latency_ns:.0f}ns，"
        f"p99 {diag.p99_latency_ns:.0f}ns"
    )

    meets_target = offered_gbps <= 0 or achieved_gbps >= offered_gbps * 0.95

    if meets_target:
        diag.classification = "achieving_target"
        d.append(f"达成 {achieved_gbps:.3f} / 目标 {offered_gbps:.3f} GB/s —— 目标已满足")
        return diag

    if sat >= OUTSTANDING_SATURATION:
        diag.classification = "own_outstanding_limited"
        d.append(
            f"★ 自己的 outstanding 打满（{sat:.0%}）——**加 outstanding 是最直接的杠杆**。"
            f"注意：只有当存储侧还有余量时加它才有效；若存储侧已饱和，加了也不涨"
        )
        diag.remediation = [
            f"提高 max_outstanding（当前 {outstanding_limit}）。用 Little's Law 估算需求："
            f"需要的在途字节 = 目标带宽 × 延迟",
            "提高 n_ids（ID 数决定并发能力，同 ID 必须保序）",
            "评估 MSHR / 请求缓冲深度是否也是上限",
        ]
    elif stall_frac > 0.3:
        diag.classification = "fabric_stalled"
        d.append(
            f"★ 在途数没打满（{sat:.0%}）但端口停滞 {stall_frac:.1%} —— "
            f"**瓶颈在下游**（从设备队列满或仲裁拿不到），不在自己的 outstanding"
        )
        diag.remediation = [
            "加深从设备输入队列 / 加大写缓冲",
            "检查仲裁策略是否让这个主设备拿不到足够份额",
            "若多个主设备都在停滞，考虑提高存储侧能力",
        ]
    else:
        diag.classification = "starved"
        if arbitration_share is not None:
            d.append(f"仲裁字节份额 {arbitration_share:.1%}")
        d.append(
            "既没打满 outstanding 也没明显停滞，但仍未达标——可能受仲裁份额或"
            "流量模式限制"
        )
        diag.remediation = [
            "检查 QoS 分级与仲裁权重设置",
            "检查是否被同 ID 保序约束（HOL 阻塞）限制",
        ]
    return diag


def diagnose_run(result) -> DiagnosisReport:
    """对一次完整的仿真做诊断。

    ``result`` 是 ``socsim.soc.RunResult``。这里用鸭子类型而不是 import，
    避免 analysis 层反向依赖 soc 层（会形成循环导入）。
    """
    rep = DiagnosisReport()
    T = result.t_measured_ps
    mon = result.monitor
    cfg = result.cfg

    # --- 从设备 ---
    for name, dev in result.slaves.items():
        eff = mon.bandwidth_gbps(mon.bytes_by_slave.get(name, 0), T)
        rep.slaves.append(diagnose_slave(dev, T, eff))

    # --- 主设备 ---
    arb_shares: dict[str, float] = {}
    for slave_name, arb in result.crossbar.arbiters.items():
        for m, share in arb.stats.bytes_share().items():
            arb_shares[m] = arb_shares.get(m, 0.0) + share / max(1, len(result.crossbar.arbiters))

    for name, m in result.masters.items():
        lat = mon.lat_master.get(name)
        diag = diagnose_master(
            name=name,
            offered_gbps=m.cfg.target_gbps,
            achieved_gbps=mon.bandwidth_gbps(mon.bytes_by_master.get(name, 0), T),
            outstanding_avg=mon.outstanding[name].time_avg(result.t_end_ps),
            outstanding_limit=result.ports[name].max_outstanding,
            stall_frac=m.stall_ps / T if T > 0 else 0.0,
            mean_latency_ps=lat.mean if lat and len(lat) else 0.0,
            p99_latency_ps=lat.percentile(99) if lat and len(lat) else 0.0,
            arbitration_share=arb_shares.get(name),
        )
        rep.masters.append(diag)

    _summarize(rep, result)
    return rep


def _summarize(rep: DiagnosisReport, result) -> None:
    """生成一句话结论与关键问题清单。"""
    concurrency_limited = [d for d in rep.slaves if d.classification == "concurrency_limited"]
    turnaround_bound = [d for d in rep.slaves if d.classification == "turnaround_bound"]
    device_limited = [d for d in rep.slaves if d.classification == "device_limited"]
    outstanding_limited = [d for d in rep.masters if d.classification == "own_outstanding_limited"]
    stalled = [d for d in rep.masters if d.classification == "fabric_stalled"]
    unmet = [d for d in rep.masters if d.classification not in ("achieving_target", "unconstrained")]

    parts = []
    if concurrency_limited:
        names = ", ".join(d.slave for d in concurrency_limited)
        parts.append(f"{names} 处于**并发受限**（总线空转，改配置能救）")
    if turnaround_bound:
        names = ", ".join(d.slave for d in turnaround_bound)
        parts.append(
            f"{names} 处于**翻转受限**（总线忙但在换向，加大批长度能救）"
        )
    if device_limited:
        names = ", ".join(d.slave for d in device_limited)
        parts.append(f"{names} 处于**器件受限**（总线饱和，只能换器件或重流片）")
    if not parts:
        parts.append("所有从设备都处于混合或轻载区间，未见明确的拐点特征")
    rep.headline = "；".join(parts)

    for d in concurrency_limited:
        if d.evidence:
            rep.critical.append(f"[{d.slave}] {d.evidence[-1]}")
    for d in turnaround_bound:
        rep.critical.append(
            f"[{d.slave}] 总线占用 {d.bus_occupied_frac:.0%} 里翻转占 "
            f"{d.bus_turnaround_frac:.0%}——这是可压缩的调度损失，"
            f"加大写缓冲/提高 min_batch_bursts 能换回带宽"
        )
    for d in outstanding_limited:
        rep.critical.append(
            f"[{d.master}] outstanding 打满（{d.outstanding_saturation:.0%}），"
            f"需要 {d.offered_gbps:.2f} 但只拿到 {d.achieved_gbps:.2f} GB/s"
        )
    for d in stalled:
        rep.critical.append(
            f"[{d.master}] 在途未满但端口停滞 {d.stall_frac:.0%}——瓶颈在下游"
        )
    if unmet and not rep.critical:
        rep.critical.append(
            f"{len(unmet)} 个主设备未达标：{', '.join(d.master for d in unmet)}"
        )
