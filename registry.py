"""线程安全的内存存储：谱系图、哈希链式决策日志、幂等表、序列号。

决策日志只追加、不可改写：每条记录包含前一条的散列，任何篡改都会导致
链校验失败。许可判定时引用的证据快照（散列）也写入日志，形成可复核链条。
"""

import threading

from domain import now_iso, digest, canonical_json
from errors import ConflictError, NotFoundError

GENESIS_HASH = "0" * 64


class Registry:
    def __init__(self):
        self.lock = threading.RLock()
        self._tables = {
            "parties": {},
            "nodes": {},
            "contributions": {},
            "series": {},
            "reservations": {},
            "editions": {},
            "screenings": {},
            "licenses": {},
        }
        self.edges = []
        self.log = []
        self._idempotency = {}
        self._counters = {}

    # ------------------------------------------------------------------
    # 通用表操作（调用方须持锁或接受单条原子性）
    # ------------------------------------------------------------------

    def put(self, table, key, value):
        with self.lock:
            table_ref = self._tables[table]
            if key in table_ref:
                raise ConflictError(f"{table} 中已存在 {key}", code="duplicate")
            table_ref[key] = value
            return value

    def get(self, table, key):
        with self.lock:
            try:
                return self._tables[table][key]
            except KeyError:
                raise NotFoundError(f"{table} 中不存在 {key}")

    def find(self, table, key):
        with self.lock:
            return self._tables[table].get(key)

    def list(self, table):
        with self.lock:
            return list(self._tables[table].values())

    # ------------------------------------------------------------------
    # 谱系图
    # ------------------------------------------------------------------

    def add_edge(self, edge):
        with self.lock:
            self.edges.append(edge)
            return edge

    def edges_of_child(self, child_id):
        with self.lock:
            return [dict(e) for e in self.edges if e["child_id"] == child_id]

    def edges_of_parent(self, parent_id):
        with self.lock:
            return [dict(e) for e in self.edges if e["parent_id"] == parent_id]

    def parents_of(self, child_id):
        """直接父节点 id 列表。"""
        return [e["parent_id"] for e in self.edges_of_child(child_id) if e["active"]]

    def lineage_of(self, node_id):
        """从给定节点向上回溯完整祖先链（去重、深度优先）。"""
        visited = []
        seen = set()

        def walk(nid):
            for pid in self.parents_of(nid):
                if pid not in seen:
                    seen.add(pid)
                    walk(pid)
                    visited.append(pid)

        walk(node_id)
        return visited

    # ------------------------------------------------------------------
    # 序列号与幂等
    # ------------------------------------------------------------------

    def next_serial(self, series_id):
        with self.lock:
            serial = self._counters.get(series_id, 0) + 1
            self._counters[series_id] = serial
            return serial

    def issued_count(self, series_id):
        with self.lock:
            return self._counters.get(series_id, 0)

    def idempotent_result(self, key):
        with self.lock:
            return self._idempotency.get(key)

    def save_idempotent(self, key, result):
        with self.lock:
            # 已存在时绝不覆盖：支付/链上回调重试返回首次结果
            if key in self._idempotency:
                return self._idempotency[key], False
            self._idempotency[key] = result
            return result, True

    # ------------------------------------------------------------------
    # 哈希链式决策日志（只追加）
    # ------------------------------------------------------------------

    def record_decision(self, action, payload, evidence=None):
        """追加一条授权/发行/展映决定。返回日志条目。

        evidence 为该决定做出时的证据快照（许可记录副本的散列等），
        与决定一起固化，事后许可变化也不会改变历史结论。
        """
        with self.lock:
            seq = len(self.log) + 1
            prev_hash = self.log[-1]["entry_hash"] if self.log else GENESIS_HASH
            entry = {
                "seq": seq,
                "timestamp": now_iso(),
                "action": action,
                "payload": payload,
                "evidence": evidence or {},
                "prev_hash": prev_hash,
            }
            entry["entry_hash"] = digest(entry)
            self.log.append(entry)
            return dict(entry)

    def decisions_about(self, node_or_edition_id):
        """找出所有与某节点/藏品编号相关的决定。"""
        return self.find_decisions([node_or_edition_id])

    def find_decisions(self, target_ids):
        """找出 payload 中出现任一目标 id（藏品、系列、节点、许可、展映）的决定。

        id 在规范化 JSON 中必为字符串值，用引号边界避免 LIM_1 误命中 LIM_10。
        """
        targets = [b'"' + t.encode("utf-8") + b'"'
                   for t in dict.fromkeys(target_ids) if t]
        with self.lock:
            hits = []
            seen = set()
            for entry in self.log:
                blob = canonical_json(entry["payload"])
                if any(target in blob for target in targets):
                    if entry["seq"] not in seen:
                        seen.add(entry["seq"])
                        hits.append(dict(entry))
            return hits

    def verify_chain(self):
        """重算整条链，任何删除、插入或改写都会被发现。"""
        with self.lock:
            prev_hash = GENESIS_HASH
            for entry in self.log:
                stored = entry["entry_hash"]
                rebuilt = digest({k: v for k, v in entry.items() if k != "entry_hash"})
                if entry["prev_hash"] != prev_hash or stored != rebuilt:
                    return False
                prev_hash = stored
            return True

    def export_state(self):
        with self.lock:
            return {
                "tables": {k: dict(v) for k, v in self._tables.items()},
                "edges": [dict(e) for e in self.edges],
                "log": [dict(e) for e in self.log],
                "counters": dict(self._counters),
                "idempotency": dict(self._idempotency),
            }
