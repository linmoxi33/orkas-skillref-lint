#!/usr/bin/env python3
"""orkas-skillref-lint — offline auditor for Orkas agent.json `skill_list` references.

Why this exists
---------------
Orkas resolves an Agent's `skill_list` in two phases (see
`src/main/model/core-agent/skill-registry.ts`):

  1. `resolveSkillAllowlistRefs(specs, refs)` matches each ref against the
     *shared* Skill roster by id, then by frontmatter name. Refs that match
     nothing are collected into an `unknown` array — but every caller
     destructures only `{ ids }` and drops `unknown`, so they disappear with no
     diagnostic.
  2. Agent-private Skills (`agents/<id>/skills/*`) are then appended
     *unconditionally*, with no reference to `skill_list`.

Net effect: a `skill_list` entry can look meaningful and do nothing. This tool
reproduces both phases offline and labels every ref as:

  binding      -> resolved against the shared roster (actually gates rendering)
  inert        -> matches only this agent's private Skill (renders anyway;
                  removing the ref changes nothing)
  dangling     -> matches nothing; dropped silently in the prompt-render path
                  and rejected by the agent authoring path (behavior differs)
  foreign      -> names another agent's private Skill; dropped by the owner
                  gate, and rejected by the authoring path

It also checks the documented authoring contract
(`resources/builtin/system/skills/agent-creator/references/llm-agent-fields.md`):
one visible Skill *name* per line, names must exist, no foreign private Skills.

Exit codes: 0 clean, 1 findings, 2 usage/input error.
Standard library only. No network.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Iterable

TOOL = "orkas-skillref-lint"
VERSION = "0.1.0"

HEX_ID_RE = re.compile(r"^[0-9a-f]{12}$")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
NAME_LINE_RE = re.compile(r"^name\s*:\s*(.+?)\s*$", re.MULTILINE)

# Rule catalogue. severity "error" drives exit code 1; "warning" also drives 1;
# "info" never fails the run (used only for the binding breakdown).
RULES = {
    "E01": ("error", "agent.json is not valid JSON"),
    "E02": ("error", "agent_id does not match its directory name"),
    "E03": ("error", "skill_list is not a list"),
    "E04": ("error", "skill_list entry is not a non-empty string"),
    "E05": ("error", "dangling ref: matches no shared, private, or foreign Skill"),
    "E06": ("error", "foreign-private ref: names another agent's private Skill"),
    "W01": ("warning", "inert ref: matches only this agent's private Skill"),
    "W02": ("warning", "shared ref given as hex id, not the documented name"),
    "W03": ("warning", "ambiguous name: matches more than one shared Skill"),
    "W04": ("warning", "name resolves in both the shared and private scope"),
    "W05": ("warning", "duplicate skill_list entry"),
    "W06": ("warning", "private Skill renders but is not declared in skill_list"),
    "W07": ("warning", "empty skill_list but the agent ships private Skills"),
    "W08": ("warning", "SKILL.md is missing a frontmatter `name`"),
    "W09": ("warning", "private SKILL.md name differs from its directory"),
}


class InputError(Exception):
    """Bad input path or unreadable marketplace layout (exit 2)."""


# --------------------------------------------------------------------------- #
# Filesystem model
# --------------------------------------------------------------------------- #

def read_frontmatter_name(path: str) -> str | None:
    """Return the YAML frontmatter `name` value, or None if absent/unreadable."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    m = FRONTMATTER_RE.match(text)
    if not m:
        return None
    nm = NAME_LINE_RE.search(m.group(1))
    if not nm:
        return None
    value = nm.group(1).strip().strip("'\"")
    return value or None


class Skill:
    __slots__ = ("id", "name", "dir_name", "path", "agent_id")

    def __init__(self, skill_id: str, dir_name: str, path: str, agent_id: str | None):
        self.id = skill_id
        self.dir_name = dir_name
        self.path = path
        self.agent_id = agent_id  # None => shared roster
        self.name = read_frontmatter_name(path)

    @property
    def is_private(self) -> bool:
        return self.agent_id is not None


class Agent:
    __slots__ = ("agent_id", "dir_path", "manifest_path", "data", "parse_error")

    def __init__(self, agent_id: str, dir_path: str, manifest_path: str):
        self.agent_id = agent_id
        self.dir_path = dir_path
        self.manifest_path = manifest_path
        self.data: dict[str, Any] = {}
        self.parse_error: str | None = None


