# -*- coding: utf-8 -*-
"""配置链路检验：确认 WebUI 上看到的配置项真的能驱动规则引擎。

覆盖三层：
  ① UI 层：每个字段都有中文 label / hint（否则界面显示英文字段名 + 无说明）
  ② 模型层：AntiBaitConfig 及各子节字段齐备，且顶层别名可读写
  ③ 引擎层：config → _rc() → RuleConfig → scan() 全链路生效

历史教训：v2 补丁只改了 rules.py，但 AntiBaitConfig 没声明字段、_rc() 没传参，
导致 config.toml 里写了也无效。所以这层必须专项验证。
"""
from __future__ import annotations

import io
import inspect
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import plugin as P
import rules as R

PASS = FAIL = 0
FAILS = []

# 子节 → 该节应有的字段
SECTIONS = {
    "gate": ["length_gate", "length_gate_max", "length_gate_max_sents",
             "length_gate_hard", "length_gate_keep_persona", "length_gate_skip_long_text"],
    "rules": ["soft_threshold", "punctuation_cap", "kaomoji_weight", "min_length",
              "long_text_threshold", "fucong_enabled", "sequence_enabled", "request_types"],
    "retry": ["dry_run", "max_retries", "pick_better"],
    "amnesty": ["amnesty_enabled", "amnesty_use_context", "amnesty_context_limit",
                "amnesty_echo_jaccard", "amnesty_meme_min_marks", "amnesty_answer_max_len",
                "amnesty_llm", "amnesty_llm_timeout"],
    "tics": ["tics_enabled", "tics_file", "extra_hard"],
    "monitor": ["monitor_enabled", "monitor_file", "monitor_record_all",
                "monitor_max_bytes", "monitor_max_text"],
}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % name)
    else:
        FAIL += 1
        FAILS.append(name)
        print("  ✗ %s   %s" % (name, detail))


def _fields_of(cls) -> dict:
    """取出配置类的字段元数据（含 json_schema_extra）。

    注意：无 SDK 的兼容层里 Field() 已经把 json_schema_extra 丢掉了
    （它只返回默认值），所以那种环境下拿不到 label/hint 是**预期行为**——
    真实 Runner 有 pydantic，元数据完整。这里对兼容层退化为「只查字段存在」，
    避免把环境差异误报成 bug。
    """
    out = {}
    mf = getattr(cls, "model_fields", None)
    if isinstance(mf, dict) and mf:
        for name, info in mf.items():
            extra = getattr(info, "json_schema_extra", None) or {}
            out[name] = {"extra": extra if isinstance(extra, dict) else {},
                         "desc": getattr(info, "description", "") or "",
                         "full": True}
        return out
    # 兼容层：只能拿到字段名
    for klass in reversed(cls.__mro__):
        for k, v in vars(klass).items():
            if k.startswith("_") or callable(v) or isinstance(v, (property, classmethod, staticmethod)):
                continue
            out.setdefault(k, {"extra": {}, "desc": "", "full": False})
    return out


def _has_metadata(cls) -> bool:
    """当前环境能否拿到 UI 元数据（有 pydantic 才行）。"""
    mf = getattr(cls, "model_fields", None)
    return isinstance(mf, dict) and bool(mf)


def test_section_fields() -> None:
    print("\n[1] 子节字段齐备")
    cfg = P.AntiBaitConfig()
    for sec, names in SECTIONS.items():
        holder = getattr(cfg, sec, None)
        check("子节 %s 存在" % sec, holder is not None)
        for n in names:
            check("  %s.%s" % (sec, n), hasattr(holder, n) if holder else False)


def test_ui_labels() -> None:
    """② UI 层：label / hint 必须存在且是中文。

    只在有 pydantic 的环境检查（真实 Runner 就是这种）；
    无 SDK 兼容层拿不到 json_schema_extra，跳过并说明原因。
    """
    print("\n[2] 中文 label / hint（界面渲染靠这两个）")
    import re
    cjk = re.compile(r"[\u4e00-\u9fff]")
    cls_map = {
        "gate": P.GateConfig, "rules": P.RuleConfigSection,
        "retry": P.GateRetryConfig, "amnesty": P.AmnestyConfig,
        "tics": P.TicsConfig, "monitor": P.MonitorConfig,
    }
    if not _has_metadata(P.GateConfig):
        print("  (跳过：当前环境无 pydantic，读不到 json_schema_extra —— "
              "真实 Runner 有 pydantic，界面元数据完整)")
        return
    for sec, names in SECTIONS.items():
        cls = cls_map[sec]
        fields = _fields_of(cls)
        for n in names:
            meta = fields.get(n) or {}
            extra = meta.get("extra") or {}
            label = str(extra.get("label") or "")
            hint = str(extra.get("hint") or "")
            check("%s.%s 有中文 label" % (sec, n), bool(label) and bool(cjk.search(label)),
                  "label=%r" % label)
            check("%s.%s 有 hint" % (sec, n), bool(hint) and bool(cjk.search(hint)),
                  "hint=%r" % (hint[:30],))
            check("%s.%s 有 order" % (sec, n), extra.get("order") is not None)


