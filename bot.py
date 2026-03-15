"""
Telegram Bot — natural language + commands
No AI here — pure regex/keyword extraction
"""
import asyncio
import logging
import os
import re
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

load_dotenv(override=True)

from orchestrator import orchestrator
from agents.code_agent   import code_agent
from agents.github_agent import github_agent
from agents.aws_agent    import aws_agent
from skills import list_skills, add_skill, delete_skill
import state

import sys
_log_handler = logging.StreamHandler(sys.stdout)
_log_handler.stream.reconfigure(encoding='utf-8', errors='replace') if hasattr(_log_handler.stream, 'reconfigure') else None
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", handlers=[_log_handler])
logger = logging.getLogger(__name__)

ALLOWED_USER = int(os.getenv("TELEGRAM_ALLOWED_USER", "0"))

sessions: dict[int, dict] = {}
running:  dict[int, bool] = {}

def is_running(uid): return running.get(uid, False)
def set_running(uid, v): running[uid] = v


# ── Natural language extractor — NO AI ───────────────────────────────────────

def extract_intent(text: str) -> dict:
    t = text.lower()
    result = {}

    # Intent
    if any(w in t for w in ["destroy", "delete", "remove", "tear down", "teardown"]):
        result["intent"] = "destroy"
    elif any(w in t for w in ["update", "change", "modify", "replace", "push file"]):
        result["intent"] = "update"
    elif any(w in t for w in ["trigger", "retrigger", "run pipeline", "rerun pipeline", "restart pipeline"]):
        result["intent"] = "trigger"
    elif any(w in t for w in ["deploy", "launch", "create", "setup", "set up", "start", "run", "build", "redeploy"]):
        result["intent"] = "deploy"
    else:
        result["intent"] = None

    # App
    apps = {
        "nginx":       ["nginx"],
        "node":        ["node", "nodejs", "node.js", "express"],
        "python":      ["python", "flask", "fastapi", "django"],
        "spring-boot": ["spring", "spring-boot", "springboot", "java"],
        "react":       ["react", "nextjs", "next.js"],
        "docker":      ["docker", "container"],
    }
    for app, keywords in apps.items():
        if any(k in t for k in keywords):
            result["app"] = app
            break

    # Cloud
    if any(w in t for w in ["aws", "ec2", "amazon"]):   result["cloud"] = "AWS"
    elif any(w in t for w in ["azure", "microsoft"]):    result["cloud"] = "Azure"
    elif any(w in t for w in ["gcp", "google cloud"]):   result["cloud"] = "GCP"

    # IaC / Config
    if "pulumi"    in t: result["iac"] = "pulumi"
    elif "terraform" in t: result["iac"] = "terraform"
    if "ansible" in t: result["config"] = "ansible"

    # Region
    region_match = re.search(r"(us-east-[12]|us-west-[12]|eu-west-[123]|ap-southeast-[12]|ap-northeast-[12])", t)
    if region_match:
        result["region"] = region_match.group(1)

    # Repo URL
    url_match = re.search(r"https?://github\.com/([\w-]+/[\w-]+)", text)
    if url_match:
        result["repo_url"]  = url_match.group(0)
        result["repo_name"] = url_match.group(1).split("/")[-1]
        result["project"]   = result["repo_name"]

    # Repo name from "in repo X" or "repo X"
    repo_match = re.search(r"(?:in|to|for|repo|repository)\s+([\w-]+)", t)
    if repo_match:
        candidate = repo_match.group(1)
        skip = {"aws","ec2","amazon","nginx","node","python","spring","react","docker",
                "terraform","ansible","the","my","a","an","to","in","on","with"}
        if candidate not in skip:
            result["repo_name"] = candidate
            if "project" not in result:
                result["project"] = candidate

    # Project name
    if "project" not in result:
        patterns = [
            r"(?:deploy|launch|setup|for|project|named?|called?)\s+([\w-]+)",
            r"([\w-]+)\s+(?:project|app|service|repo)",
        ]
        for pattern in patterns:
            match = re.search(pattern, t)
            if match:
                candidate = match.group(1)
                skip = {"nginx","node","python","spring","react","docker","aws","ec2",
                        "terraform","ansible","the","my","a","an","to","in","on","with",
                        "and","or","using","use","deploy","launch","setup","create","html",
                        "file","repo","pipeline","trigger"}
                if candidate not in skip and len(candidate) > 1:
                    result["project"] = candidate
                    break

    # Deployment target
    if any(w in t for w in ["ecs", "fargate", "container service", "elastic container"]):
        result["target"] = "ecs"
    elif any(w in t for w in ["ec2 with docker", "docker on ec2", "ec2 docker"]):
        result["target"] = "ec2-docker"
    elif any(w in t for w in ["docker", "container"]) and "ecs" not in t:
        result["target"] = "ask"   # ambiguous — bot must ask
    elif any(w in t for w in ["ec2", "vm", "instance", "directly"]):
        result["target"] = "ec2"

    # Branch name
    branch_match = re.search(r"(?:branch|on|from|cut)\s+([\w/.-]+)", t)
    if branch_match:
        candidate = branch_match.group(1)
        skip = {"main","aws","ec2","docker","nginx","the","a","an"}
        if candidate not in skip:
            result["branch"] = candidate

    # PR intent
    if any(w in t for w in ["pull request", "pr", "create pr", "open pr"]):
        result["pr"] = True

    # Merge intent
    if "merge" in t:
        result["merge"] = True
        merge_match = re.search(r"merge\s+([\w/.-]+)\s+(?:to|into)\s+([\w/.-]+)", t)
        if merge_match:
            result["merge_from"] = merge_match.group(1)
            result["merge_to"]   = merge_match.group(2)

    # File path (for update intent)
    file_match = re.search(r"([\w/.-]+\.(?:html|yml|yaml|tf|py|js|json|md|sh))", text)
    if file_match:
        result["file"] = file_match.group(1)

    return result


def missing_fields(answers: dict) -> list:
    needed = []
    if not answers.get("project"):  needed.append("project")
    if not answers.get("app"):      needed.append("app")
    if not answers.get("repo"):     needed.append("repo")
    return needed


FIELD_QUESTIONS = {
    "project": "Project name?",
    "app":     "What to deploy? (e.g. nginx, node, python, spring-boot)",
    "target":  "Where to run it?\n  EC2        — directly on EC2 (no Docker)\n  EC2-Docker — Docker container on EC2\n  ECS       — Amazon ECS Fargate (fully managed)",
    "repo":    "GitHub repo name?",
    "branch":  "Which branch? (e.g. main, feature/docker, dev)",
    "region":  "AWS region? (e.g. us-east-1, ap-southeast-1)",
}

# ── Database-specific env var defaults ────────────────────────────────────────
DB_ENV_DEFAULTS = {
    "mongodb": [
        ("DB_NAME",     "mydb"),
        ("DB_USER",     "admin"),
        ("DB_PASSWORD", ""),
        ("MONGO_URI",   "mongodb://localhost:27017/mydb"),
    ],
    "postgres": [
        ("DB_NAME",     "mydb"),
        ("DB_USER",     "postgres"),
        ("DB_PASSWORD", ""),
        ("DB_HOST",     "localhost"),
        ("DB_PORT",     "5432"),
    ],
    "mysql": [
        ("DB_NAME",     "mydb"),
        ("DB_USER",     "root"),
        ("DB_PASSWORD", ""),
        ("DB_HOST",     "localhost"),
        ("DB_PORT",     "3306"),
    ],
}

