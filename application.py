"""应用服务层：登记、许可生命周期、名额锁定、幂等发行、展映冻结。

关键不变量：
* 许可不齐备时绝不消耗发行名额；检查、占号、生成藏品在同一把锁内完成。
* 支付或链上回调凭幂等键重试：永远返回首次结果，序号与数量不变。
* 展映一经发生即冻结；素材换版只追加新版本，历史素材与授权快照都保留。
* 许可的转让、到期、撤销、争议不影响已固化决定中的证据快照。
"""

import copy

from domain import (
    Node, Series, Contribution, ProvenanceEdge,
    NODE_ORIGINAL, NODE_BATCH, NODE_CROP, NODE_EDITION,
    EDITION_LIMITED, ALL_RIGHTS,
    REL_SOURCE, REL_CROPPED, REL_DERIVED, REL_EMBODIED, REL_MATERIAL,
    now_iso, digest, short_id,
)
import licensing
from errors import LicenseError, EditionError, ConflictError, NotFoundError, ValidationError


def _public_edition(edition):
    return {k: v for k, v in edition.items() if k != "evidence"}


class ArtRightsService:
    def __init__(self, registry):
        self.registry = registry

    # ==================================================================
    # 机构与谱系登记
    # ==================================================================

    def register_party(self, party_id, name, kind="institution"):
        party = {"id": party_id, "name": name, "kind": kind}
        return self.registry.put("parties", party_id, party)

    def _add_node(self, node_id, kind, title, metadata):
        node = Node(node_id, kind, title, metadata).to_dict()
        return self.registry.put("nodes", node_id, node)

    def register_original(self, node_id, title, **metadata):
        return self._add_node(node_id, NODE_ORIGINAL, title, metadata)

    def register_batch(self, batch_id, original_id, title, **detail):
        self.registry.get("nodes", original_id)
        node = self._add_node(batch_id, NODE_BATCH, title, detail)
        self.registry.add_edge(ProvenanceEdge(
            original_id, batch_id, REL_SOURCE, detail).to_dict())
        return node

    def register_crop(self, crop_id, parent_id, title, **detail):
        self.registry.get("nodes", parent_id)
        node = self._add_node(crop_id, NODE_CROP, title, detail)
        self.registry.add_edge(ProvenanceEdge(
            parent_id, crop_id, REL_CROPPED, detail).to_dict())
        return node

    def register_artwork(self, work_id, parent_ids, title, **metadata):
        if isinstance(parent_ids, str):
            parent_ids = [parent_ids]
        for pid in parent_ids:
            self.registry.get("nodes", pid)
        node = self._add_node(work_id, NODE_EDITION, title, metadata)
        for pid in parent_ids:
            self.registry.add_edge(ProvenanceEdge(
                pid, work_id, REL_DERIVED, {}).to_dict())
        return node

    def add_contribution(self, contribution_id, node_id, party_id, role,
                         rights=None, note=""):
        self.registry.get("nodes", node_id)
        self.registry.get("parties", party_id)
        contribution = Contribution(
            contribution_id, node_id, party_id, role,
            list(rights or []), note).to_dict()
        return self.registry.put("contributions", contribution_id, contribution)

    def create_series(self, series_id, work_node_id, title,
                      kind=EDITION_LIMITED, edition_size=None,
                      required_rights=None):
        self.registry.get("nodes", work_node_id)
        if edition_size is not None and (not isinstance(edition_size, int) or edition_size <= 0):
            raise ValidationError("edition_size 必须为正整数或 null")
        series = Series(
            series_id, title, kind, work_node_id, edition_size,
            list(required_rights) if required_rights is not None else None,
        ).to_dict()
        self.registry.put("series", series_id, series)
        self.registry.add_edge(ProvenanceEdge(
            work_node_id, series_id, REL_EMBODIED, {}).to_dict())
        return series

    def _lineage_for_series(self, series_id):
        """[系列, 数字作品, 裁切, 批次, 原作]，由近及远。"""
        ancestors = self.registry.lineage_of(series_id)  # 祖先按原作→父节点
        return [series_id] + list(reversed(ancestors))

    # ==================================================================
    # 许可授予与生命周期
    # ==================================================================

    def grant_license(self, license_id, right, licensor_party_id, holder_party_id,
                      regions=None, media=None, purposes=None,
                      effective_from=None, effective_until=None,
                      node_ids=None, terms_ref="", note=""):
        self.registry.get("parties", licensor_party_id)
        self.registry.get("parties", holder_party_id)
        record = licensing.new_license(
            license_id, right, licensor_party_id, holder_party_id,
            regions, media, purposes, effective_from, effective_until,
            node_ids, terms_ref, note)
        self.registry.put("licenses", license_id, record)
        self.registry.record_decision(
            "license_granted",
            {"license_id": license_id, "right": right,
             "licensor_party_id": licensor_party_id,
             "holder_party_id": holder_party_id, "node_ids": node_ids or [],
             "regions": record["regions"], "media": record["media"],
             "purposes": record["purposes"],
             "effective_from": record["effective_from"],
             "effective_until": record["effective_until"]},
            {"license_digest": digest(record)})
        return record

    def _append_license_event(self, license_id, action, event_type, detail, at=None):
        with self.registry.lock:
            record = self.registry.get("licenses", license_id)
            record["events"].append(
                {"type": event_type, "at": at or now_iso(), "detail": detail})
            self.registry.record_decision(
                action,
                {"license_id": license_id, "right": record["right"],
                 "holder_party_id": record["holder_party_id"], "detail": detail},
                {"license_digest": digest(record)})
            return record

    def initiate_transfer(self, license_id, to_party_id, at=None):
        self.registry.get("parties", to_party_id)
        return self._append_license_event(
            license_id, "license_transfer_initiated", "transfer_initiated",
            {"to_party_id": to_party_id}, at)

    def complete_transfer(self, license_id, at=None):
        with self.registry.lock:
            record = self.registry.get("licenses", license_id)
            pending = licensing._latest_event_of_types(
                record, {"transfer_initiated"}, licensing.utcnow())
            if not pending:
                raise ConflictError("许可不存在进行中的转让", code="no_transfer")
            new_holder = pending["detail"]["to_party_id"]
            old_holder = record["holder_party_id"]
            record["holder_party_id"] = new_holder
            record["events"].append(
                {"type": "transfer_completed", "at": at or now_iso(),
                 "detail": {"from_party_id": old_holder,
                            "to_party_id": new_holder}})
            self.registry.record_decision(
                "license_transfer_completed",
                {"license_id": license_id, "right": record["right"],
                 "detail": {"from_party_id": old_holder,
                            "to_party_id": new_holder}},
                {"license_digest": digest(record)})
            return record

    def revoke_license(self, license_id, reason="", at=None):
        return self._append_license_event(
            license_id, "license_revoked", "revoked", {"reason": reason}, at)

    def open_dispute(self, license_id, reason="", at=None):
        return self._append_license_event(
            license_id, "license_dispute_opened", "dispute_opened",
            {"reason": reason}, at)

    def resolve_dispute(self, license_id, resolution="", at=None):
        return self._append_license_event(
            license_id, "license_dispute_resolved", "dispute_resolved",
            {"resolution": resolution}, at)

    # ==================================================================
    # 许可评估
    # ==================================================================

    def _required_rights(self, series, purpose):
        if series.get("required_rights"):
            return series["required_rights"]
        return licensing.RIGHTS_BY_PURPOSE.get(purpose, list(ALL_RIGHTS))

    def evaluate_series(self, series_id, region, medium, purpose, at=None):
        series = self.registry.get("series", series_id)
        lineage = self._lineage_for_series(series_id)
        all_licenses = self.registry.list("licenses")
        result = licensing.evaluate(
            all_licenses, lineage, self._required_rights(series, purpose),
            region, medium, purpose, at)
        result["lineage"] = lineage
        result["series_id"] = series_id
        return result

    # ==================================================================
    # 名额锁定与发行
    # ==================================================================

    @staticmethod
    def _format_number(series, serial):
        size = series["edition_size"]
        width = len(str(size)) if size else 4
        return f"{series['id']}-{serial:0{width}d}"

    def _allocate_serial(self, series):
        """调用方必须持有 registry.lock。"""
        used = self.registry.issued_count(series["id"])
        if series["edition_size"] is not None and used >= series["edition_size"]:
            raise EditionError(
                f"系列 {series['id']} 的 {series['edition_size']} 个名额已锁定完毕",
                code="sold_out",
                details={"series_id": series["id"],
                         "edition_size": series["edition_size"], "locked": used})
        return self.registry.next_serial(series["id"])

    def reserve_slot(self, series_id, region, medium, purpose,
                     reservation_id=None, at=None):
        """所需许可齐备才占用一个名额，并固化当时的证据快照。"""
        with self.registry.lock:
            series = self.registry.get("series", series_id)
            evaluation = self.evaluate_series(
                series_id, region, medium, purpose, at)
            if not evaluation["all_satisfied"]:
                self.registry.record_decision(
                    "slot_reservation_denied",
                    {"series_id": series_id, "region": region, "medium": medium,
                     "purpose": purpose, "missing": evaluation["missing"],
                     "rights": {r: {k: v for k, v in info.items()
                                    if k != "candidate_states"}
                                for r, info in evaluation["rights"].items()}},
                    {"snapshot_hash": evaluation["snapshot"]["snapshot_hash"]})
                raise LicenseError(
                    "所需许可未齐备，不能锁定发行名额",
                    details={"missing": evaluation["missing"],
                             "rights": evaluation["rights"],
                             "lineage": evaluation["lineage"]})
            serial = self._allocate_serial(series)
            number = self._format_number(series, serial)
            reservation_id = reservation_id or short_id(
                "rsv", f"{series_id}:{serial}:{now_iso()}")
            reservation = {
                "id": reservation_id,
                "series_id": series_id,
                "serial": serial,
                "number": number,
                "status": "locked",
                "request": {"region": region, "medium": medium, "purpose": purpose},
                "locked_at": now_iso(),
                "evidence": {
                    "snapshot_hash": evaluation["snapshot"]["snapshot_hash"],
                    "license_digests": evaluation["snapshot"]["license_digests"],
                    "snapshot": evaluation["_snapshot_records"],
                },
            }
            self.registry.put("reservations", reservation_id, reservation)
            self.registry.record_decision(
                "slot_locked",
                {"reservation_id": reservation_id, "series_id": series_id,
                 "serial": serial, "number": number,
                 "region": region, "medium": medium, "purpose": purpose},
                {"snapshot_hash": reservation["evidence"]["snapshot_hash"],
                 "license_digests": reservation["evidence"]["license_digests"]})
            return copy.deepcopy(reservation)

    def issue(self, series_id, recipient, idempotency_key,
              region, medium, purpose, at=None):
        """支付/链上回调的唯一发行入口。原子完成：校验→占号→生成藏品。

        同一 idempotency_key 的任意重试返回首次生成的同编号藏品，
        不多占用名额、不多生成记录。
        """
        if not idempotency_key:
            raise ValidationError("发行必须携带幂等键")
        with self.registry.lock:
            replay = self.registry.idempotent_result(idempotency_key)
            if replay is not None:
                edition = self.registry.get("editions", replay["edition_id"])
                return copy.deepcopy(edition), True

            series = self.registry.get("series", series_id)
            evaluation = self.evaluate_series(
                series_id, region, medium, purpose, at)
            if not evaluation["all_satisfied"]:
                self.registry.record_decision(
                    "edition_issue_denied",
                    {"series_id": series_id, "idempotency_key": idempotency_key,
                     "recipient": recipient, "region": region, "medium": medium,
                     "purpose": purpose, "missing": evaluation["missing"],
                     "rights": {r: {k: v for k, v in info.items()
                                    if k != "candidate_states"}
                                for r, info in evaluation["rights"].items()}},
                    {"snapshot_hash": evaluation["snapshot"]["snapshot_hash"],
                     "license_digests": evaluation["snapshot"]["license_digests"]})
                raise LicenseError(
                    "所需许可未齐备，发行名额未锁定、藏品未生成",
                    details={"missing": evaluation["missing"],
                             "rights": evaluation["rights"],
                             "lineage": evaluation["lineage"]})

            serial = self._allocate_serial(series)
            number = self._format_number(series, serial)
            lineage = evaluation["lineage"]
            edition = {
                "id": number,
                "series_id": series_id,
                "series_kind": series["kind"],
                "serial": serial,
                "edition_number": number,
                "edition_size": series["edition_size"],
                "recipient": recipient,
                "request": {"region": region, "medium": medium, "purpose": purpose},
                "status": "issued",
                "issued_at": now_iso(),
                "lineage": lineage,
                "evidence": {
                    "snapshot_hash": evaluation["snapshot"]["snapshot_hash"],
                    "license_digests": evaluation["snapshot"]["license_digests"],
                    "snapshot": evaluation["_snapshot_records"],
                },
            }
            self.registry.put("editions", number, edition)
            decision = self.registry.record_decision(
                "edition_issued",
                {"edition_id": number, "series_id": series_id,
                 "serial": serial, "recipient": recipient,
                 "idempotency_key": idempotency_key,
                 "region": region, "medium": medium, "purpose": purpose,
                 "lineage": lineage},
                {"snapshot_hash": edition["evidence"]["snapshot_hash"],
                 "license_digests": edition["evidence"]["license_digests"]})
            edition["decision_seq"] = decision["seq"]
            self.registry.save_idempotent(
                idempotency_key, {"edition_id": number, "issued_at": edition["issued_at"]})
            return copy.deepcopy(edition), False

    def confirm_reservation(self, reservation_id, recipient, idempotency_key, at=None):
        """先锁名额、后收链上回调的两段式流程。"""
        if not idempotency_key:
            raise ValidationError("确认发行必须携带幂等键")
        with self.registry.lock:
            replay = self.registry.idempotent_result(idempotency_key)
            if replay is not None:
                return copy.deepcopy(self.registry.get("editions", replay["edition_id"])), True

            reservation = self.registry.get("reservations", reservation_id)
            if reservation["status"] != "locked":
                raise ConflictError(f"预留名额状态为 {reservation['status']}，不能确认",
                                    code="reservation_not_locked")
            series = self.registry.get("series", reservation["series_id"])
            req = reservation["request"]
            evaluation = self.evaluate_series(
                series["id"], req["region"], req["medium"], req["purpose"], at)
            if not evaluation["all_satisfied"]:
                self.registry.record_decision(
                    "reservation_confirmation_denied",
                    {"reservation_id": reservation_id,
                     "series_id": series["id"], "missing": evaluation["missing"]},
                    {"snapshot_hash": evaluation["snapshot"]["snapshot_hash"]})
                raise LicenseError("确认时所需许可已不齐备",
                                   details={"missing": evaluation["missing"]})

            number = reservation["number"]
            edition = {
                "id": number,
                "series_id": series["id"],
                "series_kind": series["kind"],
                "serial": reservation["serial"],
                "edition_number": number,
                "edition_size": series["edition_size"],
                "recipient": recipient,
                "request": dict(req),
                "status": "issued",
                "issued_at": now_iso(),
                "lineage": self._lineage_for_series(series["id"]),
                "reservation_id": reservation_id,
                "evidence": {
                    "snapshot_hash": evaluation["snapshot"]["snapshot_hash"],
                    "license_digests": evaluation["snapshot"]["license_digests"],
                    "snapshot": evaluation["_snapshot_records"],
                    "lock_snapshot_hash": reservation["evidence"]["snapshot_hash"],
                },
            }
            self.registry.put("editions", number, edition)
            reservation["status"] = "confirmed"
            decision = self.registry.record_decision(
                "edition_issued",
                {"edition_id": number, "series_id": series["id"],
                 "serial": reservation["serial"], "recipient": recipient,
                 "reservation_id": reservation_id,
                 "idempotency_key": idempotency_key,
                 "region": req["region"], "medium": req["medium"],
                 "purpose": req["purpose"]},
                {"snapshot_hash": edition["evidence"]["snapshot_hash"],
                 "license_digests": edition["evidence"]["license_digests"],
                 "lock_snapshot_hash": reservation["evidence"]["snapshot_hash"]})
            edition["decision_seq"] = decision["seq"]
            self.registry.save_idempotent(
                idempotency_key, {"edition_id": number, "issued_at": edition["issued_at"]})
            return copy.deepcopy(edition), False

    def transfer_edition(self, edition_id, to_recipient, at=None):
        """藏品转让：只追加让渡记录，不改变编号与首发证据。"""
        with self.registry.lock:
            edition = self.registry.get("editions", edition_id)
            event = {"from": edition["recipient"], "to": to_recipient,
                     "at": at or now_iso()}
            edition.setdefault("transfers", []).append(event)
            edition["recipient"] = to_recipient
            self.registry.record_decision(
                "edition_transferred",
                {"edition_id": edition_id, **event},
                {"snapshot_hash": edition["evidence"]["snapshot_hash"]})
            return copy.deepcopy(edition)

    # ==================================================================
    # 展映：决定固化，素材可换版但不可改写历史
    # ==================================================================

    def schedule_screening(self, screening_id, edition_id, region, medium,
                           purpose, scheduled_at, material_ref, at=None):
        with self.registry.lock:
            edition = self.registry.get("editions", edition_id)
            series = self.registry.get("series", edition["series_id"])
            lineage = self._lineage_for_series(series["id"])
            all_licenses = self.registry.list("licenses")
            check_at = scheduled_at or at
            evaluation = licensing.evaluate(
                all_licenses, lineage, self._required_rights(series, purpose),
                region, medium, purpose, check_at)
            if not evaluation["all_satisfied"]:
                self.registry.record_decision(
                    "screening_denied",
                    {"screening_id": screening_id, "edition_id": edition_id,
                     "scheduled_at": scheduled_at, "missing": evaluation["missing"]},
                    {"snapshot_hash": evaluation["snapshot"]["snapshot_hash"]})
                raise LicenseError("展映所需许可未齐备",
                                   details={"missing": evaluation["missing"]})
            screening = {
                "id": screening_id,
                "edition_id": edition_id,
                "status": "scheduled",
                "region": region,
                "medium": medium,
                "purpose": purpose,
                "scheduled_at": scheduled_at,
                "materials": [
                    {"material_ref": material_ref, "version": 1,
                     "active": True, "set_at": now_iso()},
                ],
                "created_at": now_iso(),
                "evidence": {
                    "snapshot_hash": evaluation["snapshot"]["snapshot_hash"],
                    "license_digests": evaluation["snapshot"]["license_digests"],
                    "snapshot": evaluation["_snapshot_records"],
                },
            }
            self.registry.put("screenings", screening_id, screening)
            self.registry.add_edge(ProvenanceEdge(
                material_ref, screening_id, REL_MATERIAL,
                {"version": 1}, active=True).to_dict())
            self.registry.record_decision(
                "screening_scheduled",
                {"screening_id": screening_id, "edition_id": edition_id,
                 "region": region, "medium": medium, "purpose": purpose,
                 "scheduled_at": scheduled_at, "material_ref": material_ref},
                {"snapshot_hash": screening["evidence"]["snapshot_hash"],
                 "license_digests": screening["evidence"]["license_digests"]})
            return copy.deepcopy(screening)

    def replace_screening_material(self, screening_id, new_material_ref, at=None):
        """为未发生的展映换版素材：旧版本保留并标记失效，决定快照不变。"""
        with self.registry.lock:
            screening = self.registry.get("screenings", screening_id)
            if screening["status"] == "occurred":
                raise ConflictError("展映已经发生，展示素材不可改写",
                                    code="screening_frozen")
            for material in screening["materials"]:
                material["active"] = False
            version = len(screening["materials"]) + 1
            screening["materials"].append({
                "material_ref": new_material_ref, "version": version,
                "active": True, "set_at": at or now_iso()})
            self.registry.add_edge(ProvenanceEdge(
                new_material_ref, screening_id, REL_MATERIAL,
                {"version": version}, active=True).to_dict())
            self.registry.record_decision(
                "screening_material_replaced",
                {"screening_id": screening_id, "material_ref": new_material_ref,
                 "version": version},
                {"snapshot_hash": screening["evidence"]["snapshot_hash"]})
            return copy.deepcopy(screening)

    def mark_screening_occurred(self, screening_id, at=None):
        with self.registry.lock:
            screening = self.registry.get("screenings", screening_id)
            screening["status"] = "occurred"
            screening["occurred_at"] = at or now_iso()
            self.registry.record_decision(
                "screening_occurred",
                {"screening_id": screening_id,
                 "material_ref": next(
                     (m["material_ref"] for m in screening["materials"]
                      if m["active"]), None)},
                {"snapshot_hash": screening["evidence"]["snapshot_hash"]})
            return copy.deepcopy(screening)

    def get_edition(self, edition_id):
        return copy.deepcopy(self.registry.get("editions", edition_id))
