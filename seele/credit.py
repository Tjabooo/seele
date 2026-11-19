"""API-billed accounts: Claude Code logged in with an API key / Console credit instead of a plan.

They have no 5-hour or weekly limits, so /usage can't read anything for them. Instead SEELE adds up what
each run reports (`total_cost_usd`) and shows it against the account's `budget`. Runs outside SEELE
aren't counted: the Console's billing page is the real balance (`spent` in the config corrects the start).
"""
from __future__ import annotations

import re

from .usage import bar

# "Credit balance is too low to access the Anthropic API", "Your credit balance is too low", ...
OUT_OF_CREDIT_RE = re.compile(r"credit balance is too low|insufficient (?:credit|balance|funds)|billing", re.I)
WARN_AT = (0.5, 0.8, 0.9)  # say so once when the total crosses these shares of the budget


class Meter:
    """Cost of one Claude Code process. A result's `total_cost_usd` covers the whole process so far
    (several turns with live input), so only the growth since the last one is new spend."""

    def __init__(self):
        self.seen = 0.0

    def add(self, total: float) -> float:
        total = max(0.0, float(total or 0))
        new = total - self.seen if total >= self.seen else total  # a lower number = a fresh process count
        self.seen = total
        return new


def money(usd: float) -> str:
    return f"${usd:,.2f}" if usd >= 0.01 or usd == 0 else f"${usd:.4f}".rstrip("0")


def remaining(budget: float, spent: float) -> float:
    return max(0.0, budget - spent)


def summary(budget: float, spent: float) -> str:
    """One line for the status footer: "$12.30 of $200 API credit"."""
    if budget > 0:
        return f"{money(spent)} of {money(budget)} API credit"
    return f"{money(spent)} API spend"


def usage_text(budget: float, spent: float, runs: int = 0) -> str:
    """/usage for an API-billed account."""
    if budget > 0:
        pct = min(100.0, spent / budget * 100)
        left = remaining(budget, spent)
        lines = [f"`{bar(pct)}` **{money(spent)}** of {money(budget)} used · {money(left)} left"]
    else:
        lines = [f"**{money(spent)}** spent (no budget set)"]
    lines.append(f"-# API credit, no plan limits · tracked by SEELE over {runs} run{'s' * (runs != 1)}; "
                 "runs outside SEELE aren't counted (console.anthropic.com has the real balance)")
    return "\n".join(lines)


def crossed(budget: float, before: float, after: float) -> float | None:
    """The highest WARN_AT share this spend just went past, if any."""
    if budget <= 0:
        return None
    hit = [w for w in WARN_AT if before < w * budget <= after]
    return hit[-1] if hit else None


def run_cap(budget: float, spent: float, per_run: float) -> float | None:
    """--max-budget-usd for the next run: what's left of the budget, at most `per_run` (0 = no cap)."""
    caps = [c for c in ((remaining(budget, spent) if budget > 0 else 0), per_run) if c > 0]
    return round(min(caps), 2) if caps else None
