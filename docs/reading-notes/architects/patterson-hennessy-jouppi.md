# Patterson / Hennessy / Jouppi —— 量化方法学派

**身份**：Hennessy & Patterson 合著《计算机体系结构：量化研究方法》（体系结构的"圣经"），2017 年同获图灵奖；Jouppi 是 Google TPU 首席架构师。三人共同点：**一切结论必须落到可测量的数字上**。

## 在哪读

| 来源 | 内容 |
|---|---|
| Hennessy & Patterson《Computer Architecture: A Quantitative Approach》(6th ed.) | 通读一次后当参考。注意：它**不讲**片上互联 QoS，别指望它 |
| Patterson & Hennessy, "A New Golden Age for Computer Architecture"（CACM 2018，图灵奖演讲文） | 领域级综述，DSA（领域专用架构）的纲领 |
| Williams, Waterman, Patterson, "Roofline: An Insightful Visual Performance Model"（CACM 2009） | Roofline 模型原始论文 |
| Jouppi et al., "Ten Lessons From Three Generations of Google TPUs"（ISCA 2021） | 罕见的架构师公开复盘 |
| Jouppi et al., "In-Datacenter Performance Analysis of a TPU"（ISCA 2017） | TPU v1 论文 |

## 摘录

**1. 量化方法（H&P 的第一性原则）** ⚠️（教材共识，非单句引语）

> CPU 时间 = 指令数 × CPI × 时钟周期。加速比受 Amdahl 定律约束。**"让常见情况快"（make the common case fast）**。

这三板斧是所有架构评审的共同语言。本项目术语表第 9 节的公式速查就是这套东西的子集。

**2. Patterson 论测量** ⚠️

> "It's hard to make progress if you can't measure it… benchmarks shape a field, and there are examples where there are bad benchmarks."
> （测不了就没法进步……基准塑造一个领域，而历史上有过坏基准。）
> "TOPS has even less meaning than MIPS."
> （TOPS 比 MIPS 还没有意义。）

—— 对本项目跑分的直接警示：报告达成带宽时必须带条件（什么负载、什么仲裁、什么窗口），否则就是"benchmarketing"（Colwell 同名专栏）。

**3. Jouppi 论 TPU 的设计哲学** ⚠️

> "Don't invent anything more than necessary… Start from a typical vector CPU architecture and add matrix operations."
> （除非必要不要发明新东西……从一个典型的向量 CPU 架构开始，加上矩阵运算。）
> —— TPU v2 的思路明确仿照 Cray-1：向量机加矩阵，不另起炉灶。

**4. TPU 十教训（ISCA 2021）里最相关的几条** ⚠️

- 对 DSA 而言，**编译器兼容性 > 二进制兼容性**
- 目标定 **TCO（总拥有成本）而不是初始成本**
- DNN 规模每年 ~1.5×——架构要为负载增长留余量
- 拨款式阵列（systolic array）的价值：复用取数 >100 次，单位计算的能耗开销降 >10×

## 架构要点提炼

1. **Roofline 是前筛不是结论**：算术强度 vs 可达性能，一眼分类"带宽受限/计算受限"——本项目 Stage 3 用它决定哪些主设备值得做完整 DSE
2. **坏基准比没有基准更糟**：测量条件必须随数字一起报告
3. **不做无谓的发明**：TPU 的每个"创新点"都能在旧机器上找到原型
4. **复盘是架构师的责任**：TPU 十教训是极少数公开的架构 post-mortem——本项目每阶段写 ADR 就是在练习这种复盘

## 与本项目的联系

- Roofline → Stage 3 DSE 的前筛层（已列入计划）
- "TOPS 无意义" → 本报告格式坚持"达成带宽 + 测量条件"一起输出的依据
- Amdahl → 术语表公式速查；也是"优化这个旋钮值不值"的判断工具
- TPU 十教训 → ADR 写法的范本：数字、取舍、后果全都要有

## 我的笔记

（读完后写在这里）
