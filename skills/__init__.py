from pathlib import Path

SKILLS_DIR = Path(__file__).parent

def load_skill(name):
    for p in [SKILLS_DIR / "custom" / f"{name}.md", SKILLS_DIR / f"{name}.md"]:
        if p.exists():
            return p.read_text(encoding='utf-8')
    return ""

def load_skills(*names):
    parts = []
    for n in names:
        c = load_skill(n)
        if c:
            parts.append(f"## Skill: {n}\n{c}")
    return "\n\n".join(parts)

def list_skills():
    skills = []
    for p in sorted(SKILLS_DIR.glob("*.md")):
        skills.append({"name": p.stem, "type": "built-in"})
    for p in sorted((SKILLS_DIR / "custom").glob("*.md")):
        skills.append({"name": p.stem, "type": "custom"})
    return skills

def add_skill(name, content):
    p = SKILLS_DIR / "custom" / f"{name}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding='utf-8')
    return str(p)

def delete_skill(name):
    p = SKILLS_DIR / "custom" / f"{name}.md"
    if p.exists():
        p.unlink()
        return True
    return False
