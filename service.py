"""数字艺术授权谱系的运行入口与 HTTP 接口。"""

import argparse
import json
import re
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from catalog import Catalog, DomainError

SERVICE_ID = "digital-art-rights"
SERVICE_NAME = "数字艺术授权谱系"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def _today() -> str:
    return date.today().isoformat()


# ---- 路由处理 ----

def _health(catalog, match, query, body):
    return health_payload(), 200


def _lineage(catalog, match, query, body):
    return catalog.lineage(match.group(1)), 200


def _public_digest(catalog, match, query, body):
    at = query.get("at", [None])[0] or _today()
    return catalog.public_digest(match.group(1), at), 200


def _register_work(catalog, match, query, body):
    return catalog.register_work(**body), 201


def _register_batch(catalog, match, query, body):
    return catalog.register_batch(**body), 201


def _register_asset(catalog, match, query, body):
    return catalog.register_asset(**body), 201


def _register_contribution(catalog, match, query, body):
    return catalog.register_contribution(**body), 201


def _register_license(catalog, match, query, body):
    return catalog.register_license(**body), 201


def _revoke_license(catalog, match, query, body):
    return catalog.revoke_license(match.group(1), **body), 200


def _register_edition(catalog, match, query, body):
    return catalog.register_edition(**body), 201


def _lock_edition(catalog, match, query, body):
    return catalog.lock_edition_quota(match.group(1), **body), 200


def _confirm_payment(catalog, match, query, body):
    return catalog.confirm_payment(**body), 201


def _confirm_chain(catalog, match, query, body):
    return catalog.confirm_chain_callback(**body), 200


def _transfer(catalog, match, query, body):
    return catalog.transfer_collectible(match.group(1), **body), 200


def _replace_display(catalog, match, query, body):
    return catalog.replace_display(match.group(1), **body), 200


def _record_exhibition(catalog, match, query, body):
    return catalog.record_exhibition(**body), 201


def _open_dispute(catalog, match, query, body):
    return catalog.open_dispute(**body), 201


def _close_dispute(catalog, match, query, body):
    return catalog.close_dispute(match.group(1), **body), 200


def _sweep_expiry(catalog, match, query, body):
    return {"expired": catalog.sweep_expiry(**body)}, 200


ROUTES = [
    ("GET", re.compile(r"^/health$"), _health),
    ("GET", re.compile(r"^/lineage/([^/]+)$"), _lineage),
    ("GET", re.compile(r"^/public/digest/([^/]+)$"), _public_digest),
    ("POST", re.compile(r"^/works$"), _register_work),
    ("POST", re.compile(r"^/batches$"), _register_batch),
    ("POST", re.compile(r"^/assets$"), _register_asset),
    ("POST", re.compile(r"^/contributions$"), _register_contribution),
    ("POST", re.compile(r"^/licenses$"), _register_license),
    ("POST", re.compile(r"^/licenses/([^/]+)/revoke$"), _revoke_license),
    ("POST", re.compile(r"^/editions$"), _register_edition),
    ("POST", re.compile(r"^/editions/([^/]+)/lock$"), _lock_edition),
    ("POST", re.compile(r"^/payments/confirm$"), _confirm_payment),
    ("POST", re.compile(r"^/chain/callbacks$"), _confirm_chain),
    ("POST", re.compile(r"^/collectibles/([^/]+)/transfer$"), _transfer),
    ("POST", re.compile(r"^/assets/([^/]+)/display$"), _replace_display),
    ("POST", re.compile(r"^/exhibitions$"), _record_exhibition),
    ("POST", re.compile(r"^/disputes$"), _open_dispute),
    ("POST", re.compile(r"^/disputes/([^/]+)/close$"), _close_dispute),
    ("POST", re.compile(r"^/maintenance/expiry-sweep$"), _sweep_expiry),
]


class Handler(BaseHTTPRequestHandler):
    """健康检查与领域接口的统一入口，catalog 可在测试时替换。"""

    catalog = Catalog()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        for route_method, pattern, func in ROUTES:
            if route_method != method:
                continue
            match = pattern.match(parsed.path)
            if not match:
                continue
            body = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    self._send(400, {"error": "请求体不是合法 JSON"})
                    return
            try:
                payload, status = func(self.catalog, match, query, body)
            except DomainError as exc:
                self._send(exc.status, {"error": str(exc)})
                return
            except TypeError as exc:
                self._send(400, {"error": f"请求参数不完整: {exc}"})
                return
            self._send(status, payload)
            return
        self.send_error(404)

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        assert Catalog().ledger.events == []
        print("基础检查通过")
        return
    Handler.catalog = Catalog()
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
