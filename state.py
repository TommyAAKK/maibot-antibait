# -*- coding: utf-8 -*-
"""重生成状态存储：记住第一次的原文与分数，供第二次择优时回滚。

Hook 是顺序调用的，生成器自己不留旧文本（`maisaka_generator_base.py`
每轮直接覆盖 response），所以「两次对比择优」必须由插件自己记。

key 用 (session_id, reply_message_id)。MaiBot 同一会话的轮次是串行的，
撞车概率很低；真撞了就是后来的覆盖前面的，退化成「不做择优」，不会出错。
"""
from __future__ import annotations

import time
from collections import OrderedDict


class PendingStore:
    def __init__(self, ttl: float = 120.0, max_items: int = 256):
        self.ttl = float(ttl)
        self.max_items = int(max_items)
        self._d: "OrderedDict[tuple, tuple]" = OrderedDict()

    def remember(self, key: tuple, text: str, score: float, hard: int) -> None:
        self._d[key] = (time.time(), text, float(score), int(hard))
        self._d.move_to_end(key)
        while len(self._d) > self.max_items:
            self._d.popitem(last=False)

    def pop(self, key: tuple):
        """取出并删除；过期或不存在返回 None。"""
        item = self._d.pop(key, None)
        if item is None:
            return None
        ts, text, score, hard = item
        if time.time() - ts > self.ttl:
            return None
        return text, score, hard

    def clear(self) -> None:
        self._d.clear()

    def __len__(self) -> int:
        return len(self._d)
