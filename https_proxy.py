"""
HTTPS reverse proxy — run this alongside app.py.
Listens on :8443 (HTTPS), forwards everything to :8000 (HTTP Flask).

Usage:
    python app.py &
    python https_proxy.py
Then open https://10.71.225.112:8443 on your phone.
Accept the security warning once, then mic works.
"""
import http.server
import http.client
import ssl
import threading
import urllib.request

FLASK_HOST = "127.0.0.1"
FLASK_PORT = 8000
PROXY_PORT = 8443
CERT = "cert.pem"
KEY  = "key.pem"


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_REQUEST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None
        conn = http.client.HTTPConnection(FLASK_HOST, FLASK_PORT, timeout=30)
        conn.request(self.command, self.path, body=body, headers=dict(self.headers))
        resp = conn.getresponse()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in ("transfer-encoding",):
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(resp.read())
        conn.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_REQUEST

    def log_message(self, fmt, *args):
        pass  # silence access log


ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(CERT, KEY)

server = http.server.HTTPServer(("0.0.0.0", PROXY_PORT), ProxyHandler)
server.socket = ctx.wrap_socket(server.socket, server_side=True)

print(f"HTTPS proxy → https://0.0.0.0:{PROXY_PORT}  (forwarding to HTTP :{FLASK_PORT})")
print(f"Open on phone: https://10.71.225.112:{PROXY_PORT}")
server.serve_forever()