def test_top_aliases() -> None:
    """模型层：顶层别名必须能读写对应子节（兼容旧配置/旧代码）。"""
    print("\n[3] 顶层别名（向后兼容）")
    cfg = P.AntiBaitConfig()
    cfg.dry_run = False
    check("写 config.dry_run 落到 retry 子节", cfg.retry.dry_run is False,
          str(cfg.retry.dry_run))
    check("读 config.dry_run 来自 retry 子节", cfg.dry_run is False)
    cfg.retry.dry_run = True
    check("子节改后顶层同步", cfg.dry_run is True)

    cfg.length_gate_max = 28
    check("写 length_gate_max 落到 gate 子节", cfg.gate.length_gate_max == 28)
    cfg.monitor_enabled = False
    check("写 monitor_enabled 落到 monitor 子节", cfg.monitor.monitor_enabled is False)


def test_engine_chain() -> None:
    """引擎层：config → _rc() → scan() 真的生效。"""
    print("\n[4] 引擎链路：配置 → 打回")

    class FakeSelf:
        pass

    fs = FakeSelf()
    fs.config = P.AntiBaitConfig()
    fs.config.gate.length_gate = True
    fs.config.gate.length_gate_max = 30
    fs.config.gate.length_gate_hard = True
    fs.config.gate.length_gate_keep_persona = False
    fs._tics = lambda: []

    rc = P.AntiBait._rc(fs)
    check("_rc 读到 length_gate", rc.length_gate is True)
    check("_rc 读到 length_gate_max", rc.length_gate_max == 30)
    check("_rc 读到 keep_persona=False", rc.length_gate_keep_persona is False)

    long_text = ("主人，本鲸盯了两遍真没看到老头乐，你是不是编个地狱笑话钓我呀ww"
                 + "真压了功德确实得清零，给个时间点嘛(´ω`)")
    v = R.scan(long_text, rc)
    check("长回复被拦", v.blocked, "len=%d" % len(long_text))
    reason = v.reason_for(1)
    check("keep_persona=False 时理由无人设段", "颜文字" not in reason, reason[:60])
    check("理由仍要求「一句」", "一句" in reason)

    # 打开 keep_persona
    fs.config.gate.length_gate_keep_persona = True
    rc2 = P.AntiBait._rc(fs)
    v2 = R.scan(long_text, rc2)
    check("keep_persona=True 时理由含人设段", "颜文字" in v2.reason_for(1))


def test_defaults() -> None:
    print("\n[5] 默认值")
    d = R.RuleConfig()
    check("默认 length_gate=False（装上不生效）", d.length_gate is False)
    check("默认 length_gate_hard=True", d.length_gate_hard is True)
    check("默认 length_gate_keep_persona=True", d.length_gate_keep_persona is True)
    c = P.AntiBaitConfig()
    check("默认 dry_run=True（安全）", c.dry_run is True)
    check("默认 gate.length_gate=False", c.gate.length_gate is False)


def test_no_shared_state() -> None:
    """回归：兼容层曾出现多个实例共享配置子对象的问题。"""
    print("\n[6] 配置实例隔离")
    a, b = P.AntiBaitConfig(), P.AntiBaitConfig()
    check("两个 config 不是同一对象", a is not b)
    check("gate 子节不共享", a.gate is not b.gate)
    check("monitor 子节不共享", a.monitor is not b.monitor)
    a.monitor.monitor_enabled = False
    check("改 A 不影响 B", b.monitor.monitor_enabled is True,
          str(b.monitor.monitor_enabled))


def main() -> int:
    print("=" * 72)
    print("配置链路检验：WebUI 中文显示 + 配置真正生效")
    print("=" * 72)
    test_section_fields()
    test_ui_labels()
    test_top_aliases()
    test_engine_chain()
    test_defaults()
    test_no_shared_state()
    print("\n" + "=" * 72)
    print("通过 %d ｜ 失败 %d" % (PASS, FAIL))
    if FAILS:
        print("失败项:", FAILS[:10])
    print("=" * 72)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
