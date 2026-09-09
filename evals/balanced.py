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

VERSION = "cmes-records-paired-v2"
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
        case("indirect_quality", "capacity", ["direct"], query="What limit should I use?", entities=[])
        case("missing", "absent", [])
        event("prod", "deploy", f"Production rollout requires ticket {value}.", applies={"project": "prod"})
        event("stage", "deploy", f"Staging rollout requires ticket {alternate}.", applies={"project": "stage"})
        case("scope_match", "deploy", ["prod"], {"project": "prod"})
        case("scope_sibling", "deploy", ["stage"], {"project": "stage"})
        case("no_scope_filter", "deploy", ["prod", "stage"])
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
    project = case["context"].get("project")
    return project is None or event["applies"].get("project") == project


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
        if score:
            ranked.append((-score, event["key"], event))
    chosen, used = [], 0
    for _, _, event in sorted(ranked):
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
    """Validate only authorized, source-grounded records in a context packet."""
    by_id = {ids[e["key"]]: e for e in events}
    selected, violations, record_ids = [], [], []
    for record in packet.get("records", []):
        record_id = record.get("id")
        record_ids.append(record_id)
        supports = record.get("supports", [])
        evidence_ids = [record_id] if record_id in by_id else [
            support.get("record_id") for support in supports
            if support.get("record_id") in by_id
        ]
        if not evidence_ids:
            violations.append("unknown_or_redacted_source")
            continue
        if supports and not all(x.get("record_id") in by_id and x.get("quote", "") in by_id[x["record_id"]]["text"] for x in supports):
            violations.append("invalid_provenance")
        for evidence_id in evidence_ids:
            event = by_id[evidence_id]
            selected.append(event["key"])
            if not visible(event, case):
                violations.append("unauthorized_or_redacted_source")
    if len(record_ids) != len(set(record_ids)):
        violations.append("duplicate_record")
    selected = list(dict.fromkeys(selected))
    if case["entities"] and not set(case["expected"]).issubset(selected):
        violations.append("missing_required_evidence")
    rendered = json.dumps(packet, ensure_ascii=False)
    if any(e["text"] in rendered for e in events if not visible(e, case)):
        violations.append("unauthorized_or_redacted_packet_text")
    return selected, violations


def run(service, corpus, report):
    ids = {}
    corrections = {}
    report["source_mapping"], report["probes"] = [], []

    def probe(name, path, token, expected, body=None):
        status, result = service.request_raw(path, body, token=token)
        passed = status == expected
        report["probes"].append(dict(name=name, status=status, expected=expected, passed=passed))
        if not passed:
            report["violations"].append(dict(case=name, reason="source_visibility"))
        return result

    for world in corpus:
        for event in world["events"]:
            key = f"{world['index']}:{event['key']}"
            token = service.outsider if event["tenant"] == "other" else service.owner
            scope = {"visibility": event["classification"], "groups": event["read_groups"], **event["applies"]}
            ids[key] = service.insert(event["text"], key, context={"scope": scope, "entities": event["entities"]}, classification=event["classification"], read_groups=event["read_groups"], token=token)
            report["source_mapping"].append(dict(key=key, record_id=ids[key]))
    service.await_idle()
    for world in corpus:
        prefix = f"{world['index']}:"
        probe(prefix + "restricted_denied", f"/v1/records/{ids[prefix + 'restricted']}", service.guest, 404)
        probe(prefix + "restricted_allowed", f"/v1/records/{ids[prefix + 'restricted']}", service.user, 200)
        probe(prefix + "foreign_denied", f"/v1/records/{ids[prefix + 'foreign']}", service.owner, 404)
        probe(prefix + "foreign_allowed", f"/v1/records/{ids[prefix + 'foreign']}", service.outsider, 200)
    superseded = []
    for phase in ("initial", "corrected", "redacted"):
        if phase == "corrected":
            for world in corpus:
                key = f"{world['index']}:mutable"; event = next(e for e in world["events"] if e["key"] == "mutable")
                superseded.append(event["text"])
                new_key = key + ":corrected"
                corrections[world["index"]] = dict(event, key="mutable:corrected", text=world["correction"])
                ids[new_key] = service.insert(world["correction"], new_key, token=service.owner,
                                              inputs=[ids[key]], supersedes=[ids[key]])
                report["source_mapping"].append(dict(key=new_key, record_id=ids[new_key]))
            service.await_idle()
        if phase == "redacted":
            for world in corpus:
                key = f"{world['index']}:erase"
                service.request(f"/v1/records/{ids[key]}/redact", {}, token=service.owner)
                next(e for e in world["events"] if e["key"] == "erase")["redacted"] = True
            service.await_idle()
        for world in corpus:
            prefix = f"{world['index']}:"
            events = [dict(event, key=prefix + event["key"]) for event in world["events"]]
            if phase in ("corrected", "redacted"):
                events.append(dict(corrections[world["index"]], key=prefix + "mutable:corrected"))
            for case in world["cases"]:
                if case["phase"] != phase: continue
                case = dict(case, expected=[prefix + key + (":corrected" if key == "mutable" and phase != "initial" else "") for key in case["expected"]])
                actor = getattr(service, case["actor"])
                request = {"task": case["query"], "max_tokens": BUDGET}
                if case["context"].get("project"):
                    request["scopes"] = [{"project": case["context"]["project"]}]
                packet = service.request("/v1/context", request, token=actor)
                selected, violations = assess_packet(packet, case, events, ids)
                if any(text in json.dumps(packet, ensure_ascii=False) for text in superseded): violations.append("superseded_packet_text")
                for reason in violations: report["violations"].append(dict(case=case["id"], reason=reason))
                for mode in ("contextmesh", "no_memory", "bm25_structured", "gold_evidence"):
                    chosen = selected if mode == "contextmesh" else [e["key"] for e in baseline(mode, events, case)]
                    report["rows"].append(dict(id=case["id"], world=case["world"], family=case["family"], mode=mode, selected=sorted(chosen), expected=case["expected"], **score(chosen, case["expected"])))

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
