import copy
import unittest

from balanced import assess_packet, baseline, generate, score, summarize, usage, visible


class BalancedTests(unittest.TestCase):
    def setUp(self):
        self.world = generate(51, 2)[0]
        self.events = self.world["events"]
        self.cases = {case["family"]: case for case in self.world["cases"]}

    def test_reproducible_and_disjoint_worlds(self):
        self.assertEqual(generate(51, 2), generate(51, 2))
        self.assertNotEqual(generate(51, 2)[0]["tag"], generate(51, 2)[1]["tag"])
        self.assertNotEqual(generate(51, 2), generate(52, 2))

    def test_no_memory_cannot_win_by_suppression(self):
        result = score([], ["needed"])
        self.assertEqual(result["recall"], 0)
        self.assertEqual(result["precision"], 0)
        self.assertFalse(result["exact_evidence"])
        self.assertIsNone(score([], [])["recall"])
        self.assertFalse(score([], [])["false_activation"])
        case = self.cases["direct"]
        packet = dict(memories=[], conflicts=[], receipt_id="r", trace={"search_rounds": 1})
        _, violations = assess_packet(packet, case, self.events, {e["key"]: e["key"] for e in self.events})
        self.assertIn("missing_required_evidence", violations)

    def test_empty_wrong_and_duplicate_evidence_scoring(self):
        self.assertEqual(score(["a", "a", "b"], ["a"])["precision"], .5)
        self.assertEqual(score(["wrong"], ["needed"])["recall"], 0)
        self.assertTrue(score(["wrong"], [])["false_activation"])

    def test_same_tenant_denial_and_cross_tenant_boundaries(self):
        event = next(e for e in self.events if e["key"] == "restricted")
        self.assertTrue(visible(event, self.cases["authorized"]))
        self.assertFalse(visible(event, self.cases["unauthorized"]))
        foreign = next(e for e in self.events if e["key"] == "foreign")
        self.assertFalse(visible(foreign, self.cases["owner_access"]))

    def test_oracle_is_scoped_and_redaction_wins(self):
        case = self.cases["scope_match"]
        self.assertEqual([e["key"] for e in baseline("gold_evidence", self.events, case)], ["prod"])
        erased = copy.deepcopy(self.events)
        next(e for e in erased if e["key"] == "prod")["redacted"] = True
        self.assertEqual(baseline("gold_evidence", erased, case), [])

    def test_version_unknown_and_changed_require_revalidation(self):
        event = next(e for e in self.events if e["key"] == "version")
        self.assertEqual(usage(event, self.cases["version_match"]), "applicable")
        self.assertEqual(usage(event, self.cases["version_changed"]), "revalidate")
        self.assertEqual(usage(event, self.cases["version_missing"]), "revalidate")

    def test_scorer_catches_leak_and_version_misuse(self):
        event = next(e for e in self.events if e["key"] == "version")
        memory = dict(id="claim", event_id="source", text=event["text"], quote=event["text"],
                      use="applicable", source={"revision": 1})
        packet = dict(memories=[memory], conflicts=[], receipt_id="r", trace={"search_rounds": 1})
        _, failures = assess_packet(packet, self.cases["version_changed"], [event], {"version": "source"})
        self.assertIn("incorrect_version_status", failures)
        event = dict(event, redacted=True)
        _, failures = assess_packet(packet, self.cases["version_match"], [event], {"version": "source"})
        self.assertIn("unauthorized_or_redacted_source", failures)

    def test_cluster_summary_and_pairing(self):
        rows = []
        for world in range(2):
            for mode in ("contextmesh", "no_memory", "bm25_structured", "gold_evidence"):
                selected = [] if mode == "no_memory" else ["a"]
                rows.append(dict(id=str(world), world=world, mode=mode, **score(selected, ["a"])))
        summary = summarize(rows, 5)
        self.assertEqual(summary["paired_vs_contextmesh"]["no_memory"]["recall_delta"], 1)
        self.assertEqual(summary["paired_vs_contextmesh"]["bm25_structured"]["world_bootstrap_95"], [0, 0])


if __name__ == "__main__":
    unittest.main()
