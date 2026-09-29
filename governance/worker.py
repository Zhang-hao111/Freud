"""Hadoop Streaming workers for MovieLens inspection, cleaning and scoring."""

import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone


TABLES = {"ratings": 4, "users": 5, "movies": 3}
AGES = {1, 18, 25, 35, 45, 50, 56}
GENRES = {"Action", "Adventure", "Animation", "Children's", "Comedy", "Crime", "Documentary", "Drama", "Fantasy", "Film-Noir", "Horror", "Musical", "Mystery", "Romance", "Sci-Fi", "Thriller", "War", "Western"}
MIN_TIME = 946684800
MAX_TIME = 1046476799
RECENT_TIME = 1014854400
DIMENSIONS = ("Accurate", "Complete", "Unique", "Up-to-date", "Consistent")


def reference_ids():
    result = {}
    for table in ("users", "movies"):
        with open(f"{table}.dat", "rb") as source:
            result[table] = {
                int(fields[0]) for line in source
                if (fields := line.decode("iso-8859-1").strip().split("::"))
                and fields[0].isdigit() and int(fields[0]) > 0
            }
    return result


def parse(table, raw, references):
    fields = raw.rstrip("\r\n").split("::")
    issues = set()
    if len(fields) != TABLES[table]:
        issues.add("field_count")
        return None, issues
    if any(not field.strip() for field in fields):
        issues.add("missing")
    values = [field.strip() for field in fields]
    if values != fields:
        issues.add("format")
    try:
        identifier = int(values[0])
        if identifier <= 0 or values[0] != str(identifier):
            issues.add("domain")
    except ValueError:
        issues.add("domain")
        return None, issues
    if table == "ratings":
        try:
            movie_id, rating, timestamp = map(int, values[1:])
            if movie_id <= 0 or values[1] != str(movie_id) or values[2] != str(rating) or values[3] != str(timestamp):
                issues.add("domain")
            if rating not in range(1, 6):
                issues.add("domain")
            if not MIN_TIME <= timestamp <= MAX_TIME:
                issues.add("timestamp")
            if identifier not in references["users"] or movie_id not in references["movies"]:
                issues.add("reference")
        except ValueError:
            issues.add("domain")
            return None, issues
        normalized = [str(identifier), str(movie_id), str(rating), str(timestamp)]
        key = f"ratings:{identifier:010d}:{movie_id:010d}:{timestamp:012d}"
    elif table == "users":
        if values[1] not in {"M", "F"} or not values[2].isdigit() or int(values[2]) not in AGES or not values[3].isdigit() or int(values[3]) not in range(21):
            issues.add("domain")
        if not re.fullmatch(r"[0-9]{5}(?:-[0-9]{4})?", values[4]):
            issues.add("domain")
        normalized = [str(identifier), *values[1:]]
        key = f"users:{identifier:010d}"
    else:
        genres = values[2].split("|")
        if not values[1] or any(genre not in GENRES for genre in genres):
            issues.add("domain")
        if len(genres) != len(set(genres)):
            issues.add("format")
        normalized = [str(identifier), values[1], "|".join(dict.fromkeys(genres))]
        key = f"movies:{identifier:010d}"
    if normalized != fields:
        issues.add("format")
    return {"key": key, "fields": normalized, "value": "::".join(normalized)}, issues


def audit_map():
    source_path = os.environ.get("mapreduce_map_input_file") or os.environ.get("map_input_file", "")
    table = next((name for name in TABLES if f"{name}.dat" in source_path), None)
    if table is None:
        raise RuntimeError(f"Cannot identify input table: {source_path}")
    references = reference_ids() if table == "ratings" else None
    for raw_bytes in sys.stdin.buffer:
        raw = raw_bytes.decode("iso-8859-1").rstrip("\r\n")
        parsed, issues = parse(table, raw, references)
        key = parsed["key"] if parsed else f"{table}:invalid:{hashlib.sha256(raw_bytes).hexdigest()}"
        record = {"table": table, "raw": raw, "value": parsed["value"] if parsed else None,
                  "issues": sorted(issues)}
        print(f"{key}\t{json.dumps(record, ensure_ascii=False, separators=(',', ':'))}")


