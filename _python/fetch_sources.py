from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import hashlib
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

import feedparser
import yaml


ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "_data" / "sources.yml"
POSTS_DIR = ROOT / "_posts"
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
SUMMARY_LENGTH = 160


def load_sources() -> list[dict[str, str]]:
    if not SOURCES_FILE.exists():
        raise FileNotFoundError(f"Missing sources file: {SOURCES_FILE}")

    payload = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8")) or {}
    sources = payload.get("sources", [])
    if not isinstance(sources, list):
        raise ValueError("The sources.yml file must define a top-level 'sources' list.")

    parsed_sources: list[dict[str, str]] = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Each source entry must be a mapping with 'id' and 'feed_url'.")

        source_id = str(source.get("id", "")).strip()
        feed_url = str(source.get("feed_url", "")).strip()
        if not source_id or not feed_url:
            raise ValueError("Each source entry must include non-empty 'id' and 'feed_url' values.")

        parsed_sources.append({"id": source_id, "feed_url": feed_url})

    return parsed_sources


def read_front_matter(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}

    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            payload = "\n".join(lines[1:index])
            data = yaml.safe_load(payload) or {}
            return data if isinstance(data, dict) else {}

    return {}


def normalize_url(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return ""

    parsed = urlsplit(stripped)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def collect_existing_links() -> set[str]:
    links: set[str] = set()
    for post_path in POSTS_DIR.glob("*.md"):
        front_matter = read_front_matter(post_path)
        link = front_matter.get("link")
        if isinstance(link, str):
            normalized = normalize_url(link)
            if normalized:
                links.add(normalized)
    return links


def extract_author(entry: feedparser.FeedParserDict, feed_title: str, source_id: str) -> str:
    author = str(entry.get("author", "")).strip()
    if author:
        return author

    for key in ("dc_creator", "creator"):
        candidate = str(entry.get(key, "")).strip()
        if candidate:
            return candidate

    authors = entry.get("authors", [])
    if isinstance(authors, list):
        for author_entry in authors:
            if isinstance(author_entry, dict):
                candidate = str(author_entry.get("name", "")).strip()
                if candidate:
                    return candidate

    if feed_title:
        return feed_title
    return source_id


def extract_entry_summary(entry: feedparser.FeedParserDict) -> str:
    for field_name in ("summary", "description"):
        value = str(entry.get(field_name, "")).strip()
        if value:
            plain_text = unescape(HTML_TAG_PATTERN.sub(" ", value))
            collapsed = " ".join(plain_text.split())
            if len(collapsed) <= SUMMARY_LENGTH:
                return collapsed
            return f"{collapsed[: SUMMARY_LENGTH - 3].rstrip()}..."

    return ""


def extract_published_datetime(entry: feedparser.FeedParserDict) -> datetime | None:
    for field_name in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed_value = entry.get(field_name)
        if parsed_value:
            return datetime(
                parsed_value.tm_year,
                parsed_value.tm_mon,
                parsed_value.tm_mday,
                parsed_value.tm_hour,
                parsed_value.tm_min,
                parsed_value.tm_sec,
                tzinfo=timezone.utc,
            )

    for field_name in ("published", "updated", "created"):
        raw_value = str(entry.get(field_name, "")).strip()
        if not raw_value:
            continue

        try:
            parsed = parsedate_to_datetime(raw_value)
        except (TypeError, ValueError, IndexError):
            continue

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    return None


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_value.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:80].strip("-") or "item"


def entry_identity(entry: feedparser.FeedParserDict, published_at: datetime) -> str:
    for key in ("id", "guid", "link"):
        value = str(entry.get(key, "")).strip()
        if value:
            return value

    title = str(entry.get("title", "")).strip()
    if title:
        return f"{title}|{published_at.isoformat()}"

    raise ValueError("Feed entry is missing an id, guid, link, and title.")


def build_post_path(title: str, published_at: datetime, identity: str) -> Path:
    slug = slugify(title)
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10]
    filename = f"{published_at:%Y-%m-%d}-{slug}-{digest}.md"
    return POSTS_DIR / filename


def write_post(post_path: Path, metadata: dict[str, str], body: str) -> None:
    front_matter = yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip()
    normalized_body = body.strip()
    if normalized_body:
        post_path.write_text(f"---\n{front_matter}\n---\n{normalized_body}\n", encoding="utf-8")
        return

    post_path.write_text(f"---\n{front_matter}\n---\n", encoding="utf-8")


def select_latest_entry(entries: list[feedparser.FeedParserDict]) -> tuple[feedparser.FeedParserDict, str, str, datetime] | None:
    latest: tuple[feedparser.FeedParserDict, str, str, datetime] | None = None

    for entry in entries:
        link = str(entry.get("link", "")).strip()
        normalized_link = normalize_url(link)
        if not normalized_link:
            continue

        published_at = extract_published_datetime(entry)
        if published_at is None:
            continue

        if latest is None or published_at > latest[3]:
            latest = (entry, link, normalized_link, published_at)

    return latest


def sync_source(source: dict[str, str], existing_links: set[str]) -> int:
    source_id = source["id"]
    feed_url = source["feed_url"]
    parsed_feed = feedparser.parse(feed_url)
    feed_title = str(parsed_feed.feed.get("title", "")).strip()

    if getattr(parsed_feed, "bozo", 0):
        print(f"Warning: parser reported an issue for {feed_url}: {parsed_feed.bozo_exception}")

    latest = select_latest_entry(parsed_feed.entries)
    if latest is None:
        print(f"Source '{source_id}' complete: no valid latest item found.")
        return 0

    entry, link, normalized_link, published_at = latest
    if normalized_link in existing_links:
        print(f"Source '{source_id}' complete: latest item already exists.")
        return 0

    title = str(entry.get("title", "")).strip() or normalized_link
    author = extract_author(entry, feed_title, source_id)
    body = extract_entry_summary(entry)
    identity = entry_identity(entry, published_at)
    post_path = build_post_path(title, published_at, identity)

    if post_path.exists():
        existing_front_matter = read_front_matter(post_path)
        existing_link = normalize_url(str(existing_front_matter.get("link", "")))
        if existing_link and existing_link != normalized_link:
            raise FileExistsError(
                f"Filename collision for {post_path.name}: existing link {existing_link} does not match {normalized_link}."
            )

        existing_links.add(normalized_link)
        print(f"Source '{source_id}' complete: latest item already exists.")
        return 0

    write_post(
        post_path,
        {
            "title": title,
            "link": link,
            "author": author,
            "date": published_at.strftime("%Y-%m-%d %H:%M:%S %z"),
        },
        body,
    )
    existing_links.add(normalized_link)

    print(f"Source '{source_id}' complete: created 1 post.")
    return 1


def main() -> int:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    sources = load_sources()
    existing_links = collect_existing_links()

    created_total = 0
    for source in sources:
        created_total += sync_source(source, existing_links)

    print(f"Sync complete: created {created_total} new posts across {len(sources)} source(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())