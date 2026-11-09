# -*- coding: utf-8 -*-
"""maibot-antibait —— 八股 / 口癖拦截插件（测试版）。

接入点：`maisaka.replyer.after_response`
  - 第一次命中 → 置 retry=True 打回重生成（只打回一次）
  - 第二次生成后 → 插件自己比较两次分数，第二份更差就把 response 改回第一份
  - 有硬伤的旧结果永不回滚（宁可发第二份，也不发带硬伤的）

降级策略：SDK 不存在时本模块仍可导入，decorator 变成空壳，
方便脱离 MaiBot 跑单测和回放（见 demo.py / tests.py）。
"""
from __future__ import annotations

import asyncio
import os
import re

try:  # 包内导入
    from .rules import RuleConfig, scan, scrub, should_check, apply_sequence, HARD_RULES, WORD_RULES
    from .state import PendingStore
    from . import tics as tics_mod
    from . import monitor as monitor_mod
    from . import amnesty as amnesty_mod
except ImportError:  # 作为顶层模块导入（Runner 把插件目录塞进 sys.path）
    from rules import RuleConfig, scan, scrub, should_check, apply_sequence, HARD_RULES, WORD_RULES
    from state import PendingStore
    import tics as tics_mod
    import monitor as monitor_mod
    import amnesty as amnesty_mod

# ─────────────────────────── SDK 兼容层 ───────────────────────────

def _fresh_copy(value):
    """兼容层用：复制一份可变的默认值，避免实例间共享。"""
    import copy as _copy
    if value is None or isinstance(value, (str, int, float, bool, bytes)):
        return value
    try:
        return _copy.deepcopy(value)
    except Exception:
        return value


SDK_AVAILABLE = True
try:
    from maibot_sdk import (  # type: ignore
        MaiBotPlugin, HookHandler, Command, Tool, PluginConfigBase, Field,
    )
except Exception:  # pragma: no cover - 只在没有 SDK 的环境走这里
    SDK_AVAILABLE = False

# pydantic 单独 try：SDK 的测试替身里有 fake maibot_sdk 但没有 pydantic，
# 两者写在同一个 try 里会让「有 SDK 但无 pydantic」被误判成无 SDK。
try:
    from pydantic import model_validator  # type: ignore
except Exception:  # pragma: no cover
    def model_validator(*_a, **_kw):  # noqa: N802
        """兼容层：没有 pydantic 时 validator 退化成空装饰器。

        离线环境（单测/demo）不需要它——那里是自己 new 配置对象，
        不会走 dict 校验；真正需要迁移的是 Runner 加载 config.toml 的路径，
        那条路一定有 pydantic。
        """
        def deco(fn):
            return fn
        return deco


if not SDK_AVAILABLE:  # pragma: no cover - 只在没有 SDK 的环境走这里

    class PluginConfigBase:  # noqa: D101
        """无 SDK 时的兼容基类。

        没有 pydantic 就没有自动的字段复制，所以这里手动在构造时
        把类属性上的可变默认值 deepcopy 到实例上，避免多个实例共享配置。
        """

        def __init__(self, **kwargs):
            import copy as _copy
            for klass in reversed(type(self).__mro__):
                for k, v in list(vars(klass).items()):
                    if k.startswith("_") or callable(v) or isinstance(v, (property, classmethod, staticmethod)):
                        continue
                    if k in self.__dict__:
                        continue
                    try:
                        object.__setattr__(self, k, _copy.deepcopy(v))
                    except Exception:
                        object.__setattr__(self, k, v)
            for k, v in kwargs.items():
                try:
                    object.__setattr__(self, k, v)
                except Exception:
                    pass

    def Field(default=None, default_factory=None, **_kw):  # noqa: N802
        # 兼容层：没有 pydantic 时，字段值就是类属性。
        # 有 default_factory 时必须返回**新实例**，否则多个插件实例会共享同一个
        # 配置子对象（改一个全改），这正是之前离线测试出现「配置互相污染」的原因。
        if default_factory is not None:
            try:
                return _fresh_copy(default_factory())
            except Exception:
                return None
        return _fresh_copy(default)

    def _identity_decorator(*_a, **_kw):
        def deco(fn):
            return fn
        return deco

    HookHandler = Command = Tool = _identity_decorator  # noqa: N816

    class MaiBotPlugin:  # noqa: D101
        def __init__(self, *a, **kw):
            pass


_HOOK_KW = {"name": "bait_guard", "timeout_ms": 5000}
try:  # 枚举名以实际安装的 SDK 为准，这里做尽力而为的兼容
    from maibot_sdk.types import HookMode, HookOrder, ErrorPolicy  # type: ignore

    _HOOK_KW.update(mode=HookMode.BLOCKING, order=HookOrder.NORMAL, error_policy=ErrorPolicy.SKIP)
except Exception:
    _HOOK_KW.update(mode="blocking", order="normal", error_policy="skip")


_PAT_BY_CODE = {code: pat for code, _label, pat in HARD_RULES}
_PAT_BY_CODE.update({code: pat for code, _label, pat in WORD_RULES})

# 基类 `config` 属性的原始 getter；无 SDK 时为 None
_BASE_CONFIG_GETTER = getattr(getattr(MaiBotPlugin, "config", None), "fget", None)


# ─────────────────────────── 配置 ───────────────────────────

# ───────────────────── 配置（WebUI 中文界面）─────────────────────
#
# 界面渲染规则（见 dashboard/src/routes/plugin-config.tsx）：
#   label  → 主标题（不写就显示英文字段名，界面上很难看）
#   hint   → 标题下的小字说明
#   description 前端**不渲染**，只进 schema 供搜索/开发者看
#   order  → 字段排序
#
# 所以下面每个字段都同时写了：
#   description            （给 schema）
#   json_schema_extra      （label/hint/order，给界面）
# 并按功能拆成 7 个可折叠的配置节，避免全部挤在「通用设置」里。

