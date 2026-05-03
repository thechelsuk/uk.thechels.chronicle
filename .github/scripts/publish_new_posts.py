#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urldefrag, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "_python"))

from fetch_webmentions import derive_slug, load_site_url, read_front_matter  # noqa: E402


CACHE_DIR = ROOT / ".cache"
BRIDGY_CACHE_FILE = CACHE_DIR / "bridgy_fed_webmentions.yml"
OUTGOING_WEBMENTIONS_FILE = CACHE_DIR / "outgoing_webmentions.yml"
LOOKUPS_FILE = CACHE_DIR / "webmention_lookups.yml"
BAD_URIS_FILE = CACHE_DIR / "webmention_bad_uris.yml"
POSTS_DIR = ROOT / "_posts"

BRIDGY_FED_TARGET = "https://fed.brid.gy/"
USER_AGENT = "nuchronic-outgoing/1.0"
MAX_HTML_BYTES = 512 * 1024
LINK_HEADER_SPLIT_RE = re.compile(r",\s*(?=<)")
DEFAULT_RETRY_RECENT = 20
LOOKUP_CACHE_TTL = timedelta(hours=12)
BAD_URI_CACHE_TTL = timedelta(hours=6)
BRIDGY_ACCEPTED_RETRY_AFTER = timedelta(hours=2)
BRIDGY_CONVERSION_ENDPOINTS = {
    "ap": "https://web.brid.gy/convert/ap/",
    "bsky": "https://web.brid.gy/convert/bsky/",
}
BRIDGY_REQUIRED_TARGETS = tuple(BRIDGY_CONVERSION_ENDPOINTS)
OUTGOING_SOURCE_FORMAT_VERSION = 2


class EndpointParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.endpoint: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.endpoint:
            return

        if tag not in {"a", "link"}:
            return

        attr_map = {name.lower(): (value or "") for name, value in attrs}
        rel_value = attr_map.get("rel", "")
        rel_tokens = {token.strip().lower() for token in rel_value.split() if token.strip()}
        if "webmention" not in rel_tokens:
            return

        href = attr_map.get("href", "").strip()
        if href:
            self.endpoint = href


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso_timestamp(value: object) -> datetime | None:
    raw_value = str(value or "").strip()
    if not raw_value:
        return None

    if raw_value.endswith("Z"):
        raw_value = f"{raw_value[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timestamp_is_fresh(value: object, max_age: timedelta) -> bool:
    parsed = parse_iso_timestamp(value)
    if parsed is None:
        return False
    return datetime.now(timezone.utc) - parsed <= max_age


def normalize_targets(value: object) -> list[str]:
    normalized: list[str] = []
    if not isinstance(value, list):
        return normalized

    for item in value:
        target = str(item or "").strip().lower()
        if target and target in BRIDGY_CONVERSION_ENDPOINTS and target not in normalized:
            normalized.append(target)
    return normalized


def clean_url(value: str) -> str:
    raw_value = urldefrag(str(value or "").strip())[0]
    if not raw_value:
        return ""

    parsed = urlsplit(raw_value)
    query_pairs = [(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True) if not key.lower().startswith("utm_")]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query_pairs, doseq=True), ""))


def excerpt(value: str, limit: int = 280) -> str:
    collapsed = " ".join(str(value or "").split())
    return collapsed[:limit]


def load_yaml_map(path: Path, root_key: str) -> dict[str, Any]:
    payload: dict[str, Any]
    if path.exists():
        parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        payload = parsed if isinstance(parsed, dict) else {}
    else:
        payload = {}

    payload.setdefault("version", 1)
    payload.setdefault(root_key, {})
    if not isinstance(payload[root_key], dict):
        payload[root_key] = {}
    return payload


