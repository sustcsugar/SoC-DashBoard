"""可复现的随机源。

**按组件独立种子**，而不是一个全局随机流。原因：扫掠时要对比"只改了 NPU 的流量参数"
的两个配置，如果共用一条全局随机流，改动会让后续所有组件的随机序列全部错位，两次运行
的差异就不再可归因于那次改动。

组件名到种子的映射用 SHA-256 而不是内置 ``hash()``——内置哈希带进程级随机化，
跨运行不稳定。
"""

from __future__ import annotations

import hashlib
import random


def _name_seed(master_seed: int, name: str) -> int:
    """由主种子和组件名导出一个稳定的子种子。"""
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return master_seed ^ int.from_bytes(digest[:8], "big")


class RngPool:
    """组件名 → 独立且可复现的 ``random.Random`` 实例。

    同一个名字每次拿到的是同一个实例，所以一个组件在自己的生命周期内序列是连续的；
    不同名字之间互不干扰。
    """

    def __init__(self, master_seed: int = 12345):
        self.master_seed = master_seed
        self._streams: dict[str, random.Random] = {}

    def get(self, name: str) -> random.Random:
        stream = self._streams.get(name)
        if stream is None:
            stream = random.Random(_name_seed(self.master_seed, name))
            self._streams[name] = stream
        return stream

    def reset(self) -> None:
        self._streams.clear()


def poisson_interval_ps(rng: random.Random, mean_ps: int) -> int:
    """指数分布的到达间隔，均值 ``mean_ps``。用于泊松到达过程。"""
    if mean_ps <= 0:
        raise ValueError(f"平均间隔必须为正，收到 {mean_ps}")
    # random.expovariate 的均值是 1/lamb；这里直接用 mean 缩放
    return max(1, int(rng.expovariate(1.0 / mean_ps)))


def uniform_ps(rng: random.Random, low_ps: int, high_ps: int) -> int:
    """均匀分布。"""
    if high_ps <= low_ps:
        return low_ps
    return rng.randrange(low_ps, high_ps)


def lognormal_interval_ps(rng: random.Random, mean_ps: int, sigma: float) -> int:
    """对数正态间隔。用于建模"多数事务紧凑、少数长间隔"的突发流量。"""
    if mean_ps <= 0:
        raise ValueError(f"平均间隔必须为正，收到 {mean_ps}")
    mu = mean_ps / (1.0 + sigma * sigma) ** 0.5
    raw = rng.lognormvariate(0.0, sigma) * (mean_ps / (1.0 + sigma * sigma) ** 0.5)
    return max(1, int(raw))


def on_off(
    rng: random.Random,
    on_mean_ps: int,
    off_mean_ps: int,
    on_interval_mean_ps: int,
    off_interval_mean_ps: int,
) -> tuple[bool, int]:
    """开关（on-off）突发模型的状态转移。

    返回 ``(是否处于 on 期, 本期持续时长_ps)``。用于建模"CPU 突发访存后长时间空闲"
    这类非平稳流量——**这正是不能对 SoC 流量套 Jackson 网络的原因**。
    """
    if rng.random() < 0.5:
        state_on = True
    else:
        state_on = False
    if state_on:
        return True, poisson_interval_ps(rng, on_interval_mean_ps)
    return False, poisson_interval_ps(rng, off_interval_mean_ps)
