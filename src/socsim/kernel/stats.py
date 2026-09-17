"""在线统计采集。

三类数据结构，对应三类要回答的问题：

- ``SampleCollector``：**分布**类问题（延迟的 p50/p99/p999）。保存原始整数样本以
  获得精确分位——直方图或蓄水池抽样会在意 p999 的尾部分位，而尾延迟恰恰是实时性
  分析的核心。
- ``TimeSeries``：**时序**类问题（带宽随时间的演化）。固定宽度时间桶累加。
- ``Gauge``：**占用率**类问题（队列深度、在途事务数）。给出时间加权平均值——这正是
  Little's Law 校验（实测并发数 == 吞吐 × 延迟）需要的量。
- ``Counter``：**计数**类问题（仲裁获胜次数、翻转次数）。

所有时间单位是皮秒（int），所有量是整数，避免浮点累积误差。
"""

from __future__ import annotations

from array import array


class SampleCollector:
    """保存整数样本，结束时计算精确分位。

    **只在仿真结束后调用 ``percentile`` / ``summary``。** 仿真过程中数组会扩容，
    内部持有的 numpy 视图随之失效。
    """

    __slots__ = ("name", "unit", "_data", "_max_samples", "_overflow")

    #: 默认上限 200 万样本 ≈ 16MB（int64）。超过后停止采集并置 overflow 标志。
    DEFAULT_MAX = 2_000_000

    def __init__(self, name: str, unit: str = "ps", max_samples: int = DEFAULT_MAX):
        self.name = name
        self.unit = unit
        self._data = array("q")
        self._max_samples = max_samples
        self._overflow = False

    def add(self, value: int) -> None:
        if len(self._data) >= self._max_samples:
            self._overflow = True
            return
        self._data.append(value)

    def extend(self, values: array) -> None:
        for v in values:
            self.add(v)

    def __len__(self) -> int:
        return len(self._data)

    @property
    def overflowed(self) -> bool:
        """样本被截断过——分位数会偏低，报告中必须标注。"""
        return self._overflow

    def _numpy(self):
        import numpy as np

        return np.frombuffer(self._data, dtype=np.int64)

    def percentile(self, q: float) -> float:
        """分位数，``q`` 取 0~100。空样本返回 nan。"""
        if not len(self._data):
            return float("nan")
        import numpy as np

        return float(np.percentile(self._numpy(), q))

    def percentiles(self, qs: tuple[float, ...] = (50, 90, 95, 99, 99.9)) -> dict[str, float]:
        """一次算出多个分位，避免重复转换。"""
        if not len(self._data):
            return {f"p{q:g}": float("nan") for q in qs}
        import numpy as np

        arr = self._numpy()
        return {f"p{q:g}": float(np.percentile(arr, q)) for q in qs}

    @property
    def mean(self) -> float:
        if not len(self._data):
            return float("nan")
        import numpy as np

        return float(self._numpy().mean())

    @property
    def std(self) -> float:
        if len(self._data) < 2:
            return 0.0
        import numpy as np

        return float(self._numpy().std())

    @property
    def total(self) -> int:
        return sum(self._data)

    def summary(self) -> dict[str, float]:
        """报告用的一站式摘要。"""
        if not len(self._data):
            return {"count": 0}
        import numpy as np

        arr = self._numpy()
        return {
            "count": int(len(arr)),
            "mean": float(arr.mean()),
            "min": float(arr.min()),
            "p50": float(np.percentile(arr, 50)),
            "p90": float(np.percentile(arr, 90)),
            "p95": float(np.percentile(arr, 95)),
            "p99": float(np.percentile(arr, 99)),
            "p99.9": float(np.percentile(arr, 99.9)),
            "max": float(arr.max()),
            "overflowed": self._overflow,
        }

    def histogram(self, n_bins: int = 80) -> tuple["list[float]", "list[float]"]:
        """返回 ``(bin_edges, counts)``，用于 CDF / 直方图。"""
        if not len(self._data):
            return [], []
        import numpy as np

        counts, edges = np.histogram(self._numpy(), bins=n_bins)
        return edges.tolist(), counts.tolist()


class TimeSeries:
    """固定宽度时间桶累加器。桶按需增长，所以不需要预先知道仿真时长。

    >>> ts = TimeSeries("ddr_bw", bucket_ps=100_000)   # 每桶 100ns
    >>> ts.add(250_000, 64)                            # t=250ns 处累加 64 字节
    >>> ts.bucket_index(250_000)
    2
    """

    __slots__ = ("name", "unit", "bucket_ps", "_buckets")

    def __init__(self, name: str, bucket_ps: int, unit: str = "B"):
        if bucket_ps <= 0:
            raise ValueError(f"桶宽必须为正，收到 {bucket_ps}")
        self.name = name
        self.unit = unit
        self.bucket_ps = bucket_ps
        self._buckets = array("q")

    def bucket_index(self, t_ps: int) -> int:
        return t_ps // self.bucket_ps

    def add(self, t_ps: int, amount: int = 1) -> None:
        idx = t_ps // self.bucket_ps
        buckets = self._buckets
        # 中间的空桶补零——它们代表"什么都没发生"的时间段，是拥塞分析的重要信息
        while len(buckets) <= idx:
            buckets.append(0)
        buckets[idx] += amount

    @property
    def n_buckets(self) -> int:
        return len(self._buckets)

    def values(self) -> array:
        return self._buckets

    def times_ps(self) -> list[int]:
        """每个桶的左边界时刻。"""
        return [i * self.bucket_ps for i in range(len(self._buckets))]

    def as_rate(self, unit_scale: float = 1.0) -> list[float]:
        """转成速率（量/秒）。用于带宽曲线。"""
        bucket_s = self.bucket_ps / 1e12
        return [v / bucket_s * unit_scale for v in self._buckets]

    def sum(self) -> int:
        return sum(self._buckets)

    def peak(self) -> int:
        return max(self._buckets) if self._buckets else 0

    def nonzero_buckets(self) -> int:
        return sum(1 for v in self._buckets if v != 0)


