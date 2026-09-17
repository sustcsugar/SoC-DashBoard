# 早期大师 —— Cray / Cocke / Brooks / Wilson

**身份**：四个人的共同点是**在没有任何"性能建模工具"的年代，用极致的自律做出了正确的架构**。他们的方法后来都变成了工具化的学科——本项目就是这个工具化过程的一部分。

## Seymour Cray —— 系统观

Cray 超级计算机创始人。名言最多、方法论最纯粹的一位。

| 摘录 | 出处 |
|---|---|
| "Anyone can build a fast CPU. The trick is to build a fast system."（谁能造出快的 CPU；**造出快的系统**才是本事。） | 广泛引用 |
| "My guiding principle was simplicity."（我的指导原则是简单。） | 同上 |
| "A supercomputer is a machine that turns a CPU-bound problem into an I/O-bound problem."（超级计算机就是把 CPU 受限问题变成 I/O 受限问题的机器——所以内存带宽必须最大化解耦。） | 转述其观点 |

**要点**：CPU 不是机器，系统才是。**主存带宽决定 CPU 实际能跑多快**——这正是本项目测的东西（ADR-0001：PSRAM sustained 3.2 GB/s，六主设备合计需求 5.55 GB/s，谁都跑不满）。

## John Cocke —— 测量先行

IBM 801（RISC 鼻祖）背后的架构师，图灵奖得主。

> 他先统计指令使用频率，**只把高频原语做成硬件**，低频的交给编译器合成。RISC 的定义不是"指令少"，而是"一组精心挑选的原语，充分利用存储层次中最快的部分"。

**要点**：先测负载，再定结构。本项目 `sweep` 扫出的"哪个主设备吃多少带宽"就是在做 Cocke 1970 年代做的事。

## Fred Brooks —— 概念完整性

《人月神话》《The Design of Design》作者，图灵奖得主。**架构师这个角色的理论家。**

> "Conceptual integrity is the most important consideration in system design."（概念完整性是系统设计最重要的考量——一个反映单一设计思想的系统，胜过拼凑多个好想法的系统。）
> "The architect is the agent, approver, and advocate for the user."（架构师是用户的代理人、批准者与拥护者。）

**要点**：好设计出自"一个头脑"的一套思想——这也是对"英雄架构师"叙事最有力的学术辩护，以及最尖锐的检验标准（这个设计有一套连贯的思想吗？）。

## Sophie Wilson —— 做减法

ARM1 的 ISA 设计者。

> ARM1 只有约 25,000 晶体管（同代 80286 有 134,000）。著名的 1W 功耗目标**只是为了能用塑料封装**——低功耗是节俭的副产品，不是初衷。"我们一直做减法，直到几乎什么都不剩。"

**要点**：约束是设计工具。PSRAM 的约束（高延迟、outstanding 有限）之于本项目，正如塑料封装之于 ARM1。

---

## 四个人的共同模式

1. **先测量后设计**（Cocke 的指令频率、Cray 的系统瓶颈观）
2. **瓶颈几乎从不在 ALU**（Cray：内存带宽；本项目：方向翻转）
3. **删到不能再删**（Wilson 的 25k 晶体管、Cray 的简单性原则）
4. **一个连贯的设计思想**（Brooks 的概念完整性）

## 与本项目的联系

- Cray 的"快系统" → 本项目的诊断结论总是落在**系统**（总线/存储/调度）而非单个 IP
- Cocke 的测量先行 → `sweep` 扫掠 + 术语表"停顿时间公平 vs 带宽公平"的区分
- Brooks 的概念完整性 → 检验本模型：一个统一的"时间恒等式"（数据+翻转+空闲=1）贯穿瀑布分解，而不是每处各算各的

## 我的笔记

（读完后写在这里）
