"""不变量测试 —— 任何情况下都必须成立的性质。

这些不是"某个配置下结果对不对"，而是"模型在任何配置下都不能违反的约束"。
违反它们意味着模型有实现 bug，而不是参数设得不好。
"""

from __future__ import annotations

import pytest

from socsim.analysis import build_waterfall, diagnose_run
from socsim.config import load_config
from socsim.soc import Soc

from .conftest import make_config, two_master_config


# --- 三重自校验 ---


def test_conservation_holds_on_reference_config(reference_config_path):
    """★ 守恒校验：发起字节数 − 完成字节数 == 当前在途 − 预热时在途。

    这是最硬的一条不变量。不成立意味着丢包、重复计数或时间戳错误。
    """
    res = Soc(load_config(str(reference_config_path))).run()
    c = res.conservation
    assert c["ok"], (
        f"守恒校验失败：偏差 {c['mismatch_bytes']} 字节。"
        f"发起 {c['issued_bytes']} − 完成 {c['completed_bytes']} = {c['residual_bytes']}，"
        f"但期望 {c['expected_residual_bytes']}（当前在途 {c['inflight_now_bytes']} − "
        f"预热时在途 {c['inflight_at_warmup_bytes']}）"
    )


def test_upper_bound_never_violated(reference_config_path):
    """★ 上界校验：达成带宽不得超过从设备峰值。"""
    res = Soc(load_config(str(reference_config_path))).run()
    violations = res.bandwidth_check()
    assert not violations, "上界校验失败：\n" + "\n".join(violations)


def test_little_law_within_tolerance(reference_config_path):
    """★ Little's Law 校验：实测并发数 == 完成事务率 × 平均延迟。

    用的是**确定性样本路径版本**——在任意有限窗口成立，不需要平稳性假设。
    这对 SoC 流量是必须的：帧同步流量是周期性非平稳的，随机版本在这里不成立。

    容差 2%：剩余偏差来自窗口边界效应（预热时在途的事务，其延迟有一部分落在窗口外），
    量级约为 (L₀+L₁)·W̄/窗口。窗口越长这个效应越小。
    """
    res = Soc(load_config(str(reference_config_path))).run()
    worst = res.little_law_worst_error_pct()
    assert worst < 2.0, (
        f"Little's Law 最大误差 {worst:.2f}% 超过 2%。"
        f"明细：{res.little_law()}"
    )


# --- 仲裁语义 ---


def test_drr_gives_per_byte_fairness_across_burst_sizes():
    """★ DRR 的核心正确性：**每字节公平**。

    两个饱和源，一个发 8 字节 burst、一个发 256 字节 burst，纯读、目标同一从设备。

    - DRR（每字节公平）→ 两者字节份额应接近 50/50
    - RR（每事务公平）→ 小 burst 那路每字节只拿到 1/32 的机会，份额会崩塌

    这条测试是"为什么默认必须是 DRR"的量化证据，也是模型里最容易写错语义的地方。
    """
    cfg = two_master_config(policy="drr")
    res = Soc(cfg).run()
    arb = res.crossbar.arbiters["psram0"]
    share = arb.stats.bytes_share()

    # 两个 master 的字节份额应接近相等
    assert abs(share["m0"] - share["m1"]) < 0.15, (
        f"DRR 下字节份额应接近相等，实际 m0={share['m0']:.3f} m1={share['m1']:.3f}。"
        f"DRR 的每字节公平语义没有实现正确。"
    )


def test_rr_is_unfair_across_burst_sizes():
    """反向验证：RR 在 burst 几何不一致时**应该**不公平。

    如果这条测试失败（RR 也变得公平了），说明 DRR 和 RR 被实现成了同一个东西——
    那"默认用 DRR"这个决策就失去了依据，也说明测试本身没有区分能力。
    """
    cfg = two_master_config(policy="rr")
    res = Soc(cfg).run()
    arb = res.crossbar.arbiters["psram0"]
    share = arb.stats.bytes_share()

    # 小 burst 的 m0 应该明显吃亏
    assert share["m0"] < share["m1"] * 0.6, (
        f"RR 下小 burst 的 m0 应明显吃亏，实际 m0={share['m0']:.3f} m1={share['m1']:.3f}。"
        f"两者接近相等说明 RR 没有在做每事务公平——测试失去了区分能力。"
    )


