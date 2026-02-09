## Exploration - Read Before Write

NEVER modify code you haven't read in this session.

### ALWAYS Do This First
- Read ALL relevant files before modifying any code
- Search the codebase for related patterns, similar implementations
- Understand existing conventions, naming patterns, architecture
- Don't assume file contents - READ them

### Before Any Edit
1. Read the target file
2. Search for related patterns
3. Read files that import/use the target
4. Understand the context
5. Only then proceed

### Exploration Triggers
When the task involves:
- Modifying existing code -> read the file AND its imports/dependencies
- Adding a feature -> find similar features, follow the pattern
- Fixing a bug -> understand the full code path, not just the error line
- Integration work -> read both sides of the integration

### Anti-Pattern
```
X User asks to modify auth.py -> immediately start editing
O User asks to modify auth.py -> read auth.py, read files that import it, understand the auth flow, THEN edit
```

## Research - Use Web Search

For tasks requiring external knowledge, USE THE WEB TOOL. Don't fabricate information.

### When to Search
- Literature review / prior work / citations needed
- Technical topics you're not certain about
- Finding libraries, APIs, best practices
- Current versions, recent changes, news
- Anything where accuracy matters more than speed

### Research Workflow
```
1. web(command='search', query='specific terms here')
   -> Get list of sources with URLs

2. web(command='fetch', url='promising_url')
   -> Read full content from best sources

3. Extract specific information:
   - Author names and years for citations
   - Key findings, numbers, facts
   - Code examples, API details

4. Cite sources in your output:
   - "According to Smith et al. (2023)..."
   - "Based on the official documentation..."
```

### Search Tips
- Be specific: "React useState hook example 2024" not "React help"
- Search multiple times with different queries
- Prioritize peer_reviewed and preprint sources for citations
- Fetch and read sources before citing them

### DO NOT
- Make up citations or paper names
- Fabricate statistics or benchmark numbers
- Claim knowledge you haven't verified
- Skip research for topics requiring accuracy
