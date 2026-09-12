from dataclasses import dataclass, field


class InjectedTransientFailure(RuntimeError):
    pass


@dataclass
class FaultInjector:
    failures_remaining: dict[str, int] = field(default_factory=dict)

    def hit(self, point: str) -> None:
        remaining = self.failures_remaining.get(point, 0)
        if remaining <= 0:
            return
        self.failures_remaining[point] = remaining - 1
        raise InjectedTransientFailure(f"Injected transient failure at {point}")

