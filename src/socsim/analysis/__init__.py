"""分析层：双拐点判别、带宽损耗瀑布、诊断报告。

这一层是工具的价值所在——把仿真产生的一大堆数字，变成**可行动的结论**。

核心区分：**并发受限**（改配置能救）vs **器件受限**（只能重流片）。
两者在带宽曲线上长得一模一样，只能靠总线空闲率区分。
"""

from .knee import (
    DiagnosisReport,
    KneeDiagnosis,
    MasterDiagnosis,
    diagnose_master,
    diagnose_run,
    diagnose_slave,
)
from .waterfall import BandwidthWaterfall, WaterfallStep, build_waterfall

__all__ = [
    "BandwidthWaterfall",
    "DiagnosisReport",
    "KneeDiagnosis",
    "MasterDiagnosis",
    "WaterfallStep",
    "build_waterfall",
    "diagnose_master",
    "diagnose_run",
    "diagnose_slave",
]
