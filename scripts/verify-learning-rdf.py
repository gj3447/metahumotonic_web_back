# /// script
# requires-python = ">=3.11"
# dependencies = ["rdflib==7.2.1", "pyshacl==0.30.1"]
# ///
"""Offline RDF/SHACL readback of the reviewed public learning projection."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from rdflib import Dataset, Graph, Namespace, Literal, RDF, URIRef
from pyshacl import validate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('artifact', type=Path)
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    artifact = json.loads(args.artifact.read_bytes())
    ld = artifact['jsonld']
    base = 'https://metahumotonic.com/learn/'
    expected = {'@version': 1.1, 'skos': 'http://www.w3.org/2004/02/skos/core#', '@vocab': 'https://schema.org/',
                'mh': base + '#', 'rdf': str(RDF), 'prov': 'http://www.w3.org/ns/prov#'}
    assert ld['@context'] == expected, 'remote/redefined context refused'
    def guard(value):
        if isinstance(value, dict):
            assert '@context' not in value and '@import' not in value
            for child in value.values(): guard(child)
        elif isinstance(value, list):
            for child in value: guard(child)
    guard(ld['@graph'])
    dataset = Dataset()
    dataset.parse(data=json.dumps(ld), format='json-ld')
    graph = dataset.graph(URIRef(base + 'graph.jsonld'))
    assert len(graph) > 0, 'named graph must be nonempty'
    mh = Namespace(base + '#')
    skos = Namespace(expected['skos'])
    schema = Namespace('https://schema.org/')
    shapes_path = Path(__file__).resolve().parents[1] / 'engineering/learning-hub-shapes.ttl'
    shapes = Graph().parse(shapes_path, format='turtle')
    conforms, _, report = validate(graph, shacl_graph=shapes, inference='none', advanced=False)
    assert conforms, report
    assert len(set(graph.subjects(mh.reviewedAt))) == len(artifact['nodes'])
    assert len(set(graph.subjects(RDF.type, RDF.Statement))) == len(artifact['edges'])
    for node in artifact['nodes']:
        iri = URIRef(base + '#entity-' + node['id'])
        assert (iri, schema.name, Literal(node['title'])) in graph
        if node['kind'] == 'concept' and node['authority'] == 'PRIMARY_SOURCE':
            assert (iri, skos.definition, Literal(node['summary'], lang='ko')) in graph
        if node['kind'] == 'apostle':
            assert (iri, RDF.type, schema.Person) not in graph
            assert (iri, RDF.type, schema.SoftwareApplication) not in graph
    for edge in artifact['edges']:
        iri = URIRef(base + '#edge-' + edge['id'])
        assert (iri, RDF.subject, URIRef(base + '#entity-' + edge['from'])) in graph
        assert (iri, RDF.object, URIRef(base + '#entity-' + edge['to'])) in graph
    # Mutations must fail: source-less public entities and missing relation state.
    controls = []
    for subject, predicate in [(next(graph.subjects(mh.reviewedAt)), Namespace(expected['prov']).wasDerivedFrom),
                               (next(graph.subjects(RDF.type, RDF.Statement)), mh.status)]:
        mutant = Graph()
        for triple in graph:
            if triple[:2] != (subject, predicate): mutant.add(triple)
        assert not validate(mutant, shacl_graph=shapes, inference='none', advanced=False)[0]
        controls.append(str(predicate))
    receipt = dict(schema='metahumotonic/learning-rdf-verification@1', observedAt=datetime.now(timezone.utc).isoformat(),
                   scope='offline named RDF graph and SHACL; no resolver I/O or certification', status='PASS',
                   nodes=len(artifact['nodes']), relations=len(artifact['edges']), triples=len(graph),
                   sourceDigest=artifact['sourceDigest'], shapesSha256=hashlib.sha256(shapes_path.read_bytes()).hexdigest(),
                   verifierSha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), negativeControls=controls)
    if args.receipt:
        with args.receipt.open('x') as f: json.dump(receipt, f, ensure_ascii=False, indent=2)
    print(json.dumps(receipt, ensure_ascii=False))

if __name__ == '__main__': main()
