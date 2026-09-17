"""主设备端口：outstanding 管理、按从设备分队列、ID 分配。

这里区分两个**不同**的资源，混淆它们是最常见的建模错误之一：

- **请求队列**（``ready``）：已生成但尚未被互联接受的请求。AXI 上是 AR/AW 的
  VALID 已拉高、等 READY。容量是主设备内部的请求缓冲深度。
- **outstanding**（``inflight``）：已被接受、等待响应的事务。AXI 上是 AR/AW 握手
  完成、R/B 未回。容量是主设备自己的追踪能力（对应 CPU 侧的 MSHR）。

**只加大请求队列而不加 outstanding，不会提升带宽，只会让等待更长。** 反过来，
outstanding 是 PSRAM 系统里决定有效带宽的那个旋钮（Little's Law）。

**按从设备分队列**：真实交叉开关在主设备侧做地址译码，把发往不同从设备的请求放进
不同的输入队列。如果只用一个 FIFO，一个发往慢速从设备的请求会堵住后面发往空闲从设备
的请求——那是**模型凭空造出来的队头阻塞**，会让带宽被系统性低估。

队列满时主设备**停滞**（stall）。这里用事件驱动的等待者回调实现，不轮询——轮询会
制造大量空转事件，直接把仿真吞吐打下来。
"""

from __future__ import annotations

from collections import deque
from typing import Callable

from .transaction import AxiBurst, TrafficClass


class BoundedQueue:
    """有界 FIFO。

    **有界性是死锁可检的前提。** 无界缓冲在数学上不可能形成依赖环，模型里也就永远
    检测不到死锁（见 methodology 第 5.1 节）。

    维护累计字节数 ``nbytes``（O(1) 更新）。内存控制器的水位线判断需要按**字节**而不是
    按事务数——写缓冲的容量本来就是字节量，而且 burst 长度可变时事务数会误导。
    """

    __slots__ = ("name", "depth", "_q", "peak", "stall_events", "nbytes")

    def __init__(self, name: str, depth: int):
        if depth <= 0:
            raise ValueError(f"队列深度必须为正，{name} 收到 {depth}")
        self.name = name
        self.depth = depth
        self._q: deque[AxiBurst] = deque()
        self.peak = 0
        self.stall_events = 0
        self.nbytes = 0

    def full(self) -> bool:
        return len(self._q) >= self.depth

    def push(self, item: AxiBurst) -> bool:
        """入队。满则返回 False 并计入一次停滞。"""
        if self.full():
            self.stall_events += 1
            return False
        self._q.append(item)
        self.nbytes += item.nbytes
        if len(self._q) > self.peak:
            self.peak = len(self._q)
        return True

    def pop(self) -> AxiBurst | None:
        if not self._q:
            return None
        item = self._q.popleft()
        self.nbytes -= item.nbytes
        return item

    def head(self) -> AxiBurst | None:
        return self._q[0] if self._q else None

    def remove(self, item: AxiBurst) -> bool:
        """按对象移除（用于乱序调度）。O(n)，但队列深度很小。"""
        try:
            self._q.remove(item)
            self.nbytes -= item.nbytes
            return True
        except ValueError:
            return False

    def __len__(self) -> int:
        return len(self._q)

    def __iter__(self):
        return iter(self._q)


