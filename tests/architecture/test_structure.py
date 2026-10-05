#!/usr/bin/env python3
# 저장소 구조 규칙 다섯 가지(규칙5 = 마크다운 코드 블록 여닫는 줄)를 검사하고 일부러 어긴 보기로 검사기를 확인한다
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
BARE_FILE_RE = re.compile(r"`([^`/\\]+?\.(?:md|py|sh))`")

# 작업 폴더에 실행 중 생기는 이름 · 사용자 환경 파일(저장소에 없는 것)
RULE2_ALLOWLIST = frozenset(
    {
        "video-to-claude.local.md",
        "CLAUDE.md",
        "스킬개선_기록.md",
        "자막_전문.md",
        "영상_전체텍스트.md",
        "읽기용.md",
        "읽기용_1부.md",
        "발표자방법.md",
        "항목표.md",
        "항목표_1부.md",
        "인터뷰_중간.md",
        "여럿_비교표.md",
        "앞으로_이렇게_쓰세요.md",
    }
)

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


def _rule2_normalize_name(raw: str) -> str:
    return raw.rstrip(").,;:")


def _rule2_skip_bare(name: str) -> bool:
    if name in RULE2_ALLOWLIST:
        return True
    if "*" in name or "{" in name or "<" in name or "YYYYMMDD" in name:
        return True
    return False


def _rule2_resolve_file(root: Path, name: str) -> Path | None:
    for base in (root / "references", root / "scripts", root):
        candidate = base / name
        if candidate.is_file():
            return candidate
    return None


def check_rule2_file_references(root: Path) -> list[str]:
    errors: list[str] = []
    for md_path in md_files_for_rule2(root):
        text = md_path.read_text(encoding="utf-8")
        rel_md = md_path.relative_to(root)
        for m in REF_PATH_RE.finditer(text):
            name = _rule2_normalize_name(m.group(1))
            target = root / "references" / name
            if not target.is_file():
                errors.append(
                    f"규칙2 {rel_md}: references/{name} 파일 없음"
                )
        for m in SCRIPT_PATH_RE.finditer(text):
            name = _rule2_normalize_name(m.group(1))
            target = root / "scripts" / name
            if not target.is_file():
                errors.append(
                    f"규칙2 {rel_md}: scripts/{name} 파일 없음"
                )
        for m in BARE_FILE_RE.finditer(text):
            name = _rule2_normalize_name(m.group(1))
            if _rule2_skip_bare(name):
                continue
            if _rule2_resolve_file(root, name) is None:
                errors.append(f"규칙2 {rel_md}: {name} 파일 없음")
    return errors


