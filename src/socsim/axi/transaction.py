"""AXI 事务（burst）。

一个 ``AxiBurst`` 同时是**描述符**（地址、长度、ID、QoS）和**时序记录载体**
（各阶段的时刻戳）。把时刻戳挂在事务对象上而不是分散到各处，是为了让延迟归因可以
事后逐段拆解——"这个事务的时间花在哪了"是拥塞分析的基本问题。

延迟的分段方式（对应 AXI 的实际过程）::

    t_issue ──┬── 主设备端口排队（等 outstanding 槽位）──┬─ t_grant
              │                                          │
              │                                          ├── 从设备侧排队 ──┬─ t_service_start
              │                                          │                  │
              │                                          │                  ├── 服务时间 ──── t_done
              └────────────── 端到端延迟 ────────────────┴──────────────────┘

``wait_master_ps`` + ``wait_slave_ps`` + ``service_ps`` == ``latency_ps``
这个恒等式在测试里要断言成立。
"""

from __future__ import annotations

from enum import IntEnum


class Access(IntEnum):
    """访问方向。值直接用于统计分层，不要随意改动。"""

    READ = 0
    WRITE = 1

    @property
    def label(self) -> str:
        return "rd" if self is Access.READ else "wr"


class TrafficClass(IntEnum):
    """流量类别。

    这不是"优先级"，而对应三种**契约**（见 glossary 第 7 节）：

    - ``LATENCY``：最小延迟契约。CPU 类，延迟就是它的性能指标。
    - ``ISOCHRONOUS``：最大延迟契约。显示/摄像头/音频类，有硬 deadline，
      需要延迟**上界**而非平均带宽。
    - ``BANDWIDTH``：最小带宽契约。GPU/NPU/DMA 类，容忍延迟，靠深 outstanding
      提前跑。

    优先级只是实现契约的手段，不是目的。混用优先级和契约会让 QoS 设计失去依据。
    """

    LATENCY = 0
    ISOCHRONOUS = 1
    BANDWIDTH = 2
    BEST_EFFORT = 3

    @property
    def label(self) -> str:
        return {
            TrafficClass.LATENCY: "lat",
            TrafficClass.ISOCHRONOUS: "iso",
            TrafficClass.BANDWIDTH: "bw",
            TrafficClass.BEST_EFFORT: "be",
        }[self]


