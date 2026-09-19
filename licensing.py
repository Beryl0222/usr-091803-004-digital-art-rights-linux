"""许可判定引擎。

一份许可可按地域、媒介、用途和期限的组合授权；系统在任一时刻综合许可的
生效窗口与生命周期事件（转让、撤销、争议）判定某项所需权利是否齐备。
判定结果携带许可记录的深拷贝与内容散列，作为证据快照随决定固化：
事后许可到期、撤销或权利人变更，都不会改写决定做出时的结论。
"""

import copy

from domain import parse_iso, now_iso, digest, utcnow
from domain import (
    RIGHT_PUBLICATION, RIGHT_COLLECTION, RIGHT_CREATION, RIGHT_SCREENING,
    ALL_RIGHTS,
)

# 媒介
MEDIA_DIGITAL = "digital"   # 数字藏品发行
MEDIA_PRINT = "print"       # 出版/印制
MEDIA_SCREEN = "screen"     # 展映放映
MEDIA_PHYSICAL = "physical"  # 实物仿制

# 用途
PURPOSE_DISTRIBUTION = "distribution"     # 发行/领取
PURPOSE_PUBLISH = "publish"               # 出版
PURPOSE_PUBLIC_SCREENING = "screening"    # 公开展映
PURPOSE_ARCHIVE = "archive"               # 馆藏归档

WILDCARD = "*"

# 许可状态
ST_PENDING = "pending"                  # 尚未生效
ST_ACTIVE = "active"                    # 生效中
ST_EXPIRED = "expired"                  # 已到期
ST_REVOKED = "revoked"                 # 已撤销
ST_DISPUTED = "disputed"                # 争议期间
ST_TRANSFER_PENDING = "transfer_pending"  # 转让待定期间

# 不同用途所需齐备的权利组合
RIGHTS_BY_PURPOSE = {
    PURPOSE_DISTRIBUTION: [
        RIGHT_PUBLICATION, RIGHT_COLLECTION, RIGHT_CREATION],
    PURPOSE_PUBLISH: [
        RIGHT_PUBLICATION, RIGHT_COLLECTION, RIGHT_CREATION],
    PURPOSE_PUBLIC_SCREENING: [
        RIGHT_SCREENING, RIGHT_COLLECTION, RIGHT_CREATION],
    PURPOSE_ARCHIVE: [RIGHT_COLLECTION],
}


def new_license(license_id, right, licensor_party_id, holder_party_id,
                regions=None, media=None, purposes=None,
                effective_from=None, effective_until=None,
                node_ids=None, terms_ref="", note=""):
    """构造一份许可记录。缺省范围为全地域/全媒介/全用途。"""
    return {
        "id": license_id,
        "right": right,
        "licensor_party_id": licensor_party_id,
        "holder_party_id": holder_party_id,
        "node_ids": list(node_ids or []),
        "regions": list(regions or [WILDCARD]),
        "media": list(media or [WILDCARD]),
        "purposes": list(purposes or [WILDCARD]),
        "effective_from": effective_from or now_iso(),
        "effective_until": effective_until,  # None 表示长期
        "status": ST_ACTIVE,
        "terms_ref": terms_ref,  # 内部合同编号，仅供馆方反查
        "note": note,
        "events": [
            {"type": "granted", "at": now_iso(),
             "detail": {"from": licensor_party_id, "to": holder_party_id}},
        ],
        "created_at": now_iso(),
    }


def _latest_event_of_types(license_record, types, at):
    latest = None
    for event in license_record["events"]:
        if event["type"] in types and parse_iso(event["at"]) <= at:
            if latest is None or parse_iso(event["at"]) >= parse_iso(latest["at"]):
                latest = event
    return latest


