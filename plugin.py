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

SDK_AVAILABLE = True
try:
    from maibot_sdk import (  # type: ignore
        MaiBotPlugin, HookHandler, Command, Tool, PluginConfigBase, Field,
    )
except Exception:  # pragma: no cover - 只在没有 SDK 的环境走这里
    SDK_AVAILABLE = False

    class PluginConfigBase:  # noqa: D101
        pass

    def Field(default=None, default_factory=None, **_kw):  # noqa: N802
        if default_factory is not None:
            try:
                return default_factory()
            except Exception:
                return None
        return default

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

class PluginSectionConfig(PluginConfigBase):
    """宿主保留节。

    MaiBot 的 Runner 会从 `[plugin]` 节读 `enabled`（启停开关）和
    `config_version`（配置版本号）；缺任一都会判定为配置错误，
    只加载配置管理、不激活业务组件。所以这一节必须由配置模型声明出来。
    """

    __ui_label__ = "插件"
    __ui_icon__ = "plug"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件（MaiBot 启停开关）")
    config_version: str = Field(default="1.0.0", description="配置版本，改配置结构时递增")


class AntiBaitConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(
        default_factory=PluginSectionConfig,
        description="插件启停与配置版本（宿主保留节）",
    )
    enabled: bool = Field(default=True, description="插件内部总开关（与『插件』节开关同时生效）")
    dry_run: bool = Field(default=True, description="只记录不拦截，先观察误杀率")
    max_retries: int = Field(default=1, description="固定 1 次，不要改大")
    pick_better: bool = Field(default=True, description="第二次更差时回滚第一次")
    soft_threshold: float = Field(default=3.0, description="软规则打回分数")
    punctuation_cap: float = Field(default=2.0, description="标点表情类合计上限")
    kaomoji_weight: float = Field(
        default=0.3,
        description="单个颜文字的分数。颜文字常是人设特征而非八股，默认调低；设 1.0 恢复旧行为",
    )
    min_length: int = Field(default=6, description="短于此长度不检查")
    long_text_threshold: int = Field(
        default=200,
        description="超过这个字数视为产出物（榜单/教程/周报），结构类规则（列表化/排比/过长/对仗）不再判；"
                    "标签残留、自曝 AI 这类事故规则任何长度都查。设很大可关闭此豁免",
    )
    fucong_enabled: bool = Field(
        default=True,
        description="附和/服软腔这一档（行吧行吧、我菜我认、句尾嗯嗯）。单条只 1 分，靠叠加",
    )
    sequence_enabled: bool = Field(
        default=True,
        description="序列特征：同款颜文字连发、连续附和服软，各再加 1 分",
    )
    request_types: list = Field(
        default_factory=list,
        description="只处理这些 request_type，空=全部。注意 replyer 的实际值是 maisaka.replyer，不是 chat",
    )
    tics_enabled: bool = Field(default=False, description="启用口癖拦截")
    tics_file: str = Field(default="tics.json", description="口癖库文件名（在 data_dir 下）")
    extra_hard: list = Field(default_factory=list, description="追加硬规则正则")
    # ── 试运行监控 ──
    monitor_enabled: bool = Field(default=True, description="把每次检查写成 JSONL，供事后算误杀率")
    monitor_file: str = Field(default="monitor.jsonl", description="监控记录文件名（在 data_dir 下）")
    monitor_record_all: bool = Field(default=False, description="连没命中的也记（算分母用，文件会大）")
    monitor_max_bytes: int = Field(default=5242880, description="单文件上限，超过滚成 .1")
    monitor_max_text: int = Field(default=500, description="记录里正文最多存多少字")
    # ── 赦免层（打回之前的语境判断）──
    amnesty_enabled: bool = Field(default=True, description="打回前先判断是不是玩梗/复读/回答问题")
    amnesty_use_context: bool = Field(default=True, description="读最近消息做判断（需要 ctx.message）")
    amnesty_context_limit: int = Field(default=8, description="读最近几条消息")
    amnesty_echo_jaccard: float = Field(default=0.75, description="复读判定相似度")
    amnesty_meme_min_marks: int = Field(default=2, description="至少几个梗词才算玩梗")
    amnesty_answer_max_len: int = Field(default=60, description="短答上限，超过就不算随口答")
    amnesty_llm: bool = Field(default=False, description="本地判断不了时再调一次模型复核（慢、花钱，慎开）")
    amnesty_llm_timeout: float = Field(default=2.0, description="模型复核超时（秒），Hook 总超时 6 秒")


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
            if getattr(self.config, "sequence_enabled", True):
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

            if getattr(self.config, "dry_run", True):
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
        if old is None or not getattr(self.config, "pick_better", True):
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
        if not getattr(self.config, "amnesty_enabled", True):
            return None
        try:
            acfg = amnesty_mod.AmnestyConfig(
                enabled=True,
                echo_jaccard=float(getattr(self.config, "amnesty_echo_jaccard", 0.75)),
                meme_min_marks=int(getattr(self.config, "amnesty_meme_min_marks", 2)),
                answer_max_len=int(getattr(self.config, "amnesty_answer_max_len", 60)),
            )
        except Exception:
            acfg = amnesty_mod.AmnestyConfig()
        ctx = await self._amnesty_context(kw)
        am = amnesty_mod.evaluate(raw, verdict, ctx, acfg)
        if am.allowed:
            return am
        # 本地判断不了才花钱：模型复核只做一次，超时就当不赦
        if getattr(self.config, "amnesty_llm", False) and ctx.request_text:
            if await self._amnesty_llm_review(raw, ctx):
                return amnesty_mod.Amnesty(True, "llm", "模型复核：像人说的话", 0.5,
                                           [h.code for h in verdict.hits])
        return am

    async def _amnesty_context(self, kw: dict):
        empty = amnesty_mod.AmnestyContext()
        if not getattr(self.config, "amnesty_use_context", True):
            return empty
        try:
            limit = int(getattr(self.config, "amnesty_context_limit", 8) or 8)
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
        if not getattr(self.config, "monitor_enabled", True):
            return
        hit = bool(verdict.blocked) or decision == "scrubbed"
        if not (hit or getattr(self.config, "monitor_record_all", False)):
            return
        try:
            limit = int(getattr(self.config, "monitor_max_text", 500) or 500)
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
        name = getattr(self.config, "monitor_file", "monitor.jsonl") or "monitor.jsonl"
        path = os.path.join(self._data_dir(), name)
        obj = self._monitors.get(path)
        if obj is None:
            try:
                cap = int(getattr(self.config, "monitor_max_bytes", 5242880) or 5242880)
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
        return RuleConfig(
            soft_threshold=float(getattr(c, "soft_threshold", 3.0)),
            punctuation_cap=float(getattr(c, "punctuation_cap", 2.0)),
            min_length=int(getattr(c, "min_length", 6)),
            long_text_threshold=int(getattr(c, "long_text_threshold", 200)),
            kaomoji_weight=float(getattr(c, "kaomoji_weight", 0.3)),
            fucong=bool(getattr(c, "fucong_enabled", True)),
            extra_hard=list(getattr(c, "extra_hard", []) or []),
            tics=self._tics(),
        )

    def _data_dir(self) -> str:
        try:
            return str(self.ctx.paths.data_dir)
        except Exception:
            return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

    def _tics(self) -> list:
        if not getattr(self.config, "tics_enabled", False):
            return []
        name = getattr(self.config, "tics_file", "tics.json") or "tics.json"
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