class PluginSectionConfig(PluginConfigBase):
    """宿主保留节。MaiBot Runner 从这里读启停开关和配置版本号，缺一个就只加载配置管理、不激活业务组件。"""

    __ui_label__ = "插件开关"
    __ui_icon__ = "plug"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件（MaiBot 启停开关）",
        json_schema_extra={
            "label": "启用插件",
            "hint": "MaiBot 的插件启停开关。关闭后整个插件不加载，下面所有规则都不生效。",
            "order": 0,
        },
    )
    config_version: str = Field(
        default="2.0.0",
        description="配置版本，改配置结构时递增",
        json_schema_extra={
            "label": "配置版本号",
            "hint": "只有改配置结构时才需要递增，一般不用动。",
            "order": 1,
        },
    )


class GateConfig(PluginConfigBase):
    """长度闸门 —— 这个插件最有效的一条。

    实测背景：原来 12 条八股规则在 728 条真实回复上只命中 1 条（0.1%），
    因为这个 bot 根本不说「总的来说」「不是…而是…」。
    它真正的 AI 味是「太长、太完整、说完了还要补一句把话说圆」——
    这是结构问题不是词汇问题，关键词规则抓不到，只能按长度兜。
    实测信号强度是那 12 条规则的 16 倍。
    """

    __ui_label__ = "长度闸门（最有效）"
    __ui_icon__ = "ruler"
    __ui_order__ = 1

    length_gate: bool = Field(
        default=False,
        description="长度闸门总开关",
        json_schema_extra={
            "label": "启用长度闸门",
            "hint": "回复超过设定长度就打回重生成。建议先开「试运行」跑两天看误杀率，再真拦。",
            "order": 0,
        },
    )
    length_gate_max: int = Field(
        default=35,
        ge=5,
        le=500,
        description="超过这个字数算太长",
        json_schema_extra={
            "label": "字数上限",
            "hint": "真人发言中位数只有 9 字，这个 bot 中位 37 字。参考："
                    "25 字→打回 76%（真人误伤 5.6%）｜30 字→61%（3.8%）｜"
                    "35 字→45%（2.7%）｜40 字→38%（2.0%）。建议 35，嫌不够狠再往 30 压。",
            "order": 1,
        },
    )
    length_gate_max_sents: int = Field(
        default=2,
        ge=0,
        le=20,
        description="超过这个句数也算过长（0=关闭句数判定）",
        json_schema_extra={
            "label": "句数上限",
            "hint": "「说完了再补一句」是 AI 味最典型的表现，这一项直接抓它。"
                    "2=最多两句，设 0 可关闭只按字数判。"
                    "如果发现「两句短话」被误杀，就把它设成 0。",
            "order": 2,
        },
    )
    length_gate_hard: bool = Field(
        default=True,
        description="命中即打回（硬规则）",
        json_schema_extra={
            "label": "命中即打回",
            "hint": "⚠️ 建议保持开启。关掉后只记 1 分，而打回线是 3.0 分，"
                    "于是「只超长」的回复永远打不回去——实测关掉后打回率仍是 0.1%，等于没装。",
            "order": 3,
        },
    )
    length_gate_keep_persona: bool = Field(
        default=True,
        description="打回理由里要求保留颜文字/语气词/ww",
        json_schema_extra={
            "label": "保留说话风格",
            "hint": "⚠️ 建议保持开启。开启时重生成会明确「别变正经，颜文字和语气词照用」，"
                    "颜文字保留 72%；关掉后 bot 会变成客服腔，颜文字只剩 42%。",
            "order": 4,
        },
    )
    length_gate_skip_long_text: bool = Field(
        default=True,
        description="长产出物不打回",
        json_schema_extra={
            "label": "长文豁免",
            "hint": "榜单、教程、周报这类产出物天生就长，不打回。判定阈值见"
                    "「规则与打分」里的「长文判定字数」。",
            "order": 5,
        },
    )


class RuleConfigSection(PluginConfigBase):
    """规则引擎：八股规则档位、标点加权、各种阈值。"""

    __ui_label__ = "规则与打分"
    __ui_icon__ = "scale"
    __ui_order__ = 2

    soft_threshold: float = Field(
        default=3.0,
        ge=0.5,
        le=20.0,
        description="软规则打回分数线",
        json_schema_extra={
            "label": "软规则打回线",
            "hint": "软规则分数累加到多少才打回，默认 3.0。调低更严格（误杀变多）。",
            "order": 0,
        },
    )
    punctuation_cap: float = Field(
        default=2.0,
        ge=0.0,
        le=20.0,
        description="标点表情类合计上限",
        json_schema_extra={
            "label": "标点表情分上限",
            "hint": "表情、颜文字、破折号这些合计最多算几分。故意设得低于打回线（3.0），"
                    "这样纯标点问题永远打不回，必须叠加结构问题才打回。",
            "order": 1,
        },
    )
    kaomoji_weight: float = Field(
        default=0.3,
        ge=0.0,
        le=5.0,
        description="单个颜文字的分数",
        json_schema_extra={
            "label": "单个颜文字分值",
            "hint": "颜文字常是人设特征而不是八股，所以默认压到 0.3。"
                    "设成 1.0 恢复旧行为（更容易误伤爱用颜文字的 bot）。",
            "order": 2,
        },
    )
    min_length: int = Field(
        default=6,
        ge=0,
        le=100,
        description="短于此长度不检查",
        json_schema_extra={
            "label": "最短检查字数",
            "hint": "太短的话（「确实」「草」）本来就没法有 AI 味，直接跳过。",
            "order": 3,
        },
    )
    long_text_threshold: int = Field(
        default=200,
        ge=20,
        le=5000,
        description="超过这个字数视为产出物",
        json_schema_extra={
            "label": "长文判定字数",
            "hint": "超过这个字数就当产出物（榜单/教程/周报），列表化、排比、过长、"
                    "对仗这些结构规则不再判。标签残留、自曝 AI 这类事故规则任何长度都照查。"
                    "设很大可关闭豁免。",
            "order": 4,
        },
    )
    fucong_enabled: bool = Field(
        default=True,
        description="附和/服软腔这一档",
        json_schema_extra={
            "label": "附和服软腔检测",
            "hint": "「行吧行吧」「我菜我认」「嗯嗯」这类。单条只记 1 分，"
                    "靠连续出现叠加才打回——真正的 AI 味来自连着说。",
            "order": 5,
        },
    )
    sequence_enabled: bool = Field(
        default=True,
        description="序列特征",
        json_schema_extra={
            "label": "跨回复序列检测",
            "hint": "同款颜文字连发、连续附和服软，各再加 1 分。"
                    "单条「行行行」人也会说，连发三条才是机器味。",
            "order": 6,
        },
    )
    request_types: list = Field(
        default_factory=list,
        description="只处理这些 request_type，空=全部",
        json_schema_extra={
            "label": "处理范围",
            "hint": "留空=全部处理。注意 replyer 实际传的值是 maisaka.replyer 而不是 chat，"
                    "填 chat 不会生效。",
            "order": 7,
        },
    )


