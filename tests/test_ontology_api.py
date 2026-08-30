"""Contract tests for the internal conflict-aware ontology facade."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from app.ontology import (
    PROJECTION_ID,
    PUBLICATION_STATUS,
    RELEASE_STATE,
    SCHEMA_VERSION,
    OntologyProjection,
    OntologyProjectionError,
    ontology_runtime,
)


INTERNAL_KEY = "ontology-test-key-that-is-at-least-thirty-two-bytes"
CONTENT_SHA256 = "a" * 64


def _public_id(number: int) -> str:
    return f"mtg1-{number:016x}"


def _snapshot() -> dict:
    axioms = [
        {
            "public_id": _public_id(index),
            "position": index,
            "canonical_name": f"공통 축 {index}",
            "binding_state": "BOUND",
            "body_policy": "SERVE_USER_PRIMARY",
            "content_authority": "USER_PRIMARY",
            "definition": f"안전한 정의 {index}",
            "identity_authority": "USER_PRIMARY",
            "status": "ACTIVE_SOURCE",
        }
        for index in range(1, 13)
    ]

    apostle_names = {
        1: "디멘션워커",
        2: "ICE ORCA DRAGON",
        3: "초공동의 용사",
        4: "비행기맨",
        5: "스페이스걸",
        6: "인류역사흐름의강물",
        7: "리퀘스트의 나무",
        8: "입체운행구름",
        10: "깊바존",
        11: "HOH",
        12: "몬순",
    }
    apostles = []
    for position in range(1, 13):
        public_id = _public_id(100 + position)
        if position == 9:
            apostles.append(
                {
                    "public_id": public_id,
                    "position": position,
                    "structural_group": "desire_salvation",
                    "selection_state": "CONFLICT_PENDING",
                    "body_policy": "NO_DEFAULT",
                    "entity": None,
                    "candidates": [
                        {
                            "candidate_id": "apostle-09-candidate-jesus",
                            "canonical_name": "예수",
                            "authority": "PROJECT_CANON",
                            "binding_state": "UNBOUND_PENDING",
                            "default_servable": False,
                        },
                        {
                            "candidate_id": "apostle-09-candidate-aten",
                            "canonical_name": "검은 태양신 아텐",
                            "authority": "USER_PRIMARY",
                            "binding_state": "BOUND_LEGACY_CANDIDATE",
                            "default_servable": False,
                        },
                    ],
                }
            )
            continue
        entity = {
            "canonical_name": apostle_names[position],
            "aliases": [f"apostle {position}"],
            "binding_state": "BOUND",
            "body_policy": "SERVE_SOURCE_REFERENCES",
            "content_authority": "USER_PRIMARY",
            "identity_authority": "USER_PRIMARY",
        }
        if position == 8:
            entity["aliases"] = ["Orbital Motion Cloud", "OMC"]
            entity["canonical_abbreviation"] = "OMC"
        apostles.append(
            {
                "public_id": public_id,
                "position": position,
                "structural_group": "test_group",
                "selection_state": "SELECTED",
                "entity": entity,
            }
        )

    legion_names = [
        "Prometheus",
        "Longinus",
        "Eureka",
        "Occam",
        "Naesengmoon",
        "JaebaeMan",
        "Harness",
    ]
    legion_commanders = []
    for position, name in enumerate(legion_names, 1):
        record = {
            "public_id": _public_id(200 + position),
            "position": position,
            "canonical_name": name,
            "binding_state": "BOUND",
            "status": "ACTIVE",
            "verb": f"verb-{position}",
        }
        if name == "Harness":
            record["aliases"] = ["하네스", "Hades", "하데스"]
            record["identity_rule"] = "ONE_ENTITY_ONE_SLOT"
        legion_commanders.append(record)

    omc_names = ["333", "CHU", "CRL", "engineboy", "orrr", "HSWM"]
    omc_commanders = []
    for position, name in enumerate(omc_names, 1):
        record = {
            "public_id": _public_id(300 + position),
            "canonical_name": name,
            "binding_state": "BOUND",
            "status": "ACTIVE",
        }
        if name == "HSWM":
            record["scope_note"] = (
                "This is identity scope, not a claim that the implementation is complete."
            )
        omc_commanders.append(record)

    phases = [
        {
            "public_id": _public_id(401),
            "canonical_name": "공허진동자",
            "binding_state": "BOUND_PHASE_ENTITY_WITH_ORPHAN_PARENT_EDGE",
            "body_policy": "SERVE_SOURCE_REFERENCES",
            "content_authority": "USER_PRIMARY",
            "status": "ACTIVE_AS_PHASE",
        },
        {
            "public_id": _public_id(402),
            "canonical_name": "검은 태양신 아텐",
            "binding_state": "BOUND_PHASE_ENTITY",
            "body_policy": "SERVE_SOURCE_REFERENCES",
            "content_authority": "USER_PRIMARY",
            "status": "ACTIVE_AS_PHASE",
        },
    ]
    structural_patterns = [
        {
            "public_id": _public_id(500 + position),
            "canonical_name": f"Structural Pattern {position}",
            "binding_state": "BOUND",
            "body_policy": "REFERENCE_ONLY",
            "status": "ACTIVE_REFERENCE",
        }
        for position in range(1, 5)
    ]

    relationships = []
    for commander in legion_commanders:
        relationships.append(
            {
                "relationship_id": f"rel-legion-{commander['position']}",
                "from_public_id": _public_id(104),
                "predicate": "COMMANDS_LEGION",
                "to_public_id": commander["public_id"],
                "authority": "PROJECT_CANON_WITH_USER_VERDICT",
                "status": "ACTIVE",
            }
        )
    for position, commander in enumerate(omc_commanders, 1):
        relationships.append(
            {
                "relationship_id": f"rel-omc-{position}",
                "from_public_id": _public_id(108),
                "predicate": "HAS_DIRECT_COMMANDER",
                "to_public_id": commander["public_id"],
                "authority": "USER_PRIMARY",
                "status": "ACTIVE",
            }
        )
    for position, phase in enumerate(phases, 1):
        phase_policy = (
            ("USER_PRIMARY", "PROJECTED_FROM_USER_SOURCE")
            if phase["canonical_name"] == "공허진동자"
            else ("PROJECT_CANON", "PROJECTED_FROM_PROJECT_CANON")
        )
        relationships.append(
            {
                "relationship_id": f"rel-phase-{position}",
                "from_public_id": phase["public_id"],
                "predicate": "PHASE_OF",
                "to_public_id": _public_id(110),
                "authority": phase_policy[0],
                "status": phase_policy[1],
            }
        )

    conflicts = [
        {
            "conflict_id": f"conflict-{index}",
            "detail": f"Sanitized conflict detail {index}",
            "severity": "WARN" if index != 2 else "BLOCK_PUBLIC_DEFAULT",
            "status": "OPEN_CONTAINED_BY_PROJECTION",
            **(
                {"subject_public_id": _public_id(109)}
                if index == 2
                else {"subject_scope": "INTERNAL_REFERENCE_REDACTED"}
            ),
        }
        for index in range(1, 9)
    ]

    return {
        "meta": {
            "schema_version": SCHEMA_VERSION,
            "projection_id": PROJECTION_ID,
            "release_state": RELEASE_STATE,
            "publication_status": PUBLICATION_STATUS,
            "content_sha256": CONTENT_SHA256,
        },
        "data": {
            "collections": {
                "axioms": axioms,
                "apostles": apostles,
                "legion_commanders": legion_commanders,
                "omc_commanders": omc_commanders,
                "phases": phases,
                "structural_patterns": structural_patterns,
            },
            "relationships": relationships,
            "conflicts": conflicts,
        },
    }


@pytest.fixture(autouse=True)
def ontology_projection():
    projection = OntologyProjection.from_mapping(
        _snapshot(), cursor_secret=INTERNAL_KEY.encode("utf-8")
    )
    ontology_runtime.install_for_testing(projection, internal_key=INTERNAL_KEY)
    yield projection
    ontology_runtime.reset()


def _headers(**extra: str) -> dict[str, str]:
    return {"X-Ontology-Key": INTERNAL_KEY, **extra}


async def test_disabled_surface_is_503_and_request_id_is_correlated(client):
    ontology_runtime.reset()
    response = await client.get("/api/v1/ontology/schema")
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "ONTOLOGY_UNAVAILABLE"
    assert body["error"]["request_id"] == response.headers["X-Request-ID"]


async def test_readiness_fails_closed_if_enabled_runtime_is_unavailable(
    client, monkeypatch
):
    ontology_runtime.reset()
    monkeypatch.setattr("app.routers.meta.settings.ontology_enabled", True)
    response = await client.get("/ready")
    assert response.status_code == 503
    assert response.json()["ontology_required"] is True
    assert response.json()["ontology_live"] is False
    assert response.json()["degraded"] is True


async def test_surface_requires_its_independent_internal_key(client):
    missing = await client.get("/api/v1/ontology/schema")
    wrong = await client.get(
        "/api/v1/ontology/schema", headers={"X-Ontology-Key": "wrong"}
    )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.headers["Cache-Control"] == "private, no-store"


async def test_search_ranks_and_uses_context_bound_signed_cursors(client):
    first = await client.get(
        "/api/v1/ontology/search",
        params={"q": "공통", "kind": "axiom", "limit": "1"},
        headers=_headers(),
    )
    assert first.status_code == 200
    assert len(first.json()["data"]["items"]) == 1
    cursor = first.json()["page"]["next_cursor"]
    assert cursor

    second = await client.get(
        "/api/v1/ontology/search",
        params={"q": "공통", "kind": "axiom", "limit": "1", "cursor": cursor},
        headers=_headers(),
    )
    assert second.status_code == 200
    assert (
        second.json()["data"]["items"][0]["public_id"]
        != first.json()["data"]["items"][0]["public_id"]
    )

    cross_query = await client.get(
        "/api/v1/ontology/search",
        params={"q": "shared", "kind": "axiom", "limit": "1", "cursor": cursor},
        headers=_headers(),
    )
    tampered = await client.get(
        "/api/v1/ontology/search",
        params={
            "q": "공통",
            "kind": "axiom",
            "limit": "1",
            "cursor": f"{cursor[:-1]}x",
        },
        headers=_headers(),
    )
    assert cross_query.status_code == 400
    assert tampered.status_code == 400


async def test_search_validation_is_400_not_framework_422(client):
    short = await client.get(
        "/api/v1/ontology/search", params={"q": "a"}, headers=_headers()
    )
    bad_limit = await client.get(
        "/api/v1/ontology/search",
        params={"q": "shared", "limit": "lots"},
        headers=_headers(),
    )
    assert short.status_code == 400
    assert bad_limit.status_code == 400


async def test_om_does_not_resolve_the_current_omc_entity(client):
    historical = await client.get(
        "/api/v1/ontology/search", params={"q": "OM"}, headers=_headers()
    )
    current = await client.get(
        "/api/v1/ontology/search", params={"q": "OMC"}, headers=_headers()
    )
    assert historical.status_code == 200
    assert _public_id(108) not in {
        item["public_id"] for item in historical.json()["data"]["items"]
    }
    assert current.status_code == 200
    assert _public_id(108) in {
        item["public_id"] for item in current.json()["data"]["items"]
    }


async def test_node_etag_304_and_unknown_404(client):
    response = await client.get(
        f"/api/v1/ontology/nodes/{_public_id(108)}", headers=_headers()
    )
    assert response.status_code == 200
    assert response.json()["data"]["entity"]["canonical_abbreviation"] == "OMC"
    etag = response.headers["ETag"]
    cached = await client.get(
        f"/api/v1/ontology/nodes/{_public_id(108)}",
        headers=_headers(**{"If-None-Match": etag}),
    )
    unknown = await client.get(
        "/api/v1/ontology/nodes/mtg1-ffffffffffffffff", headers=_headers()
    )
    other_node = await client.get(
        f"/api/v1/ontology/nodes/{_public_id(107)}",
        headers=_headers(**{"If-None-Match": etag}),
    )
    assert cached.status_code == 304
    assert cached.content == b""
    assert unknown.status_code == 404
    assert other_node.status_code == 200
    assert other_node.headers["ETag"] != etag


async def test_slot_9_returns_explicit_409_without_a_default(client):
    response = await client.get(
        f"/api/v1/ontology/nodes/{_public_id(109)}", headers=_headers()
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "CONFLICT_PENDING"
    details = body["error"]["details"]
    assert details["selection_state"] == "CONFLICT_PENDING"
    assert details["default_servable"] is False
    assert {item["canonical_name"] for item in details["candidates"]} == {
        "예수",
        "검은 태양신 아텐",
    }
    assert all(item["default_servable"] is False for item in details["candidates"])


async def test_neighbors_support_direction_predicate_and_cursor(client):
    response = await client.get(
        f"/api/v1/ontology/nodes/{_public_id(104)}/neighbors",
        params={"direction": "out", "predicate": "COMMANDS_LEGION", "limit": "2"},
        headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]["items"]) == 2
    assert all(item["direction"] == "out" for item in body["data"]["items"])
    cursor = body["page"]["next_cursor"]
    assert cursor
    next_page = await client.get(
        f"/api/v1/ontology/nodes/{_public_id(104)}/neighbors",
        params={
            "direction": "out",
            "predicate": "COMMANDS_LEGION",
            "limit": "2",
            "cursor": cursor,
        },
        headers=_headers(),
    )
    assert next_page.status_code == 200


async def test_schema_and_immutable_release_expose_conflict_aware_roster(client):
    schema = await client.get("/api/v1/ontology/schema", headers=_headers())
    release = await client.get(
        f"/api/v1/ontology/releases/{PROJECTION_ID}", headers=_headers()
    )
    assert schema.status_code == 200
    assert schema.json()["data"]["identity_rules"]["omc_abbreviation"] == "OMC"
    assert release.status_code == 200
    release_data = release.json()["data"]
    assert release_data["counts"]["collections"]["apostles"] == 12
    assert release_data["counts"]["conflicts"] == 8
    slot9 = next(
        item
        for item in release_data["collections"]["apostles"]
        if item["position"] == 9
    )
    assert slot9["canonical_name"] == "미결"
    assert slot9["default_servable"] is False
    assert "immutable" in release.headers["Cache-Control"]


def test_loader_checks_byte_digest_and_rejects_private_material(tmp_path):
    snapshot = _snapshot()
    raw = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    path = tmp_path / "projection.json"
    path.write_bytes(raw)

    with pytest.raises(OntologyProjectionError, match="byte digest mismatch"):
        OntologyProjection.load(
            path,
            expected_file_sha256="b" * 64,
            expected_content_sha256=CONTENT_SHA256,
            cursor_secret=INTERNAL_KEY.encode("utf-8"),
        )

    loaded = OntologyProjection.load(
        path,
        expected_file_sha256=hashlib.sha256(raw).hexdigest(),
        expected_content_sha256=CONTENT_SHA256,
        cursor_secret=INTERNAL_KEY.encode("utf-8"),
    )
    assert loaded.content_sha256 == CONTENT_SHA256

    leaked = deepcopy(snapshot)
    leaked["data"]["collections"]["axioms"][0]["stable_ref"] = "sym:private"
    with pytest.raises(OntologyProjectionError, match="forbidden private field"):
        OntologyProjection.from_mapping(
            leaked,
            cursor_secret=INTERNAL_KEY.encode("utf-8"),
        )


def test_exact_dto_allowlist_rejects_unknown_fields_and_rewired_graph():
    private_field = _snapshot()
    private_field["data"]["collections"]["axioms"][0]["private_note"] = "secret"
    with pytest.raises(OntologyProjectionError, match="unexpected public DTO field"):
        OntologyProjection.from_mapping(
            private_field,
            cursor_secret=INTERNAL_KEY.encode("utf-8"),
        )

    private_relationship = _snapshot()
    private_relationship["data"]["relationships"][0]["token"] = "secret"
    with pytest.raises(OntologyProjectionError, match="unexpected public DTO field"):
        OntologyProjection.from_mapping(
            private_relationship,
            cursor_secret=INTERNAL_KEY.encode("utf-8"),
        )

    rewired = _snapshot()
    rewired["data"]["relationships"][0]["from_public_id"] = _public_id(105)
    with pytest.raises(OntologyProjectionError, match="legion commander topology"):
        OntologyProjection.from_mapping(
            rewired,
            cursor_secret=INTERNAL_KEY.encode("utf-8"),
        )
