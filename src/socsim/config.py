"""配置模型（pydantic）。

用 pydantic 而不是手写 dict 访问，唯一原因是**配置写错时要给出好的报错**。架构探索会
反复改 YAML，一个拼错的字段名如果被静默忽略，会让人得出错误结论还以为模型有问题。

配置结构（见 ``configs/reference_ap.yaml``）::

    run:          仿真控制（时长、种子、时间桶）
    clocks:       时钟域
    envelopes:    存储 IP 性能包络（可由厂商数据手册直接填）
    interconnect: 互联与仲裁
    slaves:       从设备
    masters:      主设备与流量
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .axi.transaction import TrafficClass
from .kernel.clock import ClockDomain
from .units import mhz_to_period_ps, ns, us

# --- 字符串 → 数值的宽容转换（YAML 里地址常写成 0x...）---


def _to_int(v: Any) -> int:
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip().replace("_", "")
        return int(s, 0)  # 自动识别 0x / 0b / 0o / 十进制
    if isinstance(v, float):
        return int(v)
    raise TypeError(f"无法转成整数：{v!r}")


class RunConfig(BaseModel):
    name: str = "baseline"
    duration_us: float = 200.0
    """仿真窗口长度。µs 级逐交易；ms 级需要靠事件跳跃优化（Stage 2）。"""
    warmup_us: float = 0.0
    """预热期：只推进不统计，让系统进入稳态。**不设预热会污染延迟分位数。**"""
    seed: int = 12345
    bucket_ns: float = 1000.0
    """带宽时序的时间桶宽度。"""
    max_events: int = 50_000_000
    """事件数上限，防活锁把机器跑死。"""

    @property
    def duration_ps(self) -> int:
        return us(self.duration_us)

    @property
    def warmup_ps(self) -> int:
        return us(self.warmup_us)

    @property
    def bucket_ps(self) -> int:
        return ns(self.bucket_ns)


class ClockConfig(BaseModel):
    freq_mhz: float
    width_b: int = 8
    """数据位宽（字节）。64bit = 8 字节。"""

    @field_validator("width_b")
    @classmethod
    def _check_width(cls, v: int) -> int:
        if v not in (1, 2, 4, 8, 16, 32, 64):
            raise ValueError(f"位宽 {v} 字节不合理。常见：4(32bit) / 8(64bit) / 16(128bit)")
        return v

    def to_domain(self, name: str) -> ClockDomain:
        return ClockDomain(name, self.freq_mhz)


class EnvelopeConfig(BaseModel):
    """存储 IP 性能包络。字段直接对应厂商数据手册。"""

    name: str
    peak_gbps: float
    sustained_rd_gbps: float
    sustained_wr_gbps: float
    latency_idle_ns: float
    max_outstanding_rd: int = 16
    max_outstanding_wr: int = 16
    rd_wr_turnaround_ns: float = 0.0
    cmd_overhead_ns: float = 0.0
    refresh_overhead: float = 0.0
    max_burst_bytes: int = 256
    data_width_b: int = 8
    latency_vs_outstanding: list[dict[str, float]] = Field(default_factory=list)

    # --- 调度行为（写缓冲水位滞回）---
    direction_batching: bool = True
    lookahead_depth: int = 32
    write_buffer_bytes: int = 4096
    write_drain_high_frac: float = 0.75
    write_drain_low_frac: float = 0.25
    min_batch_bursts: int = 4
    write_starve_ns: float = 10000.0
    read_starve_ns: float = 4000.0

    notes: str = ""

    def to_envelope(self):
        from .slaves.envelope import MemoryEnvelope

        curve = tuple(
            (int(p["ot"]), float(p["lat_ns"])) for p in self.latency_vs_outstanding
        )
        return MemoryEnvelope(
            name=self.name,
            peak_gbps=self.peak_gbps,
            sustained_rd_gbps=self.sustained_rd_gbps,
            sustained_wr_gbps=self.sustained_wr_gbps,
            latency_idle_ns=self.latency_idle_ns,
            max_outstanding_rd=self.max_outstanding_rd,
            max_outstanding_wr=self.max_outstanding_wr,
            rd_wr_turnaround_ns=self.rd_wr_turnaround_ns,
            cmd_overhead_ns=self.cmd_overhead_ns,
            refresh_overhead=self.refresh_overhead,
            max_burst_bytes=self.max_burst_bytes,
            data_width_b=self.data_width_b,
            latency_vs_outstanding=curve,
            direction_batching=self.direction_batching,
            lookahead_depth=self.lookahead_depth,
            write_buffer_bytes=self.write_buffer_bytes,
            write_drain_high_frac=self.write_drain_high_frac,
            write_drain_low_frac=self.write_drain_low_frac,
            min_batch_bursts=self.min_batch_bursts,
            write_starve_ns=self.write_starve_ns,
            read_starve_ns=self.read_starve_ns,
            notes=self.notes,
        )


class HardMacSlaveConfig(BaseModel):
    kind: Literal["hardmac"] = "hardmac"
    envelope: str
    """引用 ``envelopes`` 里的名字。"""
    queue_depth: int = 16
    """读队列（AR 通道缓冲）深度。"""
    write_queue_depth: int | None = None
    """写队列（AW 通道缓冲）深度。None 表示与 ``queue_depth`` 相同。"""
    base_addr: Any = 0
    size_bytes: Any = 1 << 28

    _int_base = field_validator("base_addr", "size_bytes", mode="before")(_to_int)


class SramSlaveConfig(BaseModel):
    kind: Literal["sram"] = "sram"
    n_banks: int = 8
    interleave_bytes: int = 256
    bank_gbps: float = 3.2
    access_latency_ns: float = 12.0
    queue_depth: int = 16
    ports: Literal["1r1w", "1rw"] = "1r1w"
    lookahead_depth: int = 8
    base_addr: Any = 0
    size_bytes: Any = 1 << 24

    _int_base = field_validator("base_addr", "size_bytes", mode="before")(_to_int)


SlaveConfig = HardMacSlaveConfig | SramSlaveConfig


class TrafficConfigModel(BaseModel):
    traffic_class: Literal["latency", "isochronous", "bandwidth", "best_effort"] = "bandwidth"
    enabled: bool = True
    """关闭这个主设备。**要停掉一路流量用这个，不要靠把速率设成 0**——
    ``target_gbps: 0`` 的语义是"饱和源"，两者很容易混淆。"""
    target_gbps: float = 0.0
    """目标带宽。**0 只在 ``pattern: saturating`` 时合法**：两者意思一致，都是
    "尽快灌满"。非 saturating 模式下给出 0 会在配置校验阶段被拒绝——静默当成饱和源
    会让"我明明设了 0 却跑出满带宽"这种困惑发生。"""
    pattern: Literal["periodic", "poisson", "bursty", "saturating"] = "poisson"
    rd_ratio: float = 0.5
    burst_len: int = 7
    burst_size: int = 3
    addr_mode: Literal["sequential", "strided", "random", "hotspot"] = "sequential"
    stride_bytes: int = 0
    hotspot_ratio: float = 0.8
    targets: dict[str, float] = Field(default_factory=dict)
    base_addr: Any = 0
    size_bytes: Any = 1 << 24
    frame_period_us: float = 0.0
    max_latency_ns: float = 0.0
    qos: int = 0

    _int_base = field_validator("base_addr", "size_bytes", mode="before")(_to_int)

    @field_validator("rd_ratio")
    @classmethod
    def _check_ratio(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"rd_ratio 必须在 [0,1]，收到 {v}")
        return v

    @field_validator("qos")
    @classmethod
    def _check_qos(cls, v: int) -> int:
        if not 0 <= v <= 15:
            raise ValueError(f"AxQOS 是 4 位，必须在 0~15，收到 {v}")
        return v

    @field_validator("burst_len")
    @classmethod
    def _check_len(cls, v: int) -> int:
        if not 0 <= v <= 255:
            raise ValueError(f"AxLEN 必须在 0~255（AXI4），收到 {v}")
        return v

    @model_validator(mode="after")
    def _check_rate_semantics(self) -> "TrafficConfigModel":
        """``target_gbps: 0`` 只允许搭配 ``pattern: saturating``。

        这个歧义会直接导致误判：使用者以为"速率设 0 就等于关掉"，实际得到的是饱和源，
        跑出一根满带宽的曲线，然后去追一个不存在的问题。宁可在校验阶段报错。
        """
        if self.target_gbps <= 0 and self.pattern != "saturating":
            raise ValueError(
                f"target_gbps={self.target_gbps} 但 pattern={self.pattern!r}。"
                f"速率为 0 只在 pattern='saturating' 时合法（两者都表示尽快灌满）。"
                f"要关闭这个主设备请用 enabled: false。"
            )
        return self

    def to_traffic_config(self, name: str):
        from .masters.master import TrafficConfig

        cls_map = {
            "latency": TrafficClass.LATENCY,
            "isochronous": TrafficClass.ISOCHRONOUS,
            "bandwidth": TrafficClass.BANDWIDTH,
            "best_effort": TrafficClass.BEST_EFFORT,
        }
        return TrafficConfig(
            name=name,
            traffic_class=cls_map[self.traffic_class],
            enabled=self.enabled,
            target_gbps=self.target_gbps,
            pattern=self.pattern,
            rd_ratio=self.rd_ratio,
            burst_len=self.burst_len,
            burst_size=self.burst_size,
            addr_mode=self.addr_mode,
            stride_bytes=self.stride_bytes,
            hotspot_ratio=self.hotspot_ratio,
            targets=dict(self.targets),
            frame_period_ps=us(self.frame_period_us) if self.frame_period_us > 0 else 0,
            max_latency_ns=self.max_latency_ns,
            qos=self.qos,
        )


class MasterConfig(BaseModel):
    port: TrafficConfigModel
    queue_depth: int = 8
    max_outstanding: int = 16
    """★ PSRAM 系统的头号旋钮。Little's Law：有效带宽 ≈ 在途字节数 ÷ 延迟。"""
    n_ids: int = 4
    """AXI ID 数量。同 ID 必须保序，所以 ID 数直接决定并发能力与队头阻塞范围。"""


