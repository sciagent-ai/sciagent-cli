"""
Web tool - combined search and fetch.

Handles web search with rate limiting and URL content fetching.

IMPROVEMENTS:
1. Exponential backoff with retry logic (prevents 429 errors)
2. Consecutive failure tracking for adaptive rate limiting
3. Structured results with metadata (not just formatted string)
4. Quality emoji indicators per source type
5. Retrieved date tracking per result
6. Better HTML-to-text conversion with html2text
7. Prompt parameter for fetch to indicate analysis intent
8. LLM-powered content extraction when prompt is provided
9. PDF support for fetching academic papers
"""

from __future__ import annotations

import io
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field

import requests


# =============================================================================
# FETCH LOGGING - Unbiased validation for data provenance
# =============================================================================

class FetchLogger:
    """
    Logs all HTTP fetch operations for external validation.

    This creates an immutable audit trail that the model cannot fabricate.
    The orchestrator/provenance checker can compare these logs against
    task claims to detect data fabrication.

    Log format (JSONL):
    {
        "timestamp": "2025-01-15T10:30:00",
        "url": "https://example.com/data.csv",
        "final_url": "https://example.com/data.csv",  # After redirects
        "status_code": 200,
        "content_type": "text/csv",
        "content_length": 12345,
        "content_preview": "first 200 chars...",
        "is_error_page": false,
        "error_indicators": [],
        "success": true,
        "error": null
    }
    """

    _instance = None
    _log_dir: Path = None
    _log_file: Path = None

    # Indicators that content is an error page, not actual data
    ERROR_INDICATORS = [
        "404 not found",
        "page not found",
        "error 404",
        "file not found",
        "access denied",
        "403 forbidden",
        "500 internal server error",
        "502 bad gateway",
        "503 service unavailable",
        "this page doesn't exist",
        "the requested url was not found",
    ]

    # HTML indicators - suggests we got a webpage, not data
    HTML_INDICATORS = [
        "<!doctype html",
        "<html",
        "<head>",
        "<body>",
        "text/html",
    ]

    def __new__(cls, log_dir: str = None):
        """Singleton pattern to ensure single log file."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, log_dir: str = None):
        if self._initialized:
            return

        # Default to _logs in current working directory
        if log_dir is None:
            log_dir = os.path.join(os.getcwd(), "_logs")

        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = self._log_dir / "fetch_log.jsonl"
        self._initialized = True

    def log_fetch(
        self,
        url: str,
        final_url: str,
        status_code: int,
        content_type: str,
        content: str,
        success: bool,
        error: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Log a fetch operation with content analysis.

        Returns the log entry (useful for immediate validation).
        """
        timestamp = datetime.now().isoformat()
        content_preview = content[:200] if content else ""
        content_lower = content.lower()[:2000] if content else ""

        # Detect error page indicators
        error_indicators = [
            indicator for indicator in self.ERROR_INDICATORS
            if indicator in content_lower
        ]

        # Detect if content is HTML (might be error page or wrong content type)
        is_html = any(
            indicator in content_lower
            for indicator in self.HTML_INDICATORS
        )

        # Flag as error page if status is error OR content has error indicators
        is_error_page = (
            status_code >= 400 or
            len(error_indicators) > 0 or
            (is_html and "text/html" not in content_type.lower() and content_type != "")
        )

        entry = {
            "timestamp": timestamp,
            "url": url,
            "final_url": final_url,
            "status_code": status_code,
            "content_type": content_type,
            "content_length": len(content) if content else 0,
            "content_preview": content_preview,
            "is_html": is_html,
            "is_error_page": is_error_page,
            "error_indicators": error_indicators,
            "success": success,
            "error": error,
        }

        # Append to log file
        try:
            with open(self._log_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            print(f"⚠️ Failed to write fetch log: {e}")

        return entry

    def get_log_path(self) -> Path:
        """Return path to the log file."""
        return self._log_file

    def get_recent_fetches(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Read recent fetch entries from log."""
        entries = []
        try:
            if self._log_file.exists():
                with open(self._log_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            entries.append(json.loads(line))
        except Exception as e:
            print(f"⚠️ Failed to read fetch log: {e}")

        return entries[-limit:] if limit else entries

    def find_fetch_for_url(self, url: str) -> Optional[Dict[str, Any]]:
        """Find the most recent fetch entry for a URL."""
        entries = self.get_recent_fetches(limit=0)  # Get all
        for entry in reversed(entries):
            if entry.get("url") == url or entry.get("final_url") == url:
                return entry
        return None

    def clear(self):
        """Clear the fetch log (for testing)."""
        if self._log_file.exists():
            self._log_file.unlink()


# Global fetch logger instance
_fetch_logger: Optional[FetchLogger] = None


def get_fetch_logger(log_dir: str = None) -> FetchLogger:
    """Get or create the global fetch logger."""
    global _fetch_logger
    if _fetch_logger is None:
        _fetch_logger = FetchLogger(log_dir)
    return _fetch_logger

# Import defaults for model configuration
from ...defaults import FAST_MODEL, WEB_FETCH_MAX_CONTENT, WEB_FETCH_DISPLAY_LIMIT

# Optional: DuckDuckGo fallback (try new package first, then old)
try:
    from ddgs import DDGS
except ImportError:
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

# Optional: html2text for better HTML conversion
try:
    import html2text
    HTML2TEXT_AVAILABLE = True
except ImportError:
    HTML2TEXT_AVAILABLE = False

# Optional: BeautifulSoup for fallback HTML parsing
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

# Optional: PDF extraction support
try:
    import pypdf
    PYPDF_AVAILABLE = True
except ImportError:
    try:
        import PyPDF2 as pypdf
        PYPDF_AVAILABLE = True
    except ImportError:
        PYPDF_AVAILABLE = False


@dataclass
class ToolResult:
    """Result from tool execution."""
    success: bool
    output: Any
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = field(default_factory=dict)


class WebTool:
    """Web search and fetch with rate limiting and comprehensive results."""

    name = "web"
    description = """Search the web or fetch URL content for research and information gathering.

WHEN TO USE:
- Research tasks: finding papers, documentation, APIs, libraries
- Literature review: searching for prior work, citations, references
- Technical questions: finding solutions, best practices, tutorials
- Current information: news, recent developments, version info
- Verifying claims: fact-checking, finding authoritative sources

WORKFLOW:
1. search: Find relevant sources (returns titles, URLs, snippets with quality indicators)
2. fetch: Read full content from promising URLs (with optional analysis prompt)
3. Extract: Pull out specific facts, citations, data
4. Cite: Reference sources in your output

COMMANDS:
- search: Web search. Args: query (required), num_results (default 5)
- fetch: Get URL content. Args: url (required), prompt (optional - what to analyze)

QUALITY INDICATORS:
📗 peer-reviewed | 📙 preprint | 📘 government | 📂 repository | 📖 encyclopedia | 📝 blog | 🌐 web

CONTENT TYPES:
- HTML pages: Converted to clean text
- PDF documents: Text extracted from all pages (great for academic papers!)
- Plain text: Returned as-is

LLM EXTRACTION (IMPORTANT):
When you provide a 'prompt' parameter, a fast LLM reads the FULL document and extracts
exactly what you need. This prevents missing information due to truncation.

Example: fetch(url="https://arxiv.org/pdf/...", prompt="extract wavelength and design parameters")
→ Returns focused extraction from the entire PDF, not truncated content

TIPS:
- Use specific queries: "metasurface 1550nm efficiency 2023" not "metasurface info"
- Search multiple times with different queries for thorough research
- Use prompt parameter in fetch to focus content extraction
- Prioritize 📗 and 📙 sources for academic citations
- PDF URLs work directly - no need to find HTML versions"""

    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": ["search", "fetch"],
                "description": "search: find sources | fetch: read URL content"
            },
            "query": {
                "type": "string",
                "description": "Search query - be specific (e.g., 'React hooks tutorial 2024')"
            },
            "url": {
                "type": "string",
                "description": "Full URL to fetch content from"
            },
            "prompt": {
                "type": "string",
                "description": "What to analyze in fetched content (e.g., 'extract methodology and results')"
            },
            "num_results": {
                "type": "integer",
                "description": "Number of results (1-10)",
                "default": 5
            }
        },
        "required": ["command"]
    }

    # Rate limiting - class level to persist across instances
    _last_request_time: float = 0
    _min_interval: float = 1.5
    _backoff_until: float = 0
    _consecutive_failures: int = 0
    _max_backoff: float = 32.0
    _max_retries: int = 2

    # Source classification with expanded domains
    SOURCE_TYPES = {
        'peer_reviewed': ['pubmed', 'ncbi.nlm.nih', 'nature.com', 'science.org', 'cell.com',
                         'pnas.org', 'nejm.org', 'thelancet.com', 'sciencedirect', 'springer',
                         'wiley', 'ieee', 'acm.org', 'aps.org', 'acs.org'],
        'preprint': ['arxiv.org', 'biorxiv.org', 'medrxiv.org', 'chemrxiv.org', 'ssrn.com'],
        'government': ['.gov', 'who.int', '.edu'],
        'repository': ['github.com', 'gitlab.com', 'bitbucket.org'],
        'encyclopedia': ['wikipedia.org', 'britannica.com'],
        'blog': ['medium.com', 'blog', 'wordpress', 'substack.com', 'dev.to'],
        'documentation': ['docs.', 'documentation', 'readthedocs', 'gitbook'],
    }

    # Quality emoji mapping
    QUALITY_EMOJI = {
        'peer_reviewed': '📗',
        'preprint': '📙',
        'government': '📘',
        'repository': '📂',
        'encyclopedia': '📖',
        'blog': '📝',
        'documentation': '📚',
        'web': '🌐',
    }

    def __init__(self):
        self._html2text_converter = None
        if HTML2TEXT_AVAILABLE:
            self._html2text_converter = html2text.HTML2Text()
            self._html2text_converter.ignore_links = False
            self._html2text_converter.ignore_images = True
            self._html2text_converter.ignore_emphasis = False
            self._html2text_converter.body_width = 0  # No wrapping

    def execute(self, command: str, **kwargs) -> ToolResult:
        """Execute web operation."""
        if command == "search":
            query = kwargs.get("query", "")
            if not query:
                return ToolResult(success=False, output=None, error="Missing query for search")
            return self._search(query, kwargs.get("num_results", 5))
        elif command == "fetch":
            url = kwargs.get("url", "")
            if not url:
                return ToolResult(success=False, output=None, error="Missing URL for fetch")
            prompt = kwargs.get("prompt", "")
            return self._fetch(url, prompt)
        else:
            return ToolResult(success=False, output=None, error=f"Unknown command: {command}")

    def _wait_rate_limit(self):
        """Respect rate limits with backoff awareness."""
        current = time.time()

        # Check if we're in a backoff period
        if current < WebTool._backoff_until:
            wait = WebTool._backoff_until - current
            print(f"⏳ Rate limit backoff: waiting {wait:.1f}s")
            time.sleep(wait)
            current = time.time()

        # Enforce minimum interval between requests
        elapsed = current - WebTool._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

        WebTool._last_request_time = time.time()

    def _handle_rate_limit(self):
        """Exponential backoff for rate limit errors (429)."""
        WebTool._consecutive_failures += 1
        backoff = min(2 ** WebTool._consecutive_failures, self._max_backoff)
        WebTool._backoff_until = time.time() + backoff
        print(f"⚠️ Rate limited (attempt {WebTool._consecutive_failures}). Backing off {backoff}s")

    def _reset_failures(self):
        """Reset failure counter on successful request."""
        WebTool._consecutive_failures = 0

    def _classify_source(self, url: str) -> str:
        """Classify source type for citations."""
        url_lower = url.lower()

        for source_type, domains in self.SOURCE_TYPES.items():
            if any(d in url_lower for d in domains):
                return source_type

        return 'web'

    def _get_quality_emoji(self, source_type: str) -> str:
        """Get quality indicator emoji for source type."""
        return self.QUALITY_EMOJI.get(source_type, '🌐')

    def _search(self, query: str, num_results: int = 5) -> ToolResult:
        """Search the web with retry logic and structured results."""
        num_results = min(num_results, 10)
        search_date = datetime.now().strftime('%Y-%m-%d')

        print(f"🔍 Searching: '{query}'")

        # Try Brave first with retry
        results = self._search_brave(query, num_results)
        provider = "Brave"

        # Fallback to DuckDuckGo
        if not results and DDGS:
            results = self._search_duckduckgo(query, num_results)
            provider = "DuckDuckGo"

        if not results:
            return ToolResult(
                success=False,
                output=None,
                error=f"No results for: '{query}'",
                metadata={"query": query, "provider": provider}
            )

        # Enrich results with classification, emoji, and date
        peer_reviewed = []
        preprints = []
        government = []
        other = []

        for r in results:
            source_type = self._classify_source(r['url'])
            r['source_type'] = source_type
            r['quality'] = self._get_quality_emoji(source_type)
            r['retrieved'] = search_date

            if source_type == 'peer_reviewed':
                peer_reviewed.append(r)
            elif source_type == 'preprint':
                preprints.append(r)
            elif source_type == 'government':
                government.append(r)
            else:
                other.append(r)

        # Format citation-friendly output with quality indicators
        lines = [
            f"## Search Results",
            f"**Query:** {query}",
            f"**Date:** {search_date}",
            f"**Provider:** {provider}",
            f"**Results:** {len(results)} (📗 {len(peer_reviewed)} peer-reviewed, 📙 {len(preprints)} preprints, 📘 {len(government)} government)",
            "",
            "### Sources (sorted by quality)",
            ""
        ]

        # Show results grouped by quality
        for r in results:
            snippet = r['snippet'][:200] + "..." if len(r['snippet']) > 200 else r['snippet']
            lines.append(
                f"[{r['index']}] {r['quality']} **{r['title']}**\n"
                f"    URL: {r['url']}\n"
                f"    Type: {r['source_type']} | Retrieved: {r['retrieved']}\n"
                f"    {snippet}\n"
            )

        # Add actionable next steps with priority guidance
        lines.append("")
        lines.append("### Next Steps")
        lines.append("**Priority:** 📗 peer-reviewed > 📙 preprint > 📘 government > 📚 docs > 🌐 web")
        lines.append("")

        if peer_reviewed:
            lines.append(f"**Recommended - Fetch peer-reviewed sources:**")
            for r in peer_reviewed[:3]:
                lines.append(f"  - [{r['index']}] {r['quality']} {r['url']}")

        if preprints:
            lines.append(f"**Recent research - Fetch preprints:**")
            for r in preprints[:2]:
                lines.append(f"  - [{r['index']}] {r['quality']} {r['url']}")

        if government and not peer_reviewed:
            lines.append(f"**Authoritative - Fetch government sources:**")
            for r in government[:2]:
                lines.append(f"  - [{r['index']}] {r['quality']} {r['url']}")

        lines.append("")
        lines.append("**Usage:** `web(command='fetch', url='...', prompt='what to extract')`")

        print(f"📊 Found {len(results)} results ({len(peer_reviewed)} peer-reviewed)")

        return ToolResult(
            success=True,
            output="\n".join(lines),
            error=None,
            metadata={
                "results": results,
                "query": query,
                "provider": provider,
                "num_results": len(results),
                "counts": {
                    "peer_reviewed": len(peer_reviewed),
                    "preprint": len(preprints),
                    "government": len(government),
                    "other": len(other)
                }
            }
        )

    def _search_brave(self, query: str, num_results: int, retry: int = 0) -> List[Dict]:
        """Search using Brave API with retry logic."""
        api_key = os.getenv('BRAVE_SEARCH_API_KEY')
        if not api_key:
            print("⚠️ BRAVE_SEARCH_API_KEY not set")
            return []

        self._wait_rate_limit()

        try:
            # Don't use freshness parameter - it can be too restrictive
            params = {
                "q": query,
                "count": num_results,
            }

            response = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": api_key
                },
                params=params,
                timeout=15
            )

            # Handle rate limiting with exponential backoff and retry
            if response.status_code == 429:
                self._handle_rate_limit()
                if retry < self._max_retries:
                    wait_time = 2 ** (retry + 1)
                    print(f"🔄 Retrying in {wait_time}s (attempt {retry + 1}/{self._max_retries})")
                    time.sleep(wait_time)
                    return self._search_brave(query, num_results, retry + 1)
                print(f"❌ Brave rate limited after {self._max_retries} retries")
                return []

            # Check for other error status codes
            if response.status_code != 200:
                print(f"❌ Brave API returned status {response.status_code}: {response.text[:200]}")
                return []

            self._reset_failures()  # Reset on success

            data = response.json()

            # Debug: show response structure if no results
            web_data = data.get('web', {})
            raw_results = web_data.get('results', [])

            if not raw_results:
                # Check alternative response structures
                # Some Brave API versions use different keys
                if 'results' in data:
                    raw_results = data['results']
                elif 'webPages' in data:
                    # Microsoft Bing-style response
                    raw_results = data.get('webPages', {}).get('value', [])

                if not raw_results:
                    print(f"⚠️ Brave returned no results. Response keys: {list(data.keys())}")
                    if 'web' in data:
                        print(f"   web keys: {list(data['web'].keys())}")
                    return []

            results = []
            for i, r in enumerate(raw_results, 1):
                results.append({
                    'index': i,
                    'title': r.get('title', ''),
                    'url': r.get('url', ''),
                    'snippet': r.get('description', r.get('snippet', '')),
                    'age': r.get('age', ''),  # Include result age if available
                })

            return results

        except requests.exceptions.Timeout:
            print("⚠️ Brave search timed out")
            return []
        except requests.exceptions.RequestException as e:
            print(f"❌ Brave search error: {e}")
            return []
        except Exception as e:
            print(f"❌ Unexpected error in Brave search: {e}")
            import traceback
            traceback.print_exc()
            return []

    def _search_duckduckgo(self, query: str, num_results: int) -> List[Dict]:
        """Fallback to DuckDuckGo search."""
        if not DDGS:
            print("⚠️ DuckDuckGo not available (ddgs package not installed)")
            return []

        try:
            print(f"🦆 Using DuckDuckGo fallback")
            with DDGS() as ddgs:
                raw = list(ddgs.text(query, max_results=num_results))

            results = []
            for i, r in enumerate(raw, 1):
                results.append({
                    'index': i,
                    'title': r.get('title', ''),
                    'url': r.get('href', ''),
                    'snippet': r.get('body', ''),
                    'age': '',  # DuckDuckGo doesn't provide age
                })

            print(f"🦆 DuckDuckGo returned {len(results)} results")
            return results

        except Exception as e:
            print(f"❌ DuckDuckGo error: {e}")
            return []

    def _fetch(self, url: str, prompt: str = "") -> ToolResult:
        """
        Fetch and process content from URL.

        When a prompt is provided, uses a fast LLM to extract relevant information
        from the full content, avoiding truncation issues. This follows the pattern
        used by Claude Code's WebFetch tool.

        Supports HTML, plain text, and PDF content.

        IMPORTANT: All fetch operations are logged to _logs/fetch_log.jsonl for
        external validation. This creates an audit trail that cannot be fabricated.
        """
        fetch_date = datetime.now().strftime('%Y-%m-%d %H:%M')
        source_type = self._classify_source(url)
        quality = self._get_quality_emoji(source_type)
        logger = get_fetch_logger()

        print(f"📄 Fetching: {url}")

        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            }

            response = requests.get(url, timeout=30, headers=headers, allow_redirects=True)
            response.raise_for_status()

            content_type = response.headers.get('Content-Type', '').lower()
            final_url = response.url  # Capture final URL after redirects
            title = ""
            is_pdf = False

            # Determine content type and extract text accordingly
            if 'application/pdf' in content_type or url.lower().endswith('.pdf'):
                # PDF content
                is_pdf = True
                content = self._pdf_to_text(response.content)
                title = url.split('/')[-1]  # Use filename as title for PDFs
                print(f"📑 Extracted text from PDF ({len(response.content):,} bytes)")
            elif 'html' in content_type or '<html' in response.text[:1000].lower():
                # HTML content
                content = self._html_to_text(response.text)
                title = self._extract_title(response.text)
            else:
                # Plain text or other - just clean up whitespace
                content = re.sub(r'\s+', ' ', response.text).strip()

            # Limit content for processing (but much higher than before for LLM extraction)
            content = content[:WEB_FETCH_MAX_CONTENT]

            # If prompt is provided, use LLM to extract relevant information
            # This is the key improvement - instead of truncating and hoping the
            # agent sees what it needs, we have a fast LLM read the full content
            # and extract exactly what's requested
            if prompt:
                print(f"🤖 Using LLM to extract: '{prompt[:50]}...' " if len(prompt) > 50 else f"🤖 Using LLM to extract: '{prompt}'")
                extracted_content = self._extract_with_llm(content, prompt, url)
                display_content = extracted_content
                extraction_used = True
            else:
                # No prompt - return truncated content as before
                display_content = content[:WEB_FETCH_DISPLAY_LIMIT]
                extraction_used = False

            # Build comprehensive output
            lines = [
                f"## {quality} Fetched Content",
                f"**URL:** {url}",
            ]

            if final_url != url:
                lines.append(f"**Final URL:** {final_url} (redirected)")

            if title:
                lines.append(f"**Title:** {title}")

            lines.extend([
                f"**Type:** {source_type}" + (" (PDF)" if is_pdf else ""),
                f"**Retrieved:** {fetch_date}",
                f"**Content Length:** {len(content):,} chars",
                f"**Status:** {response.status_code}",
            ])

            if prompt:
                lines.append(f"**Extraction Prompt:** {prompt}")
                lines.append(f"**Processing:** LLM-extracted (full document analyzed)")

            lines.extend([
                "",
                "---",
                "### " + ("Extracted Information" if extraction_used else "Content"),
                "",
                display_content,
            ])

            # Only show truncation notice if we didn't use LLM extraction
            if not extraction_used and len(content) > WEB_FETCH_DISPLAY_LIMIT:
                lines.append(f"\n... [truncated {len(content) - WEB_FETCH_DISPLAY_LIMIT:,} chars - use 'prompt' parameter for targeted extraction]")

            # Add citation helper
            lines.extend([
                "",
                "---",
                "### Citation Info",
                f"- **Source:** {url}",
                f"- **Type:** {source_type}",
                f"- **Accessed:** {fetch_date}",
            ])
            if title:
                lines.append(f"- **Title:** {title}")

            print(f"✅ Fetched {len(content):,} chars from {source_type} source" +
                  (f" (LLM-extracted)" if extraction_used else ""))

            # Log fetch for external validation (unbiased audit trail)
            log_entry = logger.log_fetch(
                url=url,
                final_url=final_url,
                status_code=response.status_code,
                content_type=content_type,
                content=content,
                success=True,
                error=None
            )

            return ToolResult(
                success=True,
                output="\n".join(lines),
                error=None,
                metadata={
                    "url": url,
                    "final_url": final_url,
                    "title": title,
                    "source_type": source_type,
                    "content_length": len(content),
                    "status_code": response.status_code,
                    "retrieved": fetch_date,
                    "prompt": prompt,
                    "extraction_used": extraction_used,
                    "is_pdf": is_pdf,
                    "full_content": content,  # Include full content in metadata
                    "fetch_log_entry": log_entry,  # Include log entry for validation
                }
            )

        except requests.exceptions.Timeout:
            error_msg = f"Timeout fetching {url} (>30s)"
            logger.log_fetch(
                url=url, final_url=url, status_code=0,
                content_type="", content="", success=False, error=error_msg
            )
            return ToolResult(
                success=False,
                output=None,
                error=error_msg,
                metadata={"url": url}
            )
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response else 0
            error_msg = f"HTTP error fetching {url}: {status_code}"
            # Log with response content if available (may be error page)
            error_content = e.response.text[:500] if e.response else ""
            logger.log_fetch(
                url=url, final_url=url, status_code=status_code,
                content_type=e.response.headers.get('Content-Type', '') if e.response else "",
                content=error_content, success=False, error=error_msg
            )
            return ToolResult(
                success=False,
                output=None,
                error=error_msg,
                metadata={"url": url, "status_code": status_code}
            )
        except requests.exceptions.RequestException as e:
            error_msg = f"Request error fetching {url}: {str(e)}"
            logger.log_fetch(
                url=url, final_url=url, status_code=0,
                content_type="", content="", success=False, error=error_msg
            )
            return ToolResult(
                success=False,
                output=None,
                error=error_msg,
                metadata={"url": url}
            )
        except Exception as e:
            error_msg = f"Failed to fetch {url}: {str(e)}"
            logger.log_fetch(
                url=url, final_url=url, status_code=0,
                content_type="", content="", success=False, error=error_msg
            )
            return ToolResult(
                success=False,
                output=None,
                error=error_msg,
                metadata={"url": url}
            )

    def _html_to_text(self, html: str) -> str:
        """Convert HTML to readable text using best available method."""
        # Method 1: html2text (best quality)
        if self._html2text_converter:
            try:
                text = self._html2text_converter.handle(html)
                # Clean up excessive newlines
                text = re.sub(r'\n{3,}', '\n\n', text)
                return text.strip()
            except Exception:
                pass  # Fall through to next method

        # Method 2: BeautifulSoup (good quality)
        if BS4_AVAILABLE:
            try:
                soup = BeautifulSoup(html, 'html.parser')

                # Remove script and style elements
                for element in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
                    element.decompose()

                # Get text and clean up
                text = soup.get_text(separator=' ', strip=True)
                text = re.sub(r'\s+', ' ', text)
                return text.strip()
            except Exception:
                pass  # Fall through to regex method

        # Method 3: Regex fallback (basic)
        text = html
        # Remove script and style blocks
        text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        # Remove HTML comments
        text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
        # Replace block elements with newlines
        text = re.sub(r'<(p|div|br|h[1-6]|li)[^>]*>', '\n', text, flags=re.IGNORECASE)
        # Remove remaining tags
        text = re.sub(r'<[^>]+>', ' ', text)
        # Clean up whitespace
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n\s*\n', '\n\n', text)
        return text.strip()

    def _extract_title(self, html: str) -> str:
        """Extract page title from HTML."""
        # Try regex first (fast)
        match = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
        if match:
            return match.group(1).strip()

        # Try BeautifulSoup if available
        if BS4_AVAILABLE:
            try:
                soup = BeautifulSoup(html[:5000], 'html.parser')
                title_tag = soup.find('title')
                if title_tag:
                    return title_tag.get_text(strip=True)
            except Exception:
                pass

        return ""

    def _pdf_to_text(self, pdf_bytes: bytes) -> str:
        """Extract text from PDF bytes."""
        if not PYPDF_AVAILABLE:
            return "[PDF extraction unavailable - install pypdf: pip install pypdf]"

        try:
            pdf_file = io.BytesIO(pdf_bytes)
            reader = pypdf.PdfReader(pdf_file)

            text_parts = []
            for page_num, page in enumerate(reader.pages):
                try:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(f"--- Page {page_num + 1} ---\n{page_text}")
                except Exception as e:
                    text_parts.append(f"--- Page {page_num + 1} ---\n[Error extracting: {e}]")

            return "\n\n".join(text_parts)
        except Exception as e:
            return f"[PDF extraction failed: {e}]"

    def _extract_with_llm(self, content: str, prompt: str, url: str) -> str:
        """
        Use a fast LLM to extract relevant information from content.

        This allows the main agent to receive focused, relevant information
        instead of truncated raw content that may miss important details.
        """
        try:
            # Import here to avoid circular imports
            from ...llm import LLMClient, Message

            # Create client with fast model
            client = LLMClient(
                model=FAST_MODEL,
                temperature=0.0,
                max_tokens=2000
            )

            # Build extraction prompt
            system_prompt = """You are a content extraction assistant. Your job is to extract specific information from documents based on the user's request.

Rules:
- Extract ONLY the information requested
- Be precise and include exact values, numbers, and specifications
- If the requested information is not found, say so clearly
- Keep your response focused and concise
- Include relevant context when it helps understand the extracted information"""

            user_prompt = f"""From the following document fetched from {url}, please {prompt}

---
DOCUMENT CONTENT:
---
{content}
---

Extract the requested information:"""

            messages = [
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_prompt)
            ]

            response = client.chat(messages)
            return response.content

        except Exception as e:
            # If LLM extraction fails, fall back to truncated content
            print(f"⚠️ LLM extraction failed: {e}, falling back to truncated content")
            return content[:WEB_FETCH_DISPLAY_LIMIT]

    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


def get_tool() -> WebTool:
    """Factory function for tool discovery."""
    return WebTool()
