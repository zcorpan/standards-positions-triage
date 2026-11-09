"""Parse and edit request-for-position issue bodies.

Two shapes: the issue form (`### Label` sections, issues >= ~1175) and the legacy markdown template
(`* Label: value` lines). Edits replace a field's value only and keep every other byte.
Parsing rules follow mozilla/standards-positions `gh-data.py` (`process_body`, `get_url`, `get_feature_id`).
"""

import re

# Form field ids in template order, with their form labels.
FIELDS = [
    ("spec-title", "Specification title"),
    ("spec-url", "Specification or proposal URL (if available)"),
    ("spec-explainer", "Explainer URL (if available)"),
    ("spec-authors", "Proposal author(s)"),
    ("web-feature", "web-feature id"),
    ("mdn-url", "MDN URL"),
    ("caniuse-url", "Caniuse.com URL"),
    ("bugzilla-url", "Bugzilla URL"),
    ("mozillians", "Mozillians who can provide input"),
    ("webkit-standards-position", "WebKit standards-position"),
    ("other", "Other information"),
]
IDS = [f for f, _ in FIELDS]
LABEL = dict(FIELDS)
URL_FIELDS = {"spec-url", "spec-explainer", "mdn-url", "caniuse-url", "bugzilla-url", "webkit-standards-position"}
EMPTY = {"", "_no response_", "no response", "n/a", "na", "none", "-", "tbd", "pending", "no", "not yet"}

# Legacy line prefixes (lowercased, without the trailing ":"), longest first.
_LEGACY = {
    "spec-title": ["specification title"],
    "spec-url": ["specification or proposal url (if available)", "specification or proposal url", "specification url"],
    "spec-explainer": ["explainer url (if available)", "explainer url"],
    "spec-authors": ["proposal author(s) (`@`-mention github accounts)", "proposal author(s)"],
    "web-feature": ["web-feature id", "web-feature"],
    "mdn-url": ["mdn url (optional)", "mdn url"],
    "caniuse-url": ["caniuse.com url (optional)", "caniuse.com url"],
    "bugzilla-url": ["bugzilla url (optional)", "bugzilla url"],
    "mozillians": ["mozillians who can provide input (optional)", "mozillians who can provide input"],
    "webkit-standards-position": ["webkit standards-position (optional)", "webkit standards-position"],
}


def get_url(text):
    m = re.search(r"\b(https?://[^\)\s<>\]]+)", text or "")
    return m.group(1).rstrip(",.") if m else ""


def get_feature_id(text):
    text = (text or "").strip()
    if "https://" in text:
        url = get_url(text)
        for prefix in ("https://webstatus.dev/features/", "https://web-platform-dx.github.io/web-features-explorer/features/",
                       "https://web-platform-dx.github.io/web-features-explorer/"):
            if url.startswith(prefix):
                text = url.split("#", 1)[0].split("?", 1)[0][len(prefix):].split("/", 1)[0]
                break
    m = re.search(r"[a-z][a-zA-Z0-9\-]+", text)
    return m.group() if m else None


def is_empty(value):
    v = (value or "").strip().lower()
    return v in EMPTY or v.strip("*_`") in EMPTY


class Body:
    """A parsed issue body. `fields[id]` = {"raw", "value", "span": (start, end)} for fields present."""

    def __init__(self, text):
        self.text = text or ""
        self.form = bool(re.search(r"^### Specification title\s*$", self.text, re.M))
        self.fields = self._parse_form() if self.form else self._parse_legacy()

    def _parse_form(self):
        labels = {lbl: fid for fid, lbl in FIELDS}
        heads = [(m, labels[m.group(1).strip()]) for m in re.finditer(r"^### (.+?)[ \t]*\r?$", self.text, re.M)
                 if m.group(1).strip() in labels]
        out = {}
        for i, (m, fid) in enumerate(heads):
            start = m.end()
            end = heads[i + 1][0].start() if i + 1 < len(heads) else len(self.text)
            raw = self.text[start:end]
            out[fid] = {"raw": raw.strip(), "span": (start, end)}
        return self._normalize(out)

    def _parse_legacy(self):
        out = {}
        for m in re.finditer(r"^[ \t]*[*-][ \t]+([^:\n]+?):[ \t]*(.*?)[ \t]*\r?$", self.text, re.M):
            key = m.group(1).strip().lower()
            for fid, prefixes in _LEGACY.items():
                if fid not in out and key in prefixes:
                    out[fid] = {"raw": m.group(2), "span": m.span(2)}
                    break
        return self._normalize(out)

    @staticmethod
    def _normalize(out):
        for fid, f in out.items():
            raw = f["raw"]
            if is_empty(raw):
                f["value"] = ""
            elif fid in URL_FIELDS:
                f["value"] = get_url(raw)  # free text without a URL counts as missing; `raw` keeps it
            elif fid == "web-feature":
                f["value"] = get_feature_id(raw) or ""
            else:
                f["value"] = raw.strip()
        return out

    def value(self, fid):
        f = self.fields.get(fid)
        return f["value"] if f else ""

    def values(self):
        return {fid: self.value(fid) for fid in IDS if fid != "other"}

    def missing(self):
        return [fid for fid in IDS if fid not in ("other", "spec-title") and not self.value(fid)]

    def with_fields(self, new):
        """Return body text with field values replaced (or inserted when the field is absent)."""
        text = self.text
        # apply from the end so earlier spans stay valid
        edits = []
        for fid, value in new.items():
            if fid not in LABEL or fid == "other":
                raise ValueError(f"unknown or uneditable field: {fid}")
            value = (value or "").strip()
            f = self.fields.get(fid)
            if f:
                s, e = f["span"]
                if self.form:
                    region = text[s:e]
                    lead = region[: len(region) - len(region.lstrip())]
                    trail = region[len(region.rstrip()):]
                    edits.append((s, e, lead + (value or "_No response_") + trail))
                else:
                    edits.append((s, e, value))
            else:
                edits.append(self._insertion(fid, value))
        for s, e, repl in sorted(edits, key=lambda x: x[0], reverse=True):
            text = text[:s] + repl + text[e:]
        return text

    def _insertion(self, fid, value):
        nl = "\r\n" if "\r\n" in self.text else "\n"
        order = IDS.index(fid)
        if self.form:
            # before the next present field heading in template order
            for later in IDS[order + 1:]:
                if later in self.fields:
                    s = self.fields[later]["span"][0]
                    head = self.text.rfind("###", 0, s)
                    return (head, head, f"### {LABEL[fid]}{nl}{nl}{value}{nl}{nl}")
            return (len(self.text), len(self.text), f"{nl}{nl}### {LABEL[fid]}{nl}{nl}{value}{nl}")
        # legacy: after the previous present field line, else after the last field line
        present = [x for x in IDS[:order] if x in self.fields] or list(self.fields)
        if not present:
            raise ValueError("no template fields found to insert next to")
        anchor = max(self.fields[x]["span"][1] for x in present)
        eol = self.text.find("\n", anchor)
        pos = len(self.text) if eol == -1 else eol + 1
        prefix = "" if eol != -1 else nl
        return (pos, pos, f"{prefix}* {LABEL[fid]}: {value}{nl}")


TITLE_PREFIX = re.compile(r"^\s*(?:\[?(?:request for (?:a )?(?:mozilla )?(?:standards[- ])?position|rfp)\]?\s*(?::|-|on\b)?\s*)", re.I)


def clean_title(title):
    t = TITLE_PREFIX.sub("", title, count=1).strip()
    return t if t and t != title.strip() else None
