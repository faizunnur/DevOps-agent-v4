"""
Orchestrator — coordinates all agents
No AI here. Pure coordination logic.
"""
import os
import asyncio
import logging
from typing import Callable, Optional

import state
from agents.aws_agent    import aws_agent,    AWSAgent
from agents.github_agent import github_agent, GitHubAgent
from agents.code_agent   import code_agent
from agents.error_agent  import error_agent

def _get_deploy_branch(project: str, repo_name: str) -> str:
    """
    Find which branch has the deployed terraform files for this project.
    Checks state DB first, then scans repo branches for terraform files.
    Always returns a branch — defaults to main if nothing found.
    """
    # Check state DB for saved branch
    try:
        dep    = state.get_deployment(project) or {}
        branch = dep.get("branch")
        if branch and branch != "main":
            return branch
    except Exception:
        pass

    # Scan repo branches — find one with terraform files (not main)
    try:
        branches_result = github_agent.list_branches(repo_name)
        # list_branches returns dict or list depending on version
        if isinstance(branches_result, dict):
            branch_names = branches_result.get("branches", [])
        else:
            branch_names = branches_result or []

        for b in branch_names:
            b_name = b if isinstance(b, str) else b.get("name", "")
            if not b_name or b_name == "main":
                continue
            files = github_agent.get_existing_files(repo_name, branch=b_name)
            if any(f.endswith(".tf") for f in files):
                logger.info(f"_get_deploy_branch: found terraform on branch '{b_name}'")
                return b_name
    except Exception as e:
        logger.warning(f"_get_deploy_branch: branch scan failed: {e}")

    logger.warning(f"_get_deploy_branch: no non-main branch found for {project}, using main")
    return "main"


def _patch_terraform_bucket(files: dict, correct_bucket: str, correct_region: str) -> None:
    """
    bucket, region, AND key are passed via -backend-config flags at terraform init time.
    Removes ALL hardcoded values from backend "s3" blocks so they don't
    conflict with the -backend-config flags the pipeline passes.

    CRITICAL: If the `key` is left hardcoded (e.g. key = "myapp/terraform.tfstate"),
    every branch deployment shares the SAME tfstate, causing one branch's terraform
    to see and destroy the other branch's EC2 instance.
    """
    import re as _re

    def clean_backend(m):
        block = m.group(0)
        # Strip bucket, region, AND key — all must come from -backend-config flags
        block = _re.sub(r'[ \t]*bucket[ \t]*=[ \t]*"[^"]*"\n', '', block)
        block = _re.sub(r'[ \t]*region[ \t]*=[ \t]*"[^"]*"\n', '', block)
        block = _re.sub(r'[ \t]*key[ \t]*=[ \t]*"[^"]*"\n',    '', block)
        return block

    for path, file_content in list(files.items()):
        if path.endswith(".tf"):
            patched = _re.sub(
                r'backend\s+"s3"\s*\{[^}]+\}',
                clean_backend,
                file_content,
                flags=_re.DOTALL,
            )
            if patched != file_content:
                files[path] = patched
                logger.info(f"_patch_terraform_bucket: cleaned backend block in {path}")
        elif path.endswith(".yml") or path.endswith(".yaml"):
            # Inject -reconfigure into terraform init lines in workflow files
            patched = _re.sub(
                r'(\bterraform init)(\s+(?!-reconfigure))',
                lambda m: m.group(1) + " -reconfigure" + m.group(2),
                file_content,
            )
            if patched != file_content:
                files[path] = patched
                logger.info(f"_patch_terraform_bucket: injected -reconfigure in {path}")



def _inject_node24_env(wf_content: str) -> str:
    """
    Ensures every GitHub Actions workflow has `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true`
    at the top-level `env:` block. Idempotent — skips if already present.
    """
    if "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24" in wf_content:
        return wf_content  # already present

    import re
    # Insert after the `on:` block (before `jobs:`)
    patched = re.sub(
        r'(^jobs:\s*$)',
        'env:\n  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true\n\n\\1',
        wf_content,
        count=1,
        flags=re.MULTILINE,
    )
    if patched != wf_content:
        return patched

    # Fallback: prepend before the first line that starts with `jobs:`
    lines = wf_content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("jobs:"):
            lines.insert(i, "env:\n  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true\n\n")
            return "".join(lines)

    return wf_content  # couldn't find insertion point — return unchanged


def _extract_display_error(raw_log: str) -> str:
    """
    Extract the actual error lines from a pipeline log for display in Telegram.
    Strips timestamps and GitHub Actions noise. Returns up to 3 meaningful error lines.
    """
    import re
    if not raw_log:
        return "Unknown error"

    found = []
    for line in raw_log.splitlines():
        # Strip timestamp prefix: 2024-01-01T00:00:00.000Z
        s = re.sub(r"^\d{4}-\d{2}-\d{2}T[\d:.Z]+ *", "", line.strip())
        # Strip GitHub Actions group markers: ##[error], ##[warning] etc
        s = re.sub(r"^##\[.*?\] *", "", s).strip()
        # Strip ANSI escape codes
        s = re.sub(r"\x1b\[[0-9;]*m", "", s)

        if not s or s.startswith("===") or len(s) < 8:
            continue
        # Match any meaningful error/failure line
        if re.search(r"(?i)(error:|fatal:|failed|access denied|permission denied|"
                     r"no such file|could not|unable to|exit code [^0]|"
                     r"statuscode: [45]\d\d|forbidden|unauthorized)", s):
            clean = s[:300]
            if clean not in found:
                found.append(clean)
        if len(found) >= 3:
            break

    if found:
        return "\n".join(found)

    # Fallback: last non-empty meaningful line
    for line in reversed(raw_log.splitlines()):
        s = re.sub(r"^\d{4}-\d{2}-\d{2}T[\d:.Z]+ *", "", line.strip())
        s = re.sub(r"^##\[.*?\] *", "", s).strip()
        if s and len(s) > 10 and "===" not in s:
            return s[:300]
    return "Unknown error"


