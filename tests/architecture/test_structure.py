#!/usr/bin/env python3
# 저장소 구조 규칙 네 가지를 검사하고 일부러 어긴 보기로 검사기를 확인한다
"""구조 규칙 시험. 통과 시 「구조 규칙 통과」를 출력하고 종료 코드 0."""

from __future__ import annotations

import ast
import re
import shutil
import sys
import tempfile
from pathlib import Path

# 규칙3은 이 파일에 금지 경로 예시를 글자 그대로 넣지 않기 위해 검사 대상에서 뺀다
RULE3_SKIP_REL = Path("tests/architecture/test_structure.py")

ALLOWED_EXTERNAL = frozenset(
    {
        "paddleocr",
        "cv2",
        "numpy",
        "faster_whisper",
    }
)

TEXT_SUFFIXES = frozenset(
    {".md", ".py", ".sh", ".yml", ".yaml", ".txt", ".json"}
)
TEXT_NAMES = frozenset({"LICENSE", ".gitignore"})

MD_SCAN_REL = (
    Path("SKILL.md"),
    Path("README.md"),
)
REFERENCES_GLOB = "references/*.md"

REF_PATH_RE = re.compile(r"references/([^`\s'\"]+)")
SCRIPT_PATH_RE = re.compile(r"scripts/([^`\s'\"]+)")

# README 꺾쇠 자리 표시용 /home/<...>/ 는 규칙3에서 제외
README_HOME_PLACEHOLDER_RE = re.compile(r"/+home/<[^>]+>/")

RULE3_SKIP_DIRS = frozenset({".git", "__pycache__"})


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def script_local_modules(scripts_dir: Path) -> frozenset[str]:
    return frozenset(p.stem for p in scripts_dir.glob("*.py"))


def is_stdlib(module: str) -> bool:
    top = module.split(".", 1)[0]
    return top in sys.stdlib_module_names


def check_rule1_scripts_imports(root: Path) -> list[str]:
    errors: list[str] = []
    scripts_dir = root / "scripts"
    if not scripts_dir.is_dir():
        return [f"규칙1 scripts/ 폴더 없음: {scripts_dir}"]

    local = script_local_modules(scripts_dir)
    for py_path in sorted(scripts_dir.glob("*.py")):
        try:
            tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
        except SyntaxError as exc:
            errors.append(f"규칙1 {py_path.relative_to(root)}: 구문 오류 — {exc}")
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name.split(".", 1)[0]
                    if mod in local or mod in ALLOWED_EXTERNAL or is_stdlib(mod):
                        continue
                    if mod in ("references", "tests") or mod.startswith("references."):
                        errors.append(
                            f"규칙1 {py_path.relative_to(root)}: "
                            f"허용되지 않은 불러오기 '{alias.name}'"
                        )
                    else:
                        errors.append(
                            f"규칙1 {py_path.relative_to(root)}: "
                            f"허용되지 않은 불러오기 '{alias.name}'"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    errors.append(
                        f"규칙1 {py_path.relative_to(root)}: "
                        f"다른 폴더 상대 불러오기는 금지"
                    )
                    continue
                if not node.module:
                    continue
                mod = node.module.split(".", 1)[0]
                if mod in local or mod in ALLOWED_EXTERNAL or is_stdlib(mod):
                    continue
                if mod in ("references", "tests") or mod.startswith("references."):
                    errors.append(
                        f"규칙1 {py_path.relative_to(root)}: "
                        f"허용되지 않은 불러오기 'from {node.module}'"
                    )
                else:
                    errors.append(
                        f"규칙1 {py_path.relative_to(root)}: "
                        f"허용되지 않은 불러오기 'from {node.module}'"
                    )
    return errors


def md_files_for_rule2(root: Path) -> list[Path]:
    files: list[Path] = []
    for rel in MD_SCAN_REL:
        p = root / rel
        if p.is_file():
            files.append(p)
    files.extend(sorted(root.glob(REFERENCES_GLOB)))
    return files


def check_rule2_file_references(root: Path) -> list[str]:
    errors: list[str] = []
    for md_path in md_files_for_rule2(root):
        text = md_path.read_text(encoding="utf-8")
        rel_md = md_path.relative_to(root)
        for m in REF_PATH_RE.finditer(text):
            name = m.group(1).rstrip(").,;:")
            target = root / "references" / name
            if not target.is_file():
                errors.append(
                    f"규칙2 {rel_md}: references/{name} 파일 없음"
                )
        for m in SCRIPT_PATH_RE.finditer(text):
            name = m.group(1).rstrip(").,;:")
            target = root / "scripts" / name
            if not target.is_file():
                errors.append(
                    f"규칙2 {rel_md}: scripts/{name} 파일 없음"
                )
    return errors


def _rule3_patterns() -> list[tuple[str, re.Pattern[str]]]:
    mnt_users = "/" + "mnt" + "/c/Users/"
    win_users = "C:" + "\\" + "Users" + "\\"
    gmail = "@" + "gmail.com"
    return [
        ("개인 홈 경로", re.compile(r"/home/[^/\s<>]+/")),
        ("WSL 사용자 경로", re.compile(mnt_users)),
        ("Windows 사용자 경로", re.compile(re.escape(win_users))),
        ("gmail 주소", re.compile(re.escape(gmail))),
    ]


def iter_text_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in RULE3_SKIP_DIRS for part in rel.parts):
            continue
        if rel == RULE3_SKIP_REL:
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            found.append(path)
    return sorted(found)