class InterconnectConfig(BaseModel):
    name: str = "xbar"
    policy: Literal["drr", "rr", "wrr", "prio"] = "drr"
    """**默认 drr**。rr 给的是每事务公平，在 AXI 上是错的语义。"""
    arb_latency_cycles: float = 2
    clock: str = "axi_hp"
    max_burst_bytes: int = 256
    weights: dict[str, int] = Field(default_factory=dict)
    priorities: dict[str, int] = Field(default_factory=dict)
    aging_ns: float = 0.0
    """优先级老化阈值。0 表示关闭。**没有老化的固定优先级会让低优先级永久饿死。**"""


class SocConfig(BaseModel):
    """整份配置。"""

    run: RunConfig = Field(default_factory=RunConfig)
    clocks: dict[str, ClockConfig] = Field(default_factory=dict)
    envelopes: dict[str, EnvelopeConfig] = Field(default_factory=dict)
    interconnect: InterconnectConfig = Field(default_factory=InterconnectConfig)
    slaves: dict[str, SlaveConfig] = Field(default_factory=dict)
    masters: dict[str, MasterConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_cross_refs(self) -> "SocConfig":
        # 硬核从设备引用的包络必须存在
        for name, s in self.slaves.items():
            if isinstance(s, HardMacSlaveConfig) and s.envelope not in self.envelopes:
                raise ValueError(
                    f"从设备 {name!r} 引用了不存在的包络 {s.envelope!r}。"
                    f"已定义：{sorted(self.envelopes)}"
                )
        # 主设备的 targets 必须指向存在的从设备
        for name, m in self.masters.items():
            for t in m.port.targets:
                if t not in self.slaves:
                    raise ValueError(
                        f"主设备 {name!r} 的目标 {t!r} 不是已定义的从设备。"
                        f"已定义：{sorted(self.slaves)}"
                    )
        # 仲裁权重/优先级只能引用已定义的主设备
        for key, tbl in (("weights", self.interconnect.weights),
                         ("priorities", self.interconnect.priorities)):
            for m in tbl:
                if m not in self.masters:
                    raise ValueError(
                        f"interconnect.{key} 引用了不存在的主设备 {m!r}。"
                        f"已定义：{sorted(self.masters)}"
                    )
        # 时钟引用
        if self.interconnect.clock not in self.clocks:
            raise ValueError(
                f"interconnect.clock={self.interconnect.clock!r} 未定义。"
                f"已定义：{sorted(self.clocks)}"
            )
        return self

    @property
    def arb_latency_ps(self) -> int:
        clk = self.clocks[self.interconnect.clock]
        period = mhz_to_period_ps(clk.freq_mhz)
        n = int(self.interconnect.arb_latency_cycles)
        if self.interconnect.arb_latency_cycles > n:
            n += 1
        return n * period


def load_config(path: str) -> SocConfig:
    """从 YAML 读配置并校验。"""
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} 的顶层必须是一个映射（dict）")
    return SocConfig.model_validate(raw)


def dump_schema(path: str) -> None:
    """导出 JSON Schema，让 YAML 编辑器能做补全和校验。"""
    import json

    schema = SocConfig.model_json_schema()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(schema, f, indent=2, ensure_ascii=False)
