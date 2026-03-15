"""
Skill Loader — reads markdown skill files and injects into prompts
"""
from pathlib import Path

SKILLS_DIR = Path(__file__).parent


def load_skill(name: str) -> str:
    """Load a skill by name. Returns content or empty string."""
    for path in [
        SKILLS_DIR / "custom" / f"{name}.md",   # custom first
        SKILLS_DIR / f"{name}.md",               # built-in
    ]:
        if path.exists():
            return path.read_text()
    return ""


def load_skills(*names) -> str:
    """Load multiple skills and combine."""
    parts = []
    for name in names:
        content = load_skill(name)
        if content:
            parts.append(f"## Skill: {name}\n{content}")
    return "\n\n".join(parts)


def list_skills() -> list:
    """List all available skills."""
    skills = []
    for path in sorted(SKILLS_DIR.glob("*.md")):
        skills.append({"name": path.stem, "type": "built-in", "path": str(path)})
    for path in sorted((SKILLS_DIR / "custom").glob("*.md")):
        skills.append({"name": path.stem, "type": "custom", "path": str(path)})
    return skills


def add_skill(name: str, content: str) -> str:
    """Add or update a custom skill."""
    path = SKILLS_DIR / "custom" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return str(path)


def delete_skill(name: str) -> bool:
    """Delete a custom skill."""
    path = SKILLS_DIR / "custom" / f"{name}.md"
    if path.exists():
        path.unlink()
        return True
    return False
