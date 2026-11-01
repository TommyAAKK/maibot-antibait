# -*- coding: utf-8 -*-
"""回归测试：python tests.py

覆盖三件事：
1. 遮蔽管线确实能阻止「颜文字里的破折号被重复计分」
2. 重试只一次 + 两次择优（含硬伤不回滚）
3. 口癖候选的校验能挡住灾难正则和过宽/过窄的模式
"""
from __future__ import annotations

import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from plugin import AntiBait
from rules import RuleConfig, scan, scrub, mask, span_counts, should_check
import tics as tics_mod

PASS = 0
FAIL = 0
FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % name)
    else:
        FAIL += 1
        FAILED.append(name)
        print("  ✗ %s   %s" % (name, detail))


# ═════════════════════════ 1. 遮蔽与计分 ═════════════════════════

def test_masking() -> None:
    print("\n[1] 遮蔽与计分")

    m = mask("（— w —）")
    c = span_counts(m.spans)
    check("颜文字被整体遮蔽", c["kaomoji"] == 1, str(c))
    check("遮蔽后残渣里没有破折号", "—" not in m.text, m.text)

    v = scan("（— w —）")
    check("（— w —）只算颜文字 0.3 分（默认降权）",
          abs(v.score - 0.3) < 1e-6, "score=%.2f %s" % (v.score, v.summary()))
    v_old = scan("（— w —）", RuleConfig(kaomoji_weight=1.0))
    check("kaomoji_weight=1.0 可恢复旧计分",
          abs(v_old.score - 1.0) < 1e-6, "score=%.2f" % v_old.score)

    v2 = scan("哈哈哈——真的吗！！！（— w —）")
    check("标点叠加封顶 2.0（0.5+0.5+0.3=1.3）", abs(v2.score - 1.3) < 1e-6,
          "score=%.2f %s" % (v2.score, v2.summary()))
    check("标点叠加不放行到打回", not v2.blocked)

    # 汉字括号不是颜文字
    m2 = mask("（小声）")
    check("（小声）不被当成颜文字", span_counts(m2.spans)["kaomoji"] == 0)

    # 引用与代码块里的八股不判
    check("引用块里的八股不算", scan("> 总的来说，这是对的\n嗯确实").hard == 0)
    check("代码块里的八股不算", scan("```\n总的来说\n```\n这个没问题").hard == 0)

    # 硬规则
    for text in ["总的来说，这个问题值得想想。", "这不是技术问题，而是心态问题。",
                 "希望对你有所帮助！", "作为一个AI，我无法理解。"]:
        check("硬规则命中：%s" % text[:12], scan(text).hard >= 1, text)

    check("正常口语不误伤", scan("哈哈哈这啥啊，我也想知道").score == 0.0)

    # 净化
    check("净化去掉总结腔前缀", scrub("总的来说，这个挺好的。") == "这个挺好的。",
          scrub("总的来说，这个挺好的。"))
    check("净化不动颜文字内部", scrub("（！！！）好") == "（！！！）好", scrub("（！！！）好"))

    # 短文本豁免
    check("短文本不检查", not should_check("好"))
    check("空文本不检查", not should_check("   "))


# ═════════════════════════ 2. 重试与择优 ═════════════════════════

async def simulate(plugin, candidates, mid):
    retry_count = 0
    final = ""
    trace = []
    for i, cand in enumerate(candidates):
        kw = {
            "response": cand, "output_items": [{"type": "text", "data": cand}],
            "item_schema_version": 1, "session_id": "s", "request_type": "chat",
            "task_name": "replyer", "requested_model_name": "t",
            "attempt": i + 1, "retry_count": retry_count, "max_retries": 3,
            "reply_message_id": mid, "selected_expression_ids": [], "reply_tool_args": {},
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        }
        res = await plugin.bait_guard(**kw)
        mk = res.get("modified_kwargs") or {}
        final = mk.get("response", cand)
        trace.append({"retry": bool(mk.get("retry")), "final": final,
                      "reason": mk.get("retry_reason", "")})
        if mk.get("retry") and retry_count < 3:
            retry_count += 1
            continue
        break
    return final, trace


DIRTY = "要快，要稳，要省，而且还得好看，同时不能太贵，另外也要能扩展呢😊"
DIRTIER = DIRTY + "——真的"


