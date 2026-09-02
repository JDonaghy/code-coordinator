"""Shared OpenAPI 3 builder for the three hand-rolled Starlette apps (#757).

``coord/agent_app.py`` (7433), ``coord/serve_app.py`` (7435), and
``coord/dashboard/server.py`` (7434) each declare a plain list of Starlette
``Route`` objects with no machine-readable contract. This module gives every
app a way to serve ``GET /openapi.json`` + a browsable ``GET /docs`` page
(Swagger UI) built from the *same* Python types that already define the wire
contract, mirroring the #750 codegen approach (introspect
``dataclasses.fields()`` / ``typing.get_type_hints()`` rather than
hand-writing a parallel schema):

- :func:`dataclass_schema` walks a dataclass (recursively, for nested
  dataclass fields) into a JSON Schema, registering it under
  ``components/schemas`` and returning a ``$ref``. This is what fully
  specifies ``POST /assign`` (``AssignmentSpec`` request / ``AgentAssignment``
  response) on the agent app.
  ``GET /board`` on the daemon app is specified the same way, from the
  explicit wire DTOs in ``coord/board_schema.py`` (#1849). It used to be
  built by ``PRAGMA table_info``-introspecting a live migrated SQLite
  connection — the daemon's wire schema literally *was* the DDL
  (``coord/db.py``) — which made every migration a silent wire change and
  made the storage engine's type system load-bearing on a three-language
  contract. That helper is gone; nothing in this module knows about SQLite.
- :func:`build_spec` assembles the OpenAPI 3.0.3 document.
- :func:`openapi_and_docs_routes` returns the two ``Route`` objects
  (``/openapi.json`` serving the spec, ``/docs`` serving a Swagger UI page)
  every ``build_app()`` appends to its route list.
- :func:`declared_routes` / :func:`spec_routes` extract ``(method, path)``
  sets from, respectively, the real Starlette route table and the generated
  spec, so a test can assert they're identical and the spec can't silently
  drift from the actual routes (the #757 acceptance criterion).

This is the intended input for #750's codegen: once a surface's OpenAPI
``components/schemas`` are populated here, the TS/Rust generators can point
at ``GET /openapi.json`` instead of (or in addition to) introspecting the
Python dataclasses directly.
"""

from __future__ import annotations

import dataclasses
import types as _types
import typing
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import BaseRoute, Route

OPENAPI_VERSION = "3.0.3"


# ── dataclass → JSON Schema ──────────────────────────────────────────────────

def _scalar_schema(tp: object) -> dict[str, Any] | None:
    if tp is str:
        return {"type": "string"}
    if tp is bool:
        return {"type": "boolean"}
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    return None


def json_schema_for(tp: object, components: dict[str, Any]) -> dict[str, Any]:
    """Map a resolved Python type (``typing.get_type_hints`` output) to a
    JSON Schema fragment, registering any nested dataclass into
    ``components`` and returning a ``$ref`` for it.

    Mirrors ``scripts/codegen.py``'s ``ts_type()`` structurally, just
    targeting JSON Schema (OpenAPI 3.0's dialect — ``nullable: true`` rather
    than a ``"null"`` member of a ``type`` array) instead of TypeScript.
    """
    if tp is type(None):
        return {"type": "null"}
    if tp is typing.Any:
        return {}
    if isinstance(tp, type):
        scalar = _scalar_schema(tp)
        if scalar is not None:
            return scalar
        if dataclasses.is_dataclass(tp):
            return dataclass_schema(tp, components)
        if tp is dict:
            return {"type": "object"}
        if tp is list:
            return {"type": "array", "items": {}}

    origin = typing.get_origin(tp)
    args = typing.get_args(tp)

    if origin in (list, typing.List):  # noqa: UP006
        (inner,) = args
        return {"type": "array", "items": json_schema_for(inner, components)}
    if origin in (dict, typing.Dict):  # noqa: UP006
        if len(args) == 2:
            return {
                "type": "object",
                "additionalProperties": json_schema_for(args[1], components),
            }
        return {"type": "object"}
    if origin is typing.Union or origin is _types.UnionType:
        non_none = [a for a in args if a is not type(None)]
        nullable = len(non_none) != len(args)
        if len(non_none) == 1:
            schema = dict(json_schema_for(non_none[0], components))
        else:
            schema = {"anyOf": [json_schema_for(a, components) for a in non_none]}
        if nullable:
            schema["nullable"] = True
        return schema

    raise TypeError(
        f"coord/openapi.py: no JSON Schema mapping for Python type {tp!r} — "
        "add one to json_schema_for()."
    )


