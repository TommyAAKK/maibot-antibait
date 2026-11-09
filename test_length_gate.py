# -*- coding: utf-8 -*-
"""长度闸门的专项测试（独立文件，不改原 tests.py 的计数）。

覆盖四类：
  A. 闸门本身的判定（开/关、字数、句数、硬/软）
  B. 豁免逻辑（长文本产出物、引用代码占比、请求意图）
  C. 打回措辞（必须带「只回一句」这个关键指令）
  D. 端到端（模拟 antibait 的两次 hook 调用，看最终发出的是不是短的那条）
"""
from __future__ import annotations

import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import amnesty as amnesty_mod
import rules as R

PASS = 0
FAIL = 0
FAILS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % name)
    else:
        FAIL += 1
        FAILS.append(name)
        print("  ✗ %s   %s" % (name, detail))


def cfg(**kw) -> R.RuleConfig:
    c = R.RuleConfig()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# 真实生产样本（来自 728 条真实回复，长度分布的代表点）
SHORT = "确实"                                              # 2 字，真人常见
REAL_HUMAN = "踩刹车了"                                       # 4 字
BOT_TYPICAL = "主人，本鲸盯了两遍真没看到老头乐，你是不是编个地狱笑话钓我呀ww(´ω`)"
BOT_LONG = ("主人，本鲸盯了两遍真没看到老头乐，你是不是编个地狱笑话钓我呀ww"
            + "真压了功德确实得清零，给个时间点嘛(´ω`)")
LONG_ARTICLE = "今日群聊流水账来了(￣▽￣)\n" + "".join(
    "第%d条：今天聊了很多事情，大家都挺开心的，天气也不错。" % i for i in range(12))


def test_gate_switch() -> None:
    print("\n[A] 闸门开关与基本判定")
    check("默认关闭：33 字不打回", not R.scan(BOT_TYPICAL, cfg()).blocked)
    c = cfg(length_gate=True, length_gate_max=40)
    check("开启后：33 字仍不打回", not R.scan(BOT_TYPICAL, c).blocked,
          str(R.scan(BOT_TYPICAL, c).score))
    v = R.scan(BOT_LONG, c)
    check("开启后：50 字命中长度闸门", any(h.code == "length_gate" for h in v.hits),
          str([h.code for h in v.hits]))
    check("默认是硬规则，命中即打回", v.hard == 1 and v.blocked,
          "hard=%d blocked=%s" % (v.hard, v.blocked))
    check("硬规则记 100 分（保证在择优里必胜）", v.score >= 100.0, "score=%.1f" % v.score)

    # 软模式仍可用（想只记分不打回时）
    csoft = cfg(length_gate=True, length_gate_max=40, length_gate_hard=False)
    vsoft = R.scan(BOT_LONG, csoft)
    check("显式设软模式：记 1 分且不单独越线",
          vsoft.hard == 0 and not vsoft.blocked, "hard=%d score=%.1f" % (vsoft.hard, vsoft.score))
    check("软模式仍出现在命中列表里", any(h.code == "length_gate" for h in vsoft.hits))

    c2 = cfg(length_gate=True, length_gate_max=40, length_gate_hard=True)
    v2 = R.scan(BOT_LONG, c2)
    check("显式硬模式：命中即打回", v2.blocked and v2.hard == 1, "hard=%d" % v2.hard)

    c3 = cfg(length_gate=True, length_gate_max=30)
    check("阈值 30：33 字命中", any(h.code == "length_gate" for h in R.scan(BOT_TYPICAL, c3).hits))
    c4 = cfg(length_gate=True, length_gate_max=10)
    check("阈值 10：短句仍不命中", not any(h.code == "length_gate"
                                     for h in R.scan(REAL_HUMAN, c4).hits))


def test_sentence_count() -> None:
    print("\n[A2] 按句数判定（说完还要再补一句这个动作本身）")
    c = cfg(length_gate=True, length_gate_max=999, length_gate_max_sents=2)
    one = "这也太能压了吧"
    two = "这也太能压了吧。说好的月底呢"
    three = "这也太能压了吧。说好的月底呢。真是的"
    check("一句不打回", not any(h.code == "length_gate" for h in R.scan(one, c).hits))
    check("两句不打回（上限=2）", not any(h.code == "length_gate" for h in R.scan(two, c).hits))
    check("三句命中", any(h.code == "length_gate" for h in R.scan(three, c).hits))
    c0 = cfg(length_gate=True, length_gate_max=999, length_gate_max_sents=0)
    check("sents=0 时关闭句数判定", not any(h.code == "length_gate" for h in R.scan(three, c0).hits))


