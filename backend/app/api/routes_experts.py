"""专家公会：48 人名册 + 参与度。

名册从 `experts.json` 来，**不从数据库来**
---------------------------------------
名册是**代码资产**（`experts_seed.yaml` 手写 → `gen_experts.py` 生成），
不是运行时数据。放进库里就要面对"库里的名册和仓库里的名册不一致"，
而这个不一致没有任何东西会报错。所以这里每次读的都是同一份文件，
`loader` 还给它加了 `lru_cache`。

统计从两个来源拼，而且**分得清楚**
--------------------------------
- `stats.source == "seed"` —— 名册自带的初始值，里面的 0 是
  **"还没被量过"**，不是"量出来是 0"。前端据此决定显示不显示，
  把它当成实测值印出来就是编数据。
- `participation` —— 真实数出来的：这位专家被派进过多少份报告。
  来源是 `report_team` 关联表（见 `reports.team_usage`）。

`expert_stats` 表目前**没有任何代码写它**（实测 0 行）。这里不去读它，
因为读一张永远为空表只会给接口添一个恒为 None 的分支；等真有东西
写它的时候再加。接口形状已经留好了 `source` 这个字段来承载那个区分。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.data.loader import expert_by_id, load_experts, roster_size
from app.db.repo import reports as reports_repo

router = APIRouter(prefix="/api/experts", tags=["experts"])

#: 层级顺序与展示名。**顺序在这里写死是有意的**：它是"决策 → 战略 → 执行"
#: 这个业务含义，不是名册里恰好排成那样。名册重排不该让页面跟着乱序。
LEVELS = (
    ("L3", "决策层", "决定研究边界与最终取舍"),
    ("L2", "战略层", "各自从一条战略维度切进去"),
    ("L1", "执行层", "行业与职能专才，负责取证"),
)


def _level_index(level: str) -> int:
    for index, (key, _, _) in enumerate(LEVELS):
        if key == level:
            return index
    # 名册里出现了一个没登记过的层级。排到最后而不是报错：
    # 一个专家排在队尾，比整个名册页 500 好得多。
    return len(LEVELS)


def _expert(expert, participation: int) -> dict:
    return {**expert.to_dict(), "participation": participation}


@router.get("")
def list_experts(
    level: str = Query(default="", description="按层级过滤：L1 / L2 / L3"),
    group: str = Query(default="", description="按分组过滤"),
) -> dict:
    """名册。默认按"层级 → 分组 → id"排序。

    过滤在服务端做而不是前端：48 人一次全发也就几十 KB，但
    "前端自己筛"意味着**筛选逻辑有两份**（这里是唯一真相源），
    而多一份就会漂。
    """
    usage = reports_repo.team_usage()
    items = [
        _expert(expert, usage.get(expert.expert_id, 0))
        for expert in load_experts()
        if (not level or expert.level == level) and (not group or expert.group == group)
    ]
    # 分组内部按参与度降序、id 升序：常用的排前面，而**同分时按 id**
    # 保证顺序稳定——不稳定的话每次刷新名册都在跳。
    items.sort(
        key=lambda item: (
            _level_index(str(item["level"])),
            str(item["group"]),
            -int(item["participation"]),
            str(item["expertId"]),
        )
    )

    groups: dict[str, int] = {}
    for expert in load_experts():
        groups[expert.group] = groups.get(expert.group, 0) + 1

    return {
        "items": items,
        "total": len(items),
        #: 名册总人数（不受过滤影响）。页头写"48 位专家"用的是它，
        #: 用 `total` 的话筛选之后页头会变成"12 位专家"。
        "rosterSize": roster_size(),
        "byLevel": [
            {
                "level": key,
                "label": label,
                "description": note,
                "count": sum(1 for e in load_experts() if e.level == key),
            }
            for key, label, note in LEVELS
        ],
        "byGroup": [{"value": key, "count": n} for key, n in sorted(groups.items())],
    }


@router.get("/{expert_id}")
def get_expert(expert_id: str) -> dict:
    """一个人。名册页点开卡片时用它。

    这里**带上他参与过的报告**——名册页最常问的下一句是
    "他都参与过哪几次"，让前端再去翻一遍报告列表是在浪费一次往返。
    """
    expert = expert_by_id(expert_id)
    if expert is None:
        raise HTTPException(status_code=404, detail=f"名册里没有专家 {expert_id}")

    participation = reports_repo.team_usage().get(expert_id, 0)
    # 走 `reports_of_expert()`，不是 `list_recent()` 之后自己过滤：
    # 后者为了回答"他参与过哪几次"要读 200 份报告的**正文**
    # （平均 172 KB 一份），而这里一个字段都不需要。
    reports = reports_repo.reports_of_expert(expert_id)
    return {**_expert(expert, participation), "reports": reports}


__all__ = ["router"]
