"""
Web search tool - Fixed version with rate limiting and citation support.

CHANGES FROM ORIGINAL:
1. Rate limiting with exponential backoff (prevents 429 errors)
2. No auto-progressive search (single search by default)
3. Citation-friendly output with source metadata
4. 1.5s minimum between API calls
"""

from __future__ import annotations

from typing import Dict, Any, Optional, List
import requests
import re
from bs4 import BeautifulSoup
import time
import os
from datetime import datetime

from sciagent.base_tool import BaseTool

try:
    from ddgs import DDGS
except Exception:
    DDGS = None

BRAVE_AVAILABLE = True

try:
    import html2text
    HTML2TEXT_AVAILABLE = True
except ImportError:
    HTML2TEXT_AVAILABLE = False


class WebSearchTool(BaseTool):
    """Search the web with rate limiting and citation-friendly output."""

    name = "web_search"
    description = "Search the web. Returns results with source metadata for citations. Use ONE comprehensive query."
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string", 
                "description": "Search query. Combine all key terms into ONE comprehensive query."
            },
            "num_results": {
                "type": "number", 
                "description": "Number of results (default: 5, max: 10)", 
                "default": 5
            },
        },
        "required": ["query"],
    }
    
    # Rate limiting - class level to persist across instances
    _last_request_time: float = 0
    _min_request_interval: float = 1.5
    _backoff_until: float = 0
    _consecutive_failures: int = 0

    def _wait_for_rate_limit(self) -> None:
        """Respect rate limits."""
        current_time = time.time()
        
        # Backoff period
        if current_time < WebSearchTool._backoff_until:
            wait_time = WebSearchTool._backoff_until - current_time
            print(f"⏳ Rate limit: waiting {wait_time:.1f}s")
            time.sleep(wait_time)
            current_time = time.time()
        
        # Minimum interval
        time_since_last = current_time - WebSearchTool._last_request_time
        if time_since_last < self._min_request_interval:
            time.sleep(self._min_request_interval - time_since_last)
        
        WebSearchTool._last_request_time = time.time()

    def _handle_rate_limit(self) -> None:
        """Exponential backoff for 429 errors."""
        WebSearchTool._consecutive_failures += 1
        backoff = min(2 ** WebSearchTool._consecutive_failures, 32)
        WebSearchTool._backoff_until = time.time() + backoff
        print(f"⚠️ Rate limited. Waiting {backoff}s")

    def _classify_source(self, url: str) -> str:
        """Classify source type for citations."""
        url_lower = url.lower()
        
        if any(d in url_lower for d in ['pubmed', 'ncbi.nlm.nih', 'nature.com', 'science.org', 
                                         'cell.com', 'pnas.org', 'nejm.org', 'thelancet.com']):
            return 'peer_reviewed'
        if any(d in url_lower for d in ['arxiv.org', 'biorxiv.org', 'medrxiv.org', 'chemrxiv.org']):
            return 'preprint'
        if '.gov' in url_lower or 'who.int' in url_lower:
            return 'government'
        if 'github.com' in url_lower:
            return 'repository'
        if 'wikipedia.org' in url_lower:
            return 'encyclopedia'
        if any(d in url_lower for d in ['medium.com', 'blog', 'wordpress']):
            return 'blog'
        if any(d in url_lower for d in ['sciencedirect', 'springer', 'wiley', 'ieee', 'acm.org']):
            return 'peer_reviewed'
        
        return 'web'

    def _get_quality_emoji(self, source_type: str) -> str:
        """Get quality indicator emoji."""
        return {
            'peer_reviewed': '📗',
            'preprint': '📙',
            'government': '📘',
            'repository': '📂',
            'encyclopedia': '📖',
            'blog': '📝',
            'web': '🌐',
        }.get(source_type, '🌐')

    def _search_brave(self, query: str, num_results: int, retry: int = 0) -> List[Dict[str, Any]]:
        """Search Brave with rate limiting."""
        api_key = os.getenv('BRAVE_SEARCH_API_KEY')
        if not api_key:
            print("⚠️ BRAVE_SEARCH_API_KEY not set")
            return []
        
        self._wait_for_rate_limit()
        
        try:
            response = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": api_key
                },
                params={
                    "q": query,
                    "count": min(num_results, 10),
                    "search_lang": "en",
                },
                timeout=10
            )
            
            if response.status_code == 429:
                self._handle_rate_limit()
                if retry < 2:
                    time.sleep(2 ** (retry + 1))
                    return self._search_brave(query, num_results, retry + 1)
                return []
            
            response.raise_for_status()
            WebSearchTool._consecutive_failures = 0
            
            results = []
            data = response.json()
            
            for i, r in enumerate(data.get('web', {}).get('results', []), 1):
                url = r.get('url', '')
                source_type = self._classify_source(url)
                results.append({
                    'index': i,
                    'title': r.get('title', ''),
                    'url': url,
                    'snippet': r.get('description', ''),
                    'source_type': source_type,
                    'quality': self._get_quality_emoji(source_type),
                    'retrieved': datetime.now().strftime("%Y-%m-%d"),
                })
            
            print(f"📊 Found {len(results)} results")
            return results
            
        except Exception as e:
            print(f"❌ Search error: {e}")
            return []

    def _search_duckduckgo(self, query: str, num_results: int) -> List[Dict[str, Any]]:
        """Fallback to DuckDuckGo."""
        if DDGS is None:
            return []
        
        try:
            print(f"🦆 Using DuckDuckGo fallback")
            with DDGS() as ddgs:
                raw = list(ddgs.text(query, max_results=num_results))
            
            results = []
            for i, r in enumerate(raw, 1):
                url = r.get('href', '')
                source_type = self._classify_source(url)
                results.append({
                    'index': i,
                    'title': r.get('title', ''),
                    'url': url,
                    'snippet': r.get('body', ''),
                    'source_type': source_type,
                    'quality': self._get_quality_emoji(source_type),
                    'retrieved': datetime.now().strftime("%Y-%m-%d"),
                })
            
            return results
        except Exception as e:
            print(f"❌ DuckDuckGo error: {e}")
            return []

    def run(self, tool_input: Dict[str, Any], agent: Optional[Any] = None) -> Dict[str, Any]:
        query = tool_input.get("query", "")
        num_results = min(int(tool_input.get("num_results", 5)), 10)
        
        print(f"🔍 Searching: '{query}'")
        
        # Try Brave first, then DuckDuckGo
        results = self._search_brave(query, num_results)
        provider = "Brave"
        
        if not results:
            results = self._search_duckduckgo(query, num_results)
            provider = "DuckDuckGo"
        
        if not results:
            return {"success": False, "error": f"No results for: '{query}'"}
        
        # Format citation-friendly output
        lines = [
            f"## Search Results",
            f"**Query:** {query}",
            f"**Date:** {datetime.now().strftime('%Y-%m-%d')}",
            f"**Source:** {provider}",
            f"**Results:** {len(results)}",
            "",
            "### Sources (for citations)",
            ""
        ]
        
        for r in results:
            lines.append(
                f"[{r['index']}] {r['quality']} **{r['title']}**\n"
                f"    URL: {r['url']}\n"
                f"    Type: {r['source_type']}\n"
                f"    Summary: {r['snippet'][:200]}...\n"
            )
        
        lines.extend([
            "",
            "### Next Steps",
            "Use `web_fetch` on the most relevant URLs above to get full content for citations.",
            "Priority: 📗 peer-reviewed > 📙 preprint > 📘 government > 🌐 web"
        ])
        
        return {
            "success": True,
            "output": "\n".join(lines),
            "results": results,
            "query": query,
            "num_results": len(results),
        }


def get_tool() -> BaseTool:
    return WebSearchTool()
