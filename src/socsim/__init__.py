"""socsim —— SoC 总线带宽 / 拥塞 / 互锁的交易级离散事件仿真器。

设计约束（来自 ADR-0000）：
- 核心模型 1000~1500 行可读代码，不是框架
- 时间单位统一为皮秒（int）
- 有界缓冲：死锁可检的前提
- DRR 而非 RR：AXI burst 长度可变，每事务公平是错的
- 三重自校验：上界 + Little's Law + 守恒
"""

__version__ = "0.1.0"

from . import units

__all__ = ["units", "__version__"]
