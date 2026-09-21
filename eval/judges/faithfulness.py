"""引用忠实度：**所引的证据，真的支持这个论点吗？**

这是判官指标里最有价值的一个，因为它量的是别处量不到的东西。

`metrics.hallucinationRate` 量的是"引用的编号存不存在"——编号不存在，
coercer 会把它丢掉，所以那个数天然接近 0，而且**它永远发现不了
"编号存在但内容不支持"**。后者才是真正会骗到人的那种：报告里
每个论点后面都挂着上标 `[12]`，点开也确实有第 12 条证据，
只是那条证据讲的是另一件事。铁律 1（引用强制）在字面上成立，
在实质上不成立。

**判"部分支持"是刻意的。** 只给"支持/不支持"两档会逼裁判在
"证据讲了这件事但数字对不上"和"证据讲的是另一件事"之间二选一，
而这两件事的严重程度差很远。三档之后，"部分支持"的比例本身就是一个信号：
它高说明证据找得还行、但引用时被拉伸了。

采样为什么是 20 条而不是全部
----------------------------
判一次要一次 `core` 档调用，判全部论点的成本会超过跑一遍流水线。
20 条是按"够看出比例"定的：真值 5% 时，20 条里一条没抽到的概率约 36%，
所以**这个数只用来发现大问题，不用来断言小差别**。报告里要写明这句话，
否则"忠实度 0.95"会被读成一个比它实际精度更高的数。
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any

#: 裁判看到的证据正文上限。抓回来的正文动辄上万字，全塞进去会让
#: 裁判的注意力散掉，还会把成本推到没有必要的量级。
#: 截断**必须写进 prompt**（"以下是节选"），否则裁判会拿"证据里没写"
#: 去判一个其实写在后面的东西。
EVIDENCE_CHARS = 1200

SUPPORTED = "支持"
PARTIAL = "部分支持"
UNSUPPORTED = "不支持"
VERDICTS = (SUPPORTED, PARTIAL, UNSUPPORTED)

SYSTEM_PROMPT = """你是竞品分析报告的审稿人。你的任务只有一件：
判断**给出的证据是否支持给出的论点**。

规则：
1. 只看证据本身。不要用你自己的知识去补证据没说的东西——
   你自己知道的不能算它支持了。
2. 证据是节选的，可能不完整。**不确定时判"部分支持"，不要判"不支持"。**
3. 证据支持的是"论点的主干"，不是"论点里每个字"。数字、时间、
   数量级对不上算"部分支持"；讲的是另一件事算"不支持"。
4. 只输出 JSON，不要解释。

