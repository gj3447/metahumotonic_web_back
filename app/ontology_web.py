"""Editorial web references for the pinned, internal apostle roster.

Slot identity, selected KG entity and public editorial concept remain distinct.
These links neither publish the internal snapshot nor assert owl:sameAs.
"""

from copy import deepcopy

from .ontology import OntologyProjection


WEB_ORIGIN = "https://metahumotonic.com"
# Explicit route registry, checked against selected names before serving.
# Positions come from the validated roster, never from parsing an opaque ID.
APOSTLE_WEB_ROUTES = {
    1: ("디멘션워커", "dimension-walker"),
    2: ("ICE ORCA DRAGON", "ice-orca-dragon"),
    3: ("초공동의 용사", "superhollow-knight"),
    4: ("비행기맨", "bhgman"),
    5: ("스페이스걸", "spacegirl"),
    6: ("인류역사흐름의강물", "great-river"),
    7: ("리퀘스트의 나무", "liquest-tree"),
    8: ("입체운행구름", "orbital-cloud"),
    9: (None, "jesus"),
    10: ("깊바존", "gipbajon"),
    11: ("HOH", "hoh"),
    12: ("몬순", "monsoon"),
}


def apostle_web_directory(projection: OntologyProjection) -> dict:
    """Return all twelve slots with separately attributed editorial references."""
    items = []
    for slot in sorted(projection.collections["apostles"], key=lambda s: s["position"]):
        position = slot["position"]
        expected_name, slug = APOSTLE_WEB_ROUTES[position]
        entity = slot["entity"]
        if position == 9:
            if entity is not None or slot["selection_state"] != "CONFLICT_PENDING":
                raise ValueError("The unresolved slot cannot select an editorial entity")
        elif entity is None or entity["canonical_name"] != expected_name:
            raise ValueError("The selected entity does not match the reviewed web route")
        items.append({
            **deepcopy(slot),
            "web_reference": {
                "concept_iri": f"{WEB_ORIGIN}/learn/#entity-apostle-{position}",
                "page_url": f"{WEB_ORIGIN}/apostles/{slug}/",
                "wiki_url": f"{WEB_ORIGIN}/wiki/apostles/{slug}/",
                "graph_url": f"{WEB_ORIGIN}/apostles/graph.jsonld",
                "authority": "EDITORIAL_SUMMARY",
                "mapping_status": (
                    "CONFLICT_REFERENCE_ONLY" if position == 9 else "EDITORIAL_REFERENCE"
                ),
                "identity_equivalence": False,
            },
        })
    return {"items": items, "mapping_version": "apostle-web-references/v1"}
