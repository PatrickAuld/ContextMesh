"""Paired, generated evidence-selection evaluation against real service processes."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import platform
import random
import re
import subprocess
import time

VERSION = "cmes-paired-v1"
BUDGET = 16000


def generate(seed, worlds):
    rng = random.Random(seed)
    result = []
    for index in range(worlds):
        tag = "n" + hashlib.sha256(f"{seed}:{index}".encode()).hexdigest()[:12]
        events, cases = [], []

        def event(key, topic, text, **metadata):
            item = dict(key=key, topic=topic, text=f"{tag} {topic}: {text}",
                        entities=[f"eval:{tag}:{topic}"], applies={}, dependencies={},
                        slot=None, classification="internal", read_groups=[], revision=1,
                        tenant="primary", redacted=False)
            item.update(metadata)
            events.append(item)

        def case(name, topic, wanted, context=None, actor="user", phase="initial", **extra):
            item = dict(id=f"{index}:{name}", family=name, world=index, phase=phase,
                        query=f"{tag} {topic}", entities=[f"eval:{tag}:{topic}"],
                        context=context or {}, actor=actor, expected=wanted)
            item.update(extra)
            cases.append(item)

        value, alternate = rng.sample(range(100, 999), 2)
        event("direct", "capacity", f"The approved capacity is {value} units.")
        case("direct", "capacity", ["direct"])
        case("indirect_entity", "capacity", ["direct"], query="What limit should I use?")
        case("missing", "absent", [])
        event("prod", "deploy", f"Production rollout requires ticket {value}.", applies={"project": "prod"})
        event("stage", "deploy", f"Staging rollout requires ticket {alternate}.", applies={"project": "stage"})
        case("scope_match", "deploy", ["prod"], {"project": "prod"})
        case("scope_sibling", "deploy", ["stage"], {"project": "stage"})
        case("scope_missing", "deploy", [])
        event("version", "migration", f"For engine v2 set buffer to {value}.", dependencies={"engine": "2"})
        case("version_match", "migration", ["version"], {"versions": {"engine": "2"}})
        case("version_changed", "migration", ["version"], {"versions": {"engine": "3"}})
        case("version_missing", "migration", ["version"])
        event("conflict_a", "timeout", f"Timeout is {value} milliseconds.", slot=f"{tag}:timeout")
        event("conflict_b", "timeout", f"Timeout is {alternate} milliseconds.", slot=f"{tag}:timeout")
        case("unresolved_conflict", "timeout", ["conflict_a", "conflict_b"])
        event("restricted", "payroll", f"Payroll code is {value}.", classification="restricted", read_groups=["engineering"])
        case("authorized", "payroll", ["restricted"])
        case("unauthorized", "payroll", [], actor="guest")
        case("owner_access", "payroll", ["restricted"], actor="owner")
        event("foreign", "capacity", f"Other tenant approved capacity is {alternate}.", tenant="other")
        case("tenant_boundary", "capacity", ["direct"])
        case("other_tenant_positive", "capacity", ["foreign"], actor="outsider")
        event("mutable", "routing", f"Route traffic to cell {value}.")
        case("before_correction", "routing", ["mutable"])
        case("after_correction", "routing", ["mutable"], phase="corrected")
        event("erase", "retention", f"Retained canary code is {value}.")
        event("retain", "retention", f"Unrelated evidence says preserve code {alternate}.")
        case("before_redaction", "retention", ["erase", "retain"])
        case("after_redaction", "retention", ["retain"], phase="redacted")
        case("after_rebuild", "retention", ["retain"], phase="rebuilt")
        event("distractor", "capacity", "A draft brainstorming session discussed capacity without approving a limit.")
        case("lexical_distractor", "capacity", ["direct"], entities=[])
        rng.shuffle(events)
        result.append(dict(index=index, tag=tag, events=events, cases=cases,
                           correction=f"{tag} routing: Route traffic to cell {alternate}."))
    return result


def visible(event, case):
    tenant = "other" if case["actor"] == "outsider" else "primary"
    if event["tenant"] != tenant or event["redacted"]:
        return False
    return (event["classification"] == "internal" or case["actor"] == "owner"
            or (case["actor"] == "user" and "engineering" in event["read_groups"]))


def applicable(event, case):
    return all(case["context"].get(k) == v for k, v in event["applies"].items())


def usage(event, case):
    versions = case["context"].get("versions", {})
    return "revalidate" if any(versions.get(k) != v for k, v in event["dependencies"].items()) else "applicable"


def words(text):
    return re.findall(r"\w+", text.lower())


def baseline(mode, events, case):
    eligible = [e for e in events if visible(e, case) and applicable(e, case)]
    if mode == "no_memory":
        return []
    if mode == "gold_evidence":
        return [e for e in eligible if e["key"] in case["expected"]]
    documents = [Counter(words(e["text"])) for e in eligible]
    average = sum(sum(d.values()) for d in documents) / max(1, len(documents))
    query = sorted(set(words(case["query"])))
    ranked = []
    for event, document in zip(eligible, documents):
        score = 0.0
        for term in query:
            frequency = document[term]
            df = sum(term in d for d in documents)
            idf = math.log(1 + (len(documents) - df + .5) / (df + .5))
            denominator = frequency + 1.2 * (.25 + .75 * sum(document.values()) / max(1, average))
            score += idf * frequency * 2.2 / denominator
        entity_match = bool(set(event["entities"]) & set(case["entities"]))
        if score or entity_match:
            ranked.append((-int(entity_match), -score, event["key"], event))
    chosen, used = [], 0
    for _, _, _, event in sorted(ranked):
        size = len(event["text"])
        if used + size <= BUDGET:
            chosen.append(event)
            used += size
    return chosen


def score(selected, expected):
    selected, expected = set(selected), set(expected)
    hits = len(selected & expected)
    return dict(recall=hits / len(expected) if expected else None,
                precision=hits / len(selected) if selected else (0.0 if expected else None),
                exact_evidence=selected == expected,
                false_activation=bool(selected) if not expected else None)


def interval(values):
    if not values:
        return None
    ordered = sorted(values)
    return [ordered[int(.025 * (len(ordered) - 1))], ordered[int(.975 * (len(ordered) - 1))]]


def summarize(rows, seed):
    output = {}
    for mode in sorted({r["mode"] for r in rows}):
        subset = [r for r in rows if r["mode"] == mode]
        metrics = {}
        for metric in ("recall", "precision", "exact_evidence", "false_activation"):
            clusters = defaultdict(list)
            for row in subset:
                if row[metric] is not None:
                    clusters[row["world"]].append(float(row[metric]))
            means = [sum(v) / len(v) for v in clusters.values()]
            rng = random.Random(seed)
            samples = [sum(rng.choices(means, k=len(means))) / len(means) for _ in range(1000)] if means else []
            metrics[metric] = dict(mean=sum(means) / len(means) if means else None,
                                   world_bootstrap_95=interval(samples), worlds=len(means))
        output[mode] = metrics
    paired = {}
    for other in ("no_memory", "bm25_structured", "gold_evidence"):
        deltas = defaultdict(list)
        lookup = {r["id"]: r for r in rows if r["mode"] == other}
        for row in rows:
            if row["mode"] == "contextmesh" and row["recall"] is not None:
                deltas[row["world"]].append(row["recall"] - lookup[row["id"]]["recall"])
        means = [sum(v) / len(v) for v in deltas.values()]
        rng = random.Random(seed)
        samples = [sum(rng.choices(means, k=len(means))) / len(means) for _ in range(1000)] if means else []
        paired[other] = dict(recall_delta=sum(means) / len(means) if means else None,
                             world_bootstrap_95=interval(samples))
    return dict(modes=output, paired_vs_contextmesh=paired)


def assess_packet(packet, case, events, ids):
    by_id = {ids[e["key"]]: e for e in events}
    selected, violations = [], []
    for memory in packet["memories"]:
        event = by_id.get(memory["event_id"])
        if event is None:
            violations.append("unknown_or_superseded_source")
            continue
        selected.append(event["key"])
        if not visible(event, case):
            violations.append("unauthorized_or_redacted_source")
        if not applicable(event, case):
            violations.append("inapplicable_source")
        if memory.get("use") != usage(event, case):
            violations.append("incorrect_version_status")
        if not memory.get("quote") or memory["quote"] not in event["text"]:
            violations.append("invalid_provenance")
        if memory.get("text") != event["text"] or memory.get("source", {}).get("revision") != event["revision"]:
            violations.append("source_fidelity")
    slots = defaultdict(list)
    for memory in packet["memories"]:
        event = by_id.get(memory["event_id"])
        if event and event["slot"]:
            slots[event["slot"]].append(memory["id"])
    expected_conflicts = {key: sorted(values) for key, values in slots.items() if len(values) > 1}
    actual_conflicts = {c["slot"]: sorted(c["claim_ids"]) for c in packet["conflicts"]}
    if actual_conflicts != expected_conflicts or len(actual_conflicts) != len(packet["conflicts"]):
        violations.append("incorrect_conflict_status")
    if len({m["id"] for m in packet["memories"]}) != len(packet["memories"]):
        violations.append("duplicate_memory")
    if not packet.get("receipt_id"):
        violations.append("missing_receipt")
    if packet.get("trace", {}).get("search_rounds", 999) > 3:
        violations.append("search_budget")
    if case["entities"] and not set(case["expected"]).issubset(selected):
        violations.append("missing_required_evidence")
    rendered = json.dumps(packet, ensure_ascii=False)
    if any(e["text"] in rendered for e in events if not visible(e, case)):
        violations.append("unauthorized_or_redacted_packet_text")
    return selected, violations


def run(service, corpus, report):
    all_events, ids = {}, {}
    report["source_mapping"] = []
    report["probes"] = []

    def probe(name, path, token, expected, body=None):
        status, result = service.request_raw(path, body, token=token)
        passed = status == expected
        report["probes"].append(dict(name=name, status=status, expected=expected, passed=passed))
        if not passed:
            report["violations"].append(dict(case=name, reason="source_visibility"))
        return result

    for world in corpus:
        for e in world["events"]:
            key = f"{world['index']}:{e['key']}"
            token = service.outsider if e["tenant"] == "other" else service.owner
            ids[key] = service.insert(e["text"], key, context={k: e[k] for k in ("entities", "applies", "dependencies", "slot")},
                                      classification=e["classification"], read_groups=e["read_groups"], token=token)
            all_events[key] = e
            report["source_mapping"].append(dict(key=key, revision=e["revision"], event_id=ids[key]))
    service.await_idle()
    report["initial_graphs"] = service.request("/v1/graphs", token=service.owner)
    for world in corpus:
        prefix = f"{world['index']}:"
        probe(prefix + "restricted_denied", f"/v1/events/{ids[prefix + 'restricted']}", service.guest, 404)
        probe(prefix + "restricted_allowed", f"/v1/events/{ids[prefix + 'restricted']}", service.user, 200)
        probe(prefix + "foreign_denied", f"/v1/events/{ids[prefix + 'foreign']}", service.owner, 404)
        probe(prefix + "foreign_allowed", f"/v1/events/{ids[prefix + 'foreign']}", service.outsider, 200)
    prior_receipts = []
    superseded = []
    for phase in ("initial", "corrected", "redacted", "rebuilt"):
        if phase == "corrected":
            for world in corpus:
                key = f"{world['index']}:mutable"
                e = all_events[key]
                superseded.append(e["text"])
                e.update(text=world["correction"], revision=2)
                ids[key] = service.insert(e["text"], key, revision=2, context={k: e[k] for k in ("entities", "applies", "dependencies", "slot")}, token=service.owner)
                report["source_mapping"].append(dict(key=key, revision=2, event_id=ids[key]))
            service.await_idle()
        if phase == "redacted":
            for world in corpus:
                key = f"{world['index']}:erase"
                service.request(f"/v1/events/{ids[key]}/redact", {}, token=service.owner)
                all_events[key]["redacted"] = True
                response = probe(key + ":redacted_source", f"/v1/events/{ids[key]}", service.owner, 404)
                if all_events[key]["text"] in json.dumps(response, ensure_ascii=False):
                    report["violations"].append(dict(case=key, reason="redacted_source_text"))
            service.await_idle()
            for receipt_id, removed_claims in prior_receipts:
                receipt = service.request(f"/v1/receipts/{receipt_id}", token=service.user)
                if set(receipt["claim_ids"]) & removed_claims:
                    report["violations"].append(dict(case=receipt_id, reason="receipt_redaction"))
        if phase == "rebuilt":
            graph = service.request("/v1/graphs", {"name": "balanced-rebuild", "config": {"mode": "llm", "model": "mock-curator"}}, token=service.owner)["graph_id"]
            service.await_idle()
            service.request(f"/v1/graphs/{graph}/promote", {}, token=service.owner)
        for world in corpus:
            events = world["events"]
            world_ids = {e["key"]: ids[f"{world['index']}:{e['key']}"] for e in events}
            for case in world["cases"]:
                if case["phase"] != phase:
                    continue
                body = {k: case[k] for k in ("query", "entities", "context")}
                body.update(max_chars=BUDGET, agentic=False)
                start = time.monotonic()
                packet = service.request("/v1/query", body, token=getattr(service, case["actor"]))
                elapsed = time.monotonic() - start
                selected, violations = assess_packet(packet, case, events, world_ids)
                if any(text in json.dumps(packet, ensure_ascii=False) for text in superseded):
                    violations.append("superseded_packet_text")
                receipt = service.request(f"/v1/receipts/{packet['receipt_id']}", token=getattr(service, case["actor"]))
                if set(receipt["claim_ids"]) != {m["id"] for m in packet["memories"]}:
                    violations.append("receipt_evidence_mismatch")
                if case["family"] == "before_redaction":
                    removed_claims = {m["id"] for m in packet["memories"] if m["event_id"] == world_ids["erase"]}
                    if not removed_claims:
                        violations.append("redaction_probe_missing_setup_evidence")
                    prior_receipts.append((packet["receipt_id"], removed_claims))
                if case["family"] == "after_rebuild":
                    previous = next(r for r in report["rows"] if r["mode"] == "contextmesh" and r["id"] == f"{world['index']}:after_redaction")
                    if set(previous["selected"]) != set(selected):
                        violations.append("rebuild_evidence_divergence")
                for reason in violations:
                    report["violations"].append(dict(case=case["id"], reason=reason))
                for mode in ("contextmesh", "no_memory", "bm25_structured", "gold_evidence"):
                    chosen = selected if mode == "contextmesh" else [e["key"] for e in baseline(mode, events, case)]
                    row = dict(id=case["id"], world=case["world"], family=case["family"], mode=mode,
                               selected=sorted(chosen), expected=case["expected"], **score(chosen, case["expected"]))
                    if mode == "contextmesh":
                        row.update(packet=packet, query_seconds=elapsed)
                    report["rows"].append(row)
    report["final_graphs"] = service.request("/v1/graphs", token=service.owner)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=20260908)
    parser.add_argument("--worlds", type=int, default=8)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 2 <= args.worlds <= 100:
        parser.error("--worlds must be between 2 and 100")
    from service import Service
    corpus = generate(args.seed, args.worlds)
    frozen = json.dumps(corpus, sort_keys=True, separators=(",", ":"))
    report = dict(version=VERSION, seed=args.seed, worlds=args.worlds,
                  commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
                  corpus_sha256=hashlib.sha256(frozen.encode()).hexdigest(),
                  operating_point=dict(curator="deterministic fixture; not extraction quality", agentic=False,
                                       downstream_model=None, external_inference_calls=0, max_chars=BUDGET,
                                       python=platform.python_version(), platform=platform.platform(),
                                       fixture_sha256=hashlib.sha256((Path(__file__).resolve().parents[1] / "tests/e2e.py").read_bytes()).hexdigest(),
                                       comparison="structured evidence stage only; not an end-to-end task score"),
                  rows=[], violations=[], completed=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".corpus.json").write_text(frozen + "\n")
    try:
        with Service(mock=True) as service:
            run(service, corpus, report)
        report["completed"] = True
    finally:
        report["summary"] = summarize(report["rows"], args.seed)
        report["by_family"] = {family: summarize([r for r in report["rows"] if r["family"] == family], args.seed)
                               for family in sorted({r["family"] for r in report["rows"]})}
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(dict(completed=report["completed"], violations=len(report["violations"]), summary=report["summary"]), indent=2))
    if report["violations"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
