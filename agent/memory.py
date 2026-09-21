# -*- coding: utf-8 -*-
"""跨会话记忆:标准库 sqlite3 + FTS5 自建的小检索库,零第三方依赖。

一条记忆 = 表里一行。recall 先走 FTS5 trigram 检索,再用相邻双字重叠打分
兜底 —— trigram 对"木椅"这类两字词会漏,中文里这种词又最多,所以两道都要。
命中的片段会作为"过往记忆"注入大脑下一轮 system prompt,这就是"它记得上次做过什么"。
"""
import re
import sqlite3
import threading
import time
from pathlib import Path

_DB = Path(__file__).resolve().parent / "memory.db"

# 触发器负责同步 FTS 索引,不用手工维护。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS memos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    content    TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memos_fts USING fts5(
    content, content='memos', content_rowid='id', tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS memos_ai AFTER INSERT ON memos BEGIN
    INSERT INTO memos_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memos_ad AFTER DELETE ON memos BEGIN
    INSERT INTO memos_fts(memos_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memos_au AFTER UPDATE ON memos BEGIN
    INSERT INTO memos_fts(memos_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO memos_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


def _bigrams(s):
    """去掉空白后取相邻双字符集合,中文检索的轻量重叠特征。"""
    s = re.sub(r"\s+", "", str(s))
    return {s[i:i + 2] for i in range(len(s) - 1)}


# 自动完成记忆里"空产物"的那种(打招呼/闲聊被误存成 任务完成...| 产物: 后面没东西)。
# recall 会把它丢掉,别老当"相似经验"捞出来显一句。
_JUNK_RE = re.compile(r"任务完成\s*[:：].*\|?\s*产物\s*[:：]\s*$")


def _is_junk(content):
    return bool(content) and bool(_JUNK_RE.search(str(content)))


class Memory:
    def __init__(self, db_path=None):
        path = Path(db_path or _DB)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 服务端是多线程的,连接跨线程复用要自己加锁(见 self._lock)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._db.executescript(_SCHEMA)
        # 表在、索引空(比如换了分词器重建过) -> 补一次全量索引
        n_memos = self._db.execute("SELECT COUNT(*) FROM memos").fetchone()[0]
        n_fts = self._db.execute("SELECT COUNT(*) FROM memos_fts").fetchone()[0]
        if n_memos and not n_fts:
            self._db.execute("INSERT INTO memos_fts(memos_fts) VALUES ('rebuild')")
        self._db.commit()

    # ---------- 写 ----------
    def remember(self, text: str, source: str = "agent"):
        """存一条记忆。text 应为简短一句话/一段经验。"""
        text = (text or "").strip()
        if not text:
            return
        try:
            with self._lock:
                self._db.execute("INSERT INTO memos(content, created_at) VALUES (?, ?)",
                                 (text, time.time()))
                self._db.commit()
            return True
        except Exception as e:
            return f"[记忆写入失败] {e}"

    # ---------- 查 ----------
    def recall(self, query: str, limit: int = 6):
        """按 query 召回历史片段(中文友好)。返回片段字符串列表, 无则空表。

        先走 FTS5 trigram;trigram 对"简洁的木椅"vs"简洁木椅"这类中插词会漏,
        再用 bigram 重叠打分全扫一遍兜底(记忆量小, 全扫很便宜)。
        """
        query = (query or "").strip()
        if not query:
            return []
        out = []
        hit_ids = set()  # FTS 已经吐过的行, 兜底里就别再吐一遍
        try:
            # 整句当短语查;引号内转义, 免得查询串里的符号被当成 FTS5 语法
            phrase = '"' + query.replace('"', '""') + '"'
            with self._lock:
                rows = self._db.execute(
                    "SELECT rowid, snippet(memos_fts, 0, '', '', '…', 12) "
                    "FROM memos_fts WHERE memos_fts MATCH ? ORDER BY rank LIMIT ?",
                    (phrase, limit)).fetchall()
            for mid, sn in rows:
                hit_ids.add(mid)
                if sn and sn not in out:
                    out.append(sn)
        except Exception:
            pass
        # 兜底: bigram 重叠打分(按内容倒序, 新近的优先)
        try:
            with self._lock:
                msgs = self._db.execute(
                    "SELECT id, content FROM memos ORDER BY id DESC LIMIT 500").fetchall()
        except Exception:
            msgs = []
        qbg = _bigrams(query)
        if qbg:
            # 门槛跟着查询长度走: 两字查询总共才 1 个双字, 卡"至少 2 个"就永远召不回
            floor = min(2, len(qbg))
            scored = []
            for mid, content in msgs:
                content = str(content or "")
                if mid in hit_ids or not content or _is_junk(content):
                    continue
                sc = len(qbg & _bigrams(content))
                if sc >= floor:  # 长查询仍要求共享 2 个相邻双字, 挡掉"撞一个词就硬算相关"
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
            with self._lock:
                return int(self._db.execute("SELECT COUNT(*) FROM memos").fetchone()[0])
        except Exception:
            return 0

    def recent(self, n: int = 10):
        """最近 n 条(不检索, 按插入序取尾部), 供 UI/侧栏展示。"""
        try:
            with self._lock:
                rows = self._db.execute(
                    "SELECT content FROM memos ORDER BY id DESC LIMIT ?", (n,)).fetchall()
            return [str(r[0]) for r in rows][::-1][:n]
        except Exception:
            return []


if __name__ == "__main__":
    # 冒烟: 存->召回
    import tempfile
    m = Memory(db_path=Path(tempfile.mkdtemp()) / "smoke.db")
    m.remember("做过一把简洁木椅: 橡木色, 座面+四条直圆柱腿+靠背, 出 chair.blend/glb/stl")
    print("recall 木椅 ->", m.recall("木椅"))
    print("count =", m.count())
