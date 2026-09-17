# SoC-Dashboard

面向芯片立项初期的 **SoC 总线带宽 / 拥塞 / 互锁** 软件建模平台。

用交易级离散事件仿真（DES）回答三个问题：

1. **能力边界** —— 带宽-延迟曲线的拐点在哪？能撑到什么负载？
2. **瓶颈归因** —— 有效带宽损耗花在哪里？哪部分**改配置能救**，哪部分**必须重新流片**？
3. **失效模式** —— 什么条件下出现主设备饿死、实时外设抖动、总线互锁死锁？

同时是一个**架构师能力建设载体**：读概念 → 实现它 → 回答问题 → 写 ADR → 术语入表。

---

## 快速开始

```bash
# 0. 安装 uv（一次性；独立安装，与 conda 无关）
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 1. 建环境并安装依赖（创建 .venv，版本固化到 uv.lock）
uv sync

# 2. 跑测试
uv run pytest

# 3. 跑参考场景
uv run socsim run configs/reference_ap.yaml --out out/baseline

# 4. 扫掠带宽-延迟拐点曲线
uv run socsim sweep configs/reference_ap.yaml \
    --vary "@total_load=0.8,1.6,2.4,3.2,4.0,4.8,6.0" --out out/knee_total
```

> **Python 解释器**：`.python-version` 锁定 3.11.15——uv 管理的独立解释器，缺了会自动下载，不依赖系统或 conda 的 Python。
> **uv 网络操作报证书错误时**：多为 conda 激活注入的坏 `SSL_CERT_FILE`，用 `env -u SSL_CERT_FILE uv sync` 绕过（本机已把证书包补到该路径，一般不会再遇到）。
> **遗留 conda 路径**：`environment.yml` 仍可用（`conda env create -f environment.yml`），但不再维护；二者生成的环境互相独立。

---

## 目标系统

| 项 | 值 |
|---|---|
| 产品形态 | 移动/消费类 AP |
| 总线拓扑 | 单层全交叉开关（RD/WR 路径独立） |
| 主存 | **PSRAM 硬核**（vendor controller + PHY，经 AXI 接入） |
| 片上存储 | AXI SRAM（多 bank） |
| 主设备 | 计算类（CPU/GPU/NPU）、数据搬运类（DMA/存储/网络）、等时类 |

### 关键建模决策

**PSRAM 建模它的 AXI 侧性能包络，不建模内部。** 硬核 IP 不可改，建模内部没有决策价值。真正有决策价值的是系统侧：outstanding 配置、仲裁策略、ID 分配、流量调度——这些作为变量去扫描。

**PSRAM 系统的瓶颈通常在 Little's Law，不在引脚带宽。** `有效带宽 ≈ 在途字节数 ÷ 延迟`。高延迟 + outstanding 不足 = 总线大量空转。这是「存储有效带宽不达标」最常见也最容易被误诊的原因。

---

## 目录结构

```
SoC-Dashboard/
├── environment.yml          # conda 环境声明
├── docs/
│   ├── glossary.md          # ★ 术语表（唯一真相源，持续维护）
│   ├── methodology.md       # 建模方法论与抽象层级依据
│   ├── adr/                 # 架构决策记录
│   └── reading-notes/       # 规范与论文阅读笔记
├── src/socsim/
│   ├── kernel/              # DES 内核、时钟域、RNG、在线统计
│   ├── axi/                 # 事务、五通道、主从端口、outstanding、ID 保序
│   ├── interconnect/        # 交叉开关、仲裁器（RR/WRR/DRR/QoS）、wait-for 图
│   ├── masters/             # 流量模型
│   ├── slaves/              # AXI SRAM、硬核包络模型（PSRAM）
│   ├── monitor/             # 带宽/延迟/队列/仲裁/阻塞采集
│   ├── analysis/            # 双拐点、损耗瀑布、公平性、死锁
│   ├── report/              # 图表与 HTML 报告
│   ├── config.py            # pydantic 配置模型
│   └── cli.py
├── configs/
│   ├── reference_ap.yaml    # 参考 SoC
│   ├── envelopes/           # IP 性能包络表
│   └── scenarios/           # 负载场景
└── tests/                   # 含三重自校验
```

---

## 阶段规划

| 阶段 | 目标问题 | 产出 |
|---|---|---|
| **0** | 建立语汇 | `glossary.md`、ADR-0000、数据流图 |
| **1** | 为什么 PSRAM 有效带宽只有峰值的 X%？ | 1000~1500 行内核、拐点曲线、损耗瀑布、ADR-0001 |
| **2** | 什么条件下会死锁、谁会饿死？ | ECDG 死锁检测、公平性分析、QoS 契约、ADR-0002 |
| **3** | 应该选哪个配置？ | DSE 扫掠、Pareto 前沿、Sobol 敏感度、ADR-0003 |
| **4** | 怎么直观看到？ | FastAPI + ECharts 交互面板 |

---

## 内建的正确性机制

架构工具必须自证，否则结论比没有结论更危险。

| 机制 | 内容 |
|---|---|
| **三重自校验** | 上界校验 + Little's Law 校验 + 守恒校验 |
| **DRR 而非 RR** | RR 给的是每事务公平，16-beat burst 对 1-beat single 时语义错误 |
| **确定性 Little's Law** | 样本路径版本在任意有限窗口成立，不需平稳性假设 |
| **非平稳流量警示** | 帧同步流量是周期性的，套 Jackson 网络会出错 |
| **有界缓冲** | 死锁可检的**前提**——无界缓冲在数学上不可能形成依赖环 |
| **双拐点区分** | 并发受限（改配置能救）vs 器件受限（只能重流片） |

---

## 设计约束

- **核心模型 1000~1500 行可读代码**，不是框架。模型应该是明确可用的工具，不是探索脚本。
- **不追新框架**。先把 Little's Law 和排队论吃透。
- **速度问题先测量后优化**，不预先写 C 扩展。

---

## 警示案例

**Tesla HW3** —— Musk 承认 HW3「没有能力实现无监督 FSD」，原因是内存带宽只有 HW4 的 1/8。约 400 万辆车受影响，补救成本每车 1500–2000 美元，并引发集体诉讼。

**架构的内存带宽预算为最终负载设错了，而被点名的约束不是 TOPS，是内存带宽。** 这就是本项目的存在理由。
