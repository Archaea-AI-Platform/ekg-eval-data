"""Save a graph as SQLite in query-master's own storage shape (app/ekg/models.py).

The four tables (ekg_registry_types, ekg_registry_links, ekg_records, ekg_links) are generated from
query-master's SQLAlchemy models at run time, so a column added there appears here. Postgres-only
parts are translated: ARRAY and JSONB become JSON text, times are UTC text, booleans 0/1. Every
check constraint has a SQLite equivalent below, and a constraint the models add without one stops the
export rather than being dropped. GIN indexes have no SQLite form and are skipped. As in the migration,
nothing is deleted and a record's identity never changes (triggers).

Rows are built the way app/ekg/schema/store.py builds them (put_record / put_link): the identity
fields and claim fields leave props for their columns, _stated goes to `stated`, and every record
and link is validated first; nothing invalid is stored. `_eval_meta` is not part of the schema: it
says which dataset, stages and registry the file was built from.
"""

from __future__ import annotations

import collections
import datetime as dt
import gzip
import json
import shutil
import sqlite3
from pathlib import Path

from . import common

HEX32 = "NOT GLOB '*[^0-9a-f]*'"
METHODS = "('field','integration','template_rule','text_rule','convention','snapshot','inferred','llm_judge','human')"
STATUSES = "('proposed','active','disputed','superseded','rejected','retracted','winner','planned')"
PRECISIONS = "('time','minute','day','month','year','unknown','approximate')"
CHECKS = {
    ("ekg_registry_links", "time_model"): "time_model IN ('interval','point','timeless','none')",
    ("ekg_records", "id_format"): f"length(id) = 34 AND substr(id, 1, 2) = 'n_' AND substr(id, 3) {HEX32}",
    ("ekg_records", "scope_nonempty"): "json_type(scope) = 'array' AND json_array_length(scope) > 0",
    ("ekg_records", "props_object"): "json_type(props) = 'object'",
    ("ekg_records", "key_matches"): "substr(key, 1, length(tenant_id) + 1) = tenant_id || ':' "
                                    "AND instr(substr(key, length(tenant_id) + 2), ':' || type || ':') > 0",
    ("ekg_records", "acl_from_not_self"): "acl_from IS NULL OR acl_from <> id",
    ("ekg_links", "claim_id_format"): f"length(claim_id) = 34 AND substr(claim_id, 1, 2) = 'c_' AND substr(claim_id, 3) {HEX32}",
    ("ekg_links", "canon_dir"): "canon_dir IN ('forward','inverse')",
    ("ekg_links", "method"): f"method IN {METHODS}",
    ("ekg_links", "status"): f"status IN {STATUSES}",
    ("ekg_links", "support"): "support IN ('any','all')",
    ("ekg_links", "at_source"): "at_source IN ('tool','event','snapshot')",
    ("ekg_links", "confidence"): "confidence >= 0 AND confidence <= 1",
    ("ekg_links", "support_count"): "support_count >= 1",
    ("ekg_links", "interval_order"): "valid_from IS NULL OR valid_to IS NULL OR valid_to > valid_from",
    ("ekg_links", "valid_from_precision"): f"valid_from_precision IS NULL OR valid_from_precision IN {PRECISIONS}",
    ("ekg_links", "valid_to_precision"): f"valid_to_precision IS NULL OR valid_to_precision IN {PRECISIONS}",
    ("ekg_links", "at_precision"): f"at_precision IS NULL OR at_precision IN {PRECISIONS}",
    ("ekg_links", "derived_has_rule"): "NOT derived OR rule_id IS NOT NULL",
    ("ekg_links", "acl_array"): "json_type(acl) = 'array'",
    ("ekg_links", "props_object"): "json_type(props) = 'object'",
}
EXPRESSION_INDEXES = {  # indexes on expressions, written for SQLite's JSON functions
    "ix_ekg_records_property_claims": "CREATE INDEX ix_ekg_records_property_claims ON ekg_records "
        "(tenant_id, json_extract(props, '$.node'), json_extract(props, '$.key'), json_extract(props, '$.valid_from')) "
        "WHERE type = 'PropertyClaim'",
}
TRIGGERS = [
    "CREATE TRIGGER ekg_records_no_delete BEFORE DELETE ON ekg_records BEGIN "
    "SELECT RAISE(ABORT, 'ekg_records: nothing is deleted (tombstone instead)'); END",
    "CREATE TRIGGER ekg_links_no_delete BEFORE DELETE ON ekg_links BEGIN "
    "SELECT RAISE(ABORT, 'ekg_links: nothing is deleted (set valid_to instead)'); END",
    "CREATE TRIGGER ekg_records_identity BEFORE UPDATE OF id, tenant_id, type, key ON ekg_records BEGIN "
    "SELECT RAISE(ABORT, 'ekg_records: identity columns never change'); END",
]
# The root schema (ekg-schema.html v0.8) sketched `node` and `claim` tables; query-master's v1.0 models
# store the same envelopes as ekg_records and ekg_links. Read-only views under the root's names and columns.
ROOT_VIEWS = [
    "CREATE VIEW node AS SELECT id, tenant_id, type, cores, key, source_tool, native_id, observed_at, "
    "scope AS _acl, acl_from, props, stated FROM ekg_records WHERE tombstoned_at IS NULL",
    "CREATE VIEW claim AS SELECT claim_id, tenant_id, a AS subject, predicate, type AS subtype, b AS object, props, "
    "method, rule_id, rule_id AS derivation_run, confidence, evidence, support, valid_from, valid_to, at, observed_at, "
    "acl AS _acl, canon_dir, status FROM ekg_links",
]
NOW = "(strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
JSON_TYPES = {"ARRAY", "JSONB", "JSON"}


