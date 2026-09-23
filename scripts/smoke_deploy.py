#!/usr/bin/env python3
"""End to end smoke gate for a built image of the office coffee app.

Runs the exact image that is about to be deployed in a throwaway container with
an ephemeral database, walks the real user journey against it over HTTP, and
exits non-zero on the first failure.

It answers a question the unit tests cannot answer: does the image actually work
when a person walks through it, including the parts where the Python server and
the JavaScript client have to agree on a JSON field name. Every field name the
client reads is extracted from the client source at run time and never
hardcoded here, so a rename on either side fails this gate instead of failing a
colleague in production.

It NEVER touches production: the container is created here, given a freshly
generated VAPID keypair, an explicit environment (no ambient CAFE_* variables
are inherited), an ephemeral volume, and a push endpoint that points at a local
stand in server. It is removed on every exit path.

Usage:
    python scripts/smoke_deploy.py registry.ktek-group.pt/cafe/cafe-escritorio:0.7.0
    python scripts/smoke_deploy.py sha256:<digest> --allow-skips

Requires: Docker, and the packages already pulled in by requirements.txt
(pywebpush, which brings http-ece, cryptography and requests).
"""
from __future__ import annotations

import argparse
import atexit
import base64
import json
import os
import queue
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import http_ece
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "app" / "static" / "app.js"
SW_JS = ROOT / "app" / "static" / "sw.js"
INDEX_HTML = ROOT / "app" / "static" / "index.html"

CONTAINER_PORT = 8000
STARTUP_TIMEOUT = 90.0
PUSH_TIMEOUT = 45.0
HTTP_TIMEOUT = 15.0

# Docker Desktop resolves this name to the host from inside a container. The
# --add-host flag below makes it work on plain Linux Docker too.
PUSH_HOST = os.environ.get("CAFE_SMOKE_PUSH_HOST", "host.docker.internal")


class SmokeFailure(Exception):
    """A check that ran and gave the wrong answer."""


class SmokeSkip(Exception):
    """A check that could not be run honestly. Never silently swallowed."""


# ---------------------------------------------------------------- reporting

@dataclass
class Report:
    """Accumulates one line per step so a human can read what was checked.

    An exit code is not evidence. Every step prints what it verified, and the
    closing summary names anything that was not verified at all.
    """

    results: list[tuple[str, str, str]] = field(default_factory=list)
    _step: str = ""

    def start(self, name: str) -> None:
        self._step = name
        print(f"\n== {name}")
        sys.stdout.flush()

    def detail(self, text: str) -> None:
        print(f"   {text}")
        sys.stdout.flush()

    def passed(self, text: str) -> None:
        self.results.append((self._step, "PASS", text))
        print(f"   PASS: {text}")
        sys.stdout.flush()

    def failed(self, text: str) -> None:
        self.results.append((self._step or "Setup", "FAIL", text))
        print(f"   FAIL: {text}")
        sys.stdout.flush()

    def skipped(self, text: str) -> None:
        self.results.append((self._step or "Setup", "SKIPPED", text))
        print(f"   SKIPPED: {text}")
        sys.stdout.flush()

    def not_run(self, name: str) -> None:
        self.results.append((name, "NOT RUN", "aborted after an earlier failure"))

    def summary(self, allow_skips: bool) -> int:
        fails = [r for r in self.results if r[1] == "FAIL"]
        skips = [r for r in self.results if r[1] == "SKIPPED"]
        not_run = [r for r in self.results if r[1] == "NOT RUN"]
        print("\n" + "=" * 72)
        print("SUMMARY")
        for step, status, text in self.results:
            print(f"  [{status:<7}] {step}: {text}")
        print("=" * 72)
        if fails:
            print(f"RESULT: FAILED. {len(fails)} check(s) gave the wrong answer.")
            for step, _, text in fails:
                print(f"  - {step}: {text}")
            if not_run:
                print(f"  {len(not_run)} later step(s) never ran, so they are UNVERIFIED.")
            return 1
        if skips:
            print(f"RESULT: {len(skips)} check(s) were NOT VERIFIED:")
            for step, _, text in skips:
                print(f"  - {step}: {text}")
            if allow_skips:
                print("RESULT: passing anyway because --allow-skips was given.")
                print("        The lines above are what this run did not prove.")
                return 0
            print("RESULT: FAILED. A smoke test that drops its hardest check turns")
            print("        'unverified' into a green tick. Pass --allow-skips only if")
            print("        you have read the lines above and accept them.")
            return 1
        print("RESULT: PASSED. Every step ran and gave the right answer.")
        return 0


