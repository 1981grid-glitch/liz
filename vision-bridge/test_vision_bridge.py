"""
Regression suite for vision_bridge.py.

Same discipline as the Throne and OneNote suites: every function and constant
under test is pulled out of the real source with `ast` and exec'd, never retyped,
so this file cannot drift from what ships. It runs with nothing installed -- no
anthropic, no fastapi, no network, no API key.

    python test_vision_bridge.py      # exit 0 = green
"""
import ast
import base64
import binascii
import hmac
import io
import json
import os
import sys
import time
import uuid
from contextlib import redirect_stdout

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vision_bridge.py")
source = open(SRC, encoding="utf-8").read()
tree = ast.parse(source)

WANT_FUNCS = {"_now", "_slog", "_auth_ok", "_unauth", "_sniff_media_type", "_decode_image",
              "_clamp_query", "_rate_check", "_sweep_sessions", "_trim_history",
              "_build_messages", "_answer_text", "_usage_summary", "_health_payload", "_look"}
WANT_ASSIGNS = {"MODEL", "EFFORT", "MAX_TOKENS", "_IMAGE_MAX_BYTES", "_QUERY_MAX_CHARS",
                "_HISTORY_MAX_TURNS", "_SESSION_MAX", "_SESSION_TTL", "_RATE_WINDOW",
                "_RATE_MAX_IN_WINDOW", "_DAILY_MAX", "_PRICES", "_MAGIC", "_SYSTEM",
                "_STARTED", "_sessions", "_rate"}

func_src, assign_src = {}, {}
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANT_FUNCS:
        seg = ast.get_source_segment(source, node)
        func_src[node.name] = "\n".join(
            l for l in seg.splitlines() if not l.strip().startswith("@"))
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in WANT_ASSIGNS:
                assign_src[t.id] = ast.get_source_segment(source, node)

missing = (WANT_FUNCS - set(func_src)) | (WANT_ASSIGNS - set(assign_src))
if missing:
    print(f"FATAL: could not extract from source: {sorted(missing)}")
    sys.exit(1)

# Env must be clean before the constants are exec'd -- several read os.environ at
# module scope, and a value leaking in from the shell would silently retune the
# thing under test.
for var in ("VISION_MODEL", "VISION_EFFORT", "VISION_MAX_TOKENS", "VISION_IMAGE_MAX_BYTES",
            "VISION_RATE_PER_MIN", "VISION_DAILY_MAX", "VISION_BEARER", "VISION_ALLOW_ANON",
            "ANTHROPIC_API_KEY"):
    os.environ.pop(var, None)

NS = {"os": os, "time": time, "json": json, "base64": base64, "binascii": binascii,
      "hmac": hmac, "uuid": uuid, "sys": sys}
for name in ("MODEL", "EFFORT", "MAX_TOKENS", "_IMAGE_MAX_BYTES", "_QUERY_MAX_CHARS",
             "_HISTORY_MAX_TURNS", "_SESSION_MAX", "_SESSION_TTL", "_RATE_WINDOW",
             "_RATE_MAX_IN_WINDOW", "_DAILY_MAX", "_PRICES", "_MAGIC", "_SYSTEM",
             "_STARTED", "_sessions", "_rate"):
    exec(assign_src[name], NS)
for name in WANT_FUNCS:
    exec(func_src[name], NS)

print(f"extracted {len(func_src)} functions + {len(assign_src)} constants from vision_bridge.py")

FAILED = []
COUNT = 0


def check(label, cond):
    global COUNT
    COUNT += 1
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}")
        FAILED.append(label)


def section(name):
    print(f"\n-- {name}")


# Real headers for each accepted format.
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
GIF = b"GIF89a" + b"\x00" * 40
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 40


def b64(raw):
    return base64.b64encode(raw).decode("ascii")


class FakeBlock:
    def __init__(self, type_, text=""):
        self.type, self.text = type_, text


class FakeUsage:
    def __init__(self, inp=1000, out=100, cached=0):
        self.input_tokens, self.output_tokens = inp, out
        self.cache_read_input_tokens = cached


_UNSET = object()


