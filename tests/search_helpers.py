"""A fake Greenhouse board and a database peek, shared by the tests that run `jsa search` end to end."""

import itertools
from datetime import UTC, datetime, timedelta

import httpx
from conftest import raw_connect

BOARD_TOKEN = "acme"
_ids = itertools.count(8_000_000)


class Boards:
    """The outside world: Greenhouse boards, over the injected HTTP transport."""

    def __init__(self):
        self.jobs = {}

    def job(self):
        job_id = next(_ids)
        updated = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        self.jobs[str(job_id)] = updated.replace("+00:00", "Z")
        return f"https://job-boards.greenhouse.io/{BOARD_TOKEN}/jobs/{job_id}"

    def handle(self, request):
        parts = request.url.path.strip("/").split("/")
        if request.url.host != "boards-api.greenhouse.io" or parts[2] != BOARD_TOKEN:
            return httpx.Response(404, json={"error": "not found"})
        if len(parts) == 4:
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {"id": int(job_id), "updated_at": at}
                        for job_id, at in self.jobs.items()
                    ]
                },
            )
        if parts[4] not in self.jobs:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(
            200,
            json={
                "title": "Staff Engineer",
                "content": "&lt;p&gt;Build the platform.&lt;/p&gt;",
                "location": {"name": "Remote, US"},
            },
        )


def rows(url, sql):
    conn = raw_connect(url)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()