def _iter_subdirs(path: str) -> Iterable[str]:
    try:
        names = sorted(os.listdir(path))
    except OSError:
        return []
    return [n for n in names if os.path.isdir(os.path.join(path, n))]


def load_marketplace(root: str) -> tuple[list[Agent], list[Skill], list[Skill]]:
    """Load agents, shared Skills and agent-private Skills under a marketplace root.

    Expected layout (the shape Orkas ships under
    `resources/builtin/marketplace/`):

        <root>/agents/<agent-id>/agent.json
        <root>/agents/<agent-id>/skills/<skill-dir>/SKILL.md
        <root>/skills/<skill-id>/SKILL.md
    """
    if not os.path.isdir(root):
        raise InputError(f"not a directory: {root}")
    agents_root = os.path.join(root, "agents")
    skills_root = os.path.join(root, "skills")
    if not os.path.isdir(agents_root) and not os.path.isdir(skills_root):
        raise InputError(
            f"{root} does not look like an Orkas marketplace "
            "(expected agents/ and/or skills/)"
        )

    agents: list[Agent] = []
    private: list[Skill] = []
    for aid in _iter_subdirs(agents_root):
        adir = os.path.join(agents_root, aid)
        mpath = os.path.join(adir, "agent.json")
        agent = Agent(aid, adir, mpath)
        if os.path.isfile(mpath):
            try:
                with open(mpath, "r", encoding="utf-8", errors="replace") as fh:
                    agent.data = json.load(fh)
                if not isinstance(agent.data, dict):
                    agent.parse_error = "top-level JSON is not an object"
                    agent.data = {}
            except OSError as exc:
                agent.parse_error = str(exc)
            except json.JSONDecodeError as exc:
                agent.parse_error = f"JSONDecodeError: {exc}"
        else:
            agent.parse_error = "agent.json not found"
        agents.append(agent)

        for sk_dir in _iter_subdirs(os.path.join(adir, "skills")):
            spath = os.path.join(adir, "skills", sk_dir, "SKILL.md")
            if os.path.isfile(spath):
                private.append(Skill(sk_dir, sk_dir, spath, aid))

    shared: list[Skill] = []
    for sk_dir in _iter_subdirs(skills_root):
        spath = os.path.join(skills_root, sk_dir, "SKILL.md")
        if os.path.isfile(spath):
            shared.append(Skill(sk_dir, sk_dir, spath, None))

    return agents, shared, private


# --------------------------------------------------------------------------- #
# Resolution — mirrors resolveSkillAllowlistRefs + the unconditional append
# --------------------------------------------------------------------------- #

class SharedIndex:
    """The `specs` roster that `resolveSkillAllowlistRefs` sees: shared only."""

    def __init__(self, shared: list[Skill]):
        self.by_id: dict[str, Skill] = {}
        self.by_name: dict[str, list[Skill]] = {}
        for s in shared:
            self.by_id.setdefault(s.id, s)
            if s.name:
                self.by_name.setdefault(s.name, []).append(s)
        # Upstream sorts equal-rank candidates by id; mirror that for determinism.
        for bucket in self.by_name.values():
            bucket.sort(key=lambda s: s.id)

    def resolve(self, ref: str) -> tuple[Skill | None, bool]:
        """Return (skill, ambiguous). byId wins, then first byName candidate."""
        hit = self.by_id.get(ref)
        if hit is not None:
            return hit, False
        bucket = self.by_name.get(ref)
        if bucket:
            return bucket[0], len(bucket) > 1
        return None, False


def _private_match(ref: str, agent_id: str, private: list[Skill]) -> Skill | None:
    """Match a ref to an agent-owned private Skill by dir name, then frontmatter name."""
    owned = [s for s in private if s.agent_id == agent_id]
    for s in owned:
        if s.dir_name == ref or s.id == ref:
            return s
    for s in owned:
        if s.name and s.name == ref:
            return s
    return None


def _foreign_match(ref: str, agent_id: str, private: list[Skill]) -> Skill | None:
    for s in private:
        if s.agent_id == agent_id:
            continue
        if s.dir_name == ref or s.id == ref or (s.name and s.name == ref):
            return s
    return None


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #

def _finding(rule: str, message: str, ref: str | None = None) -> dict[str, Any]:
    severity, _ = RULES[rule]
    out: dict[str, Any] = {"rule": rule, "severity": severity, "message": message}
    if ref is not None:
        out["ref"] = ref
    return out