async def _ask_field_with_buttons(update: Update, uid: int, missing_field: str):
    """Answers with an inline keyboard if the field has predefined choices."""
    text = FIELD_QUESTIONS.get(missing_field, f"{missing_field}?")
    reply_markup = None
    
    if missing_field == "app":
        keyboard = [
            [InlineKeyboardButton("Nginx", callback_data="ans_app|nginx"),
             InlineKeyboardButton("Node.js", callback_data="ans_app|node")],
            [InlineKeyboardButton("Python", callback_data="ans_app|python"),
             InlineKeyboardButton("Spring Boot", callback_data="ans_app|spring-boot")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
    elif missing_field == "target":
        keyboard = [
            [InlineKeyboardButton("EC2 (Direct)", callback_data="ans_tgt|ec2")],
            [InlineKeyboardButton("EC2 (Docker)", callback_data="ans_tgt|ec2-docker")],
            [InlineKeyboardButton("ECS Fargate", callback_data="ans_tgt|ecs")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(text, reply_markup=reply_markup)

DEPLOY_FIELDS = ["project", "app", "target", "repo", "branch", "region"]

TARGET_ALIASES = {
    "1": "ec2", "direct": "ec2", "vm": "ec2",
    "2": "ec2-docker", "docker": "ec2-docker", "ec2 docker": "ec2-docker",
    "3": "ecs", "fargate": "ecs", "container service": "ecs", "ecs fargate": "ecs",
}


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "DevOps Agent — just tell me what you want:\n\n"
        "Examples:\n"
        "  deploy nginx to aws\n"
        "  update html in repo my-repo\n"
        "  replace html/index.html in my-repo\n"
        "  trigger pipeline in my-repo\n"
        "  destroy my-repo\n\n"
        "Commands:\n"
        "  /deploy /update /destroy /trigger /status /projects\n"
        "  /code /github /aws /skills /list\n"
        "  /stop /reset"
    )

async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    orchestrator.stop(uid)
    sessions.pop(uid, None)
    set_running(uid, False)
    await update.message.reply_text("Stopping...")

async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions.pop(uid, None)
    orchestrator.resume(uid)
    set_running(uid, False)
    await update.message.reply_text("Reset done.")

async def cmd_projects(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    projects = state.list_projects()
    if not projects:
        await update.message.reply_text("No projects yet.")
        return
    lines = ["Projects:\n"]
    for p in projects:
        ip = f" → http://{p['ec2_ip']}" if p.get("ec2_ip") else ""
        lines.append(f"  {p['project']} ({p['status']}){ip}")
    await update.message.reply_text("\n".join(lines))

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 **DevOps Agent Commands**\n\n"
        "**Deployment Commands:**\n"
        "/deploy — Start a new deployment (EC2, Docker, ECS)\n"
        "/update — Update files in an existing project\n"
        "/destroy — Destroy a deployed project\n"
        "/trigger — Trigger GitHub Actions pipeline\n"
        "/status — Check deployment status\n"
        "/projects — List all deployed projects\n\n"
        "**Development Tools:**\n"
        "/code — AI-powered code generation and fixes\n"
        "/github — GitHub repository management\n"
        "/aws — AWS resource management\n"
        "/tfstate — Terraform state management\n\n"
        "**Skills Management:**\n"
        "/skills — List available DevOps skills\n"
        "/addskill — Add a custom skill\n"
        "/delskill — Delete a skill\n\n"
        "**Control Commands:**\n"
        "/start — Show help and examples\n"
        "/stop — Stop current operation\n"
        "/reset — Reset session state\n"
        "/list — Show this command list\n\n"
        "**Project Generators:**\n"
        "/initnode — Interactive wizard to generate a Node.js project"
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid     = update.effective_user.id
    sess    = sessions.get(uid, {})
    project = sess.get("project") or sess.get("answers", {}).get("project")
    if not project:
        await update.message.reply_text("No active project.")
        return
    s     = orchestrator.get_status(project)
    dep   = s.get("deployment") or {}
    steps = s.get("steps", [])
    last  = steps[-1] if steps else {}
    await update.message.reply_text(
        f"Project: {project}\n"
        f"Status:  {dep.get('status','unknown')}\n"
        f"IP:      {dep.get('ec2_ip','none')}\n"
        f"Last:    {last.get('step')} — {last.get('status')}"
    )

def _build_deployment_readme(project: str, app: str, target: str,
                              target_label: str, branch: str,
                              region: str, url: str, repo: str) -> str:
    """Generate README.md content describing this branch deployment."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    infra_details = {
        "ec2": (
            "- **Infrastructure**: AWS EC2 instance\n"
            "- **Config**: Ansible playbook\n"
            "- **State**: S3 Terraform backend"
        ),
        "ec2-docker": (
            "- **Infrastructure**: AWS EC2 instance\n"
            "- **Runtime**: Docker container\n"
            "- **Config**: Ansible installs Docker + runs container\n"
            "- **State**: S3 Terraform backend"
        ),
        "ecs": (
            "- **Infrastructure**: Amazon ECS Fargate (serverless containers)\n"
            "- **Registry**: Amazon ECR\n"
            "- **Load Balancer**: Application Load Balancer (ALB)\n"
            "- **State**: S3 Terraform backend"
        ),
    }.get(target, f"- **Target**: {target_label}")

    pipeline_details = {
        "ec2":        "Terraform → Ansible (direct install) → Verify → Notify",
        "ec2-docker": "Terraform → Ansible (Docker install + run) → Verify → Notify",
        "ecs":        "Terraform → Build Docker image → Push to ECR → Update ECS service → Verify",
    }.get(target, "Terraform → Deploy → Verify")

    return f"""# {project}

> **Branch**: `{branch}` — deployed by DevOps Agent

## 🚀 Deployment Info

| Field | Value |
|-------|-------|
| **App** | {app} |
| **Target** | {target_label} |
| **Branch** | `{branch}` |
| **Region** | {region} |
| **Live URL** | {url or "_(check pipeline logs)_"} |
| **Last Deploy** | {now} |

## 🏗 Infrastructure

{infra_details}

## ⚙️ Pipeline

```
{pipeline_details}
```

## 📁 Key Files

| File | Purpose |
|------|---------|
| `terraform/main.tf` | AWS infrastructure definition |
{"| `ansible/playbook.yml` | Server configuration & app setup |" if target != "ecs" else "| `Dockerfile` | Container image definition |"}
| `.github/workflows/deploy.yml` | CI/CD deploy pipeline |
| `.github/workflows/destroy.yml` | Infrastructure teardown |
{"| `Dockerfile` | Docker container definition |" if target == "ec2-docker" else ""}

## 🔧 How to Deploy

1. Push changes to branch `{branch}`
2. GitHub Actions will automatically trigger
3. Or use the DevOps Agent bot: `/trigger {repo}`

## 💣 How to Destroy

Use the DevOps Agent bot:
```
/destroy
```
Or trigger `.github/workflows/destroy.yml` manually in GitHub Actions.

---
*Auto-generated by DevOps Agent on {now}*
"""


async def cmd_deploy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_running(uid):
        await update.message.reply_text("Job running. /stop to cancel.")
        return
    sessions[uid] = {"mode": "collect", "answers": {}, "missing": list(DEPLOY_FIELDS)}
    await update.message.reply_text(FIELD_QUESTIONS[DEPLOY_FIELDS[0]])

async def cmd_update(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions[uid] = {"mode": "update_repo", "answers": {}}
    await update.message.reply_text("Repo name? (e.g. my-repo)")

async def cmd_destroy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions[uid] = {"mode": "destroy_project", "answers": {}}
    await update.message.reply_text("Which project to destroy?")

async def cmd_trigger(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    args = ctx.args
    if args:
        repo = args[0]
        workflow = args[1] if len(args) > 1 else "deploy.yml"
        await update.message.reply_text(f"Triggering {workflow} in {repo}...")
        r = github_agent.handle("trigger", {"repo": repo, "workflow": workflow})
        await update.message.reply_text(f"{r.get('status')} — {r.get('url', r.get('error',''))}")
    else:
        sessions[uid] = {"mode": "trigger_repo", "answers": {}}
        await update.message.reply_text("Repo name to trigger pipeline?")

async def cmd_initnode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions[uid] = {"mode": "init_node_repo", "answers": {}}
    await update.message.reply_text("🚀 Node.js Project Generator\n\nWhat should we name the new GitHub repository?")


# ── Agent commands ────────────────────────────────────────────────────────────

async def cmd_code(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    uid  = update.effective_user.id
    if not args:
        await update.message.reply_text(
            "/code ask <question>\n"
            "/code gen terraform <project>\n"
            "/code gen ansible <project> <app>\n"
            "/code gen html <project>\n"
            "/code gen pipeline <project>\n"
            "/code generate <project> <app>\n"
            "/code fix <project> <file> <error>\n"
            "/code update <project> <file> <instruction>"
        )
        return
    action = args[0].lower()

    if action == "ask":
        await update.message.reply_text("Thinking...")
        response = code_agent.ask(" ".join(args[1:]))
        sessions[uid] = {"mode": "code_response", "last_code": response}
        await _send_long(update, response)

    elif action == "gen" and len(args) >= 3:
        sub     = args[1].lower()
        project = args[2]
        await update.message.reply_text(f"Generating {sub}...")
        if sub == "terraform":
            r = code_agent.handle("gen_terraform", {"project": project, "region": args[3] if len(args)>3 else "us-east-1"})
        elif sub == "ansible":
            r = code_agent.handle("gen_ansible", {"project": project, "app": args[3] if len(args)>3 else "nginx"})
        elif sub == "html":
            r = code_agent.handle("gen_html", {"project": project})
        elif sub == "pipeline":
            r = code_agent.handle("gen_pipeline", {"project": project, "region": args[3] if len(args)>3 else "us-east-1"})
        else:
            await update.message.reply_text(f"Unknown: {sub}"); return
        sessions[uid] = {"mode": "code_response", "last_code": r.get("content",""), "last_file": r.get("file")}
        await update.message.reply_text(f"File: {r.get('file')}")
        await _send_long(update, f"```\n{r.get('content','')[:3000]}\n```")

    elif action == "generate":
        project = args[1] if len(args)>1 else ""
        app     = args[2] if len(args)>2 else "nginx"
        region  = args[3] if len(args)>3 else "us-east-1"
        await update.message.reply_text(f"Generating all files for {project}...")
        r = code_agent.handle("generate", {"project": project, "app": app, "region": region})
        await update.message.reply_text("Generated:\n" + "\n".join(f"  {f}" for f in r.get("files",[])))

    elif action == "fix":
        project = args[1] if len(args)>1 else ""
        file_   = args[2] if len(args)>2 else ""
        error   = " ".join(args[3:])
        await update.message.reply_text(f"Fixing {file_}...")
        r = code_agent.handle("fix", {"project": project, "file": file_, "error": error})
        sessions[uid] = {"mode": "fix_approval", "fix": r, "answers": {"project": project, "repo_name": project}}
        await update.message.reply_text(f"Fix ready:\n{r.get('diff_summary')}\n\nApply? (yes/no)")

    elif action == "update":
        project     = args[1] if len(args)>1 else ""
        file_       = args[2] if len(args)>2 else ""
        instruction = " ".join(args[3:])
        await update.message.reply_text(f"Updating {file_}...")
        r = code_agent.handle("update", {"project": project, "file": file_, "instruction": instruction})
        sessions[uid] = {"mode": "push_approval", "file": file_, "content": r.get("content"), "answers": {"project": project}}
        await update.message.reply_text(f"Updated:\n{r.get('diff_summary')}\n\nPush to GitHub? (yes/no)")


async def cmd_github(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    uid  = update.effective_user.id

    # No args — start conversational menu
    if not args:
        sessions[uid] = {"mode": "gh_menu", "answers": {}}
        await update.message.reply_text(
            "GitHub Agent — what do you want to do?\n\n"
            "  push      — push a file to repo\n"
            "  pull      — get a file from repo\n"
            "  trigger   — run pipeline\n"
            "  status    — pipeline status\n"
            "  logs      — last pipeline logs\n"
            "  files     — list files in repo\n"
            "  branches  — list branches\n"
            "  branch    — create a branch\n"
            "  pr        — create pull request\n"
            "  merge     — merge branch into another\n"
            "  list      — list repos\n"
            "  create    — create repo\n"
            "  delete    — delete repo\n"
            "  secrets   — set secrets\n"
        )
        return

    action = args[0].lower()

    if action == "create":
        r = github_agent.handle("create_repo", {"name": args[1]})
        await update.message.reply_text(f"{r.get('status')} — {r.get('url', r.get('error',''))}")
    elif action == "delete":
        r = github_agent.handle("delete_repo", {"name": args[1]})
        await update.message.reply_text(f"Delete: {r.get('status')}")
    elif action == "list":
        r = github_agent.handle("list_repos", {})
        lines = [f"{x['name']} — {x['url']}" for x in r.get("repos",[])[:15]]
        await update.message.reply_text("Repos:\n" + "\n".join(lines) if lines else "No repos")
    elif action == "files":
        repo  = args[1] if len(args)>1 else ""
        if not repo:
            sessions[uid] = {"mode": "gh_files_repo", "answers": {}}
            await update.message.reply_text("Repo name?")
            return
        files = github_agent.get_existing_files(repo)
        lines = list(files.keys())
        await update.message.reply_text(f"Files in {repo}:\n" + "\n".join(f"  {f}" for f in lines) if lines else "Empty repo")
    elif action == "pull":
        repo = args[1] if len(args)>1 else ""
        file = args[2] if len(args)>2 else ""
        if not repo:
            sessions[uid] = {"mode": "gh_pull_repo", "answers": {}}
            await update.message.reply_text("Repo name?")
            return
        if not file:
            sessions[uid] = {"mode": "gh_pull_file", "answers": {"repo": repo}}
            await update.message.reply_text("File path? (e.g. html/index.html)")
            return
        files = github_agent.get_existing_files(repo)
        cnt   = files.get(file, "")
        await (_send_long(update, f"```\n{cnt[:3500]}\n```") if cnt else update.message.reply_text(f"Not found: {file}"))
    elif action == "push":
        repo = args[1] if len(args)>1 else ""
        file = args[2] if len(args)>2 else ""
        if not repo:
            sessions[uid] = {"mode": "gh_push_repo", "answers": {}}
            await update.message.reply_text("Repo name?")
            return
        if not file:
            sessions[uid] = {"mode": "gh_push_file", "answers": {"repo": repo}}
            await update.message.reply_text("File path? (e.g. html/index.html)")
            return
        sessions[uid] = {"mode": "gh_push_content", "answers": {"repo": repo, "file": file}}
        await update.message.reply_text(f"Paste new content for {file}:")
    elif action == "secrets":
        repo    = args[1] if len(args)>1 else ""
        secrets = dict(kv.split("=",1) for kv in args[2:] if "=" in kv)
        r = github_agent.handle("set_secrets", {"repo": repo, "secrets": secrets})
        await update.message.reply_text(f"Set: {r.get('set', r.get('error'))}")
    elif action == "trigger":
        repo     = args[1] if len(args)>1 else ""
        workflow = args[2] if len(args)>2 else "deploy.yml"
        if not repo:
            sessions[uid] = {"mode": "gh_trigger_repo", "answers": {}}
            await update.message.reply_text("Repo name?")
            return
        r = github_agent.trigger_pipeline(repo, workflow)
        await update.message.reply_text(f"{r.get('status')} — {r.get('url', r.get('error',''))}")
    elif action == "status":
        repo = args[1] if len(args)>1 else ""
        if not repo:
            sessions[uid] = {"mode": "gh_status_repo", "answers": {}}
            await update.message.reply_text("Repo name?")
            return
        r = github_agent.get_pipeline_status(repo)
        await update.message.reply_text(f"Status: {r.get('status')}\nConclusion: {r.get('conclusion','...')}\n{r.get('run_url','')}")
    elif action == "logs":
        repo = args[1] if len(args)>1 else ""
        if not repo:
            sessions[uid] = {"mode": "gh_logs_repo", "answers": {}}
            await update.message.reply_text("Repo name?")
            return
        result = github_agent.get_pipeline_status(repo)
        for job in result.get("failed_jobs", [])[:2]:
            await _send_long(update, f"=== {job['name']} ===\n{job.get('log','')[-2000:]}")
        if not result.get("failed_jobs"):
            await update.message.reply_text("No failed jobs found")


async def cmd_aws(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text(
            "/aws check <project>\n/aws list\n/aws sshkey <project>\n"
            "/aws s3\n/aws cleanup <project>\n/aws creds"
        )
        return
    action = args[0].lower()
    if action == "check":
        r = aws_agent.handle("check_ec2", {"project": args[1]})
        await update.message.reply_text(f"EC2: {r['ip']}" if r.get("exists") else f"No EC2 for {args[1]}")
    elif action == "list":
        r = aws_agent.handle("list_ec2", {})
        lines = [f"{i['project'] or 'unknown'}: {i['ip']} ({i['type']})" for i in r.get("instances",[])]
        await update.message.reply_text("Running:\n" + "\n".join(lines) if lines else "No instances")
    elif action == "sshkey":
        r = aws_agent.handle("gen_ssh_key", {"project": args[1]})
        await update.message.reply_text("SSH key generated" if "error" not in r else f"Error: {r['error']}")
    elif action == "s3":
        r = aws_agent.handle("ensure_s3", {})
        await update.message.reply_text(f"S3: {r.get('bucket')} — {'exists' if r.get('exists') else 'created'}")
    elif action == "cleanup":
        r = aws_agent.handle("cleanup", {"project": args[1]})
        await update.message.reply_text(f"Cleaned: {r}")
    elif action == "creds":
        r     = aws_agent.handle("credentials", {})
        creds = r.get("credentials", {})
        await update.message.reply_text(f"Key: {creds.get('AWS_ACCESS_KEY_ID','')[:8]}...\nRegion: {creds.get('AWS_REGION')}")


async def cmd_tfstate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /tfstate list                     — list all projects' state in S3
    /tfstate clear <project>          — delete state for one project
    /tfstate nuke                     — delete ALL state + empty bucket (asks confirm)
    """
    uid  = update.effective_user.id
    args = ctx.args

    if not args:
        await update.message.reply_text(
            "🗄 Terraform State Manager\n\n"
            "/tfstate list              — show all states in S3\n"
            "/tfstate clear <project>   — clear state for one project\n"
            "/tfstate nuke              — wipe entire S3 bucket (careful!)"
        )
        return

    action = args[0].lower()

    if action == "list":
        r = aws_agent.list_tf_states()
        if "error" in r:
            await update.message.reply_text(f"❌ {r['error']}")
            return
        if not r.get("projects"):
            await update.message.reply_text(f"Bucket `{r['bucket']}` is empty — no state files found.")
            return
        lines = [f"Bucket: {r['bucket']}\n"]
        for proj, keys in r["projects"].items():
            lines.append(f"📁 {proj}:")
            for k in keys:
                lines.append(f"   • {k}")
        lines.append(f"\nTotal: {r['total']} file(s)")
        await update.message.reply_text("\n".join(lines))

    elif action == "clear":
        if len(args) < 2:
            await update.message.reply_text("Usage: /tfstate clear <project>")
            return
        project = args[1]
        await update.message.reply_text(f"🧹 Clearing Terraform state for `{project}`...")
        r = aws_agent.clear_tf_state(project)
        if r.get("deleted"):
            lines = [f"✅ Deleted {len(r['deleted'])} object(s) from `{r['bucket']}`:"]
            for k in r["deleted"]:
                lines.append(f"   • {k}")
            if r.get("errors"):
                lines.append(f"\n⚠️ Errors: {r['errors']}")
            await update.message.reply_text("\n".join(lines))
        elif r.get("errors"):
            await update.message.reply_text(f"❌ Errors:\n" + "\n".join(r["errors"]))
        else:
            await update.message.reply_text(f"Nothing found for `{project}` in S3.")

    elif action == "nuke":
        # Ask for confirmation first
        sessions[uid] = {"mode": "confirm_nuke_s3"}
        bucket = aws_agent.get_state_bucket_name()
        await update.message.reply_text(
            f"⚠️ WARNING: This will delete ALL objects in `{bucket}` and remove the bucket.\n"
            f"This affects ALL projects' Terraform state.\n\n"
            f"Type YES to confirm, or anything else to cancel."
        )

    else:
        await update.message.reply_text(f"Unknown action: {action}\nUse: list, clear <project>, nuke")


async def cmd_skills(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    skills = list_skills()
    lines  = [f"  [{s['type']}] {s['name']}" for s in skills]
    await update.message.reply_text("Skills:\n" + "\n".join(lines) + "\n\n/addskill to add custom")

async def cmd_addskill(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    args = ctx.args
    if args:
        sessions[uid] = {"mode": "add_skill", "skill_name": args[0]}
        await update.message.reply_text(f"Paste skill content for '{args[0]}':")
    else:
        sessions[uid] = {"mode": "add_skill_name"}
        await update.message.reply_text("Skill name?")

async def cmd_delskill(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: /delskill <name>")
        return
    ok = delete_skill(args[0])
    await update.message.reply_text(f"Deleted: {args[0]}" if ok else f"Not found: {args[0]}")


# ── Main message handler ──────────────────────────────────────────────────────

async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    text = update.message.text.strip()

    if ALLOWED_USER and uid != ALLOWED_USER:
        await update.message.reply_text("Unauthorized.")
        return

    if is_running(uid):
        await update.message.reply_text("Job running. /stop to cancel.")
        return

    sess = sessions.get(uid, {})
    mode = sess.get("mode", "")

    # ── Repo Name Validation ──────────────────────────────────────────────────
    # Check for space in repo name during repo prompt flows
    is_asking_for_repo = False
    if mode.endswith("_repo"):
        is_asking_for_repo = True
    elif mode == "collect":
        missing = sess.get("missing", [])
        if missing and missing[0] == "repo":
            is_asking_for_repo = True
            
    if is_asking_for_repo and " " in text:
        suggestion = text.replace(" ", "-").lower()
        await update.message.reply_text(
            f"⚠️ Repository names cannot contain spaces.\n"
            f"Did you mean: {suggestion}?\n"
            f"Please provide the repository name again without spaces."
        )
        return

    # ── Session modes ─────────────────────────────────────────────────────────

    # ── GitHub conversational menu ───────────────────────────────────────────
    if mode == "gh_menu":
        action = text.strip().lower()
        valid = ("push","pull","trigger","status","logs","files","branches","branch",
                 "pr","merge","list","create","delete","secrets")
        if action in valid:
            if action == "list":
                r = github_agent.handle("list_repos", {})
                lines = [f"{x['name']} — {x['url']}" for x in r.get("repos",[])[:15]]
                await update.message.reply_text("Repos:\n" + "\n".join(lines) if lines else "No repos")
                sessions.pop(uid, None)
            else:
                sessions[uid] = {"mode": f"gh_{action}_repo", "answers": {}}
                await update.message.reply_text("Repo name?")
        else:
            await update.message.reply_text(
                "Choose: push / pull / trigger / status / logs / files\n"
                "        branches / branch / pr / merge / list / create / delete"
            )
        return

    # Branch operations
    if mode == "gh_branch_repo":
        sess["answers"]["repo"] = text.strip()
        sessions[uid] = {"mode": "gh_branch_name", "answers": sess["answers"]}
        await update.message.reply_text("New branch name?")
        return

    if mode == "gh_branch_name":
        sess["answers"]["branch"] = text.strip()
        sessions[uid] = {"mode": "gh_branch_from", "answers": sess["answers"]}
        await update.message.reply_text("Create from which branch? (default: main)")
        return

    if mode == "gh_branch_from":
        from_branch = text.strip() or "main"
        repo   = sess["answers"]["repo"]
        branch = sess["answers"]["branch"]
        r      = github_agent.create_branch(repo, branch, from_branch)
        sessions.pop(uid, None)
        await update.message.reply_text(
            f"Branch '{branch}' {r.get('status')} in {repo}" if "error" not in r
            else f"Error: {r['error']}"
        )
        return

    if mode == "gh_branches_repo":
        repo = text.strip()
        sessions.pop(uid, None)
        r    = github_agent.list_branches(repo)
        branches = r.get("branches", [])
        await update.message.reply_text(
            f"Branches in {repo}:\n" + "\n".join(f"  {b}" for b in branches)
            if branches else f"No branches found or error: {r.get('error')}"
        )
        return

    # PR
    if mode == "gh_pr_repo":
        sess["answers"]["repo"] = text.strip()
        sessions[uid] = {"mode": "gh_pr_from", "answers": sess["answers"]}
        await update.message.reply_text("From branch?")
        return

    if mode == "gh_pr_from":
        sess["answers"]["from"] = text.strip()
        sessions[uid] = {"mode": "gh_pr_to", "answers": sess["answers"]}
        await update.message.reply_text("To branch?")
        return

    if mode == "gh_pr_to":
        sess["answers"]["to"] = text.strip()
        sessions[uid] = {"mode": "gh_pr_title", "answers": sess["answers"]}
        await update.message.reply_text("PR title? (or press enter for default)")
        return

    if mode == "gh_pr_title":
        title = text.strip() or None
        r     = github_agent.create_pull_request(
            sess["answers"]["repo"],
            sess["answers"]["from"],
            sess["answers"]["to"],
            title=title,
        )
        sessions.pop(uid, None)
        if r.get("status") in ("created", "exists"):
            await update.message.reply_text(f"PR {r['status']}: {r['url']}")
        else:
            await update.message.reply_text(f"Error: {r.get('error')}")
        return

    # Merge
    if mode == "gh_merge_repo":
        sess["answers"]["repo"] = text.strip()
        sessions[uid] = {"mode": "gh_merge_from", "answers": sess["answers"]}
        await update.message.reply_text("Merge FROM which branch?")
        return

    if mode == "gh_merge_from":
        sess["answers"]["from"] = text.strip()
        sessions[uid] = {"mode": "gh_merge_to", "answers": sess["answers"]}
        await update.message.reply_text("Merge INTO which branch?")
        return

    if mode == "gh_merge_to":
        r = github_agent.merge_branch(
            sess["answers"]["repo"],
            sess["answers"]["from"],
            text.strip(),
        )
        sessions.pop(uid, None)
        await update.message.reply_text(
            f"Merged {sess['answers']['from']} → {text.strip()}" if r.get("status") in ("merged","nothing_to_merge")
            else f"Error: {r.get('error')}"
        )
        return

    if mode == "gh_push_repo":
        sess["answers"]["repo"] = text.strip()
        sessions[uid] = {"mode": "gh_push_file", "answers": sess["answers"]}
        await update.message.reply_text("File path? (e.g. html/index.html)")
        return

    if mode == "gh_push_file":
        sess["answers"]["file"] = text.strip()
        sessions[uid] = {"mode": "gh_push_content", "answers": sess["answers"]}
        await update.message.reply_text(f"Paste new content for {text.strip()}:")
        return

    if mode == "gh_push_content":
        repo = sess["answers"]["repo"]
        file = sess["answers"]["file"]
        r    = github_agent.push_single_file(repo, file, text, f"Update: {file}")
        sessions.pop(uid, None)
        pushed = r.get("pushed", [])
        failed = r.get("failed", [])
        if pushed:
            await update.message.reply_text(f"Pushed {file} to {repo}\nTrigger pipeline? (yes/no)")
            sessions[uid] = {"mode": "gh_push_trigger", "answers": {"repo": repo}}
        else:
            await update.message.reply_text(f"Failed: {failed}")
        return

    if mode == "gh_push_trigger":
        if text.strip().lower() in ("yes", "y"):
            repo = sess["answers"]["repo"]
            r    = github_agent.trigger_pipeline(repo, "deploy.yml")
            await update.message.reply_text(f"Pipeline triggered: {r.get('url', r.get('error',''))}")
        else:
            await update.message.reply_text("Done — pipeline not triggered.")
        sessions.pop(uid, None)
        return

    if mode == "gh_pull_repo":
        sess["answers"]["repo"] = text.strip()
        sessions[uid] = {"mode": "gh_pull_file", "answers": sess["answers"]}
        await update.message.reply_text("File path?")
        return

    if mode == "gh_pull_file":
        repo  = sess["answers"]["repo"]
        file  = text.strip()
        files = github_agent.get_existing_files(repo)
        cnt   = files.get(file, "")
        sessions.pop(uid, None)
        if cnt:
            await _send_long(update, f"```\n{cnt[:3500]}\n```")
        else:
            await update.message.reply_text(f"Not found: {file}\nFiles: {list(files.keys())[:10]}")
        return

    if mode == "gh_trigger_repo":
        repo = text.strip()
        sessions.pop(uid, None)
        r = github_agent.trigger_pipeline(repo, "deploy.yml")
        await update.message.reply_text(f"Pipeline triggered: {r.get('url', r.get('error',''))}")
        return

    if mode == "gh_status_repo":
        repo = text.strip()
        sessions.pop(uid, None)
        r = github_agent.get_pipeline_status(repo)
        await update.message.reply_text(f"Status: {r.get('status')}\nConclusion: {r.get('conclusion','...')}\n{r.get('run_url','')}")
        return

    if mode == "gh_logs_repo":
        repo   = text.strip()
        sessions.pop(uid, None)
        result = github_agent.get_pipeline_status(repo)
        for job in result.get("failed_jobs", [])[:2]:
            await _send_long(update, f"=== {job['name']} ===\n{job.get('log','')[-2000:]}")
        if not result.get("failed_jobs"):
            await update.message.reply_text("No failed jobs")
        return

    if mode == "gh_files_repo":
        repo  = text.strip()
        sessions.pop(uid, None)
        files = github_agent.get_existing_files(repo)
        lines = list(files.keys())
        await update.message.reply_text(f"Files in {repo}:\n" + "\n".join(f"  {f}" for f in lines) if lines else "Empty repo")
        return

    if mode == "gh_create_repo":
        repo = text.strip()
        sessions.pop(uid, None)
        r = github_agent.handle("create_repo", {"name": repo})
        await update.message.reply_text(f"{r.get('status')} — {r.get('url', r.get('error',''))}")
        return

    if mode == "gh_delete_repo":
        repo = text.strip()
        sessions.pop(uid, None)
        r = github_agent.handle("delete_repo", {"name": repo})
        await update.message.reply_text(f"Delete: {r.get('status')}")
        return

    if mode == "post_deploy_pr":
        if text.strip().lower() in ("yes", "y"):
            sessions[uid] = {"mode": "post_deploy_pr_target", "answers": sess["answers"]}
            await update.message.reply_text("Merge into which branch? (e.g. main)")
        else:
            sessions.pop(uid, None)
            await update.message.reply_text("Done. No PR created.")
        return

    if mode == "post_deploy_pr_target":
        to_branch = text.strip()
        repo      = sess["answers"]["repo"]
        from_b    = sess["answers"]["branch"]
        r         = github_agent.create_pull_request(repo, from_b, to_branch)
        sessions.pop(uid, None)
        if r.get("status") in ("created", "exists"):
            await update.message.reply_text(f"PR created: {r['url']}")
        else:
            await update.message.reply_text(f"PR error: {r.get('error')}")
        return

    if mode == "github_push":
        r = github_agent.handle("push_file", {"repo": sess["repo"], "path": sess["file"], "content": text})
        sessions.pop(uid, None)
        await update.message.reply_text(f"Pushed: {r.get('pushed', r.get('error'))}")
        return

    if mode == "push_approval":
        if text.lower() in ("yes","y"):
            repo = sess["answers"].get("repo") or sess["answers"].get("project","")
            r    = github_agent.handle("push_file", {"repo": repo, "path": sess["file"], "content": sess["content"]})
            await update.message.reply_text(f"Pushed: {r.get('pushed', r.get('error'))}")
        else:
            await update.message.reply_text("Cancelled.")
        sessions.pop(uid, None)
        return

    if mode == "fix_approval":
        if text.lower() in ("yes","y","ok"):
            await _apply_fix(update, uid, sess)
        else:
            sessions.pop(uid, None)
            await update.message.reply_text("Fix cancelled.")
        return

    if mode == "add_skill_name":
        sessions[uid] = {"mode": "add_skill", "skill_name": text.strip()}
        await update.message.reply_text(f"Paste skill content for '{text.strip()}':")
        return

    if mode == "add_skill":
        add_skill(sess["skill_name"], text)
        sessions.pop(uid, None)
        await update.message.reply_text(f"Skill '{sess['skill_name']}' saved.")
        return

    if mode == "code_response":
        match = re.search(r"push(?:\s+it)?\s+to\s+([\w/-]+)", text, re.IGNORECASE)
        if match:
            repo = match.group(1)
            file = sess.get("last_file", "output.txt")
            github_agent.handle("push_file", {"repo": repo, "path": file, "content": sess["last_code"]})
            sessions.pop(uid, None)
            await update.message.reply_text(f"Pushed to {repo}/{file}")
        return

    if mode == "confirm_nuke_s3":
        if text.strip().upper() == "YES":
            sessions.pop(uid, None)
            await update.message.reply_text("💣 Nuking S3 bucket...")
            r = aws_agent.nuke_s3_bucket()
            if "error" in r:
                await update.message.reply_text(f"❌ {r['error']}")
            else:
                await update.message.reply_text(
                    f"✅ Done\n"
                    f"Bucket `{r['bucket']}` deleted\n"
                    f"Objects removed: {r.get('objects_deleted', 0)}"
                )
        else:
            sessions.pop(uid, None)
            await update.message.reply_text("Cancelled.")
        return

    if mode == "confirm_deploy":
        if text.lower() in ("yes","y","ok","go","proceed"):
            asyncio.create_task(_run_deploy(update, uid, sess["answers"]))
        else:
            sessions.pop(uid, None)
            await update.message.reply_text("Cancelled.")
        return

    if mode == "collect":
        missing  = sess.get("missing", [])
        answers  = sess.get("answers", {})
        if missing:
            key = missing[0]
            val = text.strip().lower() if key != "repo" else text.strip()
            
            # Repos names can't have spaces
            if key == "repo" and " " in val:
                suggested_name = val.replace(" ", "-").lower()
                await update.message.reply_text(
                    f"⚠️ Repository names cannot contain spaces.\n\n"
                    f"Did you mean: `{suggested_name}`?\n\n"
                    f"Please enter a valid repository name without spaces.",
                    parse_mode="Markdown"
                )
                return

            # Normalize target answer
            if key == "target":
                val = TARGET_ALIASES.get(val, val)
                if val not in ("ec2", "ec2-docker", "ecs"):
                    await update.message.reply_text(
                        "Please choose:\n  ec2 — directly on EC2\n  ec2-docker — Docker on EC2\n  ecs — Amazon ECS Fargate"
                    )
                    return
            
            answers[key] = val
            missing.pop(0)
            sess["missing"]  = missing
            sess["answers"]  = answers

            # Node.js Pre-flight Check Interception
            if key == "repo" and answers.get("app") == "node":
                await update.message.reply_text(f"⏳ Checking Node.js repository `{val}`...")
                # We pause collection mode and transfer to an async task
                sessions[uid]["mode"] = "node_preflight_wait"
                asyncio.create_task(_check_node_repo(update, uid, answers, missing))
                return

            if missing:
                await _ask_field_with_buttons(update, uid, missing[0])
            else:
                answers["repo_name"] = answers.get("repo")
                await _show_confirm(update, uid, answers)
        return

    # ── Update flow: repo → file browser → content → push + trigger ────────────────
    if mode == "update_repo":
        repo = text.strip()
        sessions[uid] = {"mode": "update_branch", "answers": {"repo": repo}}
        
        try:
            branches_result = github_agent.list_branches(repo)
            available_branches = branches_result.get("branches", [])
            
            if available_branches:
                keyboard = []
                # Layout branches in a 2-column grid
                for i in range(0, len(available_branches), 2):
                    row = [InlineKeyboardButton(b, callback_data=f"upd_br|{b}") for b in available_branches[i:i+2]]
                    keyboard.append(row)
                    
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text("Which branch?", reply_markup=reply_markup)
            else:
                await update.message.reply_text(f"⚠️ Could not find any branches for `{repo}`.\nPlease enter a valid branch name manually:", parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Failed to fetch branches for {repo} in UI prompt: {e}")
            await update.message.reply_text("Which branch? (e.g. main, dev)")
        return

    if mode == "update_branch":
        branch = text.strip()
        repo = sess["answers"]["repo"]
        
        # Validate that the branch actually exists in the repo
        try:
            branches_result = github_agent.list_branches(repo)
            available_branches = branches_result.get("branches", [])
            
            if branch not in available_branches:
                branch_list = "\n".join([f"• `{b}`" for b in available_branches]) if available_branches else "None found."
                await update.message.reply_text(
                    f"⚠️ Branch `{branch}` not found in repository `{repo}`.\n\n"
                    f"**Available branches:**\n{branch_list}\n\n"
                    f"Please enter a valid branch name:",
                    parse_mode="Markdown"
                )
                return
        except Exception as e:
            logger.warning(f"Failed to fetch branches for {repo}: {e}")
            # If we can't fetch branches for some reason, we'll just allow it to proceed and fail later
            pass

        sessions[uid]["answers"]["branch"] = branch
        sessions[uid] = {"mode": "update_browser", "answers": {"repo": repo, "branch": branch}}
        await _show_repo_browser(update, uid, repo, "", branch=branch)
        return

    if mode == "update_new_filename":
        file_name = text.strip()
        dir_path = sess["answers"].get("dir", "")
        full_path = f"{dir_path}/{file_name}" if dir_path else file_name
        
        sessions[uid]["mode"] = "update_content"
        sessions[uid]["answers"]["file"] = full_path
        await update.message.reply_text(
            f"Got it. New file will be `{full_path}`.\n"
            f"Please **paste the text content** here,\n"
            f"OR **upload a file** (Document) to populate it.",
            parse_mode="Markdown"
        )
        return

    if mode == "update_content":
        sess["answers"]["content"] = text
        sessions[uid]["mode"] = "update_deploy_ask"
        repo = sess["answers"]["repo"]
        target_path = sess["answers"]["file"]
        await update.message.reply_text(f"📤 Pushing `{target_path}` to `{repo}`...")
        asyncio.create_task(_run_push_and_ask_deploy(update, uid, sess["answers"]))
        return

    # ── Interactive Node.js Project Generator ─────────────────────────────────
    if mode == "init_node_repo":
        repo_name = text.strip()
        sessions[uid]["answers"]["repo"] = repo_name
        sessions[uid]["mode"] = "init_node_db_choice"
        
        keyboard = [
            [InlineKeyboardButton("MongoDB", callback_data="init_db|mongodb"),
             InlineKeyboardButton("PostgreSQL", callback_data="init_db|postgres")],
            [InlineKeyboardButton("MySQL", callback_data="init_db|mysql"),
             InlineKeyboardButton("None", callback_data="init_db|none")]
        ]
        
        await update.message.reply_text(
            f"Got it. Repo name: `{repo_name}`.\n\n"
            f"Do you need a Database setup for this project?\n"
            f"Select an option below or type your choice (e.g., 'mongodb', 'postgres', 'mysql', or 'none').",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    if mode == "init_node_db_choice":
        db_choice = text.strip().lower()
        sessions[uid]["answers"]["db"] = db_choice
        sessions[uid]["mode"] = "init_node_frontend_choice"
        
        keyboard = [
            [InlineKeyboardButton("⚛️ React", callback_data="init_fe|react"),
             InlineKeyboardButton("▲ Next.js", callback_data="init_fe|next.js")],
            [InlineKeyboardButton("🟢 Vue", callback_data="init_fe|vue.js"),
             InlineKeyboardButton("🅰️ Angular", callback_data="init_fe|angular")],
            [InlineKeyboardButton("🔥 Svelte", callback_data="init_fe|svelte"),
             InlineKeyboardButton("JS Vanilla", callback_data="init_fe|javascript")],
            [InlineKeyboardButton("🌐 HTML Only", callback_data="init_fe|html"),
             InlineKeyboardButton("❌ None", callback_data="init_fe|none")]
        ]
        
        await update.message.reply_text(
            f"Database: `{db_choice}`. ✅\n\n"
            f"Do you need a Frontend included?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return

    if mode == "init_node_frontend_choice":
        frontend_choice = text.strip().lower()
        sessions[uid]["answers"]["frontend"] = frontend_choice
        
        answers = sessions[uid]["answers"]
        repo = answers["repo"]
        db = answers["db"]
        frontend = answers["frontend"]
        
        await update.message.reply_text(
            f"Great! I am now setting up your full stack Node.js project:\n"
            f"📦 Repo: `{repo}`\n"
            f"🗄️ Database: `{db}`\n"
            f"🖥️ Frontend: `{frontend}`\n\n"
            f"⏳ Creating GitHub repository and generating files...",
            parse_mode="Markdown"
        )
        
        asyncio.create_task(_run_initnode_generation(update, uid, answers))
        sessions.pop(uid, None)
        return
    # ── Trigger flow ──────────────────────────────────────────────────────────
    if mode == "collect_node_secrets":
        env_trigger = text.strip().lower()
        repo = sess["answers"]["repo"]
        missing = sess["missing"]
        answers = sess["answers"]
        db = answers.get("db", "none")
        
        if env_trigger in ("no", "skip", "n"):
            await update.message.reply_text("⏭️ Skipping environment variables...")
            sessions[uid]["mode"] = "collect"
            if missing:
                await _ask_field_with_buttons(update, uid, missing[0])
            else:
                answers["repo_name"] = answers.get("repo")
                await _show_confirm(update, uid, answers)
            return

        # Pre-seed the queue with DB-specific defaults
        db_defaults = DB_ENV_DEFAULTS.get(db, [])
        env_queue = list(db_defaults)  # list of (key, default_value)
        env_vars = {}
        
        # Pre-apply DB defaults automatically (user will confirm/override)
        for k, v in db_defaults:
            env_vars[k] = v

        sessions[uid]["mode"] = "collect_node_env_confirm"
        sessions[uid]["env_vars"] = env_vars
        sessions[uid]["env_queue"] = env_queue   # remaining fields to confirm
        sessions[uid]["env_queue_idx"] = 0       # current index in queue

        if env_queue:
            key, default = env_queue[0]
            default_hint = f"`{default}`" if default else "_empty_"
            await update.message.reply_text(
                f"📋 *Setting up environment variables*\n\n"
                f"I'll walk you through each variable one by one.\n"
                f"Default values are pre-filled — just type a new value to override, or type `ok` / leave blank to keep the default.\n\n"
                f"─────────────────\n"
                f"🔑 *{key}*\n"
                f"Default: {default_hint}\n\n"
                f"Type new value or `ok` to keep default:",
                parse_mode="Markdown"
            )
        else:
            # No DB defaults — go straight to manual entry
            sessions[uid]["mode"] = "collect_node_env_key"
            await update.message.reply_text(
                "🔑 Enter the *name* of your first environment variable\n"
                "(e.g. `DATABASE_URL`)\n\n"
                "Type `done` when finished.",
                parse_mode="Markdown"
            )
        return

    if mode == "collect_node_env_confirm":
        # Walking through DB default fields, asking user to confirm/override
        env_queue = sess.get("env_queue", [])
        idx = sess.get("env_queue_idx", 0)
        env_vars = sess.get("env_vars", {})
        repo = sess["answers"]["repo"]
        
        if idx < len(env_queue):
            key, default = env_queue[idx]
            user_val = text.strip()
            
            # Accept "ok", empty, or actual value
            if user_val.lower() in ("ok", "", "keep", "yes", "y"):
                final_val = default  # keep default
            else:
                final_val = user_val  # user override
            
            env_vars[key] = final_val
            sessions[uid]["env_vars"] = env_vars
            sessions[uid]["env_queue_idx"] = idx + 1
            
            next_idx = idx + 1
            if next_idx < len(env_queue):
                # Show next field
                next_key, next_default = env_queue[next_idx]
                next_hint = f"`{next_default}`" if next_default else "_empty_"
                count = len(env_vars)
                await update.message.reply_text(
                    f"✅ `{key}` = `{final_val}`\n\n"
                    f"─────────────────\n"
                    f"🔑 *{next_key}*  ({next_idx + 1}/{len(env_queue)})\n"
                    f"Default: {next_hint}\n\n"
                    f"Type new value or `ok` to keep default:",
                    parse_mode="Markdown"
                )
                return
            else:
                # All DB defaults confirmed — now offer custom vars
                count = len(env_vars)
                await update.message.reply_text(
                    f"✅ `{key}` = `{final_val}`\n\n"
                    f"🎉 All {count} database variables confirmed!\n\n"
                    f"─────────────────\n"
                    f"➕ Would you like to add more custom variables?\n"
                    f"Enter the *name* of the next variable, or type `done` to finish.",
                    parse_mode="Markdown"
                )
                sessions[uid]["mode"] = "collect_node_env_key"
                return

    if mode == "collect_node_env_key":
        key_name = text.strip()
        repo = sess["answers"]["repo"]
        
        if key_name.lower() == "done":
            # All vars collected — push & continue
            env_vars = sess.get("env_vars", {})
            missing = sess["missing"]
            answers = sess["answers"]
            
            if env_vars:
                await update.message.reply_text("🔐 Uploading secrets to GitHub repository...")
                res = github_agent.handle("set_secrets", {"repo": repo, "secrets": env_vars})
                if res.get("status") == "ok":
                    await update.message.reply_text(f"✅ {len(env_vars)} secret(s) securely saved to GitHub!")
                else:
                    await update.message.reply_text(f"⚠️ Warning: Failed to save secrets: {res.get('error')}")
            else:
                await update.message.reply_text("⏭️ No secrets collected, continuing...")
            
            # Resume deploy flow
            analysis = sess.get("analysis_state", {})
            if not analysis.get("ready"):
                errors = analysis.get("errors", "Unknown issues")
                fix_files = analysis.get("fix_files", {})
                msg = (
                    f"❌ **Project NOT Ready for Deployment**\n\n"
                    f"**Issues Found by AI:**\n{errors}\n"
                )
                keyboard = []
                if fix_files:
                    msg += "\n✨ I have automatically generated fixes for these files!"
                    keyboard.append([InlineKeyboardButton("🤖 Apply AI Fix & Continue", callback_data=f"nodefix_ai|{uid}")])
                keyboard.append([InlineKeyboardButton("🛑 Cancel & Fix Manually", callback_data=f"nodefix_man|{uid}")])
                keyboard.append([InlineKeyboardButton("⚠️ Ignore & Deploy Anyway", callback_data=f"nodefix_ign|{uid}")])
                sessions[uid] = {"mode": "node_fix_decision", "answers": answers, "missing": missing, "fix_files": fix_files, "repo": repo}
                await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
            else:
                await update.message.reply_text("✅ AI verified: Project structure looks perfectly ready for deployment!")
                sessions[uid]["mode"] = "collect"
                if missing:
                    await _ask_field_with_buttons(update, uid, missing[0])
                else:
                    answers["repo_name"] = answers.get("repo")
                    await _show_confirm(update, uid, answers)
            return
        
        # Store key name, ask for value
        sessions[uid]["pending_env_key"] = key_name
        sessions[uid]["mode"] = "collect_node_env_val"
        await update.message.reply_text(
            f"🔒 Value for `{key_name}`?\n(Enter the value, then I'll ask for the next variable.)",
            parse_mode="Markdown"
        )
        return

    if mode == "collect_node_env_val":
        key_name = sess.get("pending_env_key", "UNKNOWN")
        value = text.strip()
        repo = sess["answers"]["repo"]
        
        sessions[uid]["env_vars"][key_name] = value
        sessions[uid]["mode"] = "collect_node_env_key"
        sessions[uid].pop("pending_env_key", None)
        
        count = len(sessions[uid]["env_vars"])
        await update.message.reply_text(
            f"✅ `{key_name}` saved! ({count} var{'' if count == 1 else 's'} collected so far)\n\n"
            f"Enter next variable *name*, or type `done` to finish.",
            parse_mode="Markdown"
        )
        return

    # ── Trigger flow ──────────────────────────────────────────────────────────
    if mode == "trigger_repo":
        repo = text.strip()
        sessions.pop(uid, None)
        await update.message.reply_text(f"Triggering deploy.yml in {repo}...")
        r = github_agent.handle("trigger", {"repo": repo, "workflow": "deploy.yml"})
        await update.message.reply_text(f"{r.get('status')} — {r.get('url', r.get('error',''))}")
        return

    # ── Destroy flow ──────────────────────────────────────────────────────────
    if mode == "destroy_project":
        project = text.strip()
        # Show available branches so user can pick the right one
        dep  = state.get_deployment(project) or {}
        repo = dep.get("repo", project)
        try:
            branches_result = github_agent.list_branches(repo)
            branches = [b for b in branches_result.get("branches", []) if b != "main"]
        except Exception:
            branches = []

        sessions[uid] = {"mode": "destroy_branch", "answers": {"project": project, "repo": repo}}
        
        keyboard = [[InlineKeyboardButton(b, callback_data=f"dst_br|{b[:50]}")] for b in branches]
        keyboard.append([InlineKeyboardButton("main", callback_data="dst_br|main")])
        await update.message.reply_text(
            f"Which branch to destroy for '{project}'?\n",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if mode == "destroy_branch":
        answers = sess["answers"]
        answers["branch"] = text.strip()
        sessions[uid] = {"mode": "destroy_confirm", "answers": answers}
        
        keyboard = [
            [InlineKeyboardButton("💣 Yes, Destroy AWS Only", callback_data="dst_conf|yes")],
            [InlineKeyboardButton("🌿 Yes, Destroy AWS + Branch", callback_data="dst_conf|yes+branch")],
            [InlineKeyboardButton("🔥 Yes, Destroy AWS + Entire Repo", callback_data="dst_conf|yes+repo")],
            [InlineKeyboardButton("❌ Cancel", callback_data="dst_conf|no")]
        ]
        await update.message.reply_text(
            f"Destroy '{answers['project']}' on branch '{answers['branch']}'?\n"
            f"Select an option below:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    if mode == "destroy_confirm":
        answers = sess["answers"]
        answers["del_repo"] = "yes" if "repo" in text.lower() else "no"
        answers["del_branch"] = "yes" if "branch" in text.lower() else "no"
        if text.lower().startswith("yes"):
            sessions[uid] = {"mode": "destroy_final_confirm", "answers": answers}
            target_str = "AWS Infrastructure"
            if answers["del_repo"] == "yes":
                target_str += " AND Entire GitHub Repository"
            elif answers["del_branch"] == "yes":
                target_str += f" AND GitHub Branch '{answers.get('branch', 'unknown')}'"
            
            await update.message.reply_text(
                f"⚠️ Are you sure? This action is irreversible.\n"
                f"You are about to permanently delete:\n"
                f"👉 **{target_str}** for `{answers.get('project', 'unknown')}`\n\n"
                f"If you are absolutely sure, please type `delete` to confirm.", 
                parse_mode="Markdown"
            )
        else:
            sessions.pop(uid, None)
            await update.message.reply_text("Cancelled.")
        return

    if mode == "destroy_final_confirm":
        if text.lower() == "delete":
            answers = sess["answers"]
            asyncio.create_task(_run_destroy(update, uid, answers))
        else:
            sessions.pop(uid, None)
            await update.message.reply_text("Cancelled. You didn't type 'delete'.")
        return

    # ── Natural language ──────────────────────────────────────────────────────
    intent = extract_intent(text)



    if intent.get("intent") == "update":
        repo    = intent.get("repo_name")
        file_   = intent.get("file")
        branch  = intent.get("branch", "main")
        if repo and file_:
            sessions[uid] = {"mode": "update_content", "answers": {"repo": repo, "file": file_, "branch": branch}}
            await update.message.reply_text(
                f"📄 Updating: `{file_}` in `{repo}`\n\n"
                f"Please **paste the new text** here,\n"
                f"OR **upload a file** (Document) to replace it.",
                parse_mode="Markdown"
            )
        elif repo:
            sessions[uid] = {"mode": "update_browser", "answers": {"repo": repo, "branch": branch}}
            await _show_repo_browser(update, uid, repo, "")
        else:
            sessions[uid] = {"mode": "update_repo", "answers": {}}
            await update.message.reply_text("Repo name? (e.g. my-repo)")
        return

    if intent.get("intent") == "trigger":
        repo   = intent.get("repo_name") or intent.get("project")
        branch = intent.get("branch", "main")
        if repo:
            await update.message.reply_text(f"Triggering pipeline in {repo} on branch {branch}...")
            r = github_agent.trigger_pipeline(repo, "deploy.yml", branch)
            await update.message.reply_text(f"{r.get('status')} — {r.get('url', r.get('error',''))}")
        else:
            sessions[uid] = {"mode": "trigger_repo", "answers": {}}
            await update.message.reply_text("Repo name?")
        return

    if intent.get("intent") == "deploy":
        raw_target = intent.get("target", "")
        # If target is ambiguous (user said docker/container) — force ask
        target = None if raw_target == "ask" else (raw_target or None)
        answers = {
            "project": intent.get("project"),
            "app":     intent.get("app"),
            "target":  target,
            "repo":    intent.get("repo_name") or intent.get("project"),
            "branch":  intent.get("branch"),
            "region":  intent.get("region"),
        }
        # Always ask all missing fields
        missing = [f for f in ["project", "app", "target", "repo", "branch", "region"] if not answers.get(f)]
        if missing:
            sessions[uid] = {"mode": "collect", "answers": answers, "missing": missing}
            await update.message.reply_text(FIELD_QUESTIONS.get(missing[0], f"{missing[0]}?"))
        else:
            answers["repo_name"] = answers["repo"]
            await _show_confirm(update, uid, answers)
        return

    if intent.get("intent") == "destroy":
        project = intent.get("project")
        if project:
            sessions[uid] = {"mode": "destroy_confirm", "answers": {"project": project}}
            keyboard = [
                [InlineKeyboardButton("💣 Yes, Destroy AWS Only", callback_data="dst_conf|yes")],
                [InlineKeyboardButton("🌿 Yes, Destroy AWS + Branch", callback_data="dst_conf|yes+branch")],
                [InlineKeyboardButton("🔥 Yes, Destroy AWS + Entire Repo", callback_data="dst_conf|yes+repo")],
                [InlineKeyboardButton("❌ Cancel", callback_data="dst_conf|no")]
            ]
            await update.message.reply_text(
                f"Destroy '{project}'?\nSelect an option below:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            sessions[uid] = {"mode": "destroy_project", "answers": {}}
            await update.message.reply_text("Which project to destroy?")
        return

    await update.message.reply_text(
        "I didn't understand that. Try:\n"
        "  deploy nginx to aws\n"
        "  update html in repo my-repo\n"
        "  trigger pipeline in my-repo\n"
        "  destroy my-repo\n"
        "Or use /deploy /update /trigger /destroy"
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _show_confirm(update, uid, answers):
    sessions[uid] = {"mode": "confirm_deploy", "answers": answers}
    target_label = {
        "ec2":        "EC2 direct (no Docker)",
        "ec2-docker": "Docker on EC2",
        "ecs":        "Amazon ECS Fargate",
    }.get(answers.get("target","ec2"), answers.get("target","ec2"))
    
    keyboard = [
        [InlineKeyboardButton("✅ Yes", callback_data="dep_conf|yes"),
         InlineKeyboardButton("❌ No", callback_data="dep_conf|no")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Ready to deploy:\n"
        f"  Project: {answers.get('project')}\n"
        f"  App:     {answers.get('app')}\n"
        f"  Target:  {target_label}\n"
        f"  Repo:    {answers.get('repo_name') or answers.get('repo')}\n"
        f"  Branch:  {answers.get('branch','main')}\n"
        f"  Region:  {answers.get('region','us-east-1')}\n\n"
        f"Proceed?",
        reply_markup=reply_markup
    )

async def _send_long(update, text):
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i+4000])

async def _run_deploy(update, uid, answers):
    sessions[uid] = {"mode": "running", "project": answers.get("project"), "answers": answers}
    set_running(uid, True)

    async def cb(msg):
        try: await update.message.reply_text(msg)
        except Exception: pass

    try:
        result = await orchestrator.deploy(
            user_id     = uid,
            project     = answers["project"],
            app         = answers["app"],
            repo_name   = answers.get("repo_name") or answers.get("repo") or answers["project"],
            region      = answers.get("region", "us-east-1"),
            branch      = answers.get("branch", "main"),
            target      = answers.get("target", "ec2"),
            progress_cb = cb,
        )
        if result["status"] == "success":
            branch  = answers.get("branch", "main")
            project = answers["project"]
            app     = answers.get("app", "")
            target  = answers.get("target", "ec2")
            region  = answers.get("region", "us-east-1")
            repo    = answers.get("repo_name") or answers.get("repo") or project
            url     = result.get("url", "") or ("http://" + result.get("ip","")) or ""

            target_label = {
                "ec2":        "EC2 Direct",
                "ec2-docker": "Docker on EC2",
                "ecs":        "Amazon ECS Fargate",
            }.get(target, target)

            # ── SUCCESS BANNER ───────────────────────────────────────────
            banner = (
                "\n"
                "🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉\n"
                "✅  DEPLOYMENT SUCCESSFUL  ✅\n"
                "🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉\n"
                "\n"
                f"📦 Project : {project}\n"
                f"🚀 App     : {app}\n"
                f"🎯 Target  : {target_label}\n"
                f"🌿 Branch  : {branch}\n"
                f"🌍 Region  : {region}\n"
                f"🔗 Repo    : github.com/{github_agent.username}/{repo}\n"
                + (f"🌐 Live at : {url}\n" if url else "🌐 Live at : (check pipeline logs)\n")
            )
            await update.message.reply_text(banner)

            # ── AUTO UPDATE README ────────────────────────────────────────
            try:
                readme = _build_deployment_readme(
                    project=project, app=app, target=target,
                    target_label=target_label, branch=branch,
                    region=region, url=url, repo=repo,
                )
                push_result = github_agent.push_single_file(
                    repo, "README.md", readme,
                    f"docs: update README for {branch} deployment [{app} on {target}] [skip ci]",
                    branch=branch,
                )
                if not push_result.get("failed"):
                    await update.message.reply_text(f"📄 README updated on branch '{branch}'")
            except Exception as readme_err:
                await update.message.reply_text(f"⚠️ README update failed: {readme_err}")

            # ── POST DEPLOY PR PROMPT ─────────────────────────────────────
            if branch != "main":
                sessions[uid] = {"mode": "post_deploy_pr", "answers": {
                    "repo": repo, "branch": branch
                }}
                await update.message.reply_text(
                    f"Want to create a PR to merge '{branch}' into another branch? (yes/no)"
                )
                return

        else:
            # ── FAILURE BANNER ────────────────────────────────────────────
            status  = result.get("status", "failed")
            message = result.get("message", "")
            run_url = result.get("run_url", "")

            last_error = message
            # Try to extract just the key error line
            if "Last error:" in message:
                last_error = message.split("Last error:")[-1].strip()

            banner = (
                "\n"
                "💥💥💥💥💥💥💥💥💥💥💥💥💥💥💥\n"
                "❌   DEPLOYMENT FAILED   ❌\n"
                "💥💥💥💥💥💥💥💥💥💥💥💥💥💥💥\n"
                "\n"
                f"📦 Project : {answers.get('project','')}\n"
                f"🌿 Branch  : {answers.get('branch','main')}\n"
                f"🔴 Status  : {status}\n"
                "\n"
                f"💬 Reason:\n{last_error[:500]}\n"
                + (f"\n🔗 Logs: {run_url}" if run_url else "")
            )
            await update.message.reply_text(banner)

        sessions.pop(uid, None)
    except Exception as e:
        await update.message.reply_text(
            "\n"
            "💥💥💥💥💥💥💥💥💥💥💥💥💥💥💥\n"
            "❌   DEPLOYMENT FAILED   ❌\n"
            "💥💥💥💥💥💥💥💥💥💥💥💥💥💥💥\n"
            f"\n⚠️ Unexpected error:\n{str(e)[:400]}"
        )
        sessions.pop(uid, None)
    finally:
        set_running(uid, False)


async def _apply_fix(update, uid, sess):
    set_running(uid, True)
    fix = sess["fix"]; answers = sess["answers"]

    async def cb(msg):
        try: await update.message.reply_text(msg)
        except Exception: pass

    try:
        result = await orchestrator.apply_fix_and_retry(
            user_id=uid, project=answers["project"],
            repo_name=answers.get("repo_name") or answers.get("repo"),
            file_path=fix["file"], fixed_content=fix["fixed_content"],
            retry=fix.get("retry", 1), progress_cb=cb,
        )
        await update.message.reply_text(
            f"Fixed! URL: {result.get('url')}" if result["status"] == "success"
            else f"Still failing: {result.get('message', result['status'])}"
        )
        sessions.pop(uid, None)
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        sessions.pop(uid, None)
    finally:
        set_running(uid, False)


async def _run_push_and_ask_deploy(update, uid, answers):
    """Push file to repo and ask if user wants to trigger deploy."""
    set_running(uid, True)
    repo      = answers.get("repo", "")
    file_path = answers.get("file", "")
    content   = answers.get("content", "")

    async def cb(msg):
        try: await update.message.reply_text(msg)
        except Exception: pass

    try:
        branch = answers.get("branch", "main")
        push = github_agent.push_single_file(repo, file_path, content, f"Update: {file_path}", branch=branch)

        if push.get("failed"):
            await update.message.reply_text(f"❌ Push failed: {push['failed']}")
            return

        # Ask to trigger
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes, Deploy Now", callback_data=f"upd_go|{repo}|{branch}"),
                InlineKeyboardButton("⏭️ No, Skip", callback_data=f"upd_no|{repo}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"✅ Successfully pushed {file_path} to {repo}.\n\n"
            f"Would you like to trigger a live deployment now?",
            reply_markup=reply_markup
        )

        sessions.pop(uid, None)

    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        sessions.pop(uid, None)
    finally:
        set_running(uid, False)


async def _run_destroy(update, uid, answers):
    set_running(uid, True)

    async def cb(msg):
        try: await update.message.reply_text(msg)
        except Exception: pass

    try:
        dep  = state.get_deployment(answers["project"])
        repo = dep.get("repo", answers["project"]) if dep else answers["project"]
        result = await orchestrator.destroy(
            user_id=uid, project=answers["project"], repo_name=repo,
            branch=answers.get("branch", "main"),
            delete_repo=answers.get("del_repo","no").lower()=="yes", 
            delete_branch=answers.get("del_branch","no").lower()=="yes",
            progress_cb=cb,
        )
        project = answers["project"]
        if result.get("status") == "success":
            await update.message.reply_text(
                "\n"
                "🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥\n"
                "✅  DESTROY SUCCESSFUL  ✅\n"
                "🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥\n"
                "\n"
                f"📦 Project : {project}\n"
                f"🗑 All AWS resources destroyed\n"
                f"💬 {result.get('message', 'Done')}\n"
            )
        else:
            await update.message.reply_text(
                "\n"
                "💥💥💥💥💥💥💥💥💥💥💥💥💥💥💥\n"
                "❌   DESTROY FAILED   ❌\n"
                "💥💥💥💥💥💥💥💥💥💥💥💥💥💥💥\n"
                "\n"
                f"📦 Project : {project}\n"
                f"🔴 Status  : {result.get('status','failed')}\n"
                f"💬 Reason  : {result.get('message','')[:400]}\n"
            )
        sessions.pop(uid, None)
    except Exception as e:
        await update.message.reply_text(
            "\n"
            "💥💥💥💥💥💥💥💥💥💥💥💥💥💥💥\n"
            "❌   DESTROY FAILED   ❌\n"
            "💥💥💥💥💥💥💥💥💥💥💥💥💥💥💥\n"
            f"\n⚠️ Unexpected error:\n{str(e)[:400]}"
        )
        sessions.pop(uid, None)
    finally:
        set_running(uid, False)


async def _run_initnode_generation(update: Update, uid: int, answers: dict):
    set_running(uid, True)
    repo     = answers.get("repo")
    db       = answers.get("db", "none")
    frontend = answers.get("frontend", "none")
    async def cb(msg, **kwargs):
        try: await update.message.reply_text(msg, **kwargs)
        except Exception as e: logger.error(f"Failed to send cb msg: {e}")
    try:
        # Step 1: Create GitHub Repo
        await cb(f"⏳ 1/3 Creating GitHub repository `{repo}`...")
        r_repo = github_agent.handle("create_repo", {"name": repo})
        if r_repo.get("status") != "created" and r_repo.get("status") != "exists":
            await cb(f"❌ Failed to create repo: {r_repo.get('error')}")
            return
            
        await cb(f"✅ Repo ready! URL: {r_repo.get('url')}")

        # Step 2: Generate Project Files using Code Agent
        await cb(f"⏳ 2/3 Generating Node.js configuration (DB: {db}, Frontend: {frontend})...")
        # Ensure we're yielding to event loop for a heavy generation task
        r_gen = await asyncio.to_thread(
            code_agent.handle, 
            "gen_node_project", 
            {"project": repo, "db": db, "frontend": frontend}
        )

        if r_gen.get("status") != "ok":
            await cb(f"❌ Failed to generate project files: {r_gen.get('error')}")
            return
            
        files_to_push = r_gen.get("files", {})
        if not files_to_push:
            await cb(f"❌ AI did not return any files to push.")
            return

        # Step 3: Push all files to main branch
        await cb(f"⏳ 3/3 Pushing {len(files_to_push)} files to GitHub...")

        failed_pushes = []
        for file_path, file_content in files_to_push.items():
            push_res = github_agent.push_single_file(
                repo, file_path, file_content, f"Init generated {file_path}", branch="main"
            )
            if push_res.get("failed"):
                failed_pushes.append(file_path)

        if failed_pushes:
            await cb(f"⚠️ Some files failed to push: {', '.join(failed_pushes)}")
        else:
            await cb(
                f"🎉 **Project successfully generated and pushed!**\n\n"
                f"Repo: {r_repo.get('url')}\n"
                f"🚀 Continuing automatically to deployment...",
                parse_mode="Markdown"
            )

            # Auto-continue to deployment flow
            answers["app"] = "node"
            answers["branch"] = "main"
            answers["repo_name"] = repo
            
            # Determine missing deploy fields (e.g. if initiated from /initnode)
            missing_fields = [f for f in DEPLOY_FIELDS if f not in answers]
            
            if db != "none" or frontend != "none":
                env_keyboard = [
                    [InlineKeyboardButton("✅ Yes, set up .env now", callback_data="init_fe|yes"),
                     InlineKeyboardButton("❌ Skip, no .env needed", callback_data="init_fe|skip")]
                ]
                await cb(
                    f"🔐 **Environment Variables**\n\n"
                    f"Your project uses a Database (`{db}`) or Frontend (`{frontend}`).\n"
                    f"Would you like to set up environment variables now?",
                    reply_markup=InlineKeyboardMarkup(env_keyboard),
                    parse_mode="Markdown"
                )
                sessions[uid] = {
                    "mode": "collect_node_secrets",
                    "answers": answers,
                    "missing": missing_fields,
                    "analysis_state": {"ready": True} # Auto-pass
                }
            else:
                sessions[uid] = {
                    "mode": "collect",
                    "answers": answers,
                    "missing": missing_fields
                }
                if missing_fields:
                    await _ask_field_with_buttons(update, uid, missing_fields[0])
                else:
                    await _show_confirm(update, uid, answers)

    except Exception as e:
        logger.error(f"Error in initnode generation: {e}")
        await cb(f"❌ Unexpected error during project setup:\n{str(e)[:400]}")
    finally:
        set_running(uid, False)


async def _check_node_repo(update: Update, uid: int, answers: dict, missing: list):
    set_running(uid, True)
    repo = answers["repo"]

    async def cb(msg, **kwargs):
        try: await update.message.reply_text(msg, **kwargs)
        except Exception as e: logger.error(f"Failed to send cb msg: {e}")

    try:
        # Check if repo exists and get files
        await cb(f"🔍 Analyzing repository `{repo}`...")
        
        # We need to see if it even exists
        res_files = github_agent.get_existing_files(repo)
        
        if res_files.get("status") == "error":
            # Assume repo doesn't exist or is empty
            await cb(
                f"⚠️ Repository `{repo}` not found or is empty.\n\n"
                f"Let's create a new full-stack Node.js project for you automatically!",
                parse_mode="Markdown"
            )
            sessions[uid] = {
                "mode": "init_node_db_choice",
                "answers": answers
            }
            
            keyboard = [
                [InlineKeyboardButton("MongoDB", callback_data="init_db|mongodb"),
                 InlineKeyboardButton("PostgreSQL", callback_data="init_db|postgres")],
                [InlineKeyboardButton("MySQL", callback_data="init_db|mysql"),
                 InlineKeyboardButton("None", callback_data="init_db|none")]
            ]
            await cb(
                f"Do you need a Database setup for this project?\n"
                f"Select an option below or type your choice.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        # Repo exists, let's analyze it with AI
        files_data = res_files.get("files", {})
        if not files_data:
            await cb("⚠️ Repository is empty! Transitioning to Code Generator...")
            sessions[uid] = {"mode": "init_node_db_choice", "answers": answers}
            keyboard = [
                [InlineKeyboardButton("MongoDB", callback_data="init_db|mongodb"),
                 InlineKeyboardButton("PostgreSQL", callback_data="init_db|postgres")],
                [InlineKeyboardButton("MySQL", callback_data="init_db|mysql"),
                 InlineKeyboardButton("None", callback_data="init_db|none")]
            ]
            await cb(
                "Do you need a Database setup for this project?",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return

        await cb(f"🤖 AI is scanning {len(files_data)} files to ensure Node.js deployment readiness...")
        
        # Call the code agent analysis
        analysis_res = await asyncio.to_thread(
            code_agent.handle, 
            "analyze_node_repo", 
            {"project": repo, "files": files_data}
        )
        
        if analysis_res.get("status") != "ok":
            await cb(f"⚠️ AI analysis failed: {analysis_res.get('error')}. We will attempt to deploy anyway.")
            # Resume normal collect
            sessions[uid] = {"mode": "collect", "answers": answers, "missing": missing}
            if missing:
                await _ask_field_with_buttons(update, uid, missing[0])
            else:
                answers["repo_name"] = answers.get("repo")
                await _show_confirm(update, uid, answers)
            return
            
        analysis = analysis_res.get("analysis", {})
        
        missing_env = analysis.get("missing_env", [])
        if missing_env:
            # We need to collect secrets from the user
            env_str = ", ".join(f"`{e}`" for e in missing_env)
            env_keyboard = [
                [InlineKeyboardButton("✅ Yes, set up .env now", callback_data="init_fe|yes"),
                 InlineKeyboardButton("❌ Skip for now", callback_data="init_fe|skip")]
            ]
            await cb(
                f"🔐 **Environment Variables Required**\n\n"
                f"This project needs: {env_str}\n\n"
                f"Would you like to set them up now?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(env_keyboard)
            )
            sessions[uid] = {
                "mode": "collect_node_secrets",
                "answers": answers,
                "missing": missing,
                "analysis_state": analysis
            }
            return

        # If it's not ready and has errors
        if not analysis.get("ready"):
            errors = analysis.get("errors", "Unknown issues")
            fix_files = analysis.get("fix_files", {})
            
            msg = (
                f"❌ **Project NOT Ready for Deployment**\n\n"
                f"**Issues Found by AI:**\n{errors}\n"
            )
            
            keyboard = []
            if fix_files:
                msg += "\n✨ I have automatically generated fixes for these files!"
                keyboard.append([InlineKeyboardButton("🤖 Apply AI Fix & Continue", callback_data=f"nodefix_ai|{uid}")])
                
            keyboard.append([InlineKeyboardButton("🛑 Cancel & Fix Manually", callback_data=f"nodefix_man|{uid}")])
            keyboard.append([InlineKeyboardButton("⚠️ Ignore & Deploy Anyway", callback_data=f"nodefix_ign|{uid}")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            sessions[uid] = {
                "mode": "node_fix_decision",
                "answers": answers,
                "missing": missing,
                "fix_files": fix_files,
                "repo": repo
            }
            
            await cb(msg, reply_markup=reply_markup, parse_mode="Markdown")
            return

        # Everything is perfect! Resume normal collect flow
        await cb("✅ AI verified: Project structure looks perfectly ready for deployment!")
        sessions[uid] = {"mode": "collect", "answers": answers, "missing": missing}
        if missing:
            await _ask_field_with_buttons(update, uid, missing[0])
        else:
            answers["repo_name"] = answers.get("repo")
            await _show_confirm(update, uid, answers)

    except Exception as e:
        logger.error(f"Error checking node repo: {e}")
        await cb(f"❌ Verification error:\n{str(e)[:400]}\nResuming deployment...")
        # Fallback to deploy
        sessions[uid] = {"mode": "collect", "answers": answers, "missing": missing}
        if missing:
            await _ask_field_with_buttons(update, uid, missing[0])
        else:
            answers["repo_name"] = answers.get("repo")
            await _show_confirm(update, uid, answers)
    finally:
        set_running(uid, False)


# ── Interactive Update Browser ──────────────────────────────────────────────

async def _show_repo_browser(update: Update, uid: int, repo: str, path: str = "", branch: str = "main"):
    """Show interactive GitHub file browser using InlineKeyboardMarkup"""
    res = github_agent.list_dir_contents(repo, path, branch=branch)
    if res.get("status") == "error":
        msg = f"❌ Error browsing {repo}/{path} on branch {branch}: {res.get('error')}"
        if update.callback_query:
            await update.callback_query.message.reply_text(msg)
        else:
            await update.message.reply_text(msg)
        return

    keyboard = []
    
    # "Upload New File Here" button for the current folder
    keyboard.append([InlineKeyboardButton("➕ Upload New File Here", callback_data=f"upd_new|{repo}|{path}")])

    # "Up a level" button if not in root
    if path:
        parent = "/".join(path.split("/")[:-1])
        keyboard.append([InlineKeyboardButton("⬆️ .. (Up to Parent)", callback_data=f"upd_nav|{repo}|{parent}")])

    # Add folders and files
    items = res.get("items", [])
    for item in items:
        icon    = "📁" if item["type"] == "dir" else "📄"
        cb_type = "upd_nav" if item["type"] == "dir" else "upd_sel"
        # callback_data is limited to 64 bytes. For deep paths, this might break.
        # But for simple repos it usually works.
        cb_data = f"{cb_type}|{repo}|{item['path']}"
        keyboard.append([InlineKeyboardButton(f"{icon} {item['name']}", callback_data=cb_data[:64])])

    reply_markup = InlineKeyboardMarkup(keyboard)
    title = f"📂 {repo}/{path}" if path else f"📂 {repo} (Root)"
    
    if update.callback_query:
        await update.callback_query.edit_message_text(f"Select file to update:\n{title}", reply_markup=reply_markup)
    else:
        await update.message.reply_text(f"Select file to update:\n{title}", reply_markup=reply_markup)


async def _poll_update_and_notify(update: Update, uid: int, repo: str, branch: str = "main"):
    """Background task to poll the pipeline triggered from the /update flow and notify the user on success/failure."""
    set_running(uid, True)
    # Compute branch-scoped project name to match what orchestrator saved in state
    dep = state.get_deployment_by_repo(repo)
    base_project = dep.get("project", repo) if dep else repo
    # Strip any existing branch suffix to get base project, then re-scope
    safe_branch = branch.replace("/", "-").replace("_", "-")[:20]
    branch_project = base_project if branch == "main" else f"{base_project}-{safe_branch}"
    
    async def cb(msg):
        try:
            if update.callback_query:
                await update.callback_query.message.reply_text(msg)
            else:
                await update.message.reply_text(msg)
        except Exception: pass
        
    try:
        await cb(f"⏳ Waiting for deployment pipeline to complete in `{repo}`...")
        
        # We need to wait a few seconds so the pipeline actually registers in GitHub before we start polling
        await asyncio.sleep(5)
        
        res = await github_agent.poll_pipeline(repo, interval=15, max_wait=900, branch=branch)
        
        if res.get("status") == "completed" and res.get("conclusion") == "success":
            # Attempt to get live URL from branch-scoped state
            dep = state.get_deployment(branch_project)
            ip = dep.get("ec2_ip") if dep else None
            
            # Auto-heal: if URL/IP is missing in DB, try to extract it from the pipeline log
            if not ip:
                region = dep.get("region", "") if dep else ""
                extracted_url = orchestrator._extract_url(res, region=region)
                if extracted_url:
                    ip = extracted_url.replace("http://", "").replace("https://", "")
                    if dep:
                        state.update_deployment(branch_project, ec2_ip=ip)
            
            url = f"http://{ip.replace('http://', '').replace('https://', '')}" if ip else None
            live_link = f"🌐 Live at: {url}\n" if url else "🌐 Live at: (check pipeline logs)\n"
            repo_link = f"🔗 Repo: github.com/{github_agent.username}/{repo}"
            
            project_name = branch_project
            banner = (
                "\n"
                "🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉\n"
                "✅  UPDATE DEPLOYED               ✅\n"
                "🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉\n"
                "\n"
                f"📦 Project : {project_name}\n"
                f"🔗 Repo    : github.com/{github_agent.username}/{repo}\n"
                f"{live_link}"
            )
            await cb(banner)
        elif res.get("status") == "completed":
            run_url = res.get("run_url", "")
            await cb(
                "💥💥💥💥💥💥💥💥💥💥💥💥💥💥💥\n"
                "❌   UPDATE DEPLOY FAILED         ❌\n"
                "💥💥💥💥💥💥💥💥💥💥💥💥💥💥💥\n"
                f"Conclusion: {res.get('conclusion')}\n"
                f"🔗 Logs: {run_url}"
            )
        else:
            await cb(f"⚠️ Pipeline polling timed out or stopped. Please check GitHub Actions for {repo}.")
    except Exception as e:
        logger.error(f"Error polling update deploy for {repo}: {e}")
    finally:
        set_running(uid, False)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid  = query.from_user.id
    data = query.data

    if data.startswith("upd_nav|"):
        _, repo, path = data.split("|", 2)
        branch = sessions.get(uid, {}).get("answers", {}).get("branch", "main")
        await _show_repo_browser(update, uid, repo, path, branch=branch)

    elif data.startswith("upd_sel|"):
        _, repo, path = data.split("|", 2)
        branch = sessions.get(uid, {}).get("answers", {}).get("branch", "main")
        sessions[uid] = {"mode": "update_content", "answers": {"repo": repo, "file": path, "branch": branch}}
        await query.message.reply_text(
            f"📄 Selected: {path} on branch {branch}\n\n"
            f"Please paste the new text here,\n"
            f"OR upload a file (Document) to replace it."
        )

    elif data.startswith("upd_new|"):
        _, repo, path = data.split("|", 2)
        branch = sessions.get(uid, {}).get("answers", {}).get("branch", "main")
        sessions[uid] = {"mode": "update_new_filename", "answers": {"repo": repo, "dir": path, "branch": branch}}
        dir_display = f"'{path}/'" if path else "the Root Folder"
        await query.message.reply_text(f"You want to upload a new file in {dir_display} on branch {branch}.\nWhat should the new file's name be? (e.g. style.css)")

    elif data.startswith("upd_go|"):
        parts = data.split("|")
        repo = parts[1]
        branch = parts[2] if len(parts) > 2 else "main"
        await query.message.reply_text(f"🚀 Triggering deploy for {repo} on branch {branch}...")
        r = github_agent.trigger_pipeline(repo, "deploy.yml", branch)
        await query.message.reply_text(f"{r.get('status')} — {r.get('url', r.get('error',''))}")
        sessions.pop(uid, None)
        
        if r.get("status") == "triggered":
            asyncio.create_task(_poll_update_and_notify(update, uid, repo, branch))

    elif data.startswith("upd_no|"):
        await query.message.reply_text("✅ Skipped deploy. You can upload more files using /update.")
        sessions.pop(uid, None)


    elif data.startswith("nodefix_ign|"):
        sess = sessions.get(uid)
        if not sess or sess.get("mode") != "node_fix_decision": return
        await query.message.reply_text("⚠️ Ignoring errors. Forcing deployment continuation...")
        sessions[uid]["mode"] = "collect"
        missing = sess["missing"]
        answers = sess["answers"]
        if missing:
            await _ask_field_with_buttons(update, uid, missing[0])
        else:
            answers["repo_name"] = answers.get("repo")
            await _show_confirm(update, uid, answers)

    elif data.startswith("nodefix_man|"):
        sess = sessions.get(uid)
        if not sess or sess.get("mode") != "node_fix_decision": return
        repo = sess["repo"]
        await query.message.reply_text(
            f"🛑 Deployment paused.\n"
            f"Please fix the repository manually in GitHub: `github.com/{github_agent.username}/{repo}`\n"
            f"Run `/deploy` again when you are ready.",
            parse_mode="Markdown"
        )
        sessions.pop(uid, None)

    # ── Interactive Choice Redirects ─────────────────────────────────────────
    elif data.startswith("ans_app|") or data.startswith("ans_tgt|") or data.startswith("init_db|") or data.startswith("init_fe|") or data.startswith("upd_br|") or data.startswith("dst_conf|") or data.startswith("dep_conf|") or data.startswith("dst_br|"):
        # We essentially mimic text input so the main state machine processes it
        value = data.split("|")[1]
        
        # Remove keyboard from previous message
        try: await query.message.edit_reply_markup(reply_markup=None)
        except Exception: pass
        
        # Force text reply as if user typed it
        await query.message.reply_text(f"> {value}")
        
        # Fake a text message Update object (best effort)
        class DummyMessage:
            def __init__(self, text, orig_msg):
                self.text = text
                self.orig_msg = orig_msg
            async def reply_text(self, *args, **kwargs):
                return await self.orig_msg.reply_text(*args, **kwargs)
                
        class DummyUpdate:
            def __init__(self, message, user):
                self.message = message
                self.effective_user = user
                self.callback_query = None
                
        fake_update = DummyUpdate(DummyMessage(value, query.message), query.from_user)
        
        # Process through main handler
        await handle_message(fake_update, context)

    elif data.startswith("nodefix_ai|"):
        sess = sessions.get(uid)
        if not sess or sess.get("mode") != "node_fix_decision": return
        repo = sess["repo"]
        fix_files = sess.get("fix_files", {})
        
        await query.message.edit_text("🤖 AI is applying fixes to the repository...")
        
        async def _apply_ai_fixes():
            set_running(uid, True)
            try:
                failed = []
                for file_path, content in fix_files.items():
                    res = github_agent.push_single_file(repo, file_path, content, f"AI Auto-fix for {file_path}", branch="main")
                    if res.get("failed"): failed.append(file_path)
                
                if failed:
                    await query.message.reply_text(f"⚠️ Failed to apply fix to: {', '.join(failed)}\nContinuing anyway...")
                else:
                    await query.message.reply_text("✅ AI applied all fixes successfully!")

                # Resume deploy collect flow
                sessions[uid]["mode"] = "collect"
                missing = sess["missing"]
                answers = sess["answers"]
                if missing:
                    await _ask_field_with_buttons(update, uid, missing[0])
                else:
                    answers["repo_name"] = answers.get("repo")
                    await _show_confirm(update, uid, answers)

            except Exception as e:
                logger.error(f"Error applying AI nodefix: {e}")
                await query.message.reply_text(f"❌ Error applying fix: {e}")
                sessions.pop(uid, None)
            finally:
                set_running(uid, False)
        
        asyncio.create_task(_apply_ai_fixes())


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid  = update.message.from_user.id
    sess = sessions.get(uid)
    if not sess: return

    if sess["mode"] == "update_content":
        doc  = update.message.document
        file_name = doc.file_name
        repo = sess["answers"]["repo"]
        target_path = sess["answers"]["file"]
        
        await update.message.reply_text(f"📥 Downloading `{file_name}`...")
        f = await context.bot.get_file(doc.file_id)
        content_bytes = await f.download_as_bytearray()
        
        sess["answers"]["content"] = bytes(content_bytes)
        
        sessions[uid]["mode"] = "update_deploy_ask"
        await update.message.reply_text(f"📤 Pushing `{target_path}` to `{repo}`...")
        asyncio.create_task(_run_push_and_ask_deploy(update, uid, sess["answers"]))

# ── App ───────────────────────────────────────────────────────────────────────

def main():
    state.init_db()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("stop",     cmd_stop))
    app.add_handler(CommandHandler("reset",    cmd_reset))
    app.add_handler(CommandHandler("deploy",   cmd_deploy))
    app.add_handler(CommandHandler("update",   cmd_update))
    app.add_handler(CommandHandler("destroy",  cmd_destroy))
    app.add_handler(CommandHandler("trigger",  cmd_trigger))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("projects", cmd_projects))
    app.add_handler(CommandHandler("list",     cmd_list))
    app.add_handler(CommandHandler("initnode", cmd_initnode))
    app.add_handler(CommandHandler("code",     cmd_code))
    app.add_handler(CommandHandler("github",   cmd_github))
    app.add_handler(CommandHandler("aws",      cmd_aws))
    app.add_handler(CommandHandler("tfstate",  cmd_tfstate))
    app.add_handler(CommandHandler("skills",   cmd_skills))
    app.add_handler(CommandHandler("addskill", cmd_addskill))
    app.add_handler(CommandHandler("delskill", cmd_delskill))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()