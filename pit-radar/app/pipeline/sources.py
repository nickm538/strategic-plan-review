"""config/sources.yaml loader (docs/04). STUB - collectors builder."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from app.paths import CONFIG_DIR
from app.pipeline.types import SourceScope


class SourcesConfig:
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: Path = CONFIG_DIR / "sources.yaml", overrides: Optional[dict[str, Any]] = None) -> "SourcesConfig":
        raise NotImplementedError

    def enabled_subreddits(self) -> list[str]:
        raise NotImplementedError

    def excluded_subreddits(self) -> set[str]:
        """Lowercased names that must never be collected."""
        raise NotImplementedError

    def scope_for(self, *, url: str, subreddit: Optional[str] = None) -> SourceScope:
        """li_scoped for an enabled LI subreddit, an li_scoped news domain, or patch.com/new-york/<LI town>;
        regional for regional domains; else general."""
        raise NotImplementedError

    def socialcrawl_capability(self, capability: str) -> dict[str, Any]:
        raise NotImplementedError

    def socialcrawl_platform_map(self) -> dict[str, str]:
        """SocialCrawl slug -> app platform value (reddit->reddit, twitter->x, google_news->news ...)."""
        raise NotImplementedError

    def exa_include_domains(self) -> list[str]:
        raise NotImplementedError
