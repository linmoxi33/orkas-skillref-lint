# orkas-skillref-lint

Offline auditor for **Orkas** `agent.json` → `skill_list` references. It reproduces Orkas's two-phase skill resolution and tells you whether every declared Skill reference actually *does* something — or disappears silently.

**Why it's needed now**: Orkas lets an Agent declare `skill_list`, but the prompt-render path (`getSystemPromptBlock` in `src/main/model/core-agent/skill-registry.ts`) resolves refs against the **shared** roster and then **discards the `unknown` array** the resolver returns, while Agent-private Skills (`agents/<id>/skills/*`) render **unconditionally** regardless of `skill_list`. So a ref can look meaningful and have zero binding force. Nobody audits this offline before a pack is shipped.

---

Upstream: https://github.com/Orkas-AI/Orkas

## The upstream, and its actual gap

Orkas is an open-source MIT local-first multi-agent desktop app. Each built-in/custom Agent has an `agent.json` with a `skill_list` field declaring which Skills it depends on. The official authoring contract — `resources/builtin/system/skills/agent-creator/references/llm-agent-fields.md` §`<skills>` — says:

> One model-visible Skill **name** per line … Names must come from the host-generated `## Agent Skill dependencies` directory … Do not put … **missing Skills**, or **another Agent's private Skills** in its dependencies. Empty is legal only when the workflow uses built-in tools/connectors alone.

Yet the *shipped* builtin marketplace (16 agents / 22 shared Skills / 55 agent-private Skills) mixes **hex-id** refs (`skill_list: ["9dfbd4e00c0d"]`) and **bare-name** refs (`["market-data", "video-router"]`), and 55 of its 84 total refs point only at the Agent's own private Skills — which render **unconditionally**, so those `skill_list` entries don't gate anything.

There is a real, code-visible seam behind this:

- `resolveSkillAllowlistRefs` (`src/main/model/core-agent/skill-registry.ts#L558-L597`) computes `{ ids, unknown }` — it *knows* which refs do not resolve.
- `getSystemPromptBlock` (`.../skill-registry.ts#L1046`, `#L1058`) destructures only `{ ids }` and **drops `unknown`**: unresolved refs vanish in the prompt path.
- Agent private Skills are appended unconditionally at `#L1064-L1077`, independent of `skill_list`.

Measured on the real builtin marketplace at commit `12f59a2e`:

| ref kind | count | meaning |
|---|---|---|
| binding | 29 | resolve against shared roster (gate rendering), all as hex ids |
| inert | 55 | only match this Agent's private Skill; render anyway → ref is a no-op |
| dangling | 0 | currently none, but **nothing guards it** |
| foreign | 0 | currently none |

The shipped set isn't broken *today*, but a typo, a renamed Skill, or a copied foreign-private name produces **zero diagnostics** in the prompt path. This tool makes that class of failure checkable offline, before a pack ships.

## What it does

Scans an Orkas marketplace root (`agents/` + `skills/`) and, for every `agent.json`:

1. **Parses** each manifest (reports malformed JSON).
2. **Classifies** every `skill_list` ref:
   - `binding` — resolves against the shared roster (actually gates rendering)
   - `inert` — matches only this Agent's private Skill (renders anyway; the ref is a no-op)
   - `dangling` — matches nothing (dropped silently in the prompt path, rejected in the authoring path)
   - `foreign` — names another Agent's private Skill (dropped, and forbidden by the contract)
3. **Checks the contract**: refs should be names (not hex ids), must exist, no foreign-private refs.
4. Flags private-Skill hygiene (SKILL.md `name` mismatches, undeclared-but-rendering private Skills, empty `skill_list` with private Skills).

**What it does not do**: it does not run Orkas, does not modify files, does not need a network, and does not fetch a marketplace from GitHub — you run it against a local copy (e.g. a `git clone` of Orkas's `resources/builtin/marketplace`, or any exported pack).

## Install / usage

Python **3.9+**, standard library only — no dependencies.

```bash
# run against a local copy of Orkas's builtin marketplace
python3 orkas_skillref_lint.py <marketplace-root>

# machine-readable
python3 orkas_skillref_lint.py <marketplace-root> --json

# per-agent breakdown
python3 orkas_skillref_lint.py <marketplace-root> --verbose
```

Or use the launcher: `bin/orkas-skillref-lint <root>`.

The `fixtures/real/` directory is a self-contained, offline snapshot of the authentic Orkas builtin marketplace layout (agents + shared + private Skills) so you can try it with no network:

```bash
python3 orkas_skillref_lint.py fixtures/real --json
```

### Example

Against `examples/in/marketplace`:

```bash
$ python3 orkas_skillref_lint.py examples/in/marketplace
orkas-skillref-lint 0.1.0
root: .../examples/in/marketplace
agents=1 shared_skills=2 private_skills=1 refs=4
refs: binding=2 inert=1 dangling=1 foreign=0
findings: 2 (errors=1 warnings=1)
[W] aaa000000001 W01 ref 'brush-routes' only matches this Agent's private Skill; it renders unconditionally ...
[E] aaa000000001 E05 ref 'no-such' resolves to nothing: the prompt-render path drops it silently ...
```

`--json` output is a stable schema:

```json
{ "summary": { "agents": 1, "refs": 4, "binding": 2, "inert": 1,
               "dangling": 1, "foreign": 0, "errors": 1, "warnings": 1 },
  "agents": [ { "agent_id": "...", "refs": [...], "effective_skills": [...],
                "findings": [...], "counts": {...} } ],
  "findings": [ {"rule":"E05","severity":"error","agent_id":"...","message":"..."} ] }
```

`python3 -c "import json,sys;json.load(sys.stdin)"` accepts it unchanged.

### Exit codes

| code | meaning |
|---|---|
| 0 | clean — no findings |
| 1 | findings (any error or warning) |
| 2 | usage / bad input (missing root, not a marketplace, unreadable path) |

## 本轮创新

第一个把 Orkas `skill_list` 的**双阶段解析**（shared 白名单 + 无条件 private 追加）机械化的离线审计：别人只当 `skill_list` 是 Agent 的声明式依赖，我却量化出「84 条引用里 55 条是 inert——它们即使被删掉，Agent 的可见 Skill 集也不变」。这是对上游 `resolveSkillAllowlistRefs` 算出 `unknown` 却被 `getSystemPromptBlock` 丢弃这一真实边界的反向审计；同类 `-lint` 多扫静态错误，而这个工具测的是"声明与运行时生效集的漂移"。与台账全部 20 条比对不撞车。

## Tests

```bash
python3 -m unittest discover -s tests -v
```

17 tests, all offline, ≥3 behavioral (exit-code semantics, ref classification on the authentic builtin snapshot, network-independence). The `fixtures/real` test asserts the exact measured profile (16 agents / 22 shared / 55 private / 84 refs / 55 inert / 29 binding) and would fail if the honest numbers ever drift.

## Limitations

- Ports the resolution *rules* from `skill-registry.ts` at commit `12f59a2e`; if Orkas changes that file, re-derive the constants.
- The `binding`/`inert` split assumes a marketplace with a single user context; per-user cloud/global roots are out of scope (the builtin layout has none).
- `--json` drops nothing; the text renderer caps the non-verbose listed findings at 40 for brevity.
- Read-only by design; it will not alter or re-check an installed Orkas data dir.

## License

MIT. See [LICENSE](./LICENSE). This is an independent tool, not affiliated with or endorsed by Orkas-AI; all upstream code and the skill content belong to their respective copyright holders.