def dataclass_schema(cls: type, components: dict[str, Any]) -> dict[str, Any]:
    """Register *cls* (a dataclass) into ``components`` and return a ``$ref``.

    Idempotent — a dataclass already registered is not re-walked, so cyclic /
    repeated references (e.g. several endpoints sharing ``Assignment``) are
    safe.
    """
    name = cls.__name__
    ref = {"$ref": f"#/components/schemas/{name}"}
    if name in components:
        return ref

    # Reserve the slot before recursing so a self-referential dataclass
    # doesn't recurse forever.
    components[name] = {}
    hints = typing.get_type_hints(cls)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for f in dataclasses.fields(cls):
        properties[f.name] = json_schema_for(hints[f.name], components)
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:  # type: ignore[misc]
            required.append(f.name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    components[name] = schema
    return ref


# ── spec assembly ─────────────────────────────────────────────────────────

def build_spec(
    *,
    title: str,
    version: str,
    description: str = "",
    paths: dict[str, Any],
    components: dict[str, Any] | None = None,
    servers: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Assemble a minimal OpenAPI 3.0.3 document."""
    spec: dict[str, Any] = {
        "openapi": OPENAPI_VERSION,
        "info": {"title": title, "version": version, "description": description},
        "paths": paths,
    }
    if servers:
        spec["servers"] = servers
    if components:
        spec["components"] = {"schemas": components}
    return spec


def openapi_and_docs_routes(
    spec: dict[str, Any],
    *,
    openapi_path: str = "/openapi.json",
    docs_path: str = "/docs",
) -> list[BaseRoute]:
    """Return the ``[GET /openapi.json, GET /docs]`` routes every app appends.

    ``/docs`` is a small Swagger UI page (CDN-hosted ``swagger-ui-dist``,
    pinned version) pointed at ``/openapi.json`` — no new Python dependency,
    consistent with how e.g. FastAPI's default docs page works.
    """

    async def openapi_json(_request: Request) -> JSONResponse:
        return JSONResponse(spec)

    title = spec.get("info", {}).get("title", "API")
    docs_html = f"""<!DOCTYPE html>
<html>
<head>
  <title>{title} — docs</title>
  <meta charset="utf-8" />
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css" />
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.onload = () => {{
      window.ui = SwaggerUIBundle({{
        url: "{openapi_path}",
        dom_id: "#swagger-ui",
      }});
    }};
  </script>
</body>
</html>"""

    async def docs(_request: Request) -> HTMLResponse:
        return HTMLResponse(docs_html)

    return [
        Route(openapi_path, openapi_json, methods=["GET"], include_in_schema=False),
        Route(docs_path, docs, methods=["GET"], include_in_schema=False),
    ]


# ── validate a JSON value against a generated schema (#748 tie-in) ─────────

_JSON_TYPE_CHECK: dict[str, type | tuple[type, ...]] = {
    "string": str,
    # bool is an int subclass in Python — exclude it from the integer/number
    # check so a JSON `true` doesn't pass as a number.
    "integer": (int,),
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def validate_json_schema(
    instance: Any,
    schema: dict[str, Any],
    components: dict[str, Any],
    *,
    path: str = "$",
    check_required: bool = True,
    check_unknown_properties: bool = True,
) -> list[str]:
    """Validate *instance* against a schema produced by this module.

    Not a general-purpose JSON Schema validator (no external dependency is
    pulled in for it) — just enough of the dialect this module itself emits
    (``type`` as a string or a JSON-Schema-style array of strings,
    ``nullable``, ``properties``/``required``, ``items``,
    ``additionalProperties``, ``$ref``, ``anyOf``) to prove the #757 specs
    actually describe a real payload, e.g. the #748 golden ``/board``
    fixture. Returns a list of human-readable error strings; empty means
    valid.

    Two checks are independently toggleable, both on by default:

    - ``check_required``: a missing declared-required property is an error.
      A caller validating a deliberately *partial* payload (#3050: a fixture
      exercising a degraded state legitimately omits fields) passes
      ``check_required=False`` to validate shape/type without demanding
      completeness.
    - ``check_unknown_properties``: a property present on *instance* but
      absent from the schema's ``properties`` (and not covered by a
      dict-valued ``additionalProperties``) is an error — this is the #3050
      check: a payload that *invents* fields the route's schema never
      declared, e.g. a fixture impersonating a shape it doesn't match. Only
      applies to schemas that actually declare ``properties`` (a closed
      shape); a schema with no ``properties`` key at all (a bare ``{"type":
      "object"}`` used for a genuinely free-form value) stays permissive, as
      does any schema with ``additionalProperties: true``.
    """
    # Nullability is checked at THIS level, before resolving `$ref`/`anyOf`:
    # the codebase's own hand-built schemas spell "nullable $ref" as
    # `{**some_ref, "nullable": True, ...}` — a sibling key alongside `$ref`
    # (e.g. `ReportResult.chart`, `MachineRow.assignments`) — which OpenAPI's
    # own `$ref`-siblings-are-ignored rule would otherwise silently drop,
    # wrongly rejecting a legitimate `null` for those fields.
    nullable = bool(schema.get("nullable"))
    json_type = schema.get("type")
    # JSON-Schema-dialect nullability: `"type": ["string", "null"]` rather
    # than this module's own `"type": "string", "nullable": true` — a few
    # hand-built dashboard schemas (e.g. `session_response`) use this form.
    if isinstance(json_type, list):
        nullable = nullable or "null" in json_type

    if instance is None:
        if nullable or json_type == "null" or not schema:
            return []
        return [f"{path}: null but schema is not nullable ({schema.get('type')!r})"]

    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        target = components.get(name)
        if target is None:
            return [f"{path}: unresolvable $ref {schema['$ref']!r}"]
        return validate_json_schema(
            instance,
            target,
            components,
            path=path,
            check_required=check_required,
            check_unknown_properties=check_unknown_properties,
        )

    if "anyOf" in schema:
        errors_per_branch = [
            validate_json_schema(
                instance,
                branch,
                components,
                path=path,
                check_required=check_required,
                check_unknown_properties=check_unknown_properties,
            )
            for branch in schema["anyOf"]
        ]
        if any(not errs for errs in errors_per_branch):
            return []
        return [f"{path}: matched none of {len(schema['anyOf'])} anyOf branches"]

    if isinstance(json_type, list):
        non_null_types = [t for t in json_type if t != "null"]
        if len(non_null_types) > 1:
            branches = [{**schema, "type": t} for t in non_null_types]
            return validate_json_schema(
                instance,
                {"anyOf": branches},
                components,
                path=path,
                check_required=check_required,
                check_unknown_properties=check_unknown_properties,
            )
        json_type = non_null_types[0] if non_null_types else None

    if json_type is None:
        return []  # untyped ("any") schema — nothing to check

    py_type = _JSON_TYPE_CHECK.get(json_type)
    if py_type is not None and not isinstance(instance, py_type):
        return [f"{path}: expected {json_type}, got {type(instance).__name__}"]

    errors: list[str] = []
    if json_type == "object" and isinstance(instance, dict):
        properties = schema.get("properties")
        if check_required:
            for req in schema.get("required", ()):
                if req not in instance:
                    errors.append(f"{path}: missing required property {req!r}")
        addl = schema.get("additionalProperties")
        for key, value in instance.items():
            prop_schema = (properties or {}).get(key)
            if prop_schema is not None:
                errors.extend(
                    validate_json_schema(
                        value,
                        prop_schema,
                        components,
                        path=f"{path}.{key}",
                        check_required=check_required,
                        check_unknown_properties=check_unknown_properties,
                    )
                )
            elif isinstance(addl, dict):
                errors.extend(
                    validate_json_schema(
                        value,
                        addl,
                        components,
                        path=f"{path}.{key}",
                        check_required=check_required,
                        check_unknown_properties=check_unknown_properties,
                    )
                )
            elif addl is True or properties is None:
                pass  # explicitly open, or a schema with no declared shape at all
            elif check_unknown_properties:
                errors.append(f"{path}: unexpected property {key!r} not declared in schema")
    elif json_type == "array" and isinstance(instance, list):
        items_schema = schema.get("items")
        if items_schema:
            for i, item in enumerate(instance):
                errors.extend(
                    validate_json_schema(
                        item,
                        items_schema,
                        components,
                        path=f"{path}[{i}]",
                        check_required=check_required,
                        check_unknown_properties=check_unknown_properties,
                    )
                )
    return errors


# ── route-inventory drift check ──────────────────────────────────────────────

def declared_routes(routes: list[BaseRoute]) -> set[tuple[str, str]]:
    """``{(METHOD, path), ...}`` for every plain ``Route`` in *routes*.

    Skips non-``Route`` entries (``Mount``/``StaticFiles`` — not a single
    documentable JSON endpoint) and anything with ``include_in_schema=False``
    (the meta ``/openapi.json`` and ``/docs`` routes themselves), and drops
    the implicit ``HEAD``/``OPTIONS`` methods Starlette adds to every
    ``GET``/any route.
    """
    out: set[tuple[str, str]] = set()
    for route in routes:
        if not isinstance(route, Route) or not route.include_in_schema:
            continue
        for method in route.methods or ["GET"]:
            if method in ("HEAD", "OPTIONS"):
                continue
            out.add((method, route.path))
    return out


def spec_routes(spec: dict[str, Any]) -> set[tuple[str, str]]:
    """``{(METHOD, path), ...}`` declared in an OpenAPI document's ``paths``."""
    out: set[tuple[str, str]] = set()
    for path, methods in spec.get("paths", {}).items():
        for method in methods:
            out.add((method.upper(), path))
    return out
