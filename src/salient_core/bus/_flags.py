"""Typed inter-agent routing flags — the RESERVED in-process channel.

Historically these rode inside the model-supplied ``args`` dict as
``_``-prefixed keys (``args["_skip_redispatch_gate"] = True``). That channel is
stringly-typed: a mis-named flag silently defaults instead of failing. This
module makes it EXPLICIT and TYPED — a routed handler receives a ``BusFlags``
as a separate argument, and internal callers construct one directly
(``BusFlags(skip_redispatch_gate=True)``), so a typo is a validation error.

Ingress now lives in ``bus_tool`` (the wire path validates model-supplied args
against the tool's Pydantic model, dropping any ``_``-key, so a model can never
set a routing flag). ``on_event`` / ``job_capture`` are Python objects that can
only arrive in-process (a JSON wire value can never be a callable or an aliased
dict); they are excluded from serialization so the wire/trace paths never carry
them.

# MIGRATION: keep field-compatible (same names/defaults, superset only) with
# the salient app's BusFlags until its bus copy is deleted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, SkipValidation


class BusFlags(BaseModel):
    """Daemon-injected inter-agent routing flags passed to ``routed=True``
    handlers as a separate argument (never inside the wire ``args`` payload).

    ``extra='forbid'`` is the runtime backstop (no pydantic mypy plugin here):
    a mis-typed flag name fails loud at construction rather than defaulting."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True, extra="forbid")

    skip_substitute_routing: bool = False
    parent_call_id: int | None = None
    skip_redispatch_gate: bool = False
    swarm_fanout_approved: bool = False
    # INERT in the kernel: the bus only FORWARDS this into `runner.submit`, which
    # stamps the child `Job.verification_leg`; the kernel never branches on it. It
    # completes the carry-channel for the verification seam core already ships
    # half of (Job.verification_leg + submit(verification_leg=) were unreachable
    # through the bus without it). A downstream verification subsystem (a skin)
    # sets it at dispatch and reads the stamped Job to scope the leg's trace.
    # INVARIANT: core may carry this bit, but must NEVER read/branch on it.
    verification_leg: bool = False
    # Detached delegation opt-in. Default False = structured concurrency: a
    # timed-out or caller-cancelled ``ask_agent`` STOPS its child runner
    # (``cancel_job``) instead of leaving it burning tokens after the caller
    # gave up. Set True only for deliberate fire-and-forget where the child is
    # meant to outlive its caller's await (it then keeps its own Job identity
    # and runs to its own completion/timeout).
    detach: bool = False
    # In-process ONLY: never serialized (excluded from model_dump/schema) and
    # kept out of reprs/traces. The wire path never constructs BusFlags by
    # validation, so a callable can't arrive from JSON.
    on_event: Callable[..., Any] | None = Field(default=None, exclude=True, repr=False)
    # In-process ONLY write-back sink: a producer (ask_consensus) passes a dict
    # it HOLDS, and the routed handler writes the dispatched child Job id into it
    # (`flags.job_capture["job_id"] = child_job.id`) so the producer can scope a
    # trace query to that specific dispatch. The reply envelope deliberately
    # keeps the id off the model-visible wire; this is the only carrier.
    #
    # `SkipValidation` is LOAD-BEARING, not decoration: with a plain
    # `dict | None` annotation pydantic-core REBUILDS the dict during validation
    # (a shallow copy), which severs the alias — `BusFlags(job_capture=cap)
    # .job_capture is cap` would be False and the write-back would land in a dict
    # nobody holds. SkipValidation passes the object through untouched, so the
    # producer and handler share one dict. Excluded from dump/repr like on_event;
    # never on the wire (the golden asserts `job_capture`/`_job_capture` appear
    # in no schema). Do NOT move this onto the args model — validate→model_dump
    # there would sever it the same way.
    job_capture: Annotated[dict[str, Any] | None, SkipValidation] = Field(
        default=None, exclude=True, repr=False
    )


_NO_FLAGS = BusFlags()
