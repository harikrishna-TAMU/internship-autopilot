from __future__ import annotations

from autopilot.fill.adapters.generic import Adapter


class LeverAdapter(Adapter):
    """Lever: the form lives at <posting>/apply. Lever parses an uploaded resume into the fields,
    so upload first and fill over its guesses."""

    name = "lever"
    upload_first = True

    def form_url(self, job) -> str:
        if job["form_url"]:
            return job["form_url"]
        url = (job["apply_url"] or "").rstrip("/")
        return url if url.endswith("/apply") else url + "/apply"