def status_at(license_record, at=None):
    """判定许可在某时刻的状态。事件优先于时间窗口。"""
    at = at or utcnow()
    if isinstance(at, str):
        at = parse_iso(at)

    revoke = _latest_event_of_types(license_record, {"revoked"}, at)
    if revoke:
        return ST_REVOKED

    dispute_open = _latest_event_of_types(license_record, {"dispute_opened"}, at)
    if dispute_open:
        resolve = _latest_event_of_types(license_record, {"dispute_resolved"}, at)
        if resolve is None or parse_iso(resolve["at"]) < parse_iso(dispute_open["at"]):
            return ST_DISPUTED

    transfer = _latest_event_of_types(license_record, {"transfer_initiated"}, at)
    if transfer:
        completed = _latest_event_of_types(license_record, {"transfer_completed"}, at)
        if completed is None or parse_iso(completed["at"]) < parse_iso(transfer["at"]):
            return ST_TRANSFER_PENDING

    if at < parse_iso(license_record["effective_from"]):
        return ST_PENDING
    if license_record["effective_until"] and at > parse_iso(license_record["effective_until"]):
        return ST_EXPIRED
    return ST_ACTIVE


def _covers(license_record, field, value):
    scope = license_record[field]
    return WILDCARD in scope or value in scope


def scope_matches(license_record, region, medium, purpose, at=None):
    """地域、媒介、用途三维度与时间窗口是否同时匹配。"""
    at = at or utcnow()
    if isinstance(at, str):
        at = parse_iso(at)
    if not (_covers(license_record, "regions", region)
            and _covers(license_record, "media", medium)
            and _covers(license_record, "purposes", purpose)):
        return False
    if at < parse_iso(license_record["effective_from"]):
        return False
    if license_record["effective_until"] and at > parse_iso(license_record["effective_until"]):
        return False
    return True


def snapshot_licenses(license_records):
    """深拷贝许可记录并生成内容散列。快照一旦生成便与外部记录脱钩。"""
    copies = {lic["id"]: copy.deepcopy(lic) for lic in license_records}
    return {
        "licenses": copies,
        "license_digests": {lid: digest(lic) for lid, lic in copies.items()},
        "snapshot_hash": digest(copies),
    }


def evaluate(licenses, lineage_node_ids, required_rights,
             region, medium, purpose, at=None):
    """组合判定：所需的每一项权利是否都有一份生效中的许可覆盖本次请求。

    licenses: 候选许可（应用层按谱系范围筛选后传入）。
    lineage_node_ids: 目标节点及其全部祖先 id，用于解析 node 级授权范围。
    返回每项权利的状态、选中的许可（深拷贝）或缺席原因，以及整体快照。
    """
    at = parse_iso(at) if at else utcnow()
    lineage = set(lineage_node_ids)
    result = {
        "at": at.isoformat(timespec="seconds"),
        "request": {"region": region, "medium": medium, "purpose": purpose},
        "rights": {},
        "all_satisfied": True,
        "missing": [],
    }

    selected = []
    selected_ids = set()
    for right in required_rights:
        candidates = [
            lic for lic in licenses
            if lic["right"] == right
            and (not lic["node_ids"] or set(lic["node_ids"]) & lineage)
        ]
        entry = {"right": right, "satisfied": False, "license_id": None,
                 "status": None, "reason": ""}
        for lic in candidates:
            status = status_at(lic, at)
            if status == ST_ACTIVE and scope_matches(lic, region, medium, purpose, at):
                entry.update(satisfied=True, license_id=lic["id"], status=status)
                if lic["id"] not in selected_ids:
                    selected_ids.add(lic["id"])
                    selected.append(lic)
                break
        if not entry["satisfied"]:
            result["all_satisfied"] = False
            result["missing"].append(right)
            if not candidates:
                entry["reason"] = "no_license"
            else:
                # 报告最接近的候选为何不可用，便于馆方排查
                states = {}
                for lic in candidates:
                    st = status_at(lic, at)
                    states.setdefault(st, []).append(lic["id"])
                if ST_ACTIVE in states:
                    entry["reason"] = "scope_mismatch"
                else:
                    # 多状态并存时按排查优先级给出主因
                    priority = (ST_DISPUTED, ST_TRANSFER_PENDING, ST_REVOKED,
                                ST_EXPIRED, ST_PENDING)
                    entry["reason"] = next(
                        (st for st in priority if st in states),
                        next(iter(states)))
                entry["candidate_states"] = states
        result["rights"][right] = entry

    snap = snapshot_licenses(selected)
    result["snapshot"] = {"snapshot_hash": snap["snapshot_hash"],
                          "license_digests": snap["license_digests"],
                          "license_count": len(snap["licenses"])}
    # 完整副本只交给应用层固化，不放在摘要里
    result["_snapshot_records"] = snap["licenses"]
    return result
