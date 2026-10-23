from __future__ import annotations

from autopilot.fill.adapters.ashby import AshbyAdapter
from autopilot.fill.adapters.generic import Adapter, OpenOnly
from autopilot.fill.adapters.greenhouse import GreenhouseAdapter
from autopilot.fill.adapters.lever import LeverAdapter
from autopilot.fill.adapters.workday import WorkdayAdapter

ADAPTERS: dict[str, Adapter] = {
    "greenhouse": GreenhouseAdapter(),
    "lever": LeverAdapter(),
    "ashby": AshbyAdapter(),
    "workday": WorkdayAdapter(),
    "linkedin": OpenOnly("linkedin", "LinkedIn bans automation, so this tab is only opened. Apply here yourself."),
}


def adapter_for(ats: str | None) -> Adapter:
    return ADAPTERS.get(ats or "", Adapter())