def _rule3_patterns() -> list[tuple[str, re.Pattern[str]]]:
    mnt_users = "/" + "mnt" + "/c/Users/"
    win_users_bs = "C:" + "\\" + "Users" + "\\"
    gmail = "@" + "gmail.com"
    ic = re.IGNORECASE
    return [
        # 슬래시 묶음(/ · // · ///) 바로 뒤 home/ 을 잡는다. 슬래시 묶음 앞 글자가 영숫자 . _ - 이면
        # 다른 경로나 주소의 일부(/opt/home/ · example.com/home/)라 뺀다. 이름 자리는 한글 포함 아무 글자
        # (공백 / < 제외)라 이름 뒤에 / 가 없어도 잡고, 꺾쇠 자리 표시 /home/<...> 는 뺀다. 대소문자는 무시한다
        (
            "개인 홈 경로",
            re.compile(r"(?<![A-Za-z0-9._/-])/+home/[^\s/<]+", ic),
        ),
        ("WSL 사용자 경로", re.compile(re.escape(mnt_users), ic)),
        ("Windows 사용자 경로(역슬래시)", re.compile(re.escape(win_users_bs), ic)),
        ("Windows 사용자 경로(슬래시)", re.compile(r"[cC]:/Users/", ic)),
        ("gmail 주소", re.compile(re.escape(gmail), ic)),
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


FENCE_LINE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
# 여는 줄 뒤 글 = 영문 언어 이름(c# · c++ 포함)으로 시작하면 그 뒤 설정 글(title="…")까지 허용한다.
# 한글 문장처럼 언어 이름이 아닌 글로 시작하면 줄을 잘못 붙인 것으로 본다
FENCE_LANG_RE = re.compile(r"[A-Za-z0-9+#._-]+(?:\s.*)?")


def iter_md_files_all(root: Path) -> list[Path]:
    # tests/ 와 .git/ 은 빼고 저장소의 모든 .md 를 모은다
    found: list[Path] = []
    for path in root.rglob("*.md"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] == "tests":
            continue
        if any(part in RULE3_SKIP_DIRS for part in rel.parts):
            continue
        found.append(path)
    return sorted(found)


def check_rule5_md_code_fences(root: Path) -> list[str]:
    errors: list[str] = []
    for path in iter_md_files_all(root):
        rel = path.relative_to(root)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        open_mark: str | None = None
        open_line = 0
        for no, line in enumerate(lines, start=1):
            m = FENCE_LINE_RE.match(line)
            if open_mark is None:
                if not m:
                    continue
                rest = m.group(2).strip()
                if rest and not FENCE_LANG_RE.fullmatch(rest):
                    errors.append(
                        f"규칙5 {rel}:{no}: 코드 블록 여는 줄 뒤에 글이 붙음"
                    )
                open_mark = m.group(1)
                open_line = no
                continue
            # 블록 안 — 같은 종류 기호이고 여는 줄보다 짧지 않은 줄만 닫는 줄 후보로 본다.
            # 더 짧은 줄(```` 블록 안의 ```bash 예시)은 내용이다
            if not m or m.group(1)[0] != open_mark[0] or len(m.group(1)) < len(open_mark):
                continue
            if m.group(2).strip():
                errors.append(
                    f"규칙5 {rel}:{no}: 코드 블록 닫는 줄 뒤에 글이 붙음"
                )
                continue
            open_mark = None
        if open_mark is not None:
            errors.append(f"규칙5 {rel}:{open_line}: 코드 블록이 닫히지 않음")
    return errors


def run_all_checks(root: Path) -> list[str]:
    errors: list[str] = []
    errors.extend(check_rule1_scripts_imports(root))
    errors.extend(check_rule2_file_references(root))
    errors.extend(check_rule3_no_personal_paths(root))
    errors.extend(check_rule4_skill_frontmatter(root))
    errors.extend(check_rule5_md_code_fences(root))
    return errors


def _copy_rule_fixtures(troot: Path, root: Path) -> None:
    shutil.copytree(root / "scripts", troot / "scripts")
    shutil.copytree(root / "references", troot / "references")
    shutil.copy(root / "SKILL.md", troot / "SKILL.md")
    shutil.copy(root / "README.md", troot / "README.md")


def _assert_rule_errors(
    root: Path,
    label: str,
    rule_num: int,
    label_substr: str | None = None,
) -> None:
    prefix = f"규칙{rule_num}"
    errs = run_all_checks(root)
    matched = [e for e in errs if e.startswith(prefix)]
    if not matched:
        raise AssertionError(
            f"일부러 어긴 보기({label}): {prefix} 오류 없음 — 전체={errs!r}"
        )
    if label_substr is not None and not any(label_substr in e for e in matched):
        raise AssertionError(
            f"일부러 어긴 보기({label}): '{label_substr}' 없음 — {matched!r}"
        )


def run_negative_self_tests() -> None:
    """일부러 어긴 보기로 각 규칙 검사가 실패를 잡는지 확인한다."""
    root = repo_root()

    with tempfile.TemporaryDirectory() as tmp:
        troot = Path(tmp)
        _copy_rule_fixtures(troot, root)
        bad_py = troot / "scripts" / "bad_import.py"
        bad_py.write_text(
            "# 일부러 어긴 규칙1 검사용\nimport references\n",
            encoding="utf-8",
        )
        _assert_rule_errors(troot, "규칙1 import", 1, "허용되지 않은 불러오기")

    with tempfile.TemporaryDirectory() as tmp:
        troot = Path(tmp)
        _copy_rule_fixtures(troot, root)
        readme = (root / "README.md").read_text(encoding="utf-8")
        readme += "\n`scripts/없는파일_일부러어긴.py`\n"
        (troot / "README.md").write_text(readme, encoding="utf-8")
        _assert_rule_errors(troot, "규칙2 scripts 경로", 2, "scripts/없는파일")

    with tempfile.TemporaryDirectory() as tmp:
        troot = Path(tmp)
        _copy_rule_fixtures(troot, root)
        skill = (root / "SKILL.md").read_text(encoding="utf-8")
        skill = skill.replace("`길_고르기.md`", "`없는파일_일부러어긴.md`", 1)
        (troot / "SKILL.md").write_text(skill, encoding="utf-8")
        _assert_rule_errors(
            troot, "규칙2 폴더 없는 이름", 2, "없는파일_일부러어긴.md"
        )

    rule3_cases = [
        ("규칙3 개인 홈", "/home/someuser/leak\n", "개인 홈 경로"),
        ("규칙3 개인 홈(끝 슬래시 없음)", "경로 /home/someuser 끝\n", "개인 홈 경로"),
        ("규칙3 개인 홈(허락 규칙 꼴 //home)", "Read(//home/someuser/.claude/x.md)\n", "개인 홈 경로"),
        ("규칙3 개인 홈(file:///home)", "file:///home/someuser/x\n", "개인 홈 경로"),
        ("규칙3 개인 홈(콜론 뒤)", "예:/home/someuser\n", "개인 홈 경로"),
        (
            "규칙3 WSL",
            "/" + "mnt" + "/c/Users/x/leak\n",
            "WSL 사용자 경로",
        ),
        (
            "규칙3 Windows 역슬래시",
            "C:" + "\\" + "Users\\x\\leak\n",
            "Windows 사용자 경로(역슬래시)",
        ),
        ("규칙3 Windows 슬래시", "C:/Users/x/leak\n", "Windows 사용자 경로(슬래시)"),
        ("규칙3 gmail", "contact" + "@gmail.com\n", "gmail 주소"),
        ("규칙3 한글 이름 홈", "/home/영근/leak\n", "개인 홈 경로"),
        ("규칙3 대문자 Home", "/Home/someuser\n", "개인 홈 경로"),
        (
            "규칙3 WSL 소문자",
            "/" + "mnt" + "/c/users/x/leak\n",
            "WSL 사용자 경로",
        ),
        ("규칙3 gmail 대소문자", "contact" + "@Gmail.com\n", "gmail 주소"),
        ("규칙3 Windows 소문자 슬래시", "c:/users/young\n", "Windows 사용자 경로(슬래시)"),
        (
            "규칙3 Windows 역슬래시 young",
            "C:" + "\\" + "Users\\young\n",
            "Windows 사용자 경로(역슬래시)",
        ),
    ]
    for case_label, leak_text, pattern_label in rule3_cases:
        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _copy_rule_fixtures(troot, root)
            (troot / "leak.md").write_text(leak_text, encoding="utf-8")
            _assert_rule_errors(troot, case_label, 3, pattern_label)

    # 잡으면 안 되는 보기(주소 안 /home/ · 경로 조각 · 꺾쇠 자리 표시)는 규칙3 오류가 없어야 한다
    rule3_ok_cases = [
        ("규칙3 허용 주소 안 home", "https://example.com/home/docs\n"),
        ("규칙3 허용 경로 조각 home", "/opt/home/someuser\n"),
        ("규칙3 허용 꺾쇠 자리 표시", "Read(//home/<사용자 이름>/.claude/x.md)\n"),
        ("규칙3 허용 꺾쇠 자리 표시(슬래시 하나)", "경로 /home/<사용자 이름>/x\n"),
    ]
    for case_label, ok_text in rule3_ok_cases:
        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _copy_rule_fixtures(troot, root)
            (troot / "leak.md").write_text(ok_text, encoding="utf-8")
            extra = [e for e in run_all_checks(troot) if e.startswith("규칙3")]
            if extra:
                raise AssertionError(
                    f"허용해야 할 보기({case_label})인데 규칙3 오류 — {extra!r}"
                )

    with tempfile.TemporaryDirectory() as tmp:
        troot = Path(tmp)
        shutil.copytree(root / "scripts", troot / "scripts")
        shutil.copytree(root / "references", troot / "references")
        shutil.copy(root / "README.md", troot / "README.md")
        (troot / "SKILL.md").write_text("# no frontmatter\n", encoding="utf-8")
        _assert_rule_errors(troot, "규칙4 머리막", 4, "규칙4 SKILL.md")

    # 규칙5 일부러 어긴 보기 셋(닫는 줄 뒤 글 · 여는 줄 뒤 글 · 안 닫힘)
    rule5_cases = [
        ("규칙5 닫는 줄 뒤 글", "```bash\nx\n``` 붙은 글\n", "닫는 줄 뒤에 글이 붙음"),
        ("규칙5 여는 줄 뒤 글", "``` 새 창에서 열기\nx\n```\n", "여는 줄 뒤에 글이 붙음"),
        ("규칙5 안 닫힘", "```bash\nx\n", "닫히지 않음"),
    ]
    for case_label, bad_text, pattern_label in rule5_cases:
        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _copy_rule_fixtures(troot, root)
            (troot / "leak.md").write_text(bad_text, encoding="utf-8")
            _assert_rule_errors(troot, case_label, 5, pattern_label)

    # 잡으면 안 되는 보기(언어 이름 붙은 정상 블록 · ~~~ 정상 블록)는 규칙5 오류가 없어야 한다
    rule5_ok_cases = [
        ("규칙5 허용 ```bash 블록", "```bash\necho hi\n```\n"),
        ("규칙5 허용 ~~~ 블록", "~~~\n``` 안의 글\n~~~\n"),
        ("규칙5 허용 ```` 블록 안 ```bash 예시", "````markdown\n```bash\nx\n```\n````\n"),
        ("규칙5 허용 ```c# · ```bash title", "```c#\nx\n```\n\n```bash title=\"a\"\nx\n```\n"),
    ]
    for case_label, ok_text in rule5_ok_cases:
        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            _copy_rule_fixtures(troot, root)
            (troot / "leak.md").write_text(ok_text, encoding="utf-8")
            extra = [e for e in run_all_checks(troot) if e.startswith("규칙5")]
            if extra:
                raise AssertionError(
                    f"허용해야 할 보기({case_label})인데 규칙5 오류 — {extra!r}"
                )


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