def test_arbiter_never_grants_more_bytes_than_granted(reference_config_path):
    """仲裁统计的内在一致性：字节份额之和为 1。"""
    res = Soc(load_config(str(reference_config_path))).run()
    for slave_name, arb in res.crossbar.arbiters.items():
        share = arb.stats.bytes_share()
        total = sum(share.values())
        assert abs(total - 1.0) < 1e-6 or total == 0.0, (
            f"[{slave_name}] 仲裁字节份额之和为 {total}，应为 1.0"
        )


def test_grants_match_slave_accepts(reference_config_path):
    """交叉开关授权的字节数应等于从设备受理的字节数。

    两者不等意味着授权后的事务没有真正进入从设备——丢事务。
    """
    res = Soc(load_config(str(reference_config_path))).run()
    granted = res.crossbar.granted_bytes
    accepted = sum(d.bytes_accepted for d in res.slaves.values())
    assert granted == accepted, (
        f"授权 {granted} 字节 ≠ 从设备受理 {accepted} 字节，差 {granted - accepted}"
    )


# --- 同 ID 保序 ---


def test_same_id_ordering_is_respected():
    """★ 同 ID 保序：同一 (主设备, ID) 的完成顺序必须与发起顺序一致。

    这条约束让 ID 数量成为真实的并发瓶颈，也是队头阻塞的来源。模型必须遵守它，
    否则会高估带宽。

    注意 ID 是**每个主设备独立**的：m0 的 ID=0 与 m1 的 ID=0 是两条无关的流。
    """
    cfg = make_config()
    for m in cfg.masters.values():
        m.n_ids = 2            # 只用 2 个 ID，强制产生保序压力
        m.max_outstanding = 16
        m.port.rd_ratio = 0.5  # 混合读写，让服务顺序有变化空间

    soc = Soc(cfg)
    completions: list[tuple[str, int, int, int]] = []  # (master, id, seq, t_done)
    for dev in soc.slaves.values():
        orig = dev._on_complete

        def spy(burst, _orig=orig):
            completions.append((burst.master, burst.axi_id, burst.seq, burst.t_done))
            _orig(burst)

        dev._on_complete = spy

    soc.run()

    assert completions, "没有采集到任何完成事件"
    by_stream: dict[tuple[str, int], list[tuple[int, int]]] = {}
    for master, axi_id, seq, t_done in completions:
        by_stream.setdefault((master, axi_id), []).append((seq, t_done))

    for (master, axi_id), rows in by_stream.items():
        rows.sort()  # 按 seq 排
        times = [t for _, t in rows]
        assert times == sorted(times), (
            f"master={master} id={axi_id} 的完成时刻非单调——同 ID 保序被违反。"
            f"明细（seq, t_done）前几项：{rows[:6]}"
        )


# --- 写缓冲滞回 ---


