"""Vercel Python serverless function: POST /api/verify

Receives one board image and asks Claude (with the web_search tool) to find
its real-world source, verifying rather than guessing. The Anthropic API key
lives only in this server's environment (ANTHROPIC_API_KEY on Vercel) --
the browser never sees it, and this function never runs inside the viewer's
own network, so a locked-down office wifi that blocks calls to
api.anthropic.com is irrelevant: only this server talks to Anthropic.
"""
import json
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

import anthropic

MODEL = "claude-sonnet-5"  # cost-sensitive by explicit choice: this task (look at a photo,
# search, summarize) doesn't need Opus-tier reasoning, and Sonnet 5 is
# 2.5x cheaper on both input and output -- see README "비용" section.
GOOGLE_VISION_API_KEY = os.environ.get("GOOGLE_VISION_API_KEY", "")

CASE_SCHEMA = {
    "type": "object",
    "properties": {
        "verified": {"type": "boolean"},
        "brand": {"type": "string"},
        "project": {"type": "string"},
        "designer": {"type": "string"},
        "location": {"type": "string"},
        "year": {"type": "string"},
        "summary": {"type": "string"},
        "takeaway": {"type": "string"},
        "sourceName": {"type": "string"},
        "sourceUrl": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": [
        "verified", "brand", "project", "designer", "location", "year",
        "summary", "takeaway", "sourceName", "sourceUrl", "reason",
    ],
    "additionalProperties": False,
}

VERIFY_PROMPT = (
    "Look at the attached interior/design photo. Use web search to find out "
    "whether it shows a real, documented project (brand, store, "
    "installation, product) -- and if so, which one.\n\n"
    "Rules:\n"
    "- Only set verified:true if you found an actual source page (a press "
    "article, the brand's own site, a design publication like Dezeen or "
    "designboom, etc.) whose photo or description clearly matches THIS "
    "image (subject, materials, layout -- not just 'similar style').\n"
    "- If you cannot find a specific matching source, or the image looks "
    "like generic stock/rental photography with no attributable project, "
    "set verified:false and explain briefly in `reason` -- never guess a "
    "brand or project name you can't back with a real link.\n"
    "- When verified, `summary` is 2-3 sentences on the project (in "
    "Korean), and `takeaway` is a single-sentence design insight/implication "
    "(in Korean) -- what this case demonstrates that's useful for someone "
    "building a mood board.\n"
    "- `sourceUrl` must be a real URL you found via search, not invented."
)


def _parse_data_uri(data_uri):
    m = re.match(r"^data:([^;]+);base64,(.+)$", data_uri, re.DOTALL)
    if not m:
        raise ValueError("bad data URI")
    return m.group(1), m.group(2)


def _extract_final_json(content_blocks):
    """The model may run several web_search turns before its final answer;
    take the LAST text block (the synthesized result), not the first --
    output_config.format only constrains the final assistant text."""
    text_blocks = [b for b in content_blocks if getattr(b, "type", None) == "text"]
    if not text_blocks:
        return None
    return json.loads(text_blocks[-1].text)


def _search_error_codes(content_blocks):
    """Web search/fetch errors come back as a normal HTTP 200 with a
    web_search_tool_result / web_fetch_tool_result block whose `.content` is
    a single error object (a *successful* call's content is a list, for
    search, or a document object, for fetch) -- they never raise, so
    without this a call that failed partway looks identical to one that
    simply found nothing."""
    codes = []
    for b in content_blocks:
        if getattr(b, "type", None) not in ("web_search_tool_result", "web_fetch_tool_result"):
            continue
        content = getattr(b, "content", None)
        error_code = getattr(content, "error_code", None)
        if error_code:
            codes.append(error_code)
    return codes


