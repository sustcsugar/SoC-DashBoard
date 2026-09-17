# Bob Colwell

**身份**：Intel P6（Pentium Pro）首席架构师，著《The Pentium Chronicles》（2006）。如果 Keller 代表"英雄架构师"的一面，Colwell 代表另一面——**流程、文化与工程判断**。他本人更愿意谈后者。

**他有大量可公开的成体系文字**，是本资料库里"书面遗产"最完整的一位：48 篇 IEEE Computer 专栏、口述史、演讲实录。

## 在哪读

| 来源 | 内容 | 链接 |
|---|---|---|
| Clemson 托管的写作索引 | 全部文章/演讲入口 | https://mark.people.clemson.edu/330/chronques.html |
| "At Random" 专栏完整索引（2002–2005） | 48 篇 IEEE Computer 专栏标题+期号 | https://mark.people.clemson.edu/330/colwell/at_random.html |
| SIGMICRO 口述史（PDF，2009） | P6 全过程的口述记录 | https://www.sigmicro.org/media/oralhistories/colwell.pdf |
| 《The Pentium Chronicles》 | Internet Archive 可借阅 | https://archive.org/details/pentiumchronicle0000colw |
| HotChips 25 主题演讲（2013，YouTube） | "The Chip Design Game at the End of Moore's Law" | 经 Clemson 索引页进入 |
| P6 内部文档（1991–95） | C 编码规范、微架构调优指南 59 页、优化指南 94 页 | 经 Clemson 索引页进入 |

## 摘录

**1. 工程的本质** ✅

> "the essence of engineering is the art of the compromise"（工程的本质是妥协的艺术）

—— 2006 年私人通信，Clemson 页面引用

**2. 失败与冒险的区分** ✅

> "Not all failures [are] equally forgivable. Well-conceived risks: good. Outright gaffes: bad."
> （不是所有失败都同样可原谅。深思熟虑的冒险：好。彻头彻尾的低级错误：坏。）

—— 2008 演讲 "How To Be A Successful Engineer"

**3. 两句最狠的** ✅（出自书中，经 Clemson 讨论题引用）

> "Mediocrity is the equivalent of gravity in the world of creative projects"
> （平庸之于创造性项目，相当于重力——它一直在把你往下拉）
> "Creativity is a poor substitute for knowing what you're doing."
> （创造力不能替代知道自己在做什么。）

**4. 坏消息的正确打开方式** ✅

> "that's a bummer; what are you going to do about it?"
> （真糟糕——你打算怎么办？）

—— SIGMICRO 口述史。要求带问题来的人同时带上方案，这条是工程文化的操作化定义。

**5. 一天实验文化（来自早期研究，⚠️ 转述）**

P6 团队用**一天就能做完的实验**来终结争论（"what would happen if" 游戏），避免"目光短浅的 do-loop"。争论不停就跑数，不跑数就别吵。—— 这是本项目 `sweep` 子命令的方法论原型。

**6. 评审与预测（同上）**

- 跳过正式设计评审是"a very bad idea"
- 架构师要**超额交付而非超额承诺**
- "gratuitous innovation"（无谓的创新）是有害的——每个新花样都要付验证代价
- 架构师不该被"流水线化"（决策有不能并行的串行依赖）

## "At Random" 专栏精选（对本项目最相关的 10 篇）

全部需要 IEEE Xplore 访问（机构订阅或单篇购买），标题即主题：

| 篇目 | 期号 | 对应本项目 |
|---|---|---|
| If You Didn't Test It, It Doesn't Work | 2002-05 | 三重自校验的价值观 |
| Engineering Decisions | 2003-08 | ADR 实践 |
| Design Reviews | 2003-10 | 架构评审方法 |
| Benchmarketing Competition | 2003-12 | 警惕"跑分营销" |
| **Design Fragility** | 2004-01 | ★ 有界缓冲与死锁敏感性（Stage 2 必读） |
| Engineers as Soothsayers | 2004-09 | 架构师=预测者，性能模型的角色 |
| The Art of the Possible | 2004-08 | 能力边界思维 |
| **Point of Highest Leverage** | 2005-04 | ★ 早期建模正是最高杠杆点 |
| Complexity in Design | 2005-10 | 复杂度管理 |
| Books Engineers Should Read | 2005-11 | 书单（与本项目阅读路径对照） |

## 架构要点提炼

1. **流程比个人英雄更可靠**：P6 的成功归因于文化（数据驱动争论、一天实验、评审纪律），不是某个天才
2. **妥协是工作本身**：架构师的产出就是"带数字的取舍"，没有取舍就没有架构
3. **区分冒险与失误**：为深思熟虑的冒险失败留空间，对低级错误零容忍
4. **文档是团队规模化的方式**：P6 时代就有 94 页优化指南——文档不是官僚产物，是带宽

## 与本项目的联系

- 一天实验 → `sweep` 子命令的设计哲学：争论让位于可复现的扫掠
- "Design Fragility" → Stage 2 的死锁敏感性分析（ECDG）
- 专栏《Point of Highest Leverage》→ 本项目存在的正当性：立项初期是修正架构成本最低的时刻

## 我的笔记

（读完后写在这里）