class MasterPort:
    """一个主设备在互联上的接入端口。

    职责：
    1. 按目标从设备持有待仲裁请求（``ready[slave]``，各有界）
    2. 限制在途事务总数（``inflight``，有界）
    3. 分配 AXI ID
    4. 队列满时通知流量生成器停滞，空间释放时回调唤醒
    """

    __slots__ = (
        "name", "max_outstanding", "n_ids", "slaves", "ready", "inflight",
        "_waiters", "_next_id", "on_submit", "on_grant",
        "issued", "completed", "stall_count", "id_wraps",
        "access_counts", "class_counts", "bytes_issued",
    )

    def __init__(
        self,
        name: str,
        slaves: list[str],
        queue_depth: int,
        max_outstanding: int,
        n_ids: int = 4,
    ):
        if max_outstanding <= 0:
            raise ValueError(f"max_outstanding 必须为正，{name} 收到 {max_outstanding}")
        self.name = name
        self.slaves = list(slaves)
        self.max_outstanding = max_outstanding
        self.n_ids = max(1, n_ids)
        self.ready: dict[str, BoundedQueue] = {
            s: BoundedQueue(f"{name}.{s}", queue_depth) for s in self.slaves
        }
        self.inflight: dict[int, AxiBurst] = {}
        self._waiters: dict[str, list[Callable[[], None]]] = {}
        self._next_id = 0
        self.on_submit: Callable[[AxiBurst], None] | None = None
        """提交成功后的回调。交叉开关用它触发一次仲裁。

        不用猴子补丁改 ``submit``：``__slots__`` 会拦下来，而且隐式替换方法会让
        调用链难以追踪。显式钩子更可靠也更好读。
        """
        self.on_grant: Callable[[AxiBurst, int], None] | None = None
        """事务被互联接受（AR/AW 握手完成）后的回调，带时刻。

        **这是 AXI 语义上事务成为 "outstanding" 的时刻**，所以监控在这里把并发计数
        加一。提交（``submit``）只是进了主设备自己的请求缓冲，还不算在途。
        """

        self.issued = 0
        self.completed = 0
        self.stall_count = 0
        self.id_wraps = 0
        self.bytes_issued = 0
        self.access_counts: dict[str, int] = {}
        self.class_counts: dict[str, int] = {}

    # --- 容量 ---

    def can_accept(self, slave: str) -> bool:
        """能否再接受一个发往 ``slave`` 的新请求。"""
        q = self.ready.get(slave)
        if q is None or q.full():
            return False
        return self.outstanding < self.max_outstanding

    @property
    def outstanding(self) -> int:
        return len(self.inflight)

    @property
    def headroom(self) -> int:
        """还剩多少 outstanding 余量。

        这是区分「并发受限」与「器件受限」的关键观测量：拐点处 headroom 接近 0
        说明是并发受限（改配置能救）；headroom 充裕但总线已饱和则是器件受限。
        """
        return self.max_outstanding - self.outstanding

    def pending_total(self) -> int:
        return sum(len(q) for q in self.ready.values())

    # --- 停滞等待者（事件驱动，不轮询）---

    def when_space(self, slave: str, cb: Callable[[], None]) -> None:
        """注册一个"该从设备队列腾出空间时调用一次"的回调。"""
        self._waiters.setdefault(slave, []).append(cb)

    def _wake(self, slave: str) -> None:
        waiters = self._waiters.pop(slave, None)
        if waiters:
            for cb in waiters:
                cb()

    def _wake_all(self) -> None:
        for slave in list(self._waiters):
            self._wake(slave)

    # --- 提交与完成 ---

    def submit(self, burst: AxiBurst) -> bool:
        """提交一个请求。无法接受则返回 False（调用方应改注册 ``when_space``）。"""
        q = self.ready.get(burst.slave)
        if q is None:
            raise KeyError(f"{self.name} 没有到从设备 {burst.slave!r} 的路径")
        if q.full() or self.outstanding >= self.max_outstanding:
            self.stall_count += 1
            return False
        burst.axi_id = self._alloc_id()
        q.push(burst)
        self.issued += 1
        self.bytes_issued += burst.nbytes
        key_ac = burst.access.label
        self.access_counts[key_ac] = self.access_counts.get(key_ac, 0) + 1
        key_cl = burst.traffic_class.label
        self.class_counts[key_cl] = self.class_counts.get(key_cl, 0) + 1
        if self.on_submit is not None:
            self.on_submit(burst)
        return True

    def _alloc_id(self) -> int:
        """轮询分配 AXI ID。

        真实主设备通常按数据流（缓存行 / 描述符）分配 ID，用 ID 换取乱序完成的
        自由度。这里用轮询近似：**ID 数量直接决定并发能力**，属于那类"加几个 ID
        就能提带宽"的旋钮。
        """
        if self.n_ids == 1:
            return 0
        axi_id = self._next_id
        self._next_id += 1
        if self._next_id >= self.n_ids:
            self._next_id = 0
            self.id_wraps += 1
        return axi_id

    def peek_ready(self, slave: str) -> AxiBurst | None:
        """本端口向互联**呈现**的第一个待仲裁请求。没有可呈现的则返回 None。

        ★ **outstanding 满时返回 None。** 这不是优化，是物理约束：真实主设备在自己
        的追踪资源用尽时就**不再拉高 AR/AW 的 VALID**，互联也就无从完成握手。

        少了这一条，请求会在 ``ready`` 队列里等授权，而等待期间其他请求被授权会把
        在途数推过上限——并发数被凭空创造，Little's Law 校验也就失去了意义。
        """
        if self.outstanding >= self.max_outstanding:
            return None
        q = self.ready.get(slave)
        return q.head() if q is not None else None

    def take_ready(self, slave: str, t_ps: int) -> AxiBurst | None:
        """互联取走一个待仲裁请求。对应 AXI 上 AR/AW 的握手完成。

        **事务在这一刻才成为 "outstanding"**，所以：
        - ``t_issue`` 在这里定，而不是在提交时定
        - 并发计数在这里加一

        这样「延迟 = t_done − t_issue」与「在途数」就落在**同一个时间区间**上，
        Little's Law 才闭合。提交到握手之间的等待（主设备端口排队）另计为端口停滞。
        """
        q = self.ready.get(slave)
        if q is None:
            return None
        if self.outstanding >= self.max_outstanding:
            # 不该发生：peek_ready 已经挡住了。到这里说明交叉开关绕过了呈现检查。
            return None
        burst = q.pop()
        if burst is not None:
            burst.t_issue = t_ps
            self.inflight[burst.seq] = burst
            if self.on_grant is not None:
                self.on_grant(burst, t_ps)
            self._wake(slave)
        return burst

    def complete(self, burst: AxiBurst) -> None:
        """事务完成（R 最后一拍 / B 响应），释放 outstanding 槽位。"""
        if self.inflight.pop(burst.seq, None) is not None:
            self.completed += 1
        self._wake_all()

    def cancel(self, burst: AxiBurst) -> None:
        """取消一个尚未完成的事务（超时路径用）。"""
        q = self.ready.get(burst.slave)
        if q is not None and q.remove(burst):
            self._wake(burst.slave)
        elif self.inflight.pop(burst.seq, None) is not None:
            self._wake_all()

    def inflight_by_class(self, cls: TrafficClass) -> int:
        return sum(1 for b in self.inflight.values() if b.traffic_class is cls)

    def reset_stats(self, t_ps: int) -> None:
        """预热结束时清空计数器。**不能清 inflight / ready**——那些是真实状态，
        清掉会丢事务并让守恒校验失败。"""
        self.issued = 0
        self.completed = 0
        self.stall_count = 0
        self.bytes_issued = 0
        self.id_wraps = 0
        self.access_counts.clear()
        self.class_counts.clear()
        for q in self.ready.values():
            q.stall_events = 0
            q.peak = len(q)

    def summary(self) -> dict[str, object]:
        return {
            "issued": self.issued,
            "completed": self.completed,
            "bytes_issued": self.bytes_issued,
            "outstanding_limit": self.max_outstanding,
            "ready_peak": max((q.peak for q in self.ready.values()), default=0),
            "ready_depth_per_slave": next(iter(self.ready.values())).depth if self.ready else 0,
            "stall_count": self.stall_count,
            "ids": self.n_ids,
        }
