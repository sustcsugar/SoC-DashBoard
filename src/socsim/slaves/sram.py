"""多 bank 片上 SRAM 从设备。

这是**自研的**存储，内部可改，所以建模深度和 PSRAM（硬核）不同：建模它的 bank 结构、
交织粒度、端口类型和 bank 冲突。

与 PSRAM 的本质差别：PSRAM 是**一条串行共享总线**（所有访问共用数据线，还要付方向
切换代价），SRAM 是 **N 个并行 bank**（不同 bank 的访问完全独立）。所以 SRAM 的带宽
瓶颈几乎总是 bank 冲突和端口竞争，而不是总线宽度，也**没有读写方向切换问题**。

地址交织：``bank = (addr // interleave_bytes) % n_banks``。

- 交织粒度太小 → burst 跨越多个 bank，一次访问被迫拆成多次
- 交织粒度太大 → 连续访问全落在同一个 bank 上，并行度用不上

这个取舍是本模型能直接回答的问题之一，也是 Stage 3 扫掠的候选旋钮。
"""

from __future__ import annotations

from typing import Callable

from ..axi.transaction import Access, AxiBurst
from ..kernel.scheduler import PRIO_FREE, Scheduler
from ..units import PS_PER_S, ns
from .base import SlaveDevice


class BankedSramSlave(SlaveDevice):
    """多 bank 片上 SRAM。

    ``ports``：
    - ``"1r1w"``：每 bank 一个读口一个写口，读写可并行（双口 SRAM）
    - ``"1rw"``：每 bank 只有一个口，读写互相阻塞（单口 SRAM，面积小但争用严重）
    """

    kind = "sram"

    def __init__(
        self,
        name: str,
        sched: Scheduler,
        n_banks: int,
        interleave_bytes: int,
        bank_gbps: float,
        access_latency_ns: float,
        queue_depth: int,
        on_complete: Callable[[AxiBurst], None],
        ports: str = "1r1w",
        lookahead_depth: int = 32,
        base_addr: int = 0,
        size_bytes: int = 0,
    ):
        super().__init__(name, sched, queue_depth, on_complete)
        if n_banks < 1:
            raise ValueError(f"[{name}] bank 数必须 ≥1")
        if ports not in ("1r1w", "1rw"):
            raise ValueError(f"[{name}] ports 必须是 '1r1w' 或 '1rw'，收到 {ports!r}")

        self.n_banks = n_banks
        self.interleave_bytes = interleave_bytes
        self.bank_bytes_per_ps = bank_gbps * 1e9 / PS_PER_S
        self.access_latency_ps = ns(access_latency_ns)
        self.ports = ports
        self.lookahead_depth = lookahead_depth
        self.base_addr = base_addr
        self.size_bytes = size_bytes

        # 每个 bank 的空闲时刻。1r1w 时读写各一条，1rw 时共用。
        self.bank_rd_free = [0] * n_banks
        self.bank_wr_free = [0] * n_banks

        # 归因
        self.bank_conflicts = 0
        self.cross_bank_splits = 0
        self.residency_ps = 0
        self.reorder_events = 0
        self.bank_busy_ps = 0
        """所有 bank 的**累计忙碌时长之和**。

        不能拿 ``bank_*_free`` 去推——那些是**绝对时刻**不是时长，而且 1r1w 的读写
        两条线相加会重复计数。必须在这里显式累加服务时间。"""

    # --- 地址映射 ---

    def bank_of(self, addr: int) -> int:
        return (addr // self.interleave_bytes) % self.n_banks

    def banks_spanned(self, burst: AxiBurst) -> list[int]:
        """burst 覆盖的 bank 列表。跨越多个 bank 会被迫拆成多次访问。"""
        first = self.bank_of(burst.addr)
        last = self.bank_of(burst.last_addr)
        if first == last:
            return [first]
        # 简化：返回首尾两个 bank。真实拆分取决于交织方式，这里只用于统计。
        return sorted({first, last})

    def _bank_free_at(self, bank: int, is_write: bool) -> int:
        if self.ports == "1rw":
            return max(self.bank_rd_free[bank], self.bank_wr_free[bank])
        return self.bank_wr_free[bank] if is_write else self.bank_rd_free[bank]

    def _set_bank_free(self, bank: int, is_write: bool, t_ps: int) -> None:
        if self.ports == "1rw":
            self.bank_rd_free[bank] = t_ps
            self.bank_wr_free[bank] = t_ps
        elif is_write:
            self.bank_wr_free[bank] = t_ps
        else:
            self.bank_rd_free[bank] = t_ps

    def _service_ps(self, burst: AxiBurst) -> int:
        rate = self.bank_bytes_per_ps
        if rate <= 0:
            raise ValueError(f"[{self.name}] bank 带宽为 0")
        return max(1, int(burst.nbytes / rate))

    # --- 服务 ---

    def _pick(self, t_ps: int) -> tuple[AxiBurst, int, object] | None:
        """选一个 bank 已就绪的事务。

        允许在 ``lookahead_depth`` 窗口内重排——真实的 SRAM 互联会做同样的事（跳过
        落在忙 bank 上的事务，先服务落在空闲 bank 上的）。**不允许重排会让 bank 并行
        完全失效**，那不是真实设计的行为，而是模型缺陷。

        重排受同 ID 保序约束。两个方向各自独立挑选——SRAM 没有读写方向切换代价，
        所以不需要 PSRAM 那样的写缓冲攒批策略。
        """
        for q in (self.read_q, self.write_q):
            items = list(q)
            if not items:
                continue
            safe = self.id_order_safe(items, self.lookahead_depth)
            for i in safe:
                b = items[i]
                bank = self.bank_of(b.addr)
                if self._bank_free_at(bank, b.access is Access.WRITE) <= t_ps:
                    if i > 0:
                        self.reorder_events += 1
                    return b, i, q
        return None

    def _try_service(self, t_ps: int) -> None:
        while True:
            t = max(t_ps, self.sched.now)
            picked = self._pick(t)
            if picked is None:
                return
            burst, idx, q = picked
            if idx != 0:
                q.remove(burst)
            else:
                q.pop()
            self._issue(burst, t)

    def _issue(self, burst: AxiBurst, t_ps: int) -> None:
        is_wr = burst.access is Access.WRITE
        bank = self.bank_of(burst.addr)
        free_at = self._bank_free_at(bank, is_wr)
        start = free_at if free_at > t_ps else t_ps

        if free_at > t_ps:
            self.bank_conflicts += 1

        if len(self.banks_spanned(burst)) > 1:
            self.cross_bank_splits += 1

        service = self._service_ps(burst)
        self._set_bank_free(bank, is_wr, start + service)
        self.bank_busy_ps += service

        burst.t_service_start = t_ps
        t_done = start + service + self.access_latency_ps
        if t_done <= t_ps:
            t_done = t_ps + 1
        burst.t_done = t_done

        self.sched.schedule(t_done - self.sched.now, self._complete_event, burst, priority=PRIO_FREE)

    def _complete_event(self, burst: AxiBurst) -> None:
        self.residency_ps += burst.t_done - burst.t_service_start
        self._finish(burst, self.sched.now)

    # --- 统计 ---

    def bank_utilization(self, t_end_ps: int) -> float:
        """所有 bank 的**平均**占用率。接近 1 说明是 bank 受限。

        用累计忙碌时长 / (窗口 × bank 数) 计算——不能用绝对空闲时刻去推，
        那样会得到超过 100% 的荒谬值。
        """
        if t_end_ps <= 0:
            return 0.0
        return self.bank_busy_ps / (t_end_ps * self.n_banks)

    def reset_stats(self, t_ps: int) -> None:
        super().reset_stats(t_ps)
        self.bank_conflicts = 0
        self.cross_bank_splits = 0
        self.residency_ps = 0
        self.reorder_events = 0
        self.bank_busy_ps = 0

    def service_stats(self) -> dict[str, object]:
        base = super().service_stats()
        base.update({
            "n_banks": self.n_banks,
            "interleave_bytes": self.interleave_bytes,
            "ports": self.ports,
            "bank_conflicts": self.bank_conflicts,
            "cross_bank_splits": self.cross_bank_splits,
            "reorder_events": self.reorder_events,
            "residency_ps": self.residency_ps,
            "bank_busy_ps": self.bank_busy_ps,
        })
        return base
