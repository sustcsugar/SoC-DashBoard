"""测试夹具。

测试的价值不在于覆盖率，而在于**能证伪**。这里的测试分三类：

1. **已知答案测试**：构造一个结果可以手算的场景，检查模型是否给出那个数。
   例：单主设备纯读、outstanding 充足时，达成带宽必须精确等于 sustained。
2. **不变量测试**：某些量在任何情况下都必须成立。例：守恒、上界、DRR 的每字节公平、
   同 ID 保序。
3. **退化用例**：极端配置下不应该崩或给出荒谬值。例：1×1 交叉开关、零流量。

**这三类里最容易写、也最容易漏的是第 1 类。** 一个仿真器能跑出漂亮的曲线，不等于
它算得对——已知答案测试是唯一能区分这两者的东西。
"""

from __future__ import annotations

import pytest

from socsim.config import SocConfig


def envelope(
    *,
    name: str = "test_env",
    peak: float = 4.0,
    rd: float = 3.2,
    wr: float = 2.6,
    lat_ns: float = 100.0,
    ot_rd: int = 16,
    ot_wr: int = 16,
    turnaround_ns: float = 0.0,
    write_buffer: int = 4096,
    burst_bytes: int = 256,
) -> dict:
    return {
        "name": name,
        "peak_gbps": peak,
        "sustained_rd_gbps": rd,
        "sustained_wr_gbps": wr,
        "latency_idle_ns": lat_ns,
        "max_outstanding_rd": ot_rd,
        "max_outstanding_wr": ot_wr,
        "rd_wr_turnaround_ns": turnaround_ns,
        "cmd_overhead_ns": 0.0,
        "refresh_overhead": 0.0,
        "max_burst_bytes": burst_bytes,
        "write_buffer_bytes": write_buffer,
        "lookahead_depth": 32,
    }


def make_config(
    *,
    duration_us: float = 100.0,
    warmup_us: float = 10.0,
    seed: int = 42,
    envelope_overrides: dict | None = None,
    master_overrides: dict | None = None,
    policy: str = "drr",
    n_masters: int = 1,
) -> SocConfig:
    """构造一个最小可用的单从设备配置。

    默认：1 个 PSRAM 从设备 + ``n_masters`` 个主设备，全部发往 psram0。
    """
    env = envelope(**(envelope_overrides or {}))

    masters = {}
    for i in range(n_masters):
        name = "m0" if n_masters == 1 else f"m{i}"
        base = {
            "port": {
                "traffic_class": "bandwidth",
                "target_gbps": 0.0,
                "pattern": "saturating",
                "rd_ratio": 1.0,
                "burst_len": 31,          # 32 拍
                "burst_size": 3,          # 8B/拍 → 256B/burst
                "addr_mode": "sequential",
                "targets": {"psram0": 1.0},
                "base_addr": 0,
                "size_bytes": 1 << 22,
                "qos": 0,
            },
            "queue_depth": 32,
            "max_outstanding": 64,
            "n_ids": 16,
        }
        if master_overrides:
            base.update(master_overrides)
        masters[name] = base

    raw = {
        "run": {
            "name": "test",
            "duration_us": duration_us,
            "warmup_us": warmup_us,
            "seed": seed,
            "bucket_ns": 500.0,
        },
        "clocks": {"clk": {"freq_mhz": 400.0, "width_b": 8}},
        "envelopes": {"test_env": env},
        "interconnect": {
            "name": "xbar",
            "policy": policy,
            "arb_latency_cycles": 2,
            "clock": "clk",
            "max_burst_bytes": 256,
        },
        "slaves": {
            "psram0": {
                "kind": "hardmac",
                "envelope": "test_env",
                "queue_depth": 64,
                "base_addr": 0,
                "size_bytes": 1 << 24,
            }
        },
        "masters": masters,
    }
    return SocConfig.model_validate(raw)


def two_master_config(**kw) -> SocConfig:
    """两个几何形状**不同**的主设备，用于验证 DRR 的每字节公平。

    m0 发 1 拍 burst（8 字节），m1 发 32 拍 burst（256 字节）。

    ★ 关键：两个主设备的 ``max_outstanding`` 都必须**远高于**系统能提供的并发，
    否则量到的是它们各自的 outstanding 上限，而不是仲裁公平性。
    m0 若要拿到一半字节，需要 8 倍于 m1 的事务数——64 个 outstanding 只能支撑
    512 字节在途，远不足以让它参与竞争。
    """
    cfg = make_config(n_masters=2, **kw)
    cfg.masters["m0"].port.burst_len = 0    # 1 拍 → 8 字节
    cfg.masters["m0"].port.burst_size = 3
    cfg.masters["m1"].port.burst_len = 31   # 32 拍 → 256 字节
    cfg.masters["m1"].port.burst_size = 3
    cfg.masters["m0"].port.rd_ratio = 1.0
    cfg.masters["m1"].port.rd_ratio = 1.0
    # 不能让自己成为瓶颈：给足 outstanding 与请求队列
    for name in ("m0", "m1"):
        cfg.masters[name].max_outstanding = 512
        cfg.masters[name].queue_depth = 256
        cfg.masters[name].n_ids = 32
    return cfg


@pytest.fixture
def single_reader():
    return make_config()


@pytest.fixture
def reference_config_path():
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / "configs" / "reference_ap.yaml"