class GateRetryConfig(PluginConfigBase):
    """命中之后怎么办：打回重生成，再在两次结果里择优。"""

    __ui_label__ = "打回与择优"
    __ui_icon__ = "refresh"
    __ui_order__ = 3

    dry_run: bool = Field(
        default=True,
        description="只记录不拦截",
        json_schema_extra={
            "label": "试运行（只记录不拦截）",
            "hint": "开启时命中只写监控日志、不打回。建议先跑两天看误杀再关掉。"
                    "日志在 data/plugins/TommyAAKK.antibait/monitor.jsonl。",
            "order": 0,
        },
    )
    max_retries: int = Field(
        default=1,
        ge=0,
        le=3,
        description="重试次数（固定 1 次）",
        json_schema_extra={
            "label": "重试次数",
            "hint": "固定 1 次，不建议改大。Hook 是顺序调用的，多一次就多一次模型费用。",
            "order": 1,
        },
    )
    pick_better: bool = Field(
        default=True,
        description="第二次更差时回滚第一次",
        json_schema_extra={
            "label": "择优回滚",
            "hint": "第二次生成比第一次更脏时，用回第一次的文本。"
                    "硬伤（自曝 AI、标签残留）永不回滚——宁可发第二份也不发带硬伤的。",
            "order": 2,
        },
    )


class AmnestyConfig(PluginConfigBase):
    """赦免层：打回之前先看语境，人也会这么说的情况放过。"""

    __ui_label__ = "赦免（防误杀）"
    __ui_icon__ = "shield"
    __ui_order__ = 4

    amnesty_enabled: bool = Field(
        default=True,
        description="启用赦免层",
        json_schema_extra={
            "label": "启用赦免",
            "hint": "打回前先判断是不是在玩梗、复读、或回答问题——这几种情况人也会那么说。"
                    "注意自曝 AI、标签残留这类永远不赦免。",
            "order": 0,
        },
    )
    amnesty_use_context: bool = Field(
        default=True,
        description="读最近消息做判断",
        json_schema_extra={
            "label": "读取上下文",
            "hint": "读最近几条群消息判断语境。关掉后只靠句子本身判断，误杀会变多。"
                    "需要插件清单声明 message.get_recent 能力。",
            "order": 1,
        },
    )
    amnesty_context_limit: int = Field(
        default=8,
        ge=0,
        le=50,
        description="读最近几条消息",
        json_schema_extra={
            "label": "上下文条数",
            "hint": "往下读几条消息做判断，默认 8 条够用。调大不会更准只会更慢。",
            "order": 2,
        },
    )
    amnesty_echo_jaccard: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        description="复读判定相似度",
        json_schema_extra={
            "label": "复读相似度阈值",
            "hint": "和群里别人说过的话相似到这个程度就算复读，默认 0.75。"
                    "调低更容易判成复读（更宽松，但可能漏拦）。",
            "order": 3,
        },
    )
    amnesty_meme_min_marks: int = Field(
        default=2,
        ge=1,
        le=10,
        description="至少几个梗词才算玩梗",
        json_schema_extra={
            "label": "玩梗最少梗词数",
            "hint": "一句话出现几个梗词（卧槽/笑死/离谱…）才算在玩梗，默认 2 个。"
                    "调高更难被赦免（更严格）。",
            "order": 4,
        },
    )
    amnesty_answer_max_len: int = Field(
        default=60,
        ge=0,
        le=500,
        description="短答上限",
        json_schema_extra={
            "label": "短答长度上限",
            "hint": "对方提问、回复又不超过这个字数，就算「随口答」，口癖类规则可赦免。默认 60。",
            "order": 5,
        },
    )
    amnesty_llm: bool = Field(
        default=False,
        description="本地判断不了时调模型复核",
        json_schema_extra={
            "label": "模型复核（慢、花钱）",
            "hint": "⚠️ 默认关闭。开启后本地判断不了会再调一次模型，比较慢也花钱。"
                    "Hook 总超时只有 6 秒，谨慎开启。",
            "order": 6,
        },
    )
    amnesty_llm_timeout: float = Field(
        default=2.0,
        ge=0.1,
        le=5.0,
        description="模型复核超时（秒）",
        json_schema_extra={
            "label": "模型复核超时",
            "hint": "单位秒，超时就当不赦免（保守处理）。Hook 总超时 6 秒，别设太大。",
            "order": 7,
        },
    )


