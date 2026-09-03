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
them, all at zero cost.

Vision's images:annotate REST endpoint also turned out NOT to accept a
plain `?key=` API key on this project -- confirmed live via a 401
"API keys are not supported by this API... Expected OAuth2 access token"
(2026-09-02), contradicting what a doc search had suggested earlier. It
needs a service account + OAuth2 bearer token instead (see
GOOGLE_VISION_SERVICE_ACCOUNT_JSON below and README). Without that
configured, there are no candidate URLs to check, and this falls back to
Gemini answering from its own training-data memory alone -- fine for
famous, widely-published cases, unreliable for anything obscure or
personal.

Vision also turned out to require Cloud Billing enabled on its GCP project
(403 "This API method requires billing to be enabled") even to stay within
its free monthly quota -- so billing had to be turned on, with a
project-level Vision quota set below the free threshold as a hard ceiling
(no way to exceed it, so no way to actually be charged; see README).
Enabling billing on that project ALSO flipped the Gemini API key that had
been created under the *same* GCP project from its free tier to a
pay-as-you-go "prepay" mode with $0 prepaid -- 429 "prepayment credits are
depleted". Fix: keep the Gemini key on its own separate, billing-free GCP
project (AI Studio's default project, not the one used for Vision) --
never let the two share a project.

url_context ALSO has a real, structural limit: it can't fetch pages behind
a bot wall, confirmed live (2026-09-03) against two real Instagram post
URLs -- both came back "fetch blocked", "Instagram restricts automated
access... requires login". Same is expected for facebook.com,
pinterest.com, tiktok.com. Vision's Web Detection can still surface these
as *candidate* URLs (its crawler ≠ Gemini's fetcher), so `candidates` is
always returned to the frontend even on verified:false -- when Gemini
can't confirm a candidate because the site blocked the fetch, the human
can still open the link and judge it themselves. There is no code fix for
the underlying block; this is a permanent characteristic of those sites,
not a bug to chase.

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
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

MODEL = "gemini-3.6-flash"
GOOGLE_VISION_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_VISION_SERVICE_ACCOUNT_JSON", "")

CASE_SCHEMA = {
    "type": "object",
    "properties": {
        "verified": {"type": "boolean"},
        "brand": {"type": "string"},
        "project": {"type": "string"},
        "storeType": {"type": "string"},
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
        "verified", "brand", "project", "storeType", "designer", "location",
        "year", "summary", "takeaway", "sourceName", "sourceUrl", "reason",
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
    "- Some candidate URLs (especially instagram.com, facebook.com, "
    "pinterest.com, tiktok.com) BLOCK automated fetching -- url_context "
    "will fail on them even when they're a genuine match. If a fetch is "
    "blocked, don't phrase `reason` as if the image were fake/unverifiable "
    "in general -- say specifically that a likely candidate exists at that "
    "URL but couldn't be automatically confirmed because that site blocks "
    "automated access, so the person should open it themselves. Still set "
    "verified:false in this case (you didn't actually confirm it).\n"
    "- When verified, `summary` is 2-3 sentences on the project (in "
    "Korean), and `takeaway` is a single-sentence design insight/implication "
    "(in Korean). `storeType` is the kind of space in Korean (e.g. "
    "플래그십 스토어, 팝업 스토어, 편집숍, 쇼룸, 레스토랑/카페 -- leave \"\" if unclear).\n"
    "- `sourceUrl` must be one of the candidate URLs you actually fetched "
    "and confirmed, not invented.\n\n"
    "Respond with ONLY a single JSON object with exactly these keys: "
    "verified (boolean), brand, project, storeType, designer, location, "
    "year, summary, takeaway, sourceName, sourceUrl, reason (all strings, "
    "use \"\" for fields that don't apply). No markdown code fences, no "
    "text before or after the JSON object."
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
    "(in Korean). `storeType` is the kind of space in Korean (e.g. "
    "플래그십 스토어, 팝업 스토어, 편집숍, 쇼룸, 레스토랑/카페 -- leave \"\" if unclear).\n\n"
    "Respond with ONLY a single JSON object with exactly these keys: "
    "verified (boolean), brand, project, storeType, designer, location, "
    "year, summary, takeaway, sourceName, sourceUrl, reason (all strings, "
    "use \"\" for fields that don't apply). No markdown code fences, no "
    "text before or after the JSON object."
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


def _vision_access_token():
    """Vision's images:annotate endpoint rejects plain API keys on this
    project (live-confirmed 401, see module docstring) -- needs a service
    account OAuth2 bearer token instead. GOOGLE_VISION_SERVICE_ACCOUNT_JSON
    holds the *entire* downloaded service-account key file's JSON as one
    string (see README). Scoped narrowly to cloud-vision, not the broad
    cloud-platform scope, since this only ever calls Vision."""
    info = json.loads(GOOGLE_VISION_SERVICE_ACCOUNT_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/cloud-vision"]
    )
    credentials.refresh(GoogleAuthRequest())
    return credentials.token


def google_reverse_image_search(b64data):
    """Google Cloud Vision's Web Detection -- real pixel-level reverse
    image search, a separate Google API/quota from Gemini (own
    project/service-account in Cloud Console). This is now the ONLY free
    source of candidate URLs in this pipeline (Gemini's own google_search
    tool is quota-blocked on this account -- see module docstring), so
    without this configured, verification falls back to Gemini's own
    training-data memory. Returns None (not an empty list) when
    GOOGLE_VISION_SERVICE_ACCOUNT_JSON isn't set, Google returns nothing
    useful, or on any request error -- callers must treat None as "no
    candidates", never as "confirmed no match"."""
    if not GOOGLE_VISION_SERVICE_ACCOUNT_JSON:
        return None
    try:
        token = _vision_access_token()
    except Exception:  # noqa: BLE001 -- google-auth raises its own exception types
        return None

    request_body = json.dumps({
        "requests": [{
            "image": {"content": b64data},
            "features": [{"type": "WEB_DETECTION", "maxResults": 10}],
        }]
    }).encode("utf-8")
    url = "https://vision.googleapis.com/v1/images:annotate"
    req = urllib.request.Request(
        url, data=request_body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None

    responses = payload.get("responses") or [{}]
    web = responses[0].get("webDetection") or {}
    if responses[0].get("error"):
        return None
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
    # Always surface Vision's raw candidate pages, verified or not -- when
    # Gemini can't confirm a match (e.g. it couldn't actually fetch an
    # Instagram URL, which is known to block many bots) the user can still
    # open the candidate themselves and judge it, instead of the lead being
    # silently thrown away.
    candidates = vision_hint["pages"] if vision_hint else []

    data = _extract_json(response.text)
    if data is None:
        return {"verified": False, "reason": "모델 응답에서 결과를 파싱하지 못했습니다.", "_usage": usage_summary, "candidates": candidates}
    data["_usage"] = usage_summary
    data["candidates"] = candidates
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
