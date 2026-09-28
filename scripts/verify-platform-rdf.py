# /// script
# requires-python = ">=3.11"
# dependencies = ["rdflib==7.2.1", "pyshacl==0.30.1"]
# ///
"""Independent offline RDF/SHACL verification of the TS platform projection."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

from pyshacl import validate
from rdflib import Dataset, Graph, Literal, Namespace, RDF, URIRef


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonld", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    catalog_file = root / "ts/config/platform-catalog.json"
    shapes_file = root / "engineering/platform-shapes.ttl"
    catalog = json.loads(catalog_file.read_text())
    document = json.loads(args.jsonld.read_text())
    # Do not let a remote JSON-LD context or import turn validation into network I/O.
    expected_context = {"@version": 1.1, "mh": "https://metahumotonic.com/vocab/platform#",
                        "schema": "https://schema.org/", "prov": "http://www.w3.org/ns/prov#",
                        "rdf": str(RDF), "xsd": "http://www.w3.org/2001/XMLSchema#"}
    require(document.get("@context") == expected_context, "unexpected or remote context")
    def guard(value):
        if isinstance(value, dict):
            require("@context" not in value and "@import" not in value, "nested context forbidden")
            for child in value.values():
                guard(child)
        elif isinstance(value, list):
            for child in value:
                guard(child)
    guard({key: value for key, value in document.items() if key != "@context"})
    # @id + @graph is a named RDF graph; Graph.parse would inspect only the
    # default graph's dataset metadata and could make SHACL pass vacuously.
    dataset = Dataset()
    dataset.parse(data=json.dumps(document), format="json-ld")
    require(document.get("@id") == "urn:metahumotonic:platform:document:catalog", "unexpected graph identity")
    data = dataset.graph(URIRef(document["@id"]))
    require(len(data) > 0, "empty named platform graph")
    shapes = Graph().parse(shapes_file, format="turtle")
    conforms, _, report = validate(data, shacl_graph=shapes, inference="none", advanced=False)
    require(conforms, report)
    mh, schema, prov = map(Namespace, (expected_context["mh"], expected_context["schema"], expected_context["prov"]))
    def iri(value, scope="node"):
        return URIRef("urn:metahumotonic:platform:" + scope + ":" + quote(value, safe=""))
    for node in catalog["nodes"]:
        subject = iri(node["id"])
        require((subject, RDF.type, mh[node["kind"]]) in data, "missing node kind")
        require((subject, schema.identifier, Literal(node["id"])) in data, "missing original node identity")
    for edge in catalog["edges"]:
        statement = iri(edge["id"], "edge")
        require((statement, RDF.subject, iri(edge["from"])) in data, "lost edge direction")
        require((statement, RDF.object, iri(edge["to"])) in data, "lost edge target")
        require((statement, mh.status, Literal(edge["status"])) in data, "lost relation status")
        require((iri(edge["from"]), mh[edge["relation"]], iri(edge["to"])) not in data,
                "a described relationship was silently asserted")
    for observation in catalog.get("observations", []):
        subject = iri(observation["id"], "observation")
        require((subject, mh.subject, iri(observation["subjectId"])) in data, "lost observed subject")
        require((subject, mh.outcome, Literal(observation["outcome"])) in data, "lost observed outcome")
    # Prove shapes reject a missing expiry and a claim without status, not just valid output.
    observation = next(data.subjects(RDF.type, mh.Observation))
    statement = next(data.subjects(RDF.type, RDF.Statement))
    for subject, predicate in [(observation, mh.expiresAt), (statement, mh.status)]:
        mutant = Graph()
        for triple in data:
            if not (triple[0] == subject and triple[1] == predicate):
                mutant.add(triple)
        require(not validate(mutant, shacl_graph=shapes, inference="none", advanced=False)[0], "negative SHACL control passed")
    print(json.dumps({"status": "PASS", "scope": "offline RDF projection and SHACL; no KG write or runtime claim",
                      "nodes": len(catalog["nodes"]), "relationships": len(catalog["edges"]),
                      "observations": len(catalog.get("observations", [])), "rdfTriples": len(data), "negativeControls": 2,
                      "catalogSha256": hashlib.sha256(catalog_file.read_bytes()).hexdigest(),
                      "jsonldSha256": hashlib.sha256(args.jsonld.read_bytes()).hexdigest(),
                      "shapesSha256": hashlib.sha256(shapes_file.read_bytes()).hexdigest()}))


if __name__ == "__main__":
    main()