class TicsConfig(PluginConfigBase):
    """口癖库：你自己配的固定句式正则。"""

    __ui_label__ = "口癖库"
    __ui_icon__ = "chat"
    __ui_order__ = 5

    tics_enabled: bool = Field(
        default=False,
        description="启用口癖拦截",
        json_schema_extra={
            "label": "启用口癖拦截",
            "hint": "默认关闭。开启后按 tics.json 里的正则判断，"
                    "severity=hard 直接打回，soft 记 1 分。",
            "order": 0,
        },
    )
    tics_file: str = Field(
        default="tics.json",
        description="口癖库文件名（在 data_dir 下）",
        json_schema_extra={
            "label": "口癖库文件",
            "hint": "放在 data/plugins/TommyAAKK.antibait/ 下。"
                    "可用 antibait_tics_suggest 工具让模型生成草稿，人工确认后再启用。",
            "order": 1,
        },
    )
    extra_hard: list = Field(
        default_factory=list,
        description="追加硬规则正则",
        json_schema_extra={
            "label": "自定义硬规则",
            "hint": "每条是一个正则，命中直接打回。适合抓这个 bot 特有的口癖。"
                    "例：^（[^）]*歪头[^）]*）",
            "order": 2,
        },
    )


class MonitorConfig(PluginConfigBase):
    """试运行监控：每次检查写一行 JSONL，供事后算误杀率。"""

    __ui_label__ = "监控记录"
    __ui_icon__ = "chart"
    __ui_order__ = 6

    monitor_enabled: bool = Field(
        default=True,
        description="把每次检查写成 JSONL",
        json_schema_extra={
            "label": "启用监控记录",
            "hint": "每次检查写一行 JSON，供事后算误杀率。可用 antibait_monitor_report 工具看汇总。",
            "order": 0,
        },
    )
    monitor_file: str = Field(
        default="monitor.jsonl",
        description="监控记录文件名（在 data_dir 下）",
        json_schema_extra={
            "label": "监控文件名",
            "hint": "放在 data/plugins/TommyAAKK.antibait/ 下，一般不用改。",
            "order": 1,
        },
    )
    monitor_record_all: bool = Field(
        default=False,
        description="连没命中的也记",
        json_schema_extra={
            "label": "记录全部（含未命中）",
            "hint": "开启后才能算准确的分母（命中率=命中数/总数）。代价是文件明显变大。",
            "order": 2,
        },
    )
    monitor_max_bytes: int = Field(
        default=5242880,
        ge=64,
        description="单文件上限，超过滚成 .1",
        json_schema_extra={
            "label": "单文件上限（字节）",
            "hint": "默认 5 MB。超过后自动滚成 monitor.jsonl.1。",
            "order": 3,
        },
    )
    monitor_max_text: int = Field(
        default=500,
        ge=0,
        le=10000,
        description="记录里正文最多存多少字",
        json_schema_extra={
            "label": "正文最多存多少字",
            "hint": "默认 500 字。太长的产出物只存开头，避免日志膨胀。",
            "order": 4,
        },
    )


# ── 顶层别名：把旧字段名转发到对应子节 ──
# 生成方式：{顶层名: 子节名}。用 property 而不是重复字段，
# 避免出现「两份值不同步」的问题。
_FIELD_ALIASES = {
    # retry 节
    "dry_run": "retry",
    "max_retries": "retry",
    "pick_better": "retry",
    # rule 节
    "soft_threshold": "rules",
    "punctuation_cap": "rules",
    "kaomoji_weight": "rules",
    "min_length": "rules",
    "long_text_threshold": "rules",
    "fucong_enabled": "rules",
    "sequence_enabled": "rules",
    "request_types": "rules",
    # gate 节
    "length_gate": "gate",
    "length_gate_max": "gate",
    "length_gate_max_sents": "gate",
    "length_gate_skip_long_text": "gate",
    "length_gate_hard": "gate",
    "length_gate_keep_persona": "gate",
    # amnesty 节
    "amnesty_enabled": "amnesty",
    "amnesty_use_context": "amnesty",
    "amnesty_context_limit": "amnesty",
    "amnesty_echo_jaccard": "amnesty",
    "amnesty_meme_min_marks": "amnesty",
    "amnesty_answer_max_len": "amnesty",
    "amnesty_llm": "amnesty",
    "amnesty_llm_timeout": "amnesty",
    # tics 节
    "tics_enabled": "tics",
    "tics_file": "tics",
    "extra_hard": "tics",
    # monitor 节
    "monitor_enabled": "monitor",
    "monitor_file": "monitor",
    "monitor_record_all": "monitor",
    "monitor_max_bytes": "monitor",
    "monitor_max_text": "monitor",
}