class FakeResponse:
    def __init__(self, blocks=None, stop_reason="end_turn", usage=_UNSET, stop_details=None):
        self.content = blocks if blocks is not None else [FakeBlock("text", "A red toolbox.")]
        self.stop_reason = stop_reason
        # `or FakeUsage()` would quietly turn an explicitly-passed None back into a
        # real usage object, so the missing-usage path would never be exercised.
        self.usage = FakeUsage() if usage is _UNSET else usage
        self.stop_details = stop_details


class FakeMessages:
    def __init__(self, response):
        self.response, self.calls = response, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response=None):
        self.beta = type("B", (), {})()
        self.beta.messages = FakeMessages(response or FakeResponse())


# --------------------------------------------------------------------------
section("media type sniffing -- the bytes decide, not the client")
check("jpeg by magic", NS["_sniff_media_type"](JPEG) == "image/jpeg")
check("png by magic", NS["_sniff_media_type"](PNG) == "image/png")
check("gif89a by magic", NS["_sniff_media_type"](GIF) == "image/gif")
check("gif87a by magic", NS["_sniff_media_type"](b"GIF87a" + b"\x00" * 8) == "image/gif")
check("webp read at offset 8, not 0", NS["_sniff_media_type"](WEBP) == "image/webp")
check("RIFF that is not WEBP is rejected",
      NS["_sniff_media_type"](b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 8) is None)
check("unknown bytes -> None", NS["_sniff_media_type"](b"not an image at all") is None)
check("short input does not raise", NS["_sniff_media_type"](b"\xff") is None)
check("empty input does not raise", NS["_sniff_media_type"](b"") is None)

# --------------------------------------------------------------------------
section("image decode and validation")
raw, mt, err = NS["_decode_image"](b64(JPEG), "image/jpeg")
check("valid jpeg accepted", err is None and mt == "image/jpeg" and raw == JPEG)

raw, mt, err = NS["_decode_image"](b64(PNG), None)
check("media_type may be omitted; sniffed value is used", err is None and mt == "image/png")

_, _, err = NS["_decode_image"](b64(PNG), "image/jpeg")
check("declared type that contradicts the bytes is REFUSED", err is not None and "does not match" in err)

_, _, err = NS["_decode_image"]("!!!! not base64 !!!!", None)
check("invalid base64 rejected", err is not None and "base64" in err)

_, _, err = NS["_decode_image"](None, None)
check("missing image rejected", err is not None and "required" in err)

_, _, err = NS["_decode_image"](123, None)
check("non-string image rejected", err is not None)

_, _, err = NS["_decode_image"]("", None)
check("empty string rejected", err is not None)

_, _, err = NS["_decode_image"](b64(b""), None)
check("empty payload rejected", err is not None)

_, _, err = NS["_decode_image"](b64(b"\x00" * 64), None)
check("valid base64 that is not an image rejected", err is not None and "unrecognized" in err)

big = b64(JPEG + b"\x00" * (NS["_IMAGE_MAX_BYTES"] + 1))
_, _, err = NS["_decode_image"](big, None)
check("oversized image rejected", err is not None and "too large" in err)

# The cheap guard must fire before the expensive decode.
huge_string = "A" * (NS["_IMAGE_MAX_BYTES"] * 2)
t0 = time.time()
_, _, err = NS["_decode_image"](huge_string, None)
check("oversized rejected on encoded length, before decoding",
      err is not None and "too large" in err and (time.time() - t0) < 0.5)

# --------------------------------------------------------------------------
section("query clamping")
check("None -> empty", NS["_clamp_query"](None) == "")
check("non-string -> empty", NS["_clamp_query"](["a"]) == "")
check("whitespace stripped", NS["_clamp_query"]("  what is this?  ") == "what is this?")
check("long query truncated to cap",
      len(NS["_clamp_query"]("x" * 99999)) == NS["_QUERY_MAX_CHARS"])

# --------------------------------------------------------------------------
section("auth -- fails closed")
os.environ.pop("VISION_BEARER", None)
os.environ.pop("VISION_ALLOW_ANON", None)
check("no bearer configured -> everything refused", NS["_auth_ok"]("Bearer anything") is False)
check("no bearer configured, no header -> refused", NS["_auth_ok"](None) is False)