def check_rule3_no_personal_paths(root: Path) -> list[str]:
    errors: list[str] = []
    patterns = _rule3_patterns()
    for path in iter_text_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(root)
        if rel == Path("README.md"):
            text = README_HOME_PLACEHOLDER_RE.sub("", text)
        for label, pat in patterns:
            if pat.search(text):
                errors.append(f"규칙3 {rel}: {label} 패턴 발견")
    return errors


def check_rule4_skill_frontmatter(root: Path) -> list[str]:
    errors: list[str] = []
    skill = root / "SKILL.md"
    if not skill.is_file():
        return ["규칙4 SKILL.md 없음"]
    text = skill.read_text(encoding="utf-8")
    if not text.startswith("---"):
        errors.append("규칙4 SKILL.md: 첫 줄이 --- 로 시작하지 않음")
        return errors
    end = text.find("\n---", 3)
    if end == -1:
        errors.append("규칙4 SKILL.md: YAML 머리막 닫는 --- 없음")
        return errors
    front = text[: end + 1]
    if not re.search(r"^name:\s*video-to-claude\s*$", front, re.MULTILINE):
        errors.append("규칙4 SKILL.md: name: video-to-claude 없음")
    if not re.search(r"^description:\s*.+", front, re.MULTILINE):
        errors.append("규칙4 SKILL.md: description: 없음")
    return errors


def run_all_checks(root: Path) -> list[str]:
    errors: list[str] = []
    errors.extend(check_rule1_scripts_imports(root))
    errors.extend(check_rule2_file_references(root))
    errors.extend(check_rule3_no_personal_paths(root))
    errors.extend(check_rule4_skill_frontmatter(root))
    return errors


def _assert_fails(root: Path, label: str) -> None:
    errs = run_all_checks(root)
    if not errs:
        raise AssertionError(f"일부러 어긴 보기({label})인데 검사가 통과함")


def run_negative_self_tests() -> None:
    """일부러 어긴 보기로 각 규칙 검사가 실패를 잡는지 확인한다."""
    root = repo_root()

    # 일부러 어긴 — 규칙1 scripts 불러오기
    with tempfile.TemporaryDirectory() as tmp:
        troot = Path(tmp)
        shutil.copytree(root / "scripts", troot / "scripts")
        shutil.copytree(root / "references", troot / "references")
        shutil.copy(root / "SKILL.md", troot / "SKILL.md")
        shutil.copy(root / "README.md", troot / "README.md")
        bad_py = troot / "scripts" / "bad_import.py"
        bad_py.write_text(
            "# 일부러 어긴 규칙1 검사용\nimport references\n",
            encoding="utf-8",
        )
        _assert_fails(troot, "규칙1")

    with tempfile.TemporaryDirectory() as tmp:
        troot = Path(tmp)
        shutil.copytree(root / "scripts", troot / "scripts")
        shutil.copytree(root / "references", troot / "references")
        shutil.copy(root / "SKILL.md", troot / "SKILL.md")
        readme = (root / "README.md").read_text(encoding="utf-8")
        readme += "\n`scripts/없는파일_일부러어긴.py`\n"
        (troot / "README.md").write_text(readme, encoding="utf-8")
        _assert_fails(troot, "규칙2")

    with tempfile.TemporaryDirectory() as tmp:
        troot = Path(tmp)
        shutil.copytree(root / "scripts", troot / "scripts")
        shutil.copytree(root / "references", troot / "references")
        shutil.copy(root / "SKILL.md", troot / "SKILL.md")
        shutil.copy(root / "README.md", troot / "README.md")
        leak = troot / "leak.md"
        # 일부러 어긴 규칙3 — 금지 경로 조각을 이어 붙임
        leak.write_text("/" + "mnt" + "/c/Users/x/leak\n", encoding="utf-8")
        _assert_fails(troot, "규칙3")

    with tempfile.TemporaryDirectory() as tmp:
        troot = Path(tmp)
        shutil.copytree(root / "scripts", troot / "scripts")
        shutil.copytree(root / "references", troot / "references")
        shutil.copy(root / "README.md", troot / "README.md")
        # 일부러 어긴 규칙4 — SKILL 머리막 없음
        (troot / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")
        _assert_fails(troot, "규칙4")


def main() -> None:
    root = repo_root()
    errors = run_all_checks(root)
    if errors:
        for line in errors:
            print(line, file=sys.stderr)
        sys.exit(1)

    try:
        run_negative_self_tests()
    except AssertionError as exc:
        print(f"자기시험 실패: {exc}", file=sys.stderr)
        sys.exit(1)

    print("구조 규칙 통과")
    sys.exit(0)


if __name__ == "__main__":
    main()
