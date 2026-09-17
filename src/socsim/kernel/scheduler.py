"""离散事件仿真（DES）内核。

事件驱动的 Verilog 仿真器就是一个 DES 引擎——事件=信号跳变，事件队列=仿真器内部的
事件队列。区别只在粒度：RTL 的事件是 1-bit 信号跳变，这里的事件是交易级的（burst
到达、仲裁授权、数据返回）。

时间只在事件发生时"跳"。因为"两个事件之间什么都没发生"是物理事实，逐拍推进等于花
180 步证明"还是什么都没发生"。

实现要点：
- 堆元素是 ``(t, prio, seq, event)`` 元组。``seq`` 全局唯一，所以比较永远到不了
  ``event`` 字段，不依赖事件对象可比较。
- 同刻事件按 ``prio`` 再按 ``seq`` 排序，保证**确定性**——扫掠时不同配置的结果可比。
- 取消采用惰性标记（``event.alive = False``），弹出时跳过。避免堆内的删除操作。
"""

from __future__ import annotations

import heapq
import time
from typing import Any, Callable

# --- 同刻事件的处理顺序（小者先）---
#
# 约定：**先释放资源，再仲裁，再传输，最后接受新请求**。这个顺序保证了同一皮秒内
# 一个事务释放的缓冲能被同刻的仲裁看见，符合硬件里"组合逻辑在同一拍内传播"的直觉。
PRIO_FREE = 0    # 事务完成、缓冲释放、credit 归还
PRIO_ARB = 10    # 仲裁决策
PRIO_XFER = 20   # 数据 / 命令传输
PRIO_ISSUE = 30  # 主设备发起新请求
PRIO_MON = 40    # 监控采样（最后，看到稳定的状态）


class Event:
    """一个已排程的事件。

    ``alive`` 为 False 表示被惰性取消——弹出时跳过，不执行回调。
    """

    __slots__ = ("seq", "cb", "args", "alive", "t")

    def __init__(self, seq: int, cb: Callable[..., None], args: tuple, t: int = 0):
        self.seq = seq
        self.cb = cb
        self.args = args
        self.alive = True
        self.t = t

    def cancel(self) -> None:
        """惰性取消。对已经执行过的事件调用是安全的空操作。"""
        self.alive = False


class Scheduler:
    """离散事件调度器。所有时间单位是皮秒（int）。"""

    __slots__ = ("_q", "_seq", "now", "_max_events", "_fired", "_skipped", "_done")

    def __init__(self, max_events: int = 50_000_000):
        self._q: list[tuple[int, int, int, Event]] = []
        self._seq = 0
        self.now = 0
        self._max_events = max_events
        self._fired = 0
        self._skipped = 0
        self._done = False

    # --- 排程 ---

    def schedule(
        self,
        delay_ps: int,
        cb: Callable[..., None],
        *args: Any,
        priority: int = PRIO_XFER,
    ) -> Event:
        """在 ``now + delay_ps`` 执行 ``cb(*args)``。

        ``delay_ps`` 为 0 表示"本皮秒内稍后执行"——仍会经过堆，所以可以安全地
        从回调里调用，不会破坏当前的回调执行。
        """
        if delay_ps < 0:
            raise ValueError(f"延迟不能为负：{delay_ps}ps")
        self._seq += 1
        ev = Event(self._seq, cb, args, self.now + delay_ps)
        heapq.heappush(self._q, (ev.t, priority, self._seq, ev))
        return ev

    def schedule_at(
        self, time_ps: int, cb: Callable[..., None], *args: Any, priority: int = PRIO_XFER
    ) -> Event:
        """在绝对时刻执行。若 ``time_ps`` 已过，则视为当前时刻。"""
        return self.schedule(max(0, time_ps - self.now), cb, *args, priority=priority)

    # --- 运行 ---

    def run(self, until_ps: int | None = None) -> None:
        """推进仿真至 ``until_ps``（含）。``None`` 表示跑到事件耗尽。

        **可以在同一次仿真里多次调用**（例如先跑预热、再跑测量窗口）。只有"跑到
        事件耗尽"才会把调度器标记为终止；带时限的调用不会——否则第二次调用会
        直接返回，预热后的测量窗口就变成零长度。
        """
        q = self._q
        limit = until_ps if until_ps is not None else float("inf")

        while q and q[0][0] <= limit:
            if self._fired >= self._max_events:
                raise RuntimeError(
                    f"事件数超过上限 {self._max_events}。仿真可能陷入活锁，"
                    f"或时间窗口过长——请检查配置或调大 max_events。"
                )
            t, _prio, _seq, ev = heapq.heappop(q)
            self.now = t
            if not ev.alive:
                self._skipped += 1
                continue
            ev.alive = False  # 已执行，二次取消无意义
            self._fired += 1
            ev.cb(*ev.args)

        if until_ps is not None:
            self.now = until_ps
        else:
            self._done = True

    def run_for(self, duration_ps: int) -> None:
        """相对推进。"""
        self.run(self.now + duration_ps)

    # --- 状态查询 ---

    @property
    def pending(self) -> int:
        """队列中尚未处理的事件数（含已取消未弹出的）。"""
        return len(self._q)

    @property
    def stats(self) -> dict[str, int]:
        return {"fired": self._fired, "cancelled_skipped": self._skipped, "pending": len(self._q)}


class Stopwatch:
    """墙钟计时，用于验证内核吞吐是否达到目标（≥1M 事件/秒）。"""

    def __init__(self) -> None:
        self._t0 = 0.0
        self._elapsed = 0.0

    def start(self) -> "Stopwatch":
        self._t0 = time.perf_counter()
        return self

    def stop(self) -> float:
        self._elapsed = time.perf_counter() - self._t0
        return self._elapsed

    @property
    def elapsed(self) -> float:
        return self._elapsed

    def report(self, events: int) -> str:
        rate = events / self._elapsed if self._elapsed > 0 else 0.0
        return (
            f"{events:,} 事件 / {self._elapsed:.3f}s = {rate:,.0f} 事件/秒"
            f"（目标 ≥1,000,000）"
        )
