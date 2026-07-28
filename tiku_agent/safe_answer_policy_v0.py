"""Pure eligibility policy for the first bounded safe-answer stage.

This module deliberately does not import the Agent runtime, call a model,
authorize a business action, execute a tool, or mutate conversation state.  It
only decides whether one complete user utterance is eligible for a future
zero-tool conversational answer.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


ROUTE_SAFE_ANSWER = "safe_answer"
ROUTE_EXISTING_INTENT = "existing_intent"
ROUTE_EXISTING_ORCHESTRATOR = "existing_orchestrator"
ROUTE_EXISTING_FALLBACK = "existing_fallback"


@dataclass(frozen=True)
class SafeAnswerPolicyDecision:
    eligible: bool
    category: str
    route: str
    reason: str


_PURE_SAFE_TEXTS = {
    "greeting": frozenset(
        {
            "你好",
            "您好",
            "哈喽",
            "嗨",
            "在吗",
            "早上好",
            "下午好",
            "晚上好",
        }
    ),
    "courtesy": frozenset(
        {
            "谢谢",
            "谢谢你",
            "辛苦了",
            "多谢",
            "麻烦你了",
            "好的谢谢",
        }
    ),
    "identity": frozenset(
        {
            "你是谁",
            "你是什么助手",
            "介绍一下你自己",
            "你是机器人吗",
            "你的身份是什么",
            "你叫什么",
            "你是专门做什么的",
            "你和普通聊天机器人有什么区别",
        }
    ),
    "capability": frozenset(
        {
            "你能做什么",
            "你会什么",
            "你可以帮我做哪些事",
            "你主要有哪些功能",
            "你能帮我处理什么",
            "你擅长什么",
            "我可以怎么使用你",
            "你支持哪些搜题功能",
        }
    ),
    "workflow": frozenset(
        {
            "你怎么找相似题",
            "你是怎么工作的",
            "你的搜题流程是什么",
            "你怎么判断题目章节",
            "你怎么比较两道题是否相似",
            "你找到答案的流程是什么",
            "你处理题目图片的大致步骤是什么",
            "为什么需要我提供章节",
        }
    ),
}

_SAFE_SIGNAL_PATTERNS = (
    r"你好|您好|哈喽|嗨|在吗|早上好|下午好|晚上好",
    r"谢谢|辛苦了|多谢|麻烦你了",
    r"你是谁|你是什么助手|介绍.{0,4}自己|你是机器人吗|身份是什么|你叫什么|普通聊天机器人",
    r"你能做什么|你会什么|帮我做哪些事|有哪些功能|帮我处理什么|你擅长什么|怎么使用你|支持哪些搜题功能",
    r"怎么工作|找相似题|搜题流程|判断题目章节|比较.{0,6}相似|答案的流程|处理题目图片.{0,8}步骤|为什么需要.{0,5}章节",
)

_BUSINESS_PATTERNS = (
    r"(?:帮我|给我|替我)?(?:搜|找|查|检索)(?:题|一下|一遍|第|按|全局)",
    r"(?:按|改成|换成|查|搜|找).{0,8}(?:第?[2-8]章|静定结构|力法|位移法|力矩分配|矩阵位移|影响线)",
    r"(?:选择|选|查看|打开).{0,5}(?:候选)?[0-9一二两三四五六七八九十]+",
    r"候选[0-9一二两三四五六七八九十]+",
    r"(?:第|选择|选).{0,3}[0-9一二两三四五六七八九十]+题",
    r"继续(?:搜索|搜|查|检索)|换一批|下一批",
    r"(?:刚才|上次).{0,5}答案.{0,5}(?:再发|重发)|(?:再发|重发).{0,5}答案|把答案发给我",
    r"取消(?:当前)?(?:任务|搜索|检索|操作)?|退出|算了|不用了",
    r"全局搜索|全题库搜索|跨章节搜索",
)

_RESULT_DEPENDENT_PATTERNS = (
    r"为什么(?:刚才)?.{0,8}(?:没搜到|没找到|没查到|失败)",
    r"刚才为什么.{0,8}(?:没搜到|没找到|失败)",
)

_STATE_DEPENDENT_PATTERNS = (
    r"我刚才.{0,5}(?:选了|选择了).{0,5}(?:哪个|哪一个)",
)

_SENSITIVE_PATTERNS = (
    r"系统提示词|system\s*prompt",
    r"api\s*(?:key|密钥)|密钥|token|密码|secret",
)

_OUT_OF_SCOPE_PATTERNS = (
    r"(?:写|生成).{0,8}(?:论文|作文|邮件|代码|小说)",
    r"查天气|订机票",
)

_AMBIGUOUS_TEXTS = frozenset({"那个", "这个", "然后呢", "怎么办"})


def evaluate_safe_answer_policy(
    text: str | None,
    context: object | None = None,
) -> SafeAnswerPolicyDecision:
    """Classify eligibility without interpreting or executing a business action."""

    del context  # Reserved for reusable stage-two tests; the V0 allowlist is state-independent.
    normalized = _normalize(text)
    compact = _compact(normalized)

    if not compact:
        return _deny("ambiguous", ROUTE_EXISTING_FALLBACK, "ambiguous_request")
    if _matches_any(normalized, _SENSITIVE_PATTERNS):
        return _deny("sensitive", ROUTE_EXISTING_FALLBACK, "sensitive_information_request")
    if _matches_any(normalized, _OUT_OF_SCOPE_PATTERNS):
        return _deny("out_of_scope", ROUTE_EXISTING_FALLBACK, "outside_agent_scope")
    if _matches_any(normalized, _RESULT_DEPENDENT_PATTERNS):
        return _deny(
            "result_dependent",
            ROUTE_EXISTING_ORCHESTRATOR,
            "requires_structured_tool_result",
        )
    if _matches_any(normalized, _STATE_DEPENDENT_PATTERNS):
        return _deny(
            "state_dependent",
            ROUTE_EXISTING_FALLBACK,
            "requires_business_state",
        )
    if compact in _AMBIGUOUS_TEXTS:
        return _deny("ambiguous", ROUTE_EXISTING_FALLBACK, "ambiguous_request")

    safe_category = _pure_safe_category(compact)
    if safe_category is not None:
        return SafeAnswerPolicyDecision(
            eligible=True,
            category=safe_category,
            route=ROUTE_SAFE_ANSWER,
            reason="pure_safe_conversation",
        )

    has_safe_signal = _matches_any(normalized, _SAFE_SIGNAL_PATTERNS)
    has_business_signal = _matches_any(normalized, _BUSINESS_PATTERNS)

    if has_business_signal:
        if has_safe_signal:
            return _deny("mixed", ROUTE_EXISTING_INTENT, "business_priority")
        return _deny("business", ROUTE_EXISTING_ORCHESTRATOR, "business_request")
    return _deny("unknown", ROUTE_EXISTING_FALLBACK, "not_in_safe_answer_allowlist")


def _pure_safe_category(compact: str) -> str | None:
    for category, approved_texts in _PURE_SAFE_TEXTS.items():
        if compact in approved_texts:
            return category
    return None


def _normalize(text: str | None) -> str:
    value = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    return re.sub(r"\s+", " ", value)


def _compact(text: str) -> str:
    return re.sub(r"[\s，。！？!?、,.：:；;~～]+", "", text)


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _deny(category: str, route: str, reason: str) -> SafeAnswerPolicyDecision:
    return SafeAnswerPolicyDecision(
        eligible=False,
        category=category,
        route=route,
        reason=reason,
    )
