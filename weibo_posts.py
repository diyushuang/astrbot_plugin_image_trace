"""微博文案库：CSV / MD 解析、映射表读写与短链提取。

签到链路要尽量快，所以把「文案 → 图库图片」的重活放在离线映射表里：
签到只读本地 JSON，不解析 CSV/MD、不请求微博、不算 pHash。

本模块只做纯数据处理，不访问网络、不依赖 AstrBot，方便单测与离线脚本复用。

支持的两种导出格式：

1. CSV（5 列）：``序号,发布时间,来源,文案,微博链接``
2. MD（微博原创文案导出）：
   - ``## 2026 年（N 条）`` 年份分组
   - ``1. 正文行`` + 缩进续行 + ``—— YYYY-MM-DD HH:MM · 微博@账号``
   - 文末 ``## 去掉话题和链接后无正文的微博`` / ``## 微博表情码对照表`` 跳过

同一账号 + 同一发布时间在 CSV 与 MD 中重复时，**MD 优先**（MD 已清洗话题与链接，
正文更适合直接上卡片），其次保留正文更长的一条。
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from dataclasses import dataclass, replace
from urllib.parse import unquote, urlsplit

# MD 末尾元信息：`—— 2026-10-30 23:52 · 微博@赵今麦工作室official`
MD_META_RE = re.compile(
    r"——\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*·\s*微博@([^\s]+)"
)
MD_ENTRY_RE = re.compile(r"^\d+\.\s+")
MD_YEAR_RE = re.compile(r"^\d{4}\s*年")
MD_APPENDIX_PREFIXES = ("去掉话题和链接后无正文", "微博表情码对照表")
URL_RE = re.compile(r"https?://[^\s]+")
CODE_RE = re.compile(r"^[0-9A-Za-z_-]{4,}$")


@dataclass(frozen=True)
class WeiboImage:
    """一条微博在图库中匹配到的图片。"""

    file_name: str
    image_url: str
    thumb_url: str = ""
    score: float = 0.0

    @property
    def direct_url(self) -> str:
        """优先缩略图（720px 下载更快），没有缩略图才退回原图。"""
        return self.thumb_url or self.image_url


@dataclass(frozen=True)
class WeiboPost:
    """一条微博文案；image 为 None 表示图库暂时没有匹配到图。"""

    link: str
    account: str
    published_at: str  # YYYY-MM-DD HH:MM
    date: str  # YYYYMMDD
    text: str
    image: WeiboImage | None = None
    code: str = ""
    source: str = ""

    @property
    def date_display(self) -> str:
        return f"{self.date[:4]}-{self.date[4:6]}-{self.date[6:8]}" if len(self.date) == 8 else ""

    @property
    def account_display(self) -> str:
        return f"微博@{self.account}" if self.account else ""


def normalize_account(raw: str) -> str:
    """去掉 ``微博@`` / ``@`` 前缀与首尾空白。"""
    text = str(raw or "").strip()
    if text.startswith("微博@"):
        text = text[3:]
    text = text.lstrip("@").strip()
    return text


def normalize_published_at(raw: str) -> str:
    """只保留 ``YYYY-MM-DD HH:MM``；无法识别时返回去空白原串。"""
    text = str(raw or "").strip()
    match = re.search(r"(\d{4}-\d{2}-\d{2})\s*(\d{2}:\d{2})?", text)
    if not match:
        return text
    date = match.group(1)
    clock = match.group(2) or "00:00"
    return f"{date} {clock}"


def date_key(published_at: str) -> str:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", str(published_at or ""))
    return match.group(1).replace("-", "") if match else ""


def extract_short_code(link: str) -> str:
    """从微博链接取短链/微博 ID。

    实测 ``https://m.weibo.cn/statuses/show?id=<短链>`` **短链可直接用**：
    ``.../6670517617/Rp7r9Dbl5`` → ``Rp7r9Dbl5``；
    ``.../detail/5359867919338383`` → ``5359867919338383``。
    """
    raw = str(link or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        return raw if CODE_RE.match(raw) else ""
    try:
        parts = [unquote(part) for part in urlsplit(raw).path.split("/") if part]
    except Exception:
        return ""
    if not parts:
        return ""
    for index, part in enumerate(parts[:-1]):
        if part.lower() in {"status", "detail", "statuses"}:
            return parts[index + 1].strip()
    last = parts[-1].strip()
    return last if CODE_RE.match(last) else ""


def _clean_body_lines(raw_body: str) -> str:
    """去掉 MD 条目的缩进与硬换行空格，保留正文换行。"""
    lines = [line.strip() for line in str(raw_body or "").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def parse_weibo_md(text: str, source: str = "") -> list[WeiboPost]:
    posts: list[WeiboPost] = []
    block: list[str] = []
    in_body = False

    def flush() -> None:
        nonlocal block
        if not block:
            return
        raw = "\n".join(block)
        block = []
        match = MD_META_RE.search(raw)
        if not match:
            return
        published = f"{match.group(1)} {match.group(2)}"
        account = normalize_account(match.group(3))
        body = _clean_body_lines(raw[: match.start()])
        body = MD_ENTRY_RE.sub("", body, count=1)
        if not body:
            return
        posts.append(
            WeiboPost(
                link="",
                account=account,
                published_at=published,
                date=date_key(published),
                text=body,
                source=source,
            )
        )

    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        if line.startswith("# "):
            flush()
            in_body = False
            continue
        if line.startswith("## "):
            flush()
            title = line[3:].strip()
            in_body = bool(MD_YEAR_RE.match(title)) and not title.startswith(
                MD_APPENDIX_PREFIXES
            )
            continue
        if not in_body:
            continue
        if MD_ENTRY_RE.match(line):
            flush()
            block.append(line)
        elif block and (line.startswith("  ") or not line.strip()):
            block.append(line)
        elif block and line.strip():
            # 非缩进行只可能是块内正文的异常形态；继续收进块，交给元信息解析
            block.append(line)
    flush()
    return posts


def parse_weibo_csv(text: str, source: str = "") -> list[WeiboPost]:
    posts: list[WeiboPost] = []
    reader = csv.DictReader(io.StringIO(str(text or "")))
    if not reader.fieldnames:
        return posts
    fields = {str(name or "").strip(): name for name in reader.fieldnames}
    required = ("发布时间", "来源", "文案")
    if any(name not in fields for name in required):
        return posts
    for row in reader:
        published = normalize_published_at(row.get(fields["发布时间"]))
        account = normalize_account(row.get(fields["来源"]))
        body = str(row.get(fields["文案"]) or "").strip()
        link = ""
        link_field = fields.get("微博链接")
        if link_field:
            link = str(row.get(link_field) or "").strip()
        if not body or not published or not account:
            continue
        posts.append(
            WeiboPost(
                link=link,
                account=account,
                published_at=published,
                date=date_key(published),
                text=body,
                code=extract_short_code(link),
                source=source,
            )
        )
    return posts


def parse_weibo_text(text: str, filename: str = "") -> list[WeiboPost]:
    """按后缀或内容识别 CSV / MD。"""
    source = os.path.basename(str(filename or ""))
    suffix = os.path.splitext(source)[1].lower()
    raw = str(text or "")
    if suffix == ".csv":
        return parse_weibo_csv(raw, source)
    if suffix in {".md", ".markdown"}:
        return parse_weibo_md(raw, source)
    first_line = next((line for line in raw.splitlines() if line.strip()), "")
    if "序号" in first_line and "发布时间" in first_line:
        return parse_weibo_csv(raw, source)
    return parse_weibo_md(raw, source)


def _carry_link(post: WeiboPost, legacy: WeiboPost) -> WeiboPost:
    """合并重复条目时保留 CSV 里的微博链接（MD 导出本身不含链接）。"""
    link = post.link or legacy.link
    code = post.code or legacy.code or extract_short_code(link)
    if link == post.link and code == post.code:
        return post
    return replace(post, link=link, code=code)


def dedupe_posts(posts: list[WeiboPost]) -> list[WeiboPost]:
    """同账号 + 同发布时间只保留一条：MD 优先，其次正文更长。

    MD 正文更干净，但 MD 导出不含微博链接；CSV 有链接。合并时必须把 CSV 的
    link/code 带给 MD 条目，否则映射脚本会因为 ``code`` 为空跳过全部微博。
    """
    best: dict[tuple[str, str], WeiboPost] = {}
    for post in posts:
        key = (post.account.casefold(), post.published_at)
        old = best.get(key)
        if old is None:
            best[key] = post
            continue
        old_md = old.source.lower().endswith((".md", ".markdown"))
        new_md = post.source.lower().endswith((".md", ".markdown"))
        if new_md and not old_md:
            best[key] = _carry_link(post, old)
            continue
        if old_md and not new_md:
            best[key] = _carry_link(old, post)
            continue
        longer_text = len(post.text) > len(old.text)
        same_length_with_new_link = (
            len(post.text) == len(old.text) and bool(post.link) and not old.link
        )
        if longer_text or same_length_with_new_link:
            best[key] = _carry_link(post, old)
    # 最后再补一轮：确保每条都有 code（CSV 的链接可能来自被覆盖的旧条目）
    return [
        post if post.code else replace(post, code=extract_short_code(post.link))
        for post in best.values()
    ]


def load_posts_from_dir(directory: str) -> tuple[list[WeiboPost], list[str], list[str]]:
    """扫描目录下所有 CSV/MD，返回 (去重文案, 成功文件, 错误说明)。"""
    posts: list[WeiboPost] = []
    files: list[str] = []
    errors: list[str] = []
    directory = str(directory or "").strip()
    if not directory or not os.path.isdir(directory):
        return posts, files, errors
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith((".csv", ".md", ".markdown")):
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8-sig") as handle:
                text = handle.read()
            parsed = parse_weibo_text(text, name)
            posts.extend(parsed)
            files.append(path)
        except (OSError, UnicodeError, csv.Error) as exc:
            errors.append(f"{name}: {exc}")
    return dedupe_posts(posts), files, errors


def post_to_map_item(post: WeiboPost) -> dict:
    image = None
    if post.image is not None:
        image = {
            "file_name": post.image.file_name,
            "image_url": post.image.image_url,
            "thumb_url": post.image.thumb_url,
            "score": round(float(post.image.score or 0.0), 4),
        }
    return {
        "link": post.link,
        "code": post.code or extract_short_code(post.link),
        "account": post.account,
        "published_at": post.published_at,
        "date": post.date or date_key(post.published_at),
        "text": post.text,
        "image": image,
    }


def save_post_map(
    path: str,
    posts: list[WeiboPost],
    *,
    sources: list[str] | None = None,
    extra: dict | None = None,
) -> None:
    """原子写入映射表；签到侧只读，不会被半截文件误导。"""
    payload = {
        "version": 1,
        "generated_at": int(time.time()),
        "sources": [os.path.basename(str(s)) for s in (sources or [])],
        "count": len(posts),
        "matched_count": sum(1 for post in posts if post.image is not None),
        "items": [post_to_map_item(post) for post in posts],
    }
    if extra:
        payload.update(extra)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    temp = f"{path}.tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(temp, path)


def load_post_map(path: str) -> tuple[list[WeiboPost], float, dict]:
    """读取映射表；文件缺失/损坏返回空列表，签到侧按「未生成」处理。"""
    target = str(path or "").strip()
    if not target or not os.path.isfile(target):
        return [], 0.0, {}
    try:
        with open(target, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [], 0.0, {}
    if not isinstance(data, dict):
        return [], 0.0, {}
    items = data.get("items")
    if not isinstance(items, list):
        return [], 0.0, {}
    posts: list[WeiboPost] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        account = normalize_account(item.get("account"))
        published = normalize_published_at(item.get("published_at"))
        date = str(item.get("date") or "").strip() or date_key(published)
        if not text or not account or not published or not date:
            continue
        image = None
        raw_image = item.get("image")
        if isinstance(raw_image, dict):
            image_url = str(raw_image.get("image_url") or "").strip()
            thumb_url = str(raw_image.get("thumb_url") or "").strip()
            if image_url or thumb_url:
                try:
                    score = float(raw_image.get("score") or 0.0)
                except (TypeError, ValueError):  # 手工改坏 score 不该让整条链报销
                    score = 0.0
                image = WeiboImage(
                    file_name=str(raw_image.get("file_name") or "").strip(),
                    image_url=image_url,
                    thumb_url=thumb_url,
                    score=score,
                )
        posts.append(
            WeiboPost(
                link=str(item.get("link") or "").strip(),
                account=account,
                published_at=published,
                date=date,
                text=text,
                image=image,
                code=str(item.get("code") or "").strip()
                or extract_short_code(item.get("link")),
            )
        )
    try:
        generated_at = float(data.get("generated_at") or 0)
    except (TypeError, ValueError):
        generated_at = 0.0
    try:
        count = int(data.get("count") or len(posts))
    except (TypeError, ValueError):
        count = len(posts)
    matched_value = data.get("matched_count")
    try:
        matched_count = (
            int(matched_value)
            if matched_value is not None
            else sum(1 for post in posts if post.image is not None)
        )
    except (TypeError, ValueError):
        matched_count = sum(1 for post in posts if post.image is not None)
    sources = data.get("sources")
    meta = {
        "count": count,
        "matched_count": matched_count,
        "sources": [str(name) for name in sources] if isinstance(sources, list) else [],
    }
    return posts, generated_at, meta


def map_is_stale(path: str, source_dir: str) -> bool:
    """映射表比任一 CSV/MD 源文件旧时视为过期（只比较 mtime）。"""
    try:
        map_mtime = os.path.getmtime(path)
    except OSError:
        return True
    if not source_dir or not os.path.isdir(source_dir):
        return False
    for name in os.listdir(source_dir):
        if not name.lower().endswith((".csv", ".md", ".markdown")):
            continue
        try:
            if os.path.getmtime(os.path.join(source_dir, name)) > map_mtime:
                return True
        except OSError:
            continue
    return False


def replace_image(post: WeiboPost, image: WeiboImage | None) -> WeiboPost:
    """构建脚本用：只替换 image，其余字段原样保留。"""
    return replace(post, image=image)
