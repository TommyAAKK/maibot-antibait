# -*- coding: utf-8 -*-
"""赦免层：打回之前先看语境，判断这句话是不是「人也会这么说」。

四个通道，每个通道只在**它能解释的范围**内放行（全覆盖才赦免，不是打分凑数）：

  echo   复读/引用  与群里别人刚说过的话高度重合 → 整句可赦
  intent 请求意图  人家明确要总结/列步骤/做对比 → 只赦该意图下合理的规则
  meme   玩梗/起哄  短句 + 梗词/情绪词 → 只赦标点表情与口语类
  answer 回答问题  问句 + 短答 → 只赦口语类

硬性不赦免（AI 味本体，任何语境都不该放过）：
  自曝身份、结尾腔、开头腔、自定义硬规则、用户标 hard 的口癖。

设计原则：宁可漏赦，不可错赦。赦免是"减少误杀"，不是"提高通过率"。
纯函数，无 IO、无异步；上下文由调用方取好传进来。
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field as dc_field

try:
    from .rules import scan
except ImportError:  # 顶层模块导入
    from rules import scan

# ─────────────────────────── 规则归属 ───────────────────────────

# 任何通道都不能赦免
NEVER_AMNESTY = {
    "zipu",          # 作为 AI / 我是一个语言模型
    "jiewei",        # 希望对你有所帮助
    "kaitou",        # 这是一个很好的问题
    "extra_hard",    # 用户自定义硬规则
    "tic_hard",      # 用户标了 hard 的口癖
    "label_leak",    # 原始标签漏进正文：纯事故，任何语境都不赦
}

# 请求明确要求结构化时，这些规则是"按要求办事"，不是八股
# 人设/标点类：插件本身就把颜文字降权（kaomoji_weight=0.3），说明它不算八股。
# 既然不算八股，就不该妨碍语境识别——否则含颜文字的回复永远走不通 intent 通道。
# （这是补丁前就存在的缺口。）
_PERSONA_CODES = {"kaomoji", "emoji", "dash", "exclaim"}

INTENT_RULES = {
    # length_gate：对方要求总结/分步骤/做对比/解释原因时，说长是对的。
    # 但「玩梗」和「短答」两个通道不加——那两种场景说长没理由。
    "summarize": {"zongjie", "guodu", "liebiao", "changdu", "lianjie", "sanduan", "paili",
                  "length_gate"} | _PERSONA_CODES,
    "steps": {"sanduan", "liebiao", "guodu", "lianjie", "changdu", "paili",
              "length_gate"} | _PERSONA_CODES,
    "compare": {"duizhang", "duizhang2", "kansi", "yuqi", "liebiao", "paili", "changdu",
                "length_gate"} | _PERSONA_CODES,
    "explain": {"benzhi", "shuodaodi", "guigen", "guodu", "lianjie",
                "length_gate"} | _PERSONA_CODES,
}

INTENT_PATTERNS = [
    ("summarize", re.compile(r"总结|概括|归纳|梳理|大意|摘要|提炼")),
    ("steps", re.compile(r"步骤|流程|怎么做|如何做|分几步|教程|操作|手把手")),
    ("compare", re.compile(r"对比|比较|区别|差异|哪个好|优缺点|差别")),
    ("explain", re.compile(r"为什么|为啥|原因|原理|解释|分析|怎么回事|凭什么")),
]

# 玩梗/起哄能解释的规则：标点表情、口语、排比
MEME_RULES = {
    "emoji_burst", "kao_burst", "exclaim_burst",
    "emoji", "kaomoji", "dash", "exclaim",
    "yuci", "fanwen", "paili",
}

# 回答问题能解释的规则：只有口语类，结构类不算
ANSWER_RULES = {
    "paili", "yuci", "liebiao", "changdu", "lianjie", "fanwen",
    "kaomoji", "emoji", "dash", "exclaim",
}

# ─────────────────────────── 梗词表 ───────────────────────────

MEME_WORDS = [
    "卧槽", "我超", "笑死", "绷不住", "难绷", "破防", "麻了", "绝了", "离谱", "逆天",
    "啊这", "属实", "就这", "不至于", "好家伙", "救命", "蚌埠住了", "乐死",
    "有一说一", "讲道理", "真的会谢", "栓Q", "典中典", "急了", "孝死", "赢麻了", "寄了",
    "awsl", "xswl", "yyds", "233", "hhh", "哈哈哈", "哈哈",
]

CHEER_RE = re.compile(
    r"^(?:哈{2,}|h{3,}|草+|卧槽|我超|笑死|确实|真实|同意|绷|典|急|孝|赢|麻|乐|寄|"
    r"绝了|离谱|6{3,}|牛+|啊这|好家伙|就这|难绷|不至于)[啊呀吧呢嘛哦啦哟~～!！。，,]*$",
    re.I,
)

QUESTION_RE = re.compile(r"[？?]\s*$|吗|呢|怎么|为什么|为啥|啥|什么|多少|哪|谁|几点|如何|能不能|是不是|有没有|行不行")

# ─────────────────────────── 配置与结果 ───────────────────────────

@dataclass
class AmnestyConfig:
    enabled: bool = True
    echo_jaccard: float = 0.75      # 复读判定阈值
    meme_min_marks: int = 2         # 至少几个梗词才算玩梗
    answer_max_len: int = 60        # 短答上限
    min_echo_len: int = 4           # 太短的不算复读


@dataclass
class AmnestyContext:
    request_text: str = ""          # 上一条别人说的话（通常是提问）
    others: list = dc_field(default_factory=list)  # 群里其他人的发言


@dataclass
class Amnesty:
    allowed: bool = False
    channel: str = ""
    reason: str = ""
    score: float = 0.0
    codes: list = dc_field(default_factory=list)


# ─────────────────────────── 工具 ───────────────────────────

def _norm_text(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"[\s\W_]+", "", s)


def _bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def _jaccard(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    A, B = _bigrams(a), _bigrams(b)
    union = A | B
    return len(A & B) / len(union) if union else 0.0


def _meme_marks(text: str) -> list:
    found = []
    low = text.lower()
    for w in MEME_WORDS:
        if w.lower() in low and w not in found:
            found.append(w)
    if re.search(r"6{3,}", text) and "6" not in found:
        found.append("6")
    return found


def _intents(request_text: str) -> list:
    if not request_text:
        return []
    out = []
    for name, pat in INTENT_PATTERNS:
        if pat.search(request_text):
            out.append(name)
    return out


# ─────────────────────────── 判定 ───────────────────────────

def evaluate(text: str, verdict, ctx: AmnestyContext = None,
             cfg: AmnestyConfig = None) -> Amnesty:
    cfg = cfg or AmnestyConfig()
    ctx = ctx or AmnestyContext()
    if not cfg.enabled:
        return Amnesty(False, "", "赦免层已关闭")

    codes = [h.code for h in verdict.hits]
    if not codes:
        return Amnesty(False, "", "无命中")

    # 一票否决：含不可赦免规则，后面什么都不用看了
    forbidden = [c for c in codes if c in NEVER_AMNESTY]
    if forbidden:
        return Amnesty(False, "", "含不可赦免规则：%s" % "、".join(sorted(set(forbidden))),
                       codes=forbidden)

    # ① 复读/引用：与别人说过的话高度重合 → 整句可赦
    hit, score, src = _echo(text, ctx, cfg)
    if hit:
        return Amnesty(True, "echo", "复读/引用群里说过的话：%s" % src[:30],
                       score=score, codes=codes)

    # ② 请求意图：明确要求结构化 → 只赦该意图下合理的规则
    for intent in _intents(ctx.request_text):
        allow = INTENT_RULES.get(intent, set())
        if codes and all(c in allow for c in codes):
            return Amnesty(True, "intent", "对方要求%s，结构化是按要求办事" % _INTENT_CN.get(intent, intent),
                           score=1.0, codes=codes)

    # ③ 玩梗/起哄：短句 + 梗词
    marks = _meme_marks(text)
    cheer = bool(CHEER_RE.match(text.strip()))
    if (len(marks) >= cfg.meme_min_marks) or (marks and len(text) <= 20) or cheer:
        if all(c in MEME_RULES for c in codes):
            why = ("起哄/附和：" + (marks[0] if marks else text.strip()[:10])) if cheer \
                else "玩梗：" + "、".join(marks[:3])
            return Amnesty(True, "meme", why, score=float(len(marks) or 1), codes=codes)

    # ④ 回答问题：问句 + 短答 → 只赦口语类
    if ctx.request_text and QUESTION_RE.search(ctx.request_text) and len(text) <= cfg.answer_max_len:
        if all(c in ANSWER_RULES for c in codes):
            return Amnesty(True, "answer", "在回答对方的问题，口语化正常",
                           score=1.0, codes=codes)

    return Amnesty(False, "", "没有可解释的语境", codes=codes)


_INTENT_CN = {
    "summarize": "总结/归纳",
    "steps": "分步骤讲",
    "compare": "做对比",
    "explain": "解释原因",
}


def _echo(text: str, ctx: AmnestyContext, cfg: AmnestyConfig):
    """返回 (是否复读, 相似度, 来源片段)。"""
    t = _norm_text(text)
    if len(t) < cfg.min_echo_len:
        return False, 0.0, ""
    best, src = 0.0, ""
    for raw in ctx.others or []:
        s = _norm_text(raw)
        if len(s) < cfg.min_echo_len:
            continue
        # 复读的对象本身是八股 → 不算人类信号（防「机器人复读自己」漏洞）
        try:
            if scan(raw).blocked:
                continue
        except Exception:
            pass
        if t == s or (len(t) >= cfg.min_echo_len and (t in s or s in t)):
            return True, 1.0, raw
        j = _jaccard(t, s)
        if j > best:
            best, src = j, raw
    return best >= cfg.echo_jaccard, best, src


# ─────────────────────────── 可选：模型复核 ───────────────────────────

def build_llm_prompt(text: str, ctx: AmnestyContext) -> str:
    lines = [
        "判断下面这句群聊回复，如果直接发出去，会不会被人觉得「像 AI 写的」。",
        "重点看三种情况：在玩梗、在复读别人的话、在认真回答别人的问题。",
        "这几种情况下，正常人类也会这么说话，就不算 AI 味。",
        "",
        "最近别人说的话：",
    ]
    for m in (ctx.others or [])[-5:]:
        lines.append("- " + str(m)[:80])
    lines += [
        "",
        "要判断的回复：",
        text[:400],
        "",
        '只输出一行 JSON：{"amnesty": true 或 false, "reason": "不超过20字"}',
    ]
    return "\n".join(lines)


def parse_llm_verdict(raw: str):
    """从模型输出里抠出 JSON；抠不到就当没赦免（失败即不放行）。"""
    if not raw:
        return None
    m = re.search(r"\{.*?\}", str(raw), re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {
        "allowed": bool(data.get("amnesty")),
        "reason": str(data.get("reason") or "")[:40],
    }