# ---------------------------------------------------------------- docker

def run_docker(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if check and proc.returncode != 0:
        raise SmokeFailure(
            f"docker {' '.join(args)} failed with exit {proc.returncode}\n"
            f"stdout: {proc.stdout.strip()}\nstderr: {proc.stderr.strip()}"
        )
    return proc


class Container:
    """A throwaway container, removed on every exit path including Ctrl-C."""

    def __init__(self, image: str, env: dict[str, str]):
        self.image = image
        self.env = env
        self.name = f"cafe-smoke-{secrets.token_hex(4)}"
        self.base_url = ""
        self._removed = False

    def start(self, rep: Report) -> None:
        args = [
            "run", "-d",
            "--name", self.name,
            # Ephemeral database: a tmpfs owned by the image's uid 1000 user, so
            # nothing survives the run and no host path is ever written.
            "--tmpfs", "/data:uid=1000,gid=1000,mode=0700",
            # Let the OS pick the host port: other sessions are live on this machine.
            "-p", f"127.0.0.1::{CONTAINER_PORT}",
            "--add-host", "host.docker.internal:host-gateway",
        ]
        for key, value in self.env.items():
            args += ["-e", f"{key}={value}"]
        args.append(self.image)
        run_docker(args)
        port = run_docker(["port", self.name, str(CONTAINER_PORT)]).stdout.strip().splitlines()[0]
        self.base_url = f"http://127.0.0.1:{port.rsplit(':', 1)[1]}"
        rep.detail(f"container {self.name} from {self.image} at {self.base_url}")

    def logs(self) -> str:
        if self._removed:
            return "(container already removed)"
        proc = run_docker(["logs", "--tail", "60", self.name], check=False)
        return (proc.stdout + proc.stderr).strip() or "(no output)"

    def remove(self) -> None:
        if self._removed:
            return
        self._removed = True
        # -v also drops the anonymous volume, so nothing is left behind.
        run_docker(["rm", "-f", "-v", self.name], check=False)


# ---------------------------------------------------------------- push stand in

class PushRequest:
    def __init__(self, path: str, headers, body: bytes):
        self.path = path
        # HTTP header names are case insensitive and pywebpush sends them in
        # lower case, so look them up that way rather than by the spelling the
        # RFC uses in prose.
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


class _PushHandler(BaseHTTPRequestHandler):
    inbox: "queue.Queue[PushRequest]" = queue.Queue()

    def do_POST(self):  # noqa: N802 (name fixed by BaseHTTPRequestHandler)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        _PushHandler.inbox.put(PushRequest(self.path, self.headers, body))
        self.send_response(201)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):
        pass  # the smoke report is the log


class PushStandIn:
    """Stands in for the real push service. Bound on 0.0.0.0 so the container
    can reach it; the port is ephemeral so parallel sessions do not collide."""

    def __init__(self):
        self.server = ThreadingHTTPServer(("0.0.0.0", 0), _PushHandler)
        self.port = self.server.server_address[1]
        self.token = secrets.token_urlsafe(8)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._closed = False

    @property
    def endpoint(self) -> str:
        return f"http://{PUSH_HOST}:{self.port}/push/{self.token}"

    def start(self) -> None:
        self._thread.start()

    def wait(self, timeout: float) -> PushRequest:
        return _PushHandler.inbox.get(timeout=timeout)

    def drain(self) -> list[PushRequest]:
        extra = []
        while True:
            try:
                extra.append(_PushHandler.inbox.get_nowait())
            except queue.Empty:
                return extra

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------- crypto