def _extract_job_errors(all_jobs: list) -> list:
    """
    Return list of {job, step, error} for each failed job — for Telegram display.
    """
    import re
    results = []
    for job in all_jobs:
        if job.get("conclusion") != "failure" and not job.get("failed_steps"):
            continue
        job_name    = job.get("name", "unknown")
        failed_step = job.get("failed_steps", ["unknown step"])[0] if job.get("failed_steps") else "unknown step"
        log         = job.get("log", "")
        error_lines = []
        for line in log.splitlines():
            s = re.sub(r"^\d{4}-\d{2}-\d{2}T[\d:.Z]+ *", "", line.strip())
            s = re.sub(r"^##\[.*?\] *", "", s).strip()
            s = re.sub(r"\x1b\[[0-9;]*m", "", s)
            if not s or len(s) < 8:
                continue
            if re.search(r"(?i)(error:|fatal:|failed|access denied|permission denied|"
                         r"no such file|could not|unable to|exit code [^0]|"
                         r"statuscode: [45]\d\d|forbidden|unauthorized)", s):
                error_lines.append(s[:300])
            if len(error_lines) >= 3:
                break
        results.append({
            "job":   job_name,
            "step":  failed_step,
            "error": "\n".join(error_lines) or "No error text found",
        })
    return results
    return raw_log[:300]


logger = logging.getLogger(__name__)

MAX_RETRIES         = 6
MAX_DESTROY_RETRIES = 3


