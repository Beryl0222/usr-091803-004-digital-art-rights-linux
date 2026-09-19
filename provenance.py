"""谱系反查与对外摘要。

馆方视角：由藏品编号反查完整谱系（原作→采集批次→局部裁切→数字作品→系列→
该编号藏品）、各方贡献与每一次授权/发行/展映决定。
公众视角：验证真伪与当前权利状态，只暴露散列证据与结论，不暴露合同编号、
许可备注等内部合同内容。
"""

import copy

from domain import RIGHT_LABELS, digest
import licensing


class ProvenanceService:
    def __init__(self, registry):
        self.registry = registry

    # ------------------------------------------------------------------
    # 馆方完整反查
    # ------------------------------------------------------------------

    def _lineage_fallback(self, series_id):
        ancestors = self.registry.lineage_of(series_id)
        return [series_id] + list(reversed(ancestors))

    def full_lineage(self, edition_id):
        edition = self.registry.get("editions", edition_id)
        series = self.registry.get("series", edition["series_id"])

        node_ids = edition.get("lineage") or self._lineage_fallback(series["id"])
        # lineage 含 series 虚拟起点，拆出真正的谱系节点
        party_index = {p["id"]: p for p in self.registry.list("parties")}
        all_contributions = self.registry.list("contributions")
        nodes, edges, real_node_ids = [], [], []
        for nid in node_ids:
            node = self.registry.find("nodes", nid)
            if node is None:
                continue
            real_node_ids.append(nid)
            contributions = []
            for c in all_contributions:
                if c["node_id"] == nid:
                    item = copy.deepcopy(c)
                    party = party_index.get(c["party_id"])
                    item["party_name"] = party["name"] if party else None
                    contributions.append(item)
            nodes.append({"node": copy.deepcopy(node),
                          "contributions": contributions})
        # 呈现顺序：原作 → 采集 → 裁切 → 数字作品
        nodes.reverse()

        for edge in self.registry.edges:
            if edge["child_id"] in node_ids or edge["parent_id"] in node_ids:
                edges.append(copy.deepcopy(edge))

        screenings = [s for s in self.registry.list("screenings")
                      if s["edition_id"] == edition_id]
        decisions = self._collect_decisions(
            edition_id, series, real_node_ids, screenings)
        decisions.sort(key=lambda e: e["seq"])

        return {
            "edition": copy.deepcopy(edition),
            "series": copy.deepcopy(series),
            "lineage": nodes,
            "edges": edges,
            "screenings": copy.deepcopy(screenings),
            "decisions": decisions,
            "chain_valid": self.registry.verify_chain(),
            "chain_head": self.registry.log[-1]["entry_hash"] if self.registry.log else None,
        }

    def _collect_decisions(self, edition_id, series, node_ids, screenings):
        """汇聚与该藏品相关的全部授权决定：其编号、系列、谱系节点、
        覆盖该谱系的每份许可以及它的展映。馆方据此复核每一次决定。"""
        node_set = set(node_ids)
        license_ids = []
        for lic in self.registry.list("licenses"):
            if not lic["node_ids"] or node_set & set(lic["node_ids"]):
                license_ids.append(lic["id"])
        targets = ([edition_id, series["id"]] + node_ids + license_ids
                   + [s["id"] for s in screenings])
        return self.registry.find_decisions(targets)

    # ------------------------------------------------------------------
    # 对外真伪与权利状态摘要
    # ------------------------------------------------------------------

    def public_summary(self, edition_id, region, medium, purpose, at=None):
        edition = self.registry.get("editions", edition_id)
        series = self.registry.get("series", edition["series_id"])

        node_ids = edition.get("lineage") or self._lineage_fallback(series["id"])
        lineage_brief = []
        for nid in reversed(node_ids):
            node = self.registry.find("nodes", nid)
            if node is not None:
                lineage_brief.append({"node_id": node["id"], "kind": node["kind"],
                                      "title": node["title"]})

        # 当前权利状态：实时重算，但只输出结论，不输出合同细节
        required = (series["required_rights"]
                    or licensing.RIGHTS_BY_PURPOSE.get(purpose))
        current = licensing.evaluate(
            self.registry.list("licenses"), node_ids,
            required, region, medium, purpose, at)
        rights_state = {}
        for right, info in current["rights"].items():
            rights_state[right] = {
                "label": RIGHT_LABELS.get(right, right),
                "satisfied": info["satisfied"],
                "state": info["status"] if info["satisfied"] else (info["reason"] or "absent"),
            }

        proof_basis = {
            "edition_id": edition["id"],
            "series_id": series["id"],
            "serial": edition["serial"],
            "issued_at": edition["issued_at"],
            "lineage": lineage_brief,
        }
        return {
            "authentic": True,
            "edition_number": edition["edition_number"],
            "series_title": series["title"],
            "series_kind": series["kind"],
            "edition_size": series["edition_size"],
            "status": edition["status"],
            "lineage": lineage_brief,
            "rights": {
                "request": current["request"],
                "all_satisfied": current["all_satisfied"],
                "items": rights_state,
            },
            "proof": {
                # 首发时固化的证据（发行后许可变化不影响该散列）
                "issuance_snapshot_hash": edition["evidence"]["snapshot_hash"],
                "issuance_license_digests": edition["evidence"]["license_digests"],
                "current_snapshot_hash": current["snapshot"]["snapshot_hash"],
                "record_digest": digest(proof_basis),
                "chain_head": self.registry.log[-1]["entry_hash"] if self.registry.log else None,
                "chain_valid": self.registry.verify_chain(),
            },
        }

    def public_screening_status(self, screening_id):
        """展映真伪与"当时使用的素材版本"证明，同样不含合同内容。"""
        screening = self.registry.get("screenings", screening_id)
        return {
            "authentic": True,
            "screening_id": screening["id"],
            "edition_id": screening["edition_id"],
            "status": screening["status"],
            "scheduled_at": screening["scheduled_at"],
            "occurred_at": screening.get("occurred_at"),
            "materials": [
                {"version": m["version"], "material_ref": m["material_ref"],
                 "active": m["active"], "set_at": m["set_at"]}
                for m in screening["materials"]
            ],
            "frozen": screening["status"] == "occurred",
            "proof": {
                "authorization_snapshot_hash": screening["evidence"]["snapshot_hash"],
                "license_digests": screening["evidence"]["license_digests"],
                "chain_head": self.registry.log[-1]["entry_hash"] if self.registry.log else None,
                "chain_valid": self.registry.verify_chain(),
            },
        }