async def test_loop() -> None:
    print("\n[2] 重试与择优")
    p = AntiBait()
    p.config.dry_run = False

    # A 第二次干净
    final, tr = await simulate(p, [DIRTY, "这玩意儿看场景吧，我觉得没啥大问题"], "a")
    check("A 只重试一次", len(tr) == 2 and tr[0]["retry"] and not tr[1]["retry"], str(tr))
    check("A 采用第二次", final == "这玩意儿看场景吧，我觉得没啥大问题", final)

    # B 第二次更脏 → 回滚
    final, tr = await simulate(p, [DIRTY, DIRTIER], "b")
    check("B 回滚到第一次", final == DIRTY, final)

    # C 第一次有硬伤（净化救不回的那种）→ 不回滚
    hard_first = "这不是技术问题，而是心态问题。"
    final, tr = await simulate(p, [hard_first, DIRTIER], "c")
    check("C 硬伤不回滚，保留第二次", final == DIRTIER, final)

    # D 一次干净
    final, tr = await simulate(p, ["哈哈哈这啥啊，我也想知道"], "d")
    check("D 不重试", len(tr) == 1 and not tr[0]["retry"], str(tr))

    # E 净化救回
    final, tr = await simulate(p, ["总的来说，这个挺好的。"], "e")
    check("E 净化后直接通过", final == "这个挺好的。" and len(tr) == 1, "%s | %s" % (final, tr))

    # F dry_run 不拦
    p2 = AntiBait()
    p2.config.dry_run = True
    final, tr = await simulate(p2, [DIRTY, "干净的话"], "f")
    check("F dry_run 只记录不拦", len(tr) == 1 and final == DIRTY, final)

    # G 没有旧记录时（插件重启/热重载）第二次直接放行，不回滚
    p3 = AntiBait()
    p3.config.dry_run = False
    res = await p3.bait_guard(**{
        "response": DIRTIER, "session_id": "s", "request_type": "chat",
        "retry_count": 1, "reply_message_id": "g-lost",
    })
    check("G 无旧记录时不回滚", res["modified_kwargs"]["response"] == DIRTIER,
          res["modified_kwargs"]["response"])

    check("统计有重试记录", p.stats()["retried"] >= 3, str(p.stats()))


# ═════════════════════════ 3. 口癖校验 ═════════════════════════

def build_samples():
    out = ["这是第%d条普通发言" % i for i in range(53)]
    out += ["今天好累喵~"] * 10
    out += ["其实，我觉得还行"] * 6
    out += ["这个我也不知道呢"] * 50
    out += ["（歪头）你说呢"]
    return out


def test_tics() -> None:
    print("\n[3] 口癖候选校验")
    samples = build_samples()

    ok, reason, rate = tics_mod.validate_candidate("喵[~～]?$", samples)
    check("合理口癖通过", ok, reason)

    ok, reason, _ = tics_mod.validate_candidate(".*呢.*", samples)
    check("首尾 .* 被拒", not ok, reason)

    ok, reason, _ = tics_mod.validate_candidate("(a+)+b", samples)
    check("嵌套量词被拒", not ok, reason)

    ok, reason, rate = tics_mod.validate_candidate("呢$", samples)
    check("命中率过高被拒", not ok and "太高" in reason, reason)

    ok, reason, rate = tics_mod.validate_candidate("（[^）]{0,6}歪头[^）]{0,6}）", samples)
    check("命中率过低被拒", not ok and "太低" in reason, reason)

    raw = """[
      {"pattern": "喵[~～]?$", "note": "结尾带喵", "severity": "soft"},
      {"pattern": "其实[，,]", "note": "开头其实", "severity": "soft"},
      {"pattern": ".*呢.*", "note": "太宽", "severity": "soft"},
      {"pattern": "呢$", "note": "人人说", "severity": "soft"}
    ]"""
    res = tics_mod.suggest_from_raw(raw, samples)
    check("只保留 2 条", len(res["accepted"]) == 2, str([a["pattern"] for a in res["accepted"]]))
    check("拒绝 2 条", len(res["rejected"]) == 2, str(res["rejected"]))
    check("草稿带示例", all(a["examples"] for a in res["accepted"]))

    bad = tics_mod.suggest_from_raw("这不是 JSON", samples)
    check("非法 JSON 有兜底", bad["accepted"] == [] and bad["rejected"], str(bad))

    # 原子写
    import os
    import tempfile
    d = tempfile.mkdtemp()
    path = os.path.join(d, "tics.json")
    tics_mod.atomic_write_json(path, [{"pattern": "喵"}])
    check("原子写可回读", tics_mod.load_json(path, None) == [{"pattern": "喵"}])


# ═════════════════════════ 4. SDK 路径 ═════════════════════════