def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def new_vapid_keys() -> tuple[str, str]:
    """A fresh server keypair per run, in the exact encoding app/main.py expects:
    the raw 32 byte scalar and the uncompressed X9.62 point, both base64url."""
    key = ec.generate_private_key(ec.SECP256R1())
    private_raw = key.private_numbers().private_value.to_bytes(32, "big")
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return b64u(private_raw), b64u(public_raw)


class BrowserSubscription:
    """The receiving half of a Web Push subscription, derived exactly the way a
    browser derives it: a P-256 keypair whose public point is p256dh, plus 16
    random bytes of auth secret."""

    def __init__(self):
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.auth_secret = os.urandom(16)
        self.p256dh = b64u(
            self.private_key.public_key().public_bytes(
                serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
            )
        )
        self.auth = b64u(self.auth_secret)

    def decrypt(self, body: bytes) -> bytes:
        return http_ece.decrypt(
            body, private_key=self.private_key, auth_secret=self.auth_secret, version="aes128gcm"
        )


# ---------------------------------------------------------------- source contracts

def client_vapid_field() -> str:
    """The JSON field app.js reads off GET /api/push/chave. Extracted, never
    hardcoded: hardcoding it here would make this gate agree with itself."""
    source = APP_JS.read_text(encoding="utf-8")
    match = re.search(r"chaveVapid\s*=\s*dados\.(\w+)", source)
    if not match:
        raise SmokeFailure(
            f"could not find `chaveVapid = dados.<field>` in {APP_JS}. Either the client "
            "stopped reading the key that way or this check has gone blind, and a check "
            "that cannot find what it looks for must not report success."
        )
    return match.group(1)


def sw_push_fields() -> list[str]:
    """Every `dados.<field>` the service worker's push handler reads. The slice
    is bounded to the push listener so the other listeners cannot contribute."""
    source = SW_JS.read_text(encoding="utf-8")
    start = source.find('addEventListener("push"')
    if start < 0:
        raise SmokeFailure(
            f"could not find the push listener in {SW_JS}. Without it this check has no "
            "contract to verify and must not report success."
        )
    end = source.find("addEventListener(", start + 1)
    block = source[start:end if end > 0 else len(source)]
    fields = sorted(set(re.findall(r"\bdados\.(\w+)", block)))
    if not fields:
        raise SmokeFailure(
            f"found the push listener in {SW_JS} but no `dados.<field>` reads inside it. "
            "A contract of zero fields proves nothing, so this is a failure, not a pass."
        )
    return fields


def client_pref_events() -> list[str]:
    """The toggles app.js sends in the preferences payload, read off the form."""
    source = INDEX_HTML.read_text(encoding="utf-8")
    events = re.findall(r'data-evento="([\w-]+)"', source)
    if not events:
        raise SmokeFailure(
            f"could not find any data-evento toggle in {INDEX_HTML}, so the payload shape "
            "app.js builds is unknown and cannot be checked."
        )
    seen: list[str] = []
    for event in events:
        if event not in seen:
            seen.append(event)
    return seen


# ---------------------------------------------------------------- the steps

def step_1_starts(rep: Report, box: Container) -> None:
    rep.start("Step 1: container starts and / answers 200")
    deadline = time.monotonic() + STARTUP_TIMEOUT
    last = ""
    while time.monotonic() < deadline:
        state = run_docker(
            ["inspect", "-f", "{{.State.Status}}", box.name], check=False
        ).stdout.strip()
        if state and state not in ("running", "created"):
            raise SmokeFailure(
                f"container is {state!r} instead of running.\n--- docker logs ---\n{box.logs()}"
            )
        try:
            resp = requests.get(box.base_url + "/", timeout=HTTP_TIMEOUT)
            if resp.status_code == 200:
                waited = STARTUP_TIMEOUT - (deadline - time.monotonic())
                rep.passed(
                    f"GET / answered 200 with {len(resp.content)} bytes after {waited:.1f}s"
                )
                return
            last = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last = type(exc).__name__
        time.sleep(0.5)
    raise SmokeFailure(
        f"GET / never answered 200 within {STARTUP_TIMEOUT:.0f}s (last: {last}).\n"
        f"--- docker logs ---\n{box.logs()}"
    )


