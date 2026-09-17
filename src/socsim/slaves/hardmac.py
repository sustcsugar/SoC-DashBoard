"""基于性能包络的硬核从设备（PSRAM）。

模型的两个核心点：

**一、延迟 ≠ 服务时间。**

- **服务时间**（``nbytes / sustained_rate``）：数据占用总线的时长。**串行、不可掩盖。**
- **延迟**（``latency_idle``）：命令到数据返回。**可被流水掩盖**——多个事务在途时
  各自的延迟重叠。

把这两者混为一谈就会得出"延迟高所以带宽低"的错误结论。真实关系是：
**延迟高本身不降低带宽；延迟高加上 outstanding 不足才降低带宽。**（Little's Law）

总线的忙碌时长直接由 ``sustained_rate`` 导出，而延迟只影响响应返回时刻。这带来一个
可测试的性质：纯方向、无竞争、outstanding 充足时，达成带宽精确等于 ``sustained_rate``。
这就是验证套件里的"已知答案测试"。

**二、读写不对称，写缓冲是批处理的来源。**

共享双向数据总线上，换向期间什么都传不了。若每个到达的事务都被立即服务，方向就会在
每个事务上切换，翻转开销可以吃掉大半带宽。

真实控制器用**写缓冲**解决这个问题：写请求先落进缓冲（发起方丢完就走，不受总线状态
影响），攒到高水位才集中排到总线上，一直排到低水位再切回读。高低水位之间的**滞回**
是关键——没有它方向会在水位附近反复横跳。

于是稳态自然形成「一批读 → 一批写 → 一批读」的长批次，翻转代价被摊薄到整批事务上。
这是内存控制器里最重要的一个调度机制，也是本模型里最有价值的建模点之一。
"""

from __future__ import annotations

from typing import Callable

from ..axi.port import BoundedQueue
from ..axi.transaction import Access, AxiBurst
from ..kernel.scheduler import PRIO_FREE, PRIO_XFER, Scheduler
from .base import SlaveDevice
from .envelope import MemoryEnvelope