class AntiBaitConfig(PluginConfigBase):
    """麦麦说人话：让麦麦别再说 AI 腔。

    实测结论：这个 bot 的 AI 味不在八股词汇上（12 条规则只命中 0.1%），
    而在「太长、太完整、说完了还要补一句把话说圆」。
    所以最该先开的是「长度闸门」。

    兼容性说明：2.0.0 把配置从「一堆扁平字段」改成了 7 个分节
    （gate/rules/retry/amnesty/tics/monitor/plugin）。为了让 1.x 用户的
    已有设置不丢，这里做了两层兼容：

      1. `_migrate_flat_fields`（model_validator，before）
         在 **dict 校验阶段**就把旧顶层字段搬进对应子节。
         这一层是必须的——Runner 升级时会用「默认配置为骨架 + 只覆盖同名同类型字段」
         重建配置，旧顶层的 dry_run 之类根本不存在于新骨架里，会被直接丢掉，
         压根走不到运行时。不在这层拦，用户升级后所有设置会静默重置
         （dry_run 变回 true，插件变成只记录不拦，看起来就像坏了）。

      2. 顶层 property 别名
         给代码里 `config.dry_run = False` 这种直接赋值用。
         注意它只管「对象已存在后的属性读写」，管不了构造时的 dict，
         所以不能替代第 1 层。
    """

    __ui_label__ = "麦麦说人话（原AI八股拦截）"
    __ui_icon__ = "shield-check"
    __ui_order__ = 0

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    enabled: bool = Field(
        default=True,
        description="插件内部总开关",
        json_schema_extra={
            "label": "启用插件",
            "hint": "插件内部总开关，和「插件开关」里那个同时生效（两个都开才生效）。",
            "order": 0,
        },
    )
    gate: GateConfig = Field(default_factory=GateConfig)
    rules: RuleConfigSection = Field(default_factory=RuleConfigSection)
    retry: GateRetryConfig = Field(default_factory=GateRetryConfig)
    amnesty: AmnestyConfig = Field(default_factory=AmnestyConfig)
    tics: TicsConfig = Field(default_factory=TicsConfig)
    monitor: MonitorConfig = Field(default_factory=MonitorConfig)

    @model_validator(mode="before")
    @classmethod
    def _migrate_flat_fields(cls, data):
        """把 1.x 的扁平配置字段搬进 2.0 的对应子节。

        例：旧配置里的 `dry_run = false` → `[retry] dry_run = false`

        为什么要靠 config_version 判优先级（这里踩过坑，别乱改）：

        Runner 升级时会用「默认骨架 + 只覆盖同名同类型字段」重建配置。
        旧顶层字段名在新骨架里不存在 → 被丢掉；但骨架会把子节补全成**默认值**。
        于是磁盘上会同时出现：旧顶层字段（用户的真实设置）+ 子节（默认值）。

        此时如果判「子节优先」，用户的 dry_run=false 会被默认的 true 覆盖，
        表现就是升级后插件突然变成只记录不拦 —— 用户会以为坏了。

        所以：
          · 配置里带着**旧版本号**（< 2.0）→ 说明子节是骨架补的默认值，
            顶层才是用户设置 → **顶层优先**
          · 配置已是**新版本号**（>= 2.0）→ 子节是用户真按新结构设的 → **子节优先**
        """
        if not isinstance(data, dict):
            return data
        flat = {k: data[k] for k in list(data) if k in _FIELD_ALIASES}
        if not flat:
            return data

        # 判据：配置自报的版本号。缺省当作旧版本（保守：优先保住用户旧设置）
        ver = ""
        plug = data.get("plugin")
        if isinstance(plug, dict):
            ver = str(plug.get("config_version") or "")
        legacy = True
        m = re.match(r"^\s*(\d+)\.(\d+)", ver or "")
        if m:
            major = int(m.group(1))
            legacy = major < 2

        out = dict(data)
        for k in flat:
            out.pop(k, None)
        for k, v in flat.items():
            section = _FIELD_ALIASES[k]
            cur = out.get(section)
            cur = dict(cur) if isinstance(cur, dict) else {}
            if legacy:
                # 旧配置：顶层是用户真实设置，覆盖骨架补出来的默认值
                cur[k] = v
            elif k not in cur:
                # 新配置：子节优先，只有子节没写时才用顶层残留
                cur[k] = v
            out[section] = cur
        return out




def _make_alias(section: str, name: str):
    def _get(self):
        holder = getattr(self, section, None)
        return getattr(holder, name) if holder is not None else None

    def _set(self, value):
        holder = getattr(self, section, None)
        if holder is not None:
            setattr(holder, name, value)

    return property(_get, _set)


for _top, _sec in _FIELD_ALIASES.items():
    setattr(AntiBaitConfig, _top, _make_alias(_sec, _top))
del _top, _sec


def _cfg_get(config, section: str, name: str, default):
    """从配置的子节里取值，子节不存在或为 None 时回退到顶层。

    这样旧配置（字段都在顶层）和新配置（字段分到子节）都能正常读。
    """
    holder = getattr(config, section, None)
    if holder is not None and hasattr(holder, name):
        v = getattr(holder, name)
        if v is not None:
            return v
    v = getattr(config, name, None)
    return default if v is None else v


def _fresh_copy(value):
    """给无 SDK 兼容层用：复制一份可变的默认值。

    为什么需要：没有 pydantic 时 `Field(default_factory=X)` 直接返回 X() 的结果，
    而类属性在多个实例之间是**共享**的——于是改 A 实例的配置会污染 B 实例。
    实测真实 Runner（有 pydantic 2.14）会正确深拷贝，只有兼容层有这个问题，
    但离线测试用的是兼容层，不修会导致测试结果不可信。
    """
    import copy as _copy
    if value is None or isinstance(value, (str, int, float, bool, bytes)):
        return value
    try:
        return _copy.deepcopy(value)
    except Exception:
        return value


def _pass(kw: dict) -> dict:
    return {"action": "continue", "modified_kwargs": kw}


def _fill_matched(kw: dict, verdict) -> None:
    first = verdict.hits[0] if verdict.hits else None
    if first is None:
        return
    kw["matched_regex"] = first.label
    kw["matched_regex_pattern"] = _PAT_BY_CODE.get(first.code, first.code)
    kw["matched_regex_description"] = verdict.summary()[:200]


def _messages_to_texts(resp) -> list:
    """把 message.get_recent 的返回值抽成纯文本列表。

    真实返回是 `{"success": bool, "messages": [MessageDict, ...]}`，
    文本在 `processed_plain_text`；兼容裸列表与对象形态，方便单测。
    """
    if isinstance(resp, dict):
        if resp.get("success") is False:
            return []
        msgs = resp.get("messages")
    else:
        msgs = resp
    if not isinstance(msgs, (list, tuple)):
        return []
    out = []
    for m in msgs:
        text = None
        if isinstance(m, dict):
            text = m.get("processed_plain_text")
            if not text:
                raw = m.get("raw_message")
                if isinstance(raw, dict):
                    text = raw.get("processed_plain_text") or raw.get("text")
                elif isinstance(raw, str):
                    text = raw
        else:
            text = getattr(m, "processed_plain_text", None) or getattr(m, "text", None)
        if isinstance(text, str) and text.strip():
            out.append(text.strip())
    return out


# ─────────────────────────── 插件 ───────────────────────────