def _line_endings(raw: bytes) -> str:
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    if crlf and lf:
        return f"mixed ({crlf} CRLF, {lf} LF)"
    return "CRLF" if crlf else "LF"


def _asset_mismatch(url_path: str, local: Path, served: bytes, on_disk: bytes) -> str:
    """Both causes of a mismatch are real failures, but they are different
    failures and the fix is different, so say which one this is. Calling a line
    ending artifact "the image was built from a different tree" is a false
    accusation, and a gate that cries wolf is a gate somebody switches off."""
    head = (
        f"{url_path} served by the image differs from {local}: "
        f"{len(served)} bytes served ({_line_endings(served)}) vs "
        f"{len(on_disk)} bytes on disk ({_line_endings(on_disk)}). "
    )
    if served.replace(b"\r\n", b"\n") == on_disk.replace(b"\r\n", b"\n"):
        return head + (
            "The text is identical and only the line endings differ. The image was built from a "
            "Windows checkout that git had not yet renormalized (.gitattributes asks for eol=lf). "
            "This is still a failure: the image does not carry the bytes this tree would deploy. "
            "Rebuild the image from a renormalized checkout rather than relaxing this check, "
            "because relaxing it also blinds the check to a genuinely different tree."
        )
    return head + (
        "The content itself differs, not just the line endings. The image was built from a "
        "different tree than the one being tested, so nothing else this script checks is about "
        "the code you are looking at."
    )