def _sql_type(col):
    name = type(col.type).__name__.upper()
    if name in JSON_TYPES:
        return "TEXT", True
    return {"BOOLEAN": "INTEGER", "FLOAT": "REAL", "INTEGER": "INTEGER"}.get(name, "TEXT"), False


def _default(col, is_json):
    d = col.server_default
    if d is None:
        return ""
    raw = str(getattr(d.arg, "text", d.arg)).strip()
    if raw == "now()":
        return f" DEFAULT {NOW}"
    if raw == "false":
        return " DEFAULT 0"
    if raw == "true":
        return " DEFAULT 1"
    if is_json:
        raw = raw.split("::")[0].strip("'")
        return " DEFAULT '[]'" if raw == "{}" and type(col.type).__name__.upper() == "ARRAY" else f" DEFAULT '{raw}'"
    return f" DEFAULT {raw}" if raw.lstrip("-").isdigit() else f" DEFAULT '{raw}'"


def _constraint_name(table, c):
    name = str(c.name)
    prefix = f"ck_{table}_"
    return name[len(prefix):] if name.startswith(prefix) else name


def ddl(models):
    """CREATE statements for the EKG tables, from query-master's models."""
    stmts, skipped = [], []
    for model in (models.RegistryType, models.RegistryLink, models.Record, models.Link):
        t = model.__table__
        cols = []
        for c in t.columns:
            typ, is_json = _sql_type(c)
            line = f"{c.name} {typ}"
            if c.primary_key:
                line += " PRIMARY KEY"
            elif not c.nullable:
                line += " NOT NULL"
            line += _default(c, is_json)
            if is_json:
                line += f" CHECK (json_valid({c.name}))" if c.nullable is False or c.primary_key else \
                        f" CHECK ({c.name} IS NULL OR json_valid({c.name}))"
            if typ == "INTEGER" and type(c.type).__name__.upper() == "BOOLEAN":
                line += f" CHECK ({c.name} IN (0, 1))"
            for fk in c.foreign_keys:
                line += f" REFERENCES {fk.column.table.name}({fk.column.name}) DEFERRABLE INITIALLY DEFERRED"
            cols.append(line)
        for c in t.constraints:
            if type(c).__name__ != "CheckConstraint":
                continue
            name = _constraint_name(t.name, c)
            if (t.name, name) not in CHECKS:
                raise RuntimeError(f"{t.name}: check constraint {name!r} ({c.sqltext}) has no SQLite translation; add it to CHECKS")
            cols.append(f"CONSTRAINT ck_{t.name}_{name} CHECK ({CHECKS[(t.name, name)]})")
        stmts.append(f"CREATE TABLE {t.name} (\n  " + ",\n  ".join(cols) + "\n)")
        for ix in sorted(t.indexes, key=lambda i: i.name):
            pg = ix.dialect_options["postgresql"]
            if ix.name in EXPRESSION_INDEXES:
                stmts.append(EXPRESSION_INDEXES[ix.name])
            elif pg.get("using") == "gin":
                skipped.append(f"{ix.name} (GIN: no SQLite equivalent)")
            elif all(hasattr(e, "name") and e.name in t.c for e in ix.expressions):
                where = f" WHERE {pg['where']}" if pg.get("where") is not None else ""
                stmts.append(f"CREATE {'UNIQUE ' if ix.unique else ''}INDEX {ix.name} ON {t.name} "
                             f"({', '.join(e.name for e in ix.expressions)}){where}")
            else:
                skipped.append(f"{ix.name} (expression index without a SQLite translation)")
    return stmts + TRIGGERS + ROOT_VIEWS, skipped


