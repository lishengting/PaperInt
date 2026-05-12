"""Abstract base class for fetchers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from paper_info.identifiers import Identifier
from paper_info.models import PaperMetadata


class Fetcher(ABC):
    """Base class for metadata fetchers."""

    name: str

    @abstractmethod
    def can_fetch(self, identifier: Identifier) -> bool:
        """Return whether this fetcher can query the identifier directly."""

    @abstractmethod
    def fetch(self, identifier: Identifier, timeout: float) -> PaperMetadata:
        """Fetch metadata for an identifier."""