def write_yaml(path: Path, payload: object) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).strip() + "\n"
    current = path.read_text(encoding="utf-8") if path.exists() else None
    if current == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def run_git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def list_new_post_paths(before_sha: str, current_sha: str) -> list[Path]:
    if before_sha == "0" * 40:
        return []

    output = run_git("diff", "--diff-filter=A", "--name-only", before_sha, current_sha, "--", "_posts/")
    paths: list[Path] = []
    for line in output.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        path = ROOT / candidate
        if path.exists() and path.is_file():
            paths.append(path)
    return paths


def list_recent_post_paths(limit: int) -> list[Path]:
    if limit <= 0:
        return []

    posts = sorted((path for path in POSTS_DIR.glob("*.md") if path.is_file()), reverse=True)
    return posts[:limit]


def parse_link_headers(header_values: list[str], base_url: str) -> str:
    for value in header_values:
        for part in LINK_HEADER_SPLIT_RE.split(value):
            url_match = re.search(r"<([^>]+)>", part)
            rel_match = re.search(r";\s*rel=(?:\"([^\"]+)\"|([^;]+))", part, flags=re.IGNORECASE)
            if not url_match or not rel_match:
                continue

            rel_value = (rel_match.group(1) or rel_match.group(2) or "").strip()
            rel_tokens = {token.strip().lower() for token in rel_value.split() if token.strip()}
            if "webmention" in rel_tokens:
                return urljoin(base_url, url_match.group(1).strip())
    return ""


def request_url(url: str, *, data: bytes | None = None, accept: str = "*/*", timeout: int = 30) -> dict[str, Any]:
    request = Request(
        url,
        data=data,
        headers={
            "Accept": accept,
            "User-Agent": USER_AGENT,
        },
    )

    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_HTML_BYTES if data is None else 1024 * 1024)
            return {
                "status": getattr(response, "status", response.getcode()),
                "url": response.geturl(),
                "headers": response.headers,
                "body": body,
            }
    except HTTPError as error:
        body = error.read(1024 * 1024)
        return {
            "status": error.code,
            "url": error.geturl(),
            "headers": error.headers,
            "body": body,
        }
    except URLError as error:
        raise RuntimeError(str(error.reason)) from error


def decode_body(response: dict[str, Any]) -> str:
    headers = response["headers"]
    charset = headers.get_content_charset() if hasattr(headers, "get_content_charset") else None
    return response["body"].decode(charset or "utf-8", errors="replace")


def discover_webmention_endpoint(
    target_url: str,
    lookups_cache: dict[str, Any],
    bad_uris_cache: dict[str, Any],
) -> tuple[str, str, str]:
    target_key = clean_url(target_url)
    lookup_entry = lookups_cache["targets"].get(target_key)
    if isinstance(lookup_entry, dict) and timestamp_is_fresh(lookup_entry.get("checked_at"), LOOKUP_CACHE_TTL):
        endpoint = str(lookup_entry.get("endpoint", "")).strip()
        resolved_url = clean_url(str(lookup_entry.get("resolved_url", target_key)).strip()) or target_key
        if endpoint:
            return resolved_url, endpoint, "cached"
    else:
        lookups_cache["targets"].pop(target_key, None)

    bad_entry = bad_uris_cache["targets"].get(target_key)
    if isinstance(bad_entry, dict) and timestamp_is_fresh(bad_entry.get("checked_at"), BAD_URI_CACHE_TTL):
        resolved_url = clean_url(str(bad_entry.get("resolved_url", target_key)).strip()) or target_key
        return resolved_url, "", str(bad_entry.get("reason", "cached-bad-uri")).strip() or "cached-bad-uri"
    else:
        bad_uris_cache["targets"].pop(target_key, None)

    response = request_url(target_key, accept="text/html,application/xhtml+xml,*/*;q=0.8")
    final_url = clean_url(response["url"]) or target_key
    if response["status"] >= 400:
        bad_uris_cache["targets"][target_key] = {
            "resolved_url": final_url,
            "reason": f"discovery-http-{response['status']}",
            "checked_at": now_iso(),
        }
        return final_url, "", f"discovery-http-{response['status']}"

    header_values = response["headers"].get_all("Link", []) if hasattr(response["headers"], "get_all") else []
    endpoint = parse_link_headers(header_values, final_url)
    body_text = decode_body(response)
    if not endpoint:
        parser = EndpointParser()
        parser.feed(body_text)
        endpoint = urljoin(final_url, parser.endpoint) if parser.endpoint else ""

    lookup_record = {
        "resolved_url": final_url,
        "endpoint": endpoint,
        "checked_at": now_iso(),
    }
    lookups_cache["targets"][target_key] = lookup_record

    if endpoint:
        bad_uris_cache["targets"].pop(target_key, None)
        return final_url, endpoint, "discovered"

    bad_uris_cache["targets"][target_key] = {
        "resolved_url": final_url,
        "reason": "no-endpoint",
        "checked_at": now_iso(),
    }
    return final_url, "", "no-endpoint"


