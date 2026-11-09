"""Third-party text (issue titles, bodies, comments, linked data): mark it for the model, flag what readers on GitHub
wouldn't see, keep it out of the CLI's own checks, and keep control sequences out of the terminal."""

import json
import re
import secrets
import sys
import urllib.parse

from . import config

# Characters that hide or reorder text: zero-width, bidi controls, word joiners, BOM, tag characters.
HIDDEN = re.compile(r"[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb"
                    r"\U000e0000-\U000e007f]")
HTML_COMMENT = re.compile(r"(?s)<!--.*?(?:-->|$)")
# Comments from the issue templates, which aren't worth flagging.
TEMPLATE_COMMENT = re.compile(r"<!--\s*(?:Please use only the specification title as the issue title\.|"
                              r"replace _No response_ with [^<>]{0,120}?)\s*-->")
DETAILS = re.compile(r"(?is)<details\b[^>]*>(.*?)(?:</details>|$)")
SUMMARY = re.compile(r"(?is)<summary\b[^>]*>(.*?)</summary>")
# A link whose text is a URL on another host than its target: [https://a.example](https://b.example).
LINK_URL = re.compile(r"\[\s*<?(https?://[^\]\s>]+)>?\s*\]\(\s*<?(https?://[^)\s>]+)")
REF_TITLE = re.compile(r"(?m)^ {0,3}\[[^\]]+\]:\s*\S+\s+[\"'(].+")
TAG = re.compile(r"<(/?untrusted)", re.I)


def _u(ch):
    """\\uXXXX for a character (a surrogate pair above U+FFFF), valid inside a JSON string."""
    cp = ord(ch)
    if cp <= 0xffff:
        return "\\u%04x" % cp
    cp -= 0x10000
    return "\\u%04x\\u%04x" % (0xd800 + (cp >> 10), 0xdc00 + (cp & 0x3ff))


def _json(s):
    out = json.dumps(s, ensure_ascii=False)
    out = re.sub(r"[\x80-\x9f]", lambda m: _u(m.group()), out)  # C1 controls; json.dumps escapes C0
    return TAG.sub(r"\\u003c\1", HIDDEN.sub(lambda m: _u(m.group()), out))


def q(s, cap=200):
    """A short third-party string, JSON-quoted."""
    s = s or ""
    return _json(s if len(s) <= cap else s[:cap] + " […]")


def hidden(text):
    """What a reader of the rendered text on GitHub wouldn't see or would likely miss, as short notes."""
    text = text or ""
    notes = []
    n = len(HTML_COMMENT.findall(TEMPLATE_COMMENT.sub("", text)))
    if n:
        notes.append(f"{n} HTML comment{'s' if n > 1 else ''} (not shown on GitHub)")
    for d in DETAILS.findall(text):
        s = SUMMARY.search(d)
        notes.append(f"collapsed <details> {q(re.sub(r'<[^>]+>', '', s.group(1)).strip(), 60) if s else '(no summary)'}")
    for shown, target in LINK_URL.findall(text):
        a, b = (urllib.parse.urlsplit(u).hostname or "" for u in (shown, target))
        if a.removeprefix("www.") != b.removeprefix("www."):
            notes.append(f"link text shows {a} but goes to {b}")
    if REF_TITLE.search(text):
        notes.append("link reference with a title (shown only on hover)")
    n = len(HIDDEN.findall(text))
    if n:
        notes.append(f"{n} hidden or bidi Unicode character{'s' if n > 1 else ''}")
    return notes


def block(text, cap, source):
    """Third-party text as a JSON string between tags with a random id. `source` (who wrote it, and when) is ours;
    the text can't fake it or the closing tag."""
    text = (text or "").replace("\r\n", "\n").strip()
    notes = hidden(text)
    if len(text) > cap:
        text = text[:cap] + " […]"
    tag = secrets.token_hex(4)
    src = source.replace('"', "'")
    out = f'<untrusted id={tag} source="{src}">\n{_json(text)}\n</untrusted id={tag}>'
    if notes:
        out += "\nhidden or easy to miss: " + "; ".join(notes)
    return out


def visible(text):
    """Text as a GitHub reader sees it, for the CLI's own checks: no HTML comments."""
    return HTML_COMMENT.sub("", text or "")


# --- terminal ---

# Our own escape sequences: SGR colors and OSC 8 links to this repo. Anything else is dropped.
_OURS = re.compile(r"\x1b\[(?:[012]|3[1-6])(?:;(?:[012]|3[1-6]))*m"
                   rf"|\x1b\]8;;https://github\.com/{re.escape(config.REPO)}/issues/\d+\x1b\\|\x1b\]8;;\x1b\\")
_BAD = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def term_safe(text):
    """Third-party text for the terminal: no control, bidi or zero-width characters."""
    return HIDDEN.sub("", _BAD.sub("", str(text or "")))


def md_text(text):
    """Third-party text as plain text inside Markdown we write (review queues, agenda): no hidden characters, and
    brackets and `<` escaped so it can't add links or HTML."""
    return re.sub(r"([\\\[\]<])", r"\\\1", term_safe(text))


def _filter(s):
    out, i = [], 0
    for m in _OURS.finditer(s):
        out.append(term_safe(s[i:m.start()]))
        out.append(m.group())
        i = m.end()
    out.append(term_safe(s[i:]))
    return "".join(out)


class SafeStream:
    """Wraps stdout so that no escape sequence other than ours reaches the terminal, whatever a call site printed."""

    def __init__(self, stream):
        self._s = stream

    def write(self, s):
        return self._s.write(_filter(s))  # keeps \n and \t; drops \r, which can overwrite a line

    def __getattr__(self, name):
        return getattr(self._s, name)


def install():
    if not isinstance(sys.stdout, SafeStream):
        sys.stdout = SafeStream(sys.stdout)
