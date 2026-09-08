#!/usr/bin/env python3
"""Cost-bounded, retrieval-stage adapters for public memory benchmarks.

This module deliberately stops at evidence retrieval.  It does not run a reader,
LLM judge, or claim final-answer accuracy.  A result is therefore a statement
about selected source evidence and budgets, not a benchmark answer score.

The input corpus is always supplied by the caller.  No dataset is downloaded by
this module.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SCHEMA_VERSION = "contextmesh-public-eval/v1"
ADAPTER_VERSION = "contextmesh-public-eval-adapter/1"
MAX_SOURCE_BYTES = 65_536
# Literal curation stores only the first 4,000 Unicode characters.  Keep a
# safety margin so public runs preserve each source atom instead of silently
# indexing a prefix.
MAX_CLAIM_CHARS = 3_500
DEFAULT_BUDGET_CHARS = 16_000
DEFAULT_SEED = 17

OFFICIAL_SOURCES = {
    "longmemeval": {
        "schema": "https://github.com/xiaowu0162/LongMemEval#dataset-format",
        "data": "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned",
        "license": "MIT (upstream repository; verify the downloaded data package terms)",
    },
    "locomo": {
        "schema": "https://github.com/snap-research/locomo",
        "paper": "https://aclanthology.org/2024.acl-long.747/",
        "license": "The upstream repository does not publish a LICENSE file; check terms before redistribution.",
    },
}

_TOKEN_RE = re.compile(r"[\w]+(?:['’][\w]+)?", re.UNICODE)


class DatasetError(ValueError):
    """Raised when a public dataset is unsupported, malformed, or truncated."""


@dataclass(frozen=True)
class HistoryItem:
    source_id: str
    text: str
    session_id: str = ""
    timestamp: str | None = None
    turn_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    benchmark: str
    question: str
    answer: Any
    history: tuple[HistoryItem, ...]
    gold_source_ids: tuple[str, ...] = ()
    category: str | int | None = None
    question_date: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvalBudget:
    """Equal retrieval budget used by every mode in one run."""

    max_context_chars: int = DEFAULT_BUDGET_CHARS
    max_candidates: int = 60

    def __post_init__(self) -> None:
        if not 1_000 <= self.max_context_chars <= 64_000:
            raise ValueError("max_context_chars must be between 1000 and 64000")
        if not 1 <= self.max_candidates <= 60:
            raise ValueError("max_candidates must be between 1 and 60")


@dataclass(frozen=True)
class RetrievedDocument:
    source_id: str
    text: str
    score: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalResult:
    mode: str
    documents: list[RetrievedDocument]
    context_text: str
    receipt: Mapping[str, Any] | None = None
    ingest: list[Mapping[str, Any]] = field(default_factory=list)
    source_mapping: list[Mapping[str, Any]] = field(default_factory=list)
    latency_ms: float = 0.0
    error: str | None = None


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha256(path: str | Path) -> str:
    """Hash the exact bytes used as input, including whitespace and ordering."""
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _tokens(text: str) -> list[str]:
    return [m.group(0).casefold() for m in _TOKEN_RE.finditer(text)]


def _load_json(path: str | Path) -> tuple[Any, bytes]:
    p = Path(path)
    raw = p.read_bytes()
    if not raw.strip():
        raise DatasetError(f"dataset is empty: {p}")
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError as exc:
        raise DatasetError(f"dataset must be a JSON document: {p}: {exc}") from exc


def _records(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        for key in ("data", "cases", "examples", "instances"):
            if isinstance(payload.get(key), list):
                records = payload[key]
                break
        else:
            raise DatasetError("JSON object must contain a data/cases/examples/instances list")
    else:
        raise DatasetError("dataset root must be a JSON list or object containing a list")
    if not records or not all(isinstance(x, Mapping) for x in records):
        raise DatasetError("dataset records must be non-empty JSON objects")
    return list(records)


def _is_truncated(obj: Mapping[str, Any]) -> bool:
    if not isinstance(obj, Mapping):
        return False
    for key in ("truncated", "is_truncated", "history_truncated", "partial"):
        if obj.get(key) is True:
            return True
    return False


def _validate_text(text: Any, where: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise DatasetError(f"{where} must be a non-empty string")
    return text


def _long_case(row: Mapping[str, Any], allow_truncated: bool) -> EvalCase:
    if _is_truncated(row) and not allow_truncated:
        raise DatasetError(f"LongMemEval case {row.get('question_id', '?')} is marked truncated")
    required = ("question_id", "question", "answer", "haystack_sessions")
    missing = [k for k in required if k not in row]
    if missing:
        raise DatasetError(f"LongMemEval case missing fields: {', '.join(missing)}")
    cid = _validate_text(row["question_id"], "question_id")
    question = _validate_text(row["question"], f"{cid}.question")
    sessions = row["haystack_sessions"]
    if not isinstance(sessions, list) or not sessions:
        raise DatasetError(f"{cid}.haystack_sessions must be a non-empty list")
    session_ids = row.get("haystack_session_ids")
    if session_ids is None:
        session_ids = [f"session_{i + 1}" for i in range(len(sessions))]
    if not isinstance(session_ids, list) or len(session_ids) != len(sessions):
        raise DatasetError(f"{cid}.haystack_session_ids must align with haystack_sessions")
    dates = row.get("haystack_dates") or [None] * len(sessions)
    if not isinstance(dates, list) or len(dates) != len(sessions):
        raise DatasetError(f"{cid}.haystack_dates must align with haystack_sessions")
    history: list[HistoryItem] = []
    for i, (sid, session, date) in enumerate(zip(session_ids, sessions, dates)):
        sid = _validate_text(sid, f"{cid}.haystack_session_ids[{i}]")
        if _is_truncated(session) and not allow_truncated:
            raise DatasetError(f"{cid} session {sid} is marked truncated")
        if not isinstance(session, list) or not session:
            raise DatasetError(f"{cid} session {sid} must be a non-empty turn list")
        lines: list[str] = []
        turn_ids: list[str] = []
        for j, turn in enumerate(session):
            if not isinstance(turn, Mapping):
                raise DatasetError(f"{cid} session {sid} turn {j} must be an object")
            role = turn.get("role")
            if role not in ("user", "assistant"):
                raise DatasetError(f"{cid} session {sid} turn {j} has unsupported role")
            content = _validate_text(turn.get("content"), f"{cid} session {sid} turn {j}.content")
            lines.append(f"{role}: {content}")
            if turn.get("has_answer") is True:
                turn_ids.append(str(j))
        history.append(HistoryItem(sid, "\n".join(lines), sid, str(date) if date is not None else None, tuple(turn_ids), {"turn_count": len(session)}))
    evidence = row.get("answer_session_ids")
    if evidence is None:
        evidence = sorted({h.session_id for h in history if h.turn_ids})
    if not isinstance(evidence, list):
        raise DatasetError(f"{cid}.answer_session_ids must be a list when present")
    known = {h.session_id for h in history}
    gold = tuple(dict.fromkeys(str(x) for x in evidence if str(x) in known))
    # An explicit evidence list must never silently refer to a missing session.
    if any(str(x) not in known for x in evidence):
        raise DatasetError(f"{cid}.answer_session_ids references an unknown session")
    return EvalCase(cid, "longmemeval", question, row["answer"], tuple(history), gold, row.get("question_type"), row.get("question_date"), {"has_answer_turns": sum(bool(h.turn_ids) for h in history)})


def _locomo_case(row: Mapping[str, Any], qa: Mapping[str, Any], sample_index: int, qa_index: int, allow_truncated: bool) -> EvalCase:
    if _is_truncated(row) and not allow_truncated:
        raise DatasetError(f"LoCoMo sample {row.get('sample_id', '?')} is marked truncated")
    cid = f"{row.get('sample_id', sample_index)}:{qa_index}"
    question = _validate_text(qa.get("question"), f"{cid}.question")
    if "answer" not in qa:
        raise DatasetError(f"{cid} QA item has no answer")
    conversation = row.get("conversation")
    if not isinstance(conversation, Mapping):
        raise DatasetError(f"{cid}.conversation must be an object")
    history: list[HistoryItem] = []
    for key, session in conversation.items():
        if not re.fullmatch(r"session_\d+", str(key)):
            continue
        if not isinstance(session, list) or not session:
            raise DatasetError(f"{cid}.{key} must be a non-empty turn list")
        date = conversation.get(f"{key}_date_time")
        lines: list[str] = []
        turn_ids: list[str] = []
        for j, turn in enumerate(session):
            if not isinstance(turn, Mapping):
                raise DatasetError(f"{cid}.{key} turn {j} must be an object")
            text = _validate_text(turn.get("text"), f"{cid}.{key}[{j}].text")
            speaker = turn.get("speaker", "")
            lines.append(f"{speaker}: {text}" if speaker else text)
            if turn.get("dia_id"):
                turn_ids.append(str(turn["dia_id"]))
        history.append(HistoryItem(str(key), "\n".join(lines), str(key), str(date) if date is not None else None, tuple(turn_ids), {"turn_count": len(session)}))
    if not history:
        raise DatasetError(f"{cid} has no conversation sessions")
    evidence = qa.get("evidence", [])
    if evidence is None:
        evidence = []
    if not isinstance(evidence, list):
        raise DatasetError(f"{cid}.qa.evidence must be a list")
    session_by_prefix = {h.session_id.removeprefix("session_"): h.session_id for h in history}
    known_turns = {turn_id for h in history for turn_id in h.turn_ids}
    gold: list[str] = []
    for ev in evidence:
        # A small number of released rows combine multiple turn IDs with `;`.
        for evs in str(ev).split(";"):
            evs = evs.strip()
            if not re.fullmatch(r"D\d+:\d+", evs) or evs not in known_turns:
                raise DatasetError(f"{cid}.qa.evidence references an unknown turn: {evs!r}")
            prefix = evs.split(":", 1)[0].removeprefix("D")
            sid = session_by_prefix.get(prefix)
            if sid and sid not in gold:
                gold.append(sid)
    return EvalCase(cid, "locomo", question, qa["answer"], tuple(history), tuple(gold), qa.get("category"), None, {"evidence_annotations": [str(x) for x in evidence], "sample_id": row.get("sample_id")})


def detect_benchmark(record: Mapping[str, Any]) -> str:
    if "haystack_sessions" in record or "question_id" in record:
        return "longmemeval"
    if "conversation" in record and "qa" in record:
        return "locomo"
    raise DatasetError("cannot detect benchmark schema (expected LongMemEval or LoCoMo fields)")


def load_public_cases(path: str | Path, benchmark: str | None = None, *, allow_truncated: bool = False, excluded: list[Mapping[str, Any]] | None = None) -> list[EvalCase]:
    """Import official JSON schemas into a common immutable case format."""
    payload, _ = _load_json(path)
    rows = _records(payload)
    if benchmark is not None and benchmark not in ("longmemeval", "locomo"):
        raise ValueError("benchmark must be longmemeval or locomo")
    detected = benchmark or detect_benchmark(rows[0])
    cases: list[EvalCase] = []
    if detected == "longmemeval":
        for row in rows:
            if detect_benchmark(row) != detected:
                raise DatasetError("mixed benchmark schemas in one input are unsupported")
            cases.append(_long_case(row, allow_truncated))
    else:
        for sample_i, row in enumerate(rows):
            if detect_benchmark(row) != detected:
                raise DatasetError("mixed benchmark schemas in one input are unsupported")
            qa = row.get("qa")
            if not isinstance(qa, list) or not qa:
                raise DatasetError(f"LoCoMo sample {sample_i}.qa must be a non-empty list")
            for qa_i, item in enumerate(qa):
                # LoCoMo category 5 has no `answer`; it supplies an
                # `adversarial_answer` for a different evaluation protocol.
                # Do not turn that field into an ordinary gold answer.
                if item.get("category") == 5:
                    if excluded is not None:
                        excluded.append({"benchmark": "locomo", "sample_index": sample_i, "qa_index": qa_i, "reason": "category_5_adversarial_answer_not_supported"})
                    continue
                try:
                    cases.append(_locomo_case(row, item, sample_i, qa_i, allow_truncated))
                except DatasetError as exc:
                    if excluded is None:
                        raise
                    excluded.append({"benchmark": "locomo", "sample_index": sample_i, "qa_index": qa_i, "reason": str(exc)})
    if len({c.case_id for c in cases}) != len(cases):
        raise DatasetError("case IDs must be unique")
    if not cases:
        raise DatasetError("no supported cases remain after validation/exclusion")
    return cases


def select_cases(cases: Sequence[EvalCase], sample_size: int | None, seed: int = DEFAULT_SEED) -> list[EvalCase]:
    """Stable sampling independent of input file order."""
    ordered = sorted(cases, key=lambda c: (c.benchmark, c.case_id))
    if sample_size is None:
        return ordered
    if sample_size < 1:
        raise ValueError("sample_size must be positive")
    if sample_size >= len(ordered):
        return ordered
    rng = random.Random(seed)
    selected = rng.sample(ordered, sample_size)
    return sorted(selected, key=lambda c: (c.benchmark, c.case_id))


def chunk_utf8(text: str, max_bytes: int = MAX_SOURCE_BYTES) -> list[str]:
    """Split on UTF-8 boundaries without exceeding the service source limit."""
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(encoded):
        end = min(start + max_bytes, len(encoded))
        while end > start:
            try:
                chunks.append(encoded[start:end].decode("utf-8"))
                break
            except UnicodeDecodeError:
                end -= 1
        if end == start:
            raise DatasetError("unable to split UTF-8 source")
        start = end
    return chunks


def source_chunks(text: str, *, max_bytes: int = MAX_SOURCE_BYTES, max_chars: int = MAX_CLAIM_CHARS) -> list[str]:
    """Split at the smaller of the transport and literal-curation limits."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    parts: list[str] = []
    current: list[str] = []
    used_chars = 0
    used_bytes = 0
    for character in text:
        char_bytes = len(character.encode("utf-8"))
        if current and (used_chars >= max_chars or used_bytes + char_bytes > max_bytes):
            parts.append("".join(current))
            current, used_chars, used_bytes = [], 0, 0
        current.append(character)
        used_chars += 1
        used_bytes += char_bytes
    if current:
        parts.append("".join(current))
    return parts or [""]