def post_form(endpoint: str, payload: dict[str, str]) -> dict[str, Any]:
    return request_url(endpoint, data=urlencode(payload).encode("utf-8"), accept="*/*")


def ensure_post_entry(cache: dict[str, Any], slug: str, source_url: str, container_key: str) -> dict[str, Any]:
    posts = cache["posts"]
    entry = posts.get(slug)
    if not isinstance(entry, dict):
        entry = {
            "source_url": source_url,
            container_key: {},
        }
        posts[slug] = entry

    entry["source_url"] = source_url
    if not isinstance(entry.get(container_key), dict):
        entry[container_key] = {}
    return entry


def has_successful_bridgy_delivery(bridgy_cache: dict[str, Any], slug: str) -> bool:
    post_entry = bridgy_cache["posts"].get(slug)
    if not isinstance(post_entry, dict):
        return False

    bridgy_entry = post_entry.get("bridgy")
    if not isinstance(bridgy_entry, dict) or bridgy_entry.get("status") != "success":
        return False

    verified_targets = set(normalize_targets(bridgy_entry.get("verified_targets")))
    return set(BRIDGY_REQUIRED_TARGETS).issubset(verified_targets)


def verify_bridgy_delivery(source_url: str, existing_record: dict[str, Any] | None) -> tuple[bool, list[str], dict[str, str]]:
    verified_targets = normalize_targets((existing_record or {}).get("verified_targets"))
    verification_errors: dict[str, str] = {}

    for target in BRIDGY_REQUIRED_TARGETS:
        if target in verified_targets:
            continue

        verification_url = f"{BRIDGY_CONVERSION_ENDPOINTS[target]}{source_url}"
        try:
            response = request_url(verification_url, accept="application/json,*/*;q=0.1", timeout=15)
        except RuntimeError as error:
            verification_errors[target] = str(error)
            continue

        if 200 <= response["status"] < 300:
            verified_targets.append(target)
        elif response["status"] not in {404, 410}:
            verification_errors[target] = f"HTTP {response['status']}"

    normalized_verified_targets = [target for target in BRIDGY_REQUIRED_TARGETS if target in verified_targets]
    is_verified = set(BRIDGY_REQUIRED_TARGETS).issubset(normalized_verified_targets)
    return is_verified, normalized_verified_targets, verification_errors


def collect_post_candidates(
    before_sha: str,
    current_sha: str,
    retry_recent: int,
) -> list[tuple[Path, bool]]:
    candidates: list[tuple[Path, bool]] = []
    seen_slugs: set[str] = set()

    for post_path in list_new_post_paths(before_sha, current_sha):
        slug = derive_slug(post_path)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        candidates.append((post_path, True))

    for post_path in list_recent_post_paths(retry_recent):
        slug = derive_slug(post_path)
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        candidates.append((post_path, False))

    return candidates


