"""
GitHub Agent — pure PyGithub, no AI
Handles: repo, push, secrets, trigger, poll, branches, PRs, merges
No hardcoded branch strategies — user decides everything.
"""
import os
import time
import logging
import requests
from github import Github
from github.GithubException import GithubException

logger = logging.getLogger(__name__)


class GitHubAgent:

    def __init__(self):
        self.token    = os.getenv("GITHUB_TOKEN", "")
        self.username = os.getenv("GITHUB_USERNAME", "")

    @classmethod
    def with_creds(cls, creds: dict) -> "GitHubAgent":
        """Return a new GitHubAgent scoped to the given per-user credentials."""
        inst          = cls.__new__(cls)
        inst.token    = creds.get("github_token") or os.getenv("GITHUB_TOKEN", "")
        inst.username = creds.get("github_username") or os.getenv("GITHUB_USERNAME", "")
        return inst

    def _gh(self):   return Github(self.token)
    def _user(self): return self._gh().get_user()

    # ── Repo ──────────────────────────────────────────────────────────────────

    def create_repo(self, name: str, description: str = "") -> dict:
        try:
            user = self._user()
            try:
                repo = user.get_repo(name)
                return {"status": "exists", "url": repo.html_url}
            except GithubException:
                repo = user.create_repo(
                    name=name,
                    description=description or "Deployed by DevOps Agent",
                    auto_init=True, private=False,
                )
                time.sleep(2)
                return {"status": "created", "url": repo.html_url}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def delete_repo(self, name: str) -> dict:
        try:
            self._user().get_repo(name).delete()
            return {"status": "deleted"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # ── Branch ────────────────────────────────────────────────────────────────

    def create_branch(self, repo_name: str, branch: str, from_branch: str = "main") -> dict:
        """Create branch from any source branch."""
        try:
            repo = self._user().get_repo(repo_name)
            try:
                repo.get_branch(branch)
                return {"status": "exists", "branch": branch}
            except GithubException:
                source = repo.get_branch(from_branch)
                repo.create_git_ref(f"refs/heads/{branch}", source.commit.sha)
                logger.info(f"Created branch {branch} from {from_branch}")
                return {"status": "created", "branch": branch}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def list_branches(self, repo_name: str) -> dict:
        try:
            repo = self._user().get_repo(repo_name)
            return {"status": "ok", "branches": [b.name for b in repo.get_branches()]}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def delete_branch(self, repo_name: str, branch: str) -> dict:
        try:
            repo = self._user().get_repo(repo_name)
            repo.get_git_ref(f"heads/{branch}").delete()
            return {"status": "deleted", "branch": branch}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def delete_file(self, repo_name: str, file_path: str, branch: str = "main") -> dict:
        """Delete a single file from a branch."""
        try:
            repo = self._user().get_repo(repo_name)
            f    = repo.get_contents(file_path, ref=branch)
            repo.delete_file(
                file_path,
                f"Remove {file_path} (no longer needed)",
                f.sha,
                branch=branch
            )
            return {"status": "deleted", "file": file_path}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def merge_branch(self, repo_name: str, from_branch: str, to_branch: str,
                     message: str = None) -> dict:
        """Merge any branch into any other branch."""
        try:
            repo  = self._user().get_repo(repo_name)
            msg   = message or f"Merge {from_branch} into {to_branch}"
            merge = repo.merge(base=to_branch, head=from_branch, commit_message=msg)
            return {"status": "merged", "sha": merge.sha if merge else "up-to-date"}
        except GithubException as e:
            if e.status == 204:
                return {"status": "nothing_to_merge"}
            return {"status": "error", "error": str(e)}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def create_pull_request(self, repo_name: str, from_branch: str, to_branch: str,
                            title: str = None, body: str = None) -> dict:
        """Create a PR from any branch to any branch."""
        try:
            repo = self._user().get_repo(repo_name)
            t    = title or f"Merge {from_branch} → {to_branch}"
            b    = body  or f"Automated PR from DevOps Agent\nBranch: {from_branch}"
            pr   = repo.create_pull(
                title=t, body=b,
                head=from_branch, base=to_branch,
            )
            return {"status": "created", "url": pr.html_url, "number": pr.number}
        except GithubException as e:
            # PR might already exist
            if "already exists" in str(e).lower() or e.status == 422:
                pulls = list(repo.get_pulls(head=f"{self.username}:{from_branch}", base=to_branch))
                if pulls:
                    return {"status": "exists", "url": pulls[0].html_url, "number": pulls[0].number}
            return {"status": "error", "error": str(e)}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def list_pull_requests(self, repo_name: str, state: str = "open") -> dict:
        try:
            repo  = self._user().get_repo(repo_name)
            pulls = list(repo.get_pulls(state=state))
            return {"status": "ok", "prs": [
                {"number": p.number, "title": p.title,
                 "from": p.head.ref, "to": p.base.ref,
                 "url": p.html_url, "state": p.state}
                for p in pulls
            ]}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # ── Files ─────────────────────────────────────────────────────────────────

    def list_dir_contents(self, repo_name: str, path: str = "", branch: str = "main") -> dict:
        """List files and folders in a specific directory (for interactive browsing)."""
        try:
            repo = self._user().get_repo(repo_name)
            try:
                contents = repo.get_contents(path, ref=branch)
            except GithubException as e:
                # 404 means dir is empty or doesn't exist
                if e.status == 404: return {"status": "ok", "items": []}
                raise

            if not isinstance(contents, list):
                return {"status": "error", "error": f"'{path}' is a file, not a directory"}

            items = []
            for item in contents:
                items.append({
                    "name": item.name,
                    "path": item.path,
                    "type": item.type, # "dir" or "file"
                })
            # Sort: directories first, then alphabetical
            items.sort(key=lambda x: (x["type"] == "file", x["name"].lower()))
            return {"status": "ok", "items": items}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def get_existing_files(self, repo_name: str, branch: str = "main") -> dict:
        try:
            repo  = self._user().get_repo(repo_name)
            files = {}
            def _walk(path=""):
                try:
                    contents = repo.get_contents(path, ref=branch)
                    if not isinstance(contents, list):
                        contents = [contents]
                    for item in contents:
                        if item.type == "dir": _walk(item.path)
                        else:
                            try: files[item.path] = item.decoded_content.decode("utf-8")
                            except: pass
                except: pass
            _walk()
            return files
        except:
            return {}

    def push_files(self, repo_name: str, files: dict,
                   message: str = "Update via DevOps Agent",
                   branch: str = "main") -> dict:
        try:
            repo   = self._user().get_repo(repo_name)
            pushed = []
            failed = []
            for path, content in files.items():
                try:
                    try:
                        existing = repo.get_contents(path, ref=branch)
                        repo.update_file(path, message, content, existing.sha, branch=branch)
                    except GithubException:
                        repo.create_file(path, message, content, branch=branch)
                    pushed.append(path)
                except Exception as e:
                    failed.append({"path": path, "error": str(e)})
            return {"pushed": pushed, "failed": failed}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def push_single_file(self, repo_name: str, path: str, content: str,
                         message: str = None, branch: str = "main") -> dict:
        msg = message or f"Update {path}"
        return self.push_files(repo_name, {path: content}, msg, branch)

    # ── Secrets ───────────────────────────────────────────────────────────────

    def set_secrets(self, repo_name: str, secrets: dict) -> dict:
        try:
            repo     = self._user().get_repo(repo_name)
            set_keys = []
            for name, value in secrets.items():
                try:
                    repo.create_secret(name, str(value))
                    set_keys.append(name)
                except Exception as e:
                    logger.error(f"Secret {name}: {e}")
            return {"set": set_keys}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # ── Pipeline ──────────────────────────────────────────────────────────────

    def trigger_pipeline(self, repo_name: str, workflow: str = "deploy.yml",
                         branch: str = "main") -> dict:
        try:
            repo = self._user().get_repo(repo_name)
            try:
                repo.get_workflow(workflow).create_dispatch(branch)
            except Exception:
                for wf in repo.get_workflows():
                    if wf.path.endswith(workflow):
                        wf.create_dispatch(branch)
                        break
            return {
                "status":   "triggered",
                "workflow": workflow,
                "branch":   branch,
                "url":      f"https://github.com/{self.username}/{repo_name}/actions",
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def get_pipeline_status(self, repo_name: str, branch: str = None) -> dict:
        try:
            repo = self._user().get_repo(repo_name)
            runs = list(repo.get_workflow_runs())
            if branch:
                filtered = [r for r in runs if r.head_branch == branch]
                if filtered: runs = filtered
            if not runs:
                return {"status": "no_runs"}
            latest      = runs[0]
            failed_jobs = []
            all_jobs    = []
            if latest.status == "completed":
                for job in latest.jobs():
                    log_text = self._fetch_job_log(repo_name, job.id)
                    failed_steps = [s.name for s in job.steps if s.conclusion == "failure"]
                    job_info = {
                        "name":         job.name,
                        "conclusion":   job.conclusion,
                        "failed_steps": failed_steps,
                        "log":          log_text,
                    }
                    all_jobs.append(job_info)
                    # Mark as failed if: conclusion=failure OR has failed steps
                    # (catches jobs that show "skipped" but had real errors)
                    if job.conclusion == "failure" or failed_steps:
                        failed_jobs.append(job_info)
                # If no failed jobs found, include all jobs — let error_agent find the error
                if not failed_jobs and latest.conclusion == "failure":
                    failed_jobs = all_jobs
            return {
                "status":      latest.status,
                "conclusion":  latest.conclusion,
                "run_id":      latest.id,
                "run_url":     latest.html_url,
                "branch":      latest.head_branch,
                "all_jobs":    all_jobs,
                "failed_jobs": failed_jobs,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def _fetch_job_log(self, repo_name: str, job_id: int) -> str:
        try:
            resp = requests.get(
                f"https://api.github.com/repos/{self.username}/{repo_name}/actions/jobs/{job_id}/logs",
                headers={"Authorization": f"token {self.token}"},
                allow_redirects=True, timeout=15,
            )
            if not resp.ok:
                return ""
            log = resp.text
            # Return full log — terraform errors appear at the START not the end
            # Cap at 20000 chars total: first 10000 + last 10000
            if len(log) > 20000:
                return log[:10000] + "\n...(middle truncated)...\n" + log[-10000:]
            return log
        except:
            return ""

    def wait_for_idle(self, repo_name: str, timeout: int = 300) -> bool:
        waited = 0
        while waited < timeout:
            try:
                repo = self._user().get_repo(repo_name)
                runs = list(repo.get_workflow_runs())
                if not [r for r in runs if r.status in ("in_progress","queued","waiting")]:
                    return True
                time.sleep(15); waited += 15
            except:
                return True
        return False

    def cancel_running_pipelines(self, repo_name: str) -> dict:
        try:
            repo      = self._user().get_repo(repo_name)
            cancelled = []
            for r in list(repo.get_workflow_runs()):
                if r.status in ("in_progress","queued","waiting"):
                    try: r.cancel(); cancelled.append(r.id)
                    except: pass
            return {"cancelled": cancelled}
        except Exception as e:
            return {"error": str(e)}

    async def poll_pipeline(self, repo_name: str, interval: int = 30,
                            max_wait: int = 1800, branch: str = None,
                            stop_flag=None, progress_cb=None) -> dict:
        import asyncio
        waited = 0
        while waited < max_wait:
            if stop_flag and stop_flag(): return {"status": "stopped"}
            await asyncio.sleep(interval)
            waited += interval
            status = self.get_pipeline_status(repo_name, branch=branch)
            if progress_cb:
                await progress_cb(f"Pipeline: {status.get('status')} / {status.get('conclusion','...')}")
            if status.get("status") == "completed":
                return status
        return {"status": "timeout"}

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def cleanup(self, repo_name: str, delete_repo: bool = False) -> dict:
        result = {}
        if delete_repo:
            result["repo"] = self.delete_repo(repo_name)
        return result

    # ── Handle — all raw git ops, no strategy hardcoded ──────────────────────

    def handle(self, action: str, args: dict) -> dict:
        try:
            if action == "create_repo":
                return self.create_repo(args["name"], args.get("description",""))
            elif action == "delete_repo":
                return self.delete_repo(args["name"])
            elif action == "list_repos":
                repos = [{"name": r.name, "url": r.html_url} for r in self._user().get_repos()]
                return {"status": "ok", "repos": repos}
            elif action == "create_branch":
                return self.create_branch(args["repo"], args["branch"], args.get("from","main"))
            elif action == "list_branches":
                return self.list_branches(args["repo"])
            elif action == "delete_branch":
                return self.delete_branch(args["repo"], args["branch"])
            elif action == "merge":
                return self.merge_branch(args["repo"], args["from"], args["to"], args.get("message"))
            elif action == "pull_request":
                return self.create_pull_request(
                    args["repo"], args["from"], args["to"],
                    args.get("title"), args.get("body"),
                )
            elif action == "list_prs":
                return self.list_pull_requests(args["repo"], args.get("state","open"))
            elif action == "push":
                return self.push_files(args["repo"], args["files"],
                                       args.get("message","Update"),
                                       args.get("branch","main"))
            elif action == "push_file":
                return self.push_single_file(args["repo"], args["path"],
                                             args["content"], args.get("message"),
                                             args.get("branch","main"))
            elif action == "set_secrets":
                return self.set_secrets(args["repo"], args["secrets"])
            elif action == "trigger":
                return self.trigger_pipeline(args["repo"],
                                             args.get("workflow","deploy.yml"),
                                             args.get("branch","main"))
            elif action == "status":
                return self.get_pipeline_status(args["repo"], args.get("branch"))
            elif action == "cleanup":
                return self.cleanup(args["repo"], args.get("delete_repo", False))
            else:
                return {"status": "error", "error": f"Unknown action: {action}"}
        except Exception as e:
            logger.error(f"GitHubAgent error: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}


github_agent = GitHubAgent()