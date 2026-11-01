# -*- coding: utf-8 -*-
"""效果演示：规则体检 + 生成循环回放 + 口癖自动生成。

直接跑：python demo.py
不需要 MaiBot，也不需要 maibot_sdk（plugin.py 有兼容层）。
"""
from __future__ import annotations

import asyncio
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from plugin import AntiBait, AntiBaitConfig  # noqa: E402
from rules import RuleConfig, scan, scrub, should_check  # noqa: E402
import rules as rules_mod  # noqa: E402
import tics as tics_mod  # noqa: E402
import monitor as monitor_mod  # noqa: E402


def w(s: str, width: int) -> str:
    """按显示宽度补空格（中文按 2 算）。"""
    disp = sum(2 if ord(c) > 0x2E80 else 1 for c in s)
    return s + " " * max(0, width - disp)


def hr(title: str = "") -> None:
    print("\n" + "─" * 78)
    if title:
        print(title)
        print("─" * 78)


# ═══════════════════════════ 1. 规则体检 ═══════════════════════════

CASES = [
    ("正常口语", "哈哈哈这啥啊，我也想知道"),
    ("正常口语", "行吧，那就这么定了"),
    ("颜文字单个", "（— w —）"),
    ("标点叠加", "哈哈哈——真的吗！！！（— w —）"),
    ("总结腔", "总的来说，这个问题值得好好想想。"),
    ("对仗腔", "这不是技术问题，而是心态问题。"),
    ("结尾腔", "希望对你有所帮助！"),
    ("自曝身份", "作为一个AI，我无法真正理解你的感受。"),
    ("三段式", "首先看成本，其次看收益，最后看风险。"),
    ("emoji 堆砌", "好耶😀😁😂🤣😊😍"),
    ("引用里的八股", "> 总的来说，这是对的\n嗯确实"),
    ("代码块里的八股", "```\n总的来说\n```\n这个函数没问题"),
    ("排比+连接词", "要快，要稳，要省，而且还得好看，同时不能太贵，另外也要能扩展"),
    ("结构残留", '<message msg_id="2019424094" user="火鸡味锅巴">\n[图片：一只猫]'),
    ("工具残留", '[glass-meme-lookup] "嘎啦嘛"\n\n[未匹配到词条，结合上下文谨慎回复]'),
    ("正常方括号", "[doge] 这个不算，[1] 引用也不算"),
    ("颜文字+装饰符", "嘿嘿，一段单口连工牌都给人发好了(｡•̀ᴗ-)✧"),
    ("普通三句（不算排比）", "之前开过也出过OST，但这期具体哪天开，我没翻到官方公告"),
    ("长榜单（不判结构）", "来了来了，榜单这就端上来(￣▽￣)\n\n本周群聊 Top10：\n\n" + "\n".join(
        "%d. 「标题%d」\n这是一条讲述某件群聊事件的正文，讲得还挺细。" % (i, i) for i in range(1, 9))),
    ("正常尖括号", "这个 3<5 是真的"),
]


def demo_rules() -> None:
    hr("① 规则体检（score ≥ 3.0 或含硬规则 → 打回）")
    print(w("场景", 16) + w("分数", 8) + w("判定", 8) + "命中 / 原文")
    print("-" * 78)
    for tag, text in CASES:
        v = scan(text, RuleConfig())
        verdict = "打回" if v.blocked else "放行"
        shown = text.replace("\n", "\\n")
        print(w(tag, 16) + w("%.1f" % v.score, 8) + w(verdict, 8) + (v.summary() or "—") + "  |  " + shown[:30])

    hr("② 机械净化（零成本，先试着改好）")
    for text in ["总的来说，这个挺好的。", "总而言之，我同意。", "综上所述：就这样吧"]:
        print("  原文：%s" % text)
        print("  净化：%s" % scrub(text, RuleConfig()))
        print()


# ═══════════════════════════ 2. 生成循环回放 ═══════════════════════════