def send_bridgy_fed_webmention(
    slug: str,
    source_url: str,
    bridgy_cache: dict[str, Any],
    lookups_cache: dict[str, Any],
    bad_uris_cache: dict[str, Any],
    *,
    dry_run: bool,
) -> tuple[bool, int]:
    post_entry = ensure_post_entry(bridgy_cache, slug, source_url, "bridgy")
    existing = post_entry.get("bridgy")
    existing_record = existing if isinstance(existing, dict) else None
    verified, verified_targets, verification_errors = verify_bridgy_delivery(source_url, existing_record)
    if verified:
        updated_record = dict(existing_record or {})
        updated_record["target_url"] = BRIDGY_FED_TARGET
        updated_record["resolved_url"] = clean_url(str(updated_record.get("resolved_url", BRIDGY_FED_TARGET)).strip()) or BRIDGY_FED_TARGET
        updated_record["verified_targets"] = verified_targets
        updated_record["verified_at"] = now_iso()
        updated_record["status"] = "success"
        if verification_errors:
            updated_record["verification_errors"] = verification_errors
        else:
            updated_record.pop("verification_errors", None)

        changed = updated_record != existing_record
        post_entry["bridgy"] = updated_record
        return changed, 0

    if isinstance(existing_record, dict) and existing_record.get("status") == "accepted" and timestamp_is_fresh(existing_record.get("updated_at"), BRIDGY_ACCEPTED_RETRY_AFTER):
        return False, 0

    if dry_run:
        print(f"Would notify Bridgy Fed about {source_url}")
        return False, 0

    resolved_url, endpoint, discovery_status = discover_webmention_endpoint(BRIDGY_FED_TARGET, lookups_cache, bad_uris_cache)
    record: dict[str, Any] = {
        "target_url": BRIDGY_FED_TARGET,
        "resolved_url": resolved_url,
        "updated_at": now_iso(),
    }

    if not endpoint:
        record["status"] = "failed"
        record["reason"] = discovery_status
        post_entry["bridgy"] = record
        print(f"Failed to discover Bridgy Fed endpoint for {source_url}: {discovery_status}")
        return True, 1

    record["endpoint"] = endpoint
    response = post_form(endpoint, {"source": source_url, "target": resolved_url})
    body_text = decode_body(response)
    record["response_status"] = int(response["status"])

    if 200 <= response["status"] < 300:
        record["status"] = "accepted"
        record["accepted_at"] = now_iso()
        record["verified_targets"] = verified_targets
        if verification_errors:
            record["verification_errors"] = verification_errors
        post_entry["bridgy"] = record
        print(f"Queued Bridgy Fed notification for {source_url}")
        return True, 0

    record["status"] = "failed"
    record["verified_targets"] = verified_targets
    if verification_errors:
        record["verification_errors"] = verification_errors
    record["error"] = excerpt(body_text) or f"HTTP {response['status']}"
    post_entry["bridgy"] = record
    print(f"Failed to notify Bridgy Fed about {source_url}: {record['error']}")
    return True, 1


def send_outgoing_webmention(
    slug: str,
    source_url: str,
    target_url: str,
    outgoing_cache: dict[str, Any],
    lookups_cache: dict[str, Any],
    bad_uris_cache: dict[str, Any],
    *,
    dry_run: bool,
) -> tuple[bool, int]:
    cleaned_target = clean_url(target_url)
    if not cleaned_target:
        return False, 0

    post_entry = ensure_post_entry(outgoing_cache, slug, source_url, "targets")
    existing = post_entry["targets"].get(cleaned_target)
    existing_record = existing if isinstance(existing, dict) else None
    existing_format_version = int((existing_record or {}).get("source_format_version", 1) or 1)
    if isinstance(existing_record, dict) and existing_record.get("status") == "success" and existing_format_version >= OUTGOING_SOURCE_FORMAT_VERSION:
        return False, 0

    if dry_run:
        print(f"Would send webmention from {source_url} to {cleaned_target}")
        return False, 0

    resolved_url, endpoint, discovery_status = discover_webmention_endpoint(cleaned_target, lookups_cache, bad_uris_cache)
    record: dict[str, Any] = {
        "target_url": cleaned_target,
        "resolved_url": resolved_url,
        "updated_at": now_iso(),
        "source_format_version": OUTGOING_SOURCE_FORMAT_VERSION,
    }

    if not endpoint:
        record["status"] = "skipped"
        record["reason"] = discovery_status
        post_entry["targets"][cleaned_target] = record
        print(f"Skipped webmention for {cleaned_target}: {discovery_status}")
        return True, 0

    record["endpoint"] = endpoint
    response = post_form(endpoint, {"source": source_url, "target": resolved_url})
    body_text = decode_body(response)
    if 200 <= response["status"] < 300:
        record["status"] = "success"
        record["sent_at"] = now_iso()
        record["response_status"] = int(response["status"])
        post_entry["targets"][cleaned_target] = record
        print(f"Sent webmention {source_url} -> {resolved_url}")
        return True, 0

    record["status"] = "failed"
    record["response_status"] = int(response["status"])
    record["error"] = excerpt(body_text) or f"HTTP {response['status']}"
    post_entry["targets"][cleaned_target] = record
    print(f"Failed webmention {source_url} -> {resolved_url}: {record['error']}")
    return True, 1


