"""The resume checklist (PRD 04): one tool-less Claude turn over a posting and the packet's resume copy."""

from pathlib import Path

from jsa import agent_loop
from jsa.assemble import Slot, app_template, assemble
from jsa.config import pandoc_bin
from jsa.errors import JsaError
from jsa.profile import AgentSettings
from jsa.tools import run_tool


def assemble_checklist_prompt(
    title: str, company: str, job_description: str | None, resume_text: str
) -> str:
    """The posting and the resume are the only inputs: no profile content (PRD 04, XC-13)."""
    slots = {
        "JOB_TITLE": Slot(title, "the posting's title"),
        "COMPANY": Slot(company, "the posting's company"),
        "JOB_DESCRIPTION": Slot(job_description, "the posting's job description"),
        "RESUME": Slot(resume_text, "the packet's resume copy"),
    }
    return assemble(app_template("checklist.md"), slots)


def run_checklist(prompt: str, settings: AgentSettings) -> str:
    result = agent_loop.run_agent(
        prompt,
        settings,
        # No tools: the checklist reads untrusted posting text and has nothing to act with.
        tools=[],
        max_turns=1,
        permission_mode="dontAsk",
    )
    text = result.text.strip()
    if not text:
        raise JsaError("the checklist agent returned no text")
    return text


def render_checklist_pdf(checklist: Path, pdf: Path) -> None:
    """Render the checklist to a PDF with pandoc, which typesets through Typst."""
    result = run_tool(
        [pandoc_bin(), str(checklist), "-o", str(pdf), "--pdf-engine=typst"]
    )
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip()).partition("\n")[0]
        raise JsaError(f"pandoc exited {result.returncode}: {detail}")
