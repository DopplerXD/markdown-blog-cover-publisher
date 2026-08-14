#!/usr/bin/env python3
"""Standalone Markdown blog cover workflow.

The configuration block is intentionally near the top of this single file.
The script uses only Python's standard library, so it does not depend on the
skill system or third-party SDKs.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ============================ User configuration ============================

SCRIPT_DIR = Path(__file__).resolve().parent

# Repository and output locations. REPO_PATH may point anywhere on disk.
REPO_PATH = SCRIPT_DIR
TEMP_IMAGE_DIR = SCRIPT_DIR / ".cover-tmp"
FINAL_IMAGE_ROOT: str | Path | None = None

# Git configuration. The script never stages itself or TEMP_IMAGE_DIR.
GIT_REMOTE = "origin"
GIT_BRANCH = ""  # Empty means the current branch.

# Long-Markdown protection. The image model never receives the full long body.
ENABLE_QWEN_SUMMARY = True
QWEN_SUMMARY_MODEL = "qwen3.5-flash"
QWEN_SUMMARY_MAX_CHARS = 1200
LONG_MARKDOWN_THRESHOLD = 1000

# DashScope-compatible summary endpoint. Keep the key out of logs.
QWEN_SUMMARY_API_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
QWEN_SUMMARY_API_KEY = ""

# DashScope Qwen Image 3.0 endpoint and image settings.
IMAGE_MODEL = "qwen-image-3.0"
IMAGE_API_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
IMAGE_API_KEY = ""
IMAGE_SIZE = "1024*576"
IMAGE_OUTPUT_FORMAT = "png"
IMAGE_PROMPT_MAX_CHARS = 4200

# General HTTP API customization. Set a different request/response adapter below
# if another provider does not use the DashScope message format.
HTTP_TIMEOUT_SECONDS = 180
SUMMARY_RESPONSE_PATH = ("output", "choices", 0, "message", "content", 0, "text")
IMAGE_RESPONSE_PATH = ("output", "choices", 0, "message", "content", 0, "image")
SUMMARY_EXTRA_HEADERS: dict[str, str] = {}
IMAGE_EXTRA_HEADERS: dict[str, str] = {}

# Do not set this to True in a repository that is not trusted. Hard-coded API
# keys are convenient but can be committed or copied with this file.
WARN_ABOUT_HARDCODED_KEYS = True


MARKDOWN_SUFFIXES = {".md", ".markdown"}
FRONT_MATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n(?:---|\.\.\.)\r?\n", re.DOTALL)
IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+['\"][^)]*['\"])?\)")
TITLE_RE = re.compile(r"(?m)^title\s*:\s*(?:['\"]([^'\"]+)['\"]|([^#\n]+))")
HEADING_RE = re.compile(r"(?m)^#{1,2}\s+(.+?)\s*$")
SECRET_KEYS = {"authorization", "api_key", "key", "token"}


@dataclass
class Article:
    repo: Path
    path: Path
    title: str
    front_matter: str
    body: str
    source_text: str
    summary: str = ""
    summary_source: str = "local"
    prompt: str = ""
    candidates: list[Path] = field(default_factory=list)


@dataclass
class RunState:
    repo: Path
    original_head: str
    original_branch: str
    original_files: dict[Path, bytes | None] = field(default_factory=dict)
    created_files: list[Path] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    pushed_commits: list[str] = field(default_factory=list)


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def log(message: str) -> None:
    print(f"[cover] {message}")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in SECRET_KEYS else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def run_git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def resolve_repo() -> Path:
    path = Path(REPO_PATH).expanduser().resolve()
    if not path.exists():
        raise RuntimeError(f"REPO_PATH does not exist: {path}")
    return Path(run_git(path, "rev-parse", "--show-toplevel")).resolve()


def read_front_matter(text: str) -> tuple[str, int]:
    match = FRONT_MATTER_RE.match(text)
    return (match.group(0), match.end()) if match else ("", 0)


def article_title(text: str, path: Path) -> str:
    front, body_start = read_front_matter(text)
    match = TITLE_RE.search(front)
    if match:
        return (match.group(1) or match.group(2)).strip()
    heading = re.search(r"(?m)^#\s+(.+?)\s*$", text[body_start:])
    return heading.group(1).strip() if heading else path.stem.replace("-", " ").replace("_", " ")


def has_cover(text: str) -> bool:
    front, body_start = read_front_matter(text)
    if re.search(r"(?mi)^\s*cover\s*:", front):
        return True
    lines = [line for line in text[body_start:].splitlines() if line.strip()]
    return any(IMAGE_RE.search(line) for line in lines[:8])


def body_for_count(body: str) -> str:
    return body


def local_summary(article: Article, limit: int = QWEN_SUMMARY_MAX_CHARS) -> str:
    headings = "；".join(match.group(1).strip() for match in HEADING_RE.finditer(article.body))
    cleaned = re.sub(r"```.*?```", " ", article.body, flags=re.DOTALL)
    cleaned = re.sub(r"!\[[^]]*\]\([^)]*\)", " ", cleaned)
    cleaned = re.sub(r"[#>*`_\[\]()~-]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    pieces = [f"主题：{article.title}"]
    if headings:
        pieces.append(f"章节：{headings[:500]}")
    if cleaned:
        pieces.append(f"内容：{cleaned}")
    return truncate(" ".join(pieces), limit)


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def json_path(value: Any, path: tuple[Any, ...]) -> Any:
    current = value
    for part in path:
        current = current[part]
    return current


def api_key(configured: str, env_name: str) -> str:
    return configured or os.environ.get(env_name, "")


def post_json(url: str, key: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    request_headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"request failed for {url}: {exc.reason}") from exc
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"non-JSON response from {url}: {raw[:500]}") from exc
    log(f"API response received: {url} -> {json.dumps(redact(result), ensure_ascii=False)[:800]}")
    if isinstance(result, dict) and result.get("code") and not result.get("output"):
        raise RuntimeError(f"API returned {result.get('code')}: {result.get('message', '')}")
    return result


def summary_prompt(article: Article) -> str:
    return (
        f"请阅读下面这篇 Markdown 文章，为博客封面设计生成一段总结性文字。\n"
        f"提炼主题、核心问题、关键机制和技术关键词，并给出适合视觉化表达的内容。\n"
        f"不要输出 Markdown 代码块，不要复述全文，不要添加‘以下是总结’等包装文字。\n"
        f"只返回总结正文，最多 {QWEN_SUMMARY_MAX_CHARS} 个 Unicode 字符。\n\n"
        f"文章标题：{article.title}\n文章正文：\n{article.body}"
    )


def build_summary_payload(article: Article) -> dict[str, Any]:
    """Build the default DashScope summary request.

    Edit this function for a provider with a different JSON request shape.
    """
    return {
        "model": QWEN_SUMMARY_MODEL,
        "input": {"messages": [{"role": "user", "content": [{"text": summary_prompt(article)}]}]},
        "parameters": {"enable_thinking": False},
    }


def summarize_long_article(article: Article) -> str:
    article.summary = local_summary(article)
    if len(body_for_count(article.body)) <= LONG_MARKDOWN_THRESHOLD:
        return article.summary
    if not ENABLE_QWEN_SUMMARY:
        log(f"{article.path}: {len(article.body)} chars, Qwen summary disabled; using local summary")
        return article.summary

    key = api_key(QWEN_SUMMARY_API_KEY, "DASHSCOPE_API_KEY")
    if not key:
        log(f"{article.path}: Qwen summary key missing; using local summary without sending long body")
        return article.summary
    payload = build_summary_payload(article)
    try:
        response = post_json(QWEN_SUMMARY_API_URL, key, payload, SUMMARY_EXTRA_HEADERS)
        result = json_path(response, SUMMARY_RESPONSE_PATH)
        if isinstance(result, list):
            result = " ".join(str(item) for item in result)
        if not str(result).strip():
            raise RuntimeError("summary model returned empty text")
        article.summary = truncate(str(result), QWEN_SUMMARY_MAX_CHARS)
        article.summary_source = QWEN_SUMMARY_MODEL
        log(f"{article.path}: summarized {len(article.body)} -> {len(article.summary)} chars with {QWEN_SUMMARY_MODEL}")
    except Exception as exc:
        log(f"{article.path}: summary failed ({exc}); using local bounded summary")
    return article.summary


def image_prompt(article: Article, redraw_guidance: str = "") -> str:
    prompt = (
        "Create a wide 16:9 static blog article cover. Use a polished colorful cartoon "
        "and technology editorial illustration style, friendly chibi proportions, crisp "
        "shapes, one clear focal metaphor, expressive but uncluttered composition, and "
        "readability at thumbnail size. Avoid dense flowcharts, large code blocks, tiny "
        "labels, generic server racks, photorealism, watermarks, copied logos, and official "
        "character designs. Prefer short keywords or no text.\n"
        f"Article title: {article.title}\n"
        f"Article summary: {article.summary}\n"
        f"Relevant headings: {truncate('；'.join(m.group(1).strip() for m in HEADING_RE.finditer(article.body)), 500)}\n"
        f"Additional redraw guidance: {redraw_guidance.strip() or 'none'}"
    )
    return truncate(prompt, IMAGE_PROMPT_MAX_CHARS)


def build_image_payload(prompt: str) -> dict[str, Any]:
    """Build the default DashScope Qwen Image request.

    Edit this function for a provider with a different JSON request shape.
    """
    return {
        "model": IMAGE_MODEL,
        "input": {"messages": [{"role": "user", "content": [{"text": prompt}]}]},
        "parameters": {
            "size": IMAGE_SIZE,
            "n": 1,
            "prompt_extend": True,
            "watermark": False,
        },
    }


def generate_image_url(article: Article, prompt: str) -> str:
    key = api_key(IMAGE_API_KEY, "DASHSCOPE_API_KEY")
    if not key:
        raise RuntimeError("image API key missing; set IMAGE_API_KEY or DASHSCOPE_API_KEY")
    payload = build_image_payload(prompt)
    response = post_json(IMAGE_API_URL, key, payload, IMAGE_EXTRA_HEADERS)
    image_url = json_path(response, IMAGE_RESPONSE_PATH)
    if not isinstance(image_url, str) or not image_url.startswith(("http://", "https://", "data:")):
        raise RuntimeError(f"image response did not contain a usable URL: {image_url!r}")
    return image_url


def receive_timestamp() -> str:
    return dt.datetime.now().strftime("%y%m%d%H%M%S")


def filename_for(article: Article, image_url: str) -> Path:
    if image_url.startswith("data:"):
        mime = image_url.split(";", 1)[0][5:].lower()
        suffix = ".jpg" if mime in {"image/jpeg", "image/jpg"} else ".png"
    else:
        suffix = Path(urllib.parse.urlparse(image_url).path).suffix.lower()
    suffix = suffix if suffix in {".png", ".jpg", ".jpeg"} else f".{IMAGE_OUTPUT_FORMAT.lstrip('.') or 'png'}"
    while True:
        destination = TEMP_IMAGE_DIR / f"{article.path.stem}-cover-{receive_timestamp()}{suffix}"
        if not destination.exists():
            return destination
        time.sleep(1)


def download_image(image_url: str, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if image_url.startswith("data:"):
        header, encoded = image_url.split(",", 1)
        destination.write_bytes(base64.b64decode(encoded))
        return destination
    request = urllib.request.Request(image_url, headers={"User-Agent": "markdown-blog-cover-publisher/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            data = response.read()
            content_type = response.headers.get_content_type()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"image download failed: {exc.reason}") from exc
    if not data:
        raise RuntimeError("image download returned empty data")
    if content_type in {"image/jpeg", "image/jpg"} and destination.suffix == ".png":
        destination = destination.with_suffix(".jpg")
    destination.write_bytes(data)
    return destination


def safe_title(title: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|\r\n]+", "-", title).strip(" .-")
    return value or "article"


def final_image_path(article: Article, temporary: Path) -> Path:
    root = Path(FINAL_IMAGE_ROOT).expanduser() if FINAL_IMAGE_ROOT else article.path.parent
    if not root.is_absolute():
        root = article.repo / root
    root = root.resolve()
    return root / "assets" / safe_title(article.title) / temporary.name


def relative_image_reference(article: Article, destination: Path) -> str:
    relative = os.path.relpath(destination, article.path.parent).replace(os.sep, "/")
    return f"![{article.title} cover]({relative})"


def install_cover(article: Article, temporary: Path, state: RunState) -> tuple[Path, str]:
    destination = final_image_path(article, temporary)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError(f"refusing to overwrite existing image: {destination}")
    state.original_files.setdefault(destination, destination.read_bytes() if destination.exists() else None)
    shutil.copy2(temporary, destination)
    state.created_files.append(destination)

    original = article.path.read_text(encoding="utf-8")
    state.original_files.setdefault(article.path, original.encode("utf-8"))
    front, body_start = read_front_matter(original)
    body = original[body_start:].lstrip("\r\n")
    reference = relative_image_reference(article, destination)
    new_text = (original[:body_start] + "\n" if front else "") + reference + "\n\n" + body
    article.path.write_text(new_text, encoding="utf-8")
    return destination, reference


def discover_articles(repo: Path) -> list[Article]:
    candidates: set[Path] = set()
    for command in (
        ("diff", "--name-only", "--diff-filter=A", "HEAD", "--", "*.md", "*.markdown"),
        ("ls-files", "--others", "--exclude-standard", "--", "*.md", "*.markdown"),
    ):
        output = run_git(repo, *command, check=False)
        candidates.update((repo / line).resolve() for line in output.splitlines() if line.strip())
    articles = []
    for path in sorted(candidates):
        if not path.is_file() or path.suffix.lower() not in MARKDOWN_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8")
        if has_cover(text):
            log(f"skip existing cover: {path.relative_to(repo)}")
            continue
        front, body_start = read_front_matter(text)
        articles.append(Article(repo, path, article_title(text, path), front, text[body_start:], text))
    return articles


def snapshot_state(repo: Path, articles: list[Article]) -> RunState:
    head = run_git(repo, "rev-parse", "HEAD")
    branch = run_git(repo, "branch", "--show-current")
    state = RunState(repo, head, branch)
    for article in articles:
        state.original_files[article.path] = article.path.read_bytes()
    return state


def commit_article(article: Article, image: Path, state: RunState) -> str:
    run_git(state.repo, "add", "--", str(article.path.relative_to(state.repo)), str(image.relative_to(state.repo)))
    run_git(state.repo, "diff", "--cached", "--check")
    run_git(state.repo, "commit", "-m", f"feat(content): add cover for {article.path.stem}")
    commit = run_git(state.repo, "rev-parse", "HEAD")
    state.commits.append(commit)
    return commit


def push_commit(state: RunState) -> None:
    branch = GIT_BRANCH or state.original_branch
    if not branch:
        raise RuntimeError("detached HEAD; configure GIT_BRANCH before pushing")
    run_git(state.repo, "push", GIT_REMOTE, branch)
    state.pushed_commits.extend(state.commits[-1:])


def restore_files(state: RunState) -> None:
    for path, original in state.original_files.items():
        if original is None:
            if path.exists():
                path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(original)
    for path in state.created_files:
        if path.exists() and path not in state.original_files:
            path.unlink()


def rollback(state: RunState) -> None:
    if state.commits:
        if state.pushed_commits:
            log("rollback requested after push; creating compensating revert commits")
        for commit in reversed(state.commits):
            run_git(state.repo, "revert", "--no-edit", commit)
        if state.pushed_commits:
            branch = GIT_BRANCH or state.original_branch
            run_git(state.repo, "push", GIT_REMOTE, branch)
    restore_files(state)


def show_candidate(article: Article, temporary: Path) -> None:
    print("\n" + "=" * 72)
    print(f"文章：{article.path}")
    print(f"标题：{article.title}")
    print(f"图片：{temporary}")
    print(f"摘要来源：{article.summary_source}；摘要长度：{len(article.summary)}")
    print(f"提示词：{truncate(article.prompt, 900)}")
    print("输入：1 保存并提交并推送 | 2 保存并提交 | 3 仅保存 | 4 重绘 | 5 跳过并保留 | 6 停止 | 7 停止并回滚")


def approve(article: Article, temporary: Path, state: RunState) -> str:
    while True:
        show_candidate(article, temporary)
        choice = input("审批 [1-7]：").strip()
        if choice == "4":
            input("按回车后输入新的重绘提示指导：")
            guidance = input("新的重绘提示指导：").strip()
            article.prompt = image_prompt(article, guidance)
            try:
                url = generate_image_url(article, article.prompt)
                new_temporary = filename_for(article, url)
                new_temporary = download_image(url, new_temporary)
                article.candidates.append(new_temporary)
                temporary = new_temporary
                log(f"redraw saved to temporary file: {temporary}")
            except Exception as exc:
                log(f"redraw failed: {exc}; current candidate remains available")
            continue
        if choice in {"1", "2", "3"}:
            image, reference = install_cover(article, temporary, state)
            if choice in {"1", "2"}:
                commit = commit_article(article, image, state)
                log(f"committed {commit}: {article.path} + {image}")
                if choice == "1":
                    push_commit(state)
                    log(f"pushed {commit} to {GIT_REMOTE}")
            state.original_files.setdefault(temporary, temporary.read_bytes())
            temporary.unlink(missing_ok=True)
            log(f"saved {image}; Markdown reference: {reference}")
            return choice
        if choice == "5":
            log("skipped current article; temporary image retained")
            return choice
        if choice == "6":
            log("stopped; previous approvals remain")
            return choice
        if choice == "7":
            rollback(state)
            log("rolled back this run")
            return choice
        print("请输入 1、2、3、4、5、6 或 7。")


def process_article(article: Article) -> None:
    summarize_long_article(article)
    article.prompt = image_prompt(article)
    if len(article.prompt) > IMAGE_PROMPT_MAX_CHARS:
        raise RuntimeError("image prompt exceeded local safety limit")


def main() -> int:
    if WARN_ABOUT_HARDCODED_KEYS and (QWEN_SUMMARY_API_KEY or IMAGE_API_KEY):
        log("warning: an API key is hard-coded in this script; do not commit or share it")
    repo = resolve_repo()
    articles = discover_articles(repo)
    if not articles:
        log("no new uncovered Markdown articles found")
        return 0
    TEMP_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    state = snapshot_state(repo, articles)
    log(f"found {len(articles)} new uncovered Markdown article(s)")

    prepared: list[tuple[Article, Path]] = []
    for article in articles:
        try:
            process_article(article)
            image_url = generate_image_url(article, article.prompt)
            temporary = filename_for(article, image_url)
            temporary = download_image(image_url, temporary)
            article.candidates.append(temporary)
            prepared.append((article, temporary))
            log(f"generated temporary image: {temporary}")
        except Exception as exc:
            log(f"failed {article.path}: {exc}; continuing with other articles")

    for article, temporary in prepared:
        try:
            result = approve(article, temporary, state)
            if result in {"6", "7"}:
                break
        except Exception as exc:
            log(f"approval action failed for {article.path}: {exc}; temporary image retained")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[cover] interrupted; temporary images retained", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f"[cover] fatal: {exc}", file=sys.stderr)
        raise SystemExit(1)