class Gauge:
    """跟踪一个随时间变化的量：当前值、峰值、**时间加权平均**。

    ``Gauge`` 是本项目里 Little's Law 校验的载体：

        实测并发数 = outstanding 的时间加权平均
        Little's Law 预测 = 完成事务率 × 平均延迟

    这两者必须相等（误差在几个百分点内），否则模型有 bug。

    **时间基准由 ``origin`` 定义**，初始为第一次 ``set`` 的时刻。预热后调用 ``reset``
    会把原点移到预热结束时刻，这样 ``time_avg`` 除以的是**测量窗口长度**而不是从零
    开始的绝对时刻。少了这一步，预热会让平均值被系统性低估 ``window/total`` 倍。

    >>> g = Gauge("outstanding")
    >>> g.set(0, 0)
    >>> g.set(4, 1000)      # t=1ns 时在途 4 个
    >>> g.set(0, 3000)      # t=3ns 时清空
    >>> g.time_avg(3000)    # 面积 / 时长 = (4*2000) / 3000
    2.6666666666666665
    """

    __slots__ = (
        "name", "value", "peak", "_area", "_t_last", "_t_origin", "_initialized",
        "_trace", "_trace_enabled", "_trace_limit",
    )

    def __init__(self, name: str, trace: bool = False, trace_limit: int = 200_000):
        self.name = name
        self.value = 0
        self.peak = 0
        self._area = 0
        self._t_last = 0
        self._t_origin = 0
        self._initialized = False
        # 可选轨迹：只在值变化时记一个点。队列深度的变化不频繁，轨迹很小，
        # 但足以画出准确的时序图（比按时间桶采样更省内存也更精确）
        self._trace_enabled = trace
        self._trace_limit = trace_limit
        self._trace: list[tuple[int, int]] = []

    def set(self, value: int, t_ps: int) -> None:
        """更新为 ``value``，并把上一段时长的面积累加进去。"""
        if self._initialized and t_ps > self._t_last:
            self._area += self.value * (t_ps - self._t_last)
        elif not self._initialized:
            self._t_origin = t_ps
        self._t_last = t_ps
        self._initialized = True
        if self._trace_enabled and value != self.value and len(self._trace) < self._trace_limit:
            self._trace.append((t_ps, value))
        self.value = value
        if value > self.peak:
            self.peak = value

    def add(self, delta: int, t_ps: int) -> None:
        """增减后更新。``set`` 的常用封装。"""
        self.set(self.value + delta, t_ps)

    def reset(self, t_ps: int) -> None:
        """把统计原点移到 ``t_ps``。

        用于预热结束：从此刻重新起算面积与窗口长度。**不能简单清零**——必须从当前的
        实际值继续积分，否则 Little's Law 校验会因为丢掉一段面积而系统性偏移。
        """
        self._area = 0
        self._t_last = t_ps
        self._t_origin = t_ps
        self._initialized = True
        self.peak = self.value
        if self._trace_enabled:
            self._trace = [(t_ps, self.value)]

    def trace(self) -> list[tuple[int, int]]:
        """``[(时刻, 值), ...]``。仅在构造时 ``trace=True`` 时非空。"""
        return self._trace

    def time_avg(self, t_end_ps: int) -> float:
        """从统计原点到 ``t_end_ps`` 的时间加权平均。"""
        if not self._initialized:
            return 0.0
        area = self._area + self.value * max(0, t_end_ps - self._t_last)
        span = t_end_ps - self._t_origin
        return area / span if span > 0 else 0.0

    def summary(self, t_end_ps: int) -> dict[str, float]:
        return {
            "current": self.value,
            "peak": self.peak,
            "time_avg": self.time_avg(t_end_ps),
        }


class Counter:
    """命名计数器集合。"""

    __slots__ = ("_c",)

    def __init__(self) -> None:
        self._c: dict[str, int] = {}

    def inc(self, key: str, amount: int = 1) -> None:
        self._c[key] = self._c.get(key, 0) + amount

    def get(self, key: str, default: int = 0) -> int:
        return self._c.get(key, default)

    def items(self):
        return self._c.items()

    def as_dict(self) -> dict[str, int]:
        return dict(self._c)

    def __contains__(self, key: str) -> bool:
        return key in self._c