def audit_agent(
    agent: Agent,
    shared_index: SharedIndex,
    shared: list[Skill],
    private: list[Skill],
    all_private: list[Skill],
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    aid = agent.agent_id
    data = agent.data

    if agent.parse_error:
        findings.append(_finding("E01", f"{agent.manifest_path}: {agent.parse_error}"))

    declared_id = data.get("agent_id")
    if isinstance(declared_id, str) and declared_id and declared_id != aid:
        findings.append(
            _finding("E02", f"agent_id {declared_id!r} != directory {aid!r}")
        )

    raw = data.get("skill_list")
    refs: list[str] = []
    if raw is None:
        refs = []
    elif not isinstance(raw, list):
        findings.append(_finding("E03", f"skill_list is {type(raw).__name__}, expected list"))
        refs = []
    else:
        for item in raw:
            if isinstance(item, str) and item.strip():
                refs.append(item.strip())
            else:
                findings.append(_finding("E04", f"bad skill_list entry: {item!r}"))

    owned_private = [s for s in private if s.agent_id == aid]

    # W08/W09 — private SKILL.md metadata hygiene
    for s in owned_private:
        if not s.name:
            findings.append(
                _finding("W08", f"{os.path.relpath(s.path, agent.dir_path)} has no frontmatter name")
            )
        elif s.name != s.dir_name:
            findings.append(
                _finding(
                    "W09",
                    f"private SKILL.md name {s.name!r} != directory {s.dir_name!r}",
                    ref=s.dir_name,
                )
            )

    seen: dict[str, int] = {}
    ref_rows: list[dict[str, Any]] = []
    effective: dict[str, Skill] = {}

    for ref in refs:
        seen[ref] = seen.get(ref, 0) + 1
        if seen[ref] > 1:
            findings.append(_finding("W05", f"duplicate ref {ref!r}", ref=ref))

        shared_hit, ambiguous = shared_index.resolve(ref)
        own_private = _private_match(ref, aid, all_private)
        foreign = None if (shared_hit or own_private) else _foreign_match(ref, aid, all_private)

        if shared_hit is not None:
            effective[shared_hit.id] = shared_hit
            how = "shared-id" if shared_hit.id == ref else "shared-name"
            ref_rows.append({"ref": ref, "kind": "binding", "resolved_via": how,
                             "skill_id": shared_hit.id})
            if ambiguous:
                names = sorted(s.id for s in shared_index.by_name.get(ref, []))
                findings.append(
                    _finding("W03", f"name {ref!r} matches shared skills {names}; first wins", ref=ref)
                )
            if HEX_ID_RE.match(ref):
                findings.append(
                    _finding("W02", f"ref {ref!r} is a hex id; the contract asks for the Skill name", ref=ref)
                )
            if own_private is not None:
                findings.append(
                    _finding("W04", f"name {ref!r} exists in both shared and private scope", ref=ref)
                )
        elif own_private is not None:
            # Renders unconditionally regardless of skill_list -> no binding force.
            ref_rows.append({"ref": ref, "kind": "inert", "resolved_via": "private",
                             "skill_id": own_private.id})
            findings.append(
                _finding(
                    "W01",
                    f"ref {ref!r} only matches this agent's private Skill; it renders "
                    "unconditionally, so the ref changes nothing",
                    ref=ref,
                )
            )
        elif foreign is not None:
            ref_rows.append({"ref": ref, "kind": "foreign", "resolved_via": "foreign-private",
                             "skill_id": foreign.id})
            findings.append(
                _finding(
                    "E06",
                    f"ref {ref!r} names a private Skill of agent {foreign.agent_id!r}; "
                    "it is dropped at runtime and forbidden by the contract",
                    ref=ref,
                )
            )
        else:
            ref_rows.append({"ref": ref, "kind": "dangling", "resolved_via": "none",
                             "skill_id": None})
            findings.append(
                _finding(
                    "E05",
                    f"ref {ref!r} resolves to nothing: the prompt-render path drops it "
                    "silently while the agent authoring path throws — behavior differs by code path",
                    ref=ref,
                )
            )

    # Private Skills render whether or not they are declared.
    for s in owned_private:
        effective[s.id] = s
        declared = any(
            r == s.dir_name or r == s.id or (s.name is not None and r == s.name) for r in refs
        )
        if not declared:
            findings.append(
                _finding(
                    "W06",
                    f"private Skill {s.dir_name!r} renders but is not declared in skill_list",
                    ref=s.dir_name,
                )
            )

    if not refs and owned_private:
        findings.append(
            _finding(
                "W07",
                f"skill_list is empty but the agent ships {len(owned_private)} private Skill(s)",
            )
        )

    counts = {"binding": 0, "inert": 0, "dangling": 0, "foreign": 0}
    for row in ref_rows:
        counts[row["kind"]] += 1

    return {
        "agent_id": aid,
        "name": data.get("name"),
        "skill_list_count": len(refs),
        "refs": ref_rows,
        "effective_skills": sorted(effective),
        "counts": counts,
        "findings": findings,
    }


def audit(root: str) -> dict[str, Any]:
    agents, shared, private = load_marketplace(root)
    index = SharedIndex(shared)

    agent_reports = [
        audit_agent(a, index, shared, private, private) for a in agents
    ]

    flat: list[dict[str, Any]] = []
    for rep in agent_reports:
        for f in rep["findings"]:
            f2 = dict(f)
            f2["agent_id"] = rep["agent_id"]
            flat.append(f2)
    flat.sort(key=lambda f: (f["agent_id"], f["rule"], f.get("ref", "")))

    totals = {"binding": 0, "inert": 0, "dangling": 0, "foreign": 0}
    for rep in agent_reports:
        for k in totals:
            totals[k] += rep["counts"][k]

    errors = sum(1 for f in flat if f["severity"] == "error")
    warnings = sum(1 for f in flat if f["severity"] == "warning")

    return {
        "tool": TOOL,
        "version": VERSION,
        "root": os.path.abspath(root),
        "summary": {
            "agents": len(agents),
            "shared_skills": len(shared),
            "private_skills": len([s for s in private if s.agent_id]),
            "refs": sum(rep["skill_list_count"] for rep in agent_reports),
            "binding": totals["binding"],
            "inert": totals["inert"],
            "dangling": totals["dangling"],
            "foreign": totals["foreign"],
            "errors": errors,
            "warnings": warnings,
            "findings": len(flat),
        },
        "agents": agent_reports,
        "findings": flat,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def render_text(report: dict[str, Any], verbose: bool) -> str:
    s = report["summary"]
    lines = [
        f"{TOOL} {VERSION}",
        f"root: {report['root']}",
        (
            f"agents={s['agents']} shared_skills={s['shared_skills']} "
            f"private_skills={s['private_skills']} refs={s['refs']}"
        ),
        (
            f"refs: binding={s['binding']} inert={s['inert']} "
            f"dangling={s['dangling']} foreign={s['foreign']}"
        ),
        f"findings: {s['findings']} (errors={s['errors']} warnings={s['warnings']})",
    ]
    if verbose:
        for rep in report["agents"]:
            lines.append("")
            lines.append(
                f"## {rep['agent_id']} {rep['name'] or '(unnamed)'} "
                f"refs={rep['skill_list_count']} effective={len(rep['effective_skills'])}"
            )
            for row in rep["refs"]:
                lines.append(
                    f"  {row['kind']:<8} {row['ref']}  -> {row['skill_id']} "
                    f"({row['resolved_via']})"
                )
            for f in rep["findings"]:
                lines.append(f"  [{f['severity'][0].upper()}] {f['rule']} {f['message']}")

    shown = [f for f in report["findings"]]
    if not verbose:
        for f in shown[:40]:
            lines.append(f"[{f['severity'][0].upper()}] {f['agent_id']} {f['rule']} {f['message']}")
        if len(shown) > 40:
            lines.append(f"... {len(shown) - 40} more (re-run with --json for the full list)")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=TOOL,
        description="Audit Orkas agent.json skill_list references offline.",
    )
    parser.add_argument("root", help="marketplace root (contains agents/ and skills/)")
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    parser.add_argument("--verbose", "-v", action="store_true", help="per-agent breakdown")
    parser.add_argument("--version", action="version", version=f"{TOOL} {VERSION}")
    args = parser.parse_args(argv)

    try:
        report = audit(args.root)
    except InputError as exc:
        print(f"{TOOL}: error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"{TOOL}: error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        json.dump(report, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_text(report, args.verbose))

    return 0 if report["summary"]["findings"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
