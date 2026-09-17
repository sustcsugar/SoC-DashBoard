# 知名架构师文章摘录与评注

> **这是什么**：知名计算机/芯片架构师的文章、访谈、演讲摘录，配中文译注和"与本项目的联系"。
> **这不是什么**：不是原文转载。每条摘录都短于原文的百分之一，附出处链接——**摘录是路标，请点进去读原文**。

## 忠实度标记

每条引语前的标记表示这条引语的核实程度：

| 标记 | 含义 |
|---|---|
| ✅ | 来源页面被直接抓取核对过，引语可信 |
| ⚠️ | 来自二手转录（如播客文字稿镜像），大意可信，**引用前建议听/看原始音视频核对措辞** |

## 文件清单

| 文件 | 人物 | 一句话定位 |
|---|---|---|
| [jim-keller.md](jim-keller.md) | Jim Keller | K7/Zen/A系列/FSD，"接口契约"与"周期性重写"方法论 |
| [bob-colwell.md](bob-colwell.md) | Bob Colwell | Pentium Pro 首席架构师，流程与工程文化的理论家，48 篇专栏索引 |
| [bill-dally.md](bill-dally.md) | Bill Dally | 互联网络教科书作者，"数据移动的能源会计" |
| [patterson-hennessy-jouppi.md](patterson-hennessy-jouppi.md) | Patterson / Hennessy / Jouppi | 量化方法学派 + TPU 十教训 |
| [early-masters.md](early-masters.md) | Cray / Cocke / Brooks / Wilson | 早期大师：系统观、测量先行、概念完整性、做减法 |
| [chinese-architects.md](chinese-architects.md) | 胡伟武 / 陈云霁 | 中文语境下最完整的两位架构方法论表达者 |

## 与项目阶段的映射

不要一次读完。按阶段配对阅读，读到的东西正好被当期的工程用到：

| 阶段 | 先读 | 呼应的项目内容 |
|---|---|---|
| 0 建立语汇 | Keller（接口契约、EV6 模型教训）、Colwell（工程决策） | ADR-0000 的方案 A 论证 |
| 1 带宽预算 | Dally（数据移动）、早期大师（Cray"快系统"） | ADR-0001 翻转受限的发现 |
| 2 阻塞与互锁 | Dally 第 17 章、Colwell"Design Fragility" | ECDG 死锁检测（待实现） |
| 3 决策与扫掠 | Patterson/Jouppi（Roofline、TPU 十教训） | DSE 扫掠引擎（待实现） |

## 维护规则

1. 读到新的文章/访谈 → 摘录进对应文件（没有就建新文件），**当天完成**，攒了就不会再做
2. 每条摘录必须带：出处链接、忠实度标记、一句"为什么重要"
3. 发现某条引语与原文有出入 → **改这里**，术语表同样只认这里的修正
4. 自己的读后感写在每文件末尾的「我的笔记」一节，与摘录分开——半年后重读时，你的笔记比摘录值钱
