from __future__ import annotations

from autopilot.fill.adapters.generic import Adapter


class WorkdayAdapter(Adapter):
    """Workday: the job page has an Apply button and the form needs a per-company account.

    You click Apply and sign in (or create the account yourself). Each step of the form is then filled
    as it appears; you click Next and, at the end, Submit.
    """

    name = "workday"
