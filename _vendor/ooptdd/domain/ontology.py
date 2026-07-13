"""Event ontology — give the gate semantic teeth.

A flat gate checks *names and counts*: "did event X arrive N times?". That cannot
see three whole classes of hallucination:

  1. **missing required attribute** — the code emits ``payment_authorized`` but with
     no ``amount``. Counted by name -> GREEN. Wrong.
  2. **unknown / fabricated event type** (drift) — the code emits an event whose
     type was never declared. A flat gate only asserts what you listed, so an
     undeclared name is invisible.
  3. **bad value** — ``status: "kinda"`` where the type allows only ``{ok, ng}``,
     or ``amount: "lots"`` where a number is required.

The ontology is a small, formal vocabulary of event types with required
attributes and value constraints. ``check_conformance`` validates observed events
against it, so the above become RED. It is deliberately minimal (required attrs +
enum/type/range) — an ontology earns its keep only when its types carry real
invariants; do not formalize for its own sake.

The semantics are borrowed verbatim from **JSON Schema** (Draft 2020-12), which is
the standard for exactly this job, so the three drift classes map 1:1 and stay
defensible:

  =========================  ============================  ====================
  hallucination class        JSON Schema construct         ooptdd field
  =========================  ============================  ====================
  missing required attr      ``"required": [...]``         ``required``
  bad value (enum/type)      ``"enum"`` / ``"type"``       ``constraints``
  unexpected attribute       ``"additionalProperties":     ``additional_properties:
                             false``                       false``
  unknown event type         (closed-world at the          ``Ontology.closed_world``
                             document level)
  =========================  ============================  ====================

We re-implement the small subset natively (no ``jsonschema`` dependency) to keep
the core stdlib-only and the offline invariant intact — but a spec author can read
the table above and reason about an EventType as the JSON Schema it denotes.

It is **file-first** (zero KG, zero network — the offline invariant holds) and can
be mirrored into the KG when available; the KG never becomes a hard dependency.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar

_NUMBER = (int, float)

# Transport/plumbing keys every envelope carries (see model.py). When an EventType
# is closed (`additional_properties: false`) these are never counted as "unexpected"
# attributes — closed-world polices the *payload* you declared, not the carrier.
ENVELOPE_KEYS = frozenset({
    "cid", "correlation_id", "cycle_id", "spec_version", "service", "level", "event",
    "_timestamp", "sig", "sig_alg", "sig_chain", "prev_sig",
    # W3C trace context (model.with_trace_context)
    "trace_id", "span_id",
    # CloudEvents context projection (model.cloudevents_envelope)
    "id", "source", "type", "specversion", "subject", "time", "datacontenttype",
})


@dataclass
class EventType:
    """One class in the ontology: an event name + what a valid instance must carry."""

    name: str
    required: list[str] = field(default_factory=list)   # attribute keys that must be present
    constraints: dict = field(default_factory=dict)     # attr -> {enum|type|min|max}
    description: str = ""
    #: JSON Schema ``additionalProperties: false`` — when False, a payload attribute
    #: that is neither declared (required/constraints) nor envelope plumbing is drift.
    additional_properties: bool = True

    def _declared(self) -> set[str]:
        return set(self.required) | set(self.constraints)

    def validate(self, event: dict) -> list[str]:
        """Return a list of human-readable violation strings (empty = conforms)."""
        out: list[str] = []
        for key in self.required:
            if key not in event or event[key] is None:
                out.append(f"missing required attr '{key}'")
        for attr, rule in self.constraints.items():
            if attr not in event or event[attr] is None:
                continue  # presence is governed by `required`; constraints only bind when present
            val = event[attr]
            if "enum" in rule and val not in rule["enum"]:
                out.append(f"'{attr}'={val!r} not in enum {rule['enum']}")
            if "type" in rule and not _type_ok(val, rule["type"]):
                out.append(f"'{attr}'={val!r} is not type {rule['type']}")
            if "min" in rule and isinstance(val, _NUMBER) and val < rule["min"]:
                out.append(f"'{attr}'={val} < min {rule['min']}")
            if "max" in rule and isinstance(val, _NUMBER) and val > rule["max"]:
                out.append(f"'{attr}'={val} > max {rule['max']}")
        if not self.additional_properties:
            allowed = self._declared() | ENVELOPE_KEYS
            for key in event:
                if key not in allowed:
                    out.append(f"unexpected attr '{key}' (additionalProperties:false)")
        return out


def _type_ok(val, t: str) -> bool:
    return {
        "number": isinstance(val, _NUMBER) and not isinstance(val, bool),
        "int": isinstance(val, int) and not isinstance(val, bool),
        "float": isinstance(val, float),
        "str": isinstance(val, str),
        "bool": isinstance(val, bool),
    }.get(t, True)  # unknown type name -> don't fail (forward-compatible)


@dataclass
class Ontology:
    types: dict[str, EventType] = field(default_factory=dict)
    #: when True, an observed event whose name is not a declared type is drift.
    closed_world: bool = False

    #: Preset ontology factories, keyed by name (e.g. ``"gen_ai"``). A ``ClassVar`` —
    #: shared state, NOT a dataclass field. Dependency-inversion seam: the core exposes
    #: :meth:`register_preset` as a registration port and preset modules
    #: (e.g. :mod:`ooptdd.semconv`) register *into* it at import time — so ``ontology.py``
    #: never imports a specific preset and the module-import graph stays acyclic.
    _PRESETS: ClassVar[dict[str, Callable[..., Ontology]]] = {}

    def get(self, name: str) -> EventType | None:
        return self.types.get(name)

    @classmethod
    def from_dict(cls, data: dict) -> Ontology:
        types = {}
        for name, spec in (data.get("event_types") or {}).items():
            spec = spec or {}
            types[name] = EventType(
                name=name,
                required=list(spec.get("required", [])),
                constraints=dict(spec.get("constraints", {})),
                description=spec.get("description", ""),
                additional_properties=bool(spec.get("additional_properties", True)),
            )
        return cls(types=types, closed_world=bool(data.get("closed_world", False)))

    @classmethod
    def from_file(cls, path: str) -> Ontology:
        import yaml

        with open(path) as fh:
            return cls.from_dict(yaml.safe_load(fh) or {})

    @classmethod
    def register_preset(cls, name: str, factory: Callable[..., Ontology]) -> None:
        """Register a shipped preset ontology factory under ``name``.

        The inversion seam: preset modules call this at import time
        (e.g. :mod:`ooptdd.semconv` registers ``"gen_ai"``), so the core never imports a
        preset. Importing the ``ooptdd`` package wires the shipped built-ins.
        """
        cls._PRESETS[name] = factory

    @classmethod
    def builtin(cls, name: str, **kwargs) -> Ontology:
        """Resolve a registered preset ontology by ``name`` — e.g. ``"gen_ai"``, the
        version-pinned OpenTelemetry GenAI semconv vocabulary (see :mod:`ooptdd.semconv`).

        Presets self-register at import; ``import ooptdd`` wires the shipped built-ins.
        """
        try:
            factory = cls._PRESETS[name]
        except KeyError:
            have = ", ".join(sorted(cls._PRESETS)) or "none (is the preset module imported?)"
            raise ValueError(f"unknown builtin ontology {name!r} (have: {have})") from None
        return factory(**kwargs)


_COMPAT_MODES = ("backward", "forward", "full")


def _enum(et: EventType, attr: str):
    rule = et.constraints.get(attr) or {}
    return set(rule["enum"]) if "enum" in rule else None


def ontology_compat(old: Ontology, new: Ontology, mode: str = "backward") -> dict:
    """Is the evolution ``old`` -> ``new`` compatible? (Confluent Schema Registry semantics.)

    ``backward`` — ``new`` can still validate data written under ``old`` (the common
    "upgrade consumers first" rule). Breaks: adding a required attr, shrinking an enum,
    and (when ``new`` is closed-world) dropping an event type that old data still emits.

    ``forward`` — ``old`` can validate data written under ``new``. Breaks: removing a
    required attr, growing an enum, and (when ``old`` is closed-world) adding an event type.

    ``full`` — both directions. Returns ``{compatible, mode, violations:[str]}``. This gates
    "did this EventType change *safely*?", a layer above per-instance validation.
    """
    if mode not in _COMPAT_MODES:
        raise ValueError(f"mode must be one of {_COMPAT_MODES}")
    back = mode in ("backward", "full")
    fwd = mode in ("forward", "full")
    v: list[str] = []
    names = set(old.types) | set(new.types)
    for name in sorted(names):
        o, n = old.get(name), new.get(name)
        if o is None:  # type added in new
            if fwd and old.closed_world:
                v.append(f"[forward] event type '{name}' added — old closed-world rejects it")
            continue
        if n is None:  # type removed in new
            if back and new.closed_world:
                v.append(f"[backward] event type '{name}' removed — new closed-world rejects it")
            continue
        o_req, n_req = set(o.required), set(n.required)
        if back:
            for a in n_req - o_req:
                v.append(f"[backward] '{name}': new required attr '{a}' — old data lacks it")
        if fwd:
            for a in o_req - n_req:
                v.append(f"[forward] '{name}': required attr '{a}' removed — old reader needs it")
        for attr in set(o.constraints) | set(n.constraints):
            oe, ne = _enum(o, attr), _enum(n, attr)
            if oe is not None and ne is not None:
                if back and (oe - ne):
                    v.append(f"[backward] '{name}.{attr}': enum shrank (dropped {sorted(oe - ne)})")
                if fwd and (ne - oe):
                    v.append(f"[forward] '{name}.{attr}': enum grew (added {sorted(ne - oe)})")
    return {"compatible": not v, "mode": mode, "violations": v}


def check_conformance(
    events: list[dict],
    ontology: Ontology,
    *,
    event_type: str | None = None,
    closed_world: bool | None = None,
) -> dict:
    """Validate events against the ontology.

    ``event_type``: restrict to events of this name (None / "*" = all events).
    ``closed_world``: override the ontology default; when True an event whose name
    is not a declared type is reported as ``unknown_event_type`` drift.

    Returns ``{passed, checked, violations:[{event,index,problems}], unknown:[names]}``.
    """
    cw = ontology.closed_world if closed_world is None else closed_world
    scope_all = event_type in (None, "*")
    violations: list[dict] = []
    unknown: list[str] = []
    checked = 0
    for i, ev in enumerate(events):
        name = ev.get("event")
        if not scope_all and name != event_type:
            continue
        et = ontology.get(name)
        if et is None:
            # only flag unknowns we were asked to police: in closed-world, any in-scope
            # event must have a declared type.
            if cw and (scope_all or name == event_type):
                unknown.append(name)
                violations.append({"event": name, "index": i,
                                   "problems": ["unknown_event_type (closed-world drift)"]})
            continue
        checked += 1
        problems = et.validate(ev)
        if problems:
            violations.append({"event": name, "index": i, "problems": problems})
    return {
        "passed": not violations,
        "checked": checked,
        "violations": violations,
        "unknown": unknown,
    }