class HardMacSlave(SlaveDevice):
    """硬核存储从设备：按厂商包络建模。"""

    kind = "hardmac"

    def __init__(
        self,
        name: str,
        sched: Scheduler,
        envelope: MemoryEnvelope,
        queue_depth: int,
        on_complete: Callable[[AxiBurst], None],
        address_range: tuple[int, int] | None = None,
        write_queue_depth: int | None = None,
    ):
        super().__init__(name, sched, queue_depth, on_complete, write_queue_depth)
        self.env = envelope
        self.address_range = address_range

        # 并发槽位（读写的 outstanding 能力不同）
        self.out_rd = 0
        self.out_wr = 0

        # 总线状态
        self.bus_free_at = 0
        self.last_busy_end = 0
        self.bus_dir: Access | None = None
        """总线**上一次实际传输**的方向。只在 ``_issue`` 里更新。

        ⚠️ 不能由 ``_pick`` 提前设置：``_pick`` 只是做决策，事务还没上总线。如果在
        决策时就改了它，``_issue`` 里的"方向是否变化"检查永远为假——**翻转损耗会被
        完全忽略**，而那正是共享总线 PSRAM 最大的一类开销。
        """

        # 归因计数器（瀑布图的数据来源）
        self.bus_busy_ps = 0
        self.bus_idle_ps = 0
        self.turnaround_ps_total = 0
        self.turnaround_events = 0
        self.direction_runs: list[int] = []
        self._run_len = 0
        self._batch_min_blocked = 0
        self._dir_start_t = 0
        """当前方向开始的时刻。

        **饥饿计时必须从这一刻算起，而不是从请求进入队列算起。** 这是本模块踩过的
        一个坑：饱和系统里队列总是很深，"最老的请求等了多久"永远是个大数，于是
        "读等太久了"这个条件恒为真，每次完成都会触发换向——批处理被彻底吃掉，
        方向连续长度塌到 1，翻转开销占 57% 的带宽。

        正确的语义是"**当前方向已经压制对向多久了**"：写排了多久没放读、读占了多久
        没排写。这才是实时保护要限制的量。"""
        """因"批还没够"而拒绝换向的次数。用于诊断实时性与效率的取舍。"""
        self.reorder_events = 0
        self.write_drains = 0
        self.forced_switches = 0
        self.read_priority_switches = 0
        self._batch_min_blocked = 0
        self._dir_start_t = 0
        """当前方向开始的时刻。

        **饥饿计时必须从这一刻算起，而不是从请求进入队列算起。** 这是本模块踩过的
        一个坑：饱和系统里队列总是很深，"最老的请求等了多久"永远是个大数，于是
        "读等太久了"这个条件恒为真，每次完成都会触发换向——批处理被彻底吃掉，
        方向连续长度塌到 1，翻转开销占 57% 的带宽。

        正确的语义是"**当前方向已经压制对向多久了**"：写排了多久没放读、读占了多久
        没排写。这才是实时保护要限制的量。"""

        self._recheck_gen = 0

    # --- 准入 ---

    def can_accept(self, burst: AxiBurst) -> bool:
        return not self.queue_for(burst).full()

    # --- 服务 ---

    def _outstanding(self, is_write: bool) -> int:
        return self.out_wr if is_write else self.out_rd

    def _has_slot(self, burst: AxiBurst) -> bool:
        is_wr = burst.access is Access.WRITE
        return self._outstanding(is_wr) < self.env.max_outstanding(is_wr)

    def _wait_ps(self, burst: AxiBurst, t_ps: int) -> int:
        """该事务已经等待了多久（从进入从设备算起）。"""
        entered = burst.t_grant if burst.t_grant > 0 else burst.t_issue
        return t_ps - entered

    def _peek(self, q: BoundedQueue, t_ps: int) -> tuple[AxiBurst, int] | None:
        """从队列里挑一个可发的事务，返回 ``(burst, 下标)``。

        受**同 ID 保序**约束——AXI 要求同 ID 的响应按序返回，所以不能把某个 ID 的
        后续事务提到它的前序之前。窗口内可以重排，这对应真实控制器命令调度器的
        重排能力（用重排换行命中率/方向批量的经典做法）。
        """
        items = list(q)
        if not items:
            return None
        safe = self.id_order_safe(items, self.env.lookahead_depth)
        for i in safe:
            if self._has_slot(items[i]):
                if i > 0:
                    self.reorder_events += 1
                return items[i], i
        return None

    def _write_blocked_ps(self, t_ps: int) -> int:
        """读方向已经压制写多久了（仅在读方向时有意义）。"""
        if self.bus_dir is Access.WRITE:
            return 0
        return t_ps - self._dir_start_t

    def _read_blocked_ps(self, t_ps: int) -> int:
        """写方向已经压制读多久了（仅在写方向时有意义）。"""
        if self.bus_dir is not Access.WRITE:
            return 0
        return t_ps - self._dir_start_t

    def _want_write(self, wr_head, t_ps: int) -> bool:
        """现在该不该切到写方向。

        三种情况之一成立就该排写：
        1. 读方向完全没货（没得选）
        2. 写缓冲到高水位（攒够了，排出去才划算）
        3. 写被读压制超过了硬上限（不能无限期推迟写）
        """
        if wr_head is None:
            return False
        env = self.env
        if not self.read_q:
            return True
        if self.write_q.nbytes >= env.write_drain_high_bytes:
            return True
        return self._write_blocked_ps(t_ps) >= env.write_starve_ps

    def _want_read(self, rd_head, t_ps: int) -> bool:
        """现在该不该切回读方向。

        读是**延迟敏感**的（发起方在等数据），两个条件都可触发：
        1. 写排空到了低水位（滞回的下沿，正常出口）
        2. 写已经压制读超过了硬上限（实时保护的兜底）

        注意条件 2 量的是"被写压制了多久"，不是"这个读等了多久"——
        见 ``_dir_start_t`` 的说明。
        """
        if rd_head is None:
            return False
        env = self.env
        if self.write_q.nbytes <= env.write_drain_low_bytes and self.read_q:
            return True
        return self._read_blocked_ps(t_ps) >= env.read_starve_ps

    def _pick(self, t_ps: int) -> tuple[AxiBurst, int, BoundedQueue] | None:
        """决定下一个服务谁。这是整个模型里最核心的一段策略。

        规则（读优先 + 写缓冲水位滞回 + 最小批）::

            当前是写方向：
              写排空到低水位 / 读等到硬上限  → 切回读
              批还没够且写还没等太久          → 继续排写（摊薄翻转代价）
              写没得发（队列空）              → 切回读

            当前是读方向：
              写到高水位 / 写等到硬上限 / 读全空 → 切去排写
              读有货但没 outstanding 槽位        → **等**，不换向
              否则                              → 发读

        最后那条是容易被忽略的一条：**读队列有货但没有 outstanding 槽位时，总线已经
        被在途事务喂饱了**，此时为了"填满总线"而换向，付出的是翻转代价却换不来带宽。
        应该等一个槽位释放（很快，就是一个完成事件）。

        规则里 1 与 3 的取舍是真实的架构矛盾：批越大翻转代价摊得越薄，但读的等待上限
        越长。``min_batch_bursts`` 就是这个取舍的旋钮。
        """
        rd = self._peek(self.read_q, t_ps)
        wr = self._peek(self.write_q, t_ps)
        env = self.env

        if not env.direction_batching:
            # 对照模式：关掉方向批处理，谁先有货就发谁。
            # 真实控制器不会这么做——它会让每个到达的事务立刻换向，
            # 翻转代价吃掉大半带宽。保留它是为了做 A/B 对照，
            # 量化"写缓冲 + 水位滞回"到底值多少。
            if rd is not None and (wr is None or self.read_q.nbytes >= self.write_q.nbytes):
                return rd[0], rd[1], self.read_q
            if wr is not None:
                return wr[0], wr[1], self.write_q
            if rd is not None:
                return rd[0], rd[1], self.read_q
            return None

        if self.bus_dir is Access.WRITE:
            if wr is None:
                # 写没得发：要么队列空，要么 outstanding 槽位满
                if rd is not None:
                    return rd[0], rd[1], self.read_q
                return None
            if self._want_read(rd, t_ps):
                self.read_priority_switches += 1
                self._dir_start_t = t_ps
                return rd[0], rd[1], self.read_q  # type: ignore[index]
            # 批还没够就不急着切——除非写排空已到低水位（那是正常出口，上面已判）
            if self._run_len < env.min_batch_bursts and self.write_q.nbytes > 0:
                self._batch_min_blocked += 1
            return wr[0], wr[1], self.write_q

        # 读方向（含初始状态）
        if rd is not None:
            if self._want_write(wr, t_ps):
                self.write_drains += 1
                self._dir_start_t = t_ps
                return wr[0], wr[1], self.write_q  # type: ignore[index]
            return rd[0], rd[1], self.read_q

        # 读没得发：队列空，或 outstanding 槽位满
        if wr is None:
            return None
        if not self.read_q:
            # 读方向彻底没货 → 没得选，只能写
            self.write_drains += 1
            self._dir_start_t = t_ps
            return wr[0], wr[1], self.write_q
        # ★ 读有货只是没槽位。总线已被在途读喂饱，不要为了填总线而换向。
        # 除非写已经等到了硬上限（否则写会被无限期饿死）。
        if self._write_blocked_ps(t_ps) >= env.write_starve_ps:
            self.forced_switches += 1
            self.write_drains += 1
            self._dir_start_t = t_ps
            return wr[0], wr[1], self.write_q
        return None

    def _try_service(self, t_ps: int) -> None:
        """把队列里能发的尽量发出去。幂等——没有可发事务时立即返回。"""
        t = max(t_ps, self.sched.now)
        issued = False
        while True:
            picked = self._pick(t)
            if picked is None:
                break
            burst, idx, q = picked
            if idx != 0:
                q.remove(burst)
            else:
                q.pop()
            self._issue(burst, t)
            issued = True

        # 如果队列里有货但被"攒批"挡住，安排一次到期复查。
        # 少了这一步，写缓冲会一直攒到天荒地老——没有外部事件会来唤醒它。
        if not issued:
            self._schedule_recheck(t)

    def _schedule_recheck(self, t_ps: int) -> None:
        """队列非空但发不出去时，在最早可能的到期时刻复查一次。"""
        if not self.read_q and not self.write_q:
            return
        env = self.env
        deadline = None

        if self.write_q and self.bus_dir is not Access.WRITE:
            # 写在高水位以下，得等写饥饿超时才能动
            deadline = self.write_q.head().t_grant + env.write_starve_ps
        if self.read_q and self.bus_dir is Access.WRITE:
            rd_dl = self.read_q.head().t_grant + env.read_starve_ps
            deadline = rd_dl if deadline is None else min(deadline, rd_dl)

        if deadline is None or deadline <= t_ps:
            return  # 等 outstanding 腾位，完成事件会自然唤醒

        self._recheck_gen += 1
        gen = self._recheck_gen

        def recheck() -> None:
            if gen == self._recheck_gen:
                self._try_service(self.sched.now)

        self.sched.schedule(deadline - self.sched.now, recheck, priority=PRIO_XFER)

    def _issue(self, burst: AxiBurst, t_ps: int) -> None:
        env = self.env
        is_wr = burst.access is Access.WRITE
        service = max(1, env.data_ps(burst.nbytes, is_wr))

        start = self.bus_free_at if self.bus_free_at > t_ps else t_ps

        # 方向切换惩罚：共享双向数据总线换向期间什么都传不了。
        # 这里读 bus_dir 是**上一次实际传输**的方向（只在 _issue 里更新），
        # 所以这是总线层面真实发生的换向，不是调度决策。
        turnaround = 0
        if self.bus_dir is not None and burst.access is not self.bus_dir:
            turnaround = env.turnaround_ps
            if turnaround > 0:
                self.turnaround_events += 1
            self.direction_runs.append(self._run_len)
            self._run_len = 0
            self._dir_start_t = t_ps

        idle_gap = start - self.last_busy_end
        if self.last_busy_end > 0 and idle_gap > 0:
            # 总线空闲：说明上游没喂够（并发不足），不是总线饱和
            self.bus_idle_ps += idle_gap

        start += turnaround
        end = start + service
        self.bus_busy_ps += service
        self.turnaround_ps_total += turnaround
        self.bus_free_at = end
        self.last_busy_end = end
        self.bus_dir = burst.access
        self._run_len += 1

        # 响应返回：延迟按当时的在途数取值
        inflight = self._outstanding(is_wr) + 1
        t_done = end + env.latency_ps(inflight)
        if t_done <= t_ps:
            t_done = t_ps + 1

        burst.t_service_start = t_ps
        burst.t_done = t_done

        if is_wr:
            self.out_wr += 1
        else:
            self.out_rd += 1

        self.sched.schedule(t_done - self.sched.now, self._complete_event, burst, priority=PRIO_FREE)

    def _complete_event(self, burst: AxiBurst) -> None:
        if burst.access is Access.WRITE:
            self.out_wr -= 1
        else:
            self.out_rd -= 1
        self._recheck_gen += 1  # 状态变了，作废待处理的复查
        self._finish(burst, self.sched.now)

    # --- 统计 ---

    def outstanding_now(self) -> int:
        return self.out_rd + self.out_wr

    def bus_utilization(self, t_end_ps: int) -> float:
        """总线利用率 = 忙碌时长 / 观测窗口。判断"器件受限"的依据。"""
        if t_end_ps <= 0:
            return 0.0
        return self.bus_busy_ps / t_end_ps

    def reset_stats(self, t_ps: int) -> None:
        super().reset_stats(t_ps)
        self.bus_busy_ps = 0
        self.bus_idle_ps = 0
        self.turnaround_ps_total = 0
        self.turnaround_events = 0
        self.direction_runs = []
        self._run_len = 0
        self.reorder_events = 0
        self.write_drains = 0
        self.forced_switches = 0
        self.read_priority_switches = 0
        self._batch_min_blocked = 0
        self._dir_start_t = 0
        """当前方向开始的时刻。

        **饥饿计时必须从这一刻算起，而不是从请求进入队列算起。** 这是本模块踩过的
        一个坑：饱和系统里队列总是很深，"最老的请求等了多久"永远是个大数，于是
        "读等太久了"这个条件恒为真，每次完成都会触发换向——批处理被彻底吃掉，
        方向连续长度塌到 1，翻转开销占 57% 的带宽。

        正确的语义是"**当前方向已经压制对向多久了**"：写排了多久没放读、读占了多久
        没排写。这才是实时保护要限制的量。"""

    def mean_direction_run(self) -> float:
        runs = self.direction_runs + ([self._run_len] if self._run_len else [])
        return sum(runs) / len(runs) if runs else 0.0

    def service_stats(self) -> dict[str, object]:
        base = super().service_stats()
        base.update({
            "envelope": self.env.name,
            "out_rd": self.out_rd,
            "out_wr": self.out_wr,
            "bus_busy_ps": self.bus_busy_ps,
            "bus_idle_ps": self.bus_idle_ps,
            "turnaround_events": self.turnaround_events,
            "turnaround_ps": self.turnaround_ps_total,
            "mean_direction_run": round(self.mean_direction_run(), 2),
            "write_drains": self.write_drains,
            "forced_switches": self.forced_switches,
            "read_priority_switches": self.read_priority_switches,
            "batch_min_blocked": self._batch_min_blocked,
            "min_batch_bursts": self.env.min_batch_bursts,
            "reorder_events": self.reorder_events,
            "write_buffer_bytes": self.env.write_buffer_bytes,
        })
        return base
