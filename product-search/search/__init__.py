"""Public integration surface: SearchService, SearchRequest, Filters, Preferences."""
from .models import SearchRequest, Filters, Preferences
from .service import SearchService
from .bootstrap import open_search

__all__ = ["SearchService", "SearchRequest", "Filters", "Preferences", "open_search"]