async def simulate(plugin: AntiBait, candidates, session="s1", mid="m1"):
    """模拟 maisaka_generator_base.py 的生成循环。"""
    retry_count = 0
    trace = []
    final = ""
    for i, cand in enumerate(candidates):
        kw = {
            "response": cand,
            "output_items": [{"type": "text", "data": cand}],
            "item_schema_version": 1,
            "session_id": session,
            "request_type": "chat",
            "task_name": "replyer",
            "requested_model_name": "test",
            "attempt": i + 1,
            "retry_count": retry_count,
            "max_retries": 3,
            "reply_message_id": mid,
            "selected_expression_ids": [],
            "reply_tool_args": {},
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        res = await plugin.bait_guard(**kw)
        mk = res.get("modified_kwargs") or {}
        final = mk.get("response", cand)
        retry = bool(mk.get("retry"))
        trace.append({
            "attempt": i + 1,
            "raw": cand,
            "final": final,
            "retry": retry,
            "reason": mk.get("retry_reason", ""),
        })
        if retry and retry_count < 3:
            retry_count += 1
            continue
        break
    return final, trace


SCENARIOS = [
    (
        "A 第二次变干净",
        [
            "要快，要稳，要省，而且还得好看，同时不能太贵，另外也要能扩展呢😊",
            "这玩意儿看场景吧，我觉得没啥大问题",
        ],
    ),
    (
        "B 第二次更脏 → 回滚",
        [
            "要快，要稳，要省，而且还得好看，同时不能太贵，另外也要能扩展呢😊",
            "要快，要稳，要省，而且还得好看，同时不能太贵，另外也要能扩展呢😊——真的",
        ],
    ),
    (
        "C 第一次有硬伤 → 不回滚",
        [
            "这不是技术问题，而是心态问题。",
            "要快，要稳，要省，而且还得好看，同时不能太贵，另外也要能扩展呢😊——真的",
        ],
    ),
    (
        "D 一次就干净",
        ["哈哈哈这啥啊，我也想知道"],
    ),
    (
        "E 机械净化救回（不重试）",
        ["总的来说，这个挺好的。"],
    ),
]


async def demo_loop() -> None:
    hr("③ 生成循环回放（重试只一次 + 两次择优）")
    plugin = AntiBait()
    plugin.config.dry_run = False
    plugin.config.pick_better = True

    for i, (name, cands) in enumerate(SCENARIOS):
        final, trace = await simulate(plugin, cands, mid="mid-%d" % i)
        print("\n【%s】" % name)
        for step in trace:
            print("  第%d次  原文：%s" % (step["attempt"], step["raw"]))
            if step["retry"]:
                print("          → 打回，理由：%s" % step["reason"][:70])
            elif step["final"] != step["raw"]:
                print("          → 改写为：%s" % step["final"])
        print("  ✅ 最终发出：%s" % final)

    print("\n统计：%s" % json.dumps(plugin.stats(), ensure_ascii=False))


# ═══════════════════════════ 3. 口癖自动生成 ═══════════════════════════

def build_samples() -> list:
    out = ["这是第%d条普通发言" % i for i in range(53)]
    out += ["今天好累喵~"] * 10                       # 8.3%
    out += ["其实，我觉得还行"] * 6                   # 5.0%
    out += ["这个我也不知道呢"] * 50                  # 41.7%（太高）
    out += ["（歪头）你说呢"]                          # 0.8%（太低）
    return out


STUB_LLM_OUTPUT = """[
  {"pattern": "喵[~～]?$", "note": "结尾带喵", "severity": "soft"},
  {"pattern": "其实[，,]", "note": "开头其实", "severity": "soft"},
  {"pattern": ".*呢.*", "note": "太宽了", "severity": "soft"},
  {"pattern": "(a+)+b", "note": "灾难回溯", "severity": "hard"},
  {"pattern": "（[^）]{0,6}歪头[^）]{0,6}）", "note": "动作描写", "severity": "hard"},
  {"pattern": "呢$", "note": "人人都说", "severity": "soft"}
]"""


def demo_tics() -> None:
    hr("④ 口癖自动生成（草稿 + 校验）")
    samples = build_samples()
    print("样本 %d 条，调模型（这里用固定输出模拟）...\n" % len(samples))

    result = tics_mod.suggest_from_raw(STUB_LLM_OUTPUT, samples)

    print("✅ 通过校验（进入草稿，需人工确认）：")
    if not result["accepted"]:
        print("   （无）")
    for item in result["accepted"]:
        print("   %s" % w(item["pattern"], 26) + "命中率 %5.1f%%  %s" % (item["hit_rate"] * 100, item["note"]))
        for ex in item["examples"]:
            print("      示例：%s" % ex)

    print("\n❌ 被拒：")
    for item in result["rejected"]:
        print("   %s" % w(item["pattern"] or "(空)", 26) + item["reason"])


# ═══════════════════════════ 4. 试运行监控 ═══════════════════════════

async def demo_monitor() -> None:
    hr("⑤ 试运行监控（dry_run：只记录、不拦截）")
    import os
    import shutil

    demo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo-data")
    shutil.rmtree(demo_dir, ignore_errors=True)
    os.makedirs(demo_dir, exist_ok=True)

    plugin = AntiBait()
    plugin.config.dry_run = True
    plugin.config.monitor_enabled = True
    plugin.config.monitor_record_all = True
    plugin._data_dir = lambda: demo_dir  # 演示时写到这里，别碰真实 data_dir

    path = os.path.join(demo_dir, "monitor.jsonl")
    print("dry_run=True：命中也不打回，只往 %s 写一行\n" % path)

    for i, (_name, cands) in enumerate(SCENARIOS):
        await simulate(plugin, cands[:1], mid="mon-%d" % i)

    with open(path, "r", encoding="utf-8") as f:
        lines = [ln for ln in f if ln.strip()]
    print("写入 %d 行，前 2 行原文：" % len(lines))
    for ln in lines[:2]:
        s = ln.strip()
        print("  " + s[:150] + ("..." if len(s) > 150 else ""))

    rep = monitor_mod.build_report([json.loads(x) for x in lines])
    print("\n" + monitor_mod.format_report(rep))
    print("\n结论：dry_run 下 retried=%d（一条都没打回），记录可直接用来算误杀率。"
          % plugin.stats()["retried"])


# ═══════════════════════════ 5. 赦免层 ═══════════════════════════

async def demo_amnesty() -> None:
    hr("⑥ 赦免层（打回之前先看语境：是不是玩梗/复读/回答问题）")
    import os
    import shutil

    import amnesty as am_mod
    from rules import scan

    ctx_none = am_mod.AmnestyContext()
    cases = [
        ("卧槽笑死，这也太离谱了吧😀😁😂🤣😊", ctx_none,
         "纯玩梗，没上下文也能认出来"),
        ("今天这活动真不错啊，总的来说挺好的。",
         am_mod.AmnestyContext(others=["今天这活动真不错啊"]), "在复读别人的话"),
        ("总的来说，这事儿分三步。",
         am_mod.AmnestyContext(request_text="帮我总结一下这几点"), "对方要求总结"),
        ("行呢。好呀。😀——（— w —）",
         am_mod.AmnestyContext(request_text="这个能用吗？"), "在回答问句，短"),
        ("这不是技术问题，而是心态问题。", ctx_none, "对仗腔，没人要求，也不像玩梗"),
        ("作为一个AI，我总结一下。",
         am_mod.AmnestyContext(request_text="帮我总结一下"), "自曝AI，任何语境都不赦"),
        ("卧槽，总的来说这事挺好😀😁😂🤣😊", ctx_none, "玩梗也盖不住总结腔"),
    ]

    print(w("回复", 40) + w("命中", 26) + "结果")
    print("-" * 78)
    for text, ctx, why in cases:
        v = scan(text)
        a = am_mod.evaluate(text, v, ctx)
        codes = ",".join(h.code for h in v.hits)[:24]
        if a.allowed:
            res = "✅ 赦免(%s)" % a.channel
        else:
            res = "→ 照常处理"
        print(w(text, 40) + w(codes or "-", 26) + res)
        print(w("", 2) + "└ " + why + ("｜" + a.reason if a.reason else ""))

    print("\n规则：不是打分凑够就赦，而是**这条回复的所有命中码都要被这个语境解释掉**。")
    print("所以「玩梗」能赦表情堆砌，但赦不了总结腔；「对方要求总结」能赦总结腔，但赦不了自曝AI。")

    # ── 接进插件：dry_run 下赦免，连重试都不发起 ──
    demo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo-data")
    shutil.rmtree(demo_dir, ignore_errors=True)
    os.makedirs(demo_dir, exist_ok=True)
    plugin = AntiBait()
    plugin.config.dry_run = True
    plugin.config.monitor_record_all = True
    plugin._data_dir = lambda: demo_dir

    kw = {
        "response": "卧槽笑死，这也太离谱了吧😀😁😂🤣😊",
        "output_items": [], "item_schema_version": 1, "session_id": "s1",
        "request_type": "chat", "task_name": "replyer", "attempt": 1,
        "retry_count": 0, "max_retries": 3, "reply_message_id": "amn-1",
        "selected_expression_ids": [], "reply_tool_args": {},
    }
    res = await plugin.bait_guard(**kw)
    print("\n接进插件（dry_run）：retry=%s，stats=%s"
          % (bool(res["modified_kwargs"].get("retry")), plugin.stats()))
    recs = plugin._monitor_obj().read()
    if recs:
        print("监控记录：decision=%s channel=%s why=%s"
              % (recs[0].get("decision"), recs[0].get("channel"), recs[0].get("why")))
    print("结论：赦免掉的不进 would_retry，误杀率统计不会被玩梗刷高。")
    shutil.rmtree(demo_dir, ignore_errors=True)


async def demo_fucong() -> None:
    print("\n" + "-" * 78)
    print("⑦ 附和服软腔 + 序列特征")
    print("-" * 78)
    print("单条「行吧行吧」「我菜我认」人也会说，所以每条只记 1 分；")
    print("连着三条都在服软、三条都用同一个颜文字，才是机器味。\n")

    rows = [
        "哼 那你说说该怎么打嘛，别光笑我(｡•́︿•̀｡)",
        "行吧行吧，参数党赢了，我闭麦看你表演(｡•́︿•̀｡)",
        "行行行我菜我认，等你调完参数科普嗯嗯(｡•́︿•̀｡)",
        "这个本子我买了，挺好用的",
    ]
    print("%-46s %6s %8s %s" % ("回复", "分数", "打回", "命中"))
    print("-" * 78)
    hist = []
    for t in rows:
        v = rules_mod.apply_sequence(rules_mod.scan(t), t, hist)
        hist.insert(0, t)
        del hist[5:]
        print("%-46s %6.1f %8s %s"
              % (t[:44], v.score, "是" if v.blocked else "否", v.summary()[:26]))
    print("\n单独看每条：0.3 / 2.3 / 3.3 / 0.0 —— 只有第三条单看就已过线；")
    print("第 2 条是被「上一句也是同款颜文字」从 2.3 推到 3.3 才打回的。")
    print("把 sequence_enabled 关掉，第 2 条就回到 2.3 放行 —— 序列特征可以单独关。")


async def main() -> None:
    print("=" * 78)
    print("maibot-antibait 测试版演示")
    print("=" * 78)
    demo_rules()
    await demo_loop()
    demo_tics()
    await demo_monitor()
    await demo_amnesty()
    await demo_fucong()
    print("\n" + "=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
