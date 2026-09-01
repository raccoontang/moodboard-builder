"""Vercel Python serverless function: POST /api/verify

Receives one board image and asks Claude (with the web_search tool) to find
its real-world source, verifying rather than guessing. The Anthropic API key
lives only in this server's environment (ANTHROPIC_API_KEY on Vercel) --
the browser never sees it, and this function never runs inside the viewer's
own network, so a locked-down office wifi that blocks calls to
api.anthropic.com is irrelevant: only this server talks to Anthropic.
"""
import json
import re
from http.server import BaseHTTPRequestHandler

import anthropic

MODEL = "claude-opus-5"

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


def verify_image(data_uri, caption):
    media_type, b64data = _parse_data_uri(data_uri)
    client = anthropic.Anthropic()

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
                    "text": VERIFY_PROMPT + (f"\n\n(User's own label for this image: {caption})" if caption else ""),
                },
            ],
        }
    ]

    request_kwargs = dict(
        model=MODEL,
        max_tokens=4096,
        tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
        output_config={"format": {"type": "json_schema", "schema": CASE_SCHEMA}},
        messages=messages,
    )

    response = client.messages.create(**request_kwargs)

    # A long search turn can pause; resume once by replaying history --
    # mirrors the documented pause_turn recovery pattern.
    restarts = 0
    while response.stop_reason == "pause_turn" and restarts < 3:
        messages = messages + [{"role": "assistant", "content": response.content}]
        response = client.messages.create(**{**request_kwargs, "messages": messages})
        restarts += 1

    if response.stop_reason == "refusal":
        return {"verified": False, "reason": "모델이 이 요청을 거절했습니다."}

    data = _extract_final_json(response.content)
    if data is None:
        return {"verified": False, "reason": "모델 응답에서 결과를 파싱하지 못했습니다."}
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
