"""Validated, conflict-aware Metahumotonic ontology snapshot runtime.

The public web backend must never query or serialize raw KG records for this
surface.  It loads the already-sanitized browser projection once at startup,
checks both operator-pinned digests and the ontology invariants, and then serves
only indexed copies of that snapshot.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "metahumotonic-public-graph/v1"
PROJECTION_ID = "metahumotonic-public-graph-v1-2026-08-30"
RELEASE_STATE = "ACTIVE_INTERNAL_CONFLICT_AWARE"
PUBLICATION_STATUS = "INTERNAL_ONLY"

PUBLIC_ID_RE = re.compile(r"^mtg1-[a-f0-9]{16}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

COLLECTION_KINDS = {
    "axioms": "axiom",
    "apostles": "apostle",
    "legion_commanders": "legion_commander",
    "omc_commanders": "omc_commander",
    "phases": "phase",
    "structural_patterns": "structural_pattern",
}
EXPECTED_CARDINALITIES = {
    "axioms": 12,
    "apostles": 12,
    "legion_commanders": 7,
    "omc_commanders": 6,
    "phases": 2,
    "structural_patterns": 4,
}
EXPECTED_RELATIONSHIPS = 15
EXPECTED_CONFLICTS = 8
OMC_COMMANDERS = {"333", "CHU", "CRL", "engineboy", "orrr", "HSWM"}

# v1 is an exact public DTO contract, not merely a private-field denylist. A
# sanitizer field addition therefore fails closed until this facade explicitly
# reviews and allows it.
_COLLECTION_ALLOWED_KEYS = {
    "axioms": {
        "binding_state",
        "body_policy",
        "canonical_name",
        "content_authority",
        "definition",
        "identity_authority",
        "position",
        "public_id",
        "status",
    },
    "apostles": {
        "body_policy",
        "candidates",
        "entity",
        "position",
        "public_id",
        "selection_state",
        "structural_group",
    },
    "legion_commanders": {
        "aliases",
        "binding_state",
        "canonical_name",
        "identity_rule",
        "position",
        "public_id",
        "status",
        "verb",
    },
    "omc_commanders": {
        "binding_state",
        "canonical_name",
        "public_id",
        "scope_note",
        "status",
    },
    "phases": {
        "binding_state",
        "body_policy",
        "canonical_name",
        "content_authority",
        "public_id",
        "status",
    },
    "structural_patterns": {
        "binding_state",
        "body_policy",
        "canonical_name",
        "public_id",
        "status",
    },
}
_COLLECTION_REQUIRED_KEYS = {
    "axioms": _COLLECTION_ALLOWED_KEYS["axioms"],
    "apostles": {
        "entity",
        "position",
        "public_id",
        "selection_state",
        "structural_group",
    },
    "legion_commanders": {
        "binding_state",
        "canonical_name",
        "position",
        "public_id",
        "status",
        "verb",
    },
    "omc_commanders": {
        "binding_state",
        "canonical_name",
        "public_id",
        "status",
    },
    "phases": _COLLECTION_ALLOWED_KEYS["phases"],
    "structural_patterns": _COLLECTION_ALLOWED_KEYS["structural_patterns"],
}
_APOSTLE_ENTITY_ALLOWED_KEYS = {
    "aliases",
    "binding_state",
    "body_policy",
    "canonical_abbreviation",
    "canonical_name",
    "content_authority",
    "identity_authority",
}
_APOSTLE_ENTITY_REQUIRED_KEYS = _APOSTLE_ENTITY_ALLOWED_KEYS - {
    "canonical_abbreviation"
}
_CANDIDATE_KEYS = {
    "authority",
    "binding_state",
    "candidate_id",
    "canonical_name",
    "default_servable",
}
_RELATIONSHIP_KEYS = {
    "authority",
    "from_public_id",
    "predicate",
    "relationship_id",
    "status",
    "to_public_id",
}
_CONFLICT_ALLOWED_KEYS = {
    "conflict_id",
    "detail",
    "expected",
    "observed",
    "selected_candidate",
    "severity",
    "status",
    "subject_public_id",
    "subject_scope",
}
_CONFLICT_REQUIRED_KEYS = {"conflict_id", "detail", "severity", "status"}

_FORBIDDEN_KEYS = {
    "cypher",
    "exact_text",
    "kg_label",
    "kg_labels",
    "kg_uid",
    "raw_cypher",
    "source_file",
    "source_path",
    "source_paths",
    "stable_ref",
    "stable_refs",
    "user_request_sha256",
    "user_utterance",
    "verification_path",
    "verification_paths",
}
_FORBIDDEN_STRING_MARKERS = (
    "/home/",
    "/users/",
    "bolt://",
    "file://",
    "neo4j://",
    "sym:",
)


class OntologyProjectionError(RuntimeError):
    """The supplied snapshot is unavailable or violates the facade contract."""


class OntologyCursorError(ValueError):
    """An opaque pagination cursor is malformed, stale, or context-mismatched."""


class OntologyConflictPending(LookupError):
    """The requested ontology slot exists but has no ratified selected entity."""

    def __init__(self, record: Mapping[str, Any]) -> None:
        super().__init__("ontology entity selection is still conflict-pending")
        self.record = deepcopy(dict(record))


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise OntologyCursorError("invalid cursor encoding") from exc


def _validate_no_private_material(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise OntologyProjectionError(f"snapshot object key is not text at {path}")
            lowered = key.casefold()
            if lowered in _FORBIDDEN_KEYS:
                raise OntologyProjectionError(
                    f"snapshot contains forbidden private field {key!r} at {path}"
                )
            if lowered.endswith("sha256") and lowered != "content_sha256":
                raise OntologyProjectionError(
                    f"snapshot contains forbidden digest field {key!r} at {path}"
                )
            _validate_no_private_material(child, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_no_private_material(child, f"{path}[{index}]")
        return
    if isinstance(value, str):
        lowered = value.casefold()
        marker = next(
            (item for item in _FORBIDDEN_STRING_MARKERS if item in lowered), None
        )
        if marker is not None:
            raise OntologyProjectionError(
                f"snapshot contains forbidden private string marker at {path}"
            )


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OntologyProjectionError(f"expected object at {path}")
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise OntologyProjectionError(f"expected array at {path}")
    return value


def _require_text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OntologyProjectionError(f"expected non-empty text at {path}")
    return value


def _validate_keys(
    value: Mapping[str, Any],
    *,
    allowed: set[str],
    required: set[str],
    path: str,
) -> None:
    keys = set(value)
    unexpected = keys - allowed
    missing = required - keys
    if unexpected:
        raise OntologyProjectionError(
            f"unexpected public DTO field(s) at {path}: {sorted(unexpected)!r}"
        )
    if missing:
        raise OntologyProjectionError(
            f"missing public DTO field(s) at {path}: {sorted(missing)!r}"
        )


def _validate_aliases(value: Any, path: str) -> None:
    aliases = _require_list(value, path)
    if any(not isinstance(alias, str) or not alias.strip() for alias in aliases):
        raise OntologyProjectionError(f"aliases must contain only non-empty text at {path}")
    if len(aliases) != len(set(aliases)):
        raise OntologyProjectionError(f"aliases must be unique at {path}")


def _copy_allowed(value: Mapping[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {key: deepcopy(value[key]) for key in allowed if key in value}


@dataclass(frozen=True)
class Page:
    items: list[dict[str, Any]]
    next_cursor: str | None


class OntologyProjection:
    """Immutable in-memory index over one validated sanitized projection."""

    def __init__(self, snapshot: Mapping[str, Any], *, cursor_secret: bytes) -> None:
        if len(cursor_secret) < 32:
            raise OntologyProjectionError("ontology cursor secret must be at least 32 bytes")
        cloned_snapshot = _json_clone(snapshot)
        self.meta = deepcopy(cloned_snapshot["meta"])
        self.content_sha256 = self.meta["content_sha256"]
        self._cursor_key = hashlib.sha256(
            b"metahumotonic-ontology-cursor\0" + cursor_secret
        ).digest()

        collections = cloned_snapshot["data"]["collections"]
        self.collections: dict[str, list[dict[str, Any]]] = {
            name: [self._public_record(name, record) for record in records]
            for name, records in collections.items()
        }
        self.relationships = [
            _copy_allowed(record, _RELATIONSHIP_KEYS)
            for record in cloned_snapshot["data"]["relationships"]
        ]
        self.conflicts = [
            _copy_allowed(record, _CONFLICT_ALLOWED_KEYS)
            for record in cloned_snapshot["data"]["conflicts"]
        ]

        self.nodes: dict[str, dict[str, Any]] = {}
        for collection_name, records in self.collections.items():
            kind = COLLECTION_KINDS[collection_name]
            for record in records:
                node = {"kind": kind, **deepcopy(record)}
                self.nodes[node["public_id"]] = node

    @staticmethod
    def _public_record(
        collection_name: str, record: Mapping[str, Any]
    ) -> dict[str, Any]:
        public_record = _copy_allowed(
            record, _COLLECTION_ALLOWED_KEYS[collection_name]
        )
        if collection_name != "apostles":
            return public_record
        entity = record.get("entity")
        public_record["entity"] = (
            _copy_allowed(entity, _APOSTLE_ENTITY_ALLOWED_KEYS)
            if isinstance(entity, Mapping)
            else None
        )
        if "candidates" in record:
            public_record["candidates"] = [
                _copy_allowed(candidate, _CANDIDATE_KEYS)
                for candidate in record["candidates"]
            ]
        return public_record

    def etag_for(
        self,
        resource: str,
        params: Mapping[str, Any] | None = None,
    ) -> str:
        """Return a strong ETag bound to one representation, not just release."""

        context = json.dumps(
            {"resource": resource, "params": dict(params or {})},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(
            f"{self.content_sha256}\0{context}".encode("utf-8")
        ).hexdigest()
        return f'"{digest}"'

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        expected_file_sha256: str,
        expected_content_sha256: str,
        cursor_secret: bytes,
    ) -> "OntologyProjection":
        if not SHA256_RE.fullmatch(expected_file_sha256):
            raise OntologyProjectionError("ontology snapshot SHA-256 is invalid")
        if not SHA256_RE.fullmatch(expected_content_sha256):
            raise OntologyProjectionError("ontology content SHA-256 is invalid")
        snapshot_path = Path(path)
        if not snapshot_path.is_file():
            raise OntologyProjectionError("ontology snapshot file is unavailable")
        try:
            raw = snapshot_path.read_bytes()
        except OSError as exc:
            raise OntologyProjectionError("ontology snapshot file is unreadable") from exc
        observed_file_sha256 = hashlib.sha256(raw).hexdigest()
        if not secrets.compare_digest(observed_file_sha256, expected_file_sha256):
            raise OntologyProjectionError("ontology snapshot byte digest mismatch")
        try:
            snapshot = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OntologyProjectionError("ontology snapshot is not valid UTF-8 JSON") from exc
        cls.validate_snapshot(snapshot, expected_content_sha256=expected_content_sha256)
        return cls(snapshot, cursor_secret=cursor_secret)

    @classmethod
    def from_mapping(
        cls,
        snapshot: Mapping[str, Any],
        *,
        cursor_secret: bytes,
        expected_content_sha256: str | None = None,
    ) -> "OntologyProjection":
        content_sha256 = expected_content_sha256
        if content_sha256 is None and isinstance(snapshot, Mapping):
            meta = snapshot.get("meta")
            if isinstance(meta, Mapping):
                candidate = meta.get("content_sha256")
                if isinstance(candidate, str):
                    content_sha256 = candidate
        if content_sha256 is None:
            raise OntologyProjectionError("ontology content SHA-256 is missing")
        cls.validate_snapshot(snapshot, expected_content_sha256=content_sha256)
        return cls(snapshot, cursor_secret=cursor_secret)

    @classmethod
    def validate_snapshot(
        cls,
        snapshot: Mapping[str, Any],
        *,
        expected_content_sha256: str,
    ) -> None:
        root = _require_mapping(snapshot, "$")
        _validate_no_private_material(root)
        if set(root) != {"meta", "data"}:
            raise OntologyProjectionError("snapshot root must contain only meta and data")

        meta = _require_mapping(root["meta"], "$.meta")
        expected_meta = {
            "schema_version": SCHEMA_VERSION,
            "projection_id": PROJECTION_ID,
            "release_state": RELEASE_STATE,
            "publication_status": PUBLICATION_STATUS,
            "content_sha256": expected_content_sha256,
        }
        if dict(meta) != expected_meta:
            raise OntologyProjectionError("ontology projection metadata mismatch")
        if not SHA256_RE.fullmatch(expected_content_sha256):
            raise OntologyProjectionError("ontology content SHA-256 is invalid")

        data = _require_mapping(root["data"], "$.data")
        if set(data) != {"collections", "relationships", "conflicts"}:
            raise OntologyProjectionError("snapshot data shape mismatch")
        collections = _require_mapping(data["collections"], "$.data.collections")
        if set(collections) != set(EXPECTED_CARDINALITIES):
            raise OntologyProjectionError("ontology collection set mismatch")

        public_ids: set[str] = set()
        records_by_collection: dict[str, list[Mapping[str, Any]]] = {}
        for name, expected_count in EXPECTED_CARDINALITIES.items():
            records = _require_list(collections[name], f"$.data.collections.{name}")
            if len(records) != expected_count:
                raise OntologyProjectionError(
                    f"ontology cardinality mismatch for {name}: expected {expected_count}"
                )
            typed_records: list[Mapping[str, Any]] = []
            for index, raw_record in enumerate(records):
                record_path = f"$.data.collections.{name}[{index}]"
                record = _require_mapping(raw_record, record_path)
                _validate_keys(
                    record,
                    allowed=_COLLECTION_ALLOWED_KEYS[name],
                    required=_COLLECTION_REQUIRED_KEYS[name],
                    path=record_path,
                )
                public_id = _require_text(
                    record.get("public_id"),
                    f"{record_path}.public_id",
                )
                if not PUBLIC_ID_RE.fullmatch(public_id):
                    raise OntologyProjectionError(
                        f"invalid opaque public ID in collection {name}"
                    )
                if public_id in public_ids:
                    raise OntologyProjectionError("duplicate ontology public ID")
                public_ids.add(public_id)
                if name != "apostles":
                    _require_text(record.get("canonical_name"), f"{record_path}.canonical_name")
                if "aliases" in record:
                    _validate_aliases(record["aliases"], f"{record_path}.aliases")
                if name == "apostles":
                    entity = record.get("entity")
                    if entity is not None:
                        entity = _require_mapping(entity, f"{record_path}.entity")
                        _validate_keys(
                            entity,
                            allowed=_APOSTLE_ENTITY_ALLOWED_KEYS,
                            required=_APOSTLE_ENTITY_REQUIRED_KEYS,
                            path=f"{record_path}.entity",
                        )
                        _require_text(
                            entity.get("canonical_name"),
                            f"{record_path}.entity.canonical_name",
                        )
                        _validate_aliases(
                            entity.get("aliases"), f"{record_path}.entity.aliases"
                        )
                    if "candidates" in record:
                        candidates = _require_list(
                            record["candidates"], f"{record_path}.candidates"
                        )
                        for candidate_index, raw_candidate in enumerate(candidates):
                            candidate_path = (
                                f"{record_path}.candidates[{candidate_index}]"
                            )
                            candidate = _require_mapping(raw_candidate, candidate_path)
                            _validate_keys(
                                candidate,
                                allowed=_CANDIDATE_KEYS,
                                required=_CANDIDATE_KEYS,
                                path=candidate_path,
                            )
                            for field in _CANDIDATE_KEYS - {"default_servable"}:
                                _require_text(
                                    candidate.get(field), f"{candidate_path}.{field}"
                                )
                typed_records.append(record)
            records_by_collection[name] = typed_records

        cls._validate_domain_invariants(records_by_collection)

        relationships = _require_list(data["relationships"], "$.data.relationships")
        if len(relationships) != EXPECTED_RELATIONSHIPS:
            raise OntologyProjectionError("ontology relationship cardinality mismatch")
        relationship_ids: set[str] = set()
        for index, raw_relationship in enumerate(relationships):
            relationship_path = f"$.data.relationships[{index}]"
            relationship = _require_mapping(raw_relationship, relationship_path)
            _validate_keys(
                relationship,
                allowed=_RELATIONSHIP_KEYS,
                required=_RELATIONSHIP_KEYS,
                path=relationship_path,
            )
            relationship_id = _require_text(
                relationship.get("relationship_id"),
                f"{relationship_path}.relationship_id",
            )
            if relationship_id in relationship_ids:
                raise OntologyProjectionError("duplicate ontology relationship ID")
            relationship_ids.add(relationship_id)
            for field in ("predicate", "authority", "status"):
                _require_text(relationship.get(field), f"{relationship_path}.{field}")
            for endpoint in ("from_public_id", "to_public_id"):
                public_id = relationship.get(endpoint)
                if public_id not in public_ids:
                    raise OntologyProjectionError(
                        f"ontology relationship has unknown {endpoint}"
                    )
        cls._validate_relationship_invariants(records_by_collection, relationships)

        conflicts = _require_list(data["conflicts"], "$.data.conflicts")
        if len(conflicts) != EXPECTED_CONFLICTS:
            raise OntologyProjectionError("ontology conflict cardinality mismatch")
        conflict_ids: set[str] = set()
        for index, raw_conflict in enumerate(conflicts):
            conflict_path = f"$.data.conflicts[{index}]"
            conflict = _require_mapping(raw_conflict, conflict_path)
            _validate_keys(
                conflict,
                allowed=_CONFLICT_ALLOWED_KEYS,
                required=_CONFLICT_REQUIRED_KEYS,
                path=conflict_path,
            )
            conflict_id = _require_text(
                conflict.get("conflict_id"), f"{conflict_path}.conflict_id"
            )
            if conflict_id in conflict_ids:
                raise OntologyProjectionError("duplicate ontology conflict ID")
            conflict_ids.add(conflict_id)
            for field in ("detail", "severity", "status"):
                _require_text(conflict.get(field), f"{conflict_path}.{field}")
            has_public_subject = "subject_public_id" in conflict
            has_scoped_subject = "subject_scope" in conflict
            if has_public_subject == has_scoped_subject:
                raise OntologyProjectionError(
                    "ontology conflict must have exactly one subject reference"
                )
            subject_public_id = conflict.get("subject_public_id")
            if subject_public_id is not None and subject_public_id not in public_ids:
                raise OntologyProjectionError("ontology conflict has unknown subject")

    @staticmethod
    def _validate_domain_invariants(
        collections: Mapping[str, Sequence[Mapping[str, Any]]],
    ) -> None:
        apostles = collections["apostles"]
        positions = [record.get("position") for record in apostles]
        if any(not isinstance(item, int) or isinstance(item, bool) for item in positions):
            raise OntologyProjectionError("apostle positions must be integers")
        if sorted(positions) != list(range(1, 13)):
            raise OntologyProjectionError("apostle positions must be exactly 1 through 12")
        axiom_positions = [record.get("position") for record in collections["axioms"]]
        if (
            any(
                not isinstance(item, int) or isinstance(item, bool)
                for item in axiom_positions
            )
            or sorted(axiom_positions) != list(range(1, 13))
        ):
            raise OntologyProjectionError("axiom positions must be exactly 1 through 12")
        commander_positions = [
            record.get("position") for record in collections["legion_commanders"]
        ]
        if (
            any(
                not isinstance(item, int) or isinstance(item, bool)
                for item in commander_positions
            )
            or sorted(commander_positions) != list(range(1, 8))
        ):
            raise OntologyProjectionError(
                "legion commander positions must be exactly 1 through 7"
            )

        slot8 = next(record for record in apostles if record.get("position") == 8)
        slot8_entity = _require_mapping(slot8.get("entity"), "apostle slot 8 entity")
        if slot8_entity.get("canonical_name") != "입체운행구름":
            raise OntologyProjectionError("apostle slot 8 must be 입체운행구름")
        if slot8_entity.get("canonical_abbreviation") != "OMC":
            raise OntologyProjectionError("OMC must be the canonical abbreviation")
        aliases = slot8_entity.get("aliases")
        if not isinstance(aliases, list) or "OMC" not in aliases or "OM" in aliases:
            raise OntologyProjectionError("OMC aliases violate the OM exclusion rule")

        slot9 = next(record for record in apostles if record.get("position") == 9)
        if slot9.get("selection_state") != "CONFLICT_PENDING":
            raise OntologyProjectionError("apostle slot 9 must remain conflict-pending")
        if slot9.get("entity") is not None or slot9.get("body_policy") != "NO_DEFAULT":
            raise OntologyProjectionError("apostle slot 9 must not select a default entity")
        candidates = _require_list(slot9.get("candidates"), "apostle slot 9 candidates")
        candidate_names = {
            candidate.get("canonical_name")
            for candidate in candidates
            if isinstance(candidate, Mapping)
        }
        if candidate_names != {"예수", "검은 태양신 아텐"}:
            raise OntologyProjectionError("apostle slot 9 candidate set mismatch")
        if any(
            not isinstance(candidate, Mapping)
            or candidate.get("default_servable") is not False
            for candidate in candidates
        ):
            raise OntologyProjectionError("apostle slot 9 candidates must not be default-served")

        commanders = collections["legion_commanders"]
        harness = [record for record in commanders if record.get("canonical_name") == "Harness"]
        if len(harness) != 1 or harness[0].get("identity_rule") != "ONE_ENTITY_ONE_SLOT":
            raise OntologyProjectionError("Harness/Hades must occupy one commander slot")
        if any(record.get("canonical_name") == "Hades" for record in commanders):
            raise OntologyProjectionError("Hades must not be a second commander entity")
        harness_aliases = harness[0].get("aliases")
        if not isinstance(harness_aliases, list) or "Hades" not in harness_aliases:
            raise OntologyProjectionError("Harness must preserve Hades as an alias")

        omc_commanders = collections["omc_commanders"]
        omc_names = {record.get("canonical_name") for record in omc_commanders}
        if omc_names != OMC_COMMANDERS:
            raise OntologyProjectionError("OMC commander roster mismatch")
        hswm = next(
            record for record in omc_commanders if record.get("canonical_name") == "HSWM"
        )
        scope_note = hswm.get("scope_note")
        if (
            not isinstance(scope_note, str)
            or "implementation" not in scope_note.casefold()
            or "not a claim" not in scope_note.casefold()
        ):
            raise OntologyProjectionError(
                "HSWM must be scoped as identity, not implementation completeness"
            )

    @staticmethod
    def _validate_relationship_invariants(
        collections: Mapping[str, Sequence[Mapping[str, Any]]],
        relationships: Sequence[Mapping[str, Any]],
    ) -> None:
        by_predicate: dict[str, list[Mapping[str, Any]]] = {}
        for relationship in relationships:
            by_predicate.setdefault(relationship["predicate"], []).append(relationship)
        if set(by_predicate) != {
            "COMMANDS_LEGION",
            "HAS_DIRECT_COMMANDER",
            "PHASE_OF",
        }:
            raise OntologyProjectionError("ontology relationship predicate set mismatch")

        apostles_by_position = {
            record["position"]: record for record in collections["apostles"]
        }
        legion_source = apostles_by_position[4]["public_id"]
        legion_targets = {
            record["public_id"] for record in collections["legion_commanders"]
        }
        legion_edges = by_predicate["COMMANDS_LEGION"]
        if (
            len(legion_edges) != 7
            or {edge["from_public_id"] for edge in legion_edges} != {legion_source}
            or {edge["to_public_id"] for edge in legion_edges} != legion_targets
            or any(
                edge["authority"] != "PROJECT_CANON_WITH_USER_VERDICT"
                or edge["status"] != "ACTIVE"
                for edge in legion_edges
            )
        ):
            raise OntologyProjectionError(
                "apostle 4 to legion commander topology mismatch"
            )

        omc_source = apostles_by_position[8]["public_id"]
        omc_targets = {
            record["public_id"] for record in collections["omc_commanders"]
        }
        omc_edges = by_predicate["HAS_DIRECT_COMMANDER"]
        if (
            len(omc_edges) != 6
            or {edge["from_public_id"] for edge in omc_edges} != {omc_source}
            or {edge["to_public_id"] for edge in omc_edges} != omc_targets
            or any(
                edge["authority"] != "USER_PRIMARY"
                or edge["status"] != "ACTIVE"
                for edge in omc_edges
            )
        ):
            raise OntologyProjectionError(
                "OMC to direct commander topology mismatch"
            )

        gipbajon_target = apostles_by_position[10]["public_id"]
        expected_phase_policy = {
            "공허진동자": ("USER_PRIMARY", "PROJECTED_FROM_USER_SOURCE"),
            "검은 태양신 아텐": (
                "PROJECT_CANON",
                "PROJECTED_FROM_PROJECT_CANON",
            ),
        }
        phase_edges = by_predicate["PHASE_OF"]
        if len(phase_edges) != 2:
            raise OntologyProjectionError("phase topology cardinality mismatch")
        phase_edge_by_source = {
            edge["from_public_id"]: edge for edge in phase_edges
        }
        if len(phase_edge_by_source) != 2:
            raise OntologyProjectionError("phase topology has duplicate source")
        for phase in collections["phases"]:
            edge = phase_edge_by_source.get(phase["public_id"])
            expected_policy = expected_phase_policy.get(phase["canonical_name"])
            if (
                edge is None
                or expected_policy is None
                or edge["to_public_id"] != gipbajon_target
                or (edge["authority"], edge["status"]) != expected_policy
            ):
                raise OntologyProjectionError("phase to apostle 10 topology mismatch")

    def meta_payload(self) -> dict[str, Any]:
        return deepcopy(self.meta)

    def _display_identity(self, node: Mapping[str, Any]) -> tuple[str, list[str]]:
        if node.get("kind") == "apostle":
            entity = node.get("entity")
            if isinstance(entity, Mapping):
                name = entity.get("canonical_name")
                aliases = entity.get("aliases", [])
                return (
                    name if isinstance(name, str) else "",
                    [item for item in aliases if isinstance(item, str)],
                )
            return "미결", []
        name = node.get("canonical_name")
        aliases = node.get("aliases", [])
        return (
            name if isinstance(name, str) else "",
            [item for item in aliases if isinstance(item, str)],
        )

    def summary(self, node: Mapping[str, Any]) -> dict[str, Any]:
        name, aliases = self._display_identity(node)
        summary: dict[str, Any] = {
            "public_id": node["public_id"],
            "kind": node["kind"],
            "canonical_name": name,
            "aliases": aliases,
        }
        for key in (
            "position",
            "status",
            "binding_state",
            "selection_state",
            "structural_group",
        ):
            if key in node:
                summary[key] = deepcopy(node[key])
        if node.get("kind") == "apostle":
            entity = node.get("entity")
            if isinstance(entity, Mapping):
                for key in (
                    "binding_state",
                    "body_policy",
                    "content_authority",
                    "identity_authority",
                ):
                    if key in entity:
                        summary[key] = deepcopy(entity[key])
            elif node.get("selection_state") == "CONFLICT_PENDING":
                summary["candidate_count"] = len(node.get("candidates", []))
                summary["default_servable"] = False
                summary["body_policy"] = node.get("body_policy")
        return summary

    def get_node(self, public_id: str) -> dict[str, Any] | None:
        node = self.nodes.get(public_id)
        if node is None:
            return None
        if (
            node.get("kind") == "apostle"
            and node.get("selection_state") == "CONFLICT_PENDING"
        ):
            raise OntologyConflictPending(node)
        return deepcopy(node)

    def conflict_payload(self, record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "public_id": record["public_id"],
            "kind": "apostle",
            "position": record.get("position"),
            "selection_state": record.get("selection_state"),
            "body_policy": record.get("body_policy"),
            "default_servable": False,
            "candidates": deepcopy(record.get("candidates", [])),
        }

    def search(
        self,
        query: str,
        *,
        kind: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page:
        normalized_query = _normalized(query)
        matches: list[tuple[int, str, dict[str, Any]]] = []
        for node in self.nodes.values():
            if kind is not None and node["kind"] != kind:
                continue
            if (
                node.get("kind") == "apostle"
                and node.get("selection_state") == "CONFLICT_PENDING"
            ):
                continue
            # OM survives only in historical quotation/mantra context. It is
            # not a current abbreviation that may resolve the OMC entity.
            if (
                normalized_query == "om"
                and node.get("kind") == "apostle"
                and node.get("position") == 8
            ):
                continue
            name, aliases = self._display_identity(node)
            terms = [name, *aliases]
            normalized_terms = [_normalized(term) for term in terms if term]
            if normalized_query in normalized_terms:
                score = 0
            elif any(term.startswith(normalized_query) for term in normalized_terms):
                score = 1
            elif any(normalized_query in term for term in normalized_terms):
                score = 2
            else:
                continue
            matches.append((score, node["public_id"], self.summary(node)))
        matches.sort(key=lambda item: (item[0], item[1]))
        items = [item[2] for item in matches]
        return self._page(
            items,
            route="search",
            params={"q": normalized_query, "kind": kind},
            limit=limit,
            cursor=cursor,
        )

    def neighbors(
        self,
        public_id: str,
        *,
        direction: str,
        predicate: str | None,
        limit: int,
        cursor: str | None,
    ) -> Page | None:
        if public_id not in self.nodes:
            return None
        entries: list[dict[str, Any]] = []
        for relationship in self.relationships:
            edge_direction: str | None = None
            other_id: str | None = None
            if direction in {"out", "both"} and relationship["from_public_id"] == public_id:
                edge_direction = "out"
                other_id = relationship["to_public_id"]
            elif direction in {"in", "both"} and relationship["to_public_id"] == public_id:
                edge_direction = "in"
                other_id = relationship["from_public_id"]
            if edge_direction is None or other_id is None:
                continue
            if predicate is not None and relationship["predicate"] != predicate:
                continue
            entries.append(
                {
                    "direction": edge_direction,
                    "relationship": deepcopy(relationship),
                    "node": self.summary(self.nodes[other_id]),
                }
            )
        entries.sort(
            key=lambda item: (
                item["relationship"]["predicate"],
                item["relationship"]["relationship_id"],
                item["direction"],
            )
        )
        return self._page(
            entries,
            route="neighbors",
            params={
                "public_id": public_id,
                "direction": direction,
                "predicate": predicate,
            },
            limit=limit,
            cursor=cursor,
        )

    def schema_document(self) -> dict[str, Any]:
        predicates = sorted({item["predicate"] for item in self.relationships})
        return {
            "schema_version": SCHEMA_VERSION,
            "projection_id": PROJECTION_ID,
            "public_id_pattern": PUBLIC_ID_RE.pattern,
            "digest_semantics": {
                "content_sha256": "OPERATOR_PINNED_SOURCE_MANIFEST_CANONICAL_DIGEST",
                "snapshot_bytes": "PINNED_SEPARATELY_AT_STARTUP",
            },
            "collection_kinds": deepcopy(COLLECTION_KINDS),
            "predicates": predicates,
            "conflict_policy": {
                "apostle_slot_9": "CONFLICT_PENDING",
                "default_servable": False,
                "node_status": 409,
            },
            "identity_rules": {
                "omc_abbreviation": "OMC",
                "om_is_current_alias": False,
                "harness_hades": "ONE_ENTITY_ONE_SLOT",
                "hswm_scope": "IDENTITY_ONLY_NOT_IMPLEMENTATION_COMPLETENESS",
            },
        }

    def release_document(self) -> dict[str, Any]:
        return {
            **self.meta_payload(),
            "counts": {
                "collections": {
                    name: len(records) for name, records in self.collections.items()
                },
                "relationships": len(self.relationships),
                "conflicts": len(self.conflicts),
            },
            "collections": {
                name: [self.summary({"kind": COLLECTION_KINDS[name], **record}) for record in records]
                for name, records in self.collections.items()
            },
            "conflicts": deepcopy(self.conflicts),
        }

    def _page(
        self,
        items: Sequence[dict[str, Any]],
        *,
        route: str,
        params: Mapping[str, Any],
        limit: int,
        cursor: str | None,
    ) -> Page:
        offset = 0
        if cursor:
            payload = self._decode_cursor(cursor)
            expected = {
                "v": 1,
                "content_sha256": self.content_sha256,
                "route": route,
                "params": dict(params),
                "limit": limit,
            }
            for key, value in expected.items():
                if payload.get(key) != value:
                    raise OntologyCursorError("cursor does not match this request")
            offset = payload.get("offset")
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                raise OntologyCursorError("cursor offset is invalid")
            if offset > len(items):
                raise OntologyCursorError("cursor is outside the result set")
        selected = [deepcopy(item) for item in items[offset : offset + limit]]
        next_offset = offset + len(selected)
        next_cursor = None
        if next_offset < len(items):
            next_cursor = self._encode_cursor(
                {
                    "v": 1,
                    "content_sha256": self.content_sha256,
                    "route": route,
                    "params": dict(params),
                    "limit": limit,
                    "offset": next_offset,
                }
            )
        return Page(items=selected, next_cursor=next_cursor)

    def _encode_cursor(self, payload: Mapping[str, Any]) -> str:
        raw = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(self._cursor_key, raw, hashlib.sha256).digest()
        return f"{_b64encode(raw)}.{_b64encode(signature)}"

    def _decode_cursor(self, cursor: str) -> Mapping[str, Any]:
        if len(cursor) > 4096 or cursor.count(".") != 1:
            raise OntologyCursorError("invalid cursor")
        encoded_payload, encoded_signature = cursor.split(".", 1)
        raw = _b64decode(encoded_payload)
        signature = _b64decode(encoded_signature)
        expected_signature = hmac.new(self._cursor_key, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected_signature):
            raise OntologyCursorError("invalid cursor signature")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OntologyCursorError("invalid cursor payload") from exc
        if not isinstance(payload, Mapping):
            raise OntologyCursorError("invalid cursor payload")
        return payload


class OntologyRuntime:
    """Process-wide read-only projection and independent internal API key."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.enabled = False
        self.ready = False
        self.projection: OntologyProjection | None = None
        self._internal_key = ""

    def configure(self, runtime_settings: Any) -> None:
        self.reset()
        if not runtime_settings.ontology_enabled:
            return
        internal_key = runtime_settings.ontology_internal_key
        if len(internal_key.encode("utf-8")) < 32:
            raise RuntimeError("MHB_ONTOLOGY_INTERNAL_KEY must be at least 32 bytes")
        if not runtime_settings.ontology_snapshot_path:
            raise RuntimeError(
                "MHB_ONTOLOGY_SNAPSHOT_PATH is required when ontology is enabled"
            )
        try:
            projection = OntologyProjection.load(
                runtime_settings.ontology_snapshot_path,
                expected_file_sha256=runtime_settings.ontology_snapshot_sha256,
                expected_content_sha256=runtime_settings.ontology_content_sha256,
                cursor_secret=internal_key.encode("utf-8"),
            )
        except OntologyProjectionError as exc:
            raise RuntimeError(f"ontology snapshot validation failed: {exc}") from exc
        self.enabled = True
        self.ready = True
        self.projection = projection
        self._internal_key = internal_key

    def install_for_testing(
        self,
        projection: OntologyProjection,
        *,
        internal_key: str,
    ) -> None:
        if len(internal_key.encode("utf-8")) < 32:
            raise ValueError("test ontology key must be at least 32 bytes")
        self.enabled = True
        self.ready = True
        self.projection = projection
        self._internal_key = internal_key

    def authorized(self, supplied_key: str | None) -> bool:
        if not supplied_key or not self._internal_key:
            return False
        return secrets.compare_digest(supplied_key, self._internal_key)


ontology_runtime = OntologyRuntime()
