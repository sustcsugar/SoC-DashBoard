"""指标采集。

采集的四个维度，对应四类要回答的问题：

1. **带宽**（按主设备 / 从设备 / 方向 / 时间桶）→ 谁拿多了、谁拿少了、什么时候开始恶化
2. **延迟分布**（按主设备 / 从设备 / 流量类别）→ 平均值之外，尾延迟才是实时性的决定因素
3. **占用率**（outstanding、队列深度）→ 区分并发受限与器件受限
4. **守恒量**（进出字节数）→ 自校验

**带宽必须按字节而不是事务数统计。** 事务数会被 burst 长度差异误导——这正是 RR 仲裁
在 AXI 上语义错误的原因，也是为什么统计口径要对齐到字节。
"""

from __future__ import annotations

from ..axi.transaction import AxiBurst, TrafficClass
from ..kernel.stats import Gauge, SampleCollector, TimeSeries
from ..units import PS_PER_S


class Monitor:
    """全局指标采集器。"""

    def __init__(
        self,
        master_names: list[str],
        slave_names: list[str],
        bucket_ps: int = 1_000_000,  # 默认 1µs 一个桶
    ):
        self.bucket_ps = bucket_ps
        self.master_names = list(master_names)
        self.slave_names = list(slave_names)

        # --- 带宽：标量汇总 ---
        self.bytes_by_master: dict[str, int] = {m: 0 for m in self.master_names}
        self.bytes_by_slave: dict[str, int] = {s: 0 for s in self.slave_names}
        self.bytes_matrix: dict[tuple[str, str], int] = {}
        self.bytes_by_access: dict[str, int] = {"rd": 0, "wr": 0}
        self.bytes_master_access: dict[tuple[str, str], int] = {}
        self.bursts_by_master: dict[str, int] = {m: 0 for m in self.master_names}

        # --- 带宽：时序 ---
        self.ts_total_rd = TimeSeries("bw_total_rd", bucket_ps)
        self.ts_total_wr = TimeSeries("bw_total_wr", bucket_ps)
        self.ts_master: dict[str, TimeSeries] = {
            m: TimeSeries(f"bw_{m}", bucket_ps) for m in self.master_names
        }
        self.ts_slave: dict[str, TimeSeries] = {
            s: TimeSeries(f"bw_{s}", bucket_ps) for s in self.slave_names
        }

        # --- 延迟分布 ---
        self.lat_master: dict[str, SampleCollector] = {
            m: SampleCollector(f"lat_{m}") for m in self.master_names
        }
        self.lat_slave: dict[str, SampleCollector] = {
            s: SampleCollector(f"lat_{s}") for s in self.slave_names
        }
        self.lat_class: dict[str, SampleCollector] = {
            c.label: SampleCollector(f"lat_{c.label}") for c in TrafficClass
        }
        # 延迟分段（用于归因：时间花在等待还是服务）
        self.wait_master: dict[str, SampleCollector] = {
            m: SampleCollector(f"waitm_{m}") for m in self.master_names
        }
        self.wait_slave: dict[str, SampleCollector] = {
            s: SampleCollector(f"waits_{s}") for s in self.slave_names
        }

        # --- 占用率 ---
        self.outstanding = {
            m: Gauge(f"ot_{m}", trace=True) for m in self.master_names
        }
        # 队列深度开轨迹：时序图需要它，而且深度变化不频繁，轨迹很小
        self.queue_depth = {
            s: Gauge(f"q_{s}", trace=True) for s in self.slave_names
        }

        # --- 守恒 ---
        self.issued_bytes = 0
        self.completed_bytes = 0

        # --- 请求队列等待（在 master port 里排队的时间）---
        self.stall_samples: dict[str, SampleCollector] = {
            m: SampleCollector(f"stall_{m}") for m in self.master_names
        }

        self._t_last = 0

    # --- 预热 ---

    def reset_stats(self, t_ps: int) -> None:
        """清空统计并重新开始计时。用于**预热**：先让系统进入稳态，再开始测量。

        不设预热的后果是延迟分位数被启动瞬态污染——尤其是 PSRAM 这种高延迟设备，
        头几个事务的延迟会显著拉高 p50/p90，而它们不代表稳态行为。

        ``Gauge`` 不能简单清零：它必须从**当前的实际在途数**继续积分，否则
        Little's Law 校验会因为丢掉一段面积而系统性偏移。
        """
        self._t_last = t_ps
        self.issued_bytes = 0
        self.completed_bytes = 0

        for d in (self.bytes_by_master, self.bytes_by_slave, self.bytes_by_access,
                  self.bursts_by_master):
            for k in list(d):
                d[k] = 0
        self.bytes_matrix.clear()
        self.bytes_master_access.clear()

        # 时序重新开始
        self.ts_total_rd = TimeSeries("bw_total_rd", self.bucket_ps)
        self.ts_total_wr = TimeSeries("bw_total_wr", self.bucket_ps)
        for m in self.master_names:
            self.ts_master[m] = TimeSeries(f"bw_{m}", self.bucket_ps)
        for s in self.slave_names:
            self.ts_slave[s] = TimeSeries(f"bw_{s}", self.bucket_ps)

        # 分布重新开始
        for m in self.master_names:
            self.lat_master[m] = SampleCollector(f"lat_{m}")
            self.wait_master[m] = SampleCollector(f"waitm_{m}")
            self.stall_samples[m] = SampleCollector(f"stall_{m}")
        for s in self.slave_names:
            self.lat_slave[s] = SampleCollector(f"lat_{s}")
            self.wait_slave[s] = SampleCollector(f"waits_{s}")
        for c in TrafficClass:
            self.lat_class[c.label] = SampleCollector(f"lat_{c.label}")

        # Gauge：把统计原点移到预热结束时刻。
        # 不能清零——必须从当前实际值继续积分，否则 Little's Law 校验会丢一段面积。
        for g in self.outstanding.values():
            g.reset(t_ps)
        for g in self.queue_depth.values():
            g.reset(t_ps)

    # --- 事件钩子 ---

    def record_created(self, burst: AxiBurst, t_ps: int) -> None:
        """主设备**想**发起一个事务（进了自己的请求缓冲）。

        **不动在途计数**——此刻它还不是 outstanding。这是"想发起"的时刻戳，
        端口停滞时长要等握手完成才能算出来，所以在 ``record_outstanding`` 里采样。
        """

    def record_outstanding(self, burst: AxiBurst, t_ps: int) -> None:
        """事务被互联接受（AR/AW 握手完成）→ **成为 outstanding**。

        ★ 这是并发计数的起点，与 ``latency_ps`` 的起点（``t_issue``，由
        ``MasterPort.take_ready`` 在握手完成时设定）严格一致。两者同源，
        Little's Law 才闭合。用提交时刻起算会让误差与 outstanding 压力成正比——
        一个很容易被误读成模型物理误差的假象。
        """
        g = self.outstanding.get(burst.master)
        if g is not None:
            g.add(1, t_ps)
        self.issued_bytes += burst.nbytes

        # 此刻才能算出主设备在端口等了多久
        s = self.stall_samples.get(burst.master)
        if s is not None:
            s.add(burst.port_stall_ps)

    def record_complete(self, burst: AxiBurst, t_ps: int, queue_depth: int = 0) -> None:
        """事务完成。这里是所有带宽与延迟统计的唯一入口。"""
        self._t_last = t_ps

        g = self.outstanding.get(burst.master)
        if g is not None:
            g.add(-1, t_ps)

        # 守恒
        self.completed_bytes += burst.nbytes

        # 带宽
        self.bytes_by_master[burst.master] = (
            self.bytes_by_master.get(burst.master, 0) + burst.nbytes
        )
        self.bytes_by_slave[burst.slave] = self.bytes_by_slave.get(burst.slave, 0) + burst.nbytes
        key = (burst.master, burst.slave)
        self.bytes_matrix[key] = self.bytes_matrix.get(key, 0) + burst.nbytes
        self.bursts_by_master[burst.master] = self.bursts_by_master.get(burst.master, 0) + 1

        ac = burst.access.label
        self.bytes_by_access[ac] = self.bytes_by_access.get(ac, 0) + burst.nbytes
        ka = (burst.master, ac)
        self.bytes_master_access[ka] = self.bytes_master_access.get(ka, 0) + burst.nbytes

        # 时序
        if burst.access.label == "rd":
            self.ts_total_rd.add(t_ps, burst.nbytes)
        else:
            self.ts_total_wr.add(t_ps, burst.nbytes)
        ts_m = self.ts_master.get(burst.master)
        if ts_m is not None:
            ts_m.add(t_ps, burst.nbytes)
        ts_s = self.ts_slave.get(burst.slave)
        if ts_s is not None:
            ts_s.add(t_ps, burst.nbytes)

        # 延迟分布
        lat = burst.latency_ps
        lm = self.lat_master.get(burst.master)
        if lm is not None:
            lm.add(lat)
        ls = self.lat_slave.get(burst.slave)
        if ls is not None:
            ls.add(lat)
        lc = self.lat_class.get(burst.traffic_class.label)
        if lc is not None:
            lc.add(lat)

        wm = self.wait_master.get(burst.master)
        if wm is not None:
            wm.add(burst.wait_master_ps)
        ws = self.wait_slave.get(burst.slave)
        if ws is not None:
            ws.add(burst.wait_slave_ps)

        # 队列深度
        qg = self.queue_depth.get(burst.slave)
        if qg is not None:
            qg.set(queue_depth, t_ps)

    # --- 结果 ---

    @property
    def t_end(self) -> int:
        return self._t_last

    def bandwidth_gbps(self, nbytes: int, t_end_ps: int) -> float:
        if t_end_ps <= 0:
            return 0.0
        return nbytes / (t_end_ps / PS_PER_S) / 1e9

    def achieved_gbps_by_master(self, t_end_ps: int) -> dict[str, float]:
        return {
            m: self.bandwidth_gbps(b, t_end_ps) for m, b in self.bytes_by_master.items()
        }

    def achieved_gbps_by_slave(self, t_end_ps: int) -> dict[str, float]:
        return {s: self.bandwidth_gbps(b, t_end_ps) for s, b in self.bytes_by_slave.items()}

    def matrix_gbps(self, t_end_ps: int) -> dict[str, dict[str, float]]:
        """主设备 × 从设备的带宽矩阵。热力图的数据源。"""
        out: dict[str, dict[str, float]] = {m: {} for m in self.master_names}
        for (m, s), b in self.bytes_matrix.items():
            out.setdefault(m, {})[s] = self.bandwidth_gbps(b, t_end_ps)
        return out

    @property
    def conservation_residual(self) -> int:
        """发起字节数 − 完成字节数。等于仿真结束时**仍在途**的字节数。

        严格守恒校验要拿这个残差跟端口里实际的在途字节数比对——见
        ``conservation_check``。只看"相等"会把运行结束时正常的在途事务误判成丢包。
        """
        return self.issued_bytes - self.completed_bytes

    def conservation_check(self, inflight_bytes_at_end: int) -> dict[str, object]:
        """★ 三重自校验之一：守恒校验。

        恒等式：``发起字节数 − 完成字节数 == 仿真结束时仍在途的字节数``

        不成立意味着丢包、重复计数或时间戳错误——最严重的模型 bug。
        """
        residual = self.conservation_residual
        ok = residual == inflight_bytes_at_end
        return {
            "ok": ok,
            "issued_bytes": self.issued_bytes,
            "completed_bytes": self.completed_bytes,
            "residual_bytes": residual,
            "inflight_bytes_at_end": inflight_bytes_at_end,
            "mismatch": residual - inflight_bytes_at_end,
        }

    def little_law_check(self, t_end_abs_ps: int, window_ps: int) -> dict[str, dict[str, float]]:
        """★ 三重自校验之一：Little's Law 校验。

        对每个主设备：

            实测并发数（outstanding 的时间加权平均）
                ?= 完成事务率 × 平均延迟

        **量纲要点：Little's Law 的单位是「事务数」，不是字节数。** 用字节会得到
        "在途字节数"，那是带宽延迟积，不是本定律里的 L。两者差一个平均 burst 字节数。

        **时间基准要点**：``Gauge.time_avg`` 需要**绝对时刻**（它的原点是预热结束时刻），
        而速率需要**测量窗口长度**。这两个量不同，混用会让误差恰好等于
        ``window/total`` 那个比例——一个很容易被误当成模型物理误差的假象。

        **用的是确定性样本路径版本**：在任意有限窗口成立，不需要平稳性假设。这对
        SoC 流量是必须的——帧同步流量是周期性非平稳的，随机版本在这里不成立。
        """
        out = {}
        window_s = window_ps / PS_PER_S if window_ps > 0 else 0.0
        for m in self.master_names:
            g = self.outstanding[m]
            measured = g.time_avg(t_end_abs_ps)
            if window_s <= 0:
                out[m] = {
                    "measured": measured, "predicted": float("nan"),
                    "error_pct": float("nan"),
                }
                continue
            n_bursts = self.bursts_by_master.get(m, 0)
            arrival_rate = n_bursts / window_s  # 事务/秒
            lat_mean_ps = self.lat_master[m].mean if len(self.lat_master[m]) else 0.0
            mean_lat_s = lat_mean_ps / PS_PER_S
            predicted = arrival_rate * mean_lat_s
            denom = max(measured, 1e-9)
            out[m] = {
                "measured": round(measured, 4),
                "predicted": round(predicted, 4),
                "error_pct": round((predicted - measured) / denom * 100.0, 2),
                "bursts": n_bursts,
                "arrival_rate_per_s": round(arrival_rate, 1),
                "mean_latency_ps": round(lat_mean_ps, 1),
            }
        return out

    def little_law_worst_error_pct(self, t_end_abs_ps: int, window_ps: int) -> float:
        """最大的 Little's Law 相对误差。校验套件用它做单值判据。"""
        checks = self.little_law_check(t_end_abs_ps, window_ps)
        errs = []
        for d in checks.values():
            e = d.get("error_pct")
            if isinstance(e, (int, float)) and e == e:  # 排除 NaN
                errs.append(abs(e))
        return max(errs) if errs else 0.0

    def upper_bound_check(self, slave_caps_gbps: dict[str, float], t_end_ps: int) -> list[str]:
        """★ 三重自校验之一：上界校验。

        达成带宽不得超过该从设备的峰值。违反即模型 bug（重复计数、时间戳错误等）。
        """
        violations = []
        for s, cap in slave_caps_gbps.items():
            ach = self.bandwidth_gbps(self.bytes_by_slave.get(s, 0), t_end_ps)
            if ach > cap * 1.001:  # 留 0.1% 给取整
                violations.append(
                    f"[{s}] 达成带宽 {ach:.4f} GB/s 超过峰值 {cap:.4f} GB/s"
                    f"（超出 {(ach/cap - 1)*100:.2f}%）——模型 bug"
                )
        return violations

    def summary(self, t_end_ps: int) -> dict[str, object]:
        return {
            "t_end_ps": t_end_ps,
            "total_gbps": self.bandwidth_gbps(self.completed_bytes, t_end_ps),
            "rd_gbps": self.bandwidth_gbps(self.bytes_by_access.get("rd", 0), t_end_ps),
            "wr_gbps": self.bandwidth_gbps(self.bytes_by_access.get("wr", 0), t_end_ps),
            "per_master_gbps": {
                m: round(v, 4) for m, v in self.achieved_gbps_by_master(t_end_ps).items()
            },
            "per_slave_gbps": {
                s: round(v, 4) for s, v in self.achieved_gbps_by_slave(t_end_ps).items()
            },
            "conservation_residual_bytes": self.conservation_residual,
            "issued_bytes": self.issued_bytes,
            "completed_bytes": self.completed_bytes,
        }
