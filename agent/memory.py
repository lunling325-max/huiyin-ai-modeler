# -*- coding: utf-8 -*-
"""跨会话记忆:借 Hermes Agent 的 SessionDB(SQLite FTS5/trigram, 含 CJK 检索)当库用。

用独立的 db_path 建我们自己的库, 不碰 hermes 的 state.db。
每条记忆 = 一条伪会话里的消息; recall 用 BM25 检索历史, 命中片段会作为
"过往记忆" 注入大脑下一轮 system prompt —— 这就是"它记得上次做过什么"。

对 Hermes 的依赖只到这里: 不复用它的主循环/记忆工具/技能格式, 只是 SQLite 检索底层。
"""
import re
from pathlib import Path

_DB = Path(__file__).resolve().parent / "memory_state.db"
_SESSION = "agent-memory"  # 所有记忆存这一个伪会话


def _bigrams(s):
    """去掉空白后取相邻双字符集合, 中文检索的轻量重叠特征。"""
    s = re.sub(r"\s+", "", str(s))
    return {s[i:i + 2] for i in range(len(s) - 1)}


# 自动完成记忆里"空产物"的那种(打招呼/闲聊被误存成 任务完成...| 产物: 后面没东西)。
# recall 会把它丢掉, 别老当"相似经验"捞出来显一句。
_JUNK_RE = re.compile(r"任务完成\s*[:：].*\|?\s*产物\s*[:：]\s*$")


def _is_junk(content):
    return bool(content) and bool(_JUNK_RE.search(str(content)))


class Memory:
    def __init__(self, db_path=None):
        # SessionDB 需要 Path, 不是 str
        from hermes_state import SessionDB
        self._db = SessionDB(db_path=Path(db_path or _DB))
        try:
            self._db.create_session(_SESSION, source="zaowutai-memory")
        except Exception:
            pass  # 已存在则直接用
        # 自检一次能否写入
        try:
            self._db.message_count(_SESSION)
        except Exception as e:  # 会话不存在等 -> 尝试补建
            try:
                self._db.create_session(_SESSION, source="zaowutai-memory")
            except Exception:
                raise RuntimeError(f"记忆库初始化失败: {e}")

    # ---------- 写 ----------
    def remember(self, text: str, source: str = "agent"):
        """存一条记忆。text 应为简短一句话/一段经验。"""
        if not text or not str(text).strip():
            return
        try:
            self._db.append_message(_SESSION, "user", str(text).strip())
            return True
        except Exception as e:
            return f"[记忆写入失败] {e}"

    # ---------- 查 ----------
    def recall(self, query: str, limit: int = 6):
        """按 query 召回历史片段(中文友好)。返回片段字符串列表, 无则空表。

        主用 SessionDB 的 BM25/trigram; 对"简洁的木椅"vs"简洁木椅"这类中插词,
        trigram 会漏, 再用 bigram 重叠打分全扫一遍兜底(记忆量小, 全扫很便宜)。
        """
        query = (query or "").strip()
        if not query:
            return []
        out = []
        try:
            rows = self._db.search_messages(query, limit=limit) or []
            for r in rows:
                sn = (r or {}).get("snippet")
                if sn and sn not in out:
                    out.append(sn)
        except Exception:
            pass
        # 兜底: bigram 重叠打分(按内容倒序, 新近的优先)
        try:
            msgs = self._db.get_messages(_SESSION, limit=500) or []
        except Exception:
            msgs = []
        qbg = _bigrams(query)
        if qbg:
            scored = []
            for m in reversed(msgs):  # 新→旧
                content = str((m or {}).get("content") or "")
                if not content or content in out or _is_junk(content):
                    continue
                sc = len(qbg & _bigrams(content))
                if sc >= 2:  # 至少共享 2 个相邻双字, 过滤掉"撞了一个词就硬算相关"的噪音
                    scored.append((sc, content))
            scored.sort(key=lambda x: x[0], reverse=True)
            for _, content in scored:
                if len(out) >= limit:
                    break
                if content not in out:
                    out.append(content)
        # 过滤: 丢掉空产物的自动完成记忆
        out = [c for c in out if not _is_junk(c)]
        return out[:limit]

    # ---------- 统计(展示用) ----------
    def count(self) -> int:
        try:
            return int(self._db.message_count(_SESSION))
        except Exception:
            return 0

    def recent(self, n: int = 10):
        """最近 n 条(不检索, 按插入序取尾部), 供 UI/侧栏展示。"""
        try:
            total = self.count()
            off = max(0, total - n)
            rows = self._db.get_messages(_SESSION, offset=off, limit=n) or []
            return [str(r.get("content", "")) for r in rows][:n]
        except Exception:
            return []


if __name__ == "__main__":
    # 冒烟: 存->召回
    import sys, tempfile
    m = Memory(db_path=Path(tempfile.mkdtemp()) / "smoke.db")
    m.remember("做过一把简洁木椅: 橡木色, 座面+四条直圆柱腿+靠背, 出 chair.blend/glb/stl")
    print("recall 木椅 ->", m.recall("木椅"))
    print("count =", m.count())