def test_sdk_path() -> None:
    """装上 maibot_sdk 后插件能不能正常注册组件（用假 SDK 模拟）。"""
    print("\n[4] SDK 装载路径")
    import importlib
    import types

    calls = {"hooks": [], "tools": []}

    class _Base:
        pass

    class _Plugin:
        def __init__(self, *a, **k):
            self.ctx = None

    def _field(default=None, default_factory=None, **_kw):
        return default_factory() if default_factory else default

    def _hook(*a, **kw):
        def deco(fn):
            calls["hooks"].append((a, kw, fn.__name__))
            return fn
        return deco

    def _tool(*a, **kw):
        def deco(fn):
            calls["tools"].append((a, kw, fn.__name__))
            return fn
        return deco

    fake = types.ModuleType("maibot_sdk")
    fake.MaiBotPlugin = _Plugin
    fake.HookHandler = _hook
    fake.Tool = _tool
    fake.Command = _tool
    fake.PluginConfigBase = _Base
    fake.Field = _field

    sys.modules["maibot_sdk"] = fake
    sys.modules.pop("plugin", None)
    try:
        mod = importlib.import_module("plugin")
        p = mod.create_plugin()
        check("SDK 路径可导入", mod.SDK_AVAILABLE is True)
        check("Hook 注册在 after_response",
              any("maisaka.replyer.after_response" in c[0] for c in calls["hooks"]),
              str(calls["hooks"]))
        check("Hook 带 name/timeout", any(c[1].get("name") == "bait_guard" and c[1].get("timeout_ms")
                                          for c in calls["hooks"]))
        check("Tool 已注册", any(c[2] == "tics_suggest" for c in calls["tools"]), str(calls["tools"]))
        check("配置默认值可用", p.config.dry_run is True and p.config.max_retries == 1)
    finally:
        sys.modules.pop("maibot_sdk", None)
        sys.modules.pop("plugin", None)
        importlib.import_module("plugin")  # 恢复无 SDK 状态


