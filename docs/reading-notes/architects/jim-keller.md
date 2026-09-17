# Jim Keller

**身份**：DEC Alpha → AMD K7/x86-64 → SiByte/Broadcom → PA Semi → Apple A4/A5 → AMD Zen → Tesla FSD → Intel → Tenstorrent CEO。传奇色彩最浓的在世架构师，但本文件只收**有据可查的方法论**，不收光环。

**他没有个人博客。** 他的"文章"以访谈、播客、演讲实录的形式散落在下列渠道。书面（可精读、可引用）与音视频分开列。

## 在哪读

**书面访谈（可精读）**

| 来源 | 内容 | 链接 |
|---|---|---|
| Harvard Data Science Review（MIT Press） | 炉边谈话：从硅到智能，AI 计算的未来 | https://hdsr.mitpress.mit.edu/pub/n7la10vt |
| More Than Moore（Ian Cutress） | Tenstorrent 书面问答 | https://morethanmoore.substack.com/p/interview-with-jim-keller-tenstorrent |
| EE Times / Tenstorrent newsroom | "AI 仍服从计算的老定律"（2026） | https://tenstorrent.com/newsroom/jim-keller-ai-still-obeys-the-old-laws-of-compute |
| EE Times Asia | 谈 RISC-V 与边缘 IP | https://www.eetasia.com/jim-keller-on-ai-risc-v-tenstorrents-move-to-edge-ip/ |
| EEPW 中文长访谈 | 中文翻译的长篇访谈（Zen 团队组建细节最全） | https://www.eepw.com.cn/zhuanlan/202106/198741.html |
| Ojo-Yoshida Report（Tenstorrent 转载） | 从 CPU 到 CEO 的历程 | https://tenstorrent.com/en/newsroom/the-ojo-yoshi-report-jim-kellers-journey-from-cpus-to-ceo |

**音视频/演讲**

| 来源 | 内容 |
|---|---|
| Lex Fridman Podcast #70、#162 | 两次长谈，EV6 性能模型故事在 #162 |
| UC Berkeley 2019 colloquium | "Moore's Law is Not Dead"（有摘要页：https://eecs.berkeley.edu/research/colloquium/190918-2/ ） |
| SemiEngineering "Tech Threads" | 与 Baya Systems 对谈抽象层与模块化 |
| Computer Architecture Podcast Ep. 11 | 执行模型契约 |
| TSMC OIP 论坛 2022 | "Designing in 2023: 10 Problems to Solve"（YouTube） |

## 摘录

**1. EV6 性能模型课——本项目的立项约束** ⚠️

> "Alan Eustace 告诉我他的模型是 1000 行，而我的是 30000 行。我重写到大约 1100 行，压到 1000 行用了一个不眠周。"

—— Lex Fridman #162

 Keller 的结论不是"代码越少越好"，而是：**模型应该是有价值的、可理解的、明确可用的工具，不是探索想法的脚本。** 这直接变成了本项目的硬约束（核心模型 1000~1500 行，见 ADR-0000）。诚实备注：同一件事他在中文访谈里有另一个版本——"EV6 的简单性能模型预测能力弱，因为抓不到交互效应"。两个版本都是他讲的，合起来才是完整的教训：**模型要小，但要知道它抓不到什么。**

**2. 抽象层与模块化** ✅

> "你没法修好一个又坏又复杂的系统。"
> "通过让一切都有正确的抽象层和模块化，我们真的能用简单组件搭出非常复杂的东西。"

—— SemiEngineering "Tech Threads"

**3. 数据移动是新前沿** ✅

> "data movement, not just compute, has become the new frontier."

—— 同上

这是本工具存在的第一性依据。ADR-0001 的发现（翻转受限、有效带宽只有峰值的 52%）就是这句话的具体形态。

**4. 执行模型是硬件软件之间的契约** ⚠️

> "第一号规则是你不能违反软件看到的执行模型。VLIW 失败就是因为他们试图违反这个模型……一旦你要求程序员成为微架构师，你就失败了。Itanium 有大概八个屏障，没人知道它们是干嘛的。"

—— Computer Architecture Podcast Ep. 11

**5. 周期性重写** ⚠️

> "如果你想在计算机体系结构上取得大进展，应该每 3 到 5 年从零做一次。"

—— Lex Fridman #70

**6. Rent's Rule 与通信稀缺** ✅

> "Rent's Rule 看起来是稳固的。"（I/O 海滩线随逻辑面积亚线性增长，所以算力越大通信越稀缺）
> "低估它常常是致命伤（often a fatal flaw）。"

—— EE Times, 2026（Tenstorrent newsroom 转载）

**7. 接口先于 RTL（Zen 的做法）** ✅（中文访谈）

> 所有接口在任何一块 RTL 写出来之前就定义好。验证主管告诉他这意味着"找不到那些交互 bug"——他的回答是我们本来就不想要那些 bug。

—— EEPW 中文访谈

**8. 只用老定律推理** ✅

> "没有新定律。AI 计算的基本原理植根于 1970 年代的 HPC，几十年来已被充分理解。"

—— EE Times, 2026

注意他的推理工具箱：Rent's Rule（1960s）、Amdahl 定律（1967）、Little's Law（1961）。**不追新框架，先把老定律吃透**——这也是本项目的学习方法论。

## 架构要点提炼

1. **接口是契约**：可见的契约必须简单稳定，契约背后的实现可以任意混乱（乱序执行就是典范——内部狂野，外部按序）
2. **模型的可用性 > 模型的完备性**：1000 行可理解的模型胜过 30000 行探索脚本
3. **复杂度靠删除不靠管理**：修不好就重写，3~5 年为一个周期
4. **用 1960-70 年代的定量定律思考**：Rent's Rule、Amdahl、Little's Law
5. **约束要带数字**：Zen 的 10mm²/5W/3.5GHz 是谈判出来的硬约束，然后把差距变成可解的问题

## 与本项目的联系

- EV6 教训 → `pyproject` 之外的第二条硬约束（ADR-0000 约束 D）
- "data movement is the frontier" → ADR-0001 的翻转受限发现是这句话的一个实例
- 接口先于 RTL → 本项目里 `MasterPort`/`SlaveDevice` 的接口先于 crossbar 实现

## 我的笔记

（读完后写在这里）