class AntiBait(MaiBotPlugin):
    config_model = AntiBaitConfig

    def __init__(self, *args, **kwargs):
        try:
            super().__init__(*args, **kwargs)
        except Exception:
            pass
        self._pending = PendingStore(ttl=120.0)
        self._stats = {"checked": 0, "retried": 0, "rolled_back": 0,
                       "passed": 0, "would_retry": 0, "amnestied": 0}
        self._monitors = {}
        # 每个会话最近说过的几句（序列判定用：同款颜文字连发、连续服软）
        self._said_history = {}
        self._said_keep = 5

    # ── 配置兜底 ──

    @property
    def config(self):
        """Runner 注入配置前也能安全读取。

        SDK 的 `MaiBotPlugin.config` 在配置未注入时会抛 RuntimeError，
        单测 / demo / 组件注册前调用 `getattr(self.config, ...)` 会直接炸。
        这里退回一份默认配置实例，注入后仍返回真实配置。
        """
        getter = _BASE_CONFIG_GETTER
        if getter is not None:
            try:
                return getter(self)
            except Exception:
                pass
        inst = self.__dict__.get("_fallback_config")
        if inst is None:
            inst = AntiBaitConfig()
            self.__dict__["_fallback_config"] = inst
        return inst

    # ── 生命周期 ──

    async def on_load(self) -> None:
        self._log("info", "antibait 已加载（dry_run=%s）", getattr(self.config, "dry_run", True))

    async def on_unload(self) -> None:
        self._pending.clear()

    async def on_config_update(self, scope, config_data, version) -> None:
        self._pending.clear()
        self._monitors.clear()  # 文件名/上限可能变了，下次按新配置重建
        self._log("info", "antibait 配置已更新（%s v%s）", scope, version)

    # ── 核心 Hook ──

    @HookHandler("maisaka.replyer.after_response", **_HOOK_KW)
    async def bait_guard(self, **kwargs):
        out = dict(kwargs)
        try:
            res = await self._guard(out)
        except Exception as exc:  # 插件绝不能把聊天带崩
            self._log("warning", "antibait 内部异常，已放行：%s", exc)
            return _pass(out)
        # 记下这次最终会发出去的文本，供下一条做序列判定。
        # 打回重生成时第一次的文本并没有发出去，不能进历史。
        try:
            mk = (res or {}).get("modified_kwargs") or out
            if not mk.get("retry"):
                final = mk.get("response") or out.get("response")
                if final:
                    self._remember_said(str(out.get("session_id") or ""), final)
        except Exception:
            pass
        return res

    # ── 自有回复历史（序列判定）──

    def _said(self, session_id: str) -> list:
        """同一会话里机器人自己最近说过的话，最近的在前。"""
        return list(self._said_history.get(session_id) or [])

    def _remember_said(self, session_id: str, text: str) -> None:
        if not text:
            return
        buf = self._said_history.get(session_id)
        if buf is None:
            buf = self._said_history[session_id] = []
        buf.insert(0, text)
        del buf[self._said_keep:]
        # 会话太多时丢掉最久没更新的，防止无界增长
        if len(self._said_history) > 512:
            for k in list(self._said_history)[:128]:
                if k != session_id:
                    self._said_history.pop(k, None)

    async def _guard(self, kw: dict) -> dict:
        if not getattr(self.config, "enabled", True):
            return _pass(kw)

        rts = getattr(self.config, "request_types", None)
        rt = kw.get("request_type")
        if rts and rt and rt not in rts:
            return _pass(kw)

        raw = kw.get("response") or ""
        cfg = self._rc()
        if not should_check(raw, cfg):
            return _pass(kw)

        self._stats["checked"] += 1
        sid = str(kw.get("session_id") or "")
        verdict = scan(raw, cfg)
        retry_count = int(kw.get("retry_count") or 0)
        key = (sid, str(kw.get("reply_message_id") or "-"))

        # ── 第一次 ──
        if retry_count == 0:
            if verdict.blocked:
                # 打回之前先赦免：玩梗/复读/回答问题这类，人也会这么说，不该打回
                am = await self._amnesty(raw, verdict, kw)
                if am is not None and am.allowed:
                    self._stats["amnestied"] += 1
                    self._stats["passed"] += 1
                    self._record(kw, raw, verdict, "amnesty",
                                 extra={"channel": am.channel, "why": am.reason})
                    self._log("info", "赦免（%s）：%s | %s", am.channel, am.reason, raw[:60])
                    return _pass(kw)

            # 跨回复的序列特征：同款颜文字连发 / 连续服软（赦免过的不再叠加）
            if _cfg_get(self.config, "rules", "sequence_enabled", True):
                verdict = apply_sequence(verdict, raw, self._said(sid), cfg)

            if not verdict.blocked:
                cleaned = scrub(raw, cfg)
                if cleaned != raw and not scan(cleaned, cfg).blocked:
                    kw["response"] = cleaned
                    self._log("info", "机械净化生效：%s", cleaned[:60])
                self._stats["passed"] += 1
                self._record(kw, raw, verdict, "pass")
                return _pass(kw)

            # 命中后先试零成本净化：能改好就不用花一次模型调用
            cleaned = scrub(raw, cfg)
            if cleaned != raw and not scan(cleaned, cfg).blocked:
                kw["response"] = cleaned
                self._stats["passed"] += 1
                self._log("info", "机械净化直接救回：%s", cleaned[:60])
                self._record(kw, raw, verdict, "scrubbed", extra={"after": cleaned})
                return _pass(kw)

            if _cfg_get(self.config, "retry", "dry_run", True):
                self._stats["would_retry"] += 1
                self._record(kw, raw, verdict, "would_retry")
                self._log("info", "[dry-run] 会拦但放过 score=%.1f | %s | %s",
                          verdict.score, verdict.summary(), raw[:80])
                return _pass(kw)

            self._pending.remember(key, raw, verdict.score, verdict.hard)
            kw["retry"] = True
            kw["retry_reason"] = verdict.reason_for(1)
            _fill_matched(kw, verdict)
            self._stats["retried"] += 1
            self._record(kw, raw, verdict, "retry")
            self._log("info", "打回重生成 score=%.1f | %s", verdict.score, verdict.summary())
            return _pass(kw)

        # ── 第二次：择优 ──
        old = self._pending.pop(key)
        if old is None or not _cfg_get(self.config, "retry", "pick_better", True):
            if verdict.blocked:
                self._log("info", "第二次仍命中（无旧文本可回滚）score=%.1f", verdict.score)
                self._record(kw, raw, verdict, "keep_second_no_state")
            else:
                self._record(kw, raw, verdict, "pass_second")
            return _pass(kw)

        old_text, old_score, old_hard = old
        if not verdict.blocked:
            self._log("info", "第二次干净，采用（旧 %.1f）", old_score)
            self._record(kw, raw, verdict, "pass_second", extra={"old_score": old_score})
            return _pass(kw)

        # 第二份也可能是玩梗/回答：能解释就别为了"更干净"回滚成更差的第一份
        am = await self._amnesty(raw, verdict, kw)
        if am is not None and am.allowed:
            self._stats["amnestied"] += 1
            self._stats["passed"] += 1
            self._record(kw, raw, verdict, "amnesty_second",
                         extra={"channel": am.channel, "why": am.reason,
                                "old_score": old_score})
            self._log("info", "第二次赦免（%s），保留第二份：%s", am.channel, am.reason)
            return _pass(kw)

        # 硬伤永不回滚：宁可发第二份，也不发带硬伤的第一份
        if old_hard == 0 and old_score < verdict.score:
            kw["response"] = old_text
            self._stats["rolled_back"] += 1
            self._log("info", "择优回滚：新 %.1f > 旧 %.1f，保留第一次", verdict.score, old_score)
            self._record(kw, raw, verdict, "rollback",
                         extra={"old_score": old_score, "old_text": old_text[:500]})
        else:
            self._log("info", "择优保留第二次：新 %.1f vs 旧 %.1f（旧硬伤 %d）",
                      verdict.score, old_score, old_hard)
            self._record(kw, raw, verdict, "keep_second",
                         extra={"old_score": old_score, "old_hard": old_hard})
        return _pass(kw)

    # ── 赦免层：打回前判断「人也会这么说」 ──

    async def _amnesty(self, raw: str, verdict, kw: dict):
        """返回 Amnesty；未启用返回 None。判定为不赦时也返回结果，便于记录。"""
        if not _cfg_get(self.config, "amnesty", "amnesty_enabled", True):
            return None
        try:
            acfg = amnesty_mod.AmnestyConfig(
                enabled=True,
                echo_jaccard=float(_cfg_get(self.config, "amnesty", "amnesty_echo_jaccard", 0.75)),
                meme_min_marks=int(_cfg_get(self.config, "amnesty", "amnesty_meme_min_marks", 2)),
                answer_max_len=int(_cfg_get(self.config, "amnesty", "amnesty_answer_max_len", 60)),
            )
        except Exception:
            acfg = amnesty_mod.AmnestyConfig()
        ctx = await self._amnesty_context(kw)
        am = amnesty_mod.evaluate(raw, verdict, ctx, acfg)
        if am.allowed:
            return am
        # 本地判断不了才花钱：模型复核只做一次，超时就当不赦
        if _cfg_get(self.config, "amnesty", "amnesty_llm", False) and ctx.request_text:
            if await self._amnesty_llm_review(raw, ctx):
                return amnesty_mod.Amnesty(True, "llm", "模型复核：像人说的话", 0.5,
                                           [h.code for h in verdict.hits])
        return am

    async def _amnesty_context(self, kw: dict):
        empty = amnesty_mod.AmnestyContext()
        if not _cfg_get(self.config, "amnesty", "amnesty_use_context", True):
            return empty
        try:
            limit = int(_cfg_get(self.config, "amnesty", "amnesty_context_limit", 8) or 8)
        except Exception:
            limit = 8
        sid = str(kw.get("session_id") or "")
        if not sid:
            return empty
        try:
            resp = await self.ctx.message.get_recent(chat_id=sid, limit=limit)
        except Exception:
            return empty
        texts = _messages_to_texts(resp)
        cur = (kw.get("response") or "").strip()
        texts = [t for t in texts if t != cur]
        if not texts:
            return empty
        return amnesty_mod.AmnestyContext(request_text=texts[-1], others=texts)

    async def _amnesty_llm_review(self, raw: str, ctx) -> bool:
        try:
            timeout = float(getattr(self.config, "amnesty_llm_timeout", 2.0))
            out = await asyncio.wait_for(
                self._llm(amnesty_mod.build_llm_prompt(raw, ctx)), timeout=timeout)
        except Exception:
            return False
        v = amnesty_mod.parse_llm_verdict(out)
        return bool(v and v.get("allowed"))

    # ── 试运行监控 ──

    def _record(self, kw: dict, raw: str, verdict, decision: str, extra: dict = None) -> None:
        """把一次判定写成一行 JSONL。dry_run 下这就是"发生了什么"的唯一证据。"""
        if not _cfg_get(self.config, "monitor", "monitor_enabled", True):
            return
        hit = bool(verdict.blocked) or decision == "scrubbed"
        if not (hit or _cfg_get(self.config, "monitor", "monitor_record_all", False)):
            return
        try:
            limit = int(_cfg_get(self.config, "monitor", "monitor_max_text", 500) or 500)
        except Exception:
            limit = 500
        entry = {
            "ts": monitor_mod.now_iso(),
            "mode": "dry_run" if getattr(self.config, "dry_run", True) else "live",
            "session_id": str(kw.get("session_id") or ""),
            "reply_message_id": str(kw.get("reply_message_id") or ""),
            "request_type": kw.get("request_type"),
            "task_name": kw.get("task_name"),
            "attempt": int(kw.get("attempt") or 0),
            "retry_count": int(kw.get("retry_count") or 0),
            "score": round(float(verdict.score), 2),
            "hard": int(verdict.hard),
            "blocked": bool(verdict.blocked),
            "decision": decision,
            "rules": [
                {"code": h.code, "label": h.label, "weight": h.weight, "hard": bool(h.hard)}
                for h in verdict.hits
            ],
            "text": raw[:limit],
            "text_len": len(raw),
            "truncated": len(raw) > limit,
        }
        if extra:
            entry.update(extra)
        try:
            self._monitor_obj().record(entry)
        except Exception as exc:
            self._log("warning", "监控写入失败：%s", exc)

    def _monitor_obj(self):
        name = _cfg_get(self.config, "monitor", "monitor_file", "monitor.jsonl") or "monitor.jsonl"
        path = os.path.join(self._data_dir(), name)
        obj = self._monitors.get(path)
        if obj is None:
            try:
                cap = int(_cfg_get(self.config, "monitor", "monitor_max_bytes", 5242880) or 5242880)
            except Exception:
                cap = 5242880
            obj = monitor_mod.Monitor(path, max_bytes=cap)
            self._monitors[path] = obj
        return obj

    @Tool(name="antibait_monitor_report",
          description="汇总试运行监控记录：命中率、规则分布、高分样例，用来判断误杀",
          parameters={
              "top": {"type": "integer", "description": "每种规则最多列几个样例", "required": False},
              "clear": {"type": "boolean", "description": "汇总后是否清空监控文件", "required": False},
          })
    async def monitor_report(self, top: int = 5, clear: bool = False):
        mon = self._monitor_obj()
        records = mon.read()
        rep = monitor_mod.build_report(records, top=top)
        rep["file"] = mon.path
        rep["stats"] = self.stats()
        if clear:
            mon.clear()
            rep["cleared"] = True
        return rep

    # ── 口癖自动生成（草稿，需人工确认）──

    @Tool(name="antibait_tics_suggest",
          description="根据最近发言推断口癖候选，返回草稿供人工确认，不直接生效",
          parameters={
              "session_id": {"type": "string", "description": "要分析的会话 ID（群号）", "required": False},
              "limit": {"type": "integer", "description": "最多读多少条消息", "required": False},
          })
    async def tics_suggest(self, session_id: str = "", limit: int = 200):
        samples = await self._recent_samples(session_id, limit)
        if not samples:
            return {"ok": False, "error": "没有可用的样本（需要 ctx.message.get_recent）"}
        prompt = tics_mod.build_prompt(samples)
        try:
            raw = await self._llm(prompt)
        except Exception as exc:
            return {"ok": False, "error": "调用模型失败：%s" % exc}
        result = tics_mod.suggest_from_raw(raw, samples, existing=self._existing_patterns())
        result["ok"] = True
        if result.get("accepted"):
            path = os.path.join(self._data_dir(), "tics.draft.json")
            try:
                tics_mod.atomic_write_json(path, result["accepted"])
                result["draft_path"] = path
            except OSError as exc:
                result["draft_path"] = None
                result["warn"] = "草稿写盘失败：%s" % exc
        return result

    # ── 内部工具 ──

    def _rc(self) -> RuleConfig:
        c = self.config
        gate = getattr(c, "gate", None)
        rules_c = getattr(c, "rules", None)
        tics_c = getattr(c, "tics", None)

        def g(name, default):
            """从对应子节取值，子节缺失时回退到顶层（兼容旧配置）。"""
            for holder in (gate, rules_c, tics_c, c):
                if holder is not None and hasattr(holder, name):
                    v = getattr(holder, name)
                    if v is not None:
                        return v
            return default

        return RuleConfig(
            soft_threshold=float(g("soft_threshold", 3.0)),
            punctuation_cap=float(g("punctuation_cap", 2.0)),
            min_length=int(g("min_length", 6)),
            long_text_threshold=int(g("long_text_threshold", 200)),
            kaomoji_weight=float(g("kaomoji_weight", 0.3)),
            fucong=bool(g("fucong_enabled", True)),
            extra_hard=list(g("extra_hard", []) or []),
            tics=self._tics(),
            length_gate=bool(g("length_gate", False)),
            length_gate_max=int(g("length_gate_max", 35)),
            length_gate_max_sents=int(g("length_gate_max_sents", 2)),
            length_gate_skip_long_text=bool(g("length_gate_skip_long_text", True)),
            length_gate_hard=bool(g("length_gate_hard", True)),
            length_gate_keep_persona=bool(g("length_gate_keep_persona", True)),
        )

    def _data_dir(self) -> str:
        try:
            return str(self.ctx.paths.data_dir)
        except Exception:
            return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

    def _tics(self) -> list:
        if not _cfg_get(self.config, "tics", "tics_enabled", False):
            return []
        name = _cfg_get(self.config, "tics", "tics_file", "tics.json") or "tics.json"
        data = tics_mod.load_json(os.path.join(self._data_dir(), name), [])
        out = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict) or not item.get("enabled", True):
                continue
            pattern = str(item.get("pattern") or "").strip()
            if pattern:
                out.append((pattern, str(item.get("severity") or "soft"), str(item.get("note") or "")))
        return out

    def _existing_patterns(self) -> list:
        return [p for p, _s, _n in self._tics()]

    async def _recent_samples(self, session_id: str, limit: int) -> list:
        if not session_id:
            return []
        try:
            resp = await self.ctx.message.get_recent(chat_id=session_id, limit=limit)
        except Exception:
            return []
        return _messages_to_texts(resp)

    async def _llm(self, prompt: str) -> str:
        resp = await self.ctx.llm.generate(prompt=prompt)
        if isinstance(resp, str):
            return resp
        if isinstance(resp, dict):
            if resp.get("success") is False:
                raise RuntimeError(str(resp.get("error") or "LLM 调用失败"))
            return str(resp.get("response") or resp.get("content") or "")
        return str(getattr(resp, "response", None) or getattr(resp, "content", None) or resp)

    def _log(self, level: str, msg: str, *args) -> None:
        try:
            logger = self.ctx.logger
        except Exception:  # ctx 未注入时 SDK 的 property 会抛错
            return
        fn = getattr(logger, level, None)
        if fn is None:
            return
        try:
            fn(msg, *args)
        except Exception:
            pass

    # 便于测试/监控
    def stats(self) -> dict:
        return dict(self._stats)


def create_plugin():
    return AntiBait()
