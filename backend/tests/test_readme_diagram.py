"""Keep the README state diagram honest.

A diagram that drifts from the code is worse than no diagram: it is confidently
wrong. This test makes the README's state machine a checked artefact, so any
change to ALLOWED_TRANSITIONS that is not reflected in the diagram fails here
rather than quietly misleading a reader.
"""

import pathlib
import re

from app.models import ReferralState
from app.workflow import ALLOWED_TRANSITIONS

README = pathlib.Path(__file__).resolve().parents[2] / "README.md"

# Every non-terminal state can be cancelled. Thirteen identical edges would bury
# the diagram's actual shape, so the README states it once in a note instead.
CANCELLATION_IS_DOCUMENTED_AS_A_NOTE = True


def _diagram_edges() -> set[tuple[str, str]]:
    blocks = re.findall(r"```mermaid\n(.*?)```", README.read_text(), re.DOTALL)
    # The machine is drawn as two diagrams - happy path and recovery - because
    # GitHub renders every mermaid block in a fixed 180px-tall frame and all
    # fourteen states in one picture are illegible there. Verification is over
    # the union, so splitting the drawing cannot hide a transition.
    state_blocks = [b for b in blocks if "stateDiagram" in b]
    assert state_blocks, "expected at least one state diagram in the README"
    edges = set()
    for block in state_blocks:
        for source, target in re.findall(r"^\s*(\w+)\s*-->\s*(\w+)", block, re.MULTILINE):
            if source == "[*]" or target == "[*]":
                continue
            edges.add((source, target))
    return edges


def test_every_edge_in_the_diagram_is_a_legal_transition():
    for source, target in _diagram_edges():
        assert source in ReferralState.__members__, f"{source} is not a real state"
        assert target in ReferralState.__members__, f"{target} is not a real state"
        allowed = ALLOWED_TRANSITIONS[ReferralState[source]]
        assert ReferralState[target] in allowed, f"README draws {source} -> {target}, which the code forbids"


def test_every_legal_transition_appears_in_the_diagram():
    drawn = _diagram_edges()
    missing = []
    for source, targets in ALLOWED_TRANSITIONS.items():
        for target in targets:
            if target is ReferralState.CANCELLED and CANCELLATION_IS_DOCUMENTED_AS_A_NOTE:
                continue
            if (source.value, target.value) not in drawn:
                missing.append(f"{source.value} -> {target.value}")
    assert not missing, "transitions exist in code but not in the README diagram: " + ", ".join(sorted(missing))


def test_cancellation_really_is_available_from_every_non_terminal_state():
    """The note in the README claims this; verify the claim rather than trust it."""
    for state, targets in ALLOWED_TRANSITIONS.items():
        if not targets:
            continue
        assert ReferralState.CANCELLED in targets, f"{state.value} cannot be cancelled, so the README note is wrong"


def test_booking_is_the_only_route_into_confirmed():
    """The diagram's central safety claim."""
    sources = [state for state, targets in ALLOWED_TRANSITIONS.items() if ReferralState.CONFIRMED in targets]
    assert sources == [ReferralState.BOOKING]