def process_posts(before_sha: str, current_sha: str, *, dry_run: bool, retry_recent: int) -> int:
    site_url = load_site_url()
    bridgy_cache = load_yaml_map(BRIDGY_CACHE_FILE, "posts")
    outgoing_cache = load_yaml_map(OUTGOING_WEBMENTIONS_FILE, "posts")
    lookups_cache = load_yaml_map(LOOKUPS_FILE, "targets")
    bad_uris_cache = load_yaml_map(BAD_URIS_FILE, "targets")
    candidates = collect_post_candidates(before_sha, current_sha, retry_recent)
    if not candidates:
        print("No new or retryable posts detected, skipping publication.")
        return 0

    any_changes = False
    failures = 0
    new_post_count = 0
    retry_count = 0

    for post_path, is_new in candidates:
        if is_new:
            new_post_count += 1
        else:
            retry_count += 1

        slug = derive_slug(post_path)
        source_url = f"{site_url}/item/{slug}/"
        front_matter = read_front_matter(post_path)
        source_link = clean_url(str(front_matter.get("link", "")).strip())

        changed, publish_failures = send_bridgy_fed_webmention(
            slug,
            source_url,
            bridgy_cache,
            lookups_cache,
            bad_uris_cache,
            dry_run=dry_run,
        )
        any_changes = any_changes or changed
        failures += publish_failures

        if source_link:
            changed, webmention_failures = send_outgoing_webmention(
                slug,
                source_url,
                source_link,
                outgoing_cache,
                lookups_cache,
                bad_uris_cache,
                dry_run=dry_run,
            )
            any_changes = any_changes or changed
            failures += webmention_failures

    if dry_run:
        print(f"Dry run complete for {len(candidates)} post(s).")
        return 0

    wrote_any = False
    wrote_any = write_yaml(BRIDGY_CACHE_FILE, bridgy_cache) or wrote_any
    wrote_any = write_yaml(OUTGOING_WEBMENTIONS_FILE, outgoing_cache) or wrote_any
    wrote_any = write_yaml(LOOKUPS_FILE, lookups_cache) or wrote_any
    wrote_any = write_yaml(BAD_URIS_FILE, bad_uris_cache) or wrote_any

    if wrote_any or any_changes:
        if retry_count:
            print(f"Processed {len(candidates)} post(s): {new_post_count} new, {retry_count} retry.")
        else:
            print(f"Processed {len(candidates)} new post(s).")
    else:
        print("No publication state changed.")

    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Notify Bridgy Fed about new posts and send outgoing source webmentions.")
    parser.add_argument("before_sha")
    parser.add_argument("current_sha")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--retry-recent",
        type=int,
        default=DEFAULT_RETRY_RECENT,
        help="Also retry the most recent N posts that do not yet have a successful Bridgy Fed notification.",
    )
    args = parser.parse_args()
    return process_posts(args.before_sha, args.current_sha, dry_run=args.dry_run, retry_recent=max(0, args.retry_recent))


if __name__ == "__main__":
    raise SystemExit(main())