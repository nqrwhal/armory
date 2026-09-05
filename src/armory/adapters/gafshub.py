"""gafshub.com — Discourse forum, GAFS successor. All content JSON requires login."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

from ..models import HealthResult, Listing
from .base import AdapterError, SourceAdapter

BASE = "https://gafshub.com"

# Discourse rotates the `_t` session token via Set-Cookie every ~10 minutes of
# use; a token that misses two rotations is invalidated. The env var only
# bootstraps the jar — rotated tokens are persisted here afterwards.
JAR_PATH = Path("gafshub.cookies.json")


def load_jar() -> dict[str, str] | None:
    try:
        data = json.loads(JAR_PATH.read_text())
        if data.get("_t"):
            return data
    except (OSError, ValueError):
        pass
    return None


def save_jar(cookies: dict[str, str]) -> None:
    try:
        JAR_PATH.write_text(json.dumps(cookies))
    except OSError:
        pass  # read-only cwd shouldn't kill polling


class GafshubAdapter(SourceAdapter):
    name = "gafshub"

    def __init__(self, cfg, fetcher):
        super().__init__(cfg, fetcher)
        self._categories: dict[int, str] | None = None

    # --- plumbing ---

    def _persist_jar(self) -> None:
        try:
            save_jar(dict(self.fetcher._session.cookies))
        except Exception:  # noqa: BLE001
            pass

    def _get_json(self, path: str, params: dict | None = None) -> dict:
        resp = self.fetcher.get(BASE + path, params=params)
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError:
            raise AdapterError(f"gafshub: {path} returned non-JSON (HTTP {resp.status_code}) — Cloudflare interstitial?")
        if isinstance(data, dict) and data.get("error_type") == "not_logged_in":
            raise AdapterError(
                "session expired — re-export GAFSHUB_COOKIE into .env, delete "
                "gafshub.cookies.json, then restart the watcher (see `armory setup`)"
            )
        if resp.status_code != 200:
            raise AdapterError(f"gafshub: {path} returned HTTP {resp.status_code}")
        return data

    def _category_name(self, category_id: int | None) -> str | None:
        if category_id is None:
            return None
        if self._categories is None:
            try:
                site = self._get_json("/site.json")
                self._categories = {c["id"]: c["name"] for c in site.get("categories", [])}
            except AdapterError:
                self._categories = {}
        return self._categories.get(category_id)

    @staticmethod
    def _parse_dt(iso: str | None) -> datetime | None:
        if not iso:
            return None
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _strip_html(cooked: str) -> str:
        return re.sub(r"\s+", " ", BeautifulSoup(cooked, "html.parser").get_text(" ", strip=True))

    def _topic_url(self, t: dict) -> str:
        slug = t.get("slug") or "topic"
        return f"{BASE}/t/{slug}/{t['id']}"

    def _topic_listing(self, t: dict, users_by_id: dict[int, str]) -> Listing:
        author = None
        posters = t.get("posters") or []
        if posters and posters[0].get("user_id") in users_by_id:
            author = users_by_id[posters[0]["user_id"]]
        return Listing(
            source=self.name,
            external_id=str(t["id"]),
            url=self._topic_url(t),
            title=t.get("title", ""),
            category=self._category_name(t.get("category_id")),
            author=author,
            posted_at=self._parse_dt(t.get("created_at")),
        )

    # --- interface ---

    def poll(self, pages: int = 1) -> list[Listing]:
        listings: list[Listing] = []
        try:
            for page in range(1, max(1, pages) + 1):
                data = self._get_json("/latest.json", params={"page": page} if page > 1 else None)
                tl = data.get("topic_list", {})
                users_by_id = {u["id"]: u["username"] for u in tl.get("users", [])}
                topics = tl.get("topics", [])
                if not topics:
                    break
                listings.extend(self._topic_listing(t, users_by_id) for t in topics)
                # first-post bodies for NEW topics are fetched lazily via enrich()
        finally:
            self._persist_jar()
        return listings

    def search(self, query: str, limit: int = 25) -> list[Listing]:
        try:
            data = self._get_json("/search.json", params={"q": query})
            out: list[Listing] = []
            for t in data.get("topics", [])[:limit]:
                out.append(
                    Listing(
                        source=self.name,
                        external_id=str(t["id"]),
                        url=self._topic_url(t),
                        title=t.get("title", ""),
                        category=self._category_name(t.get("category_id")),
                        posted_at=self._parse_dt(t.get("created_at")),
                    )
                )
            return out
        finally:
            self._persist_jar()

    def enrich(self, listing: Listing) -> None:
        """Fetch the first post body for topics the latest-feed window didn't cover."""
        if listing.body:
            return
        try:
            td = self._get_json(f"/t/{listing.external_id}.json")
            posts = td.get("post_stream", {}).get("posts", [])
            if posts:
                listing.body = self._strip_html(posts[0].get("cooked", ""))[:4000]
        except AdapterError:
            return
        finally:
            self._persist_jar()

    def health(self) -> HealthResult:
        try:
            resp = self.fetcher.get(BASE + "/session/current.json")
        except Exception as exc:  # noqa: BLE001
            return HealthResult(False, f"network error: {exc}")
        finally:
            self._persist_jar()
        if resp.status_code == 200:
            try:
                user = json.loads(resp.text).get("current_user", {})
                if user.get("username"):
                    return HealthResult(True, f"logged in as {user['username']}")
            except json.JSONDecodeError:
                pass
            return HealthResult(True, "session valid")
        if resp.status_code in (403, 404):
            return HealthResult(False, "session expired — re-export GAFSHUB_COOKIE (see `armory setup`)")
        return HealthResult(False, f"HTTP {resp.status_code}")
