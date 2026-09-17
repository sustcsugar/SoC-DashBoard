"""从设备模型基类。

所有从设备共享的结构：**按方向分开的有界输入队列 + 服务资源 + 完成回调**。

**为什么要按方向分开队列。** 这不是实现细节，是物理事实：AXI 的 AR 与 AW 是两条
独立通道，控制器前端本来就有两套缓冲。更重要的是，读和写的行为**不对称**：

- 读是**按需**的——发起方在等数据，延迟直接变成主设备的停顿
- 写是**可缓冲**的——发起方把数据丢进写缓冲就走，只要缓冲没满就不影响它

内存控制器正是利用这个不对称：把写请求攒在缓冲里，攒够一批再集中排到总线上，
从而把"读写方向切换"这个昂贵操作摊薄到很多个事务上。如果模型把读写得对称处理
（谁先到服务谁），批处理就永远不会发生，翻转损耗会被严重高估。

有界性是刻意的——它是死锁可检的前提（无界缓冲在数学上不可能形成依赖环）。同时
"队列满"这个状态本身就是重要的观测量：它区分「从设备处理不过来」和「上游没喂够」，
这两者的修复方向完全相反。
"""

from __future__ import annotations

from typing import Callable

from ..axi.port import BoundedQueue
from ..axi.transaction import Access, AxiBurst
from ..kernel.scheduler import Scheduler


class SlaveDevice:
    """从设备基类。

    子类需要实现 ``_try_service(t)``：把队列里能发的事务尽量发出去。要求**幂等**——
    没有可服务事务时立即返回，不要维护"是否正在服务"的状态位（那个状态位在事件驱动
    模型里容易和真实的资源占用混淆）。
    """

    kind = "base"

    def __init__(
        self,
        name: str,
        sched: Scheduler,
        queue_depth: int,
        on_complete: Callable[[AxiBurst], None],
        write_queue_depth: int | None = None,
    ):
        self.name = name
        self.sched = sched
        # 读队列 = AR 通道缓冲；写队列 = AW 通道缓冲（写缓冲）
        self.read_q = BoundedQueue(f"{name}.ar", queue_depth)
        self.write_q = BoundedQueue(
            f"{name}.aw", write_queue_depth if write_queue_depth else queue_depth
        )
        self._on_complete = on_complete
        self._waiters: list[Callable[[], None]] = []

        self.accepted = 0
        self.completed = 0
        self.bytes_accepted = 0
        self.bytes_completed = 0

    # --- 队列接口（互联调用）---

    def queue_for(self, burst: AxiBurst) -> BoundedQueue:
        return self.write_q if burst.access is Access.WRITE else self.read_q

    def queues(self) -> tuple[BoundedQueue, BoundedQueue]:
        return self.read_q, self.write_q

    def can_accept(self, burst: AxiBurst) -> bool:
        """能否接收这个具体请求。

        按方向分别判断——这是"写缓冲还能吸收但读队列已满"这类状态能表达出来的前提。
        """
        return not self.queue_for(burst).full()

    def has_space(self) -> bool:
        """任一方向还有空间。不知道具体 burst 时的宽松检查。"""
        return not self.read_q.full() or not self.write_q.full()

    def enqueue(self, burst: AxiBurst, t_ps: int) -> bool:
        q = self.queue_for(burst)
        if not q.push(burst):
            return False
        self.accepted += 1
        self.bytes_accepted += burst.nbytes
        self._try_service(t_ps)
        return True

    def when_space(self, cb: Callable[[], None]) -> None:
        """任一队列腾出空间时回调一次。

        这是**反压传导**的实现：从设备满了 → 互联拿不到 → 主设备停在 ready 队列里
        → 流量生成器停滞。拥塞就是这样沿链路向上传播的，不是局部现象。
        """
        self._waiters.append(cb)

    def _wake(self) -> None:
        if self._waiters:
            waiters, self._waiters = self._waiters, []
            for cb in waiters:
                cb()

    # --- 子类共用的调度辅助 ---

    @staticmethod
    def id_order_safe(items: list[AxiBurst], window_len: int) -> list[int]:
        """返回可安全提前调度的候选下标（**同 ID 保序**约束下）。

        AXI 要求同一 ID 的响应按序返回。所以不能把某个 ID 的后续事务提到它的前序
        之前——这条约束让 **AXI ID 数量成为真实的并发瓶颈**，也是队头阻塞的来源。

        ``ID 是每个主设备独立的``：主设备 A 的 ID=0 与主设备 B 的 ID=0 是**两条无关
        的流**。保序约束作用在 ``(master, axi_id)`` 上，不是 ``axi_id`` 上。按后者
        索引会让不同主设备互相阻塞，把带宽严重低估。
        """
        first_seen: dict[tuple[str, int], int] = {}
        for i, b in enumerate(items):
            first_seen.setdefault((b.master, b.axi_id), i)
        out = []
        for i in range(min(window_len, len(items))):
            if first_seen[(items[i].master, items[i].axi_id)] == i:
                out.append(i)
        return out

    # --- 子类实现 ---

    def _try_service(self, t_ps: int) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    def _finish(self, burst: AxiBurst, t_ps: int) -> None:
        """服务完成：交给互联，释放资源，尝试继续服务。"""
        self.completed += 1
        self.bytes_completed += burst.nbytes
        self._on_complete(burst)
        self._wake()          # 队列腾空了，唤醒等空间的一方
        self._try_service(t_ps)

    # --- 统计 ---

    @property
    def queue(self) -> BoundedQueue:
        """主队列（读队列）。保留这个别名让"队列深度"这类通用统计有明确指向。"""
        return self.read_q

    def queue_snapshot(self) -> dict[str, int]:
        return {"read": len(self.read_q), "write": len(self.write_q)}

    def service_stats(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "accepted": self.accepted,
            "completed": self.completed,
            "bytes_completed": self.bytes_completed,
            "read_q_depth": self.read_q.depth,
            "write_q_depth": self.write_q.depth,
            "read_q_peak": self.read_q.peak,
            "write_q_peak": self.write_q.peak,
            "read_q_stalls": self.read_q.stall_events,
            "write_q_stalls": self.write_q.stall_events,
        }

    def conservation_ok(self) -> bool:
        """守恒校验（局部）：受理字节数 == 完成字节数。

        只在未预热、且仿真跑到队列排空时成立。全局守恒校验由 ``SocResult`` 用
        "发起 − 完成 == 在途"的口径做，那个在预热和截断下都成立。
        """
        return self.bytes_accepted == self.bytes_completed

    def reset_stats(self, t_ps: int) -> None:
        """预热结束时清空归因计数器。"""
        self.accepted = 0
        self.completed = 0
        self.bytes_accepted = 0
        self.bytes_completed = 0
        for q in self.queues():
            q.stall_events = 0
            q.peak = len(q)

    def summary(self) -> dict[str, object]:
        return self.service_stats()
