"""仲裁器。

**正确性要点：默认必须是 DRR，不是 RR。**

简单轮询给的是**每事务公平**。当一个主设备发 16 拍 burst、另一个发 1 拍 single 时，
burst 那一路每字节只拿到 1/16 的仲裁机会。AXI 的 burst 长度可变，所以"每事务公平"
在 AXI 上是错的语义。这是很多自研模型里隐藏的 bug。

DRR（Deficit Round Robin，Shreedhar & Varghese 1995）给的是**每字节公平**，
且对变长包是 O(1) 复杂度。

各种仲裁器的适用场景：

===================  ==================  ==========================================
仲裁器                公平性语义           适用
===================  ==================  ==========================================
``RoundRobin``        每事务              仅当所有主设备 burst 几何形状相同时才对
``WeightedRR``        每事务 × 权重        带宽分层，但仍有每事务公平的缺陷
``DeficitRR``         每字节 ★             **默认选择**
``FixedPriority``     无（会饿死）         仅在有 strict 实时要求且流量可预测时
===================  ==================  ==========================================
"""

from __future__ import annotations

from typing import Mapping

from ..axi.transaction import AxiBurst


class ArbiterStats:
    """仲裁行为统计。公平性与饿死分析的原始数据。"""

    __slots__ = ("grants", "granted_bytes", "last_grant_t", "waits")

    def __init__(self, flows: list[str]):
        self.grants: dict[str, int] = {f: 0 for f in flows}
        self.granted_bytes: dict[str, int] = {f: 0 for f in flows}
        self.last_grant_t: dict[str, int] = {f: -1 for f in flows}
        self.waits: dict[str, int] = {f: 0 for f in flows}

    def record(self, flow: str, burst: AxiBurst, t_ps: int) -> None:
        self.grants[flow] = self.grants.get(flow, 0) + 1
        self.granted_bytes[flow] = self.granted_bytes.get(flow, 0) + burst.nbytes
        self.last_grant_t[flow] = t_ps
        burst.granted_by = flow

    def note_wait(self, flows: list[str]) -> None:
        for f in flows:
            self.waits[f] = self.waits.get(f, 0) + 1

    def starvation_gap_ps(self, flow: str, t_end: int) -> int:
        """距该流上次获得授权的时长。用于饿死检测。"""
        last = self.last_grant_t.get(flow, -1)
        if last < 0:
            return t_end
        return t_end - last

    def bytes_share(self) -> dict[str, float]:
        """各流拿到的字节占比。这是**带宽公平**，不是停顿时间公平。"""
        total = sum(self.granted_bytes.values())
        if total == 0:
            return {f: 0.0 for f in self.granted_bytes}
        return {f: b / total for f, b in self.granted_bytes.items()}


class Arbiter:
    """仲裁器基类。

    ``select`` 接收本轮的候选（``flow -> 队头 burst``），返回获胜的流名或 None。
    """

    name = "base"

    def __init__(self, flows: list[str]):
        self.flows = list(flows)
        self.stats = ArbiterStats(self.flows)

    def select(self, heads: Mapping[str, AxiBurst], t_ps: int) -> str | None:
        raise NotImplementedError

    def _pick(self, heads: Mapping[str, AxiBurst], flow: str, t_ps: int) -> str:
        self.stats.record(flow, heads[flow], t_ps)
        return flow

    def starvation_report(self, t_end: int, threshold_ps: int) -> list[dict[str, object]]:
        """返回超过阈值的饿死记录。空列表表示没有流被饿死。

        ★ **只有参与过竞争的流才能被判定为饿死。** 一个从不访问这个从设备的主设备
        （``waits == 0`` 且零授权）没有"被饿死"——它根本不需要这个资源。
        少了这条过滤，报告会给每个从设备都列出一串假的饿死告警，把真正的信号淹掉。
        """
        out = []
        for f in self.flows:
            if self.stats.waits.get(f, 0) == 0 and self.stats.grants.get(f, 0) == 0:
                continue  # 从未参与竞争，不是利益相关方
            gap = self.stats.starvation_gap_ps(f, t_end)
            if gap > threshold_ps:
                out.append({
                    "flow": f,
                    "gap_ps": gap,
                    "grants": self.stats.grants.get(f, 0),
                    "granted_bytes": self.stats.granted_bytes.get(f, 0),
                    "waits": self.stats.waits.get(f, 0),
                })
        out.sort(key=lambda r: -r["gap_ps"])  # type: ignore[arg-type]
        return out


