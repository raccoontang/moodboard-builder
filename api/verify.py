"""Vercel Python serverless function: POST /api/verify

Receives one board image and verifies its real-world source using Gemini +
Google Cloud Vision -- for free. The pipeline had to be redesigned around a
real constraint discovered by live testing (2026-09-02), not documentation:

  - gemini-2.5-flash (which had a free Google Search grounding quota) is
    "no longer available to new users" -- confirmed via a live 404 from the
    API itself.
  - gemini-3.6-flash (what Google's own error message told us to switch
    to) does NOT have free Google Search grounding -- confirmed via a live
    429 RESOURCE_EXHAUSTED the instant the `google_search` tool is used,
    even with zero prior usage on a brand-new key.
  - The `url_context` tool (fetch and read a SPECIFIC already-known URL) is
    a separate tool from `google_search` (open-ended search) and is NOT
    quota-blocked -- confirmed live, same model, same key, `toolCalls: 1`,
    no 429.

So this can't do open web search for free. What it CAN do for free: fetch
and actually read specific candidate URLs. That's exactly what Google
Cloud Vision's Web Detection provides (real pixel-level reverse image
search, a genuinely separate free Google API/quota) -- so Vision finds
candidate pages, and Gemini's url_context tool actually opens and checks
them, all at zero cost. Without a GOOGLE_VISION_API_KEY configured, there
are no candidate URLs to check, and this falls back to Gemini answering
from its own training-data memory alone -- fine for famous, widely-
published cases, unreliable for anything obscure or personal. Set up
Google Vision (see README) to get real verification instead of recall.

The Gemini API key lives only in this server's environment (GOOGLE_API_KEY
on Vercel -- this is the exact name the SDK auto-reads, confirmed from the
google-genai client source) -- the browser never sees it, and this
function never runs inside the viewer's own network, so a locked-down
office wifi that blocks calls to generativelanguage.googleapis.com is
irrelevant: only this server talks to Google.

API surface below was confirmed by introspecting the installed google-genai
package directly (its docs pages describe a different `interactions.create`
surface that doesn't exist in the installed SDK version) -- if a future SDK
upgrade changes shapes, re-introspect rather than trusting docs text alone.
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
_VISION_DEBUG = {}  # TEMPORARY (2026-09-02 debug) -- remove once resolved

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

VERIFY_PROMPT_WITH_CANDIDATES = (
    "Look at the attached interior/design photo. I'm also giving you a list "
    "of candidate source pages found by a real reverse-image search (Google "
    "Cloud Vision Web Detection) -- use the url_context tool to actually "
    "fetch and read each candidate below before deciding.\n\n"
    "Rules:\n"
    "- Only set verified:true if one of the fetched candidate pages "
    "clearly matches THIS image (subject, materials, layout -- not just "
    "'similar style') and names a real brand/project.\n"
    "- If none of the candidates actually match on inspection, set "
    "verified:false and say so in `reason` -- never guess a brand or "
    "project name you can't back with a real fetched page.\n"
    "- When verified, `summary` is 2-3 sentences on the project (in "
    "Korean), and `takeaway` is a single-sentence design insight/implication "
    "(in Korean).\n"
    "- `sourceUrl` must be one of the candidate URLs you actually fetched "
    "and confirmed, not invented.\n\n"
    "Respond with ONLY a single JSON object with exactly these keys: "
    "verified (boolean), brand, project, designer, location, year, summary, "
    "takeaway, sourceName, sourceUrl, reason (all strings, use \"\" for "
    "fields that don't apply). No markdown code fences, no text before or "
    "after the JSON object."
)

VERIFY_PROMPT_NO_TOOLS = (
    "Look at the attached interior/design photo. No search tool is "
    "available for this request -- you can only answer from what you "
    "already know, not by looking anything up.\n\n"
    "Rules:\n"
    "- Only set verified:true if this is a SPECIFIC, well-documented, "
    "famous design/architecture project you can confidently name (brand, "
    "designer, location, year) from memory -- the kind that's been "
    "published in Dezeen, designboom, etc. and you're genuinely certain "
    "about, not guessing from general style.\n"
    "- If you're not confident, or this looks like a generic/personal "
    "photo you can't specifically place, set verified:false and say why in "
    "`reason` -- never invent a brand, project, or sourceUrl. Leave "
    "sourceUrl empty rather than fabricate one.\n"
    "- When verified, `summary` is 2-3 sentences on the project (in "
    "Korean), and `takeaway` is a single-sentence design insight/implication "
    "(in Korean).\n\n"
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
    """Google Cloud Vision's Web Detection -- real pixel-level reverse
    image search, a separate Google API/quota from Gemini (own
    project/API-enable/key in Cloud Console). This is now the ONLY free
    source of candidate URLs in this pipeline (Gemini's own google_search
    tool is quota-blocked on this account -- see module docstring), so
    without this configured, verification falls back to Gemini's own
    training-data memory. Returns None (not an empty list) when
    GOOGLE_VISION_API_KEY isn't set, Google returns nothing useful, or on
    any request error -- callers must treat None as "no candidates",
    never as "confirmed no match"."""
    _VISION_DEBUG.clear()
    if not GOOGLE_VISION_API_KEY:
        _VISION_DEBUG["error"] = "GOOGLE_VISION_API_KEY not set"
        return None
    _VISION_DEBUG["keyLen"] = len(GOOGLE_VISION_API_KEY)
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
    except urllib.error.HTTPError as e:
        # TEMPORARY (2026-09-02 debug): surface the real cause instead of
        # silently returning None -- remove _visionDebug once resolved.
        _VISION_DEBUG["error"] = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:500]}"
        return None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        _VISION_DEBUG["error"] = f"{type(e).__name__}: {e}"
        return None

    responses = payload.get("responses") or [{}]
    web = responses[0].get("webDetection") or {}
    error = responses[0].get("error")
    if error:
        _VISION_DEBUG["error"] = f"API error in response: {error}"
        return None
    pages = web.get("pagesWithMatchingImages") or []
    labels = web.get("bestGuessLabels") or []
    _VISION_DEBUG["pagesFound"] = len(pages)
    _VISION_DEBUG["labelsFound"] = len(labels)
    if not pages and not labels:
        return None
    return {
        "pages": [{"url": p.get("url"), "title": p.get("pageTitle", "")} for p in pages[:8] if p.get("url")],
        "labels": [l.get("label") for l in labels if l.get("label")],
    }


def verify_image(data_uri, caption):
    media_type, b64data = _parse_data_uri(data_uri)
    raw_bytes = base64.b64decode(b64data)
    client = genai.Client()  # reads GOOGLE_API_KEY from the environment

    vision_hint = google_reverse_image_search(b64data)
    has_candidates = bool(vision_hint and vision_hint["pages"])

    if has_candidates:
        lines = [VERIFY_PROMPT_WITH_CANDIDATES, "\nCandidate pages (fetch each with url_context):"]
        for p in vision_hint["pages"]:
            lines.append(f"- {p['url']}" + (f" ({p['title']})" if p["title"] else ""))
        if vision_hint["labels"]:
            lines.append("\nGoogle's best-guess labels for this image: " + ", ".join(vision_hint["labels"]))
        prompt_text = "\n".join(lines)
        tools = [types.Tool(url_context=types.UrlContext())]
    else:
        prompt_text = VERIFY_PROMPT_NO_TOOLS
        tools = []

    if caption:
        prompt_text += f"\n\n(User's own label for this image: {caption})"

    image_part = types.Part.from_bytes(data=raw_bytes, mime_type=media_type)
    contents = [prompt_text, image_part]

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

    # response_json_schema + a tool isn't confirmed to work in combination
    # on every model -- the prompt already asks for bare JSON as a
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
        "usedCandidates": has_candidates,
        "model": MODEL,
    }
    vision_debug = dict(_VISION_DEBUG)  # TEMPORARY (2026-09-02 debug)

    data = _extract_json(response.text)
    if data is None:
        return {"verified": False, "reason": "모델 응답에서 결과를 파싱하지 못했습니다.", "_usage": usage_summary, "_visionDebug": vision_debug}
    data["_usage"] = usage_summary
    data["_visionDebug"] = vision_debug
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
        except Exception as e:  # noqa: BLE001 -- always return JSON, never a raw 500 page
            self._respond(500, {"error": str(e)})

    def _respond(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
