"""报告层：图表与自包含 HTML。"""

from .plots import make_all_plots
from .summary import build_run_summary, write_run_json

__all__ = ["build_run_summary", "make_all_plots", "write_run_json"]
