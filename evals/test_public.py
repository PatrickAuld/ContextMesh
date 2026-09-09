import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

try:
    from .public import (
        BM25Adapter, ContextMeshHTTPAdapter, DatasetError, EvalBudget,
        FullHistoryAdapter, NoMemoryAdapter, aggregate_scores, chunk_utf8,
        load_public_cases, make_manifest, run_evaluation, select_cases,
        source_recall, write_artifacts,
    )
except ImportError:  # unittest discover -s evals imports this as test_public
    from public import (
        BM25Adapter, ContextMeshHTTPAdapter, DatasetError, EvalBudget,
        FullHistoryAdapter, NoMemoryAdapter, aggregate_scores, chunk_utf8,
        load_public_cases, make_manifest, run_evaluation, select_cases,
        source_recall, write_artifacts,
    )


LONG = [
    {
        "question_id": "q2",
        "question_type": "single-session-user",
        "question": "Which color?",
        "answer": "blue",
        "question_date": "2024-01-03",
        "haystack_session_ids": ["s1", "s2"],
        "haystack_dates": ["2024-01-01", "2024-01-02"],
        "haystack_sessions": [
            [{"role": "user", "content": "The color is blue.", "has_answer": True}],
            [{"role": "assistant", "content": "An unrelated note."}],
        ],
        "answer_session_ids": ["s1"],
    },
    {
        "question_id": "q1",
        "question": "Which animal?",
        "answer": "cat",
        "haystack_session_ids": ["s3"],
        "haystack_dates": ["2024-01-04"],
        "haystack_sessions": [[{"role": "user", "content": "I have a cat."}]],
        "answer_session_ids": ["s3"],
    },
]

LOCOMO = [
    {
        "sample_id": 7,
        "conversation": {
            "session_1_date_time": "1 pm on 1 May, 2023",
            "session_1": [{"speaker": "A", "dia_id": "D1:1", "text": "I visited the museum."}],
        },
        "qa": [{"question": "Where did A go?", "answer": "museum", "evidence": ["D1:1"], "category": 1}],
    }
]


class _Handler(BaseHTTPRequestHandler):
    calls = []

    def do_POST(self):
        n = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(n))
        self.__class__.calls.append((self.path, body, self.headers.get("Authorization")))
        if self.path == "/v1/records":
            result = {"records": [{"id": body["records"][0]["id"], "duplicate": False}]}
        else:
            result = {"watermark": 1, "records": [{"id": "record-1", "content": "I visited the museum.", "metadata": {"source_id": "session_1"}}]}
        encoded = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        self.__class__.calls.append((self.path, {}, self.headers.get("Authorization")))
        result = {"jobs": {}}
        encoded = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        return


class PublicEvalTests(unittest.TestCase):
    def _file(self, value):
        td = TemporaryDirectory()
        path = Path(td.name) / "data.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.addCleanup(td.cleanup)
        return path

    def test_import_longmemeval_and_gold_sources(self):
        cases = load_public_cases(self._file(LONG))
        self.assertEqual([c.case_id for c in cases], ["q2", "q1"])
        self.assertEqual(cases[0].gold_source_ids, ("s1",))

    def test_import_locomo_maps_dia_id_to_session(self):
        cases = load_public_cases(self._file(LOCOMO))
        self.assertEqual(cases[0].benchmark, "locomo")
        self.assertEqual(cases[0].gold_source_ids, ("session_1",))

    def test_reject_truncated_unless_explicit(self):
        row = json.loads(json.dumps(LONG[0]))
        row["truncated"] = True
        path = self._file([row])
        with self.assertRaises(DatasetError):
            load_public_cases(path)
        self.assertEqual(len(load_public_cases(path, allow_truncated=True)), 1)

    def test_sampling_is_order_independent(self):
        cases = load_public_cases(self._file(LONG))
        a = [x.case_id for x in select_cases(cases, 1, seed=91)]
        b = [x.case_id for x in select_cases(list(reversed(cases)), 1, seed=91)]
        self.assertEqual(a, b)

    def test_utf8_chunking_respects_bytes(self):
        chunks = chunk_utf8("é" * 50, max_bytes=17)
        self.assertEqual("".join(chunks), "é" * 50)
        self.assertTrue(all(len(x.encode()) <= 17 for x in chunks))

    def test_baselines_share_budget_and_source_recall(self):
        case = load_public_cases(self._file(LONG))[0]
        budget = EvalBudget(1_000, 60)
        results = run_evaluation(case and [case], [NoMemoryAdapter(), FullHistoryAdapter(), BM25Adapter()], budget)
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["source_recall"], 0.0)
        self.assertEqual(results[2]["answer_scored"], False)
        self.assertTrue(all(r["budget_ok"] for r in results))

    def test_http_adapter_ingests_and_queries_real_endpoints(self):
        _Handler.calls = []
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop_server():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.addCleanup(stop_server)
        case = load_public_cases(self._file(LOCOMO))[0]
        adapter = ContextMeshHTTPAdapter(f"http://127.0.0.1:{server.server_port}", "secret", namespace="ns")
        result = adapter.retrieve(case, EvalBudget(1_000, 60))
        self.assertIsNone(result.error)
        self.assertEqual(result.receipt["watermark"], 1)
        self.assertTrue(all(c[2] == "Bearer secret" for c in _Handler.calls))
        self.assertEqual({c[0] for c in _Handler.calls}, {"/v1/records", "/v1/context", "/v1/status"})

    def test_artifacts_include_hash_version_and_aggregate(self):
        data_path = self._file(LONG)
        cases = load_public_cases(data_path)
        budget = EvalBudget(1_000, 60)
        scores = run_evaluation(cases, [BM25Adapter()], budget)
        manifest = make_manifest(data_path, cases, seed=3, budget=budget, modes=["bm25"])
        with TemporaryDirectory() as d:
            write_artifacts(d, manifest, scores)
            self.assertTrue((Path(d) / "manifest.json").exists())
            self.assertEqual(json.loads((Path(d) / "aggregate.json").read_text())["answer_scored"], False)
            self.assertEqual(json.loads((Path(d) / "manifest.json").read_text())["schema_version"], "contextmesh-public-eval/v1")

    def test_aggregate_does_not_turn_empty_evidence_into_zero(self):
        case = load_public_cases(self._file(LOCOMO))[0]
        case = case.__class__(case.case_id, case.benchmark, case.question, case.answer, case.history, (), case.category, case.question_date, case.metadata)
        score = {"mode": "bm25", "source_recall": None, "error": None, "budget_ok": True, "latency_ms": 0, "ingest_event_count": 0}
        self.assertIsNone(aggregate_scores([score])["modes"]["bm25"]["source_recall_mean"])


if __name__ == "__main__":
    unittest.main()