def emit_group(group):
    if not group:
        return
    values = {record["value"] for record in group}
    conflict = len(values) > 1 and not any(record["value"] is None for record in group)
    for index, record in enumerate(group):
        issues = set(record["issues"])
        if conflict:
            issues.add("conflict")
        elif index:
            issues.add("duplicate")
        record["issues"] = sorted(issues)
        print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def audit_reduce():
    last_key, group = None, []
    for line in sys.stdin:
        key, payload = line.rstrip("\n").split("\t", 1)
        if last_key is not None and key != last_key:
            emit_group(group)
            group = []
        group.append(json.loads(payload))
        last_key = key
    emit_group(group)


def clean_map():
    references = reference_ids()
    critical = {"field_count", "missing", "domain", "timestamp", "reference", "conflict"}
    for line in sys.stdin:
        record = json.loads(line)
        issues = set(record["issues"])
        table = record["table"]
        if table == "ratings" and record["value"]:
            user_id, movie_id = map(int, record["value"].split("::")[:2])
            if user_id not in references["users"] or movie_id not in references["movies"]:
                issues.add("reference")
        if issues & critical:
            action = "quarantine"
        elif "duplicate" in issues:
            action = "deduplicate"
        elif "format" in issues:
            action = "repair"
        else:
            action = "keep"
        record["issues"] = sorted(issues)
        record["action"] = action
        print(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def empty_summary():
    return {"total": 0, "tables": {}, "issues": {}, "dimensions": {}, "days": {}, "examples": []}


def score_map():
    for line in sys.stdin:
        record = json.loads(line)
        issues = set(record["issues"])
        table = record["table"]
        summary = empty_summary()
        summary["total"] = 1
        summary["tables"] = {table: 1}
        summary["issues"] = {issue: 1 for issue in issues}
        complete = not (issues & {"field_count", "missing"})
        accurate = not (issues & {"field_count", "missing", "domain", "timestamp"})
        unique = record["value"] is not None and not (issues & {"duplicate", "conflict"})
        consistent = not (issues & {"field_count", "domain", "timestamp", "reference", "conflict", "format"})
        flags = {"Accurate": accurate, "Complete": complete, "Unique": unique, "Consistent": consistent}
        for dimension, passed in flags.items():
            summary["dimensions"][dimension] = [int(passed), 1]
        if table == "ratings":
            try:
                timestamp = int(record["value"].split("::")[3])
            except (ValueError, IndexError, AttributeError):
                timestamp = 0
            summary["dimensions"]["Up-to-date"] = [int(RECENT_TIME <= timestamp <= MAX_TIME), 1]
            if MIN_TIME <= timestamp <= MAX_TIME and not issues & {"duplicate", "conflict", "reference"}:
                day = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
                summary["days"] = {day: 1}
        if issues:
            summary["examples"] = [{"table": table, "raw": record["raw"], "issues": sorted(issues)}]
        print("summary\t" + json.dumps(summary, ensure_ascii=False, separators=(",", ":")))


def merge_summaries(lines):
    total = 0
    tables, issues, days = Counter(), Counter(), Counter()
    dimensions = defaultdict(lambda: [0, 0])
    examples = []
    represented = set()
    for line in lines:
        payload = line.rstrip("\n").split("\t", 1)[-1]
        item = json.loads(payload)
        total += item["total"]
        tables.update(item["tables"])
        issues.update(item["issues"])
        days.update(item["days"])
        for name, pair in item["dimensions"].items():
            dimensions[name][0] += pair[0]
            dimensions[name][1] += pair[1]
        for example in item["examples"]:
            new_issues = set(example["issues"]) - represented
            if len(examples) < 12 and new_issues:
                examples.append(example)
                represented.update(example["issues"])
    return {"total": total, "tables": dict(tables), "issues": dict(issues),
            "dimensions": dict(dimensions), "days": dict(days), "examples": examples}


def score_combine():
    print("summary\t" + json.dumps(merge_summaries(sys.stdin), ensure_ascii=False, separators=(",", ":")))


def score_reduce():
    summary = merge_summaries(sys.stdin)
    summary["scores"] = {
        name: round(100 * summary["dimensions"][name][0] / summary["dimensions"][name][1], 4)
        if name in summary["dimensions"] and summary["dimensions"][name][1] else None
        for name in DIMENSIONS
    }
    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    {"audit-map": audit_map, "audit-reduce": audit_reduce, "clean-map": clean_map,
     "score-map": score_map, "score-combine": score_combine,
     "score-reduce": score_reduce}[sys.argv[1]]()