class RoundRobin(Arbiter):
    """每事务轮询。

    ⚠️ **在 AXI 上语义是错的**（除非所有主设备的 burst 字节数相同）。保留它是为了
    在报告里做对照——"把 DRR 换成 RR 会损失多少"本身就是一个有价值的架构问题。
    """

    name = "rr"

    def __init__(self, flows: list[str]):
        super().__init__(flows)
        self._ptr = 0

    def select(self, heads: Mapping[str, AxiBurst], t_ps: int) -> str | None:
        if not heads:
            return None
        self.stats.note_wait(list(heads))
        n = len(self.flows)
        for i in range(n):
            flow = self.flows[(self._ptr + i) % n]
            if flow in heads:
                self._ptr = (self._ptr + i + 1) % n
                return self._pick(heads, flow, t_ps)
        return None


class WeightedRR(Arbiter):
    """加权轮询（每事务公平 × 权重）。

    权重是**事务数**的配额，不是字节数——所以它和 RR 有同样的每字节不公平问题，
    只是有了分层能力。
    """

    name = "wrr"

    def __init__(self, flows: list[str], weights: Mapping[str, int] | None = None):
        super().__init__(flows)
        self.weights = {f: max(1, (weights or {}).get(f, 1)) for f in flows}
        self._credit = {f: 0 for f in flows}
        self._ptr = 0

    def select(self, heads: Mapping[str, AxiBurst], t_ps: int) -> str | None:
        if not heads:
            return None
        self.stats.note_wait(list(heads))
        n = len(self.flows)
        # 每流按权重补充信用，轮询找第一个有信用且有请求的流
        for _ in range(n * 2):
            flow = self.flows[self._ptr]
            self._ptr = (self._ptr + 1) % n
            if flow not in heads:
                self._credit[flow] = 0
                continue
            if self._credit[flow] == 0:
                self._credit[flow] = self.weights[flow]
            self._credit[flow] -= 1
            return self._pick(heads, flow, t_ps)
        return None


class DeficitRR(Arbiter):
    """差额轮询：**每字节公平**。本项目的默认仲裁器。

    每个流维护一个差额计数器（deficit）。每轮给活跃流补充配额（quantum），
    队头包能装进差额里才发；发完扣掉包的字节数。余额跨轮保留，所以长 burst
    的流不会被短 burst 的流系统性占便宜。

    参数约束：``quantum`` 必须 ≥ 最大的单包字节数，否则一个包可能永远装不进差额
    （经典 DRR 的已知前提）。构造时校验，不满足直接报错——静默退化会给出错误结论。
    """

    name = "drr"

    def __init__(
        self,
        flows: list[str],
        max_packet_bytes: int,
        weights: Mapping[str, int] | None = None,
        quantum_bytes: int | None = None,
    ):
        super().__init__(flows)
        self.weights = {f: max(1, (weights or {}).get(f, 1)) for f in flows}
        self.max_packet_bytes = max_packet_bytes
        # 默认配额 = 权重 × 最大包字节数，保证单轮内总能让一个包通过
        base = quantum_bytes if quantum_bytes is not None else max_packet_bytes
        if base < max_packet_bytes:
            raise ValueError(
                f"DRR 配额 {base}B 小于最大包 {max_packet_bytes}B，"
                f"会导致包永远装不进差额。请提高 quantum 或检查 burst 几何配置。"
            )
        self.quanta = {f: base * self.weights[f] for f in flows}
        self._deficit = {f: 0 for f in flows}
        self._ptr = 0

    def select(self, heads: Mapping[str, AxiBurst], t_ps: int) -> str | None:
        """选下一个流。

        ★ **DRR 的语义是"每流每轮获得 quantum 字节的配额"**，不是"每流每轮发一个包"。

        这个区别对 AXI 是决定性的：一个流发 8 字节包、另一个发 256 字节包、配额都是
        256 字节时，
        - 正确实现：小包流一轮内可发 32 个包（共 256B），大包流发 1 个（256B）→ 字节 50/50
        - 错误实现（每流每轮一个包）：小包流 8B、大包流 256B → 字节份额 3%/97%，
          这正是「每事务公平」的结果，也就是 DRR 本该修掉的那个问题

        实现要点：指针在**每个包之后**前移；配额不足的流本轮跳过（不消耗其配额）；
        全部流配额都不够时开新一轮统一补配额。
        """
        if not heads:
            return None
        self.stats.note_wait(list(heads))
        n = len(self.flows)

        # 最多两轮：第一轮用剩余配额，第二轮补充配额后重试
        for _round in range(2):
            for _ in range(n):
                flow = self.flows[self._ptr]
                if flow not in heads:
                    # 空闲流清零，防止长期不活动后突然爆发
                    self._deficit[flow] = 0
                    self._ptr = (self._ptr + 1) % n
                    continue
                need = heads[flow].nbytes
                if self._deficit[flow] >= need:
                    self._deficit[flow] -= need
                    self._ptr = (self._ptr + 1) % n
                    return self._pick(heads, flow, t_ps)
                # 配额不够 → 本轮让给下一个流
                self._ptr = (self._ptr + 1) % n
            # 有货的流配额都不够 → 开新一轮
            for f in heads:
                self._deficit[f] += self.quanta[f]

        # 兜底：只有在 quantum < 包大小时才可能走到这里，构造时已禁止。
        # 保守处理，避免仿真卡死。
        flow = max(heads, key=lambda f: self._deficit[f])
        self._deficit[flow] -= heads[flow].nbytes
        self._ptr = (self.flows.index(flow) + 1) % n
        return self._pick(heads, flow, t_ps)


