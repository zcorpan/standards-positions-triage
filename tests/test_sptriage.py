import json
import unittest
from pathlib import Path
from unittest import mock

from sptriage import body, checks, data, plan, webfeatures

FIX = Path(__file__).parent / "fixtures"


def fixture(n):
    with open(FIX / f"body-{n}.md", newline="") as f:
        return f.read()


class BodyTests(unittest.TestCase):
    def test_form_values(self):
        b = body.Body(fixture(1472))
        self.assertTrue(b.form)
        self.assertEqual(b.value("web-feature"), "webtransport")
        self.assertEqual(b.value("bugzilla-url"), "")
        self.assertEqual(b.value("webkit-standards-position"), "")  # free text without a URL
        self.assertIn("bugzilla-url", b.missing())

    def test_round_trip(self):
        for n in (1472, 1175, 759, 354):
            text = fixture(n)
            self.assertEqual(body.Body(text).with_fields({}), text, n)

    def test_edit_keeps_other_fields(self):
        for n in (1472, 1175, 759):
            b = body.Body(fixture(n))
            url = "https://bugzilla.mozilla.org/show_bug.cgi?id=123456"
            b2 = body.Body(b.with_fields({"bugzilla-url": url, "web-feature": "foo-bar"}))
            self.assertEqual(b2.value("bugzilla-url"), url, n)
            self.assertEqual(b2.value("web-feature"), "foo-bar", n)
            for fid in body.IDS:
                if fid not in ("bugzilla-url", "web-feature", "other"):
                    self.assertEqual(b2.value(fid), b.value(fid), (n, fid))

    def test_insert_missing_form_heading(self):
        b = body.Body(fixture(1175))
        self.assertNotIn("web-feature", b.fields)
        new = b.with_fields({"web-feature": "foo"})
        self.assertLess(new.index("### web-feature id"), new.index("### MDN URL"))

    def test_legacy_crlf(self):
        b = body.Body(fixture(759))
        self.assertFalse(b.form)
        self.assertEqual(b.value("spec-url"), "https://url.spec.whatwg.org/")
        self.assertEqual(b.value("spec-explainer"), "")
        new = b.with_fields({"webkit-standards-position": "https://github.com/WebKit/standards-positions/issues/1"})
        self.assertIn("* WebKit standards-position: https://github.com/WebKit/standards-positions/issues/1\r\n", new)

    def test_freeform_cannot_edit(self):
        with self.assertRaises(ValueError):
            body.Body(fixture(354)).with_fields({"bugzilla-url": "https://bugzilla.mozilla.org/show_bug.cgi?id=1"})

    def test_clean_title(self):
        self.assertEqual(body.clean_title("Request for Mozilla Position: Foo"), "Foo")
        self.assertEqual(body.clean_title("RFP: Foo"), "Foo")
        self.assertIsNone(body.clean_title("Foo"))

    def test_feature_id(self):
        self.assertEqual(body.get_feature_id("https://webstatus.dev/features/anchor-positioning?q=1"), "anchor-positioning")
        self.assertEqual(body.get_feature_id("`view-transitions`"), "view-transitions")


class ProposalTests(unittest.TestCase):
    def test_wording(self):
        yes = ["I think we're fine with this. Propose positive.", "Proposed position: **negative**",
               "Mozilla position: **positive**. We plan to implement this.", "Suggest positive.",
               "I propose positive unless there's any objection", "Suggested position: neutral #1431"]
        no = ["Positive?", "Is this position negative?", "I'm positive this is a bug", "we should defer to the WG"]
        for t in yes:
            self.assertIsNotNone(checks.PROPOSAL_RE.search(t), t)
        for t in no:
            self.assertIsNone(checks.PROPOSAL_RE.search(t), t)


class EngagedTests(unittest.TestCase):
    def test_cc(self):
        ctx = mock.Mock()
        ctx.is_mozillian = lambda l: l in {"larseggert", "jesup", "mxinden"}
        issue = {"author": "req", "created": "2026-10-01T00:00:00Z", "body": fixture(1472), "comments": [
            {"author": "larseggert", "at": "2026-10-22T00:00:00Z", "body": "CC @jesup @mxinden `@supports` @req"},
            {"author": "mxinden", "at": "2026-10-23T00:00:00Z", "body": "Looks fine"}]}
        e = checks.engaged(issue, ctx)
        self.assertEqual(e["jesup"]["pinged_by"], "larseggert")
        self.assertFalse(e["jesup"]["replied"])
        self.assertTrue(e["mxinden"]["replied"])
        self.assertNotIn("supports", e)
        self.assertNotIn("req", e)


class HintTests(unittest.TestCase):
    def test_spec_hint(self):
        self.assertEqual(data.spec_hint("https://drafts.csswg.org/css-ui-4/#x")["topic"], "CSS")
        self.assertEqual(data.spec_hint("https://html.spec.whatwg.org/multipage/x.html")["venue"], "WHATWG")
        self.assertEqual(data.spec_hint("https://wicg.github.io/foo/")["venue"], "W3C CG")
        self.assertEqual(data.spec_hint("https://w3c.github.io/webtransport/")["venue"], "W3C")
        self.assertEqual(data.spec_hint("https://github.com/WICG/foo")["venue"], "W3C CG")

    def test_spec_key(self):
        a = webfeatures.spec_key("https://www.w3.org/TR/webtransport/#dom-x")
        b = webfeatures.spec_key("https://w3c.github.io/webtransport/#dom-x")
        self.assertEqual(a, b)
        self.assertEqual(webfeatures.spec_key("https://drafts.csswg.org/css-ui-4/#a"), ("css-ui", "a"))
        self.assertEqual(webfeatures.spec_key("https://dom.spec.whatwg.org/#b")[0], "dom")


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.issue = {"number": 1472, "labels": [], "status": "Unscreened", "updated": "2026-10-22T05:35:27Z",
                      "body": fixture(1472), "title": "x"}
        p = [mock.patch("sptriage.sync.repo_labels", return_value={"topic: API": {}, "venue: W3C": {}}),
             mock.patch("sptriage.sync.assignable", return_value={"jesup"}),
             mock.patch("sptriage.plan.team_hint", return_value="ask")]
        for x in p:
            x.start()
            self.addCleanup(x.stop)

    def good(self, **kw):
        d = {"issue": 1472, "base_updated_at": "2026-10-22T05:35:27Z", "labels_add": ["topic: API"],
             "status": "Needs proposed position", "assignees_add": ["jesup"],
             "body_fields": {"bugzilla-url": "https://bugzilla.mozilla.org/show_bug.cgi?id=1"}}
        d.update(kw)
        return d

    def test_ok(self):
        errs, _ = plan.validate(self.good(), self.issue)
        self.assertEqual(errs, [])

    def test_rejects(self):
        cases = [dict(labels_add=["team: Nope"]), dict(status="Triaged"), dict(assignees_add=["someone"]),
                 dict(body_fields={"other": "x"}), dict(body_fields={"bugzilla-url": "https://evil.example/"}),
                 dict(comment="hi​"), dict(base_updated_at="2026-01-01T00:00:00Z"), dict(bogus=1),
                 dict(close={"reason": "wontfix"})]
        for c in cases:
            errs, _ = plan.validate(self.good(**c), self.issue)
            self.assertTrue(errs, c)

    def test_planned_label_allowed(self):
        errs, _ = plan.validate(self.good(labels_add=["team: DOM LWS"]), self.issue, ["team: DOM LWS"])
        self.assertEqual(errs, [])


if __name__ == "__main__":
    unittest.main()
