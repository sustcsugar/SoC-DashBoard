# Bill Dally

**身份**：斯坦福教授，MIT 结业于 Caltech；与 Towles 合著互联网络权威教材《Principles and Practices of Interconnection Networks》（2004）；现任 NVIDIA 首席科学家。**互联结构、流控、死锁**这三个词的学术源头基本都绕不开他。

## 在哪读

| 来源 | 内容 |
|---|---|
| 《Principles and Practices of Interconnection Networks》 | 教科书。第 17 章死锁、18–20 章仲裁与 QoS 是 Stage 2 的配套读物 |
| Duato, Yalamanchili, Ni《Interconnection Networks: An Engineering Approach》 | 互补教材，死锁免路由理论更强 |
| 演讲 "From Here to ExaScale" 等 keynote（网上多处可寻） | 数据移动能耗的论证 |
| Stanford 相关课程讲义 | 互联网络专题 |

（Dally 本人的公开博客产出不多，他的思想主要通过教材和 keynote 传播。）

## 摘录

**1. 数据移动的能源会计** ⚠️（多次 keynote 的一致表述）

> "Fetching operands costs more than computing on them… It's not about the FLOPS. It's about data movement."
> （取操作数比算它们更贵……重点不是 FLOPS，是数据移动。）
> "A joule is a terrible thing to waste."
> （一焦耳都不能浪费。）

**2. 能耗的数量级（他反复引用的一组数）** ⚠️

64 位双精度运算 ≈ 20 pJ；DRAM 读写 ≈ 1.3–26 nJ；系统级链路传输 ≈ 1 nJ。**从内存取一个数的能耗是算一下的三到一百多倍。**

**3. 处方：局部性** ⚠️

> 解法是 locality——把数据放得离计算更近（更近的内存、堆叠 DRAM），加上领域专用化。

## 架构要点提炼

1. **把数据移动当作一等公民来记账**——不是"功耗里有一部分是总线"，而是"系统的一切成本主要由搬运产生"
2. **死锁是图论问题**：通道依赖图找环（第 17 章），这是 Stage 2 ECDG 方法的源头
3. **仲裁是可分离的分配问题**：RR/WRR/wavefront/分离分配器的分类学（第 18–20 章），本项目仲裁器的谱系从这里来
4. **流控决定网络能撑多满**：credit/onthefly 等机制决定缓冲该多大

## 与本项目的联系

- "It's about data movement" → ADR-0001 的全部内容本质上是给这句话画了一张瀑布图
- 第 17 章通道依赖图 → Stage 2 的 wait-for 图 + 环检测（ECDG 是它的扩展）
- 第 18–20 章 → 本项目 `arbiter.py` 里 RR/WRR/DRR/Prio 的理论出处
- 他的能耗账 → 将来给模型加功耗维度时的框架（数据搬运功耗远大于计算）

## 我的笔记

（读完后写在这里）
