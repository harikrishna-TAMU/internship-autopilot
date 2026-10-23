from __future__ import annotations

from autopilot.fill.adapters.generic import Adapter


class AshbyAdapter(Adapter):
    """Ashby: the form is on the posting's /application tab (a React app that renders late)."""

    name = "ashby"

    def form_url(self, job) -> str:
        if job["form_url"]:
            return job["form_url"]
        url = (job["apply_url"] or "").rstrip("/")
        return url if url.endswith("/application") else url + "/application"
