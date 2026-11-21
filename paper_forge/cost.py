from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config


class CapExceeded(RuntimeError):
    pass


@dataclass
class Call:
    label: str
    model: str
    input_tokens: int
    output_tokens: int
    searches: int
    usd: float

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class CostTracker:
    cfg: Config
    calls: list[Call] = field(default_factory=list)
    base_tokens: int = 0                 # tokens used by other tasks, so the session cap covers all of them
    on_add: object = None                # called after each Claude call (the worker saves the running total)
    gate: object = None                  # called before every Claude call: lets the job queue pause or cancel the running task

    @property
    def total(self) -> float:
        return sum(c.usd for c in self.calls)

    @property
    def tokens(self) -> int:
        return sum(c.tokens for c in self.calls)

    def price(self, model: str, in_tok: int, out_tok: int, searches: int) -> float:
        p = self.cfg.prices.get(model)
        if p is None:
            raise KeyError(f"No price for model {model!r} in forge.toml [prices]")
        return (in_tok * p["input"] + out_tok * p["output"]) / 1e6 + searches * self.cfg.web_search_usd

    def add(self, label: str, model: str, usage) -> Call:
        """usage is the `usage` object of an API response (or any object with the same attributes)."""
        # Cached input is billed differently; count it as normal input here, which slightly overstates.
        in_tok = (
            (getattr(usage, "input_tokens", 0) or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        )
        out_tok = getattr(usage, "output_tokens", 0) or 0
        stu = getattr(usage, "server_tool_use", None)
        searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        call = Call(label, model, in_tok, out_tok, searches, self.price(model, in_tok, out_tok, searches))
        self.calls.append(call)
        if self.on_add:
            self.on_add()
        return call

    def check(self) -> None:
        if self.gate:
            self.gate()
        if self.base_tokens + self.tokens >= self.cfg.token_cap:
            raise CapExceeded(
                f"Token use {self.base_tokens + self.tokens:,} reached the cap {self.cfg.token_cap:,}. "
                "Finished work is saved. Raise limits.token_cap in forge.toml to continue."
            )
        if self.total >= self.cfg.spend_cap_usd:
            raise CapExceeded(
                f"Spend ${self.total:.2f} reached the cap ${self.cfg.spend_cap_usd:.2f}. "
                "Finished work is saved. Raise limits.spend_cap_usd in forge.toml to continue."
            )

    def summary(self) -> str:
        lines = [f"{'step':<22}{'model':<20}{'in':>10}{'out':>9}{'total':>10}{'search':>7}"]
        for c in self.calls:
            lines.append(f"{c.label:<22}{c.model:<20}{c.input_tokens:>10,}{c.output_tokens:>9,}{c.tokens:>10,}{c.searches:>7}")
        lines.append(f"{'TOTAL':<22}{'':<20}{'':>10}{'':>9}{self.tokens:>10,}{'':>7}  (cap {self.cfg.token_cap:,} tokens)")
        return "\n".join(lines)
