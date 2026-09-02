"""Vercel Python serverless function: POST /api/verify

Receives one board image and asks Gemini (with Google Search grounding) to
find its real-world source, verifying rather than guessing. Runs on
gemini-2.5-flash specifically -- as of this writing, free-tier Google
Search grounding (up to 500 requests/day, no charge) is only offered on the
2.5 model family; the newer 3.x "flash" models don't grant free grounding
via the API (Studio-only). If Google ever changes this, re-check
https://ai.google.dev/gemini-api/docs/pricing before switching models.

The Gemini API key lives only in this server's environment (GOOGLE_API_KEY
on Vercel -- this is the exact name the SDK auto-reads, confirmed from the
google-genai client source, not guessed) -- the browser never sees it, and
this function never runs inside the viewer's own network, so a locked-down
office wifi that blocks calls to generativelanguage.googleapis.com is
irrelevant: only this server talks to Google.

API surface below was confirmed by introspecting the installed google-genai
1.47.0 package directly (docs pages describe a different `interactions.create`
surface that doesn't exist in this SDK version) -- if a future SDK upgrade
changes shapes, re-introspect rather than trusting docs text alone.
"""
import base64
import json
import os
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

from google import genai
from google.genai import types

MODEL = "gemini-3.6-flash"
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
}

VERIFY_PROMPT = (
    "Look at the attached interior/design photo. Use Google Search to find out "
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
    "- `sourceUrl` must be a real URL you found via search, not invented.\n\n"
    "Respond with ONLY a single JSON object with exactly these keys: "
    "verified (boolean), brand, project, designer, location, year, summary, "
    "takeaway, sourceName, sourceUrl, reason (all strings, use \"\" for "
    "fields that don't apply). No markdown code fences, no text before or "
    "after the JSON object."
)


def _parse_data_uri(data_uri):
    m = re.match(r"^data:([^;]+);base64,(.+)$", data_uri, re.DOTALL)
    if not m:
        raise ValueError("bad data URI")
    return m.group(1), m.group(2)


def _extract_json(text):
    if not text:
        return None
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group(0))
        except (ValueError, TypeError):
            return None


def google_reverse_image_search(b64data):
    """Optional pre-pass: Google Cloud Vision's Web Detection does actual
    pixel-level reverse image search, which text-only search can miss
    entirely for images with no legible logo/signage/caption. This is a
    SEPARATE Google API from Gemini -- its own project/API-enable/key in
    Cloud Console, not reused from the Gemini API key. Returns None (not an
    empty list) when GOOGLE_VISION_API_KEY isn't set, when Google returns
    nothing useful, or on any request error -- callers must treat None as
    "skip this step", never as "confirmed no match"."""
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


def verify_image(data_uri, caption, mode="search"):
    media_type, b64data = _parse_data_uri(data_uri)
    raw_bytes = base64.b64decode(b64data)
    client = genai.Client()  # reads GOOGLE_API_KEY from the environment

    vision_hint = google_reverse_image_search(b64data)
    vision_text = ""
    if vision_hint and (vision_hint["pages"] or vision_hint["labels"]):
        lines = ["\n\nGoogle Cloud Vision's reverse image search (real pixel-level match, already run for you) found:"]
        if vision_hint["labels"]:
            lines.append("Best-guess labels: " + ", ".join(vision_hint["labels"]))
        for p in vision_hint["pages"]:
            lines.append(f"- {p['url']}" + (f" ({p['title']})" if p["title"] else ""))
        lines.append(
            "Check these candidate pages first via search -- if one clearly matches this "
            "exact image, that's strong evidence. If none actually match on inspection, fall "
            "back to your own web search. Never treat a candidate URL as confirmed without "
            "actually checking it matches this specific photo."
        )
        vision_text = "\n".join(lines)

    prompt_text = VERIFY_PROMPT + (f"\n\n(User's own label for this image: {caption})" if caption else "") + vision_text
    if mode == "url_context":
        # Diagnostic-only: give it a known real URL to fetch/check, since
        # url_context needs something concrete to test against.
        prompt_text += (
            "\n\nAlso: use the url_context tool to fetch and check "
            "https://www.dezeen.com/2018/09/24/valerio-olgiati-celine-miami-flagship-store-interior-architecture/ "
            "-- does that page's content match this image?"
        )
    image_part = types.Part.from_bytes(data=raw_bytes, mime_type=media_type)
    contents = [prompt_text, image_part]
    if mode == "search":
        tools = [types.Tool(google_search=types.GoogleSearch())]
    elif mode == "url_context":
        tools = [types.Tool(url_context=types.UrlContext())]
    else:
        tools = []

    def call(with_schema):
        if with_schema:
            config = types.GenerateContentConfig(
                tools=tools,
                response_mime_type="application/json",
                response_json_schema=CASE_SCHEMA,
            )
        else:
            config = types.GenerateContentConfig(tools=tools)
        return client.models.generate_content(model=MODEL, contents=contents, config=config)

    # response_json_schema + google_search together isn't confirmed to work
    # in combination -- the prompt already asks for bare JSON as a
    # fallback, so if the combo is rejected, retry without the schema
    # constraint and rely on that text instruction instead.
    try:
        response = call(with_schema=True)
    except Exception:
        response = call(with_schema=False)

    usage = response.usage_metadata
    usage_summary = {
        "inputTokens": getattr(usage, "prompt_token_count", 0) or 0,
        "outputTokens": getattr(usage, "candidates_token_count", 0) or 0,
        "toolCalls": getattr(usage, "tool_use_prompt_token_count", None) and 1 or 0,
        "model": MODEL,
    }

    data = _extract_json(response.text)
    if data is None:
        return {"verified": False, "reason": "모델 응답에서 결과를 파싱하지 못했습니다.", "_usage": usage_summary}
    data["_usage"] = usage_summary
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

            # TEMPORARY diagnostic flag (2026-09-02): lets us test
            # search / url_context / no-tool against the SAME deployment
            # without a redeploy between tests. Remove once resolved.
            mode = body.get("_debugMode", "search")
            result = verify_image(data_uri, caption, mode=mode)
            result["hash"] = image_hash
            self._respond(200, result)
        except Exception as e:  # noqa: BLE001 -- always return JSON, never a raw 500 page
            self._respond(500, {"error": str(e)})

    def _respond(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