def step_2_assets(rep: Report, box: Container) -> None:
    rep.start("Step 2: served sw.js and app.js are byte identical to the working tree")
    for url_path, local in (("/sw.js", SW_JS), ("/static/app.js", APP_JS)):
        resp = requests.get(box.base_url + url_path, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            raise SmokeFailure(f"GET {url_path} answered {resp.status_code}, expected 200")
        served, on_disk = resp.content, local.read_bytes()
        if served != on_disk:
            raise SmokeFailure(_asset_mismatch(url_path, local, served, on_disk))
        rep.passed(f"{url_path} matches {local.relative_to(ROOT)} exactly ({len(served)} bytes)")


def step_3_journey(rep: Report, box: Container) -> requests.Session:
    rep.start("Step 3: register, log in, drink a coffee, and see the count actually move")
    session = requests.Session()
    name = f"Smoke {secrets.token_hex(3)}"
    pin = "".join(secrets.choice("0123456789") for _ in range(4))

    resp = session.post(
        box.base_url + "/api/registar",
        json={"nome": name, "pin": pin, "cafes_dia": 2},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 201:
        raise SmokeFailure(f"POST /api/registar answered {resp.status_code}: {resp.text[:300]}")
    user_id = resp.json()["id"]
    rep.detail(f"registered {name!r} as id {user_id}")

    session.post(box.base_url + "/api/logout", timeout=HTTP_TIMEOUT)
    resp = session.post(
        box.base_url + "/api/login",
        json={"utilizador_id": user_id, "pin": pin},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 200:
        raise SmokeFailure(f"POST /api/login answered {resp.status_code}: {resp.text[:300]}")
    rep.detail("logged in with a fresh session cookie")

    before = session.get(box.base_url + "/api/eu", timeout=HTTP_TIMEOUT)
    if before.status_code != 200:
        raise SmokeFailure(f"GET /api/eu answered {before.status_code}: {before.text[:300]}")
    count_before = before.json()["cafes"]

    resp = session.post(box.base_url + "/api/cafe", timeout=HTTP_TIMEOUT)
    if resp.status_code != 201:
        raise SmokeFailure(f"POST /api/cafe answered {resp.status_code}: {resp.text[:300]}")

    after = session.get(box.base_url + "/api/eu", timeout=HTTP_TIMEOUT)
    if after.status_code != 200:
        raise SmokeFailure(f"GET /api/eu answered {after.status_code}: {after.text[:300]}")
    payload = after.json()
    count_after = payload["cafes"]
    expected = 1
    if count_after != expected:
        raise SmokeFailure(
            f"the coffee count did not move: GET /api/eu reports cafes={count_after!r} after one "
            f"POST /api/cafe, expected {expected}. The write returned 201 but had no effect, "
            "which is exactly what asserting on a status code would have missed."
        )
    rep.passed(
        f"cafes went {count_before} -> {count_after} and valor_cent is "
        f"{payload['valor_cent']} at {payload['preco_cent']} cent each"
    )
    return session


def step_4_vapid(rep: Report, box: Container, public_key: str) -> None:
    rep.start("Step 4: GET /api/push/chave carries the field app.js actually reads")
    field_name = client_vapid_field()
    rep.detail(f"app.js reads dados.{field_name} (extracted from {APP_JS.relative_to(ROOT)})")

    resp = requests.get(box.base_url + "/api/push/chave", timeout=HTTP_TIMEOUT)
    if resp.status_code != 200:
        raise SmokeFailure(
            f"GET /api/push/chave answered {resp.status_code}: {resp.text[:300]}. With the VAPID "
            "variables set in the container this must be 200."
        )
    body = resp.json()
    if field_name not in body:
        raise SmokeFailure(
            f"the client reads dados.{field_name} but the response has no such field. "
            f"It carries {sorted(body)}. This is the server and the client disagreeing on a "
            "name, which no single sided test can see."
        )
    value = body[field_name]
    if not isinstance(value, str) or not value.strip():
        raise SmokeFailure(
            f"field {field_name!r} is present but empty ({value!r}). The client would take it "
            "and fail at pushManager.subscribe with an unreadable error."
        )
    if value != public_key:
        raise SmokeFailure(
            f"field {field_name!r} is {value[:16]}... but this run started the container with "
            f"{public_key[:16]}..., so the server is not serving the key it was given."
        )
    rep.passed(f"{field_name} = {value[:20]}... ({len(value)} chars) and matches the injected key")


def step_5_preferences(rep: Report, box: Container, session: requests.Session) -> None:
    rep.start("Step 5: notification preferences round trip in the shape app.js builds")
    events = client_pref_events()
    rep.detail(f"app.js sends one key per toggle: {events}")

    # app.js always sends the full form state, so this is the real payload shape.
    # "compra" is flipped rather than "cafe" because step 6 needs "cafe" on.
    flipped = "compra" if "compra" in events else events[0]
    sent = {event: event != flipped for event in events}
    resp = session.put(
        box.base_url + "/api/notificacoes/preferencias", json=sent, timeout=HTTP_TIMEOUT
    )
    if resp.status_code != 200:
        raise SmokeFailure(
            f"PUT /api/notificacoes/preferencias answered {resp.status_code}: {resp.text[:300]}"
        )

    resp = session.get(box.base_url + "/api/notificacoes/preferencias", timeout=HTTP_TIMEOUT)
    if resp.status_code != 200:
        raise SmokeFailure(
            f"GET /api/notificacoes/preferencias answered {resp.status_code}: {resp.text[:300]}"
        )
    got = resp.json()
    missing = [event for event in events if event not in got]
    if missing:
        raise SmokeFailure(
            f"the client has toggles for {missing} but the server does not report them, so those "
            "switches would silently do nothing."
        )
    wrong = {e: (sent[e], got[e]) for e in events if got[e] != sent[e]}
    if wrong:
        raise SmokeFailure(
            f"preferences did not persist. sent vs read back: {wrong}. The PUT answered 200 and "
            "changed nothing."
        )
    rep.passed(f"{flipped!r} turned off and read back off; the other {len(events) - 1} stayed on")

    # Put everything back on so step 6 does not depend on which toggle was flipped here.
    session.put(
        box.base_url + "/api/notificacoes/preferencias",
        json={event: True for event in events},
        timeout=HTTP_TIMEOUT,
    )
    rep.detail("all toggles restored to on before the push step")


def step_6_push(rep: Report, box: Container, subscriber: requests.Session, push: PushStandIn) -> None:
    rep.start("Step 6: a real push arrives, decrypts, and carries non-empty values")
    required = sw_push_fields()
    rep.detail(f"sw.js push handler reads {required} (extracted from {SW_JS.relative_to(ROOT)})")

    # The app never notifies the author of an event, so the person who drinks the
    # coffee has to be someone other than the subscriber. Register the second
    # person BEFORE subscribing: registration itself fires a "registo" push, and
    # a subscription that already exists would receive it and muddy the inbox.
    other = requests.Session()
    other_name = f"Smoke {secrets.token_hex(3)}"
    other_pin = "".join(secrets.choice("0123456789") for _ in range(4))
    resp = other.post(
        box.base_url + "/api/registar",
        json={"nome": other_name, "pin": other_pin, "cafes_dia": 1},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 201:
        raise SmokeFailure(f"POST /api/registar (second user) answered {resp.status_code}")
    rep.detail(f"second person {other_name!r} registered (nobody is subscribed yet)")

    browser = BrowserSubscription()
    resp = subscriber.post(
        box.base_url + "/api/push/subscricoes",
        json={
            "endpoint": push.endpoint,
            "p256dh": browser.p256dh,
            "auth": browser.auth,
            "dispositivo": "smoke_deploy.py",
        },
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 201:
        raise SmokeFailure(
            f"POST /api/push/subscricoes answered {resp.status_code}: {resp.text[:300]}"
        )
    rep.detail(f"subscribed with endpoint {push.endpoint}")

    resp = other.post(box.base_url + "/api/cafe", timeout=HTTP_TIMEOUT)
    if resp.status_code != 201:
        raise SmokeFailure(f"POST /api/cafe (second user) answered {resp.status_code}")
    rep.detail("second person drank a coffee; waiting for the push")

    try:
        received = push.wait(PUSH_TIMEOUT)
    except queue.Empty:
        raise SmokeFailure(
            f"no push reached the stand in server within {PUSH_TIMEOUT:.0f}s. The send runs in a "
            "BackgroundTask, so POST /api/cafe answers 201 whether or not it worked and the error "
            "only exists in the container log. Usual causes: the container cannot reach "
            f"{PUSH_HOST} on port {push.port} (Windows Firewall inbound rule, or a stand in bound "
            "to 127.0.0.1 instead of 0.0.0.0), or the VAPID variables did not reach the app.\n"
            f"--- docker logs ---\n{box.logs()}"
        ) from None

    encoding = received.header("Content-Encoding")
    rep.detail(
        f"received POST {received.path}, {len(received.body)} bytes, "
        f"Content-Encoding: {encoding or '(none)'}"
    )
    if encoding != "aes128gcm":
        raise SmokeFailure(
            f"push body arrived with Content-Encoding {encoding!r}, expected 'aes128gcm'. A real "
            "browser would refuse to decrypt it."
        )
    if not received.header("Authorization").strip():
        raise SmokeFailure(
            "push arrived with no Authorization header, so it carried no VAPID signature and a "
            "real push service would reject it."
        )

    try:
        plain = browser.decrypt(received.body)
    except Exception as exc:
        raise SmokeFailure(
            f"the push body did not decrypt with the subscription keys this script generated: "
            f"{type(exc).__name__}: {exc}. A browser would drop it silently."
        ) from None
    try:
        payload = json.loads(plain)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(
            f"the decrypted push body is not JSON ({exc}): {plain[:200]!r}. sw.js calls "
            "event.data.json() on it."
        ) from None
    rep.detail(f"decrypted payload: {json.dumps(payload, ensure_ascii=False)}")

    if not isinstance(payload, dict):
        raise SmokeFailure(f"the decrypted payload is {type(payload).__name__}, expected an object")

    for name in required:
        if name not in payload:
            raise SmokeFailure(
                f"sw.js reads dados.{name} but the pushed payload has no such field. It carries "
                f"{sorted(payload)}. The notification would fall back to its placeholder, which "
                "is the bug this script exists to catch."
            )
        value = payload[name]
        if value is None or (isinstance(value, str) and not value.strip()):
            raise SmokeFailure(
                f"sw.js reads dados.{name} and the field is present but empty ({value!r}). A "
                "notification that arrives with an empty title is exactly the bug that shipped: "
                "the key check would have passed here."
            )
        rep.detail(f"dados.{name} = {value!r}")

    extra = push.drain()
    if extra:
        raise SmokeFailure(
            f"{len(extra)} unexpected extra push(es) arrived. This step is built so exactly one "
            "is sent, so an extra one means the notification rules changed and the payload "
            "checked above may not be the coffee notification."
        )
    rep.passed(
        f"one push sent, decrypted, and all {len(required)} field(s) sw.js reads "
        f"({', '.join(required)}) are present and non-empty"
    )


def _register(box: Container, label: str) -> tuple[requests.Session, int]:
    session = requests.Session()
    name = f"Smoke {secrets.token_hex(3)}"
    pin = "".join(secrets.choice("0123456789") for _ in range(4))
    resp = session.post(
        box.base_url + "/api/registar",
        json={"nome": name, "pin": pin, "cafes_dia": 1},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 201:
        raise SmokeFailure(f"POST /api/registar ({label}) answered {resp.status_code}: {resp.text[:300]}")
    return session, resp.json()["id"]


def _read_me(box: Container, session: requests.Session, label: str) -> dict:
    resp = session.get(box.base_url + "/api/eu", timeout=HTTP_TIMEOUT)
    if resp.status_code != 200:
        raise SmokeFailure(f"GET /api/eu ({label}) answered {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    if not isinstance(body.get("saldo_cent"), int):
        raise SmokeFailure(
            f"GET /api/eu ({label}) has no integer saldo_cent (got {body.get('saldo_cent')!r}). "
            "The client would draw its balance card from nothing."
        )
    return body


def step_7_payment(rep: Report, box: Container) -> None:
    rep.start("Step 7: one person drinks, pays another by MB WAY, and both balances move")
    # Two fresh accounts, so no earlier step can have moved these balances. The
    # step 6 subscriber still gets their registo and cafe pushes; nothing here
    # reads the push inbox, so they do not matter.
    payer, payer_id = _register(box, "payer")
    receiver, receiver_id = _register(box, "receiver")
    rep.detail(f"payer id {payer_id}, receiver id {receiver_id}")

    before = _read_me(box, payer, "payer")
    resp = payer.post(box.base_url + "/api/cafe", timeout=HTTP_TIMEOUT)
    if resp.status_code != 201:
        raise SmokeFailure(f"POST /api/cafe (payer) answered {resp.status_code}: {resp.text[:300]}")
    drank = _read_me(box, payer, "payer")
    price = drank["valor_cent"] - before["valor_cent"]
    if price <= 0 or drank["saldo_cent"] != before["saldo_cent"] - price:
        raise SmokeFailure(
            f"after one coffee the payer's saldo_cent went {before['saldo_cent']} -> "
            f"{drank['saldo_cent']} while the month's valor_cent moved by {price}. The balance "
            "must drop by exactly the price stamped on the coffee."
        )
    rep.detail(f"the coffee cost {price} cent; payer saldo_cent is now {drank['saldo_cent']}")

    receiver_before = _read_me(box, receiver, "receiver")["saldo_cent"]
    amount = 500
    resp = payer.post(
        box.base_url + "/api/transferencias",
        json={"recebedor_id": receiver_id, "valor_cent": amount},
        timeout=HTTP_TIMEOUT,
    )
    if resp.status_code != 201:
        raise SmokeFailure(f"POST /api/transferencias answered {resp.status_code}: {resp.text[:300]}")
    transfer_id = resp.json().get("id")

    payer_after = _read_me(box, payer, "payer")["saldo_cent"]
    receiver_view = _read_me(box, receiver, "receiver")
    receiver_after = receiver_view["saldo_cent"]
    if payer_after != drank["saldo_cent"] + amount or receiver_after != receiver_before - amount:
        raise SmokeFailure(
            f"POST /api/transferencias answered 201 but the balances read back are wrong: payer "
            f"{drank['saldo_cent']} -> {payer_after} (expected +{amount}), receiver "
            f"{receiver_before} -> {receiver_after} (expected -{amount})."
        )
    pending = [t for t in receiver_view.get("por_confirmar") or [] if t.get("id") == transfer_id]
    if not pending or pending[0].get("pagador_id") != payer_id or pending[0].get("valor_cent") != amount:
        raise SmokeFailure(
            f"the receiver's por_confirmar does not list transfer {transfer_id} from {payer_id} "
            f"for {amount} cent: {receiver_view.get('por_confirmar')!r}. They would have nothing "
            "to confirm or reject."
        )
    rep.passed(
        f"payer saldo_cent {drank['saldo_cent']} -> {payer_after}, receiver "
        f"{receiver_before} -> {receiver_after}, and the payment waits in por_confirmar"
    )


# ---------------------------------------------------------------- driver

def main() -> int:
    # The payloads are Portuguese, so the report contains accented text. A
    # console that cannot encode it must not abort the run half way through the
    # evidence; printing a replacement character is the lesser evil.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="Run the office coffee image end to end in a throwaway container."
    )
    parser.add_argument("image", help="image reference (tag or digest) to smoke test")
    parser.add_argument(
        "--allow-skips",
        action="store_true",
        help="exit 0 even when a check could not be run, after printing what went unverified",
    )
    parser.add_argument(
        "--pull", action="store_true", help="pull the image before running it"
    )
    args = parser.parse_args()

    rep = Report()
    print("=" * 72)
    print("Smoke gate for the office coffee app")
    print(f"  image : {args.image}")
    print(f"  tree  : {ROOT}")
    print("  target: a throwaway container. Production is never contacted.")
    print("=" * 72)

    present = run_docker(["image", "inspect", args.image], check=False).returncode == 0
    if args.pull or not present:
        print(f"\n== pulling {args.image}")
        proc = run_docker(["pull", args.image], check=False)
        if proc.returncode != 0:
            print(f"   FAIL: could not pull {args.image}: {proc.stderr.strip()}")
            return 1

    private_key, public_key = new_vapid_keys()
    push = PushStandIn()
    # Explicit environment only: no ambient CAFE_* variable can leak in and point
    # this run at anything real.
    box = Container(
        args.image,
        {
            "CAFE_DB": "/data/cafe.db",
            "CAFE_TZ": "Europe/Lisbon",
            "CAFE_VAPID_PRIVATE": private_key,
            "CAFE_VAPID_PUBLIC": public_key,
            "CAFE_VAPID_CONTACTO": "mailto:smoke@invalid.example",
        },
    )

    def cleanup() -> None:
        box.remove()
        push.close()

    atexit.register(cleanup)
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: sys.exit(130))
        except (ValueError, OSError):
            pass  # not the main thread, or not supported on this platform

    remaining = [
        "Step 1: container starts and / answers 200",
        "Step 2: served sw.js and app.js are byte identical to the working tree",
        "Step 3: register, log in, drink a coffee, and see the count actually move",
        "Step 4: GET /api/push/chave carries the field app.js actually reads",
        "Step 5: notification preferences round trip in the shape app.js builds",
        "Step 6: a real push arrives, decrypts, and carries non-empty values",
        "Step 7: one person drinks, pays another by MB WAY, and both balances move",
    ]

    try:
        push.start()
        print(f"\n== push stand in listening on 0.0.0.0:{push.port}")
        box.start(rep)

        for step in (
            lambda: step_1_starts(rep, box),
            lambda: step_2_assets(rep, box),
        ):
            step()
            remaining.pop(0)

        session = step_3_journey(rep, box)
        remaining.pop(0)
        step_4_vapid(rep, box, public_key)
        remaining.pop(0)
        step_5_preferences(rep, box, session)
        remaining.pop(0)
        step_6_push(rep, box, session, push)
        remaining.pop(0)
        step_7_payment(rep, box)
        remaining.pop(0)
    except SmokeFailure as exc:
        rep.failed(str(exc))
        for step in remaining:
            if step != rep._step:
                rep.not_run(step)
    except SmokeSkip as exc:
        rep.skipped(str(exc))
        for step in remaining:
            if step != rep._step:
                rep.not_run(step)
    except KeyboardInterrupt:
        print("\nInterrupted. Removing the container and freeing the port.")
        cleanup()
        return 130
    finally:
        cleanup()
        atexit.unregister(cleanup)

    return rep.summary(args.allow_skips)


if __name__ == "__main__":
    sys.exit(main())