class Orchestrator:

    def __init__(self):
        self._stop_flags: dict[int, bool] = {}

    # ── Stop control ──────────────────────────────────────────────────────────

    def stop(self, user_id: int):
        self._stop_flags[user_id] = True

    def resume(self, user_id: int):
        self._stop_flags[user_id] = False

    def is_stopped(self, user_id: int) -> bool:
        return self._stop_flags.get(user_id, False)

    def _check_stop(self, user_id: int):
        if self.is_stopped(user_id):
            raise StopIteration("Stopped by user")

    # ── Deploy ────────────────────────────────────────────────────────────────

    async def deploy(
        self,
        user_id:     int,
        project:     str,
        app:         str,
        repo_name:   str,
        region:      str = "us-east-1",
        branch:      str = "main",
        target:      str = "ec2",
        creds:       dict | None = None,
        progress_cb: Optional[Callable] = None,
    ) -> dict:
        # Scope agents to per-user credentials if provided
        _aws    = AWSAgent.with_creds(creds)    if creds else aws_agent
        _github = GitHubAgent.with_creds(creds) if creds else github_agent
        self.resume(user_id)
        cb = progress_cb or (lambda m: None)
        # Use a holder so step() closure always uses the branch-scoped project name
        _proj = [project]  # _proj[0] will be updated to branch_project inside try

        async def step(name, msg):
            self._check_stop(user_id)
            await cb(msg)
            state.log_step(_proj[0], name, "running")

        try:
            # ── Branch isolation: each branch gets its own scoped project name ──
            # e.g. project="myapp", branch="staging" → branch_project="myapp-staging"
            # This ensures separate EC2, key pair, security group, and S3 state key per branch.
            safe_branch = branch.replace("/", "-").replace("_", "-")[:20]
            branch_project = project if branch == "main" else f"{project}-{safe_branch}"
            _proj[0] = branch_project  # update closure so step() uses correct name
            if branch_project != project:
                await cb(f"🔀 Branch isolation: using project key '{branch_project}' for branch '{branch}'")

            state.save_deployment(branch_project, app, repo_name, region=region, branch=branch)

            # ── Check if previously deployed successfully ──────────────────────
            dep = state.get_deployment(branch_project)
            if dep and dep.get("status") == "deployed" and dep.get("ec2_ip"):
                await cb(f"Project '{branch_project}' was previously deployed successfully.")
                await cb(f"Updating secrets and workflow backend before re-triggering...")

                # Always refresh secrets so the pipeline uses THIS user's credentials/bucket
                bucket_name  = _aws.get_state_bucket_name()
                _aws.ensure_s3_bucket(bucket_name)
                aws_creds_rerun = _aws.get_credentials()
                ssh_keys_rerun  = _aws.get_ssh_keys(branch_project)
                if not ssh_keys_rerun.get("private_key"):
                    ssh_keys_rerun = _aws.generate_ssh_key(branch_project)

                rerun_secrets = {
                    "AWS_ACCESS_KEY_ID":     aws_creds_rerun["AWS_ACCESS_KEY_ID"],
                    "AWS_SECRET_ACCESS_KEY": aws_creds_rerun["AWS_SECRET_ACCESS_KEY"],
                    "AWS_REGION":            region,
                    "SSH_PRIVATE_KEY":       ssh_keys_rerun.get("private_key", ""),
                    "SSH_PUBLIC_KEY":        ssh_keys_rerun.get("public_key", ""),
                    "PROJECT_NAME":          branch_project,
                    "TF_STATE_BUCKET":       bucket_name,
                    "SSH_USER":              os.getenv("SSH_USER", "ubuntu"),
                }
                _github.set_secrets(repo_name, rerun_secrets)
                await cb(f"✓ Secrets updated — bucket: {bucket_name}")

                # Patch workflow files to ensure -reconfigure is present
                branch_files_rerun = _github.get_existing_files(repo_name, branch=branch)
                branch_files_before_rerun = dict(branch_files_rerun)
                _patch_terraform_bucket(branch_files_rerun, bucket_name, region)
                rerun_patches = {
                    p: c for p, c in branch_files_rerun.items()
                    if c != branch_files_before_rerun.get(p)
                }
                if rerun_patches:
                    pr = _github.push_files(repo_name, rerun_patches,
                                            message="fix: update terraform backend for rerun",
                                            branch=branch)
                    await cb(f"✓ Patched workflow files: {pr.get('pushed', [])}")

                cancelled = _github.cancel_running_pipelines(repo_name)
                if cancelled.get("cancelled"):
                    await cb(f"Cancelled {len(cancelled['cancelled'])} running pipeline(s)")
                    await asyncio.sleep(5)

                trigger_result = _github.trigger_pipeline(repo_name, "deploy.yml", branch)
                if trigger_result.get("status") == "error":
                    raise Exception(f"Trigger failed: {trigger_result['error']}")
                await cb(f"Pipeline triggered: {trigger_result.get('url')}")

                pipeline = await _github.poll_pipeline(
                    repo_name, interval=30,
                    stop_flag=lambda: self.is_stopped(user_id),
                    progress_cb=cb,
                )
                if pipeline.get("conclusion") == "success":
                    ip = dep["ec2_ip"]
                    return {"status": "success", "ip": ip, "url": f"http://{ip}", "project": branch_project}
                else:
                    await cb(f"Pipeline failed: {pipeline.get('run_url','')}")
                    return {"status": "failed", "message": "Pipeline failed on rerun"}

            # ── Step 1: AWS Prepare ───────────────────────────────────────────
            await step("aws_prepare", "Preparing AWS resources...")
            aws_result = _aws.prepare(branch_project)

            if "error" in aws_result.get("ssh", {}):
                raise Exception(f"SSH key generation failed: {aws_result['ssh']['error']}")

            ssh_keys  = aws_result["ssh"]
            aws_creds = aws_result["credentials"]
            ec2       = aws_result["ec2"]
            existing = aws_result.get("existing", {})

            # Report all existing resources
            ec2_status = "exists at " + ec2.get("ip", "") if ec2.get("exists") else "none"
            kp_status  = "exists" if existing.get("key_pair",        {}).get("exists") else "none"
            sg_status  = "exists" if existing.get("security_group",  {}).get("exists") else "none"
            s3_status  = "exists" if existing.get("s3_state",        {}).get("exists") else "none"
            sk_status  = "exists" if existing.get("ssh_keys",        {}).get("exists") else "none"

            state.log_step(branch_project, "aws_prepare", "done", result=str(existing))
            await cb(
                f"AWS resources for '{branch_project}':\n"
                f"  EC2:            {ec2_status}\n"
                f"  Key pair:       {kp_status}\n"
                f"  Security group: {sg_status}\n"
                f"  S3 state:       {s3_status}\n"
                f"  SSH keys:       {sk_status}"
            )

            # ── Step 2: GitHub Setup ──────────────────────────────────────────
            self._check_stop(user_id)
            await step("github_setup", f"Setting up GitHub repo {repo_name}...")

            repo_result = _github.create_repo(repo_name, f"DevOps Agent — {project}")
            await cb(f"Repo: {repo_result.get('url', repo_name)}")

            # Create branch from main FIRST — so branch inherits all existing files
            if branch != "main":
                br = _github.create_branch(repo_name, branch, "main")
                await cb(f"Branch '{branch}' {br.get('status', 'ready')} (from main)")

            # ── Step 3: Files ─────────────────────────────────────────────────
            await step("generate_files", f"Reading files from branch '{branch}'...")

            # Read ALL existing files from the branch
            # (if branch was just created from main, it already has all main files)
            repo_files = _github.get_existing_files(repo_name, branch=branch)
            await cb(f"Found {len(repo_files)} files in branch '{branch}'")

            # Context-aware generation:
            # Pass existing files so code_agent can see what's there
            # and only generate what actually needs to change for this app
            await cb(f"Analysing what needs to change for '{app}' on {target}...")

            # plan_deployment reads FULL file contents so Claude decides intelligently
            plan = code_agent.plan_deployment(branch_project, app, region, target, repo_files)

            # Show user exactly what will change before touching anything
            if plan["keep"]:
                await cb(f"✔ Keeping unchanged: {plan['keep']}")
            if plan["update"]:
                await cb(f"✏ Updating: {plan['update']}")
            if plan["create"]:
                await cb(f"➕ Creating new: {plan['create']}")
            if plan["delete"]:
                await cb(f"🗑 Removing: {plan['delete']}")
            await cb(f"Reason: {plan['reasoning']}")

            to_generate = plan["update"] + plan["create"]

            if not to_generate and not plan["delete"]:
                await cb(f"✅ No changes needed — branch '{branch}' is already up to date")
                files_to_push = {}
            else:
                # Generate only what changed
                files_to_push = code_agent.generate_files(
                    branch_project, app, region,
                    existing_files=repo_files,
                    target=target,
                )

            state.log_step(project, "generate_files", "done",
                           result=f"{len(files_to_push)} files to push")

            # ── Auto-generate branded homepage on first deployment only ────────
            # Check if ANY .html file exists anywhere in the repo or generated files
            all_paths = list(repo_files.keys()) + list(files_to_push.keys())
            html_exists = any(p.endswith(".html") for p in all_paths)
            if not html_exists:
                await cb("Generating branded homepage (index.html)...")
                html_content = code_agent.gen_html(branch_project, app, repo_name)
                files_to_push["index.html"] = html_content
                await cb("✓ index.html created")

            # Determine state bucket early so we can clean backend blocks in generated terraform files
            bucket_name = _aws.get_state_bucket_name()

            if files_to_push:
                # Ensure generated terraform files have no hardcoded backend bucket/region
                _patch_terraform_bucket(files_to_push, bucket_name, region)
                # Ensure all workflow files opt into Node.js 24 runtime
                for wf_path in list(files_to_push.keys()):
                    if wf_path.endswith(".yml") and ".github" in wf_path:
                        files_to_push[wf_path] = _inject_node24_env(files_to_push[wf_path])
                await cb(f"Pushing {len(files_to_push)} file(s) to '{branch}'...")
                push_result = _github.push_files(repo_name, files_to_push, branch=branch)
                if push_result.get("failed"):
                    await cb(f"Warning: failed to push: {push_result['failed']}")
                await cb(f"Pushed: {push_result.get('pushed', [])}")

            # Delete files no longer needed
            for path in plan.get("delete", []):
                try:
                    if hasattr(github_agent, "delete_file"):
                        del_result = _github.delete_file(repo_name, path, branch=branch)
                        await cb(f"Deleted {path}: {del_result.get('status', 'done')}")
                    else:
                        await cb(f"Skipping delete {path} (update github_agent to enable)")
                except Exception as e:
                    await cb(f"Could not delete {path}: {e}")

            # Ensure S3 terraform state bucket exists in THIS AWS account
            # Bucket name is auto-derived from account ID — different per account
            bucket_name = _aws.get_state_bucket_name()
            bucket_result = _aws.ensure_s3_bucket(bucket_name)
            if bucket_result.get("created"):
                await cb(f"✓ Created S3 bucket: {bucket_name}")
            elif bucket_result.get("error"):
                await cb(f"⚠️ S3 bucket error: {bucket_result['error']}")

            # Patch any terraform/workflow files in the branch that have a wrong bucket name
            # or are missing -reconfigure. Collect diffs and push them back.
            repo_files_before = dict(repo_files)  # snapshot before patching
            _patch_terraform_bucket(repo_files, bucket_name, region)
            backend_patches = {
                p: c for p, c in repo_files.items()
                if c != repo_files_before.get(p)
            }
            if backend_patches:
                await cb(f"Patching backend config in {list(backend_patches.keys())}...")
                patch_result = _github.push_files(repo_name, backend_patches,
                                                  message="fix: update terraform backend bucket and reconfigure flag",
                                                  branch=branch)
                if patch_result.get("failed"):
                    await cb(f"⚠️ Backend patch push failed: {patch_result['failed']}")
                else:
                    await cb(f"✓ Backend patched: {patch_result.get('pushed', [])}")

            # Set secrets — PROJECT_NAME uses branch_project so each branch has isolated AWS resources
            secrets = {
                "AWS_ACCESS_KEY_ID":     aws_creds["AWS_ACCESS_KEY_ID"],
                "AWS_SECRET_ACCESS_KEY": aws_creds["AWS_SECRET_ACCESS_KEY"],
                "AWS_REGION":            region,
                "SSH_PRIVATE_KEY":       ssh_keys["private_key"],
                "SSH_PUBLIC_KEY":        ssh_keys["public_key"],
                "PROJECT_NAME":          branch_project,
                "TF_STATE_BUCKET":       bucket_name,
                "SSH_USER":              os.getenv("SSH_USER", "ubuntu"),
            }
            secret_result = _github.set_secrets(repo_name, secrets)
            await cb(f"Set {len(secret_result.get('set', []))} secrets")
            state.log_step(branch_project, "github_setup", "done")

            # ── Step 4: Trigger Pipeline ──────────────────────────────────────
            self._check_stop(user_id)
            await step("trigger", "Checking for running pipelines...")

            cancelled = _github.cancel_running_pipelines(repo_name)
            if cancelled.get("cancelled"):
                await cb(f"Cancelled {len(cancelled['cancelled'])} running pipeline(s)")
                await asyncio.sleep(5)

            await cb(f"Triggering pipeline on branch '{branch}'...")
            trigger_result = _github.trigger_pipeline(repo_name, "deploy.yml", branch)
            if trigger_result.get("status") == "error":
                raise Exception(f"Trigger failed: {trigger_result['error']}")
            await cb(f"Pipeline triggered: {trigger_result.get('url')}")
            state.log_step(branch_project, "trigger", "done")

            # ── Step 5: Poll + Auto-fix loop (no approval needed) ─────────────
            retry      = 0
            last_error = "Unknown error"
            did_push_fix = True
            pipeline = None

            while retry <= MAX_RETRIES:
                self._check_stop(user_id)

                # Only poll on first iteration — retrigger handles subsequent ones
                if retry > 0 and did_push_fix:
                    await cb(f"Retriggering pipeline (attempt {retry}/{MAX_RETRIES})...")
                    trigger2 = _github.trigger_pipeline(repo_name, "deploy.yml", branch)
                    if trigger2.get("status") == "error":
                        last_error = trigger2["error"]
                        await cb(f"Retrigger failed: {last_error}")
                        break
                    await cb(f"Pipeline: {trigger2.get('url', '')}")
                elif retry > 0:
                    await cb(f"Retrying AI auto-fix (pipeline not retriggered)...")

                if retry == 0 or did_push_fix:
                    await cb(f"Polling... (attempt {retry + 1}/{MAX_RETRIES + 1})")
                    pipeline = await _github.poll_pipeline(
                        repo_name,
                        interval=30,
                        branch=branch,
                        stop_flag=lambda: self.is_stopped(user_id),
                        progress_cb=cb,
                    )

                did_push_fix = False

                if pipeline.get("status") == "stopped":
                    state.log_step(project, "pipeline", "stopped")
                    return {"status": "stopped"}

                if pipeline.get("status") == "timeout":
                    state.log_step(project, "pipeline", "timeout")
                    return {"status": "timeout", "message": "Pipeline timed out"}

                if pipeline.get("conclusion") == "success":
                    if target == "ecs":
                        url = self._extract_url(pipeline, region=region) or ""
                        ip  = url.replace("http://", "")
                    else:
                        ip  = self._extract_ip(pipeline)
                        if not ip and ec2.get("exists"):
                            ip = ec2.get("ip", "")
                        if not ip:
                            fresh_ec2 = _aws.check_ec2(branch_project)
                            ip = fresh_ec2.get("ip", "")
                        url = f"http://{ip}" if ip else ""
                    state.update_deployment(branch_project, status="deployed", ec2_ip=ip)
                    state.log_step(branch_project, "pipeline", "done", result=ip)
                    return {"status": "success", "ip": ip, "url": url, "project": branch_project}

                # Pipeline failed — capture error before checking retry limit
                analysis   = error_agent.analyze(
                    pipeline.get("failed_jobs", []),
                    all_jobs=pipeline.get("all_jobs", []),
                )
                # last_error for display — extract clean summary, not raw log
                raw_log    = analysis.get("log_context", "") or analysis.get("full_log", "") or ""
                last_error = _extract_display_error(raw_log)
                last_error_short = last_error[:300]

                if retry >= MAX_RETRIES:
                    await cb(f"Failed after {MAX_RETRIES} attempts.")
                    await cb(f"Last error: {last_error_short}")
                    await cb(f"Pipeline logs: {pipeline.get('run_url', '')}")
                    break

                retry += 1

                # Report exactly what failed and where — stage + error lines
                job_errors = _extract_job_errors(pipeline.get("all_jobs", []))
                for je in job_errors:
                    await cb(
                        f"❌ Stage: {je['job']}\n"
                        f"   Step:  {je['step']}\n"
                        f"   Error: {je['error']}"
                    )

                await cb(f"🔧 Auto-fixing (attempt {retry}/{MAX_RETRIES})...")

                # Always read LIVE files from the actual branch — never trust local state
                await cb(f"Reading current files from branch '{branch}'...")
                repo_files_now = _github.get_existing_files(repo_name, branch=branch)

                # Sync to local state so fix_file reads the real current content
                for path, fcontent in repo_files_now.items():
                    if path and fcontent:
                        state.save_file(branch_project, path, fcontent)

                await cb(f"Analysing error against {len(repo_files_now)} live files...")

                # ── S3 403: wrong bucket — update secret + patch .tf, skip AI ──
                combined_log = analysis.get("log_context", "") + analysis.get("full_log", "")
                if ("403" in combined_log or "AccessDenied" in combined_log or "Access Denied" in combined_log) and ("tfstate" in combined_log.lower() or "ListObjects" in combined_log or "HeadObject" in combined_log):
                    correct_bucket = _aws.get_state_bucket_name()
                    await cb(f"S3 403 detected — wrong bucket in secret. Updating TF_STATE_BUCKET → {correct_bucket}")
                    _github.set_secrets(repo_name, {"TF_STATE_BUCKET": correct_bucket})
                    # Also clean any hardcoded bucket from .tf files
                    _patch_terraform_bucket(repo_files_now, correct_bucket, region)
                    for path, fc in repo_files_now.items():
                        if path.endswith(".tf"):
                            _github.push_single_file(
                                repo_name, path, fc,
                                f"fix: clean hardcoded backend bucket (attempt {retry})",
                                branch=branch,
                            )
                    await asyncio.sleep(3)
                    trigger2 = _github.trigger_pipeline(repo_name, "deploy.yml", branch)
                    if trigger2.get("error"):
                        last_error = trigger2["error"]
                        break
                    await cb(f"Retriggering pipeline (attempt {retry}/{MAX_RETRIES})...")
                    continue

                # ── Node.js 20 deprecation: inject FORCE_JAVASCRIPT_ACTIONS_TO_NODE24 ──
                if "Node.js 20" in combined_log or "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24" not in combined_log and "node_modules" not in combined_log and "actions are running on Node.js 20" in combined_log:
                    patched_any_wf = False
                    for wf_path, wf_content in list(repo_files_now.items()):
                        if wf_path.endswith(".yml") and ".github" in wf_path:
                            new_content = _inject_node24_env(wf_content)
                            if new_content != wf_content:
                                _github.push_single_file(
                                    repo_name, wf_path, new_content,
                                    f"fix: add FORCE_JAVASCRIPT_ACTIONS_TO_NODE24 (attempt {retry})",
                                    branch=branch,
                                )
                                await cb(f"Patched {wf_path} → Node.js 24")
                                patched_any_wf = True
                    if patched_any_wf:
                        did_push_fix = True
                        continue

                # Pass live files directly so Claude sees exactly what's in the repo
                fix_result = code_agent.analyze_and_fix(
                    branch_project,
                    analysis.get("log_context", "") or analysis.get("full_log", ""),
                    all_files=repo_files_now,
                )

                if "error" in fix_result:
                    last_error = fix_result["error"]
                    await cb(
                        f"Cannot auto-fix: {last_error}\n"
                        f"Check logs — validator may have rejected AI output.\n"
                        f"Pipeline: {pipeline.get('run_url', '')}"
                    )
                    continue

                # Push ALL fixed files (may be multiple)
                all_fixes = fix_result.get("all_fixes", [fix_result])
                for fx in all_fixes:
                    fx_path    = fx.get("file") or fix_result.get("file")
                    fx_content = fx.get("content") or fx.get("fixed_content")
                    if fx_path and fx_content:
                        _github.push_single_file(
                            repo_name, fx_path, fx_content,
                            f"fix: {fx.get('error','')[:60]} (attempt {retry})",
                            branch=branch,
                        )
                        await cb(f"Pushed fix: {fx_path}")

                if not fix_result.get("file") or not fix_result.get("fixed_content"):
                    await cb(f"Auto-fix produced no usable output — skipping push.")
                    last_error = fix_result.get("error_summary", "no fix generated")
                    await cb(f"Pipeline: {pipeline.get('run_url', '')}")
                    continue

                last_error = fix_result.get("error_summary", "unknown")
                await cb(
                    f"Fixed {fix_result['file']}\n"
                    f"Error was: {last_error}\n"
                    f"Change: {fix_result.get('diff_summary', '')}"
                )

                push = _github.push_single_file(
                    repo_name,
                    fix_result["file"],
                    fix_result["fixed_content"],
                    f"Auto-fix attempt {retry}: {fix_result['file']}",
                    branch=branch,
                )
                if push.get("failed"):
                    last_error = str(push["failed"])
                    await cb(f"Push failed: {last_error}")
                    continue

                did_push_fix = True
                state.log_step(branch_project, f"fix_{retry}", "done", result=fix_result["file"])
                await asyncio.sleep(3)

            state.log_step(branch_project, "pipeline", "failed")
            state.update_deployment(branch_project, status="failed")
            return {
                "status":  "failed",
                "message": f"Failed after {MAX_RETRIES} attempts. Last error: {last_error}",
            }

        except StopIteration:
            state.log_step(branch_project, "stopped", "stopped")
            return {"status": "stopped"}
        except Exception as e:
            logger.error(f"Deploy error: {e}", exc_info=True)
            state.log_step(branch_project, "error", "error", error=str(e))
            return {"status": "error", "message": str(e)}

    # ── Apply fix and retry (kept for manual use from bot) ───────────────────

    async def apply_fix_and_retry(
        self,
        user_id:       int,
        project:       str,
        repo_name:     str,
        file_path:     str,
        fixed_content: str,
        retry:         int,
        creds:         dict | None = None,
        progress_cb:   Optional[Callable] = None,
    ) -> dict:
        _github = GitHubAgent.with_creds(creds) if creds else github_agent
        cb = progress_cb or (lambda m: None)
        self._check_stop(user_id)

        await cb(f"Pushing fix for {file_path}...")
        push = _github.push_single_file(
            repo_name, file_path, fixed_content,
            f"Fix: {file_path} (attempt {retry})"
        )
        if push.get("failed"):
            return {"status": "error", "message": f"Push failed: {push['failed']}"}

        await cb("Waiting for any running pipelines...")
        _github.wait_for_idle(repo_name, timeout=120)

        await cb("Retriggering pipeline...")
        trigger = _github.trigger_pipeline(repo_name, "deploy.yml")
        if trigger.get("status") == "error":
            return {"status": "error", "message": trigger["error"]}

        await cb(f"Pipeline retriggered: {trigger.get('url')}")
        state.log_step(project, f"fix_{retry}", "done")

        pipeline = await _github.poll_pipeline(
            repo_name,
            interval=30,
            stop_flag=lambda: self.is_stopped(user_id),
            progress_cb=cb,
        )

        if pipeline.get("conclusion") == "success":
            ip = self._extract_ip(pipeline)
            state.update_deployment(project, status="deployed", ec2_ip=ip)
            return {"status": "success", "ip": ip, "url": f"http://{ip}"}

        return {"status": "failed", "pipeline": pipeline}

    # ── Update file ───────────────────────────────────────────────────────────

    async def update_file(
        self,
        user_id:     int,
        project:     str,
        repo_name:   str,
        file_path:   str,
        content:     str,
        creds:       dict | None = None,
        progress_cb: Optional[Callable] = None,
    ) -> dict:
        _github = GitHubAgent.with_creds(creds) if creds else github_agent
        self.resume(user_id)
        cb = progress_cb or (lambda m: None)

        try:
            self._check_stop(user_id)
            await cb(f"Pushing {file_path} to {repo_name}...")
            state.save_file(project, file_path, content)

            push = _github.push_single_file(repo_name, file_path, content)
            if push.get("failed"):
                return {"status": "error", "message": str(push["failed"])}

            await cb("File pushed. Triggering pipeline...")

            cancelled = _github.cancel_running_pipelines(repo_name)
            if cancelled.get("cancelled"):
                await cb(f"Cancelled {len(cancelled['cancelled'])} running pipeline(s)")
                await asyncio.sleep(5)

            trigger = _github.trigger_pipeline(repo_name, "deploy.yml")
            if trigger.get("status") == "error":
                return {"status": "error", "message": trigger["error"]}

            await cb(f"Pipeline triggered: {trigger.get('url')}")

            pipeline = await _github.poll_pipeline(
                repo_name,
                interval=30,
                stop_flag=lambda: self.is_stopped(user_id),
                progress_cb=cb,
            )

            if pipeline.get("conclusion") == "success":
                dep = state.get_deployment(project)
                ip  = (dep.get("ec2_ip") if dep else None) or ""
                return {"status": "success", "ip": ip, "url": f"http://{ip}" if ip else "done"}

            return {"status": "failed", "pipeline": pipeline}

        except StopIteration:
            return {"status": "stopped"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── Destroy ───────────────────────────────────────────────────────────────

    async def destroy(
        self,
        user_id:     int,
        project:     str,
        repo_name:   str,
        branch:      str = "main",
        delete_repo: bool = False,
        delete_branch: bool = False,
        creds:       dict | None = None,
        progress_cb: Optional[Callable] = None,
    ) -> dict:
        # Scope agents to per-user credentials if provided
        _aws    = AWSAgent.with_creds(creds)    if creds else aws_agent
        _github = GitHubAgent.with_creds(creds) if creds else github_agent
        self.resume(user_id)
        cb = progress_cb or (lambda m: None)

        try:
            self._check_stop(user_id)

            deploy_branch = branch
            await cb(f"Destroying project '{project}' from branch '{deploy_branch}'...")

            # Patch terraform files on that branch with correct bucket before destroying
            bucket_name = _aws.get_state_bucket_name()
            dep         = state.get_deployment(project) or {}
            region      = dep.get("region", "us-east-1")

            # Ensure S3 bucket exists in this account
            _aws.ensure_s3_bucket(bucket_name)

            # Patch terraform backend + workflow files on the deploy branch before triggering destroy
            branch_files = _github.get_existing_files(repo_name, branch=deploy_branch)
            branch_files_before = dict(branch_files)
            _patch_terraform_bucket(branch_files, bucket_name, region)
            destroy_patches = {
                p: c for p, c in branch_files.items()
                if c != branch_files_before.get(p)
            }
            if destroy_patches:
                push_r = _github.push_files(
                    repo_name, destroy_patches,
                    message="fix: update terraform backend for destroy",
                    branch=deploy_branch,
                )
                for path in push_r.get("pushed", []):
                    await cb(f"Patched {path} → bucket: {bucket_name}")

            # Cancel any running pipelines first
            cancelled = _github.cancel_running_pipelines(repo_name)
            if cancelled.get("cancelled"):
                await cb(f"Cancelled {len(cancelled['cancelled'])} running pipeline(s)")
                await asyncio.sleep(5)

            retry = 0
            did_push_fix = True
            pipeline = None

            while retry <= MAX_DESTROY_RETRIES:
                self._check_stop(user_id)
                
                if did_push_fix:
                    await cb(f"Triggering destroy pipeline on branch '{deploy_branch}'... (attempt {retry + 1})")
                    trigger = _github.trigger_pipeline(repo_name, "destroy.yml",
                                                            branch=deploy_branch)
                    if trigger.get("status") == "error":
                        await cb(f"Trigger failed: {trigger['error']}")
                        break
                    await cb(f"Pipeline: {trigger.get('url', '')}")
                else:
                    await cb(f"Retrying AI auto-fix (pipeline not retriggered)...")

                if did_push_fix:
                    pipeline = await _github.poll_pipeline(
                        repo_name,
                        interval=30,
                        stop_flag=lambda: self.is_stopped(user_id),
                        progress_cb=cb,
                    )
                
                did_push_fix = False

                if pipeline.get("status") == "stopped":
                    return {"status": "stopped"}

                if pipeline.get("conclusion") == "success":
                    await cb("Destroy succeeded — cleaning up SSM and S3...")
                    _aws.delete_ssm_keys(project)
                    _aws.delete_s3_state(project)
                    state.update_deployment(project, status="destroyed")
                    state.log_step(project, "destroy", "done")

                    if delete_repo:
                        # ── Destroy AWS resources for ALL other branches before deleting repo ──
                        all_deps = state.list_deployments_by_repo(repo_name)
                        other_deps = [
                            d for d in all_deps
                            if d.get("project") != project and d.get("status") != "destroyed"
                        ]
                        if other_deps:
                            await cb(f"Found {len(other_deps)} other branch(es) with AWS resources — destroying...")
                        for dep_rec in other_deps:
                            br_project = dep_rec["project"]
                            br_branch  = dep_rec.get("branch", "main")
                            await cb(f"  → Destroying branch '{br_branch}' ({br_project})...")
                            try:
                                # Patch terraform files on that branch
                                br_files = _github.get_existing_files(repo_name, branch=br_branch)
                                _patch_terraform_bucket(br_files, bucket_name, dep_rec.get("region", region))
                                for fpath, fc in br_files.items():
                                    if fpath.endswith(".tf"):
                                        _github.push_single_file(
                                            repo_name, fpath, fc,
                                            "fix: update terraform backend for full-repo destroy",
                                            branch=br_branch,
                                        )
                                # Trigger destroy pipeline on that branch
                                trig = _github.trigger_pipeline(repo_name, "destroy.yml", branch=br_branch)
                                if trig.get("status") == "error":
                                    await cb(f"    Could not trigger destroy for '{br_branch}': {trig.get('error')} — skipping")
                                    continue
                                await cb(f"    Pipeline triggered: {trig.get('url', '')}")
                                br_pipeline = await _github.poll_pipeline(
                                    repo_name,
                                    interval=30,
                                    stop_flag=lambda: self.is_stopped(user_id),
                                    progress_cb=cb,
                                )
                                if br_pipeline.get("conclusion") == "success":
                                    _aws.delete_ssm_keys(br_project)
                                    _aws.delete_s3_state(br_project)
                                    state.update_deployment(br_project, status="destroyed")
                                    state.log_step(br_project, "destroy", "done")
                                    await cb(f"    ✓ Branch '{br_branch}' AWS resources destroyed")
                                else:
                                    await cb(f"    ⚠ Branch '{br_branch}' destroy pipeline did not succeed — cleaning up state anyway")
                                    _aws.delete_ssm_keys(br_project)
                                    _aws.delete_s3_state(br_project)
                                    state.update_deployment(br_project, status="destroyed")
                            except Exception as e_br:
                                await cb(f"    ⚠ Error destroying branch '{br_branch}': {e_br} — continuing")

                        await cb("Deleting GitHub repo...")
                        _github.cleanup(repo_name, delete_repo=True)
                    elif delete_branch and branch != "main":
                        await cb(f"Deleting branch '{branch}'...")
                        try:
                            _github.delete_branch(repo_name, branch)
                        except Exception as e:
                            await cb(f"Could not delete branch: {e}")

                    return {"status": "success", "message": f"Destroyed {project}"}

                # Pipeline failed — auto fix and retry
                if retry >= MAX_DESTROY_RETRIES:
                    await cb(f"Destroy failed after {MAX_DESTROY_RETRIES} attempts")
                    await cb(f"Check logs: {pipeline.get('run_url', '')}")
                    await cb("Use /aws cleanup to remove resources manually")
                    break

                retry += 1
                await cb(f"Destroy pipeline failed — fixing (retry {retry}/{MAX_DESTROY_RETRIES})...")

                # Fetch latest files from the DEPLOY branch (not main)
                repo_files_now = _github.get_existing_files(repo_name, branch=deploy_branch)
                for path, fcontent in repo_files_now.items():
                    if path and fcontent:  # guard against None/empty paths from GitHub API tree objects
                        state.save_file(project, path, fcontent)

                # S3 403 on destroy = wrong bucket in backend config
                # Fix by re-patching .tf files and re-pushing to deploy branch
                combined_log = " ".join(
                    j.get("log", "") for j in pipeline.get("all_jobs", [])
                )
                if ("403" in combined_log or "AccessDenied" in combined_log or "Access Denied" in combined_log) and ("tfstate" in combined_log.lower() or "ListObjects" in combined_log or "HeadObject" in combined_log):
                    correct_bucket = _aws.get_state_bucket_name()
                    await cb(f"S3 403 detected — updating TF_STATE_BUCKET secret → {correct_bucket}")
                    # Update secret so pipeline uses correct bucket on retry
                    _github.set_secrets(repo_name, {"TF_STATE_BUCKET": correct_bucket})
                    # Clean any hardcoded bucket from .tf files
                    _patch_terraform_bucket(repo_files_now, correct_bucket, region)
                    for path, fc in repo_files_now.items():
                        if path.endswith(".tf"):
                            _github.push_single_file(
                                repo_name, path, fc,
                                f"fix: correct S3 backend bucket for destroy attempt {retry}",
                                branch=deploy_branch,
                            )
                            await cb(f"Patched {path}")
                    did_push_fix = True
                    await asyncio.sleep(3)
                    continue

                # General AI fix — pass deploy branch so fix goes to right place
                analysis   = error_agent.analyze(
                    pipeline.get("failed_jobs", []),
                    all_jobs=pipeline.get("all_jobs", []),
                )
                fixes = code_agent.analyze_and_fix(
                    project,
                    analysis.get("log_context", "") or analysis.get("full_log", ""),
                )

                if "error" in fixes:
                    await cb(f"Could not auto-fix: {fixes['error']}")
                    continue

                # Push all fixes to deploy branch
                fixed_files = fixes.get("files") or ([fixes] if fixes.get("file") else [])
                pushed_any = False
                for fix in fixed_files:
                    if not fix.get("file"):
                        continue
                    await cb(f"Fixed {fix['file']}: {fix.get('diff_summary','')}")
                    push = _github.push_single_file(
                        repo_name,
                        fix["file"],
                        fix["fixed_content"],
                        f"fix destroy attempt {retry}",
                        branch=deploy_branch,
                    )
                    if not push.get("failed"):
                        pushed_any = True
                
                if pushed_any:
                    did_push_fix = True
                    await asyncio.sleep(3)
                else:
                    await cb("No file was pushed.")

            state.update_deployment(project, status="destroy_failed")
            return {"status": "failed", "message": "Destroy pipeline failed"}

        except StopIteration:
            return {"status": "stopped"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ── Status ────────────────────────────────────────────────────────────────

    def get_status(self, project: str) -> dict:
        return {
            "deployment": state.get_deployment(project),
            "steps":      state.get_steps(project),
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _extract_url(self, pipeline: dict, region: str = "") -> str:
        """Extract ALB URL from ECS pipeline logs."""
        import re
        for job in pipeline.get("all_jobs", []):
            log = job.get("log", "")
            for pattern in [
                r'alb_url\s*=\s*"?(https?://[a-zA-Z0-9.*-]+(?:\.[a-zA-Z0-9.*-]+)*)"?',
                r"Application is available at:\s*(https?://[a-zA-Z0-9.*-]+(?:\.[a-zA-Z0-9.*-]+)*)",
                r"Live URL:\s*(https?://[a-zA-Z0-9.*-]+(?:\.[a-zA-Z0-9.*-]+)*)",
                r"http://([a-zA-Z0-9.*-]+(?:\.[a-zA-Z0-9.*-]+)*\.elb\.amazonaws\.com)",
            ]:
                match = re.search(pattern, log)
                if match:
                    val = match.group(1)
                    url = val if val.startswith("http") else f"http://{val}"
                    if "***" in url and region:
                        url = url.replace("***", region)
                    return url
        return ""

    def _extract_ip(self, pipeline: dict) -> str:
        import re
        ip_pattern = re.compile(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})")
        for job in pipeline.get("all_jobs", []):
            log = job.get("log", "")
            # Try all common patterns in pipeline logs
            for pattern in [
                r"Live URL:\s*http://(\d+\.\d+\.\d+\.\d+)",
                r"Live at http://(\d+\.\d+\.\d+\.\d+)",
                r"Server IP:\s*(\d+\.\d+\.\d+\.\d+)",
                r"server_ip=(\d+\.\d+\.\d+\.\d+)",
                r"ip=(\d+\.\d+\.\d+\.\d+)",
                r"public_ip=(\d+\.\d+\.\d+\.\d+)",
                r"http://(\d+\.\d+\.\d+\.\d+)",
            ]:
                match = re.search(pattern, log)
                if match:
                    return match.group(1)
        return ""


orchestrator = Orchestrator()