def _kw(text: str, retry_count: int = 0, mid: str = "m1") -> dict:
    return {
        "response": text,
        "output_items": [{"type": "text", "data": text}],
        "item_schema_version": 1,
        "session_id": "s1",
        "request_type": "chat",
        "task_name": "replyer",
        "attempt": retry_count + 1,
        "retry_count": retry_count,
        "max_retries": 3,
        "reply_message_id": mid,
        "selected_expression_ids": [],
        "reply_tool_args": {},
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


async def test_monitor() -> None:
    print("\n[5] 试运行监控")
    import os as _os
    import shutil
    import tempfile

    import monitor as monitor_mod

    tmp = tempfile.mkdtemp(prefix="antibait-mon-")
    try:
        # ── 5.1 dry_run：写记录，但一条都不打回 ──
        p = AntiBait()
        p.config.dry_run = True
        p.config.monitor_enabled = True
        p.config.monitor_record_all = False
        p._data_dir = lambda: tmp

        res = await p.bait_guard(**_kw("这不是技术问题，而是心态问题。"))
        mk = res["modified_kwargs"]
        check("dry_run 不置 retry", not mk.get("retry"))
        check("dry_run 计数 would_retry=1 / retried=0",
              p.stats()["would_retry"] == 1 and p.stats()["retried"] == 0, str(p.stats()))

        recs = p._monitor_obj().read()
        check("dry_run 写了 1 行", len(recs) == 1, str(len(recs)))
        r0 = recs[0] if recs else {}
        check("记录 mode=dry_run", r0.get("mode") == "dry_run", str(r0.get("mode")))
        check("记录 decision=would_retry", r0.get("decision") == "would_retry", str(r0.get("decision")))
        check("记录含 rules/score/text",
              bool(r0.get("rules")) and float(r0.get("score") or 0) > 0 and bool(r0.get("text")))
        check("记录带 session/reply id",
              r0.get("session_id") == "s1" and r0.get("reply_message_id") == "m1")

        # ── 5.2 默认不记干净回复 ──
        p2 = AntiBait()
        p2.config.dry_run = True
        p2.config.monitor_record_all = False
        p2._data_dir = lambda: tmp
        await p2.bait_guard(**_kw("哈哈哈这啥啊，我也想知道", mid="m2"))
        check("干净回复默认不入库", len(p2._monitor_obj().read()) == 1,
              str(len(p2._monitor_obj().read())))

        # ── 5.3 monitor_record_all 连干净的一起记（算分母）──
        p3 = AntiBait()
        p3.config.dry_run = True
        p3.config.monitor_record_all = True
        p3._data_dir = lambda: tmp
        await p3.bait_guard(**_kw("哈哈哈这啥啊，我也想知道", mid="m3"))
        recs3 = p3._monitor_obj().read()
        check("record_all 记下干净回复", len(recs3) == 2, str(len(recs3)))
        check("干净回复 decision=pass",
              any(r.get("decision") == "pass" for r in recs3))

        # ── 5.4 live 模式：真打回，记录 decision=retry ──
        p4 = AntiBait()
        p4.config.dry_run = False
        p4._data_dir = lambda: tmp
        res4 = await p4.bait_guard(**_kw("这不是技术问题，而是心态问题。", mid="m4"))
        check("live 置 retry=True", bool(res4["modified_kwargs"].get("retry")))
        recs4 = [r for r in p4._monitor_obj().read() if r.get("reply_message_id") == "m4"]
        check("live 记录 decision=retry",
              bool(recs4) and recs4[0].get("decision") == "retry", str(recs4[:1]))

        # ── 5.5 第二次择优回滚也留痕 ──
        tmp2 = tempfile.mkdtemp(prefix="antibait-mon2-")
        p5 = AntiBait()
        p5.config.dry_run = False
        p5._data_dir = lambda: tmp2
        bad1 = "要快，要稳，要省，而且还得好看，同时不能太贵，另外也要能扩展呢😊"
        bad2 = bad1 + "——真的"
        await p5.bait_guard(**_kw(bad1, mid="m5"))
        res5 = await p5.bait_guard(**_kw(bad2, retry_count=1, mid="m5"))
        check("第二次更脏时回滚第一次",
              res5["modified_kwargs"].get("response") == bad1)
        recs5 = p5._monitor_obj().read()
        check("回滚留了 decision=rollback",
              any(r.get("decision") == "rollback" for r in recs5), str([r.get("decision") for r in recs5]))

        # ── 5.6 汇总报告 ──
        rep = monitor_mod.build_report(p5._monitor_obj().read(), top=3)
        check("报告 total/blocked 合理",
              rep["total"] == len(recs5) and rep["blocked"] >= 1, str(rep))
        check("报告有规则分布", bool(rep["top_rules"]), str(rep["top_rules"]))
        check("报告有样例", len(rep["samples"]) >= 1)
        check("报告文案可渲染", "监控汇总" in monitor_mod.format_report(rep))

        # ── 5.7 关闭监控就不写 ──
        tmp3 = tempfile.mkdtemp(prefix="antibait-mon3-")
        p6 = AntiBait()
        p6.config.monitor_enabled = False
        p6._data_dir = lambda: tmp3
        await p6.bait_guard(**_kw("这不是技术问题，而是心态问题。", mid="m6"))
        check("monitor_enabled=False 不落盘",
              not _os.path.exists(_os.path.join(tmp3, "monitor.jsonl")))

        # ── 5.8 超过上限自动轮转 ──
        tmp4 = tempfile.mkdtemp(prefix="antibait-mon4-")
        p7 = AntiBait()
        p7.config.dry_run = True
        p7.config.monitor_max_bytes = 150
        p7._data_dir = lambda: tmp4
        for i in range(3):
            await p7.bait_guard(**_kw("这不是技术问题，而是心态问题。", mid="r%d" % i))
        check("超限滚成 .1", _os.path.exists(_os.path.join(tmp4, "monitor.jsonl.1")))
        for d in (tmp2, tmp3, tmp4):
            shutil.rmtree(d, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_amnesty() -> None:
    print("\n[6] 赦免层")
    import shutil
    import tempfile

    import amnesty as am_mod
    from rules import scan as rscan

    def V(t):
        return rscan(t)

    # ── 6.1 一票否决 ──
    t = "作为一个AI，我觉得卧槽笑死😀😁😂🤣😊"
    a = am_mod.evaluate(t, V(t))
    check("自曝AI 一票否决", (not a.allowed) and ("zipu" in a.codes), str(a))

    # ── 6.2 复读：把别人的话接着说 ──
    src = "今天这活动真不错啊"
    rep = "今天这活动真不错啊，总的来说挺好的。"
    a = am_mod.evaluate(rep, V(rep), am_mod.AmnestyContext(others=[src]))
    check("复读可赦", a.allowed and a.channel == "echo", str(a))

    # ── 6.3 复读源本身是八股 → 不赦（防机器人复读自己）──
    bait = "这不是技术问题，而是心态问题。"
    a = am_mod.evaluate(bait, V(bait), am_mod.AmnestyContext(others=[bait]))
    check("复读八股不赦", not a.allowed, str(a))

    # ── 6.4 请求意图：对方明确要总结 ──
    t = "总的来说，这事儿分三步。"
    a = am_mod.evaluate(t, V(t), am_mod.AmnestyContext(request_text="帮我总结一下这几点"))
    check("按要求总结可赦", a.allowed and a.channel == "intent", str(a))

    # ── 6.5 意图覆盖不了自曝AI ──
    t = "作为一个AI，我总结一下。"
    a = am_mod.evaluate(t, V(t), am_mod.AmnestyContext(request_text="帮我总结一下"))
    check("意图不赦自曝AI", not a.allowed, str(a))

    # ── 6.6 玩梗：梗词 + 表情风暴 ──
    t = "卧槽笑死😀😁😂🤣😊"
    a = am_mod.evaluate(t, V(t))
    check("玩梗可赦", a.allowed and a.channel == "meme", str(a))

    # ── 6.7 玩梗覆盖不了总结腔 ──
    t = "卧槽，总的来说这事挺好😀😁😂🤣😊"
    a = am_mod.evaluate(t, V(t))
    check("玩梗不赦总结腔", not a.allowed, str(a))

    # ── 6.8 回答问题：问句 + 短答 ──
    t = "行呢。好呀。😀——（— w —）"
    a = am_mod.evaluate(t, V(t), am_mod.AmnestyContext(request_text="这个能用吗？"))
    check("回答问句可赦", a.allowed and a.channel == "answer", str(a))

    # ── 6.9 长答不算随口答 ──
    t = "行呢。好呀。😀——（— w —）" + "这个我觉得还是要看具体场景再决定吧。" * 3
    a = am_mod.evaluate(t, V(t), am_mod.AmnestyContext(request_text="这个能用吗？"))
    check("长答不赦", (not a.allowed) and len(t) > 60, str(a))

    # ── 6.10 没上下文就不乱赦 ──
    t = "这不是技术问题，而是心态问题。"
    a = am_mod.evaluate(t, V(t))
    check("无上下文不赦", not a.allowed, str(a))

    # ── 6.11 关闭赦免 ──
    a = am_mod.evaluate("卧槽笑死😀😁😂🤣😊", V("卧槽笑死😀😁😂🤣😊"),
                        cfg=am_mod.AmnestyConfig(enabled=False))
    check("关闭后不赦", not a.allowed, str(a))

    # ── 6.12 模型复核解析 ──
    ok = am_mod.parse_llm_verdict('{"amnesty": true, "reason": "在玩梗"}')
    check("解析 amnesty=true", (ok or {}).get("allowed") is True, str(ok))
    check("解析垃圾输出不放行", am_mod.parse_llm_verdict("我看不懂") is None)
    check("提示词带上下文",
          "最近别人说的话" in am_mod.build_llm_prompt("x", am_mod.AmnestyContext(others=["a"])))

    # ── 6.13 接进 _guard ──
    tmp = tempfile.mkdtemp(prefix="antibait-amn-")
    try:
        p = AntiBait()
        p.config.dry_run = True
        p._data_dir = lambda: tmp
        res = await p.bait_guard(**_kw("卧槽笑死，这也太离谱了吧😀😁😂🤣😊", mid="a1"))
        check("赦免后不置 retry", not res["modified_kwargs"].get("retry"))
        check("赦免计数 +1 且不算 would_retry",
              p.stats()["amnestied"] == 1 and p.stats()["would_retry"] == 0, str(p.stats()))
        recs = p._monitor_obj().read()
        check("记录 decision=amnesty",
              bool(recs) and recs[0].get("decision") == "amnesty", str(recs[:1]))
        check("记录带 channel=meme",
              bool(recs) and recs[0].get("channel") == "meme", str(recs[:1]))

        # ── 6.14 关掉赦免后恢复打回 ──
        p2 = AntiBait()
        p2.config.dry_run = True
        p2.config.amnesty_enabled = False
        p2._data_dir = lambda: tmp
        await p2.bait_guard(**_kw("卧槽笑死，这也太离谱了吧😀😁😂🤣😊", mid="a2"))
        check("关掉赦免恢复 would_retry", p2.stats()["would_retry"] == 1, str(p2.stats()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_label_leak() -> None:
    print("\n[7] 结构残留与颜文字降权")

    leak = ('<message msg_id="2019424094" quote="" time="22:50:44" user="火鸡味锅巴">\n'
            '[图片：这是游戏的全新怪物展示图]')
    v = scan(leak)
    check("原始 message 标签被判硬伤", v.blocked and v.hard >= 1, v.summary())
    check("命中码是 label_leak",
          any(h.code == "label_leak" for h in v.hits), v.summary())

    check("单独 msg_id= 也拦", scan("看这个 msg_id=123 的东西").hard >= 1)
    check("[图片： 也拦", scan("这啥呀[图片：一只猫]").hard >= 1)
    check("</message> 也拦", scan("嗯嗯</message>").hard >= 1)

    # 不该误伤
    check("正常尖括号不误伤", scan("这个 3<5 是真的").hard == 0)
    check("代码块里的标签不误伤",
          scan("```html\n<div class=\"x\">hi</div>\n```\n这个没问题").hard == 0)
    check("中文书名号不误伤", scan("《message》这书我没看过").hard == 0)

    # 颜文字降权：单条不再垫 1 分
    v1 = scan("在呢在呢，刚回来就听到主人喊我～这几天有没有想小鲸鱼呀(｡•́︿•̀｡)")
    check("颜文字单条只计 0.3", abs(v1.score - 0.3) < 1e-6, "score=%.2f %s" % (v1.score, v1.summary()))
    check("颜文字单条不触发打回", not v1.blocked)

    # 颜文字 + 两条软规则仍能到阈值（0.3+1+1=2.3 < 3.0，需三条）
    v2 = scan("本质上说，这不是技术问题，而是心态问题(￣▽￣)")
    check("颜文字降权后硬规则仍独立生效", v2.blocked and v2.hard >= 1, v2.summary())

    # 赦免不能赦结构残留
    import amnesty as am_mod
    a = am_mod.evaluate(leak, v, None)
    check("结构残留不可赦免", not a.allowed, str(a))


def test_fucong() -> None:
    print("\n[8] 附和服软腔 + 序列特征")
    from rules import apply_sequence

    # 单条：每项只 1 分，单独不会打回
    v = scan("行吧行吧，参数党赢了")
    codes = {h.code for h in v.hits}
    check("叠词服软命中", "diezi" in codes, str(codes))
    check("夸对方赢命中", "kuata" in codes, str(codes))
    check("两条叠加只有 2 分", abs(v.score - 2.0) < 1e-6, "score=%.2f" % v.score)
    check("单条 2 分不打回", not v.blocked)

    v = scan("行行行我菜我认")
    codes = {h.code for h in v.hits}
    check("认输自贬命中", "zibian" in codes, str(codes))

    # 句尾附和：必须挂在句末
    check("句尾嗯嗯命中", "fuhe" in {h.code for h in scan("等你调完参数科普嗯嗯").hits})
    check("中间嗯嗯不算", "fuhe" not in {h.code for h in scan("嗯嗯我知道了这个").hits})
    # 颜文字在尾部时也要能判到句末（占位符要先摘掉）
    check("颜文字结尾仍能判句尾附和",
          "fuhe" in {h.code for h in scan("等你调完参数科普嗯嗯(｡•́︿•̀｡)").hits})

    # 不该误伤：正常短句
    check("正常短句不误伤", scan("这个本子我买了，挺好用的").score == 0.0)
    check("单个嗯不误伤", "fuhe" not in {h.code for h in scan("嗯，知道了").hits})

    # 序列：同款颜文字连发
    cur = "行吧行吧，参数党赢了(｡•́︿•̀｡)"
    prev = ["我闭麦看你表演(｡•́︿•̀｡)"]
    v = apply_sequence(scan(cur), cur, prev)
    check("同款颜文字连发命中", "kao_repeat" in {h.code for h in v.hits}, v.summary())
    check("连发后越过打回线", v.blocked, "score=%.2f" % v.score)

    # 换一个颜文字就不算连发
    cur2 = "行吧行吧，参数党赢了(￣▽￣)"
    v2 = apply_sequence(scan(cur2), cur2, prev)
    check("换颜文字不算连发", "kao_repeat" not in {h.code for h in v2.hits})

    # 序列：连续附和服软
    prev2 = ["行吧行吧，参数党赢了"]
    cur3 = "行行行我菜我认"
    v3 = apply_sequence(scan(cur3), cur3, prev2)
    check("连续服软命中", "fucong_repeat" in {h.code for h in v3.hits}, v3.summary())

    # 没有历史时不叠加
    v4 = apply_sequence(scan(cur3), cur3, [])
    check("无历史不叠加", "fucong_repeat" not in {h.code for h in v4.hits})
    check("无历史分数不变", abs(v4.score - scan(cur3).score) < 1e-6)

    # 关掉这一档就完全不计分
    cfg_off = RuleConfig(fucong=False)
    check("fucong=False 时不命中", scan("行吧行吧，参数党赢了", cfg_off).score == 0.0)

    # 三条真实语料：正是用户报的那几句
    real = [
        ("哼 那你说说该怎么打嘛，别光笑我(｡•́︿•̀｡)", False),
        ("行吧行吧，参数党赢了，我闭麦看你表演(｡•́︿•̀｡)", True),
        ("行行行我菜我认，等你调完参数科普嗯嗯(｡•́︿•̀｡)", True),
    ]
    hist = []
    for text, want in real:
        vv = apply_sequence(scan(text), text, hist)
        hist.insert(0, text)
        check("真实语料「%s…」%s" % (text[:8], "打回" if want else "放行"),
              vv.blocked == want, "score=%.1f %s" % (vv.score, vv.summary()))


async def test_sequence_plugin() -> None:
    print("\n[9] 序列特征接进插件")
    import shutil
    import tempfile

    tmp = tempfile.mkdtemp(prefix="antibait-seq-")
    try:
        p = AntiBait()
        p.config.dry_run = True
        p.config.monitor_enabled = False
        p._data_dir = lambda: tmp

        # 第一条：只有颜文字，放行，并进历史
        await p.bait_guard(**_kw("我闭麦看你表演(｡•́︿•̀｡)", mid="s1"))
        check("第一条进历史", len(p._said("s1")) == 1, str(p._said("s1")))

        # 第二条：同款颜文字 + 服软 → 被序列推过线，dry_run 记 would_retry
        await p.bait_guard(**_kw("行吧行吧，参数党赢了，我闭麦看你表演(｡•́︿•̀｡)", mid="s2"))
        check("第二条被判会拦", p.stats()["would_retry"] == 1, str(p.stats()))
        check("历史累计 2 条", len(p._said("s1")) == 2, str(len(p._said("s1"))))

        # 打回重生成时，第一次的文本不该进历史
        q = AntiBait()
        q.config.dry_run = False
        q.config.monitor_enabled = False
        q._data_dir = lambda: tmp
        before = len(q._said("s1"))
        await q.bait_guard(**_kw("行行行我菜我认，等你调完参数科普嗯嗯(｡•́︿•̀｡)", mid="s3"))
        check("打回的那条不进历史", len(q._said("s1")) == before, str(q._said("s1")))

        # 关掉 sequence_enabled 就只剩单条分
        r = AntiBait()
        r.config.dry_run = True
        r.config.monitor_enabled = False
        r.config.sequence_enabled = False
        r._data_dir = lambda: tmp
        await r.bait_guard(**_kw("我闭麦看你表演(｡•́︿•̀｡)", mid="s4"))
        await r.bait_guard(**_kw("行吧行吧，参数党赢了，我闭麦看你表演(｡•́︿•̀｡)", mid="s5"))
        check("关掉序列后不打回", r.stats()["would_retry"] == 0, str(r.stats()))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_deco_and_tool_leak() -> None:
    print("\n[10] 颜文字装饰符 & 工具残留")
    import amnesty as am_mod

    # 颜文字尾巴的 ✧ 不该再被算成一个 emoji
    v = scan("嘿嘿，一段单口连工牌都给人发好了(｡•̀ᴗ-)✧")
    check("颜文字+✧ 只计 0.3", abs(v.score - 0.3) < 1e-6, "score=%.2f %s" % (v.score, v.summary()))
    check("颜文字+✧ 不算 emoji", "emoji" not in {h.code for h in v.hits})
    check("✧ 归进颜文字原文", "✧" in mask("嘿嘿(｡•̀ᴗ-)✧").spans[0][1],
          str(mask("嘿嘿(｡•̀ᴗ-)✧").spans))

    # 真的 emoji 还是照常算
    check("真 emoji 仍计分", "emoji" in {h.code for h in scan("正常表情😀😁😂").hits})
    check("真 emoji 打回", scan("好好好😀😁😂🤣😊😄").blocked)

    # 工具/调试残留
    leak = ('[glass-meme-lookup] "嘎啦嘛"\n\n没有找到完全匹配的词条\n\n'
            "[未匹配到词条，结合上下文谨慎回复]")
    check("工具名残留判硬伤", scan(leak).hard >= 1, scan(leak).summary())
    check("工具名残留不可赦免", not am_mod.evaluate(leak, scan(leak), None).allowed)
    check("[memory-search] 也拦", scan("[memory-search] 查到了").hard >= 1)
    check("[web_fetch] 也拦", scan("[web_fetch] 结果如下").hard >= 1)
    check("[未找到相关记录] 也拦", scan("[未找到相关记录]").hard >= 1)

    # 不该误伤
    check("[doge] 不误伤", scan("[doge] 这个不算").hard == 0)
    check("[1] 引用不误伤", scan("[1] 引用不算").hard == 0)
    check("[abc] 不误伤", scan("[abc] 三个字母").hard == 0)
    check("书名号不误伤", scan("《message》这书我看过").hard == 0)


def test_long_text_and_density() -> None:
    print("\n[11] 长文本豁免 & 密度判定")
    import amnesty as am_mod

    # ── 堆砌是「密度」概念：短消息里塞一堆才算 ──
    short_spam = "好耶😀😁😂🤣😊😍"
    check("短消息 emoji 堆砌仍硬伤", scan(short_spam).hard >= 1, scan(short_spam).summary())

    kao_spam = "嘿嘿嘿(｡•̀ᴗ-)✧(´･ω･`)(￣▽￣)"
    check("短消息 3 个颜文字算堆砌", scan(kao_spam).hard >= 1, scan(kao_spam).summary())

    # 同样 3 个颜文字，但放在长文本里 → 不算堆砌
    long_kao = "本周群聊 Top10 来了(￣▽￣)\n\n" + ("这是一段凑字数的正文内容，用来把长度撑过两百字。" * 12) \
        + "\n3. 社死现场(´･ω･`)\n\n" + ("再补一段正文，确保总长度远超阈值。" * 6) + "(｡･ω･｡)"
    v_long = scan(long_kao)
    check("长文本里 3 个颜文字不算堆砌", v_long.hard == 0, v_long.summary())

    # ── 长文本整类跳过结构规则 ──
    long_list = ("来了来了，榜单这就端上来(￣▽￣)\n\n本周群聊 Top10：\n\n"
                 + "\n".join("%d. 「标题%d」\n这是一条讲述某件群聊事件的正文，讲得还挺细。" % (i, i)
                            for i in range(1, 11)))
    v = scan(long_list)
    check("长榜单不判列表化", "liebiao" not in {h.code for h in v.hits}, v.summary())
    check("长榜单不判过长", "changdu" not in {h.code for h in v.hits}, v.summary())
    check("长榜单不打回", not v.blocked, "score=%.2f %s" % (v.score, v.summary()))

    # 长文本里的「不是…而是…」也不该单独把整篇打回
    long_duizhang = long_list + "\n技术难点不是搬，而是别什么玩意儿都往群里倒。"
    check("长文本跳过对仗腔硬规则", scan(long_duizhang).hard == 0, scan(long_duizhang).summary())

    # ── 但「事故类」规则任何长度都要查 ──
    long_leak = long_list + '\n<message msg_id="123" user="某人"> [图片：一只猫]'
    check("长文本仍拦标签残留", scan(long_leak).hard >= 1, scan(long_leak).summary())
    long_ai = long_list + "\n作为一个AI，我无法真正理解你的感受。"
    check("长文本仍拦自曝 AI", scan(long_ai).hard >= 1, scan(long_ai).summary())

    # ── 排比要有真实的重复结构 ──
    check("要快/要稳/要省 判排比", "paili" in {h.code for h in scan("要快，要稳，要省，别整那些虚的").hits},
          scan("要快，要稳，要省，别整那些虚的").summary())
    plain = "之前开过也出过OST，但这期具体哪天开，我没翻到官方公告"
    check("普通三句不判排比", "paili" not in {h.code for h in scan(plain).hits}, scan(plain).summary())

    # 阈值可调：把 long_text_threshold 调小，长文本也照常查
    cfg_tight = RuleConfig(long_text_threshold=10)
    check("阈值可调小", scan(long_list, cfg_tight).hits != [], "调小后应命中结构规则")
    # 阈值调大后，短文本的结构规则照常生效
    check("阈值调大不影响短文本",
          "paili" in {h.code for h in scan("要快，要稳，要省", RuleConfig(long_text_threshold=9999)).hits})


async def main() -> None:
    print("=" * 70)
    print("maibot-antibait 回归测试")
    print("=" * 70)
    test_masking()
    await test_loop()
    test_tics()
    test_sdk_path()
    await test_monitor()
    await test_amnesty()
    test_label_leak()
    test_fucong()
    await test_sequence_plugin()
    test_deco_and_tool_leak()
    test_long_text_and_density()
    print("\n" + "=" * 70)
    print("通过 %d ｜ 失败 %d" % (PASS, FAIL))
    if FAILED:
        print("失败项：" + "、".join(FAILED))
    print("=" * 70)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