# ---------------------------------------------------------------- rows, as store.py builds them

LINK_COLUMNS = ("claim_id", "predicate", "canon_dir", "method", "confidence", "evidence", "support", "status",
                "at_source", "observed_at", "valid_from", "valid_to", "at", "rule_id", "derived", "end_basis",
                "end_evidence", "bound_link", "scope_override", "support_count")


def _j(v):
    return None if v is None else json.dumps(v, ensure_ascii=False, sort_keys=True)


def record_row(r, observed_at, sha):
    p = dict(r["props"])
    stated = p.pop("_stated", None)
    p.pop("_stub", None)
    key, native, tenant, tool = p.pop("_key"), p.pop("native_id"), p.pop("tenant_id"), p.pop("source_tool")
    return {"id": r["id"], "tenant_id": tenant, "type": r["type"], "cores": _j(r.get("cores", [])), "key": key,
            "native_id": native, "source_tool": tool, "scope": _j(r["scope"]), "acl_from": r.get("acl_from"),
            "props": _j(p), "stated": _j(stated), "observed_at": observed_at, "registry_sha256": sha}


def link_row(lk, tenant, sha):
    p = dict(lk["props"])
    cols = {k: p.pop(k, None) for k in LINK_COLUMNS}
    stated = p.pop("_stated", None)
    precisions = {f"{t}_precision": p.pop(f"{t}_precision", None) for t in ("valid_from", "valid_to", "at")}
    return {**cols, **precisions, "tenant_id": tenant, "type": lk["type"], "a": lk["a"], "b": lk["b"],
            "acl": _j(lk.get("acl") or []), "props": _j(p), "stated": _j(stated), "registry_sha256": sha,
            "evidence": _j(cols["evidence"] or []), "end_evidence": _j(cols["end_evidence"]),
            "scope_override": _j(cols["scope_override"]), "derived": int(bool(cols["derived"])),
            "support_count": cols["support_count"] or 1, "support": cols["support"] or "any",
            "status": cols["status"] or "active"}


