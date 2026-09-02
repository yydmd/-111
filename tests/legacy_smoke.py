# -*- coding: utf-8 -*-
"""One-off smoke test for the legacy reserve.py bug fixes."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from utils import reserve  # noqa: E402

# 1) per-seat budget + `not suc` loop
r = reserve(sleep_time=0, max_attempt=2, enable_slider=False)
calls = []
r._get_page_token = lambda url, require_value=False: ("tok", "algo")
r.get_submit = lambda *a, **k: (calls.append(1), False)[1]
assert r.submit(["08:00", "09:00"], "10713", ["097", "141"], False) is False
assert len(calls) == 4, calls
print("PASS per-seat budget: 2 seats x 2 attempts =", len(calls))

# 2) success short-circuits
r2 = reserve(sleep_time=0, max_attempt=5)
r2._get_page_token = lambda url, require_value=False: ("tok", "algo")
r2.get_submit = lambda *a, **k: True
assert r2.submit(["08:00", "09:00"], "10713", ["097", "141"], False) is True
print("PASS success short-circuit")

# 3) empty seat list no longer crashes
r3 = reserve(sleep_time=0, max_attempt=2)
assert r3.submit(["08:00", "09:00"], "10713", [], False) is False
print("PASS empty seatid guard")


class FakeResp:
    def __init__(self, body):
        self.content = body.encode()


def token_of(html):
    rr = reserve(sleep_time=0)
    rr.requests.get = lambda url, verify=False, **k: FakeResp(html)
    return rr._get_page_token("http://x", require_value=True)


# 4) page-token extraction: three shapes
std = '<input id="submit_enc" value="tok"><input id="algorithm" value="algo">'
reordered = '<input value="algo" id="algorithm"><input value="tok" id="submit_enc">'
single = '<input id="submit_enc" value="tok">'
ambiguous = '<input value="SEARCH"><input id="submit_enc" value="tok">'
assert token_of(std) == ("tok", "algo"), token_of(std)
assert token_of(reordered) == ("tok", "algo"), token_of(reordered)
assert token_of(single) == ("tok", "tok"), token_of(single)
assert token_of(ambiguous) == ("tok", ""), token_of(ambiguous)
print("PASS token extraction: anchored algorithm / single-field fallback / ambiguous rejected")

# 5) get_submit tolerates non-JSON
r5 = reserve(sleep_time=0)
r5.requests.post = lambda *a, **k: FakeResp("<html>login expired</html>")
assert r5.get_submit("u", ["08:00", "09:00"], "tok", "10713", "097", value="algo") is False
print("PASS non-JSON submit response tolerated")

# 6) login sends t=true and logs the readable username
captured = {}


class LoginResp:
    def json(self):
        return {"status": True}


def fake_post(url, params=None, headers=None, verify=False, **k):
    captured.update(params=params)
    return LoginResp()


r6 = reserve(sleep_time=0)
r6.requests.post = fake_post
ok, msg = r6.login("15500000000", "pw")
assert ok and captured["params"]["t"] == "true"
assert captured["params"]["uname"] != "15500000000"  # still AES-encrypted on the wire
print("PASS login: t=true, wire uname encrypted, return ok")

# 7) env var fail-fast
import utils  # noqa: E402

assert utils._fetch_env_variables("X", False) == ""
fail_fast_message = ""
try:
    utils._fetch_env_variables("DEFINITELY_MISSING_VAR_12345", True)
    raise AssertionError("should have raised")
except SystemExit as exc:
    fail_fast_message = str(exc)
assert "DEFINITELY_MISSING_VAR_12345" in fail_fast_message
print("PASS env fail-fast:", fail_fast_message[:40], "...")

print("\nALL LEGACY SMOKE TESTS PASSED")