输出格式：{"verdict": "支持" | "部分支持" | "不支持", "reason": "一句话"}"""


@dataclass
class ClaimJudgement:
    claim_id: str
    verdict: str
    reason: str = ""


@dataclass
class FaithfulnessResult:
    report: str
    #: 判了几条
    judged: int
    #: 报告里一共有几条论点（judged 可能小于它，见 `sample_size`）
    total_claims: int
    verdicts: dict[str, int] = field(default_factory=dict)
    #: `支持` 算 1、`部分支持` 算 0.5、`不支持` 算 0。
    #: **加权而不是只数"支持"**：把"部分支持"算成失败会让忠实度
    #: 在证据找得不错但引用偏松时骤降，而那种情况该被看见的是
    #: "部分支持"这个数本身。
    score: float = 0.0
    items: list[ClaimJudgement] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "report": self.report,
            "judged": self.judged,
            "totalClaims": self.total_claims,
            "verdicts": self.verdicts,
            "score": round(self.score, 4),
            "items": [
                {"claimId": i.claim_id, "verdict": i.verdict, "reason": i.reason}
                for i in self.items
            ],
        }


def _weight(verdict: str) -> float:
    return {SUPPORTED: 1.0, PARTIAL: 0.5, UNSUPPORTED: 0.0}.get(verdict, 0.0)


def sample_claims(
    claims: list[dict[str, Any]], *, size: int, seed: int
) -> list[dict[str, Any]]:
    """抽要判的论点。**固定种子**：换一次跑就换一批样本的话，
    两次的忠实度不可比，而这个数唯一的用处就是比来比去。

    只抽**有引用**的论点：没有引用的论点是"无证据立论"，
    那个由 `unsupportedClaimRate` 确定性地量，让裁判再判一遍是重复计费。
    """
    with_evidence = [c for c in claims if c.get("evidenceIds") or c.get("evidence_ids")]
    if len(with_evidence) <= size:
        return with_evidence
    return random.Random(seed).sample(with_evidence, size)


def build_prompt(claim: dict[str, Any], evidences: dict[str, dict[str, Any]]) -> str:
    """把一条论点与它引的证据拼成裁判要看的东西。

    证据**只取该论点真正引到的那几条**——把整份报告的证据都给它，
    它就会去挑一条能支持论点的，然后忠实度变成"证据库里有没有能支持的话"，
    而不是"报告引的那条支不支持"。
    """
    ids = claim.get("evidenceIds") or claim.get("evidence_ids") or []
    blocks = []
    for eid in ids:
        ev = evidences.get(eid)
        if ev is None:
            # 引了一条报告里没有的证据。这不是判官该判的——coercer 的
            # 幻觉引用率已经记了它。这里如实写出来，让裁判知道这条对不上。
            blocks.append(f"[{eid}] （报告里找不到这条证据）")
            continue
        text = str(ev.get("fullText") or ev.get("snippet") or "")
        if len(text) > EVIDENCE_CHARS:
            text = text[:EVIDENCE_CHARS] + "……（节选，正文未完）"
        blocks.append(
            f"[{eid}] {ev.get('title') or ''}\n"
            f"来源：{ev.get('url') or ''}\n"
            f"{text}"
        )

    return (
        f"论点：{claim.get('text') or claim.get('statement') or ''}\n\n"
        f"这条论点引用的证据（{len(ids)} 条）：\n"
        + "\n\n".join(blocks)
        + '\n\n请判断：上面这些证据是否支持这条论点？只输出 JSON：'
        '{"verdict": "支持" | "部分支持" | "不支持", "reason": "一句话"}'
    )


def judge_report(
    body: dict[str, Any],
    *,
    name: str,
    sample_size: int = 20,
    seed: int = 20260901,
    tier: str = "core",
    temperature: float = 0.0,
    llm: Any = None,
) -> FaithfulnessResult:
    """判一份报告。`llm` 传 None 时取当前配置的 provider。

    `temperature=0.0`：判官要的是**可复现**，不是"有想法"。
    这个参数会写进报告，因为换一个温度就换了一个数。
    """
    from app.providers.base import ChatMessage

    if llm is None:
        from app.providers.registry import get_llm

        llm = get_llm()

    claims = list(body.get("claims") or [])
    evidences = {
        str(ev.get("evidenceId") or ev.get("id") or ""): ev
        for ev in (body.get("evidences") or [])
    }
    picked = sample_claims(claims, size=sample_size, seed=seed)

    result = FaithfulnessResult(report=name, judged=0, total_claims=len(claims))
    for claim in picked:
        prompt = build_prompt(claim, evidences)
        try:
            response = llm.chat(
                [
                    ChatMessage(role="system", content=SYSTEM_PROMPT),
                    ChatMessage(role="user", content=prompt),
                ],
                tier=tier,  # type: ignore[arg-type]
                temperature=temperature,
                max_tokens=200,
                json_mode=True,
                purpose="eval.faithfulness",
            )
            parsed = _parse(response.text)
        except Exception as exc:  # 判一条失败不该让整批没有结果
            parsed = {"verdict": PARTIAL, "reason": f"裁判调用失败：{exc}"}

        verdict = parsed["verdict"]
        result.items.append(
            ClaimJudgement(
                claim_id=str(claim.get("claimId") or claim.get("id") or ""),
                verdict=verdict,
                reason=parsed.get("reason", ""),
            )
        )
        result.verdicts[verdict] = result.verdicts.get(verdict, 0) + 1
        result.judged += 1

    if result.judged:
        result.score = sum(_weight(i.verdict) for i in result.items) / result.judged
    return result


def _parse(text: str) -> dict[str, str]:
    """把裁判的输出解析成 `{verdict, reason}`。

    解析失败时判 **"部分支持"** 而不是"不支持"：裁判没答上来不等于
    证据不支持论点，把解析失败记成"不支持"会让忠实度因为格式问题而虚低——
    而虚低的数会被当成"质量真的变差了"，于是去改一个根本没坏的地方。
    """
    raw = (text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start : end + 1]
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {"verdict": PARTIAL, "reason": "裁判输出不是 JSON"}
    verdict = str(data.get("verdict") or "").strip()
    if verdict not in VERDICTS:
        return {"verdict": PARTIAL, "reason": f"裁判给了一个没见过的判定：{verdict!r}"}
    return {"verdict": verdict, "reason": str(data.get("reason") or "")}
