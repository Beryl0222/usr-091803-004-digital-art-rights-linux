"""数字资产目录、授权评估与谱系追踪的核心领域逻辑。

灵境艺术馆将馆藏原作拆分为多款数字作品发行，本模块负责：
- 记录原作来源、采集批次、局部裁切、衍生关系、发行数量与各方贡献；
- 按地域、媒介、用途、期限组合评估许可，每次决定都写入证据快照；
- 许可齐备才锁定发行名额，支付与链上回调按幂等键去重，重试不多发藏品；
- 展示素材可替换为新版本，但已发生的展映记录保持原版本不变；
- 支持由编号反查完整谱系，并生成不泄露内部合同的对外验证摘要。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


def canonical(value: Any) -> str:
    """生成稳定的 JSON 串，用于哈希与证据快照。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_of(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


ACTIVE = "active"
REVOKED = "revoked"

STATUS_VALID = "有效"
STATUS_EXPIRED = "已到期"
STATUS_REVOKED = "已撤销"
STATUS_DISPUTE = "争议冻结"
STATUS_UNCOVERED = "未覆盖"


class DomainError(Exception):
    """业务规则冲突，status 供 HTTP 层映射。"""

    status = 400


class NotFound(DomainError):
    status = 404


class Conflict(DomainError):
    status = 409


class LicenseDenied(DomainError):
    status = 422


@dataclass
class SourceWork:
    """原作来源：馆藏机构保存的原始作品。"""

    work_id: str
    title: str
    author: str
    repository: str


@dataclass
class AcquisitionBatch:
    """采集批次：一次数字化采集的来源记录。"""

    batch_id: str
    work_id: str
    acquired_at: str
    method: str


@dataclass
class AssetVersion:
    """展示素材的一个不可改写版本。"""

    version: int
    content_hash: str
    at: str


@dataclass
class Asset:
    """数字资产：由原作裁切或衍生而来，携带来源与版本链。"""

    asset_id: str
    work_id: str
    batch_id: str
    crop: Any
    parent_asset_id: str | None
    kind: str
    versions: list[AssetVersion] = field(default_factory=list)


@dataclass
class Contribution:
    """各方贡献：采集、策划、创作等环节的责任记录。"""

    asset_id: str
    party: str
    role: str


@dataclass
class License:
    """许可：按地域、媒介、用途、期限组合授权，contract_ref 仅内部可见。"""

    license_id: str
    right_type: str
    licensor: str
    territories: tuple
    media: tuple
    purposes: tuple
    valid_from: str
    valid_to: str
    contract_ref: str
    status: str = ACTIVE


@dataclass
class Edition:
    """发行版本：限定数量，许可齐备后才锁定名额。"""

    edition_id: str
    code: str
    asset_id: str
    total: int
    required_rights: tuple
    quota_locked: bool = False
    lock_context: dict | None = None
    sold: int = 0


@dataclass
class Collectible:
    """带独立编号的藏品。"""

    serial: str
    edition_id: str
    owner: str
    minted_at: str
    payment_key: str
    tx_hash: str | None = None


class EventLedger:
    """仅追加的证据日志：事件哈希前后链接，写入后不可改写。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def append(self, event_type: str, payload: dict, snapshot: dict) -> dict:
        prev = self.events[-1]["hash"] if self.events else "GENESIS"
        body = {
            "seq": len(self.events),
            "type": event_type,
            "payload": payload,
            "snapshot": snapshot,
            "snapshot_hash": digest_of(snapshot),
            "prev": prev,
        }
        event = dict(body, hash=digest_of(body))
        self.events.append(event)
        return event


def _matches(values: tuple, value: Any) -> bool:
    return "*" in values or value in values


def _coverage(lic: License, ctx: dict) -> tuple[bool, str]:
    """判断许可是否覆盖请求场景，返回结果与原因。"""
    if lic.status != ACTIVE:
        return False, "许可已撤销"
    if not lic.valid_from <= ctx["at"] <= lic.valid_to:
        return False, "超出许可期限"
    if not _matches(lic.territories, ctx["territory"]):
        return False, "地域不在许可范围"
    if not _matches(lic.media, ctx["medium"]):
        return False, "媒介不在许可范围"
    if not _matches(lic.purposes, ctx["purpose"]):
        return False, "用途不在许可范围"
    return True, ""


class Catalog:
    """数字资产与授权的目录，所有状态变化都留下证据。"""

    def __init__(self) -> None:
        self.works: dict[str, SourceWork] = {}
        self.batches: dict[str, AcquisitionBatch] = {}
        self.assets: dict[str, Asset] = {}
        self.contributions: list[Contribution] = []
        self.licenses: dict[str, License] = {}
        self.editions: dict[str, Edition] = {}
        self.collectibles: dict[str, Collectible] = {}
        self.payments: dict[str, Collectible] = {}
        self.chain_callbacks: dict[str, dict] = {}
        self.exhibitions: dict[str, dict] = {}
        self.disputes: dict[str, dict] = {}
        self.ledger = EventLedger()

    # ---- 基础登记 ----

    def register_work(self, work_id: str, title: str, author: str, repository: str) -> dict:
        self._require_new(self.works, work_id, "原作")
        work = SourceWork(work_id, title, author, repository)
        self.works[work_id] = work
        self.ledger.append("work.registered", {"work_id": work_id, "subjects": [work_id]}, asdict(work))
        return asdict(work)

    def register_batch(self, batch_id: str, work_id: str, acquired_at: str, method: str) -> dict:
        self._require_new(self.batches, batch_id, "采集批次")
        self._work(work_id)
        batch = AcquisitionBatch(batch_id, work_id, acquired_at, method)
        self.batches[batch_id] = batch
        self.ledger.append(
            "batch.registered", {"batch_id": batch_id, "work_id": work_id, "subjects": [batch_id, work_id]}, asdict(batch)
        )
        return asdict(batch)

    def register_asset(
        self,
        asset_id: str,
        work_id: str,
        batch_id: str,
        crop: Any,
        parent_asset_id: str | None,
        kind: str,
        content_hash: str,
        at: str,
    ) -> dict:
        self._require_new(self.assets, asset_id, "资产")
        self._work(work_id)
        batch = self._batch(batch_id)
        if batch.work_id != work_id:
            raise Conflict("采集批次与原作不一致")
        if parent_asset_id is not None:
            self._asset(parent_asset_id)
        asset = Asset(asset_id, work_id, batch_id, crop, parent_asset_id, kind)
        asset.versions.append(AssetVersion(1, content_hash, at))
        self.assets[asset_id] = asset
        self.ledger.append(
            "asset.registered",
            {"asset_id": asset_id, "work_id": work_id, "batch_id": batch_id, "parent_asset_id": parent_asset_id,
             "kind": kind, "subjects": [asset_id, work_id, batch_id]},
            {"crop": crop, "version": asdict(asset.versions[0])},
        )
        return self._asset_view(asset)

    def register_contribution(self, asset_id: str, party: str, role: str) -> dict:
        self._asset(asset_id)
        contribution = Contribution(asset_id, party, role)
        self.contributions.append(contribution)
        self.ledger.append(
            "contribution.registered", {"asset_id": asset_id, "party": party, "role": role, "subjects": [asset_id]},
            asdict(contribution),
        )
        return asdict(contribution)

    # ---- 许可 ----

    def register_license(
        self,
        license_id: str,
        right_type: str,
        licensor: str,
        territories: tuple,
        media: tuple,
        purposes: tuple,
        valid_from: str,
        valid_to: str,
        contract_ref: str,
    ) -> dict:
        self._require_new(self.licenses, license_id, "许可")
        if valid_from > valid_to:
            raise Conflict("许可期限起止颠倒")
        lic = License(license_id, right_type, licensor, tuple(territories), tuple(media), tuple(purposes),
                      valid_from, valid_to, contract_ref)
        self.licenses[license_id] = lic
        self.ledger.append(
            "license.registered", {"license_id": license_id, "right_type": right_type, "subjects": [license_id]},
            self._license_snapshot(lic),
        )
        return self._license_public(lic)

    def revoke_license(self, license_id: str, at: str) -> dict:
        lic = self._license(license_id)
        if lic.status == REVOKED:
            raise Conflict("许可已撤销")
        lic.status = REVOKED
        self.ledger.append(
            "license.revoked", {"license_id": license_id, "at": at, "subjects": [license_id]},
            self._license_snapshot(lic),
        )
        return self._license_public(lic)

    def sweep_expiry(self, at: str) -> list[str]:
        """为已经越过期限的许可补记到期事件，作为证据留存。"""
        expired = [lic for lic in self.licenses.values() if lic.status == ACTIVE and lic.valid_to < at]
        for lic in expired:
            self.ledger.append(
                "license.expired",
                {"license_id": lic.license_id, "right_type": lic.right_type, "valid_to": lic.valid_to, "at": at,
                 "subjects": [lic.license_id]},
                self._license_snapshot(lic),
            )
        return [lic.license_id for lic in expired]

    # ---- 发行 ----

    def register_edition(self, edition_id: str, code: str, asset_id: str, total: int, required_rights: tuple) -> dict:
        self._require_new(self.editions, edition_id, "发行版本")
        self._asset(asset_id)
        if total <= 0:
            raise Conflict("发行数量必须为正")
        if not required_rights:
            raise Conflict("必须声明所需许可")
        edition = Edition(edition_id, code, asset_id, total, tuple(required_rights))
        self.editions[edition_id] = edition
        self.ledger.append(
            "edition.registered",
            {"edition_id": edition_id, "asset_id": asset_id, "total": total,
             "required_rights": list(required_rights), "subjects": [edition_id, asset_id]},
            {"code": code},
        )
        return self._edition_view(edition)

    def lock_edition_quota(self, edition_id: str, territory: str, medium: str, purpose: str, at: str) -> dict:
        """评估所需许可，全部齐备才锁定发行名额；评估过程留证。"""
        edition = self._edition(edition_id)
        if edition.quota_locked:
            return self._edition_view(edition)
        asset = self._asset(edition.asset_id)
        self._ensure_no_dispute(asset.asset_id)
        ctx = {"territory": territory, "medium": medium, "purpose": purpose, "at": at}
        missing = [rt for rt in edition.required_rights
                   if not self._evaluate(rt, ctx, {edition_id, asset.asset_id})]
        if missing:
            raise LicenseDenied("许可未齐备: " + ",".join(missing))
        edition.quota_locked = True
        edition.lock_context = ctx
        self.ledger.append(
            "edition.quota_locked",
            {"edition_id": edition_id, "asset_id": asset.asset_id, "context": ctx,
             "subjects": [edition_id, asset.asset_id]},
            {"edition": self._edition_view(edition),
             "licenses": [self._license_snapshot(lic) for lic in self.licenses.values()]},
        )
        return self._edition_view(edition)

    def confirm_payment(self, idempotency_key: str, edition_id: str, owner: str, at: str) -> dict:
        """支付回调：同一幂等键重试返回同一藏品，不会重复生成。"""
        if idempotency_key in self.payments:
            return self._collectible_view(self.payments[idempotency_key])
        edition = self._edition(edition_id)
        asset = self._asset(edition.asset_id)
        if not edition.quota_locked:
            raise Conflict("发行名额尚未锁定，不能生成藏品")
        self._ensure_no_dispute(asset.asset_id)
        ctx = dict(edition.lock_context or {}, at=at)
        missing = [rt for rt in edition.required_rights
                   if not self._evaluate(rt, ctx, {edition_id, asset.asset_id})]
        if missing:
            raise LicenseDenied("许可已失效: " + ",".join(missing))
        if edition.sold >= edition.total:
            raise Conflict("发行名额已售罄")
        edition.sold += 1
        serial = f"{edition.code}-{edition.sold:04d}"
        collectible = Collectible(serial, edition_id, owner, at, idempotency_key)
        self.collectibles[serial] = collectible
        self.payments[idempotency_key] = collectible
        self.ledger.append(
            "collectible.minted",
            {"serial": serial, "edition_id": edition_id, "owner": owner, "payment_key": idempotency_key,
             "at": at, "subjects": [serial, edition_id, asset.asset_id]},
            {"edition": {"sold": edition.sold, "total": edition.total},
             "asset_version": asdict(asset.versions[-1])},
        )
        return self._collectible_view(collectible)

    def confirm_chain_callback(self, callback_id: str, payment_key: str, tx_hash: str, at: str) -> dict:
        """链上回调：同一回调编号重试返回同一结果，不新增藏品。"""
        if callback_id in self.chain_callbacks:
            return self.chain_callbacks[callback_id]
        collectible = self.payments.get(payment_key)
        if collectible is None:
            raise NotFound("支付凭证不存在")
        if collectible.tx_hash and collectible.tx_hash != tx_hash:
            raise Conflict("该藏品已锚定其他交易")
        collectible.tx_hash = tx_hash
        record = {"callback_id": callback_id, "serial": collectible.serial, "tx_hash": tx_hash, "at": at}
        self.chain_callbacks[callback_id] = record
        self.ledger.append(
            "chain.anchored",
            dict(record, subjects=[collectible.serial, collectible.edition_id]),
            {"serial": collectible.serial, "tx_hash": tx_hash},
        )
        return record

    def transfer_collectible(self, serial: str, to_owner: str, at: str) -> dict:
        collectible = self._collectible(serial)
        edition = self._edition(collectible.edition_id)
        asset = self._asset(edition.asset_id)
        self._ensure_no_dispute(asset.asset_id)
        snapshot = {
            "serial": serial,
            "from": collectible.owner,
            "to": to_owner,
            "licenses": [self._license_snapshot(lic) for lic in self.licenses.values()],
        }
        self.ledger.append(
            "ownership.transferred",
            {"serial": serial, "from": collectible.owner, "to": to_owner, "at": at,
             "subjects": [serial, edition.edition_id, asset.asset_id]},
            snapshot,
        )
        collectible.owner = to_owner
        return self._collectible_view(collectible)

    # ---- 展示与展映 ----

    def replace_display(self, asset_id: str, content_hash: str, at: str) -> dict:
        """替换展示素材产生新版本；已发生的展映仍引用原版本。"""
        asset = self._asset(asset_id)
        version = AssetVersion(len(asset.versions) + 1, content_hash, at)
        asset.versions.append(version)
        self.ledger.append(
            "asset.display_replaced",
            {"asset_id": asset_id, "version": version.version, "content_hash": content_hash, "at": at,
             "subjects": [asset_id]},
            {"previous": asdict(asset.versions[-2]), "current": asdict(version)},
        )
        return asdict(version)

    def record_exhibition(self, exhibition_id: str, asset_id: str, venue: str,
                          territory: str, medium: str, at: str) -> dict:
        self._require_new(self.exhibitions, exhibition_id, "展映")
        asset = self._asset(asset_id)
        self._ensure_no_dispute(asset_id)
        ctx = {"territory": territory, "medium": medium, "purpose": "展览", "at": at}
        if not self._evaluate("展映", ctx, {asset_id, exhibition_id}):
            raise LicenseDenied("展映许可未覆盖该场景")
        version = asset.versions[-1]
        record = {
            "exhibition_id": exhibition_id,
            "asset_id": asset_id,
            "asset_version": version.version,
            "content_hash": version.content_hash,
            "venue": venue,
            "territory": territory,
            "medium": medium,
            "at": at,
        }
        self.exhibitions[exhibition_id] = record
        self.ledger.append(
            "exhibition.recorded", dict(record, subjects=[asset_id, exhibition_id]),
            {"asset_version": asdict(version)},
        )
        return record

    # ---- 争议 ----

    def open_dispute(self, dispute_id: str, asset_id: str, reason: str, at: str) -> dict:
        self._require_new(self.disputes, dispute_id, "争议")
        self._asset(asset_id)
        record = {"dispute_id": dispute_id, "asset_id": asset_id, "reason": reason,
                  "opened_at": at, "closed_at": None, "status": "open"}
        self.disputes[dispute_id] = record
        self.ledger.append("dispute.opened", dict(record, subjects=[dispute_id, asset_id]), dict(record))
        return record

    def close_dispute(self, dispute_id: str, at: str) -> dict:
        record = self.disputes.get(dispute_id)
        if record is None:
            raise NotFound("争议不存在")
        if record["status"] != "open":
            raise Conflict("争议已结案")
        record["status"] = "closed"
        record["closed_at"] = at
        self.ledger.append(
            "dispute.closed", {"dispute_id": dispute_id, "at": at, "subjects": [dispute_id, record["asset_id"]]},
            dict(record),
        )
        return record

    # ---- 查询 ----

    def rights_status(self, serial: str, at: str) -> str:
        collectible = self._collectible(serial)
        edition = self._edition(collectible.edition_id)
        asset = self._asset(edition.asset_id)
        if self._open_dispute(asset.asset_id) is not None:
            return STATUS_DISPUTE
        ctx = dict(edition.lock_context or {}, at=at)
        statuses = {self._right_status(rt, ctx) for rt in edition.required_rights}
        for st in (STATUS_REVOKED, STATUS_EXPIRED, STATUS_UNCOVERED):
            if st in statuses:
                return st
        return STATUS_VALID

    def public_digest(self, serial: str, at: str) -> dict:
        """对外摘要：可验证真伪与当前权利状态，不含合同与内部方信息。"""
        collectible = self._collectible(serial)
        edition = self._edition(collectible.edition_id)
        asset = self._asset(edition.asset_id)
        view = {
            "serial": collectible.serial,
            "edition_code": edition.code,
            "asset_id": asset.asset_id,
            "kind": asset.kind,
            "content_hash": asset.versions[-1].content_hash,
            "rights_status": self.rights_status(serial, at),
            "anchored": collectible.tx_hash is not None,
            "owner_commitment": digest_of({"owner": collectible.owner}),
        }
        return dict(view, verification=digest_of(view))

    def verify_public(self, serial: str, at: str, verification: str) -> bool:
        return self.public_digest(serial, at)["verification"] == verification

    def lineage(self, serial: str) -> dict:
        """由编号反查完整谱系与每一次授权决定（馆方内部视图）。"""
        collectible = self._collectible(serial)
        edition = self._edition(collectible.edition_id)
        asset = self._asset(edition.asset_id)
        chain = []
        current: Asset | None = asset
        while current is not None:
            chain.append(self._asset_view(current))
            current = self.assets.get(current.parent_asset_id) if current.parent_asset_id else None
        asset_ids = {a["asset_id"] for a in chain}
        subjects = asset_ids | {serial, edition.edition_id}
        decisions = [ev for ev in self.ledger.events
                     if subjects & set(ev["payload"].get("subjects", []))]
        return {
            "collectible": self._collectible_view(collectible),
            "edition": self._edition_view(edition),
            "asset_chain": chain,
            "work": asdict(self._work(asset.work_id)),
            "batch": asdict(self._batch(asset.batch_id)),
            "contributions": [asdict(c) for c in self.contributions if c.asset_id in asset_ids],
            "exhibitions": [r for r in self.exhibitions.values() if r["asset_id"] in asset_ids],
            "decisions": decisions,
        }

    # ---- 内部工具 ----

    def _evaluate(self, right_type: str, ctx: dict, subjects: set) -> bool:
        """评估某类许可并留存证据快照，返回是否获准。"""
        decisions = []
        granted = False
        for lic in self.licenses.values():
            if lic.right_type != right_type:
                continue
            ok, reason = _coverage(lic, ctx)
            decisions.append({"license_id": lic.license_id, "granted": ok, "reason": reason})
            granted = granted or ok
        self.ledger.append(
            "license.decision",
            {"right_type": right_type, "request": ctx, "granted": granted, "subjects": sorted(subjects)},
            {"decisions": decisions,
             "licenses": [self._license_snapshot(lic) for lic in self.licenses.values()
                          if lic.right_type == right_type]},
        )
        return granted

    def _right_status(self, right_type: str, ctx: dict) -> str:
        lics = [lic for lic in self.licenses.values() if lic.right_type == right_type]
        if any(_coverage(lic, ctx)[0] for lic in lics):
            return STATUS_VALID

        def scope(lic: License) -> bool:
            return (_matches(lic.territories, ctx.get("territory"))
                    and _matches(lic.media, ctx.get("medium"))
                    and _matches(lic.purposes, ctx.get("purpose")))

        if any(lic.status == REVOKED and scope(lic) for lic in lics):
            return STATUS_REVOKED
        if any(scope(lic) and lic.valid_to < ctx["at"] for lic in lics):
            return STATUS_EXPIRED
        return STATUS_UNCOVERED

    def _asset_chain_ids(self, asset_id: str) -> set:
        ids = set()
        current = self.assets.get(asset_id)
        while current is not None:
            ids.add(current.asset_id)
            current = self.assets.get(current.parent_asset_id) if current.parent_asset_id else None
        return ids

    def _open_dispute(self, asset_id: str) -> dict | None:
        chain = self._asset_chain_ids(asset_id)
        for record in self.disputes.values():
            if record["status"] == "open" and record["asset_id"] in chain:
                return record
        return None

    def _ensure_no_dispute(self, asset_id: str) -> None:
        record = self._open_dispute(asset_id)
        if record is not None:
            raise Conflict(f"资产存在未决争议: {record['dispute_id']}")

    @staticmethod
    def _require_new(store: dict, key: str, label: str) -> None:
        if key in store:
            raise Conflict(f"{label}已存在: {key}")

    def _work(self, work_id: str) -> SourceWork:
        work = self.works.get(work_id)
        if work is None:
            raise NotFound(f"原作不存在: {work_id}")
        return work

    def _batch(self, batch_id: str) -> AcquisitionBatch:
        batch = self.batches.get(batch_id)
        if batch is None:
            raise NotFound(f"采集批次不存在: {batch_id}")
        return batch

    def _asset(self, asset_id: str) -> Asset:
        asset = self.assets.get(asset_id)
        if asset is None:
            raise NotFound(f"资产不存在: {asset_id}")
        return asset

    def _license(self, license_id: str) -> License:
        lic = self.licenses.get(license_id)
        if lic is None:
            raise NotFound(f"许可不存在: {license_id}")
        return lic

    def _edition(self, edition_id: str) -> Edition:
        edition = self.editions.get(edition_id)
        if edition is None:
            raise NotFound(f"发行版本不存在: {edition_id}")
        return edition

    def _collectible(self, serial: str) -> Collectible:
        collectible = self.collectibles.get(serial)
        if collectible is None:
            raise NotFound(f"藏品编号不存在: {serial}")
        return collectible

    @staticmethod
    def _license_snapshot(lic: License) -> dict:
        """内部证据快照，包含合同编号，仅供馆内留存。"""
        return asdict(lic)

    @staticmethod
    def _license_public(lic: License) -> dict:
        view = asdict(lic)
        view.pop("contract_ref")
        return view

    @staticmethod
    def _asset_view(asset: Asset) -> dict:
        return {
            "asset_id": asset.asset_id,
            "work_id": asset.work_id,
            "batch_id": asset.batch_id,
            "crop": asset.crop,
            "parent_asset_id": asset.parent_asset_id,
            "kind": asset.kind,
            "current_version": asset.versions[-1].version,
            "content_hash": asset.versions[-1].content_hash,
            "version_count": len(asset.versions),
        }

    @staticmethod
    def _edition_view(edition: Edition) -> dict:
        return {
            "edition_id": edition.edition_id,
            "code": edition.code,
            "asset_id": edition.asset_id,
            "total": edition.total,
            "sold": edition.sold,
            "required_rights": list(edition.required_rights),
            "quota_locked": edition.quota_locked,
        }

    @staticmethod
    def _collectible_view(collectible: Collectible) -> dict:
        return {
            "serial": collectible.serial,
            "edition_id": collectible.edition_id,
            "owner": collectible.owner,
            "minted_at": collectible.minted_at,
            "tx_hash": collectible.tx_hash,
        }