class FixedPriority(Arbiter):
    """固定优先级 + 可选老化。

    没有老化的固定优先级会让低优先级流**永久饿死**——这不是"性能差一点"，是功能性
    缺陷。``aging_ps`` 到期后把该流的有效优先级提升到最高，保证等待上界。

    优先级数值约定（对齐 Arm 的实践）：**CPU 放 12~14，不要放 15**。留出 15 给实时
    主设备在濒临饿死时升级使用；把 CPU 放在 15 会堵死实时保护机制。
    """

    name = "prio"

    def __init__(
        self,
        flows: list[str],
        priorities: Mapping[str, int] | None = None,
        aging_ps: int = 0,
    ):
        super().__init__(flows)
        self.priorities = {f: int((priorities or {}).get(f, 0)) for f in flows}
        self.aging_ps = aging_ps

    def select(self, heads: Mapping[str, AxiBurst], t_ps: int) -> str | None:
        if not heads:
            return None
        self.stats.note_wait(list(heads))

        best_flow = None
        best_key = None
        for flow in heads:
            prio = self.priorities.get(flow, 0)
            # AxQOS 参与：4 位值越大约高。这是"建议权重"，不是保证——
            # 真正的保证来自这里的仲裁逻辑 + 主设备侧的入口调节器。
            prio = prio * 16 + heads[flow].qos
            aged = False
            if self.aging_ps > 0:
                gap = self.stats.starvation_gap_ps(flow, t_ps)
                if gap >= self.aging_ps:
                    prio = 1 << 30  # 老化到最高
                    aged = True
            key = (0 if aged else 1, -prio, flow)
            if best_key is None or key < best_key:
                best_key, best_flow = key, flow

        assert best_flow is not None
        return self._pick(heads, best_flow, t_ps)


def make_arbiter(
    policy: str,
    flows: list[str],
    max_packet_bytes: int,
    weights: Mapping[str, int] | None = None,
    priorities: Mapping[str, int] | None = None,
    aging_ps: int = 0,
) -> Arbiter:
    """按配置构造仲裁器。"""
    p = policy.lower()
    if p in ("drr", "deficit_round_robin"):
        return DeficitRR(flows, max_packet_bytes, weights)
    if p in ("rr", "round_robin"):
        return RoundRobin(flows)
    if p in ("wrr", "weighted_round_robin"):
        return WeightedRR(flows, weights)
    if p in ("prio", "fixed_priority", "priority"):
        return FixedPriority(flows, priorities, aging_ps)
    raise ValueError(
        f"未知仲裁策略 {policy!r}。可选：drr / rr / wrr / prio"
    )