os.environ["VISION_BEARER"] = "s3cret-token"
check("correct token accepted", NS["_auth_ok"]("Bearer s3cret-token") is True)
check("wrong token refused", NS["_auth_ok"]("Bearer wrong") is False)
check("missing header refused", NS["_auth_ok"](None) is False)
check("empty header refused", NS["_auth_ok"]("") is False)
check("bare token without scheme refused", NS["_auth_ok"]("s3cret-token") is False)
check("wrong scheme refused", NS["_auth_ok"]("Basic s3cret-token") is False)
check("prefix of the token refused", NS["_auth_ok"]("Bearer s3cret") is False)
check("token plus suffix refused", NS["_auth_ok"]("Bearer s3cret-token-extra") is False)

os.environ["VISION_ALLOW_ANON"] = "1"
check("anon allowed only when explicitly set to 1", NS["_auth_ok"](None) is True)
os.environ["VISION_ALLOW_ANON"] = "true"
check("anon flag is exactly '1', not any truthy string", NS["_auth_ok"](None) is False)
os.environ.pop("VISION_ALLOW_ANON", None)
check("unauth body carries no detail", NS["_unauth"]() == {"ok": False, "error": "unauthorized"})

# --------------------------------------------------------------------------
section("rate limiting -- bounds what a leaked token can spend")
NS["_rate"].clear()
allowed_count = sum(1 for _ in range(NS["_RATE_MAX_IN_WINDOW"]) if NS["_rate_check"]("tok")[0])
check("requests up to the per-minute cap are allowed",
      allowed_count == NS["_RATE_MAX_IN_WINDOW"])
allowed, why = NS["_rate_check"]("tok")
check("the next one is refused", allowed is False and "per minute" in why)
allowed, _ = NS["_rate_check"]("other-token")
check("a different token has its own budget", allowed is True)

NS["_rate"].clear()
old = time.time() - (NS["_RATE_WINDOW"] + 5)
NS["_rate"]["tok"] = ([old] * NS["_RATE_MAX_IN_WINDOW"], 0, time.time())
allowed, _ = NS["_rate_check"]("tok")
check("hits outside the window stop counting", allowed is True)

NS["_rate"].clear()
NS["_rate"]["tok"] = ([], NS["_DAILY_MAX"], time.time())
allowed, why = NS["_rate_check"]("tok")
check("daily cap refuses even with an empty window", allowed is False and "daily" in why)

NS["_rate"].clear()
NS["_rate"]["tok"] = ([], NS["_DAILY_MAX"], time.time() - 86500)
allowed, _ = NS["_rate_check"]("tok")
check("daily counter resets after 24h", allowed is True)
NS["_rate"].clear()

# --------------------------------------------------------------------------
section("session store stays bounded")
NS["_sessions"].clear()
now = time.time()
NS["_sessions"]["fresh"] = (now, [])
NS["_sessions"]["stale"] = (now - NS["_SESSION_TTL"] - 10, [])
NS["_sweep_sessions"]()
check("expired session dropped", "stale" not in NS["_sessions"])
check("fresh session kept", "fresh" in NS["_sessions"])

NS["_sessions"].clear()
for i in range(NS["_SESSION_MAX"] + 25):
    NS["_sessions"][f"s{i}"] = (now + i, [])
NS["_sweep_sessions"]()
check("session count capped", len(NS["_sessions"]) == NS["_SESSION_MAX"])
check("oldest evicted first, newest retained",
      "s0" not in NS["_sessions"] and f"s{NS['_SESSION_MAX'] + 24}" in NS["_sessions"])
NS["_sessions"].clear()

# --------------------------------------------------------------------------
section("history trimming -- old frames must not be resent")
def user_turn(text):
    return {"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}},
        {"type": "text", "text": text}]}

def asst_turn(text):
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}

hist = []
for i in range(10):
    hist += [user_turn(f"q{i}"), asst_turn(f"a{i}")]
