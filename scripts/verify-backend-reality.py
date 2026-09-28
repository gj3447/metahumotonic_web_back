# /// script
# requires-python = ">=3.11"
# dependencies = ["rdflib==7.2.1", "pyshacl==0.30.1"]
# ///
"""Independent RDF/SHACL and source-receipt check for the backend reality graph."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from pyshacl import validate
from rdflib import Dataset, Graph, Literal, Namespace, RDF, URIRef


ROOT = Path(__file__).resolve().parents[1]
NS = "https://metahumotonic.com/vocab/backend-reality#"
CONTEXT = {"@version": 1.1, "mh": NS, "schema": "https://schema.org/",
           "prov": "http://www.w3.org/ns/prov#", "rdf": str(RDF),
           "xsd": "http://www.w3.org/2001/XMLSchema#"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def iri(value, scope="node"):
    return URIRef("urn:metahumotonic:backend-reality:" + scope + ":" + quote(value, safe=""))


def check_source(reference, kind):
    prefix, separator, locator = reference.partition(":")
    require(separator and prefix in {"repo", "doc", "receipt"}, "unsupported source reference")
    relative, _, pointer = locator.partition("#")
    if prefix == "repo":
        require(kind in {"source", "test", "document"}, "repository evidence kind mismatch")
        if kind == "test":
            require(relative.startswith(("ts/test/", "tests/")), "test reference outside test suite")
        if kind == "document":
            require(relative.endswith(".md"), "document reference is not Markdown")
    else:
        require(kind == {"doc": "document", "receipt": "runtime"}[prefix], "evidence kind mismatch")
    file = (ROOT / relative).resolve()
    require(file.is_relative_to(ROOT) and file.is_file(), "source file missing or outside repository")
    if prefix == "receipt" and pointer:
        value = json.loads(file.read_text())
        require(pointer.startswith("/"), "invalid receipt pointer")
        for segment in pointer.removeprefix("/").split("/"):
            require(isinstance(value, dict) and segment in value, "missing receipt pointer")
            value = value[segment]


def evaluate(node, at):
    evidence = sorted(node.get("runtimeEvidence", []), key=lambda row: row["observedAt"], reverse=True)
    if not evidence:
        return "unverified"
    item = evidence[0]
    observed = datetime.fromisoformat(item["observedAt"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(item["expiresAt"].replace("Z", "+00:00"))
    if at < observed:
        return "unverified"
    if at >= expires:
        return "stale"
    return item["outcome"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonld", type=Path)
    args = parser.parse_args()
    catalog_file = ROOT / "ts/config/backend-reality.json"
    document = json.loads(args.jsonld.read_text())
    catalog = json.loads(catalog_file.read_text())
    require(document.get("@context") == CONTEXT, "unexpected or remote JSON-LD context")

    def guard(value):
        if isinstance(value, dict):
            require("@context" not in value and "@import" not in value, "nested JSON-LD context")
            for child in value.values():
                guard(child)
        elif isinstance(value, list):
            for child in value:
                guard(child)
    guard({key: value for key, value in document.items() if key != "@context"})
    require(document.get("@id") == str(iri("assessment", "document")), "unexpected graph identity")
    evaluated_at = datetime.fromisoformat(document["mh:evaluatedAt"]["@value"].replace("Z", "+00:00"))
    dataset = Dataset()
    dataset.parse(data=json.dumps(document), format="json-ld")
    data = dataset.graph(URIRef(document["@id"]))
    require(len(data) > 0, "empty named graph")
    shapes = Graph().parse(ROOT / "engineering/backend-reality-shapes.ttl", format="turtle")
    conforms, _, report = validate(data, shacl_graph=shapes, inference="none", advanced=False)
    require(conforms, str(report))
    mh, schema, prov = Namespace(NS), Namespace(CONTEXT["schema"]), Namespace(CONTEXT["prov"])
    platform = json.loads((ROOT / "ts/config/platform-catalog.json").read_text())
    platform_ids = {row["id"] for row in platform["nodes"]}
    require(len({node["id"] for node in catalog["nodes"]}) == len(catalog["nodes"]), "duplicate component ID")
    require(len({edge["id"] for edge in catalog["edges"]}) == len(catalog["edges"]), "duplicate relation ID")
    for node in catalog["nodes"]:
        subject = iri(node["id"])
        require((subject, RDF.type, mh.Component) in data, "component type missing")
        require((subject, schema.identifier, Literal(node["id"])) in data, "component identity lost")
        for field in ["implementation", "runtime", "decision"]:
            require((subject, mh[field], Literal(node[field])) in data, "component classification lost")
        require((subject, mh.evidenceState, Literal(evaluate(node, evaluated_at))) in data, "evidence freshness mismatch")
        if "platformNodeId" in node:
            require(node["platformNodeId"] in platform_ids, "unknown platform bridge")
            platform_ref = URIRef("urn:metahumotonic:platform:node:" + quote(node["platformNodeId"], safe=""))
            require((subject, mh.platformReference, platform_ref) in data, "platform identity bridge lost")
        if node["runtime"] == "production-observed":
            require(node.get("runtimeEvidence"), "unproved production label")
        if node["implementation"] == "simulation":
            require(node["runtime"] == "development-only", "simulation promoted")
        for evidence in node["evidence"]:
            check_source(evidence["source"], evidence["kind"])
        for index, evidence in enumerate(node.get("runtimeEvidence", [])):
            check_source(evidence["source"], "runtime")
            subject_evidence = iri(f"{node['id']}:{index}", "runtime")
            require((subject, mh.runtimeEvidence, subject_evidence) in data, "runtime evidence lost")
            require((subject_evidence, mh.expiresAt, Literal(evidence["expiresAt"], datatype=Namespace(CONTEXT["xsd"]).dateTime)) in data,
                    "runtime expiry lost")
    for edge in catalog["edges"]:
        subject = iri(edge["id"], "edge")
        require((subject, RDF.type, RDF.Statement) in data, "relationship reification lost")
        require((subject, RDF.subject, iri(edge["from"])) in data, "relationship source lost")
        require((subject, RDF.object, iri(edge["to"])) in data, "relationship target lost")
        require((subject, mh.status, Literal(edge["status"])) in data, "relationship status lost")
        require((iri(edge["from"]), mh[edge["relation"]], iri(edge["to"])) not in data,
                "declared relation silently asserted")
        for evidence in edge["evidence"]:
            check_source(evidence["source"], evidence["kind"])
    receipt = json.loads((ROOT / "docs/evidence/backend-reality-2026-09-28.json").read_text())
    for name in ["web-back-pve-1", "web-back-pve-2"]:
        runtime = receipt["vm100"][name]
        require(runtime["command"][:2] == ["uvicorn", "app.main:app"] and runtime["state"] == "running"
                and runtime["health"] == "healthy" and runtime["healthHttpStatus"] == 200 and runtime["readyHttpStatus"] == 200,
                "Python production receipt does not support claim")
        flags = runtime["readyFlags"]
        require(flags["wiki_live"] is True and flags["wiki_store_live"] is True and runtime["wikiListHttpStatus"] == 200,
                "Wiki production receipt does not support claim")
        require(runtime["researchSummaryHttpStatus"] == 200 and runtime["researchSummarySource"] == "live",
                "research production receipt does not support claim")
        require(flags["ontology_required"] is False and flags["ontology_live"] is False,
                "ontology disabled-state receipt differs from claim")
    registry = receipt["public"]["api-mcp-status"]
    require(registry["httpStatus"] == 200 and registry["source"] == "live" and registry["total"] == registry["serverCount"] == 0
            and registry["lastVerifyAt"] is None, "empty MCP registry claim differs from receipt")
    statement = next(data.subjects(RDF.type, RDF.Statement))
    runtime = next(data.subjects(RDF.type, mh.RuntimeEvidence))
    for subject, predicate in [(statement, mh.status), (runtime, mh.expiresAt)]:
        mutant = Graph()
        for triple in data:
            if triple[0] != subject or triple[1] != predicate:
                mutant.add(triple)
        require(not validate(mutant, shacl_graph=shapes, inference="none", advanced=False)[0], "negative SHACL control passed")
    print(json.dumps({"status": "PASS", "scope": "offline implementation and dated runtime evidence; no current production claim",
                      "components": len(catalog["nodes"]), "relationships": len(catalog["edges"]),
                      "runtimeEvidence": sum(len(node.get("runtimeEvidence", [])) for node in catalog["nodes"]),
                      "rdfTriples": len(data), "negativeControls": 2,
                      "catalogSha256": hashlib.sha256(catalog_file.read_bytes()).hexdigest(),
                      "jsonldSha256": hashlib.sha256(args.jsonld.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
