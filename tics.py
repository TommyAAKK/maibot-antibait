# -*- coding: utf-8 -*-
"""口癖自动生成：调一次模型拿候选，然后**过校验**再入库。

核心不是 prompt，是校验。模型很爱给 `.*呢.*` 这种，一开就把所有回复拦了。
所以候选必须落在「2% ~ 30%」的命中率区间里：
  低于 2%  → 偶发，是噪声
  高于 30% → 人人都这么说，是正常表达，拦它等于拦一切
"""
from __future__ import annotations

import json
import os
import re
import tempfile

MAX_PATTERN_LEN = 40
MIN_RATE = 0.02
MAX_RATE = 0.30

_NESTED_QUANT = re.compile(r"\([^)]*[+*][^)]*\)\s*[+*]")


def compile_safe(pattern: str):
    """返回 (compiled, 错误说明)。不安全的直接拒绝。"""
    pattern = (pattern or "").strip()
    if not pattern:
        return None, "空模式"
    if len(pattern) > MAX_PATTERN_LEN:
        return None, "太长（>%d 字符）" % MAX_PATTERN_LEN
    if _NESTED_QUANT.search(pattern):
        return None, "嵌套量词，有灾难回溯风险"
    if pattern.startswith(".*") or pattern.endswith(".*"):
        return None, "首尾 .* 太宽，会命中所有内容"
    try:
        return re.compile(pattern), ""
    except re.error as e:
        return None, "编译失败：%s" % e


def hit_rate(pattern: str, samples) -> float:
    rx, err = compile_safe(pattern)
    if rx is None or not samples:
        return 0.0
    hit = sum(1 for s in samples if rx.search(s))
    return hit / float(len(samples))


def validate_candidate(pattern: str, samples, min_rate: float = MIN_RATE, max_rate: float = MAX_RATE):
    """返回 (ok, reason, rate)。"""
    rx, err = compile_safe(pattern)
    if rx is None:
        return False, err, 0.0
    rate = hit_rate(pattern, samples)
    if rate < min_rate:
        return False, "命中率 %.1f%% 太低（偶发，不是口癖）" % (rate * 100), rate
    if rate > max_rate:
        return False, "命中率 %.1f%% 太高（人人都这么说，是正常表达）" % (rate * 100), rate
    return True, "", rate


def _bigrams(s: str) -> set:
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similar(a: str, b: str) -> float:
    A, B = _bigrams(a), _bigrams(b)
    if not A or not B:
        return 0.0
    return len(A & B) / float(len(A | B))


PROMPT = """下面是某个聊天机器人最近的发言记录，一行一条。

请找出它**反复出现、带有个人色彩**的语言习惯，输出 JSON 数组，不要输出其他内容。

要求：
1. pattern 必须是 Python 正则，只匹配口癖本身（几个字），不要匹配整句
2. 不要用 .* 开头或结尾，不要写嵌套量词
3. severity 只能是 "soft" 或 "hard"，拿不准就用 soft
4. 最多 10 条，宁缺毋滥

格式示例：
[{"pattern": "喵[~～]?$", "note": "结尾带喵", "severity": "soft"}]

发言记录：
%s
"""


def build_prompt(samples, limit: int = 200) -> str:
    lines = [s.replace("\n", " ").strip() for s in samples if s and s.strip()]
    return PROMPT % "\n".join(lines[:limit])


def _extract_json_array(raw: str):
    start = raw.find("[")
    end = raw.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None


def suggest(samples, llm_call, existing=None):
    """llm_call(prompt: str) -> str（同步）。

    返回 {"accepted": [...], "rejected": [...]}，全部是**草稿**，不落配置。
    """
    return suggest_from_raw(llm_call(build_prompt(samples)), samples, existing=existing)


def suggest_from_raw(raw, samples, existing=None):
    """已经拿到模型原文时用这个（插件里是 await 完再调）。"""
    existing = list(existing or [])
    data = _extract_json_array(raw if isinstance(raw, str) else str(raw))
    if data is None:
        return {"accepted": [], "rejected": [{"pattern": "", "reason": "模型没返回合法 JSON 数组"}]}

    accepted, rejected = [], []
    seen = list(existing)
    for item in data:
        if not isinstance(item, dict):
            continue
        pattern = str(item.get("pattern") or "").strip()
        note = str(item.get("note") or "").strip()
        severity = str(item.get("severity") or "soft").lower()
        if severity not in ("soft", "hard"):
            severity = "soft"

        ok, reason, rate = validate_candidate(pattern, samples)
        if ok:
            dup = next((s for s in seen if similar(s, pattern) > 0.8), None)
            if dup is not None:
                ok, reason = False, "与已有模式太像：%s" % dup
        if ok:
            seen.append(pattern)
            examples = []
            for s in samples:
                if s not in examples and _safe_search(pattern, s):
                    examples.append(s)
                if len(examples) >= 3:
                    break
            accepted.append({
                "pattern": pattern,
                "note": note,
                "severity": severity,
                "hit_rate": round(rate, 4),
                "examples": examples,
            })
        else:
            rejected.append({"pattern": pattern, "note": note, "reason": reason})

    return {"accepted": accepted, "rejected": rejected}


def _safe_search(pattern: str, text: str) -> bool:
    rx, _ = compile_safe(pattern)
    return bool(rx and rx.search(text))


# ─────────────────────────── 草稿与正式库的存取 ───────────────────────────

def atomic_write_json(path: str, data) -> None:
    """原子写：临时文件 + os.replace，避免写一半被读到。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