class AxiBurst:
    """一个 AXI burst 事务。

    ``length`` 与 ``size`` 遵循 AXI 语义：拍数 = ``length + 1``，
    每拍字节数 = ``2 ** size``。AXI4 的 ``length`` 范围是 0~255。
    """

    __slots__ = (
        "seq", "master", "slave", "addr", "length", "size", "access",
        "axi_id", "qos", "traffic_class", "deadline_ps",
        "t_created", "t_issue", "t_grant", "t_service_start", "t_done",
        "granted_by", "blocked_by", "slot",
    )

    def __init__(
        self,
        seq: int,
        master: str,
        slave: str,
        addr: int,
        length: int,
        size: int,
        access: Access,
        axi_id: int = 0,
        qos: int = 0,
        traffic_class: TrafficClass = TrafficClass.BEST_EFFORT,
        deadline_ps: int = 0,
        t_issue: int = 0,
    ):
        if length < 0 or length > 255:
            raise ValueError(f"AxLEN 必须在 0~255，收到 {length}")
        if size < 0 or size > 7:
            raise ValueError(f"AxSIZE 必须在 0~7（1~128 字节/拍），收到 {size}")
        self.seq = seq
        self.master = master
        self.slave = slave
        self.addr = addr
        self.length = length
        self.size = size
        self.access = access
        self.axi_id = axi_id
        self.qos = qos
        self.traffic_class = traffic_class
        self.deadline_ps = deadline_ps

        # 时序记录
        #
        # ★ ``t_created`` 与 ``t_issue`` 必须区分，这是 Little's Law 能闭合的关键：
        #
        # - ``t_created``：主设备**想**发起这个事务的时刻。用来量主设备被端口挡住多久。
        # - ``t_issue``：端口**接受**这个事务的时刻（AR/AW 握手完成）。AXI 语义上，
        #   这一刻它才成为 "outstanding"。
        #
        # 延迟与在途数必须用**同一个起点**，否则会话两侧对不上：如果延迟从 t_created
        # 算、而在途数从 t_issue 开始计，那段"卡在主设备端口"的时间只进了延迟没进并发，
        # Little's Law 就永远差一截，而且**误差与 outstanding 压力成正比**——很容易被
        # 误读成模型的物理误差。
        self.t_created = t_issue
        self.t_issue = t_issue
        self.t_grant = 0
        self.t_service_start = 0
        self.t_done = 0

        # 归因：谁阻塞了这个事务、它占用了哪个缓冲槽
        self.granted_by = ""
        self.blocked_by = ""
        self.slot = -1

    # --- 几何 ---

    @property
    def beats(self) -> int:
        return self.length + 1

    @property
    def beat_bytes(self) -> int:
        return 1 << self.size

    @property
    def nbytes(self) -> int:
        """事务的总数据字节数。统计带宽必须用它，不能用事务数。"""
        return self.beats * self.beat_bytes

    @property
    def last_addr(self) -> int:
        return self.addr + self.nbytes - 1

    # --- 延迟分解 ---

    @property
    def latency_ps(self) -> int:
        """端到端延迟：从端口接受请求（AR/AW 握手完成）到事务完成。

        **起点是 ``t_issue`` 而不是 ``t_created``**——这样它与 outstanding 的计数
        区间严格一致，Little's Law 才成立。主设备被端口挡住的时长另计在
        ``stall_ps`` 和 ``created_to_done_ps`` 里。
        """
        return self.t_done - self.t_issue

    @property
    def created_to_done_ps(self) -> int:
        """从"主设备想发起"到完成的总时长。包含主设备端口停滞。

        这是主设备**实际体验**到的延迟，比 ``latency_ps`` 更长。实时性分析要用这个，
        因为它包含了"想发但发不出去"的时间。
        """
        return self.t_done - self.t_created

    @property
    def port_stall_ps(self) -> int:
        """在主设备端口等待被接受的时长。"""
        return self.t_issue - self.t_created

    @property
    def wait_master_ps(self) -> int:
        """在主设备端口排队的时间（等 outstanding 槽位 + 等仲裁）。"""
        return self.t_grant - self.t_issue

    @property
    def wait_slave_ps(self) -> int:
        """进入从设备端口后的排队时间。"""
        return self.t_service_start - self.t_grant

    @property
    def service_ps(self) -> int:
        """被从设备服务的时间（数据传输 + 固有延迟）。"""
        return self.t_done - self.t_service_start

    def latency_breakdown(self) -> dict[str, int]:
        return {
            "wait_master": self.wait_master_ps,
            "wait_slave": self.wait_slave_ps,
            "service": self.service_ps,
            "total": self.latency_ps,
        }

    # --- 等时契约 ---

    @property
    def deadline_missed(self) -> bool:
        """是否错过截止时间。仅对 isochronous 类有意义。"""
        return self.deadline_ps > 0 and self.t_done > self.deadline_ps

    @property
    def deadline_slack_ps(self) -> int:
        """距截止时间的余量。负值表示已经错过。"""
        if self.deadline_ps <= 0:
            return 0
        return self.deadline_ps - self.t_done

    def __repr__(self) -> str:
        return (
            f"Burst#{self.seq} {self.master}->{self.slave} "
            f"{self.access.label} addr=0x{self.addr:x} {self.beats}x{self.beat_bytes}B "
            f"id={self.axi_id} lat={self.latency_ps}ps"
        )


def burst_bytes(length: int, size: int) -> int:
    """不构造对象就算出字节数，供配置校验使用。"""
    return (length + 1) * (1 << size)


def bytes_to_bursts(nbytes: int, length: int, size: int) -> int:
    """给定总字节数，需要多少个该几何形状的 burst。"""
    per = burst_bytes(length, size)
    return -(-nbytes // per)