def test_write_drain_avoids_direction_thrashing():
    """★ 写缓冲的水位滞回应把方向切换摊薄，而不是每事务切一次。

    判据：平均方向连续长度应显著大于 1。若接近 1，说明滞回没有生效——
    那会让翻转开销吃掉大半带宽（实测过：1.84 的连续长度下，翻转占了 64% 的时间）。
    """
    cfg = make_config(envelope_overrides={
        "turnaround_ns": 120.0, "write_buffer": 4096,
    })
    for m in cfg.masters.values():
        m.port.rd_ratio = 0.5   # 读写各半，最容易触发频繁翻转
        m.port.target_gbps = 0.0
        m.port.pattern = "saturating"

    res = Soc(cfg).run()
    ps = res.slaves["psram0"]
    run_len = ps.mean_direction_run()

    # A/B 对比：关掉方向批处理（等价于没有写缓冲滞回）作为对照。
    # 这比"跑一个数看它是否超过某个拍脑袋的阈值"有说服力得多——
    # 它直接量化了滞回机制的作用。
    cfg_b = make_config(envelope_overrides={
        "turnaround_ns": 120.0, "write_buffer": 4096,
    })
    cfg_b.envelopes["test_env"].direction_batching = False
    for m in cfg_b.masters.values():
        m.port.rd_ratio = 0.5
        m.port.target_gbps = 0.0
        m.port.pattern = "saturating"
    res_b = Soc(cfg_b).run()
    ps_b = res_b.slaves["psram0"]
    bw_b = res_b.monitor.bandwidth_gbps(res_b.monitor.bytes_by_slave["psram0"], res_b.t_measured_ps)
    bw_a = res.monitor.bandwidth_gbps(res.monitor.bytes_by_slave["psram0"], res.t_measured_ps)

    assert run_len > 4.0, (
        f"平均方向连续长度仅 {run_len:.2f}，写缓冲滞回没有生效。"
        f"翻转 {ps.turnaround_events} 次，占窗口 "
        f"{ps.turnaround_ps_total / res.t_measured_ps:.1%}"
    )
    turn_frac = ps.turnaround_ps_total / res.t_measured_ps
    assert turn_frac < 0.35, (
        f"翻转开销占窗口 {turn_frac:.1%}。理论上限约为 "
        f"turnaround/(data_per_burst*run_len + turnaround)，据此判断是否异常"
    )
    # 有滞回必须显著优于没有滞回
    assert bw_a > bw_b * 1.3, (
        f"写缓冲滞回没有带来预期收益：有滞回 {bw_a:.3f} GB/s vs "
        f"无滞回 {bw_b:.3f} GB/s（连续长度 {ps_b.mean_direction_run():.2f}）"
    )


def test_turnaround_zero_means_no_turnaround_cost():
    """退化用例：翻转开销设为 0 时不应有任何翻转损失。"""
    cfg = make_config(envelope_overrides={"turnaround_ns": 0.0})
    for m in cfg.masters.values():
        m.port.rd_ratio = 0.5
    res = Soc(cfg).run()
    ps = res.slaves["psram0"]
    assert ps.turnaround_ps_total == 0, (
        f"翻转开销为 0 却累计了 {ps.turnaround_ps_total}ps"
    )


# --- 诊断层自洽 ---


def test_waterfall_does_not_double_count(reference_config_path):
    """★ 瀑布分解不得重复计数：各项之和 + 有效带宽 ≤ 峰值。

    重复计数是这类分解最容易犯的错（比如把已经包含在 sustained 里的命令开销
    再单独列一项）。检查器必须能抓到它。
    """
    res = Soc(load_config(str(reference_config_path))).run()
    mon = res.monitor
    for name, dev in res.slaves.items():
        eff = mon.bandwidth_gbps(mon.bytes_by_slave.get(name, 0), res.t_measured_ps)
        wf = build_waterfall(dev, res.t_measured_ps, eff, 0)
        assert not wf.errors, f"[{name}] 瀑布分解自检失败：{wf.errors}"


def test_diagnosis_classification_is_exclusive(reference_config_path):
    """诊断分类必须互斥且落在已知集合内。"""
    valid = {"concurrency_limited", "turnaround_bound", "device_limited", "mixed", "underloaded"}
    res = Soc(load_config(str(reference_config_path))).run()
    rep = diagnose_run(res)
    for d in rep.slaves:
        assert d.classification in valid, f"[{d.slave}] 未知分类 {d.classification!r}"
    for d in rep.masters:
        assert d.classification in {
            "own_outstanding_limited", "fabric_stalled",
            "achieving_target", "starved", "unconstrained",
        }, f"[{d.master}] 未知分类 {d.classification!r}"


def test_outstanding_never_exceeds_limit(reference_config_path):
    """在途数不得超过配置上限——否则并发数是被凭空创造的。

    这条直接关系到 Little's Law 校验的可信度：如果实测并发能超过硬件上限，
    那说明计数区间与限流区间不一致。
    """
    res = Soc(load_config(str(reference_config_path))).run()
    for name, port in res.ports.items():
        avg = res.monitor.outstanding[name].time_avg(res.t_end_ps)
        peak = res.monitor.outstanding[name].peak
        assert peak <= port.max_outstanding, (
            f"[{name}] 在途峰值 {peak} 超过上限 {port.max_outstanding}"
        )
        assert avg <= port.max_outstanding, (
            f"[{name}] 在途均值 {avg:.2f} 超过上限 {port.max_outstanding}"
        )
