"""数字艺术授权谱系的运行入口与 HTTP 接口。

路由一览（除 /health 与 GET 外均为 POST JSON）：
  GET  /health                                   服务身份
  GET  /api/series                               系列与名额占用
  POST /api/evaluate                             试判许可是否齐备（不占名额）
  POST /api/reservations                         许可齐备才锁定名额
  POST /api/reservations/{id}/confirm            凭幂等键确认锁位发行
  POST /api/editions/issue                       原子发行（幂等键去重）
  POST /api/editions/{id}/transfer               藏品让渡（只追加）
  POST /api/screenings                           安排展映（固化授权快照）
  POST /api/screenings/{id}/material             展映素材换版（发生后拒绝）
  POST /api/screenings/{id}/occur                标记展映发生
  GET  /api/editions/{id}/lineage                馆方完整谱系反查
  GET  /api/editions/{id}/summary                对外真伪与权利状态摘要
  GET  /api/screenings/{id}/summary              对外展映状态摘要
  GET  /api/decisions?about=<编号或节点>          授权决定链
  GET  /api/chain                                哈希链校验
"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from registry import Registry
from application import ArtRightsService
from provenance import ProvenanceService
from errors import (
    DomainError, NotFoundError, ValidationError,
    LicenseError, EditionError, ConflictError,
)
import scenario as demo_scenario

SERVICE_ID = "digital-art-rights"
SERVICE_NAME = "数字艺术授权谱系"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_services(registry=None, seed_demo=False):
    if seed_demo:
        return demo_scenario.build()
    registry = registry or Registry()
    return registry, ArtRightsService(registry), ProvenanceService(registry)


def create_handler(services):
    registry, svc, prov = services

    class Handler(BaseHTTPRequestHandler):
        """业务接口处理器，服务实例由工厂注入。"""

        def log_message(self, *_args):
            return

        # --------------------------------------------------------------
        # 基础收发
        # --------------------------------------------------------------

        def _send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                raise ValidationError("请求体不是合法 JSON")
            if not isinstance(data, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            return data

        @staticmethod
        def _need(body, key):
            if key not in body or body[key] in (None, ""):
                raise ValidationError(f"缺少必填字段 {key}")
            return body[key]

        def _handle_errors(self, fn):
            try:
                fn()
            except (NotFoundError,) as exc:
                self._send_json(404, {"error": {"code": exc.code, "message": str(exc),
                                                "details": exc.details}})
            except ValidationError as exc:
                self._send_json(400, {"error": {"code": exc.code, "message": str(exc),
                                                "details": exc.details}})
            except LicenseError as exc:
                self._send_json(422, {"error": {"code": exc.code, "message": str(exc),
                                                "details": exc.details}})
            except (EditionError, ConflictError) as exc:
                self._send_json(409, {"error": {"code": exc.code, "message": str(exc),
                                                "details": exc.details}})
            except DomainError as exc:
                self._send_json(400, {"error": {"code": exc.code, "message": str(exc),
                                                "details": exc.details}})

        # --------------------------------------------------------------
        # GET 路由
        # --------------------------------------------------------------

        def do_GET(self):
            self._handle_errors(lambda: self._route_get())

        def _route_get(self):
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            if path == "/health":
                self._send_json(200, health_payload())
                return
            if path == "/api/series":
                self._send_json(200, {"series": [
                    {**s, "issued": registry.issued_count(s["id"]),
                     "remaining": (None if s["edition_size"] is None
                                   else s["edition_size"] - registry.issued_count(s["id"]))}
                    for s in registry.list("series")]})
                return
            if path == "/api/chain":
                self._send_json(200, {"valid": registry.verify_chain(),
                                      "length": len(registry.log),
                                      "head": registry.log[-1]["entry_hash"]
                                      if registry.log else None})
                return

            match = re.fullmatch(r"/api/decisions", path)
            if match and "about" in query:
                self._send_json(200, {"decisions": registry.decisions_about(
                    query["about"][0])})
                return

            match = re.fullmatch(r"/api/editions/([^/]+)/lineage", path)
            if match:
                self._send_json(200, prov.full_lineage(match.group(1)))
                return
            match = re.fullmatch(r"/api/editions/([^/]+)/summary", path)
            if match:
                self._send_json(200, prov.public_summary(
                    match.group(1),
                    query.get("region", ["CN"])[0],
                    query.get("medium", ["digital"])[0],
                    query.get("purpose", ["distribution"])[0]))
                return
            match = re.fullmatch(r"/api/screenings/([^/]+)/summary", path)
            if match:
                self._send_json(200, prov.public_screening_status(match.group(1)))
                return

            self.send_error(404)

        # --------------------------------------------------------------
        # POST 路由
        # --------------------------------------------------------------

        def do_POST(self):
            self._handle_errors(lambda: self._route_post())

        def _route_post(self):
            path = urlparse(self.path).path
            body = self._read_body()

            if path == "/api/evaluate":
                result = svc.evaluate_series(
                    self._need(body, "series_id"),
                    self._need(body, "region"), self._need(body, "medium"),
                    self._need(body, "purpose"), body.get("at"))
                result.pop("_snapshot_records", None)
                self._send_json(200, result)
                return
            if path == "/api/reservations":
                reservation = svc.reserve_slot(
                    self._need(body, "series_id"),
                    self._need(body, "region"), self._need(body, "medium"),
                    self._need(body, "purpose"), body.get("reservation_id"),
                    body.get("at"))
                self._send_json(201, {k: v for k, v in reservation.items()
                                      if k != "evidence"})
                return
            if path == "/api/editions/issue":
                edition, replayed = svc.issue(
                    self._need(body, "series_id"),
                    self._need(body, "recipient"),
                    self._need(body, "idempotency_key"),
                    self._need(body, "region"), self._need(body, "medium"),
                    self._need(body, "purpose"), body.get("at"))
                self._send_json(201 if not replayed else 200,
                                {"edition": _public_edition(edition),
                                 "replayed": replayed})
                return

            match = re.fullmatch(r"/api/reservations/([^/]+)/confirm", path)
            if match:
                edition, replayed = svc.confirm_reservation(
                    match.group(1), self._need(body, "recipient"),
                    self._need(body, "idempotency_key"), body.get("at"))
                self._send_json(201 if not replayed else 200,
                                {"edition": _public_edition(edition),
                                 "replayed": replayed})
                return
            match = re.fullmatch(r"/api/editions/([^/]+)/transfer", path)
            if match:
                edition = svc.transfer_edition(
                    match.group(1), self._need(body, "to_recipient"),
                    body.get("at"))
                self._send_json(200, {"edition": _public_edition(edition)})
                return
            if path == "/api/screenings":
                screening = svc.schedule_screening(
                    self._need(body, "screening_id"),
                    self._need(body, "edition_id"),
                    self._need(body, "region"), self._need(body, "medium"),
                    self._need(body, "purpose"),
                    self._need(body, "scheduled_at"),
                    self._need(body, "material_ref"), body.get("at"))
                self._send_json(201, _public_screening(screening))
                return
            match = re.fullmatch(r"/api/screenings/([^/]+)/material", path)
            if match:
                screening = svc.replace_screening_material(
                    match.group(1), self._need(body, "material_ref"),
                    body.get("at"))
                self._send_json(200, _public_screening(screening))
                return
            match = re.fullmatch(r"/api/screenings/([^/]+)/occur", path)
            if match:
                screening = svc.mark_screening_occurred(
                    match.group(1), body.get("at"))
                self._send_json(200, _public_screening(screening))
                return

            self.send_error(404)

    return Handler


def _public_edition(edition):
    data = {k: v for k, v in edition.items() if k != "evidence"}
    data["evidence_hash"] = edition["evidence"]["snapshot_hash"]
    return data


def _public_screening(screening):
    return {k: v for k, v in screening.items() if k != "evidence"}


# 模块级默认处理器，供契约测试与本地联调直接导入
Handler = create_handler(build_services())


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--demo", action="store_true",
                        help="启动时播种《草书诗帖》演示场景")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        registry, _, _ = build_services(seed_demo=True)
        assert registry.verify_chain()
        print("基础检查通过")
        return
    handler = create_handler(build_services(seed_demo=args.demo))
    ThreadingHTTPServer(("0.0.0.0", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
