"""专家名册与静态数据。

名册是**生成物**，不是手写文件：

    experts_seed.yaml（手写语义内核，48×8 行）
      → scripts/gen_experts.py（校验 + 确定性生成）
      → experts.json（提交入库的产物）
      → loader.py（只读，不做判断）

`app.data` 是这一层的公开门面，流水线只从这里导入。它取代了阶段 2 的
临时名册 `roster.py`（15 人），函数签名保持不变——`pipeline/dispatch.py`
与 `pipeline/context.py` 只需要改导入路径，逻辑一行没动。
"""
from app.data.loader import (
    DEFAULT_TEAM,
    DIGEST_BUDGET,
    Expert,
    distribution,
    expert_by_id,
    experts_by_level,
    known_ids,
    load_experts,
    reload_experts,
    roster_digest,
    roster_size,
)

__all__ = [
    "DEFAULT_TEAM",
    "DIGEST_BUDGET",
    "Expert",
    "distribution",
    "expert_by_id",
    "experts_by_level",
    "known_ids",
    "load_experts",
    "reload_experts",
    "roster_digest",
    "roster_size",
]
