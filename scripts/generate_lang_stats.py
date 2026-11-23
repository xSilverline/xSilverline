#!/usr/bin/env python3
"""Generuje kartę SVG z językami programowania na podstawie commitów ze wszystkich branchy."""

from __future__ import annotations

import base64
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

# Jawna mapa rozszerzeń -> język (HTML/CSS/JSON/MD pominięte celowo)
EXTENSION_TO_LANG: dict[str, str] = {
    ".py": "Python",
    ".pyw": "Python",
    ".cs": "C#",
    ".cpp": "C++",
    ".cc": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".hh": "C++",
    ".hxx": "C++",
    ".inl": "C++",
    ".c": "C",
    ".java": "Java",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".mts": "TypeScript",
    ".cts": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".vue": "Vue",
    ".sql": "SQL",
    ".gd": "GDScript",
    ".hlsl": "Shaders",
    ".glsl": "Shaders",
    ".shader": "Shaders",
    ".usf": "Shaders",
    ".ush": "Shaders",
    ".rs": "Rust",
    ".go": "Go",
    ".kt": "Kotlin",
}

CPP_EXTS = {".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".inl"}

# Oficjalne kolory GitHub Linguist
LANG_COLORS: dict[str, str] = {
    "Python": "#3572A5",
    "C#": "#178600",
    "C++": "#f34b7d",
    "C": "#555555",
    "Java": "#b07219",
    "TypeScript": "#3178c6",
    "JavaScript": "#f1e05a",
    "Vue": "#41b883",
    "SQL": "#e38c00",
    "GDScript": "#355570",
    "Shaders": "#aace60",
    "Rust": "#dea584",
    "Go": "#00ADD8",
    "Kotlin": "#A97BFF",
}

IGNORED_DIRS = {
    "node_modules", "dist", "build", "out", "target", "vendor",
    ".venv", "venv", "__pycache__", ".git", ".idea", ".vs",
    "binaries", "intermediate", "saved", "deriveddatacache",  # Unreal Engine
    "library", "temp", "obj", "bin", "packages", "logs",      # Unity / .NET
}

IGNORED_SUFFIXES = (
    ".min.js", ".bundle.js", ".d.ts", ".designer.cs", ".g.cs", ".generated.h",
)

RENAME_BRACE_RE = re.compile(r"\{[^{}]* => ([^{}]*)\}")


def parse_csv_env(name: str, lower: bool = True) -> set[str]:
    raw = os.environ.get(name, "")
    items = [item.strip() for item in raw.split(",") if item.strip()]
    return {item.lower() for item in items} if lower else set(items)


def normalize_git_path(raw_path: str) -> str:
    """Obsługuje zapisy zmian nazw z git --numstat (np. src/{old => new}/a.py)."""
    if " => " not in raw_path:
        return raw_path
    if "{" in raw_path and "}" in raw_path:
        return RENAME_BRACE_RE.sub(r"\1", raw_path)
    return raw_path.split(" => ")[-1]


def is_ignored_path(path_str: str) -> bool:
    lower_path = path_str.lower()
    if lower_path.endswith(IGNORED_SUFFIXES):
        return True
    parts = {part.lower() for part in Path(path_str).parts[:-1]}
    return bool(parts & IGNORED_DIRS)


def fetch_accessible_repos(
    token: str,
    include_forks: bool,
    exclude_repos: set[str],
    extra_repos: set[str],
    max_size_kb: int,
) -> list[str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "all-branch-lang-stats",
    }
    repos: dict[str, str] = {}
    page = 1

    while True:
        url = (
            "https://api.github.com/user/repos"
            f"?affiliation=owner,collaborator,organization_member&per_page=100&page={page}"
        )
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                batch = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            sys.exit(f"[ERROR] GitHub API zwróciło HTTP {exc.code}. Sprawdź uprawnienia METRICS_TOKEN.")

        if not batch:
            break

        for repo in batch:
            full_name = repo["full_name"]
            short_name = repo["name"].lower()
            full_lower = full_name.lower()

            if full_lower in exclude_repos or short_name in exclude_repos:
                continue
            if not include_forks and repo.get("fork"):
                continue
            if repo.get("size", 0) == 0:
                continue
            if max_size_kb > 0 and repo.get("size", 0) > max_size_kb:
                print(f"[SKIP] {full_name} przekracza limit rozmiaru ({repo['size']} KB > {max_size_kb} KB)")
                continue

            repos[full_lower] = full_name

        if len(batch) < 100:
            break
        page += 1

    for extra in extra_repos:
        if extra.lower() not in exclude_repos:
            repos.setdefault(extra.lower(), extra)

    return sorted(repos.values(), key=str.lower)


def analyze_repo(
    full_name: str,
    auth_header: str,
    author_emails: set[str],
    author_names: set[str],
    seen_commits: set[str],
    max_lines_per_file: int,
) -> tuple[Counter[str], int]:
    git_env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
    }
    repo_ext_lines: Counter[str] = Counter()
    repo_commits = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        bare_dir = Path(tmpdir) / "repo.git"
        clone_res = subprocess.run(
            [
                "git",
                "-c", f"http.extraheader={auth_header}",
                "clone", "--bare", "--quiet",
                f"https://github.com/{full_name}.git",
                str(bare_dir),
            ],
            env=git_env,
            capture_output=True,
            text=True,
        )
        if clone_res.returncode != 0:
            print(f"[WARN] Nie udało się sklonować {full_name} (kod {clone_res.returncode})")
            return Counter(), 0

        log_res = subprocess.run(
            [
                "git", "-C", str(bare_dir),
                "-c", "core.quotepath=false",
                "log", "--all", "--no-merges",
                "--format=COMMIT%x09%H%x09%an%x09%ae",
                "--numstat",
            ],
            env=git_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if log_res.returncode != 0:
            print(f"[SKIP] {full_name}: brak historii commitów")
            return Counter(), 0

        active_commit = False
        for line in log_res.stdout.splitlines():
            if line.startswith("COMMIT\t"):
                parts = line.split("\t", 3)
                if len(parts) < 4:
                    active_commit = False
                    continue
                _, sha, name, email = parts
                is_mine = (email.strip().lower() in author_emails) or (
                    name.strip().lower() in author_names
                )
                if is_mine and sha not in seen_commits:
                    seen_commits.add(sha)
                    repo_commits += 1
                    active_commit = True
                else:
                    active_commit = False
                continue

            if not active_commit or "\t" not in line:
                continue

            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            added_str, _, raw_path = parts
            if added_str == "-":  # plik binarny
                continue

            try:
                added = int(added_str)
            except ValueError:
                continue
            if added <= 0:
                continue

            norm_path = normalize_git_path(raw_path.strip())
            if is_ignored_path(norm_path):
                continue

            ext = Path(norm_path).suffix.lower()
            if ext not in EXTENSION_TO_LANG and ext != ".h":
                continue

            if max_lines_per_file > 0:
                added = min(added, max_lines_per_file)
            repo_ext_lines[ext] += added

    # Heurystyka dla plików .h: jeśli w repo jest kod C++, klasyfikuj .h jako C++
    has_cpp = any(ext in repo_ext_lines for ext in CPP_EXTS)
    repo_langs: Counter[str] = Counter()
    for ext, count in repo_ext_lines.items():
        lang = ("C++" if has_cpp else "C") if ext == ".h" else EXTENSION_TO_LANG[ext]
        repo_langs[lang] += count

    return repo_langs, repo_commits


def render_svg(lang_lines: Counter[str], total_commits: int, active_repos: int, top_n: int = 6) -> str:
    top_langs = lang_lines.most_common(top_n)
    total_lines = sum(count for _, count in top_langs)
    if total_lines == 0:
        sys.exit("[ERROR] Zliczono 0 linii kodu. Sprawdź AUTHOR_EMAILS / AUTHOR_NAMES w logach.")

    width, height = 340, 195
    bar_x, bar_y, bar_w, bar_h = 25, 50, 290, 8

    bar_segments: list[str] = []
    cursor_x = float(bar_x)
    for idx, (lang, count) in enumerate(top_langs):
        seg_w = (bar_x + bar_w - cursor_x) if idx == len(top_langs) - 1 else (count / total_lines) * bar_w
        color = LANG_COLORS.get(lang, "#8b949e")
        bar_segments.append(
            f'<rect x="{cursor_x:.2f}" y="{bar_y}" width="{max(seg_w, 0.5):.2f}" height="{bar_h}" fill="{color}" />'
        )
        cursor_x += seg_w

    legend_items: list[str] = []
    for idx, (lang, count) in enumerate(top_langs):
        pct = (count / total_lines) * 100
        col = idx // 3
        row = idx % 3
        x = 25 + col * 155
        y = 84 + row * 26
        color = LANG_COLORS.get(lang, "#8b949e")
        safe_lang = html.escape(lang)
        legend_items.append(
            f'<circle cx="{x + 5}" cy="{y - 4}" r="5" fill="{color}" />'
            f'<text x="{x + 16}" y="{y}" class="lang-text">{safe_lang} {pct:.2f}%</text>'
        )

    footer = f"{total_commits} commits · {active_repos} repos · all branches"

    return f"""<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Most Used Languages">
  <style>
    .title {{ font: 600 18px 'Segoe UI', Ubuntu, Sans-Serif; fill: #ffffff; }}
    .lang-text {{ font: 400 12px 'Segoe UI', Ubuntu, Sans-Serif; fill: #c9d1d9; }}
    .footer {{ font: 400 11px 'Segoe UI', Ubuntu, Sans-Serif; fill: #8b949e; }}
  </style>
  <rect x="0.5" y="0.5" rx="4.5" width="{width - 1}" height="{height - 1}" fill="#0d1117" stroke="#c9d1d9" stroke-opacity="0.85" />
  <text x="25" y="34" class="title">Most Used Languages</text>
  <defs>
    <clipPath id="bar-clip">
      <rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="{bar_h}" rx="4" />
    </clipPath>
  </defs>
  <g clip-path="url(#bar-clip)">
    {"".join(bar_segments)}
  </g>
  <g>
    {"".join(legend_items)}
  </g>
  <text x="25" y="174" class="footer">{html.escape(footer)}</text>
</svg>
"""


def main() -> None:
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        sys.exit("[ERROR] Brak zmiennej GH_TOKEN.")

    author_emails = parse_csv_env("AUTHOR_EMAILS")
    author_names = parse_csv_env("AUTHOR_NAMES")
    if not author_emails and not author_names:
        sys.exit("[ERROR] Podaj przynajmniej AUTHOR_EMAILS lub AUTHOR_NAMES.")

    extra_repos = parse_csv_env("EXTRA_REPOS", lower=False)
    exclude_repos = parse_csv_env("EXCLUDE_REPOS")
    include_forks = os.environ.get("INCLUDE_FORKS", "false").lower() == "true"
    max_size_kb = int(os.environ.get("MAX_REPO_SIZE_KB", "500000"))
    max_lines_per_file = int(os.environ.get("MAX_LINES_PER_FILE", "3000"))
    output_path = Path(os.environ.get("OUTPUT_PATH", "assets/languages.svg"))

    auth_b64 = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    auth_header = f"AUTHORIZATION: basic {auth_b64}"

    repos = fetch_accessible_repos(token, include_forks, exclude_repos, extra_repos, max_size_kb)
    print(f"[INFO] Znaleziono {len(repos)} repozytoriów do przeskanowania.")

    total_langs: Counter[str] = Counter()
    seen_commits: set[str] = set()
    active_repos = 0

    for full_name in repos:
        repo_langs, repo_commits = analyze_repo(
            full_name, auth_header, author_emails, author_names, seen_commits, max_lines_per_file
        )
        if repo_commits > 0:
            active_repos += 1
            total_langs.update(repo_langs)
            summary = ", ".join(f"{k}:{v}" for k, v in repo_langs.most_common(4)) or "brak kodu z mapy"
            print(f"[OK] {full_name}: {repo_commits} commitów ({summary})")

    print(f"\n[PODSUMOWANIE] Unikalne commity: {len(seen_commits)} | Aktywne repo: {active_repos}")
    for lang, lines in total_langs.most_common():
        print(f"  - {lang}: {lines} linii")

    svg = render_svg(total_langs, len(seen_commits), active_repos)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(svg, encoding="utf-8")
    print(f"[DONE] Zapisano kartę do {output_path}")


if __name__ == "__main__":
    main()