def _canonical_documents(case: EvalCase) -> list[RetrievedDocument]:
    """The common chunking used by BM25/full-history and HTTP ingestion."""
    docs: list[RetrievedDocument] = []
    for item in case.history:
        for index, text in enumerate(source_chunks(item.text)):
            docs.append(RetrievedDocument(item.source_id, text, 0.0, {"session_id": item.session_id, "chunk_index": index}))
    return docs


def _pack(documents: Iterable[RetrievedDocument], budget: EvalBudget) -> tuple[list[RetrievedDocument], str]:
    selected: list[RetrievedDocument] = []
    lines: list[str] = []
    used = 0
    for doc in documents:
        line = f"[{doc.source_id}] {doc.text}\n"
        if used + len(line) > budget.max_context_chars:
            continue
        selected.append(doc)
        lines.append(line)
        used += len(line)
        if len(selected) >= budget.max_candidates:
            break
    return selected, "".join(lines)


class RetrievalAdapter:
    mode = "adapter"

    def retrieve(self, case: EvalCase, budget: EvalBudget) -> RetrievalResult:
        raise NotImplementedError


class NoMemoryAdapter(RetrievalAdapter):
    mode = "no-memory"

    def retrieve(self, case: EvalCase, budget: EvalBudget) -> RetrievalResult:
        return RetrievalResult(self.mode, [], "")