trimmed = NS["_trim_history"](hist)
check("history capped at the turn limit", len(trimmed) == NS["_HISTORY_MAX_TURNS"] * 2)

images = [b for m in trimmed for b in m["content"] if b.get("type") == "image"]
check("stored history carries NO images -- the live frame is the only one sent",
      len(images) == 0)

last_user = max(i for i, m in enumerate(trimmed) if m["role"] == "user")
check("text of older turns is preserved for follow-ups",
      any(b.get("text", "").startswith("q") for m in trimmed[:last_user]
          for b in m["content"] if b.get("type") == "text"))
check("a stripped turn is never left with empty content",
      all(len(m["content"]) > 0 for m in trimmed))
check("empty history is handled", NS["_trim_history"]([]) == [])

# --------------------------------------------------------------------------
section("message assembly")
msgs = NS["_build_messages"]([], "AAAA", "image/jpeg", "what is this?")
check("one message when history is empty", len(msgs) == 1)
content = msgs[0]["content"]
check("image block comes before the text block",
      content[0]["type"] == "image" and content[1]["type"] == "text")
check("media type is carried into the image block",
      content[0]["source"]["media_type"] == "image/jpeg")
check("query text is carried through", content[1]["text"] == "what is this?")
msgs = NS["_build_messages"]([], "AAAA", "image/png", "")
check("empty query gets a sensible default",
      msgs[0]["content"][1]["text"] == "What am I looking at?")

# --------------------------------------------------------------------------
section("answer extraction -- refusal is checked before content")
text, refused = NS["_answer_text"](FakeResponse([FakeBlock("text", "A red toolbox.")]))
check("plain answer returned", text == "A red toolbox." and refused is False)

text, refused = NS["_answer_text"](FakeResponse(
    [FakeBlock("text", "some content that is NOT the answer")], stop_reason="refusal"))
check("refusal flagged", refused is True)
check("refusal does not read the content blocks aloud",
      "NOT the answer" not in text)

details = type("D", (), {"category": "cyber"})()
text, _ = NS["_answer_text"](FakeResponse([], stop_reason="refusal", stop_details=details))
check("refusal category surfaced when present", "cyber" in text)

text, refused = NS["_answer_text"](FakeResponse([]))
check("empty content yields a speakable fallback", text and refused is False)

text, _ = NS["_answer_text"](FakeResponse(
    [FakeBlock("thinking", "hmm"), FakeBlock("text", "Two."), FakeBlock("text", "Maybe three.")]))
check("non-text blocks skipped, text blocks joined", text == "Two. Maybe three.")

# --------------------------------------------------------------------------
section("usage and cost")
u = NS["_usage_summary"](FakeResponse(usage=FakeUsage(1000, 100)), "claude-opus-5")
check("input tokens reported", u["input_tokens"] == 1000)
check("cost computed from the model's own rates",
      abs(u["est_cost_usd"] - ((1000 * 5.0 + 100 * 25.0) / 1_000_000)) < 1e-9)
u = NS["_usage_summary"](FakeResponse(usage=FakeUsage(1000, 100)), "some-unknown-model")
check("unknown model prices at zero rather than raising", u["est_cost_usd"] == 0.0)
check("missing usage handled", NS["_usage_summary"](FakeResponse(usage=None), NS["MODEL"]) == {})

# --------------------------------------------------------------------------
section("health payload leaks nothing")
os.environ["VISION_BEARER"] = "super-secret-value"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-must-not-appear"
payload = NS["_health_payload"]()
blob = json.dumps(payload)
check("bearer value absent from /health", "super-secret-value" not in blob)
check("api key absent from /health", "sk-ant-must-not-appear" not in blob)
check("no key-shaped substring leaks", "sk-ant" not in blob)
check("presence is reported as a boolean only", payload["auth_configured"] is True)
check("api key presence reported as a boolean only", payload["api_key_present"] is True)
check("health is otherwise useful", payload["ok"] is True and payload["model"])
os.environ.pop("ANTHROPIC_API_KEY", None)

