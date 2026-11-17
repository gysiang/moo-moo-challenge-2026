from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

NAME = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)


def skill_name(skill_md: Path) -> str:
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{skill_md}: missing YAML frontmatter")
    match = NAME.search(text.split("---", 2)[1])
    if not match:
        raise ValueError(f"{skill_md}: frontmatter has no name")
    return match.group(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zip each deploy/skills/<name>/ for QCA upload")
    parser.add_argument("--skills", default=str(Path(__file__).parent / "skills"))
    parser.add_argument("--out", default=str(Path(__file__).parent / "dist"))
    args = parser.parse_args(argv)

    skills = Path(args.skills)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    built = 0
    for folder in sorted(p for p in skills.iterdir() if p.is_dir()):
        manifest = folder / "SKILL.md"
        if not manifest.exists():
            continue
        name = skill_name(manifest)
        if name != folder.name:
            raise ValueError(f"{folder}: frontmatter name {name!r} != folder name")
        target = out / f"{name}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(folder.rglob("*")):
                if file.is_file() and "__pycache__" not in file.parts:
                    archive.write(file, (Path(name) / file.relative_to(folder)).as_posix())
        print(f"{name}.zip  {target.stat().st_size} bytes")
        built += 1
    if not built:
        raise SystemExit(f"no skills found under {skills}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
