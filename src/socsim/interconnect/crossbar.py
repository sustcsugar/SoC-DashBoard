"""单层全交叉开关。

结构：**每主设备一个按从设备分队列的输入缓冲 + 每从设备一个仲裁器**。

反压的传导路径（这是"拥塞"的物理机制，不是局部现象）::

    从设备队列满
      → 互联取不走事务（_arbitrate 提前返回并挂等待者）
        → 主设备的 ready 队列不腾空
          → 流量生成器停止提交（stall）
            → 上游负载被压住

每一环都在模型里显式存在，所以"某个主设备被谁堵住了"可以被逐环归因，而不是只能看到
一个终态带宽数字。
"""

from __future__ import annotations

from typing import Mapping

from ..axi.port import MasterPort
from ..axi.transaction import AxiBurst
from ..kernel.scheduler import PRIO_ARB, Scheduler
from ..slaves.base import SlaveDevice
from .arbiter import Arbiter, make_arbiter


class Crossbar:
    """单层全交叉开关：所有主设备可达所有从设备。"""

    def __init__(
        self,
        name: str,
        sched: Scheduler,
        master_ports: list[MasterPort],
        slave_devices: Mapping[str, SlaveDevice],
        policy: str = "drr",
        arb_latency_ps: int = 0,
        max_packet_bytes: int = 256,
        weights: Mapping[str, int] | None = None,
        priorities: Mapping[str, int] | None = None,
        aging_ps: int = 0,
    ):
        self.name = name
        self.sched = sched
        self.masters: dict[str, MasterPort] = {p.name: p for p in master_ports}
        self.slaves: dict[str, SlaveDevice] = dict(slave_devices)
        self.arb_latency_ps = arb_latency_ps

        # 每从设备一个仲裁器；候选是全部主设备
        flows = list(self.masters)
        self.arbiters: dict[str, Arbiter] = {
            s: make_arbiter(
                policy, flows, max_packet_bytes,
                weights=weights, priorities=priorities, aging_ps=aging_ps,
            )
            for s in self.slaves
        }

        self._arb_scheduled: dict[str, bool] = {s: False for s in self.slaves}

        # 计数器（归因用）
        self.grants = 0
        self.granted_bytes = 0
        self.backpressure_events = 0
        self.slave_busy_events = 0
        self.rearbitrations = 0

    # --- 主设备侧通知 ---

    def notify(self, slave: str) -> None:
        """某主设备有了发往 ``slave`` 的新请求，需要一次仲裁。

        合并重复通知：同刻多次 notify 只排一次仲裁事件。不合并会制造大量空转事件，
        直接把仿真吞吐打下来。
        """
        if self._arb_scheduled.get(slave, False):
            return
        self._arb_scheduled[slave] = True
        if self.arb_latency_ps > 0:
            self.sched.schedule(self.arb_latency_ps, self._arbitrate, slave, priority=PRIO_ARB)
        else:
            # 组合逻辑仲裁：同刻处理，但仍经事件队列以保证确定性排序
            self.sched.schedule(0, self._arbitrate, slave, priority=PRIO_ARB)

    # --- 仲裁 ---

    def _arbitrate(self, slave: str) -> None:
        self._arb_scheduled[slave] = False
        t = self.sched.now
        sp = self.slaves.get(slave)
        if sp is None:
            return

        # 收集候选。**只把从设备能接收的放进候选集**——这对应 AXI 里
        # "下游 READY 未拉高时上游无法完成握手"，物理上就是没有信用。
        # 按方向分别准入（AR 与 AW 是独立的通道），所以这里必须逐 burst 判断。
        heads: dict[str, AxiBurst] = {}
        blocked = 0
        for name, mp in self.masters.items():
            b = mp.peek_ready(slave)
            if b is None:
                continue
            if not sp.can_accept(b):
                blocked += 1
                continue
            heads[name] = b

        if not heads:
            if blocked:
                # 从设备反压。挂等待者，等它腾出空间再重新仲裁——
                # 这就是拥塞沿链路向上传播的实现。
                self.backpressure_events += 1
                sp.when_space(lambda s=slave: self.notify(s))
            return

        arb = self.arbiters[slave]
        winner = arb.select(heads, t)
        if winner is None:
            return

        mp = self.masters[winner]
        burst = mp.take_ready(slave, t)
        if burst is None:  # pragma: no cover - peek 与 take 之间没有事件，不应发生
            return
        burst.t_grant = t

        # 候选已按 can_accept 过滤，且检查与入队之间没有事件，所以入队必然成功。
        # 用断言而不是静默回滚——静默回滚会掩盖真实 bug。
        assert sp.enqueue(burst, t), (
            f"[{self.name}] {slave} 通过了准入检查但入队失败——模型内部状态不一致"
        )
        self.grants += 1
        self.granted_bytes += burst.nbytes

        # 还有别的候选 → 继续仲裁，别等下一次外部通知。
        # 少了这一步，每轮只能授权一个事务，会严重低估带宽。
        for m in self.masters.values():
            b = m.peek_ready(slave)
            if b is not None and sp.can_accept(b):
                self.rearbitrations += 1
                self.notify(slave)
                return

    # --- 统计 ---

    def arbiter_summary(self) -> dict[str, dict[str, object]]:
        out = {}
        for s, arb in self.arbiters.items():
            out[s] = {
                "policy": arb.name,
                "grants": dict(arb.stats.grants),
                "granted_bytes": dict(arb.stats.granted_bytes),
                "bytes_share": {k: round(v, 4) for k, v in arb.stats.bytes_share().items()},
            }
        return out

    def starvation_report(self, t_end_ps: int, threshold_ps: int) -> dict[str, list[dict]]:
        return {
            s: arb.starvation_report(t_end_ps, threshold_ps)
            for s, arb in self.arbiters.items()
        }

    def reset_stats(self, t_ps: int) -> None:
        """预热结束时清空计数器。

        **必须做**：这些计数器与从设备的 ``bytes_accepted`` 是配对的口径，只在一边
        清零会让"授权字节数 == 受理字节数"这条不变量在预热场景下永远不成立。
        """
        self.grants = 0
        self.granted_bytes = 0
        self.backpressure_events = 0
        self.slave_busy_events = 0
        self.rearbitrations = 0
        for arb in self.arbiters.values():
            arb.stats.grants = {f: 0 for f in arb.flows}
            arb.stats.granted_bytes = {f: 0 for f in arb.flows}
            arb.stats.last_grant_t = {f: -1 for f in arb.flows}
            arb.stats.waits = {f: 0 for f in arb.flows}

    def summary(self) -> dict[str, object]:
        return {
            "name": self.name,
            "masters": list(self.masters),
            "slaves": list(self.slaves),
            "grants": self.grants,
            "granted_bytes": self.granted_bytes,
            "backpressure_events": self.backpressure_events,
            "rearbitrations": self.rearbitrations,
            "arb_latency_ps": self.arb_latency_ps,
        }
