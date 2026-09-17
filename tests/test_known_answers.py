"""已知答案测试 —— 能证伪模型的那一类。

一个仿真器能跑出漂亮的曲线，不等于它算得对。这些测试构造了结果可以**手算**的场景，
检查模型是否给出那个数。如果它们挂了，说明模型的物理语义错了，后面所有的分析结论
都不能信。
"""

from __future__ import annotations

import pytest

from socsim.monitor.monitor import Monitor
from socsim.soc import Soc

from .conftest import make_config

TOL = 0.03  # 3% 容差：允许调度开销和窗口边界效应


def test_single_master_pure_read_hits_sustained(single_reader):
    """★ 核心已知答案：单主设备、纯读、outstanding 充足 → 精确等于读持续带宽。

    这是整个模型最重要的一条性质。它成立意味着：
    服务时间确实由 sustained_rate 导出，延迟没有意外地限制带宽，
    仲裁没有引入额外损耗。

    它不成立则说明模型把某个不该算的东西算进了关键路径。
    """
    res = Soc(single_reader).run()
    achieved = res.monitor.bandwidth_gbps(
        res.monitor.bytes_by_slave["psram0"], res.t_measured_ps
    )
    expected = single_reader.envelopes["test_env"].sustained_rd_gbps

    assert abs(achieved - expected) / expected < TOL, (
        f"纯读达成带宽 {achieved:.4f} GB/s 偏离 sustained {expected:.4f} GB/s "
        f"超过 {TOL:.0%}。模型的服务时间推导或延迟处理有问题。"
    )


def test_single_master_pure_write_hits_sustained():
    """写方向的同一性质。写还要等 B 响应，容易在实现里被漏掉。"""
    cfg = make_config(master_overrides={"port": {
        "traffic_class": "bandwidth", "target_gbps": 0.0, "pattern": "saturating",
        "rd_ratio": 0.0, "burst_len": 31, "burst_size": 3,
        "addr_mode": "sequential", "targets": {"psram0": 1.0},
        "base_addr": 0, "size_bytes": 1 << 22, "qos": 0,
    }})
    res = Soc(cfg).run()
    achieved = res.monitor.bandwidth_gbps(
        res.monitor.bytes_by_slave["psram0"], res.t_measured_ps
    )
    expected = cfg.envelopes["test_env"].sustained_wr_gbps
    assert abs(achieved - expected) / expected < TOL, (
        f"纯写达成带宽 {achieved:.4f} 偏离 sustained {expected:.4f}"
    )


def test_bandwidth_delay_product_is_the_real_limit():
    """★ 用 Little's Law 反推：outstanding 不足时，带宽必须被 BDP 封顶。

    构造：延迟 1000ns、sustained 3.2 GB/s、max_outstanding=2、burst=256B。
    在途上限 = 2 × 256B = 512B。
    Little's Law 预测带宽 = 512B / 1000ns = 0.512 GB/s。

    若模型给出明显更高的值，说明它在某处凭空创造了并发。
    """
    cfg = make_config(
        envelope_overrides={"lat_ns": 1000.0, "ot_rd": 2, "ot_wr": 2},
    )
    res = Soc(cfg).run()
    achieved = res.monitor.bandwidth_gbps(
        res.monitor.bytes_by_slave["psram0"], res.t_measured_ps
    )
    in_flight_bytes = 2 * 256
    predicted = in_flight_bytes / (1000e-9) / 1e9  # GB/s

    assert achieved <= predicted * (1 + TOL), (
        f"达成 {achieved:.4f} GB/s 超过了 Little's Law 上限 {predicted:.4f} GB/s "
        f"（在途上限 {in_flight_bytes}B / 延迟 1000ns）。模型凭空创造了并发。"
    )
    # 同时也应该真的逼近这个上限（否则说明另有瓶颈，测试就没在测想测的东西）
    assert achieved > predicted * 0.85, (
        f"达成 {achieved:.4f} 远低于 Little's Law 上限 {predicted:.4f}，"
        f"说明还有别的瓶颈在起作用，这个测试没有验证到目标性质"
    )


def test_zero_traffic_gives_zero_bandwidth_and_no_events_storm():
    """退化用例：所有主设备关闭。不应产生带宽，也不应卡住或爆炸。

    注意用 ``enabled: false`` 而不是把速率设成 0 —— 后者语义是"饱和源"，
    配置校验会直接拒绝，正是为了避免这个歧义。
    """
    cfg = make_config()
    for m in cfg.masters.values():
        m.port.enabled = False

    res = Soc(cfg).run()
    assert res.monitor.bytes_by_slave["psram0"] == 0
    assert res.monitor.completed_bytes == 0


def test_1x1_crossbar_has_no_arbitration_loss(single_reader):
    """退化用例：1 主设备 1 从设备时，仲裁不应引入任何损耗。

    如果这个测试挂了，说明仲裁路径在只有一路时也在"公平地"分时——那是个实现 bug，
    真实交叉开关在单请求时是无条件授权的。
    """
    res = Soc(single_reader).run()
    ps = res.slaves["psram0"]
    # 单一主设备不应该被反压
    assert res.crossbar.backpressure_events == 0, (
        f"1×1 场景出现了 {res.crossbar.backpressure_events} 次反压"
    )
    # 总线应该几乎不空闲
    idle_frac = ps.bus_idle_ps / res.t_measured_ps
    assert idle_frac < 0.02, f"1×1 场景总线空闲 {idle_frac:.1%}，不该有这么多"


def test_offered_rate_below_capacity_is_achieved_exactly():
    """速率受限源：目标低于容量时，达成应精确等于目标（容许 3%）。

    这条验证的是"主设备能拿到它要的"，而不是"总能拿到最多"。
    """
    cfg = make_config()
    for m in cfg.masters.values():
        m.port.target_gbps = 1.0
        m.port.pattern = "poisson"

    res = Soc(cfg).run()
    achieved = res.monitor.bandwidth_gbps(
        res.monitor.bytes_by_slave["psram0"], res.t_measured_ps
    )
    assert abs(achieved - 1.0) / 1.0 < TOL, (
        f"目标 1.0 GB/s（容量 3.2 GB/s）时达成 {achieved:.4f}，偏离目标超过 {TOL:.0%}"
    )


def test_measurement_window_excludes_warmup():
    """预热必须真的把统计清零：窗口长度与统计口径要对得上。"""
    cfg = make_config(duration_us=100.0, warmup_us=10.0)
    res = Soc(cfg).run()
    assert res.t_end_ps == 110_000_000, f"仿真结束时刻应为 110µs，得到 {res.t_end_ps/1e6}µs"
    assert res.t_measured_ps == 100_000_000, (
        f"测量窗口应为 100µs，得到 {res.t_measured_ps/1e6}µs"
    )