class FullHistoryAdapter(RetrievalAdapter):
    mode = "full-history"

    def retrieve(self, case: EvalCase, budget: EvalBudget) -> RetrievalResult:
        docs = _canonical_documents(case)
        selected, context = _pack(docs, budget)
        if len(selected) != len(docs):
            return RetrievalResult(self.mode, [], "", error="full_history_exceeds_budget")
        return RetrievalResult(self.mode, selected, context)


class BM25Adapter(RetrievalAdapter):
    mode = "bm25"

    def __init__(self, k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b

    def retrieve(self, case: EvalCase, budget: EvalBudget) -> RetrievalResult:
        query = _tokens(case.question)
        docs = [(x, _tokens(x.text)) for x in _canonical_documents(case)]
        n = len(docs)
        avgdl = sum(len(t) for _, t in docs) / max(n, 1)
        df: dict[str, int] = {}
        for _, toks in docs:
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        scored: list[RetrievedDocument] = []
        for item, toks in docs:
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            score = 0.0
            for term in query:
                if term not in tf:
                    continue
                idf = math.log(1.0 + (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
                denom = tf[term] + self.k1 * (1 - self.b + self.b * len(toks) / max(avgdl, 1.0))
                score += idf * tf[term] * (self.k1 + 1) / denom
            scored.append(RetrievedDocument(item.source_id, item.text, score, dict(item.metadata)))
        scored.sort(key=lambda d: (-d.score, d.source_id))
        return RetrievalResult(self.mode, *_pack(scored, budget))


def _safe_external_id(namespace: str, case_id: str, source_id: str, chunk_index: int) -> str:
    raw = f"public-eval/{namespace}/{case_id}/{source_id}/{chunk_index}"
    if len(raw.encode("utf-8")) <= 512:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"public-eval/{namespace}/{digest}/{chunk_index}"


class ContextMeshHTTPAdapter(RetrievalAdapter):
    """Real HTTP adapter for `/v1/events` and `/v1/query`.

    It intentionally has no fake response or local score path.  Ingestion and
    retrieval failures are represented in the result and surfaced in artifacts.
    """

    mode = "contextmesh"

    def __init__(self, base_url: str, token: str, *, namespace: str = "run", timeout: float = 30.0, max_source_bytes: int = MAX_SOURCE_BYTES, wait_timeout: float = 60.0, require_fresh_case: bool = True):
        if not base_url or not token:
            raise ValueError("base_url and token are required")
        if max_source_bytes > MAX_SOURCE_BYTES:
            raise ValueError("max_source_bytes cannot exceed the ContextMesh limit")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.namespace = namespace
        self.timeout = timeout
        self.max_source_bytes = max_source_bytes
        self.wait_timeout = wait_timeout
        self.require_fresh_case = require_fresh_case
        self._source_to_external: dict[str, list[str]] = {}
        self._last_case_id: str | None = None

    def _request(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + path,
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                value = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            # Do not include response bodies: a gateway may echo source material.
            raise RuntimeError(f"ContextMesh HTTP {exc.code} at {path}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"ContextMesh request failed at {path}: {type(exc).__name__}") from exc
        if not isinstance(value, Mapping):
            raise RuntimeError(f"ContextMesh returned non-object JSON at {path}")
        return value

    def _get(self, path: str) -> Mapping[str, Any]:
        req = urllib.request.Request(self.base_url + path, method="GET", headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                value = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"ContextMesh HTTP {exc.code} at {path}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"ContextMesh request failed at {path}: {type(exc).__name__}") from exc
        if not isinstance(value, Mapping):
            raise RuntimeError(f"ContextMesh returned non-object JSON at {path}")
        return value

    def await_curation(self) -> Mapping[str, Any]:
        """Wait for the owner-visible durable job queue to drain.

        Querying before this watermark would score a transient empty packet as
        retrieval failure.  `/v1/status` is deliberately required; callers
        using a non-owner token must provision an owner evaluation token.
        """
        deadline = time.monotonic() + self.wait_timeout
        last: Mapping[str, Any] = {}
        while True:
            last = self._get("/v1/status")
            jobs = last.get("jobs", {})
            if isinstance(jobs, Mapping):
                if int(jobs.get("failed", 0) or 0):
                    raise RuntimeError("ContextMesh curation failed")
                drained = not any(int(jobs.get(k, 0) or 0) for k in ("pending", "running"))
            elif isinstance(jobs, list):
                failed = any(isinstance(x, Mapping) and x.get("state") == "failed" and int(x.get("count", 1) or 1) for x in jobs)
                if failed:
                    raise RuntimeError("ContextMesh curation failed")
                drained = not any(isinstance(x, Mapping) and x.get("state") in ("pending", "running") and int(x.get("count", 1) or 1) for x in jobs)
            else:
                raise RuntimeError("ContextMesh status has unsupported jobs shape")
            if drained:
                graphs = self._get("/v1/graphs")
                rows = graphs.get("graphs")
                if isinstance(rows, list):
                    active = [x for x in rows if isinstance(x, Mapping) and x.get("active") is True]
                    if not active or any(x.get("state") != "ready" or int(x.get("pending", 0) or 0) or int(x.get("failed", 0) or 0) for x in active):
                        drained = False
                    else:
                        last = dict(last)
                        last["graphs"] = rows
                else:
                    raise RuntimeError("ContextMesh graph listing is malformed")
                if drained:
                    return last
            if time.monotonic() >= deadline:
                raise RuntimeError("ContextMesh curation watermark timeout")
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    def ingest(self, case: EvalCase) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
        acknowledgements: list[Mapping[str, Any]] = []
        mapping: list[Mapping[str, Any]] = []
        self._source_to_external = {}
        for item in case.history:
            chunks = source_chunks(item.text, max_bytes=self.max_source_bytes)
            self._source_to_external[item.source_id] = []
            byte_offset = 0
            for index, chunk in enumerate(chunks):
                external_id = _safe_external_id(self.namespace, case.case_id, item.source_id, index)
                payload = {
                    "source": "public-eval",
                    "external_id": external_id,
                    "revision": 1,
                    "text": chunk,
                    "context": {"benchmark": case.benchmark, "case_id": case.case_id, "source_id": item.source_id, "session_id": item.session_id, "chunk_index": index, "chunk_count": len(chunks)},
                    "classification": "internal",
                    "read_groups": [],
                }
                ack = self._request("/v1/events", payload)
                acknowledgements.append(ack)
                self._source_to_external[item.source_id].append(external_id)
                mapping.append({"source_id": item.source_id, "external_id": external_id, "chunk_index": index, "chunk_count": len(chunks), "byte_start": byte_offset, "byte_end": byte_offset + len(chunk.encode("utf-8")), "event_id": ack.get("event_id")})
                byte_offset += len(chunk.encode("utf-8"))
        return acknowledgements, mapping

    def retrieve(self, case: EvalCase, budget: EvalBudget) -> RetrievalResult:
        started = time.perf_counter()
        acks, mapping = [], []
        try:
            if self.require_fresh_case and self._last_case_id is not None and self._last_case_id != case.case_id:
                raise RuntimeError("case_isolation_required: use a fresh ContextMesh service or tenant per case")
            self._last_case_id = case.case_id
            acks, mapping = self.ingest(case)
            watermark = self.await_curation()
            packet = self._request("/v1/query", {"query": case.question, "context": {"benchmark": case.benchmark, "case_id": case.case_id}, "max_chars": budget.max_context_chars})
            self._last_case_id = case.case_id
            raw = packet.get("memories", [])
            if not isinstance(raw, list):
                raise RuntimeError("ContextMesh packet memories is not a list")
            docs: list[RetrievedDocument] = []
            for memory in raw:
                if not isinstance(memory, Mapping):
                    continue
                source = memory.get("source") if isinstance(memory.get("source"), Mapping) else {}
                sid = source.get("external_id") or memory.get("external_id") or str(memory.get("event_id") or memory.get("id") or "unknown")
                # Map chunk external IDs back to canonical dataset source IDs.
                source_id = next((k for k, values in self._source_to_external.items() if sid in values), str(sid))
                text = str(memory.get("text") or memory.get("quote") or "")
                if text:
                    docs.append(RetrievedDocument(source_id, text, 0.0, {"memory": dict(memory)}))
            selected, context = _pack(docs, budget)
            packet = dict(packet)
            packet["evaluation_watermark"] = dict(watermark)
            return RetrievalResult(self.mode, selected, context, packet, acks, mapping, (time.perf_counter() - started) * 1000)
        except Exception as exc:  # noqa: BLE001 - artifact must retain case-level failure
            return RetrievalResult(self.mode, [], "", None, acks, mapping, (time.perf_counter() - started) * 1000, str(exc))


class ManagedContextMeshAdapter(RetrievalAdapter):
    """Start a fresh real service/tenant for each benchmark case.

    This is intentionally opt-in because it needs PostgreSQL and a compiled
    ContextMesh binary.  Fresh process state prevents a previous case's
    literal claims from being returned for the next case.
    """

    mode = "contextmesh"

    def __init__(self, *, binary: str | None = None, database_url: str | None = None, migration_database_url: str | None = None, workers: int = 2, namespace: str = "managed"):
        self.service_options = {"binary": binary, "database_url": database_url, "migration_database_url": migration_database_url, "workers": workers}
        self.namespace = namespace

    def retrieve(self, case: EvalCase, budget: EvalBudget) -> RetrievalResult:
        try:
            try:
                from .service import Service
            except ImportError:
                from service import Service
            with Service(**self.service_options) as service:
                assert service.url is not None
                adapter = ContextMeshHTTPAdapter(service.url, service.owner_token, namespace=self.namespace, require_fresh_case=False)
                return adapter.retrieve(case, budget)
        except Exception as exc:  # noqa: BLE001 - preserve case-level infrastructure failure
            return RetrievalResult(self.mode, [], "", error=f"managed_service: {type(exc).__name__}: {exc}")


# Short compatibility names for callers embedding the adapter.
ContextMeshAdapter = ContextMeshHTTPAdapter


def source_recall(case: EvalCase, result: RetrievalResult) -> float | None:
    """Recall of annotated evidence sources; empty-gold cases are N/A."""
    if not case.gold_source_ids:
        return None
    selected = {d.source_id for d in result.documents}
    return len(selected.intersection(case.gold_source_ids)) / len(set(case.gold_source_ids))


def score_case(case: EvalCase, result: RetrievalResult, budget: EvalBudget) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "benchmark": case.benchmark,
        "category": case.category,
        "mode": result.mode,
        "gold_source_count": len(case.gold_source_ids),
        "selected_source_ids": [d.source_id for d in result.documents],
        "retrieved_evidence": [
            {"source_id": d.source_id, "text": d.text, "score": d.score, "metadata": dict(d.metadata)}
            for d in result.documents
        ],
        "context_text": result.context_text,
        # These fields make the artifact useful for audit/reproduction.  The
        # service packet is the closest inspectable committed-memory state for
        # an HTTP run; local baselines leave it null.
        "accepted_source_events": list(result.ingest),
        "source_mapping": list(result.source_mapping),
        "query_packet": dict(result.receipt) if isinstance(result.receipt, Mapping) else None,
        "source_recall": source_recall(case, result),
        "context_chars": len(result.context_text),
        "budget_chars": budget.max_context_chars,
        "budget_ok": len(result.context_text) <= budget.max_context_chars,
        "ingest_event_count": len(result.ingest),
        "latency_ms": round(result.latency_ms, 3),
        "error": result.error,
        "receipt_id": result.receipt.get("receipt_id") if isinstance(result.receipt, Mapping) else None,
        "final_answer": None,
        "answer_scored": False,
    }


def aggregate_scores(scores: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_mode: dict[str, list[Mapping[str, Any]]] = {}
    for score in scores:
        by_mode.setdefault(str(score["mode"]), []).append(score)
    aggregate: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "answer_scored": False, "modes": {}}
    for mode, rows in sorted(by_mode.items()):
        recalls = [float(x["source_recall"]) for x in rows if x.get("source_recall") is not None and x.get("error") is None]
        aggregate["modes"][mode] = {
            "cases": len(rows),
            "successful_cases": sum(x.get("error") is None for x in rows),
            "source_recall_mean": (sum(recalls) / len(recalls)) if recalls else None,
            "source_recall_cases": len(recalls),
            "budget_violations": sum(not bool(x.get("budget_ok")) for x in rows),
            "errors": sum(x.get("error") is not None and x.get("error") != "full_history_exceeds_budget" for x in rows),
            "unavailable_cases": sum(x.get("error") == "full_history_exceeds_budget" for x in rows),
            "latency_ms_mean": sum(float(x.get("latency_ms", 0)) for x in rows) / len(rows),
            "ingest_events": sum(int(x.get("ingest_event_count", 0)) for x in rows),
        }
    return aggregate


def make_manifest(dataset_path: str | Path, cases: Sequence[EvalCase], *, seed: int, budget: EvalBudget, modes: Sequence[str], dataset_version: str | None = None, excluded: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    path = Path(dataset_path)
    return {
        "schema_version": SCHEMA_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "dataset_path": str(path),
        "dataset_sha256": file_sha256(path),
        "dataset_version": dataset_version or "unspecified-local-input",
        "benchmarks": sorted({c.benchmark for c in cases}),
        "case_count": len(cases),
        "excluded_cases": [dict(x) for x in excluded],
        "case_ids_sha256": _sha256_bytes("\n".join(c.case_id for c in cases).encode()),
        "selection": {"seed": seed, "stable_sort": "benchmark,case_id"},
        "budget": {"max_context_chars": budget.max_context_chars, "max_candidates": budget.max_candidates},
        "modes": list(modes),
        "official_sources": {k: dict(v) for k, v in OFFICIAL_SOURCES.items()},
        "retrieval_stage_only": True,
        "answer_scored": False,
        "comparability": {
            "strict_equal_budget": False,
            "reason": "Same source chunks and output ceiling; ContextMesh also charges quote/packet metadata internally, reducing effective source capacity.",
            "evidence_granularity": "session recall, not answer-supporting turn/span recall",
            "reader_model": None,
        },
    }


def write_artifacts(output_dir: str | Path, manifest: Mapping[str, Any], scores: Sequence[Mapping[str, Any]]) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with (out / "cases.jsonl").open("w", encoding="utf-8") as f:
        for row in scores:
            f.write(json.dumps(dict(row), sort_keys=True, ensure_ascii=False) + "\n")
    (out / "aggregate.json").write_text(json.dumps(aggregate_scores(scores), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_evaluation(cases: Sequence[EvalCase], adapters: Sequence[RetrievalAdapter], budget: EvalBudget) -> list[dict[str, Any]]:
    scores: list[dict[str, Any]] = []
    for adapter in adapters:
        for case in cases:
            started = time.perf_counter()
            result = adapter.retrieve(case, budget)
            result.latency_ms = (time.perf_counter() - started) * 1000
            scores.append(score_case(case, result, budget))
    return scores


def _cli(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Retrieval-stage public benchmark evaluation for ContextMesh")
    parser.add_argument("dataset", type=Path, nargs="?", help="local official LongMemEval or LoCoMo JSON file")
    parser.add_argument("--dataset", dest="dataset_option", type=Path, help="same as the positional dataset")
    parser.add_argument("--benchmark", choices=("longmemeval", "locomo"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-size", "--max-cases", type=int)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--max-context-chars", type=int, default=DEFAULT_BUDGET_CHARS)
    parser.add_argument("--max-candidates", type=int, default=60)
    parser.add_argument("--allow-truncated", action="store_true")
    parser.add_argument("--mode", action="append", choices=("no-memory", "full-history", "bm25", "contextmesh"), dest="modes")
    parser.add_argument("--contextmesh-url")
    parser.add_argument("--contextmesh-token")
    parser.add_argument("--managed-service", action="store_true", help="start a fresh real ContextMesh service and tenant per case (requires eval service environment)")
    parser.add_argument("--namespace", default="run")
    parser.add_argument("--dataset-version", dest="dataset_version")
    args = parser.parse_args(argv)
    try:
        dataset = args.dataset or args.dataset_option
        if dataset is None:
            parser.error("a dataset path is required")
        excluded: list[Mapping[str, Any]] = []
        loaded = load_public_cases(dataset, args.benchmark, allow_truncated=args.allow_truncated, excluded=excluded)
        cases = select_cases(loaded, args.sample_size, args.seed)
        budget = EvalBudget(args.max_context_chars, args.max_candidates)
        modes = args.modes or (["no-memory", "full-history", "bm25", "contextmesh"] if (args.managed_service or args.contextmesh_url) else ["no-memory", "full-history", "bm25"])
        adapters: list[RetrievalAdapter] = []
        for mode in modes:
            if mode == "no-memory": adapters.append(NoMemoryAdapter())
            elif mode == "full-history": adapters.append(FullHistoryAdapter())
            elif mode == "bm25": adapters.append(BM25Adapter())
            elif mode == "contextmesh":
                if args.managed_service:
                    adapters.append(ManagedContextMeshAdapter(namespace=args.namespace))
                elif not args.contextmesh_url or not args.contextmesh_token:
                    parser.error("--contextmesh-url and --contextmesh-token are required for contextmesh mode")
                else:
                    adapters.append(ContextMeshHTTPAdapter(args.contextmesh_url, args.contextmesh_token, namespace=args.namespace))
        scores = run_evaluation(cases, adapters, budget)
        fatal_errors = [x for x in scores if x.get("error") and x.get("error") != "full_history_exceeds_budget"]
        manifest = make_manifest(dataset, cases, seed=args.seed, budget=budget, modes=modes, dataset_version=args.dataset_version, excluded=excluded)
        manifest["completed"] = not fatal_errors
        write_artifacts(args.output, manifest, scores)
        print(json.dumps(aggregate_scores(scores), indent=2, sort_keys=True))
        if fatal_errors:
            print(f"public evaluation completed with {len(fatal_errors)} infrastructure/error cases; inspect cases.jsonl", file=sys.stderr)
            return 3
        return 0
    except (DatasetError, ValueError, OSError) as exc:
        print(f"public evaluation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
