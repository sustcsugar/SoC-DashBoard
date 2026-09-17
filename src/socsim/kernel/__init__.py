"""DES 内核：事件调度、时钟域、可复现随机、在线统计。"""

from .clock import ClockDomain, cdc_latency_ps
from .rng import RngPool, poisson_interval_ps
from .scheduler import (
    PRIO_ARB,
    PRIO_FREE,
    PRIO_ISSUE,
    PRIO_MON,
    PRIO_XFER,
    Event,
    Scheduler,
    Stopwatch,
)
from .stats import Counter, Gauge, SampleCollector, TimeSeries

__all__ = [
    "ClockDomain",
    "Counter",
    "Event",
    "Gauge",
    "PRIO_ARB",
    "PRIO_FREE",
    "PRIO_ISSUE",
    "PRIO_MON",
    "PRIO_XFER",
    "RngPool",
    "SampleCollector",
    "Scheduler",
    "Stopwatch",
    "TimeSeries",
    "cdc_latency_ps",
    "poisson_interval_ps",
]