def google_reverse_image_search(b64data):
    """Optional pre-pass: Google Cloud Vision's Web Detection does actual
    pixel-level reverse image search (finds pages using this exact or a
    near-duplicate photo), which text-only web search can miss entirely for
    images with no legible logo/signage/caption. Returns None (not an empty
    list) when GOOGLE_VISION_API_KEY isn't set, when Google returns nothing
    useful, or on any request error -- callers must treat None as "skip
    this step", never as "confirmed no match" (that would silently turn an
    API hiccup into a false negative)."""
    if not GOOGLE_VISION_API_KEY:
        return None
    request_body = json.dumps({
        "requests": [{
            "image": {"content": b64data},
            "features": [{"type": "WEB_DETECTION", "maxResults": 10}],
        }]
    }).encode("utf-8")
    url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}"
    req = urllib.request.Request(url, data=request_body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return None

    web = (payload.get("responses") or [{}])[0].get("webDetection") or {}
    pages = web.get("pagesWithMatchingImages") or []
    labels = web.get("bestGuessLabels") or []
    if not pages and not labels:
        return None
    return {
        "pages": [{"url": p.get("url"), "title": p.get("pageTitle", "")} for p in pages[:8] if p.get("url")],
        "labels": [l.get("label") for l in labels if l.get("label")],
    }


def verify_image(data_uri, caption):
    media_type, b64data = _parse_data_uri(data_uri)
    client = anthropic.Anthropic()

    vision_hint = google_reverse_image_search(b64data)
    vision_text = ""
    if vision_hint and (vision_hint["pages"] or vision_hint["labels"]):
        lines = ["\n\nGoogle Cloud Vision's reverse image search (real pixel-level match, already run for you) found:"]
        if vision_hint["labels"]:
            lines.append("Best-guess labels: " + ", ".join(vision_hint["labels"]))
        for p in vision_hint["pages"]:
            lines.append(f"- {p['url']}" + (f" ({p['title']})" if p["title"] else ""))
        lines.append(
            "Use web_fetch or web_search to check these candidate pages first -- if one clearly "
            "matches this exact image, that's strong evidence. If none actually match on inspection, "
            "fall back to your own web search. Never treat a candidate URL as confirmed without "
            "actually checking it matches this specific photo."
        )
        vision_text = "\n".join(lines)

    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64data},
                },
                {
                    "type": "text",
                    "text": VERIFY_PROMPT + (f"\n\n(User's own label for this image: {caption})" if caption else "") + vision_text,
                },
            ],
        }
    ]

    request_kwargs = dict(
        model=MODEL,
        max_tokens=4096,
        # A hard ceiling, not a target: 20+10 (30 tool round-trips, each one
        # pulling in a full page of content as input tokens) is what
        # actually blew through $5 of credit on a handful of test images.
        # A well-documented project is usually found in 1-3 searches; this
        # just gives room for a couple of wrong guesses, not an exhaustive
        # crawl. Google Vision (when configured) narrowing the search
        # up front should make this budget go further, not need to be bigger.
        tools=[
            {"type": "web_search_20260209", "name": "web_search", "max_uses": 6},
            {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 3},
        ],
        # Lower effort = fewer/more-consolidated tool calls and less
        # preamble for a task this simple, which also means fewer billed
        # thinking tokens.
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": CASE_SCHEMA}},
        messages=messages,
    )

    response = client.messages.create(**request_kwargs)
    usage_calls = [response.usage]

    # A long search turn can pause; resume once by replaying history --
    # mirrors the documented pause_turn recovery pattern. Each resume is a
    # separate billed request, so its usage is tracked too, not just the
    # last one's.
    restarts = 0
    while response.stop_reason == "pause_turn" and restarts < 3:
        messages = messages + [{"role": "assistant", "content": response.content}]
        response = client.messages.create(**{**request_kwargs, "messages": messages})
        usage_calls.append(response.usage)
        restarts += 1

    search_uses = sum(1 for b in response.content if getattr(b, "type", None) in ("server_tool_use",))
    usage_summary = {
        "inputTokens": sum(getattr(u, "input_tokens", 0) or 0 for u in usage_calls),
        "outputTokens": sum(getattr(u, "output_tokens", 0) or 0 for u in usage_calls),
        "toolCalls": search_uses,
        "model": MODEL,
    }

    if response.stop_reason == "refusal":
        return {"verified": False, "reason": "모델이 이 요청을 거절했습니다.", "_usage": usage_summary}

    search_errors = _search_error_codes(response.content)
    data = _extract_final_json(response.content)
    if data is None:
        reason = "모델 응답에서 결과를 파싱하지 못했습니다."
        if search_errors:
            reason += f" (검색 도구 오류: {', '.join(search_errors)})"
        return {"verified": False, "reason": reason, "_usage": usage_summary}
    data["_usage"] = usage_summary
    if search_errors and not data.get("verified"):
        data["reason"] = (data.get("reason") or "").strip()
        data["reason"] += f" [검색 도구에서 오류 발생: {', '.join(search_errors)}]"
    return data


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            data_uri = body.get("dataUri")
            image_hash = body.get("hash", "")
            caption = body.get("caption", "")
            if not data_uri:
                self._respond(400, {"error": "missing dataUri"})
                return

            result = verify_image(data_uri, caption)
            result["hash"] = image_hash
            self._respond(200, result)
        except anthropic.APIStatusError as e:
            self._respond(502, {"error": f"Anthropic API error: {e.status_code}", "detail": str(e.message)})
        except Exception as e:  # noqa: BLE001 -- always return JSON, never a raw 500 page
            self._respond(500, {"error": str(e)})

    def _respond(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
