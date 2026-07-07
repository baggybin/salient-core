# bus_tool field-constraint rubric

`@bus_tool` derives a tool's wire schema from a Pydantic model and validates
model-supplied args against it before the handler runs (see
`salient_core.bus._common.bus_tool`). This is the rubric the step-B migration
(PRs #14–#24) converged on for deciding how to type each field. Reproduce the
*reasoning*, not just the shape, when adding or changing a bus tool.

## Required vs. optional (the de-require litmus)

- **De-require** a field only when its omitted state has a single, documented,
  useful meaning *and* the default value **is** that meaning — e.g.
  `key="latest"`, `filter=""` ("all"), `length=8192`. Then a model can express
  the common intent by omitting the field.
- **Keep required** when the fallback merely *names its own absence* (a
  placeholder, not a value you'd document as a default). Exception: never let a
  required field block a **cleanup/teardown** path — there, de-require so a
  missing label can't stop the teardown (`swarm_finish.reason`).
- An all-optional model has **no** `required` key (Pydantic omits it); that is
  wire-equivalent to `required: []`.

## Numeric constraints (`ge`/`le`) — bound the quantity, not the threshold

Advertise the domain (`ge=`→`minimum`, `le=`→`maximum`) and let validation
reject out-of-domain input with a friendly, self-correcting error instead of a
silent handler clamp. But **classify the field first**:

- **Quantity** — the value *is* the thing, with an intrinsic domain
  (`confidence` ∈ [0,1], a probability). Out-of-domain is *meaningless* → bound
  it (`ge=0, le=1`).
- **Count / floor** — a self-evident invariant bounds it (`min_observations≥1`,
  `limit≥1`, `depth≥1`): `0`/negative defeat the parameter or corrupt a slice
  (`rows[:-3]` silently drops rows). Floor with `ge=1`.
- **Threshold** — compared *against* a (clamped) quantity, so its domain is
  larger than the quantity's: `min_score>1` = match-nothing, `<0` = match-all
  are both meaningful, monotonic endpoints. Leave **unbounded** (typed `float`,
  no `ge`/`le`). Rule of thumb: unbounded is safe iff out-of-range degrades
  *monotonically and predictably*; bound it if it *inverts or corrupts*.

Ceiling clamps (`le=`) are the "reject, don't silently clamp" case: `length` >
max means "give me the max", so the old silent clamp *preserved* intent — but
advertising `maximum` and erroring is still better (compliant clients never hit
it), keep any internal clamp as defense-in-depth for `.trusted` paths.

## Never silently rewrite a legitimate value

The migration's core bug class: `x = args.get("k") or DEFAULT` coalesces a
*falsy-but-valid* value to `DEFAULT`. Fix it when a legitimate value is rewritten
to a **different** legitimate value — e.g. `confidence=0.0` ("zero confidence")
was inflated to `1.0` (its opposite) on a persisted ledger. Preserve the value;
put any blank→canonical normalization in a `field_validator` (single source of
truth), and delete the handler's coalesce.

## Enums — Literal vs. advertise-only

- **Static, code-owned set** → `Literal[...]` (enforced) *plus* a
  `field_validator(mode="before")` normalizer if case/whitespace leniency is
  wanted (advertise strict, accept a normalized superset).
- **Runtime / externally-owned set** (values from a table or another module) →
  plain `str` with `json_schema_extra={"enum": list(THE_SET)}` to advertise, and
  a handler check that **errors loudly** on a miss (`rule_kind`, `grade` via
  `normalize_grade`). Advertise-without-enforce is safe *only* because the
  handler's check is a superset of the advertised set and loud on a real miss.

## Sentinels & tri-state

When a field has three non-collapsible states, use `T | None` (→ `anyOf[T,null]`)
and reserve `None` for "absent/use-default": e.g. `ttl_days` = `None` (engagement
default) / `≤0` (never) / `>0` (duration). Document all states in the field
description (an overloaded sentinel like "≤0 means never" is undiscoverable from
the schema). Guard unbounded floats that feed persisted decisions with
`allow_inf_nan=False` (a `NaN` ttl silently means "never expires").

## Defaults, descriptions, secrets, side-channels

- `_clean_tool_schema` strips the `default` keyword — so a semantic default's
  visibility (and the golden's change-tripwire) must live in the field
  **description** ("defaults to 'latest'"). Neutral defaults (`0`/`""`/`False`)
  need no description.
- Secret fields (`cred_record.value`) get `Field(repr=False)` so a logged
  traceback can't leak them; they stay in `model_dump()` for the handler.
- Routing flags and in-process write-back sinks travel on **`BusFlags`** (the
  typed `.trusted` channel), never in the args model — the wire path can't set
  them, and `model_dump()` would sever an aliased write-back dict.

## Golden discipline

Every migrated tool gains `+additionalProperties: false` (root). The
golden-master test (`tests/test_bus_schema_golden.py`) pins each wire schema
byte-for-byte; regenerate with `UPDATE_BUS_GOLDENS=1 pytest` and review the diff
— a schema change should be exactly the reviewable delta you intended.
