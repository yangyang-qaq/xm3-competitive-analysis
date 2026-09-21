"""舆情、图表、指标。

三个模块的共同点：**它们都不产生新的判断，只把已有判断变成可读的形式**。
舆情把文本变成分布，图表把数字变成图，指标把整个运行变成一组可比对的数。

分开的原因：它们的失败模式完全不同。舆情错了是"标注不准"，
图表错了是"图画得不对"，指标错了是"报告对自己说了谎"。混在一起时，
后者的错误会被前两者的噪声掩盖——而指标错了恰恰是最严重的那种。
"""
from app.core.analysis.charts import build_charts
from app.core.analysis.metrics import compute_metrics, rework_delta
from app.core.analysis.sentiment import label_sentiment, label_text_by_rules

__all__ = [
    "build_charts",
    "compute_metrics",
    "label_sentiment",
    "label_text_by_rules",
    "rework_delta",
]