def _rows(path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def save(graph, query_master: Path, db_path: Path | None = None, log=print):
    """One graph folder, or several: each folder's records/links.jsonl (adapters), overlaid with its llm/
    (LLM stage) -> one SQLite file.

    Several folders make one combined graph. Record ids come from the tenant and the source's own key, so
    the same person, address or ticket built from two downloads is one row, and a stub one download left for
    an item outside its window is replaced by the real record from the download that has it: its links then
    reach that record. A real record is never replaced by a stub."""
    reg = common.bind(query_master)
    from app.ekg import models
    graphs = [Path(x) for x in (graph if isinstance(graph, (list, tuple)) else [graph])]
    if db_path is None and len(graphs) > 1:
        raise ValueError("several graphs need an explicit output file")
    db_path = Path(db_path or graphs[0] / "graph.sqlite")
    stages = {}
    for gp in graphs:
        name = gp.name if len(graphs) > 1 else ""
        stages[f"{name}:adapters".lstrip(":")] = (_rows(gp / "records.jsonl"), _rows(gp / "links.jsonl"))
        stages[f"{name}:llm".lstrip(":")] = (_rows(gp / "llm" / "records.jsonl"), _rows(gp / "llm" / "links.jsonl"))
    records, stage_of, observed_of = {}, {}, {}
    merged = collections.Counter()
    for stage, (recs, lks) in stages.items():
        when = next((lk["props"].get("observed_at") for lk in lks), None)
        for r in recs:  # a later stage's copy of a record (an LLM-filled field) replaces the adapter's
            old = records.get(r["id"])
            if old is not None and r["props"].get("_stub") and not old["props"].get("_stub"):
                continue
            if old is not None and stage.split(":")[0] != stage_of[r["id"]].split(":")[0]:
                merged["stub replaced by the real record" if old["props"].get("_stub") else "in more than one graph"] += 1
            records[r["id"]] = r
            stage_of[r["id"]] = stage
            observed_of[r["id"]] = when
    links, link_stage = {}, {}
    for stage, (_, lks) in stages.items():
        for lk in lks:
            links[lk["props"]["claim_id"]] = lk
            link_stage[lk["props"]["claim_id"]] = stage
    observed = next((lk["props"]["observed_at"] for lk in links.values()), None) or \
        dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    report = {"records": collections.Counter(), "links": collections.Counter(), "invalid": collections.Counter()}
    log(f"  validating {len(records):,} records and {len(links):,} links")
    rec_rows = []
    for rid, r in records.items():
        res = common._vrec(r)
        if not res.ok:
            report["invalid"][f"record {r['type']}: {res.errors[0][:100]}"] += 1
            continue
        rec_rows.append(record_row(r, observed_of.get(rid) or observed, reg.spec_sha256))
        report["records"][stage_of[rid]] += 1
    stored = {row["id"]: row for row in rec_rows}
    link_rows = []
    for cid, lk in links.items():
        a, b = stored.get(lk["a"]), stored.get(lk["b"])
        if not (a and b):
            report["invalid"][f"link {lk['type']}: an end is not a stored record"] += 1
            continue
        res = common._vlink(lk, a["type"], b["type"])
        if not res.ok:
            report["invalid"][f"link {lk['type']}: {res.errors[0][:100]}"] += 1
            continue
        link_rows.append(link_row(lk, a["tenant_id"], reg.spec_sha256))
        report["links"][link_stage[cid]] += 1

    tmp = db_path.with_suffix(".sqlite.tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.execute("PRAGMA foreign_keys = ON")
    statements, skipped = ddl(models)
    for s in statements:
        con.execute(s)
    con.execute("CREATE TABLE _eval_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def insert(table, rows):
        if rows:
            cols = list(rows[0])
            con.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                            [tuple(r[c] for c in cols) for r in rows])

    with con:  # one transaction: deferred foreign keys are checked at commit
        insert("ekg_registry_types", [{"name": t.name, "cores": _j(list(t.cores)), "pillar": t.pillars[0],
                                       "spec_sha256": reg.spec_sha256} for t in reg.records.values()])
        insert("ekg_registry_links", [{"name": lk.name, "relation": lk.relation, "internal": int(lk.internal),
                                       "time_model": lk.time, "from_types": _j(list(lk.from_types)),
                                       "to_types": _j(list(lk.to_types)), "spec_sha256": reg.spec_sha256}
                                      for lk in reg.links.values()])
        insert("ekg_records", rec_rows)
        insert("ekg_links", link_rows)
        meta = {"graph": str(graphs[0]) if len(graphs) == 1 else json.dumps([str(x) for x in graphs]),
                "records_merged_across_graphs": dict(merged), "schema_version": reg.schema_version, "registry_sha256": reg.spec_sha256,
                "built_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "observed_at": observed,
                "records_by_stage": dict(report["records"]), "links_by_stage": dict(report["links"]),
                "invalid_skipped": dict(report["invalid"]), "indexes_skipped": skipped,
                "stages": "adapters = method field..convention/inferred; llm = method llm_judge, source_tool derived"}
        insert("_eval_meta", [{"key": k, "value": v if isinstance(v, str) else json.dumps(v)} for k, v in meta.items()])
    con.execute("PRAGMA optimize")
    con.close()
    tmp.replace(db_path)
    gz = db_path.with_name(db_path.name + ".gz")  # the copy to commit: GitHub refuses files over 100 MB
    with open(db_path, "rb") as src, gzip.open(gz, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst, 1 << 20)
    return {"sqlite": str(db_path), "size_mb": round(db_path.stat().st_size / 1e6, 1),
            "gzip_mb": round(gz.stat().st_size / 1e6, 1), **{k: v for k, v in meta.items()
            if k in ("records_by_stage", "links_by_stage", "invalid_skipped", "indexes_skipped",
                     "records_merged_across_graphs")}}
