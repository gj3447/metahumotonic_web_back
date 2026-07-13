"""OpenTelemetry GenAI semantic conventions — a built-in verify vocabulary (Tier-2 #8).

ooptdd's gate/verify asserts on events *by name and attribute*, which needs a stable,
shared vocabulary. Rather than invent event/attribute names for agent telemetry, we
adopt OpenTelemetry's **GenAI semantic conventions** (``gen_ai.*``): the operation
enum (``chat`` / ``execute_tool`` / ``invoke_agent`` / …) and attribute names
(``gen_ai.tool.name``, ``gen_ai.request.model``, ``gen_ai.provider.name`` — whose enum
includes ``anthropic``). An assertion can then reference an external standard.

⚠ The GenAI conventions are **Development / experimental** — attribute names churn
between releases. So this preset is **version-pinned** (``SEMCONV_VERSION``); bump it
deliberately when you upgrade. The *stable* pieces ooptdd leans on are OTLP (write)
and W3C trace context (``trace_id``/``span_id`` correlation, see
``ooptdd.model.with_trace_context``), not the experimental attribute set.

Practical ooptdd mapping: emit one structured event per agent operation, named by the
operation (``event = "gen_ai.execute_tool"``), carrying the ``gen_ai.*`` attributes.
``gen_ai_ontology()`` returns an :class:`~ooptdd.ontology.Ontology` that makes a
missing ``gen_ai.tool.name`` on an ``execute_tool`` (etc.) RED.

Refs: https://opentelemetry.io/docs/specs/semconv/gen-ai/ (spans + attribute registry).
"""
from __future__ import annotations

from .ontology import Ontology

#: The semconv revision this preset was written against. Pin it; bump on upgrade.
SEMCONV_VERSION = "1.30.0-experimental"

#: gen_ai.operation.name enum (the agent operations we model as event names).
GEN_AI_OPERATIONS = (
    "chat",
    "generate_content",
    "text_completion",
    "embeddings",
    "execute_tool",
    "create_agent",
    "invoke_agent",
    "invoke_workflow",
)

#: gen_ai.provider.name enum (representative; includes anthropic per the registry).
GEN_AI_PROVIDERS = (
    "anthropic", "openai", "azure.ai.openai", "aws.bedrock", "gcp.gemini",
    "cohere", "mistral_ai", "x_ai", "groq", "deepseek", "perplexity", "ibm.watsonx.ai",
)

# event-type name -> required gen_ai.* attributes (the load-bearing invariant per op).
_REQUIRED = {
    "gen_ai.chat": ["gen_ai.provider.name", "gen_ai.request.model"],
    "gen_ai.generate_content": ["gen_ai.provider.name", "gen_ai.request.model"],
    "gen_ai.text_completion": ["gen_ai.provider.name", "gen_ai.request.model"],
    "gen_ai.embeddings": ["gen_ai.request.model"],
    "gen_ai.execute_tool": ["gen_ai.tool.name"],
    "gen_ai.create_agent": ["gen_ai.agent.id"],
    "gen_ai.invoke_agent": ["gen_ai.agent.id"],
    "gen_ai.invoke_workflow": [],
}


def gen_ai_ontology(*, closed_world: bool = False) -> Ontology:
    """Build the version-pinned GenAI ontology.

    ``closed_world`` (default False so it composes with your own event types): when True,
    an in-scope event whose name is not a ``gen_ai.*`` type is reported as drift.
    """
    event_types = {}
    for name, required in _REQUIRED.items():
        constraints = {
            "gen_ai.provider.name": {"enum": list(GEN_AI_PROVIDERS)},
            "gen_ai.usage.input_tokens": {"type": "int", "min": 0},
            "gen_ai.usage.output_tokens": {"type": "int", "min": 0},
        }
        event_types[name] = {
            "required": required,
            "constraints": {k: v for k, v in constraints.items()
                            if k in required or k.startswith("gen_ai.usage")},
            "description": f"OTel GenAI semconv {SEMCONV_VERSION}: {name}",
        }
    return Ontology.from_dict({"event_types": event_types, "closed_world": closed_world})


# Self-register as the ``gen_ai`` built-in preset. Inversion seam (see
# :meth:`ooptdd.ontology.Ontology.register_preset`): this preset depends on the core,
# the core does NOT depend on the preset — so ``ontology.py`` never imports ``semconv``
# and the module-import graph stays acyclic (``ooptdd.ontology`` is a pure sink).
Ontology.register_preset("gen_ai", gen_ai_ontology)