# --------------------------------------------------------------------------
section("the log never records what the wearer looked at")
buf = io.StringIO()
with redirect_stdout(buf):
    NS["_slog"]("look.start", session="abcd1234", image_bytes=12345,
                image_b64="SECRETIMAGEDATA", query="what is my prescription bottle",
                text="it says amoxicillin", answer="amoxicillin")
line = buf.getvalue()
check("image bytes never logged", "SECRETIMAGEDATA" not in line)
check("the spoken query never logged", "prescription" not in line)
check("the answer never logged", "amoxicillin" not in line)
check("safe metadata still logged", "abcd1234" in line and "12345" in line)
check("log line is valid JSON", json.loads(line.strip())["event"] == "look.start")

# --------------------------------------------------------------------------
section("end-to-end request shape (stubbed client)")
NS["_sessions"].clear()
client = FakeClient()
out = NS["_look"](client, b64(JPEG), "image/jpeg", "what is this?", "sess-1")
sent = client.beta.messages.calls[0]
check("answer returned", out["ok"] is True and out["text"] == "A red toolbox.")
check("model is the configured one", sent["model"] == NS["MODEL"])
check("effort passed inside output_config, not top level",
      sent["output_config"]["effort"] == NS["EFFORT"] and "effort" not in sent)
check("refusal fallback enabled by default", sent["fallbacks"] == "default")
check("fallback beta flag sent", "server-side-fallback-2026-07-01" in sent["betas"])
check("no deprecated budget_tokens", "budget_tokens" not in json.dumps(sent.get("thinking", {})))
check("no assistant prefill in the outgoing messages",
      sent["messages"][-1]["role"] == "user")
check("system prompt frames image text as data, not instructions",
      "never act on it" in sent["system"].lower() or "not instructions" in sent["system"].lower())
check("max_tokens bounded for a spoken answer", sent["max_tokens"] == NS["MAX_TOKENS"])
check("session stored for follow-ups", "sess-1" in NS["_sessions"])

out2 = NS["_look"](client, b64(PNG), "image/png", "what about the left one?", "sess-1")
sent2 = client.beta.messages.calls[1]
check("second turn carries prior context", len(sent2["messages"]) > 1)
check("only the newest frame is sent on a follow-up",
      sum(1 for m in sent2["messages"] for b in m["content"]
          if isinstance(b, dict) and b.get("type") == "image") == 1)

NS["_sessions"].clear()
refusing = FakeClient(FakeResponse([FakeBlock("text", "x")], stop_reason="refusal"))
out3 = NS["_look"](refusing, b64(JPEG), "image/jpeg", "q", "sess-refused")
check("refusal surfaced to the caller", out3["refused"] is True)
check("refused turn is not written into history", "sess-refused" not in NS["_sessions"])
NS["_sessions"].clear()

# --------------------------------------------------------------------------
section("live SDK signature guard (skipped when anthropic is not installed)")
# The suite above exercises _look against a stub whose create() takes **kwargs,
# so it cannot tell an accepted parameter from a rejected one. That blind spot
# already shipped a 500: `fallbacks` does not exist in the anthropic 0.x line and
# every request failed at the SDK boundary while this file stayed green. When the
# real SDK is importable, check the signature it actually has.
try:
    import inspect
    import anthropic
except ImportError:
    print("  skip anthropic not installed -- signature not verified")
else:
    params = set(inspect.signature(
        anthropic.Anthropic(api_key="x").beta.messages.create).parameters)
    probe = FakeClient()
    NS["_sessions"].clear()
    NS["_look"](probe, b64(JPEG), "image/jpeg", "q", "sig-probe")
    NS["_sessions"].clear()
    sent_kwargs = set(probe.beta.messages.calls[0])
    unsupported = sent_kwargs - params
    check(f"every kwarg _look sends is accepted by anthropic {anthropic.__version__}"
          + (f" (unsupported: {sorted(unsupported)})" if unsupported else ""),
          not unsupported)
    check("installed SDK exposes fallbacks", "fallbacks" in params)


# --------------------------------------------------------------------------
print(f"\n{COUNT - len(FAILED)}/{COUNT} checks passed")
if FAILED:
    print("FAILED:")
    for f in FAILED:
        print(f"  - {f}")
    sys.exit(1)
print("green")