def test_exemptions() -> None:
    print("\n[B] 豁免：不该打回的别打回")
    c = cfg(length_gate=True, length_gate_max=40)
    v = R.scan(LONG_ARTICLE, c)
    check("长文本产出物（>200字）不打回", not any(h.code == "length_gate" for h in v.hits),
          "len=%d" % len(LONG_ARTICLE))
    c_noskip = cfg(length_gate=True, length_gate_max=40, length_gate_skip_long_text=False)
    check("skip_long_text=False 时长文会被判", any(
        h.code == "length_gate" for h in R.scan(LONG_ARTICLE, c_noskip).hits))

    quoted = "> " + "这是别人说的很长很长的一段话，完全不是我自己写的，" * 4 + "\n确实"
    check("引用占比高不打回", not any(
        h.code == "length_gate" for h in R.scan(quoted, c).hits), "len=%d" % len(quoted))

    code = "```\n" + "print('hello world')\n" * 8 + "```\n能跑"
    check("代码占比高不打回", not any(h.code == "length_gate" for h in R.scan(code, c).hits))

    # 意图赦免
    v = R.scan(BOT_LONG, cfg(length_gate=True, length_gate_max=40, length_gate_hard=True))
    check("前置：BOT_LONG 硬模式打回", v.blocked)
    ctx = amnesty_mod.AmnestyContext(request_text="帮我总结一下这几点")
    am = amnesty_mod.evaluate(BOT_LONG, v, ctx)
    check("要求总结 → 长度闸门被赦免", am.allowed and am.channel == "intent",
          "%s/%s" % (am.allowed, am.channel))
    ctx2 = amnesty_mod.AmnestyContext(request_text="今天打apex吗")
    am2 = amnesty_mod.evaluate(BOT_LONG, v, ctx2)
    check("普通闲聊 → 不赦免", not am2.allowed, str(am2.channel))

    # 玩梗/短答通道不应赦免长度
    ctx3 = amnesty_mod.AmnestyContext(request_text="笑死，这也太离谱了吧")
    am3 = amnesty_mod.evaluate(BOT_LONG, v, ctx3)
    check("玩梗语境 → 不赦免长度", not am3.allowed, str(am3.channel))


def test_reason() -> None:
    print("\n[C] 打回措辞（这是效果的关键，不是装饰）")
    v = R.scan(BOT_LONG, cfg(length_gate=True, length_gate_max=30))
    reason = v.reason_for(1)
    check("措辞含「太长」", "太长" in reason, reason)
    check("措辞含「一句」", "一句" in reason, reason)
    check("措辞含具体字数范围", "20 字" in reason, reason)
    check("措辞含「不要补充第二句」", "第二句" in reason, reason)
    # ★ v2 新增：必须显式保护人设，否则重生成会变得一本正经
    check("措辞含颜文字保护", "颜文字" in reason, reason)
    check("措辞含语气词保护", "语气词" in reason, reason)
    check("措辞含 ww 保护", "ww" in reason, reason)
    check("措辞明确说「不要变得正经」", "正经" in reason, reason)
    check("措辞说明人设不是多余内容", "不是多余内容" in reason, reason)

    # keep_persona=False 时应去掉人设段（否则这个配置项没意义）
    v_off = R.scan(BOT_LONG, cfg(length_gate=True, length_gate_max=30,
                                 length_gate_keep_persona=False))
    r_off = v_off.reason_for(1)
    check("keep_persona=False 去掉人设段", "颜文字" not in r_off, r_off)
    check("keep_persona=False 仍说别补第二句", "第二句" in r_off, r_off)

    # 非长度命中仍走原有措辞
    v2 = R.scan("这不是技术问题，而是心态问题。", cfg())
    check("非长度命中不串味", "第二句" not in v2.reason_for(1), v2.reason_for(1))


def test_end_to_end() -> None:
    print("\n[D] 端到端：模拟两次 hook，看最后发出去的是哪条")
    c = cfg(length_gate=True, length_gate_max=40, length_gate_hard=True)

    first = BOT_LONG
    v1 = R.scan(first, c)
    check("第一次生成命中", v1.blocked)
    kw = {"response": first, "retry_count": 0, "session_id": "s", "reply_message_id": "m"}
    # 第一次：置 retry + reason
    kw["retry"] = True
    kw["retry_reason"] = v1.reason_for(1)
    check("置了 retry=True", kw["retry"] is True)
    check("retry_reason 是长度专用措辞", "第二句" in kw["retry_reason"])

    # 第二次：模型按 reason 重生成（用实测 B2 分支的真实样本）
    second = "主人给个时间点嘛(´ω`)"
    v2 = R.scan(second, c)
    check("第二次生成干净", not v2.blocked, "score=%.1f" % v2.score)
    check("第二次长度合理", len(second) <= 40, "%d 字" % len(second))


def test_no_regression_on_persona() -> None:
    print("\n[E] 人设保护：长度闸门不碰文本，只打回")
    c = cfg(length_gate=True, length_gate_max=20)
    before = "本鲸觉得这个不行喵！"
    v = R.scan(before, c)
    after = R.scrub(before, c)
    check("scrub 不会缩短文本", len(after) >= len(before) - 2, "%r → %r" % (before, after))
    check("scrub 不改「本鲸」", "本鲸" in after, after)
    check("scrub 不改颜文字", R.scan(before, cfg()).score ==
          R.scan(after, cfg()).score or "喵" in after, after)


def main() -> int:
    print("=" * 70)
    print("长度闸门专项测试")
    print("=" * 70)
    test_gate_switch()
    test_sentence_count()
    test_exemptions()
    test_reason()
    test_end_to_end()
    test_no_regression_on_persona()
    print("\n" + "=" * 70)
    print("通过 %d ｜ 失败 %d" % (PASS, FAIL))
    if FAILS:
        print("失败项: %s" % FAILS)
    print("=" * 70)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
