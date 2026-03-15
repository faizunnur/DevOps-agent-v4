"""
Error Agent — pure Python, no AI
Collects logs from ALL pipeline jobs (not just failed ones) and
passes the complete picture to code_agent for AI analysis.
No hardcoded error patterns — the AI reads the raw logs and decides.
"""
import logging

logger = logging.getLogger(__name__)


class ErrorAgent:

    def analyze(self, failed_jobs: list, all_jobs: list = None) -> dict:
        """
        Collect logs from every job — failed AND passed.
        A job that 'passes' but silently does nothing (e.g. ansible skipping all hosts)
        is just as broken as a hard failure. The AI needs to see all of it.
        """
        jobs_to_scan = all_jobs or failed_jobs
        if not jobs_to_scan:
            return {"error": "No jobs", "log_context": "", "full_log": "", "file": None}

        # Build full combined log — label each job clearly
        # Put failed jobs first so they appear at the top of context
        failed_names = {j.get("name", "") for j in failed_jobs}

        sections = []

        # Preserve pipeline execution order — earlier jobs first
        # This is critical: ansible warning appears in an early [passed] job
        # If we put failed jobs first, the warning gets pushed to the end and cut off
        for job in jobs_to_scan:
            log    = job.get("log", "").strip()
            name   = job.get("name", "unknown")
            status = "[FAILED]" if name in failed_names else "[passed]"
            if log:
                sections.append(f"=== JOB: {name} {status} ===\n{log}")

        combined_log = "\n\n".join(sections)

        # job_name = the first failed job (for display)
        job_name = failed_jobs[0].get("name", "unknown") if failed_jobs else "unknown"

        return {
            "job_name":    job_name,
            "log_context": combined_log,       # full log — AI reads everything
            "full_log":    combined_log,
            "file":        None,
            "error":       None,
        }

    def format_for_user(self, analysis: dict) -> str:
        return f"Analysing logs from all jobs..."


error_agent = ErrorAgent()