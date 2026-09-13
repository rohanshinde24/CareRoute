"""The provider-ranking investigation as an explicit state graph.

This is an orchestration alternative to ProviderInvestigator's loop, selected by
AGENT_RUNTIME. It exists to make the investigation's shape legible and testable
as a graph, and to be measured against the handwritten version - not to change
what the investigation is allowed to do.

The deterministic policy is *reused*, not restated: `_available` decides which
actions are offered and `_validate` accepts or rejects what the model returns,
both imported from the existing investigator. A second copy of those rules would
make the comparison meaningless and would give the safety boundary two places to
drift apart.

What the graph owns: which node runs next, and the turn and tool-call ceilings.
What it does not own: the candidate set, slot selection, referral state,
booking, retries across services, or the right to skip validation.
"""

from __future__ import annotations

import uuid
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from .investigation import InvestigationPolicyError, ProviderInvestigator
from .metrics import record_agent_turns, record_policy_validation
from .model_providers import (
    ProviderCandidate,
    ProviderInvestigationAction,
    ProviderInvestigationInput,
)
from .telemetry import operation, set_span_attributes


class ProviderGraphState(TypedDict, total=False):
    referral_id: uuid.UUID
    location_preference: str
    candidates: list[ProviderCandidate]
    matching: list[ProviderCandidate]
    observations: dict[str, Any]
    turn: int
    tool_calls: int
    available: list[ProviderInvestigationAction]
    decision: Any
    outcome: str
    proposed_provider_id: uuid.UUID | None


class ProviderGraphInvestigator(ProviderInvestigator):
    """Same contract, same policy, graph-shaped control flow.

    Subclassing is deliberate: `_available`, `_validate` and `_event` are the
    deterministic policy and the audit trail, and both runtimes must use exactly
    the same ones.
    """

    async def investigate(self, referral, providers) -> uuid.UUID | None:
        with operation(
            "careroute.agent.investigate.provider",
            {"careroute.agent.kind": "provider", "careroute.agent.runtime": "langgraph"},
        ) as span:
            set_span_attributes(
                span,
                {
                    "careroute.workflow.run_id": str(self.run.id),
                    "careroute.agent.candidate_count": len(providers),
                },
            )
            if not referral.location_preference:
                return None

            candidates = [ProviderCandidate(id=item.id, location=item.location) for item in providers]
            preference = referral.location_preference.casefold()
            matching = [item for item in candidates if preference in item.location.casefold()]

            # Both pre-conditions are deterministic and identical to the loop's.
            if not matching:
                record_agent_turns(0, "provider")
                self._event("provider_investigation_completed", {"outcome": "no_location_match", "turns": 0, "tool_calls": 0})
                return None
            if len(matching) > self.max_tool_calls:
                self._event("provider_investigation_completed", {"outcome": "candidate_limit", "candidate_count": len(matching), "turns": 0, "tool_calls": 0})
                return None

            final: ProviderGraphState = await self._graph().ainvoke(
                {
                    "referral_id": referral.id,
                    "location_preference": referral.location_preference,
                    "candidates": candidates,
                    "matching": matching,
                    "observations": {},
                    "turn": 0,
                    "tool_calls": 0,
                },
                # A hard stop independent of the graph's own routing, so a
                # malformed edge cannot produce an unbounded run.
                {"recursion_limit": (self.max_turns * 3) + 6},
            )
            return final.get("proposed_provider_id")

    def _graph(self):
        graph = StateGraph(ProviderGraphState)
        graph.add_node("decide", self._decide)
        graph.add_node("observe", self._observe)
        graph.add_node("finish", self._finish)
        graph.set_entry_point("decide")
        graph.add_conditional_edges("decide", self._route, {"observe": "observe", "finish": "finish"})
        graph.add_edge("observe", "decide")
        graph.add_edge("finish", END)
        return graph.compile()

    async def _decide(self, state: ProviderGraphState) -> ProviderGraphState:
        """Ask the model for one bounded action, then validate it deterministically."""
        if state["turn"] >= self.max_turns:
            record_agent_turns(self.max_turns, "provider")
            return {**state, "outcome": "turn_limit", "decision": None}

        available = self._available(state["observations"], state["tool_calls"], state["matching"])
        turn = state["turn"] + 1
        with operation("careroute.agent.turn", {"careroute.agent.kind": "provider", "careroute.agent.turn": turn}) as turn_span:
            decision = await self.model.investigate_providers(
                ProviderInvestigationInput(
                    referral_id=state["referral_id"],
                    location_preference=state["location_preference"],
                    candidate_providers=state["candidates"],
                    available_actions=available,
                    observations=state["observations"],
                )
            )
            set_span_attributes(turn_span, {"careroute.agent.action": decision.action.value})
            with operation("careroute.agent.policy.validate", {"careroute.agent.kind": "provider", "careroute.agent.action": decision.action.value}) as policy_span:
                try:
                    self._validate(decision, available, state["candidates"], state["location_preference"], state["observations"])
                except InvestigationPolicyError:
                    record_policy_validation("rejected", "provider")
                    record_agent_turns(turn, "provider")
                    # Fail closed: a rejected proposal ends the investigation by
                    # raising, exactly as the loop does. The graph does not get
                    # to retry its way past policy.
                    raise
                set_span_attributes(policy_span, {"careroute.policy.outcome": "accepted"})
                record_policy_validation("accepted", "provider")

        self._event("provider_investigation_decision", {"turn": turn, "action": decision.action.value, "evidence_sources": sorted(state["observations"])})
        return {**state, "turn": turn, "available": available, "decision": decision}

    def _route(self, state: ProviderGraphState) -> str:
        decision = state.get("decision")
        if decision is None:
            return "finish"
        if decision.action == ProviderInvestigationAction.GET_AVAILABLE_SLOTS and state["tool_calls"] < self.max_tool_calls:
            return "observe"
        return "finish"

    async def _observe(self, state: ProviderGraphState) -> ProviderGraphState:
        """The one tool this investigation may call, through the gateway."""
        decision = state["decision"]
        self.fault_injector.hit("tool:getAvailableSlots")
        result = await self.provider_gateway.get_available_slots(decision.target_provider_id, self.run.id)
        observations = {**state["observations"]}
        slots = dict(observations.get("available_slots", {}))
        slots[str(decision.target_provider_id)] = [
            {"id": str(item.id), "start_at": item.start_at.isoformat()} for item in result.items
        ]
        observations["available_slots"] = slots
        self._event(
            "provider_investigation_observation",
            {"tool": "getAvailableSlots", "provider_id": str(decision.target_provider_id), "result_count": len(result.items)},
        )
        return {**state, "observations": observations, "tool_calls": state["tool_calls"] + 1}

    async def _finish(self, state: ProviderGraphState) -> ProviderGraphState:
        decision = state.get("decision")
        turns = state["turn"]
        if decision is None:
            self._event("provider_investigation_completed", {"outcome": state.get("outcome", "turn_limit"), "turns": self.max_turns, "tool_calls": state["tool_calls"]})
            return {**state, "proposed_provider_id": None}

        record_agent_turns(turns, "provider")
        if decision.action == ProviderInvestigationAction.PROPOSE_PROVIDER:
            self._event("provider_investigation_completed", {"outcome": "provider_proposed", "provider_id": str(decision.proposed_provider_id), "turns": turns, "tool_calls": state["tool_calls"]})
            return {**state, "proposed_provider_id": decision.proposed_provider_id}

        self._event("provider_investigation_completed", {"outcome": decision.action.value, "turns": turns, "tool_calls": state["tool_calls"]})
        return {**state, "proposed_provider_id": None}
