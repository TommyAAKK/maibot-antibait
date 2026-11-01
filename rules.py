# -*- coding: utf-8 -*-
"""八股规则引擎：遮蔽管线 + 三档规则 + 连续打分。

纯函数，无 IO、无异步、不依赖 maibot_sdk —— 方便单测、回放、离线调参。

设计要点（对应设计文档）：
1. 先遮蔽再计分：URL / 代码块 / 引用行 / 颜文字 / emoji 各自成为 span，
   标点规则只看残渣，物理上杜绝「（— w —）里两个破折号再算一次」。
2. 一个 span 只归属一条规则，每类单独设上限。
3. 标点表情类合计封顶（默认 2.0），低于打回门槛（默认 3.0），
   所以纯标点问题永远打不回，必须叠加结构性问题。
4. 打分是连续值，供「重生成后择优」比较大小；硬规则记 100 分，
   保证任何有硬伤的结果在比较中必输。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field as dc_field

PH = "\x00"  # 占位符分隔符，正常文本里不可能出现

# ─────────────────────────────── 遮蔽 ───────────────────────────────

CODE_FENCE_RE = re.compile(r"```.*?```", re.S)
CODE_INLINE_RE = re.compile(r"`[^`\n]{1,80}`")
URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff]+|www\.[^\s\u4e00-\u9fff]+")
QUOTE_LINE_RE = re.compile(r"^[ \t]*>.*$", re.M)
KAOMOJI_RE = re.compile(r"[（(][^（）()\n]{0,14}[）)]")

_EMOJI_CLASS = (
    "\U0001F300-\U0001FAFF"   # 符号与象形文字
    "\U0001F000-\U0001F2FF"   # 麻将/扑克/括注字母
    "\U00002600-\U000027BF"   # 杂项符号、装饰符号
    "\U00002B00-\U00002BFF"   # 箭头杂项
    "\U0000FE0F"              # 变体选择符
)
EMOJI_RE = re.compile("[" + _EMOJI_CLASS + "]+")

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SYMBOL_RE = re.compile(r"[^\w\s]")

# 挂在颜文字后面的装饰符，属于颜文字本身（不单独算 emoji）
KAOMOJI_DECO = "✧☆★♪♫✩✦❀✿♥♡٩۶و～~·°"


@dataclass
class Masked:
    text: str
    spans: list  # list[tuple[kind, 原文]]


def mask(raw: str) -> Masked:
    """把不该参与判定的片段替换成占位符，并记录原文。"""
    spans: list = []

    def put(kind: str, s: str) -> str:
        idx = len(spans)
        spans.append((kind, s))
        return "%s%s%d%s" % (PH, kind[0].upper(), idx, PH)

    t = raw
    t = CODE_FENCE_RE.sub(lambda m: put("code", m.group(0)), t)
    t = CODE_INLINE_RE.sub(lambda m: put("code", m.group(0)), t)
    t = URL_RE.sub(lambda m: put("url", m.group(0)), t)
    t = QUOTE_LINE_RE.sub(lambda m: put("quote", m.group(0)), t)

    def _kao(m: re.Match) -> str:
        s = m.group(0)
        inner = s[1:-1]
        # 含汉字 → 不是颜文字（是「（小声）」这类动作描写，交给口癖规则）
        if CJK_RE.search(inner):
            return s
        # 没有任何符号 → 只是括号括了个词，不算颜文字
        if not _SYMBOL_RE.search(inner):
            return s
        return put("kaomoji", s)

    t = KAOMOJI_RE.sub(_kao, t)

    # 颜文字尾巴上的装饰符（✧☆★♪♡…）属于颜文字的一部分，
    # 不能单独算一个 emoji —— 否则同一段 `(｡•̀ᴗ-)✧` 会被计两次分。
    def _absorb_deco(m: re.Match) -> str:
        ph, deco = m.group(1), m.group(2)
        idx = int(re.search(r"%sK(\d+)%s" % (PH, PH), ph).group(1))
        kind, s = spans[idx]
        spans[idx] = (kind, s + deco)
        return ph

    t = re.sub(r"(%sK\d+%s)([%s]+)" % (PH, PH, KAOMOJI_DECO), _absorb_deco, t)

    t = EMOJI_RE.sub(lambda m: put("emoji", m.group(0)), t)
    return Masked(t, spans)


def unmask(text: str, spans: list) -> str:
    out = text
    for i, (kind, s) in enumerate(spans):
        out = out.replace("%s%s%d%s" % (PH, kind[0].upper(), i, PH), s)
    return out


def span_counts(spans: list) -> dict:
    c = {"code": 0, "url": 0, "quote": 0, "kaomoji": 0, "emoji": 0}
    for kind, _ in spans:
        c[kind] = c.get(kind, 0) + 1
    return c


def _norm(s: str) -> str:
    """只用于检测：全角转半角，避免「！！」和「!!」漏判。改写输出时用原文。"""
    return unicodedata.normalize("NFKC", s)


# ─────────────────────────────── 规则表 ───────────────────────────────
# (code, 显示名, 正则)

HARD_RULES = [
    ("zongjie", "总结腔", r"综上所述|总而言之|总的来说|综上[，,、]"),
    ("guodu", "过渡腔", r"值得(一提|注意)的是|需要(指出|说明)的是|更重要的是|与此同时|不可否认"),
    ("zipu", "自曝身份", r"作为(一个)?(AI|人工智能|语言模型|大模型)|我是(一个)?(AI|人工智能|语言模型)"),
    ("jiewei", "结尾腔", r"希望(对你|能对你有|对你有所)帮助|如果(你)?还有(什么)?(问题|疑问|需要).{0,12}?随时|有任何(问题|疑问).{0,10}?随时"),
    ("kaitou", "开头腔", r"这是(一个)?(很)?好的?问题|好问题[，,].{0,6}让我"),
    ("duizhang", "对仗腔（不是…而是…）", r"不是[^，。！？；\n]{1,24}[，,]\s*而是"),
    ("duizhang2", "对仗腔（不仅…更…）", r"不仅[^，。！？；\n]{1,24}[，,]\s*(更|还|也)"),
    ("kansi", "对仗腔（看似…实则…）", r"看似.{0,24}?实则"),
    ("yuqi", "对仗腔（与其说…不如说…）", r"与其说.{0,24}?不如说"),
    ("label_leak", "结构残留（原始标签漏进正文）",
     r"</?(message|quote|user|image|record|forward)\b[^>\n]{0,120}>"
     r"|msg_id\s*="
     r"|\[(图片|表情|语音|视频|文件|动画表情|回复)[：:]"
     # 工具/调试残留：模型把自己的工具调用痕迹一起说出来了
     # 形如 [glass-meme-lookup]、[memory-search]、[web_fetch]（必须带分隔符或下划线，避免误伤 [doge]）
     r"|\[\s*[A-Za-z][A-Za-z0-9]{1,}(?:[-_.][A-Za-z0-9]+){1,3}\s*\]"
     # 形如 [未匹配到词条，结合上下文谨慎回复]
     r"|\[(未匹配到|没有找到|未找到|工具调用|工具返回|调用结果)[^\]\n]{0,60}\]"),
]

# 软规则里的「词汇类」：这些词真人也会说，单次只记 1 分，且合计封顶
WORD_RULES = [
    ("benzhi", "总结词（本质上）", r"本质上(说|讲)?"),
    ("shuodaodi", "总结词（说到底）", r"说到底"),
    ("guigen", "总结词（归根结底）", r"归根(结底|到底)"),
    ("wuyong", "总结词（毋庸置疑）", r"毋庸(置疑|讳言)"),
    ("niti", "套路句（你提到的…让我想到）", r"你(提到|说)的.{0,20}?让我(想到|想起)"),
]

# 附和/服软腔：单条本身人也会说，但连着来就是 AI 味。
# 所以每条只记 1 分，靠叠加上去打回线（见 apply_sequence 的序列判定）。
FU_CONG_RULES = [
    ("diezi", "叠词服软（行吧行吧/行行行）",
     r"^\s*(行吧行吧|行行行|好好好|是是是|对对对|得得得|算了算了|服了服了)"),
    ("zibian", "认输自贬（我菜/我傻）", r"(我菜|我蠢|我笨|我傻|我认输|我服了|认了认了|我认了)"),
    ("kuata", "夸对方赢（你赢了/你厉害）",
     r"(你赢了|你厉害|还是你.{0,4}(行|强|牛)|参数党赢了|你牛|你说得对)"),
]
# 句尾孤立附和词：必须挂在句子末尾才算（「科普嗯嗯」），中间出现不算
TAIL_FUHE_RE = re.compile(r"(嗯嗯|哦哦|对对|是是|嗯呢|哦克)[。！!～~…\s]*$")

PARTICLES = "呢呀哦啦嘛哟诶欸咯嘞"
COLLOQUIAL_RE = re.compile(r"[啊呀吧呢嘛哈嘿嗯哦诶欸咯嘞]")
CONNECTIVES_RE = re.compile(r"而且|并且|此外|同时|另外|再者")
LIST_RE = re.compile(r"(?m)^[ \t]*(?:\d+[.、)）]|[一二三四五六七八九十]+[、.）])|第一[，,]|首先[，,]")
RHETORICAL_RE = re.compile(r"难道|岂不|不是吗|何必|何尝")


@dataclass
class Hit:
    code: str
    label: str
    weight: float
    hard: bool
    sample: str = ""


@dataclass
class Verdict:
    score: float = 0.0
    hard: int = 0
    hits: list = dc_field(default_factory=list)
    blocked: bool = False

    def labels(self) -> list:
        hard = [h.label for h in self.hits if h.hard]
        soft = [h.label for h in self.hits if not h.hard]
        return hard + soft

    def reason_for(self, attempt: int) -> str:
        names = self.labels()[:3]
        joined = "、".join(names) if names else "八股特征"
        if attempt <= 1:
            return "回复命中了「%s」。请换成更口语、更短的表达，不要用总结、对仗、排比这类结构。" % joined
        if attempt == 2:
            samples = [h.sample for h in self.hits if h.sample][:4]
            tail = ("；这些说法一个都不要出现：%s" % "、".join(samples)) if samples else ""
            return "回复命中了「%s」。请换成更口语、更短的表达%s。" % (joined, tail)
        return "回复命中了「%s」。请只回一句 10 字以内的短话，不要解释、不要总结。" % joined

    def summary(self) -> str:
        if not self.hits:
            return "干净"
        parts = ["%s(%.1f)" % (h.label, h.weight) for h in self.hits]
        return "、".join(parts)


@dataclass
class RuleConfig:
    soft_threshold: float = 3.0
    punctuation_cap: float = 2.0
    word_cap: float = 2.0
    tic_cap: float = 2.0
    min_length: int = 6
    kaomoji_weight: float = 0.3                            # 颜文字是很多人的人设特征，不该和八股同权
    fucong: bool = True                                 # 附和/服软腔这一档（叠词、认输、夸对方赢、句尾嗯嗯）
    long_text_threshold: int = 200                      # 超过这个字数算「产出物」（榜单/教程/总结），结构类规则不适用
    extra_hard: list = dc_field(default_factory=list)   # list[str] 正则
    tics: list = dc_field(default_factory=list)         # list[(pattern, severity, note)]


# ─────────────────────────────── 打分 ───────────────────────────────

WORD_CODES = {c for c, _, _ in WORD_RULES}
PUNCT_CODES = {"kaomoji", "emoji", "dash", "exclaim"}

# 「结构类」规则描述的是文本骨架（列表、长句、排比、对仗）。
# 长文本（榜单、教程、周报）天生就有这些骨架，拿来判八股只会误伤，故超过
# long_text_threshold 后整类跳过；而「事故类」规则（标签漏出、自曝 AI）任何长度都要查。
STRUCTURAL_HARD_CODES = {"duizhang", "duizhang2"}
STRUCTURAL_SOFT_CODES = {"paili", "liebiao", "changdu", "lianjie"}


def _add(hits: list, code: str, label: str, weight: float, sample: str, hard: bool = False) -> None:
    hits.append(Hit(code, label, weight, hard, sample))


def scan(raw: str, cfg: RuleConfig | None = None) -> Verdict:
    cfg = cfg or RuleConfig()
    m = mask(raw)
    text = _norm(m.text)
    counts = span_counts(m.spans)
    hits: list = []
    long_text = len(text) > cfg.long_text_threshold

    # ── A 档：硬规则 ──
    for code, label, pat in HARD_RULES:
        if long_text and code in STRUCTURAL_HARD_CODES:
            continue
        mm = re.search(pat, text, re.M)
        if mm:
            _add(hits, code, label, 100.0, mm.group(0), hard=True)

    if re.search(r"首先", text) and re.search(r"其次", text) and re.search(r"最后", text):
        _add(hits, "sanduan", "三段式（首先/其次/最后）", 100.0, "首先…其次…最后", hard=True)

    for pat in cfg.extra_hard:
        try:
            mm = re.search(pat, text)
        except re.error:
            continue
        if mm:
            _add(hits, "extra_hard", "自定义硬规则", 100.0, mm.group(0), hard=True)

    # 极端标点：emoji 按**字符数**算，不是按「连续段」算。
    # 「堆砌」是密度概念：短消息里塞一堆才算，长文本里出现几个是正常的。
    emoji_chars = sum(len(s.replace("\ufe0f", "")) for k, s in m.spans if k == "emoji")
    n_kao = counts["kaomoji"]
    n_exc = len(re.findall(r"[!！]{2,}", text))
    kao_burst = n_kao >= 3 and len(text) <= 25 * n_kao
    emoji_burst = emoji_chars >= 5 and len(text) <= 25 * emoji_chars
    exclaim_burst = n_exc >= 4 and len(text) <= 25 * n_exc

    if emoji_burst:
        _add(hits, "emoji_burst", "emoji 堆砌", 100.0, "%d 个 emoji" % emoji_chars, hard=True)
    if kao_burst:
        _add(hits, "kao_burst", "颜文字堆砌", 100.0, "%d 个颜文字" % n_kao, hard=True)
    if exclaim_burst:
        _add(hits, "exclaim_burst", "感叹号堆砌", 100.0, "连续感叹号", hard=True)

    # ── B 档：词汇类软规则（合计封顶）──
    for code, label, pat in WORD_RULES:
        mm = re.search(pat, text, re.M)
        if mm:
            _add(hits, code, label, 1.0, mm.group(0))

    # ── B 档：标点表情类（先遮蔽，故不会互相重复计分）──
    if counts["kaomoji"] and not kao_burst:
        _add(hits, "kaomoji", "颜文字", cfg.kaomoji_weight,
             next((s for k, s in m.spans if k == "kaomoji"), ""))
    if counts["emoji"] and not emoji_burst:
        _add(hits, "emoji", "emoji", 1.0, next((s for k, s in m.spans if k == "emoji"), ""))
    if re.search(r"—{2,}", text):
        _add(hits, "dash", "破折号", 0.5, "——")
    if re.search(r"[!！]{2,}", text) and not exclaim_burst:
        _add(hits, "exclaim", "连续感叹号", 0.5, "！！")

    # ── B 档：结构类软规则（长文本整类跳过，见 STRUCTURAL_SOFT_CODES 注释）──
    sanduan_hit = any(h.code == "sanduan" for h in hits)
    if not long_text:
        if not sanduan_hit and _has_parallel(text):
            _add(hits, "paili", "排比三连", 1.0, _parallel_sample(text))
    n_particle = _particle_endings(text)
    if n_particle >= 2:
        _add(hits, "yuci", "句尾语气词堆叠", 1.0, "%d 句语气词结尾" % n_particle)
    if _rhetorical_end(text):
        _add(hits, "fanwen", "反问收尾", 1.0, "反问")
    if not long_text:
        if LIST_RE.search(text):
            _add(hits, "liebiao", "列表化", 1.0, "列表结构")
        if len(text) > 80 and not COLLOQUIAL_RE.search(text):
            _add(hits, "changdu", "过长且无口语", 1.0, "%d 字" % len(text))
        n_conn = len(CONNECTIVES_RE.findall(text))
        if n_conn >= 3:
            _add(hits, "lianjie", "连接词堆叠", 1.0, "%d 个连接词" % n_conn)

    # ── B 档：附和/服软腔（单条 1 分，靠序列叠加）──
    if cfg.fucong:
        for code, label, pat in FU_CONG_RULES:
            mm = re.search(pat, text, re.M)
            if mm:
                _add(hits, code, label, 1.0, mm.group(0))
        # 句尾附和词：先摘掉尾部占位符（颜文字/emoji 已被遮蔽），再判句末
        tail = re.sub(r"%s[A-Z]\d+%s" % (PH, PH), "", text).rstrip()
        mm = TAIL_FUHE_RE.search(tail)
        if mm:
            _add(hits, "fuhe", "句尾附和（嗯嗯/哦哦）", 1.0, mm.group(0))

    # ── C 档：用户配置的口癖 ──
    for item in cfg.tics:
        try:
            pat, sev, note = item[0], item[1], item[2]
        except (TypeError, IndexError):
            continue
        try:
            mm = re.search(pat, text)
        except re.error:
            continue
        if mm:
            hard = str(sev).lower() == "hard"
            _add(hits, "tic_hard" if hard else "tic_soft",
                 "口癖（%s）" % (note or pat), 100.0 if hard else 1.0, mm.group(0), hard=hard)

    # ── 汇总 ──
    hard_n = sum(1 for h in hits if h.hard)
    word_s = min(sum(h.weight for h in hits if h.code in WORD_CODES), cfg.word_cap)
    punct_s = min(sum(h.weight for h in hits if h.code in PUNCT_CODES), cfg.punctuation_cap)
    struct_s = sum(h.weight for h in hits if h.code in {"paili", "yuci", "fanwen", "liebiao", "changdu", "lianjie",
                                                       "diezi", "zibian", "kuata", "fuhe"})
    tic_s = min(sum(h.weight for h in hits if h.code == "tic_soft"), cfg.tic_cap)
    score = hard_n * 100.0 + word_s + punct_s + struct_s + tic_s

    # 把占位符还原，让 retry_reason 里的示例是给人看的样子
    for h in hits:
        h.sample = unmask(h.sample, m.spans)

    return Verdict(
        score=score,
        hard=hard_n,
        hits=hits,
        blocked=(hard_n > 0 or score >= cfg.soft_threshold),
    )


# ─────────────────────── 序列判定（要看上一条回复）───────────────────────
# 单条「行行行」「嗯嗯」人也会说；连着三条都在服软、三条都用同一个颜文字，
# 才是机器味。这类特征只能在跨回复层面判，所以单独一个函数。

FU_CONG_CODES = {"diezi", "zibian", "kuata", "fuhe"}


def _kaomoji_of(raw: str) -> str:
    m = mask(raw or "")
    ks = [s for k, s in m.spans if k == "kaomoji"]
    return ks[-1] if ks else ""


def apply_sequence(verdict: Verdict, raw: str, prev_texts, cfg: RuleConfig | None = None) -> Verdict:
    """在单条判定之上叠加「跟上一句像」的扣分。

    raw: 当前这条回复的原文。
    prev_texts: 同一会话里机器人**自己**最近说过的话，最近的在最前。
    """
    cfg = cfg or RuleConfig()
    if not prev_texts:
        return verdict
    prev = prev_texts[0] if isinstance(prev_texts, (list, tuple)) else prev_texts

    extra = 0.0

    # 1) 同款颜文字连发
    cur_k = _kaomoji_of(raw)
    if cur_k and cur_k == _kaomoji_of(prev):
        _add(verdict.hits, "kao_repeat", "同款颜文字连发", 1.0, cur_k)
        extra += 1.0

    # 2) 连续附和服软
    if any(h.code in FU_CONG_CODES for h in verdict.hits) and any(
            h.code in FU_CONG_CODES for h in scan(prev, cfg).hits):
        _add(verdict.hits, "fucong_repeat", "连续附和服软", 1.0, "上一条也在服软")
        extra += 1.0

    if extra:
        verdict.score += extra
        verdict.blocked = verdict.hard > 0 or verdict.score >= cfg.soft_threshold
    return verdict


# ─────────────────────────── 结构类辅助判定 ───────────────────────────

def _clauses(text: str) -> list:
    return [c.strip() for c in re.split(r"[，,。！？!?\n；;]", text) if c.strip()]


def _has_parallel(text: str) -> bool:
    """连续三个短句、长度接近、**且有共同的重复结构** → 排比。

    只看「三个分句长度接近」会把普通叙述误判成排比（中文句子本来就多四到十字）。
    真排比一定有重复的骨架：同首字（要快/要稳/要省）、同二字头、同二字尾，
    或三个分句共有一个二字片段。
    """
    cs = _clauses(text)
    for i in range(len(cs) - 2):
        trio = cs[i:i + 3]
        if not all(2 <= len(c) <= 14 for c in trio):
            continue
        lens = [len(c) for c in trio]
        if max(lens) - min(lens) > 2:
            continue
        if _parallel_linked(trio):
            return True
    return False


def _parallel_linked(trio: list) -> bool:
    """三个分句之间是否有真实的重复结构。"""
    if len({c[0] for c in trio}) == 1:          # 要快 / 要稳 / 要省
        return True
    if len({c[:2] for c in trio}) == 1:         # 第一… / 第一…
        return True
    if len({c[-2:] for c in trio}) == 1:        # …成本 / …收益 / …风险 之外的同尾
        return True
    common = None
    for c in trio:
        grams = {c[j:j + 2] for j in range(len(c) - 1)}
        common = grams if common is None else (common & grams)
        if not common:
            return False
    return bool(common)


def _parallel_sample(text: str) -> str:
    cs = _clauses(text)
    for i in range(len(cs) - 2):
        trio = cs[i:i + 3]
        lens = [len(c) for c in trio]
        if all(2 <= len(c) <= 14 for c in trio) and max(lens) - min(lens) <= 2 \
                and _parallel_linked(trio):
            return "，".join(trio)
    return "排比"


def _particle_endings(text: str) -> int:
    sents = [s.strip() for s in re.split(r"[。！？!?\n]", text) if s.strip()]
    return sum(1 for s in sents if s and s[-1] in PARTICLES)


def _rhetorical_end(text: str) -> bool:
    sents = [s.strip() for s in re.split(r"[。！？!?\n]", text) if s.strip()]
    if not sents:
        return False
    last = sents[-1]
    return ("？" in text[-3:] or "?" in text[-3:]) and bool(RHETORICAL_RE.search(last))


# ─────────────────────────── 机械净化 ───────────────────────────

LEAD_SUMMARY_RE = re.compile(r"^[ \t]*(?:总的来说|综上所述|总而言之|总之|简单来说|简单说|首先)[，,、:：]?[ \t]*")


def scrub(raw: str, cfg: RuleConfig | None = None) -> str:
    """零成本的机械清理：只动明显多余的东西，不重写句子。

    在遮蔽后的文本上操作，所以引用块、代码块、颜文字内部不会被误改。
    """
    m = mask(raw)
    t = m.text
    t = LEAD_SUMMARY_RE.sub("", t)
    t = re.sub(r"([!！])\1{2,}", r"\1", t)      # 三个以上感叹号压成一个
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = unmask(t, m.spans)
    return t.strip()


def should_check(raw: str, cfg: RuleConfig | None = None) -> bool:
    cfg = cfg or RuleConfig()
    if not raw or not raw.strip():
        return False
    m = mask(raw)
    residue = re.sub(r"%s[A-Z]\d+%s" % (PH, PH), "", m.text).strip()
    if len(residue) < cfg.min_length:
        return False
    return True
