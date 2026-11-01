# -*- coding: utf-8 -*-
"""试运行监控：把每次检查写成 JSONL，供事后算误杀率、调阈值。

只做两件事：
  Monitor.record(entry)  追加一行（带轮转，不会把磁盘写满）
  build_report(records)  汇总：命中率、规则分布、高分样例

不依赖 SDK、不联网，纯文件 IO。
"""
from __future__ import annotations

import json
import os
import threading
from collections import Counter
from datetime import datetime


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Monitor:
    def __init__(self, path: str, max_bytes: int = 5 * 1024 * 1024):
        self.path = path
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()

    def record(self, entry: dict) -> None:
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            self._rotate()
            directory = os.path.dirname(os.path.abspath(self.path)) or "."
            os.makedirs(directory, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    def _rotate(self) -> None:
        """超过上限就滚成 .1（只保留一份备份，够用了）。"""
        try:
            if os.path.getsize(self.path) >= self.max_bytes:
                backup = self.path + ".1"
                if os.path.exists(backup):
                    os.remove(backup)
                os.replace(self.path, backup)
        except OSError:
            pass

    def read(self, max_lines: int = 50000) -> list:
        out = []
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        if max_lines and len(out) > max_lines:
            out = out[-max_lines:]
        return out

    def clear(self) -> None:
        with self._lock:
            for p in (self.path, self.path + ".1"):
                try:
                    os.remove(p)
                except OSError:
                    pass


def build_report(records: list, top: int = 5) -> dict:
    total = len(records)
    blocked = [r for r in records if r.get("blocked")]
    hard = [r for r in blocked if r.get("hard")]

    rule_counts = Counter()
    for r in blocked:
        for rule in r.get("rules") or []:
            rule_counts[str(rule.get("label") or rule.get("code") or "?")] += 1

    decisions = Counter(str(r.get("decision") or "?") for r in records)
    modes = Counter(str(r.get("mode") or "?") for r in records)
    scores = [float(r.get("score") or 0) for r in records]

    samples = sorted(blocked, key=lambda r: -(float(r.get("score") or 0)))[:max(1, int(top))]

    return {
        "total": total,
        "blocked": len(blocked),
        "blocked_rate": round(len(blocked) / total, 4) if total else 0.0,
        "hard": len(hard),
        "avg_score": round(sum(scores) / len(scores), 2) if scores else 0.0,
        "modes": dict(modes),
        "decisions": dict(decisions),
        "top_rules": rule_counts.most_common(10),
        "samples": [
            {
                "ts": s.get("ts"),
                "score": s.get("score"),
                "hard": s.get("hard"),
                "text": str(s.get("text") or "")[:100],
                "rules": [r.get("label") for r in (s.get("rules") or [])],
            }
            for s in samples
        ],
    }


def format_report(rep: dict) -> str:
    lines = [
        "监控汇总：共 %d 条，命中 %d 条（%.1f%%），其中硬规则 %d 条，平均分 %.2f"
        % (rep.get("total", 0), rep.get("blocked", 0),
           float(rep.get("blocked_rate", 0)) * 100, rep.get("hard", 0), rep.get("avg_score", 0)),
    ]
    if rep.get("modes"):
        lines.append("  模式：" + "、".join("%s=%d" % kv for kv in rep["modes"].items()))
    if rep.get("decisions"):
        lines.append("  处置：" + "、".join("%s=%d" % kv for kv in rep["decisions"].items()))
    if rep.get("top_rules"):
        lines.append("  规则：" + "、".join("%s×%d" % (k, v) for k, v in rep["top_rules"]))
    for s in rep.get("samples") or []:
        lines.append("  [%.1f] %s  ← %s" % (float(s.get("score") or 0),
                                            str(s.get("text") or "")[:50],
                                            "、".join(s.get("rules") or [])))
    return "\n".join(lines)
