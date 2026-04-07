#!/usr/bin/env python3
"""
Daily Hub Content Update
────────────────────────
Fetches live news via RSS, then calls the Claude API to generate/summarise
content for every data file. Finally commits and pushes to GitHub so the
live site updates automatically.

Run manually:  python3 tools/daily_update.py
Scheduled by:  GitHub Actions (.github/workflows/daily-update.yml)
               OR ~/Library/LaunchAgents/com.alfieedworthy.daily-hub.plist
"""

import subprocess
import datetime
import os
import re
import sys
import time
import random

try:
    import feedparser
except ImportError:
    print("Installing feedparser...")
    subprocess.run([sys.executable, '-m', 'pip', 'install', 'feedparser'], check=True)
    import feedparser

import glob
import ssl
import urllib.request

# Fix SSL certificate verification on Mac (common Python install issue)
ssl._create_default_https_context = ssl._create_unverified_context

# ── Load .env ─────────────────────────────────────────────────────────────────
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
if os.path.exists(_env_path):
    with open(_env_path) as _ef:
        for _line in _ef:
            _line = _line.strip()
            if _line and not _line.startswith('#') and '=' in _line:
                _k, _v = _line.split('=', 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"\''))

# ── Config ────────────────────────────────────────────────────────────────────

TODAY      = datetime.date.today().isoformat()
LOG_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'daily_update.log')

def find_repo_root():
    """Walk up from the script looking for a .git directory."""
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(d, '.git')):
            return d
        d = os.path.dirname(d)
    # Fallback: known location
    return os.path.expanduser('~/Documents/AI/Personal Hub')

REPO_DIR = find_repo_root()

def find_claude():
    """Find the Claude Code binary, handling version number changes."""
    # Check standard PATH first
    r = subprocess.run(['which', 'claude'], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    # Fall back to known install locations (sorted so highest version wins)
    patterns = [
        os.path.expanduser('~/Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude'),
        os.path.expanduser('~/.local/bin/claude'),
        '/usr/local/bin/claude',
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]  # highest version
    return None

CLAUDE_BIN = find_claude()

# ── Helpers ───────────────────────────────────────────────────────────────────

def log(msg):
    line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line)
    with open(LOG_FILE, 'a') as f:
        f.write(line + '\n')

def call_claude(prompt, timeout=180, max_tokens=4096):
    """Call Claude — uses Anthropic Python SDK (works locally and in GitHub Actions)."""
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if api_key:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        for attempt in range(4):
            try:
                message = client.messages.create(
                    model='claude-opus-4-6',
                    max_tokens=max_tokens,
                    messages=[{'role': 'user', 'content': prompt}]
                )
                return message.content[0].text.strip()
            except anthropic.APIStatusError as e:
                if e.status_code in (500, 529, 429) and attempt < 3:
                    wait = 30 * (attempt + 1)
                    log(f"  [retry] API error {e.status_code}, waiting {wait}s (attempt {attempt+1}/4)...")
                    time.sleep(wait)
                else:
                    log(f"  [warning] Anthropic SDK failed after retries: {e}")
                    break
            except Exception as e:
                log(f"  [warning] Anthropic SDK failed: {e}")
                break
        return None  # API key set but all attempts failed — skip this file gracefully

    # Fallback: Claude CLI (local Mac only, no API key set)
    if not CLAUDE_BIN:
        log("  [fatal] No ANTHROPIC_API_KEY set and Claude CLI not found.")
        sys.exit(1)
    try:
        result = subprocess.run(
            [CLAUDE_BIN, '-p', prompt,
             '--dangerously-skip-permissions',
             '--output-format', 'text'],
            capture_output=True, text=True, timeout=timeout, cwd='/tmp'
        )
        if result.returncode != 0:
            log(f"  [claude error] {result.stderr.strip()[:200]}")
            return None
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        log("  [timeout] Claude took too long — skipping this file")
        return None

def extract_js(text):
    """Strip markdown fences, isolate the JS block, validate output."""
    if not text:
        return None
    # Strip outer code fences if present
    text = re.sub(r'^```(?:javascript|js)?\s*\n?', '', text.strip())
    text = re.sub(r'\n?```\s*$', '', text)
    text = text.strip()
    # Reject permission prompts
    if 'grant permission' in text.lower() or 'waiting for your permission' in text.lower():
        log("  [error] Claude returned a permission request — skipping")
        return None
    # If Claude appended a prose summary after the JS, truncate at the last }; or ];
    # Find the last top-level statement terminator
    last_js = max(text.rfind('};'), text.rfind('];'))
    if last_js != -1 and last_js < len(text) - 3:
        trailing = text[last_js + 2:].strip()
        if trailing:  # there is non-JS text after the last };
            log(f"  [info] Trimmed trailing prose ({len(trailing)} chars) from Claude output")
            text = text[:last_js + 2]
    # Reject if the output doesn't start with a JS statement
    first_line = text.lstrip().split('\n')[0]
    if not any(first_line.startswith(kw) for kw in ('var ', 'window.', 'const ', 'let ', '//')):
        log(f"  [error] Output doesn't look like JavaScript (starts with: {first_line[:60]!r}) — skipping")
        return None
    # Reject if braces/brackets are unbalanced (truncated response)
    brace_balance  = text.count('{') - text.count('}')
    bracket_balance = text.count('[') - text.count(']')
    if brace_balance != 0 or bracket_balance != 0:
        log(f"  [error] JS is unbalanced (braces={brace_balance} arrays={bracket_balance}) — Claude response was likely truncated, skipping")
        return None
    return text.strip()

def write_file(filename, content):
    path = os.path.join(REPO_DIR, filename)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)
    log(f"  ✓ Written: {filename}")
    return filename

import json
import urllib.parse

def fetch_book_cover(title, author):
    """Fetch book cover URL and Amazon UK URL from Open Library (no API key needed)."""
    try:
        q = urllib.parse.quote(f'{title} {author}')
        url = f'https://openlibrary.org/search.json?q={q}&limit=1&fields=cover_i,isbn'
        req = urllib.request.Request(url, headers={'User-Agent': 'PersonalHub/1.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        docs = data.get('docs', [])
        if not docs:
            return None, None
        doc = docs[0]
        cover_url = None
        if doc.get('cover_i'):
            cover_url = f'https://covers.openlibrary.org/b/id/{doc["cover_i"]}-L.jpg'
        # Pick a 10-digit ISBN (ISBN-10) for Amazon dp URL, preferring UK editions
        amazon_url = None
        isbns = doc.get('isbn', [])
        isbn10 = next((i for i in isbns if len(i) == 10), None)
        if isbn10:
            amazon_url = f'https://www.amazon.co.uk/dp/{isbn10}'
        return cover_url, amazon_url
    except Exception as e:
        log(f'  [warning] book cover fetch failed: {e}')
    return None, None

def fetch_film_poster(title, year):
    """Fetch film poster from Wikipedia (no API key needed)."""
    variants = [
        f'{title} ({year} film)',
        f'{title} (film)',
        title,
    ]
    for variant in variants:
        try:
            slug = urllib.parse.quote(variant.replace(' ', '_'))
            url = f'https://en.wikipedia.org/api/rest_v1/page/summary/{slug}'
            req = urllib.request.Request(url, headers={'User-Agent': 'PersonalHub/1.0'})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            thumb = data.get('originalimage', data.get('thumbnail', {})).get('source')
            if thumb:
                return thumb
        except Exception:
            continue
    return None

def fetch_wikipedia_image(name):
    """Fetch an image for a person, place, org, or topic from Wikipedia (free, no API key needed).
    For news titles, tries the full title first, then extracts likely proper nouns to search individually."""
    import string

    def _try_slug(term):
        try:
            slug = urllib.parse.quote(term.strip().replace(' ', '_'))
            url = f'https://en.wikipedia.org/api/rest_v1/page/summary/{slug}'
            req = urllib.request.Request(url, headers={'User-Agent': 'PersonalHub/1.0'})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            return data.get('originalimage', data.get('thumbnail', {})).get('source')
        except Exception:
            return None

    # 1. Try the name/title directly
    result = _try_slug(name)
    if result:
        return result

    # 2. Extract capitalised multi-word phrases (likely proper nouns: people, orgs, places)
    #    Split on common stop words and punctuation, keep runs of Title Case words
    stop = {'the','a','an','and','or','but','in','on','at','to','for','of','with','by',
            'as','is','are','was','were','has','have','had','be','been','that','this',
            'from','after','over','amid','says','say','amid','amid','its','their'}
    words = re.sub(r'[^\w\s\-]', ' ', name).split()
    # Build runs of capitalised words (excluding sentence-start heuristic)
    candidates = []
    current = []
    for w in words:
        if w[0].isupper() and w.lower() not in stop:
            current.append(w)
        else:
            if len(current) >= 1:
                candidates.append(' '.join(current))
            current = []
    if current:
        candidates.append(' '.join(current))

    # Try longest candidates first (more specific = better Wikipedia match)
    candidates.sort(key=len, reverse=True)
    for candidate in candidates[:4]:
        if len(candidate) < 3:
            continue
        result = _try_slug(candidate)
        if result:
            return result

    return None

def fetch_pexels_image(query, orientation='landscape'):
    """Fetch a relevant photo from Pexels free API (requires PEXELS_API_KEY in .env)."""
    try:
        key = os.environ.get('PEXELS_API_KEY', '')
        if not key:
            return None
        url = ('https://api.pexels.com/v1/search?query='
               + urllib.parse.quote(query)
               + '&per_page=3&orientation=' + orientation)
        req = urllib.request.Request(url, headers={'Authorization': key, 'User-Agent': 'PersonalHub/1.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        photos = data.get('photos', [])
        if photos:
            src = photos[0]['src']
            return src.get('large2x') or src.get('large') or src.get('original')
    except Exception as e:
        log(f'  [warning] Pexels fetch failed for "{query}": {e}')
    return None


def upgrade_image_url(url):
    """Bump known CDN URLs to a larger size."""
    if not url:
        return url
    # BBC: standard/240 → standard/1536
    url = re.sub(r'(ichef\.bbci\.co\.uk/ace/standard/)\d+/', r'\g<1>1536/', url)
    # Guardian: width=140 → width=1200
    url = re.sub(r'(i\.guim\.co\.uk/.+?width=)\d+', r'\g<1>1200', url)
    return url

def extract_rss_image(entry):
    """Try every common RSS image location, return first URL found (full size)."""
    # media:content
    for m in entry.get('media_content', []):
        if m.get('url') and 'image' in m.get('medium', 'image'):
            return upgrade_image_url(m['url'])
    # media:thumbnail
    for m in entry.get('media_thumbnail', []):
        if m.get('url'):
            return upgrade_image_url(m['url'])
    # enclosures
    for enc in entry.get('enclosures', []):
        if enc.get('type', '').startswith('image/') and enc.get('href'):
            return upgrade_image_url(enc['href'])
    # <img> tag inside summary/description HTML
    raw = entry.get('summary', entry.get('description', ''))
    img_m = re.search(r'<img[^>]+src=[\"\'](.*?)[\"\']', raw)
    if img_m:
        return upgrade_image_url(img_m.group(1))
    return None

def fetch_rss(*urls, max_per_feed=4):
    """Fetch multiple RSS feeds, return combined list of article dicts (deduplicated by title)."""
    import socket
    articles = []
    seen_titles = set()
    for url in urls:
        try:
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(15)  # 15s max per feed — prevents hanging
            try:
                feed = feedparser.parse(url)
            finally:
                socket.setdefaulttimeout(old_timeout)
            for entry in feed.entries[:max_per_feed]:
                title = entry.get('title', '').strip()
                # Normalise for comparison: lowercase, strip punctuation
                norm = re.sub(r'[^a-z0-9 ]', '', title.lower()).strip()
                # Skip if we've seen a very similar title already
                if norm in seen_titles:
                    continue
                # Also skip if first 6 words match an existing title (catches same story with slightly different headline)
                prefix = ' '.join(norm.split()[:6])
                if any(t.startswith(prefix) for t in seen_titles if prefix):
                    continue
                seen_titles.add(norm)
                raw_summary = entry.get('summary', entry.get('description', ''))
                clean_summary = re.sub(r'<[^>]+>', '', raw_summary).strip()
                articles.append({
                    'title':   title,
                    'summary': clean_summary[:400],
                    'source':  feed.feed.get('title', url),
                    'link':    entry.get('link', ''),
                    'image':   extract_rss_image(entry),
                })
        except Exception as e:
            log(f"  [warning] RSS failed for {url}: {e}")
    return articles

def articles_to_text(articles, max=20):
    lines = []
    for i, a in enumerate(articles[:max], 1):
        lines.append(f"{i}. [{a['source']}] {a['title']}")
        if a.get('link'):
            lines.append(f"   URL: {a['link']}")
        if a['summary']:
            lines.append(f"   {a['summary']}")
    return '\n'.join(lines)

# ── Image pools (by category) ─────────────────────────────────────────────────
# Using curated Unsplash IDs so images are stable and load reliably.

IMAGES = {
    'world': [
        'https://images.unsplash.com/photo-1569163139394-de4e4f43e4e3?w=1200&auto=format&fit=crop',
        'https://images.unsplash.com/photo-1504711434969-e33886168f5c?w=1200&auto=format&fit=crop',
        'https://images.unsplash.com/photo-1526470498-9ae73c665de8?w=1200&auto=format&fit=crop',
        'https://images.unsplash.com/photo-1585776245991-cf89dd7fc73a?w=1200&auto=format&fit=crop',
    ],
    'uk': [
        'https://images.unsplash.com/photo-1529107386315-e1a2ed48a620?w=1200&auto=format&fit=crop',
        'https://images.unsplash.com/photo-1580130775562-0ef92da028de?w=1200&auto=format&fit=crop',
        'https://images.unsplash.com/photo-1513635269975-59663e0ac1ad?w=1200&auto=format&fit=crop',
    ],
    'us': [
        'https://images.unsplash.com/photo-1617531653332-bd46c16f4d68?w=1200&auto=format&fit=crop',
        'https://images.unsplash.com/photo-1508847154043-be5407fcaa5a?w=1200&auto=format&fit=crop',
        'https://images.unsplash.com/photo-1541872703-74c5e44368f9?w=1200&auto=format&fit=crop',
    ],
    'financial': [
        'https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=1200&auto=format&fit=crop',
        'https://images.unsplash.com/photo-1542744173-8e7e53415bb0?w=1200&auto=format&fit=crop',
        'https://images.unsplash.com/photo-1460925895917-afdab827c52f?w=1200&auto=format&fit=crop',
    ],
    'tech': [
        'https://images.unsplash.com/photo-1518770660439-4636190af475?w=1200&auto=format&fit=crop',
        'https://images.unsplash.com/photo-1550751827-4bd374c3f58b?w=1200&auto=format&fit=crop',
        'https://images.unsplash.com/photo-1451187580459-43490279c0fa?w=1200&auto=format&fit=crop',
    ],
    'secondary': [
        'https://images.unsplash.com/photo-1504711434969-e33886168f5c?w=800&auto=format&fit=crop',
        'https://images.unsplash.com/photo-1526470498-9ae73c665de8?w=800&auto=format&fit=crop',
        'https://images.unsplash.com/photo-1580130775562-0ef92da028de?w=800&auto=format&fit=crop',
        'https://images.unsplash.com/photo-1568454537842-d933259bb258?w=800&auto=format&fit=crop',
        'https://images.unsplash.com/photo-1585776245991-cf89dd7fc73a?w=800&auto=format&fit=crop',
    ]
}

def img(category='world'):
    return random.choice(IMAGES.get(category, IMAGES['world']))

def sec_imgs():
    pool = IMAGES['secondary'][:]
    random.shuffle(pool)
    return pool[:3]

# ── RSS feed definitions ───────────────────────────────────────────────────────

RSS = {
    'world': [
        'https://feeds.bbci.co.uk/news/world/rss.xml',
        'https://www.theguardian.com/world/rss',
        'https://feeds.npr.org/1004/rss.xml',                      # NPR World
        'https://rss.nytimes.com/services/xml/rss/nyt/World.xml',  # NYT World
        'https://feeds.reuters.com/reuters/worldNews',             # Reuters World
        'https://www.aljazeera.com/xml/rss/all.xml',               # Al Jazeera
    ],
    'uk_politics': [
        'https://feeds.bbci.co.uk/news/politics/rss.xml',
        'https://www.theguardian.com/politics/rss',
        'https://www.independent.co.uk/news/uk/politics/rss',      # The Independent
        'https://www.telegraph.co.uk/politics/rss.xml',            # The Telegraph
        'https://feeds.skynews.com/feeds/rss/politics.xml',        # Sky News Politics
    ],
    'us_politics': [
        'https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml',
        'https://feeds.npr.org/1014/rss.xml',                      # NPR Politics
        'https://rss.nytimes.com/services/xml/rss/nyt/Politics.xml', # NYT Politics
        'https://feeds.washingtonpost.com/rss/politics',           # Washington Post
        'https://thehill.com/rss/syndicator/19110',                # The Hill
        'https://feeds.reuters.com/Reuters/PoliticsNews',          # Reuters Politics
    ],
    'financial': [
        'https://feeds.bbci.co.uk/news/business/rss.xml',
        'https://www.theguardian.com/uk/business/rss',
        'https://feeds.ft.com/rss/companies',
        'https://feeds.reuters.com/reuters/businessNews',
        'https://rss.nytimes.com/services/xml/rss/nyt/Business.xml', # NYT Business
        'https://feeds.marketwatch.com/marketwatch/topstories/',   # MarketWatch
        'https://feeds.bloomberg.com/markets/news.rss',            # Bloomberg Markets
    ],
    'tech': [
        'https://feeds.bbci.co.uk/news/technology/rss.xml',
        'https://www.theguardian.com/uk/technology/rss',
        'https://feeds.arstechnica.com/arstechnica/index',         # Ars Technica
        'https://www.wired.com/feed/rss',                          # Wired
        'https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml', # NYT Tech
        'https://feeds.reuters.com/reuters/technologyNews',        # Reuters Tech
        'https://feeds.feedburner.com/TechCrunch/',                # TechCrunch
    ],
}

# ── News generator ─────────────────────────────────────────────────────────────

def gen_news(category, var_name, img_key, secondary_ids, focus_hint='', all_articles=None):
    """Fetch RSS and use Claude to write a structured news briefing."""
    log(f"\n── {category.replace('_', ' ').title()} news")
    articles = all_articles if all_articles is not None else fetch_rss(*RSS[category])
    if not articles:
        log("  [skip] No articles fetched from RSS")
        return None

    art_text = articles_to_text(articles)
    main_img = img(img_key)
    s_imgs   = sec_imgs()

    prompt = f"""You are writing a daily news briefing for a personal reading website. Today is {TODAY}.

Here are today's real headlines from reputable news sources:
{art_text}

Output ONLY valid JavaScript — absolutely no explanation, no markdown, no preamble. Start directly with "var {var_name}".

Use only real stories from the headlines above. Write 5 substantial, well-crafted paragraphs for the main story (each at least 3 sentences). Pick the most significant story as the main piece. Write 3 secondary stories with a one-sentence summary each.{(' ' + focus_hint) if focus_hint else ''}

IMPORTANT: Each story above includes a URL field — use the exact URL provided for that story in the sourceUrl/url fields below.

var {var_name} = {{
  date: "{TODAY}",
  main: {{
    title: "THE MOST SIGNIFICANT HEADLINE FROM ABOVE",
    category: "CATEGORY (e.g. Politics, Economics, International)",
    content: ["paragraph 1 (3+ sentences)", "paragraph 2 (3+ sentences)", "paragraph 3 (3+ sentences)", "paragraph 4 (3+ sentences)", "paragraph 5 (3+ sentences)"],
    image: "__IMG_MAIN__",
    source: "SOURCE NAME",
    sourceUrl: "EXACT URL FROM THE ARTICLE ABOVE"
  }},
  secondary: [
    {{ id: "{secondary_ids[0]}", title: "SECOND STORY TITLE", summary: "One sentence summary.", image: "__IMG_S1__", source: "SOURCE", url: "EXACT URL FROM THE ARTICLE ABOVE", category: "CATEGORY" }},
    {{ id: "{secondary_ids[1]}", title: "THIRD STORY TITLE", summary: "One sentence summary.", image: "__IMG_S2__", source: "SOURCE", url: "EXACT URL FROM THE ARTICLE ABOVE", category: "CATEGORY" }},
    {{ id: "{secondary_ids[2]}", title: "FOURTH STORY TITLE", summary: "One sentence summary.", image: "__IMG_S3__", source: "SOURCE", url: "EXACT URL FROM THE ARTICLE ABOVE", category: "CATEGORY" }}
  ]
}};"""

    js = extract_js(call_claude(prompt))
    if not js:
        return None
    # Inject images: RSS article image → Pexels → Unsplash fallback
    try:
        titles = re.findall(r'title:\s*["\'](.+?)["\']', js)
        # Build a title→rss_image lookup from fetched articles
        rss_lookup = {a['title']: a['image'] for a in articles if a.get('image')}
        fallbacks = [img(img_key)] + sec_imgs()
        sentinels = ['__IMG_MAIN__', '__IMG_S1__', '__IMG_S2__', '__IMG_S3__']
        for i, sentinel in enumerate(sentinels):
            if sentinel not in js:
                continue
            title = titles[i] if i < len(titles) else ''
            url = None

            # 1. Try RSS image — exact match first, then fuzzy word-overlap match
            rss_url = rss_lookup.get(title)
            if not rss_url:
                # Fuzzy: score by how many words the titles share
                title_words = set(title.lower().split())
                best_score, best_img = 0, None
                for rss_title, rss_img in rss_lookup.items():
                    if not rss_img:
                        continue
                    shared = len(title_words & set(rss_title.lower().split()))
                    if shared > best_score:
                        best_score, best_img = shared, rss_img
                if best_score >= 3:  # at least 3 words in common
                    rss_url = best_img
            if rss_url:
                url = rss_url
                log(f'  ✓ RSS image [{i}]: {title[:40]}')

            # 2. Pexels using the article title as the search query
            if not url:
                url = fetch_pexels_image(title or category)
                if url:
                    log(f'  ✓ Pexels image [{i}]: {title[:40]}')

            # 3. Unsplash fallback
            if not url:
                url = fallbacks[i] if i < len(fallbacks) else img(img_key)
                log(f'  ✓ Unsplash fallback [{i}]: {title[:40]}')

            js = js.replace(f'"{sentinel}"', f'"{url}"', 1)
    except Exception as e:
        log(f'  [warning] News image injection failed: {e}')
        for sentinel in ['__IMG_MAIN__', '__IMG_S1__', '__IMG_S2__', '__IMG_S3__']:
            js = js.replace(f'"{sentinel}"', f'"{img(img_key)}"')
    return js

# ── AI-generated content ───────────────────────────────────────────────────────

def gen_quiz():
    log("\n── Quiz of the day")
    prompt = f"""Generate a general knowledge question of the day for {TODAY}.
Choose from topics the user enjoys: Ancient History, History, British Politics, Geography, Sport, Literature, Science, Art, Music, Philosophy.
Make sure the question is genuinely interesting and non-trivial — not too easy.

Output ONLY this JavaScript. No explanation, no markdown. Start directly with "window.QUIZ_DATA".

window.QUIZ_DATA = {{
  date: '{TODAY}',
  category: 'CATEGORY',
  question: 'Full question here?',
  answer: 'Concise but complete answer.',
  funFact: 'An interesting elaboration in 2-3 sentences that the reader will find satisfying.'
}};"""
    return extract_js(call_claude(prompt, timeout=60))


def gen_philosophy():
    log("\n── Philosophy Corner")
    prompt = f"""Generate a daily philosophy article for a personal learning website. Today is {TODAY}.
Choose a significant philosophical theory, thinker, or concept. Avoid: Kant's Categorical Imperative (already covered).
Write in the style of a high-quality long-read magazine — engaging, intelligent, structured with subheadings.
Main article: EXACTLY 8 content blocks (mix of paragraphs and headings). Each paragraph: 4-5 sentences.
Counter theories: 2 real philosophers who challenged this theory, 2-3 paragraph argument each.
Philosopher of the Day: a real historical philosopher (different from the main theory's thinker), accurate biography.

Output ONLY this JavaScript. No explanation, no markdown. Start directly with "window.PHILOSOPHY_DATA".

window.PHILOSOPHY_DATA = {{
  date: '{TODAY}',
  mainTheory: {{
    title: 'TITLE',
    subject: 'The primary philosopher, movement, or concept (e.g. \'Plato\', \'Stoicism\', \'The Allegory of the Cave\'). Used for image search.',
    subtitle: 'A compelling one-sentence hook subtitle.',
    readTime: 'X min',
    image: '__IMG_PHILOSOPHY__',
    content: [
      {{ type: 'paragraph', text: '...' }},
      {{ type: 'heading', text: '...' }},
      {{ type: 'paragraph', text: '...' }},
      {{ type: 'heading', text: '...' }},
      {{ type: 'paragraph', text: '...' }},
      {{ type: 'heading', text: '...' }},
      {{ type: 'paragraph', text: '...' }},
      {{ type: 'paragraph', text: '...' }}
    ]
  }},
  keyTakeaways: [
    'Key point 1',
    'Key point 2',
    'Key point 3',
    'Key point 4'
  ],
  counterTheories: [
    {{
      philosopher: 'PHILOSOPHER NAME',
      period: 'c. YYYY–YYYY',
      school: 'School of thought',
      argument: [
        {{ type: 'paragraph', text: '...' }},
        {{ type: 'paragraph', text: '...' }},
        {{ type: 'paragraph', text: '...' }}
      ],
      contrast: 'One sentence contrasting their view with the main theory.'
    }},
    {{
      philosopher: 'PHILOSOPHER NAME',
      period: 'c. YYYY–YYYY',
      school: 'School of thought',
      argument: [
        {{ type: 'paragraph', text: '...' }},
        {{ type: 'paragraph', text: '...' }}
      ],
      contrast: 'One sentence contrasting their view with the main theory.'
    }}
  ],
  whyItMatters: {{
    content: [
      {{ type: 'paragraph', text: '...' }},
      {{ type: 'paragraph', text: '...' }}
    ]
  }},
  philosopherOfTheDay: {{
    name: 'FULL NAME',
    lifespan: 'YYYY–YYYY',
    category: 'e.g. Metaphysics & Epistemology',
    image: null,
    bio: '3-paragraph biography as a single string. Use \\\\n\\\\n to separate paragraphs.',
    contributions: 'Their main philosophical contributions in 2-3 sentences.',
    rivals: 'Their key intellectual rivals or critics in 1-2 sentences.'
  }}
}};"""
    js = extract_js(call_claude(prompt, timeout=420))
    if not js:
        return None
    # Inject image for the main theory: Wikipedia first (person/place/concept), then Pexels
    try:
        title_m   = re.search(r'mainTheory\s*:\s*\{[^{]*?title\s*:\s*["\'](.+?)["\']', js, re.DOTALL)
        subject_m = re.search(r'mainTheory\s*:\s*\{[^{]*?subject\s*:\s*["\'](.+?)["\']', js, re.DOTALL)
        phil_m    = re.search(r'philosopherOfTheDay\s*:\s*\{[^{]*?name\s*:\s*["\'](.+?)["\']', js, re.DOTALL)
        if '__IMG_PHILOSOPHY__' in js:
            title_str   = title_m.group(1)   if title_m   else ''
            subject_str = subject_m.group(1) if subject_m else ''
            search_term = subject_str or title_str or 'philosophy'
            # Try Wikipedia first (great for named philosophers, concepts, places)
            url = fetch_wikipedia_image(search_term)
            if url:
                log(f'  ✓ Philosophy main image (Wikipedia): {search_term[:40]}')
            else:
                url = fetch_pexels_image(search_term + ' philosophy') or 'https://images.unsplash.com/photo-1481627834876-b7833e8f5570?w=1200&fit=crop&q=80'
                log(f'  ✓ Philosophy main image (Pexels): {search_term[:40]}')
            js = js.replace("'__IMG_PHILOSOPHY__'", f"'{url}'", 1)
            js = js.replace('"__IMG_PHILOSOPHY__"', f'"{url}"', 1)
        if 'image: null' in js and phil_m:
            name = phil_m.group(1)
            url = fetch_wikipedia_image(name)
            if url:
                log(f'  ✓ Philosopher of day image (Wikipedia): {name[:40]}')
            else:
                url = fetch_pexels_image(name + ' philosopher portrait')
                if url:
                    log(f'  ✓ Philosopher of day image (Pexels): {name[:40]}')
            if url:
                js = js.replace('image: null', f'image: "{url}"', 1)
    except Exception as e:
        log(f'  [warning] Philosophy image injection failed: {e}')
        js = js.replace("'__IMG_PHILOSOPHY__'", "'https://images.unsplash.com/photo-1481627834876-b7833e8f5570?w=1200&fit=crop&q=80'", 1)
        js = js.replace('"__IMG_PHILOSOPHY__"', '"https://images.unsplash.com/photo-1481627834876-b7833e8f5570?w=1200&fit=crop&q=80"', 1)
    return js


def gen_rics():
    log("\n── RICS Study")
    # Curated Unsplash images relevant to property, planning & development
    rics_images = [
        'https://images.unsplash.com/photo-1486406146926-c627a92ad1ab?w=1200&auto=format&fit=crop',  # glass office towers
        'https://images.unsplash.com/photo-1460317442991-0ec209397118?w=1200&auto=format&fit=crop',  # city skyline at dusk
        'https://images.unsplash.com/photo-1503387762-592deb58ef4e?w=1200&auto=format&fit=crop',     # construction site
        'https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=1200&auto=format&fit=crop',     # architectural model
        'https://images.unsplash.com/photo-1574920162043-b872873f19bc?w=1200&auto=format&fit=crop',  # blueprints / plans
        'https://images.unsplash.com/photo-1497366216548-37526070297c?w=1200&auto=format&fit=crop',  # modern office interior
        'https://images.unsplash.com/photo-1554469384-e58fac16e23a?w=1200&auto=format&fit=crop',     # modern building facade
        'https://images.unsplash.com/photo-1582407947304-fd86f028f716?w=1200&auto=format&fit=crop',  # premium office building
        'https://images.unsplash.com/photo-1486325212027-8081e485255e?w=1200&auto=format&fit=crop',  # glass curtain wall
        'https://images.unsplash.com/photo-1590846406792-0adc7f938f1d?w=1200&auto=format&fit=crop',  # construction crane
        'https://images.unsplash.com/photo-1560518883-ce09059eeffa?w=1200&auto=format&fit=crop',     # house keys (property)
        'https://images.unsplash.com/photo-1449824913935-59a10b8d2000?w=1200&auto=format&fit=crop',  # city street view
        'https://images.unsplash.com/photo-1545324418-cc1a3fa10c00?w=1200&auto=format&fit=crop',     # residential block
        'https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=1200&auto=format&fit=crop',  # financial data screen
        'https://images.unsplash.com/photo-1450101499163-c8848c66ca85?w=1200&auto=format&fit=crop',  # professional meeting
        'https://images.unsplash.com/photo-1529107386315-e1a2ed48a620?w=1200&auto=format&fit=crop',  # Houses of Parliament
        'https://images.unsplash.com/photo-1470723710355-95304d8aece4?w=1200&auto=format&fit=crop',  # urban skyline
        'https://images.unsplash.com/photo-1542314831-068cd1dbfeeb?w=1200&auto=format&fit=crop',     # property exterior
    ]
    images_list = '\n'.join(f'  {i+1}. {url}' for i, url in enumerate(rics_images))

    prompt = f"""Generate a daily RICS APC study lesson for {TODAY} for the Planning and Development (P&D) pathway.

CANDIDATE PROFILE: Assistant Development Manager at Latimer by Clarion Housing Group — the development arm of one of the UK's largest housing associations. Day-to-day responsibilities include: managing development appraisals and viability, coordinating with planners and LPAs on S106/CIL, overseeing project delivery timelines, liaising with contractors and consultants, managing grant funding (Homes England AHP), and supporting land acquisition. All examples, worked scenarios, and APC tips must be grounded in this residential-led, affordable housing, housing association context.

TOPIC ROTATION — cycle fairly through these competencies, picking whichever has been least recently covered:
  Level 3 (deep mastery): Development Appraisals, Development/Project Briefs, Project Finance, Planning and Development Management
  Level 2 (working knowledge): Masterplanning and Urban Design, Spatial Policy and Infrastructure, Legal/Regulatory Compliance, Valuation
  Level 1 (awareness): Measurement, Surveying and Mapping

RULES:
- Pick a NICHE SUB-TOPIC, not a broad overview. Bad: "Introduction to Development Appraisals". Good: "Benchmark Land Value vs Market Value in Affordable Housing Viability", "Grant Funding Stacks and Homes England AHP Conditions", "Design Codes and the NPPF 2024 Requirements for Masterplans", "Revenue Recognition under IFRS 15 for RP Development Programmes"
- Every worked example must reference housing association/affordable housing scenarios — no generic commercial examples
- Write at a high level — APC preparation for a practitioner, not a student
- Content blocks: mix of paragraphs, headings, callout (APC tips with worked examples), and key_term blocks
- At least 10 content blocks
- 5 specific technical Q&A pairs an APC assessor would ask this candidate
- 2-3 news items relevant to the topic from UK housing/planning (plausible recent news)
- Pick the MOST RELEVANT image from the list below

Available images:
{images_list}

Output ONLY valid JavaScript. No explanation, no markdown. Start directly with "var RICS_DATA".

var RICS_DATA = {{
  date: "{TODAY}",
  topic: "NICHE SPECIFIC TOPIC TITLE",
  module: "MODULE NAME",
  level: 3,
  apc_competency: "Competency Name (Level X)",
  focus: "2-3 sentence description of what this lesson covers and why it matters for the APC.",
  image: "PICK THE MOST RELEVANT URL FROM THE LIST ABOVE",
  content: [
    {{ type: "paragraph", text: "Opening paragraph..." }},
    {{ type: "heading", text: "Section heading" }},
    {{ type: "paragraph", text: "..." }},
    {{ type: "key_term", term: "Term Name", text: "Definition and context." }},
    {{ type: "paragraph", text: "..." }},
    {{ type: "callout", label: "APC Tip", text: "Worked example or examiner tip..." }},
    {{ type: "heading", text: "Another section" }},
    {{ type: "paragraph", text: "..." }},
    {{ type: "paragraph", text: "..." }},
    {{ type: "paragraph", text: "..." }}
  ],
  summary: [
    "Key point 1 — specific and actionable",
    "Key point 2",
    "Key point 3",
    "Key point 4",
    "Key point 5"
  ],
  qa: [
    {{ q: "Specific APC-style question?", a: "Precise technical answer with any relevant figures or thresholds." }},
    {{ q: "...", a: "..." }},
    {{ q: "...", a: "..." }},
    {{ q: "...", a: "..." }},
    {{ q: "...", a: "..." }}
  ],
  news: [
    {{ tag: "Topic Tag", headline: "Relevant UK real estate/planning headline", body: "2-3 sentence body." }},
    {{ tag: "Topic Tag", headline: "...", body: "..." }},
    {{ tag: "Topic Tag", headline: "...", body: "..." }}
  ]
}};"""

    return extract_js(call_claude(prompt, timeout=420, max_tokens=8192))


def append_rics_log(rics_js):
    """Extract summary fields from today's RICS lesson and append to the rolling logbook."""
    try:
        def get_str(field):
            m = re.search(rf'{field}:\s*"([^"]*)"', rics_js)
            return m.group(1) if m else ''
        def get_int(field):
            m = re.search(rf'{field}:\s*(\d+)', rics_js)
            return int(m.group(1)) if m else 0

        summary_m = re.search(r'summary:\s*\[([\s\S]*?)\]', rics_js)
        summary = re.findall(r'"([^"]{10,})"', summary_m.group(1)) if summary_m else []

        qa_m = re.search(r'qa:\s*\[([\s\S]*?)\](?:\s*,\s*\n\s*news|\s*\n\s*\})', rics_js)
        qa = []
        if qa_m:
            qs = re.findall(r'q:\s*"([^"]+)"', qa_m.group(1))
            as_ = re.findall(r'a:\s*"([^"]+)"', qa_m.group(1))
            qa = [{'q': q, 'a': a} for q, a in zip(qs, as_)]

        entry = {
            'date': TODAY,
            'topic': get_str('topic'),
            'module': get_str('module'),
            'level': get_int('level'),
            'apc_competency': get_str('apc_competency'),
            'focus': get_str('focus'),
            'image': get_str('image'),
            'summary': summary,
            'qa': qa,
        }

        log_path = os.path.join(REPO_DIR, 'rics-log.js')
        entries = []
        if os.path.exists(log_path):
            raw = open(log_path).read()
            arr_m = re.search(r'var RICS_LOG\s*=\s*(\[[\s\S]*?\]);', raw)
            if arr_m:
                try:
                    entries = json.loads(arr_m.group(1))
                except Exception:
                    entries = []

        # Remove any existing entry for today before prepending fresh one
        entries = [e for e in entries if e.get('date') != TODAY]
        entries.insert(0, entry)
        entries = entries[:90]  # keep ~3 months

        with open(log_path, 'w', encoding='utf-8') as f:
            f.write('// rics-log.js\n// Auto-generated — do not edit manually\n\n')
            f.write('var RICS_LOG = ' + json.dumps(entries, indent=2) + ';\n')
        log('  ✓ RICS log updated')
        return log_path
    except Exception as e:
        log(f'  [warning] RICS log append failed: {e}')
        return None


def gen_curiosity():
    log("\n── Curiosity Corner")
    prompt = f"""Generate curiosity corner content for a personal learning website. Today is {TODAY}.
The user loves: ancient history, political history, exploration, remarkable lives, science, art, great events.
Write in the style of a high-quality long-read. The main article should have EXACTLY 8 content blocks — no more.
Each paragraph should be 3-4 sentences (not longer). Keep the bio to 3 paragraphs.
The onThisDay section must be a REAL event that actually happened on {datetime.date.today().strftime('%B %-d')} in history.
The personOfTheDay must be a real historical figure with an accurate biography.

Output ONLY this JavaScript. No explanation, no markdown. Start directly with "var CURIOSITY_DATA".

var CURIOSITY_DATA = {{
  date: "{TODAY}",
  mainArticle: {{
    title: "TITLE",
    subject: "The primary person, place, or object the article is about (e.g. 'Julius Caesar', 'Pompeii', 'The Silk Road'). Used for image search.",
    subtitle: "A compelling subtitle hook.",
    image: "__IMG_CURIOSITY_MAIN__",
    readTime: "X min",
    content: [
      {{ type: "paragraph", text: "..." }},
      {{ type: "heading", text: "..." }},
      {{ type: "paragraph", text: "..." }},
      {{ type: "heading", text: "..." }},
      {{ type: "paragraph", text: "..." }},
      {{ type: "heading", text: "..." }},
      {{ type: "paragraph", text: "..." }},
      {{ type: "heading", text: "..." }},
      {{ type: "paragraph", text: "..." }},
      {{ type: "paragraph", text: "..." }}
    ]
  }},
  personOfTheDay: {{
    name: "FULL NAME",
    lifespan: "YYYY\u2013YYYY",
    category: "e.g. Science & Engineering",
    image: null,
    bio: "4-5 paragraph biography as a single string. Use \\n\\n to separate paragraphs."
  }},
  onThisDay: {{
    headline: "SHORT PUNCHY HEADLINE",
    date: "{datetime.date.today().strftime('%B %-d, %Y — replace year with actual historical year')}",
    summary: "2-3 paragraph account of the event. Must be a real event.",
    image: "__IMG_CURIOSITY_OTD__"
  }}
}};"""
    js = extract_js(call_claude(prompt, timeout=420))
    if not js:
        return None
    # Inject images: Wikipedia first (person/place/event), then Pexels fallback
    try:
        main_title_m    = re.search(r'mainArticle\s*:\s*\{[^{]*?title\s*:\s*["\'](.+?)["\']', js, re.DOTALL)
        main_subject_m  = re.search(r'mainArticle\s*:\s*\{[^{]*?subject\s*:\s*["\'](.+?)["\']', js, re.DOTALL)
        otd_m           = re.search(r'onThisDay\s*:\s*\{[^{]*?headline\s*:\s*["\'](.+?)["\']', js, re.DOTALL)
        person_m        = re.search(r'personOfTheDay\s*:\s*\{[^{]*?name\s*:\s*["\'](.+?)["\']', js, re.DOTALL)
        if '__IMG_CURIOSITY_MAIN__' in js:
            title_str   = main_title_m.group(1)   if main_title_m   else ''
            subject_str = main_subject_m.group(1) if main_subject_m else ''
            search_term = subject_str or title_str or 'ancient history exploration'
            url = fetch_wikipedia_image(search_term)
            if url:
                log(f'  ✓ Curiosity main image (Wikipedia): {search_term[:40]}')
            else:
                url = fetch_pexels_image(search_term) or 'https://images.unsplash.com/photo-1461360370896-922624d12aa1?w=1200&auto=format&fit=crop'
                log(f'  ✓ Curiosity main image (Pexels): {search_term[:40]}')
            js = js.replace('"__IMG_CURIOSITY_MAIN__"', f'"{url}"', 1)
        if '__IMG_CURIOSITY_OTD__' in js:
            otd_query = otd_m.group(1) if otd_m else 'historical event'
            url = fetch_wikipedia_image(otd_query)
            if url:
                log(f'  ✓ On this day image (Wikipedia): {otd_query[:40]}')
            else:
                url = fetch_pexels_image(otd_query) or 'https://images.unsplash.com/photo-1489447068241-b3490214e879?w=800&auto=format&fit=crop'
                log(f'  ✓ On this day image (Pexels): {otd_query[:40]}')
            js = js.replace('"__IMG_CURIOSITY_OTD__"', f'"{url}"', 1)
        if 'image: null' in js and person_m:
            name = person_m.group(1)
            url = fetch_wikipedia_image(name)
            if url:
                log(f'  ✓ Person of day image (Wikipedia): {name[:40]}')
            else:
                url = fetch_pexels_image(name + ' portrait')
                if url:
                    log(f'  ✓ Person of day image (Pexels): {name[:40]}')
            if url:
                js = js.replace('image: null', f'image: "{url}"', 1)
    except Exception as e:
        log(f'  [warning] Curiosity image injection failed: {e}')
        js = js.replace('"__IMG_CURIOSITY_MAIN__"', '"https://images.unsplash.com/photo-1461360370896-922624d12aa1?w=1200&auto=format&fit=crop"', 1)
        js = js.replace('"__IMG_CURIOSITY_OTD__"', '"https://images.unsplash.com/photo-1489447068241-b3490214e879?w=800&auto=format&fit=crop"', 1)
    return js

def gen_reads():
    log("\n── Book of the Day")
    # Read yesterday's book so Claude avoids repeating it
    prev_book = ''
    try:
        reads_path = os.path.join(REPO_DIR, 'reads-data.js')
        if os.path.exists(reads_path):
            with open(reads_path) as _f:
                _content = _f.read()
            _m = re.search(r'title:\s*["\'](.+?)["\']', _content)
            if _m:
                prev_book = _m.group(1)
    except Exception:
        pass
    avoid = 'Sapiens, Wolf Hall, Rubicon'
    if prev_book:
        avoid += f', {prev_book} (yesterday\'s pick)'

    prompt = f"""Pick a single book to recommend today ({TODAY}) on a personal reading website.
The user reads very broadly across all subjects and genres — do not default to history or ancient history.
Range freely across: literary fiction, science, philosophy, politics, economics, nature, sport, art, music, travel, biography, memoir, true crime, psychology, sociology, technology, food, humour, and more.
Aim for roughly 60% non-fiction, 40% fiction overall, but today just pick whichever is the most compelling choice.
The book must be genuinely well-regarded (critically acclaimed or beloved by readers). Avoid obscure picks. Avoid: {avoid}.
Write a rich 4-5 sentence description and explain why it's worth reading now.

Output ONLY valid JavaScript. No explanation, no markdown. Start directly with "var READS_DATA".

var READS_DATA = {{
  date: "{TODAY}",
  book: {{
    title: "EXACT BOOK TITLE",
    author: "AUTHOR NAME",
    year: YYYY,
    genres: ["Genre 1", "Genre 2", "Genre 3"],
    desc: "4-5 sentence description that captures what makes this book special.",
    whyRead: "1-2 sentence hook — why read this one, right now.",
    rating: 4.3,
    ratingSource: "Goodreads",
    ratingCount: "X,000+",
    coverUrl: null,
    amazonUrl: null
  }}
}};"""
    js = extract_js(call_claude(prompt, timeout=60))
    if not js:
        return None
    # Inject cover URL and Amazon URL from Open Library
    try:
        title_m = re.search(r'title:\s*["\'](.+?)["\']', js)
        author_m = re.search(r'author:\s*["\'](.+?)["\']', js)
        if title_m and author_m:
            cover_url, amazon_url = fetch_book_cover(title_m.group(1), author_m.group(1))
            if cover_url:
                js = js.replace('coverUrl: null', f'coverUrl: "{cover_url}"', 1)
                log(f'  ✓ Cover: {cover_url}')
            else:
                log('  [warning] No cover found, using SVG fallback')
            if amazon_url:
                js = js.replace('amazonUrl: null', f'amazonUrl: "{amazon_url}"', 1)
                log(f'  ✓ Amazon: {amazon_url}')
            else:
                log('  [warning] No ISBN found, Amazon button will use search fallback')
    except Exception as e:
        log(f'  [warning] Cover/Amazon injection failed: {e}')
    return js


def gen_films():
    log("\n── Film of the Day")
    # Read yesterday's film so we can tell Claude to avoid it
    prev_film = ''
    try:
        films_path = os.path.join(REPO_DIR, 'films-data.js')
        if os.path.exists(films_path):
            with open(films_path) as _f:
                _content = _f.read()
            _m = re.search(r'title:\s*["\'](.+?)["\']', _content)
            if _m:
                prev_film = _m.group(1)
    except Exception:
        pass
    avoid = 'No Country for Old Men, Lawrence of Arabia, The Battle of Algiers'
    if prev_film:
        avoid += f', {prev_film} (yesterday\'s pick)'

    prompt = f"""Pick a single film to recommend today ({TODAY}) on a personal reading website.
The user watches very broadly — do not default to historical epics or political dramas.
Range freely across: comedy, romance, sci-fi, horror, animation, documentary, crime, fantasy, westerns, musicals, sports films, family films, world cinema, and more.
The film must be genuinely well-regarded — critically acclaimed or a widely loved classic. Avoid: {avoid}.
Write a rich 4-5 sentence description.

Output ONLY valid JavaScript. No explanation, no markdown. Start directly with "var FILMS_DATA".

var FILMS_DATA = {{
  date: "{TODAY}",
  film: {{
    title: "EXACT FILM TITLE",
    director: "DIRECTOR NAME",
    year: YYYY,
    genres: ["Genre 1", "Genre 2"],
    desc: "4-5 sentence description that captures what makes this film special.",
    cast: ["Actor 1", "Actor 2", "Actor 3"],
    rating: 95,
    ratingSource: "Rotten Tomatoes",
    ratingExtra: "X Academy Awards",
    posterUrl: null
  }}
}};"""
    js = extract_js(call_claude(prompt, timeout=60))
    if not js:
        return None
    # Replace null posterUrl with real URL fetched from Wikipedia
    try:
        title_m = re.search(r'title:\s*["\'](.+?)["\']', js)
        year_m  = re.search(r'year:\s*(\d{4})', js)
        if title_m and year_m:
            poster_url = fetch_film_poster(title_m.group(1), year_m.group(1))
            if poster_url:
                js = js.replace('posterUrl: null', f'posterUrl: "{poster_url}"', 1)
                log(f'  ✓ Poster: {poster_url}')
            else:
                log('  [warning] No poster found, using SVG fallback')
    except Exception as e:
        log(f'  [warning] Poster injection failed: {e}')
    return js


def gen_quote():
    log("\n── Quote of the Day")
    prompt = f"""Pick a single outstanding quote for today ({TODAY}) for a personal dashboard.
Choose something genuinely interesting — from philosophy, history, literature, politics, science, or sport.
Avoid clichés and overused motivational quotes. Prefer surprising, precise, or profound selections.

Output ONLY valid JavaScript. No explanation, no markdown. Start directly with "var QUOTE_DATA".

var QUOTE_DATA = {{
  date: "{TODAY}",
  text: "The exact quote text here.",
  author: "Full Name"
}};"""
    return extract_js(call_claude(prompt, timeout=30))


def gen_recipes():
    log("\n── Suggested Recipes")
    prompt = f"""Generate exactly 3 recipe suggestions for today ({TODAY}) for a personal recipe website.
The user loves: bold flavours, European and world cuisines, seasonal ingredients, and genuinely delicious food.
Pick a varied spread — e.g. one meat, one fish/seafood, one vegetarian or lighter dish.
The recipes should be high quality and taste impressive, but accessible to a confident home cook — avoid overly complex techniques, specialist equipment, or hard-to-find ingredients. Think the kind of recipe a good home cook would be proud to serve.

Output ONLY valid JavaScript. No explanation, no markdown. Start directly with "window.SUGGESTED_RECIPES".

window.SUGGESTED_RECIPES = [
  {{
    id: "sug1",
    title: "Recipe Title",
    category: "Dinner",
    time: "45 mins",
    serves: "4",
    desc: "2-3 sentence description of what makes this dish special.",
    emoji: "🍽️",
    image: "__IMG_R1__",
    ingredients: [
      {{ name: "Ingredient name", quantity: 200, unit: "g" }},
      {{ name: "Another ingredient", quantity: 2, unit: "tbsp" }}
    ],
    instructions: [
      "Step 1 — detailed, technique-forward instruction.",
      "Step 2 — continue with the same quality."
    ]
  }},
  {{
    id: "sug2",
    title: "Second Recipe Title",
    category: "Lunch",
    time: "30 mins",
    serves: "2",
    desc: "2-3 sentence description.",
    emoji: "🐟",
    image: "__IMG_R2__",
    ingredients: [
      {{ name: "Ingredient", quantity: 1, unit: "" }}
    ],
    instructions: [
      "Step 1."
    ]
  }},
  {{
    id: "sug3",
    title: "Third Recipe Title",
    category: "Dinner",
    time: "1 hr",
    serves: "4",
    desc: "2-3 sentence description.",
    emoji: "🥗",
    image: "__IMG_R3__",
    ingredients: [
      {{ name: "Ingredient", quantity: 100, unit: "g" }}
    ],
    instructions: [
      "Step 1."
    ]
  }}
];"""
    js = extract_js(call_claude(prompt, timeout=240, max_tokens=8192))
    if not js:
        return None
    # Inject Pexels images using each recipe title
    try:
        titles = re.findall(r'title:\s*["\'](.+?)["\']', js)
        sentinels = ['__IMG_R1__', '__IMG_R2__', '__IMG_R3__']
        for i, sentinel in enumerate(sentinels):
            if f'"{sentinel}"' in js or f"'{sentinel}'" in js:
                query = (titles[i] + ' food dish') if i < len(titles) else 'gourmet food'
                url = fetch_pexels_image(query, orientation='landscape')
                if url:
                    js = js.replace(f'"{sentinel}"', f'"{url}"', 1)
                    js = js.replace(f"'{sentinel}'", f"'{url}'", 1)
                    log(f'  ✓ Recipe image [{i+1}]: {query[:40]}')
                else:
                    js = js.replace(f'"{sentinel}"', 'null', 1)
                    js = js.replace(f"'{sentinel}'", 'null', 1)
    except Exception as e:
        log(f'  [warning] Recipe image injection failed: {e}')
        for s in ['__IMG_R1__', '__IMG_R2__', '__IMG_R3__']:
            js = js.replace(f'"{s}"', 'null', 1)
    return js


# ── Cache buster ──────────────────────────────────────────────────────────────

def bump_cache_busters():
    """Update ?v= query strings in HTML files so GitHub Pages CDN serves fresh data."""
    # Map each data file to the HTML page(s) that load it
    mappings = [
        # Each dedicated page
        ('philosophy-data.js',       'philosophy.html'),
        ('curiosity-data.js',        'curiosity.html'),
        ('rics-data.js',             'rics-study.html'),
        ('world-news-data.js',       'world-news.html'),
        ('uk-politics-news-data.js', 'uk-politics-news.html'),
        ('us-politics-news-data.js', 'us-politics-news.html'),
        ('financial-news-data.js',   'financial-news.html'),
        ('tech-news-data.js',        'tech-news.html'),
        ('reads-data.js',            'reads.html'),
        ('films-data.js',            'films.html'),
        ('suggested-recipes-data.js','recipes.html'),
        # Everything that feeds personal_hub.html previews
        ('quote-data.js',                'personal_hub.html'),
        ('quiz-data.js',                 'personal_hub.html'),
        ('rics-data.js',                 'personal_hub.html'),
        ('world-news-data.js',           'personal_hub.html'),
        ('curiosity-data.js',            'personal_hub.html'),
        ('philosophy-data.js',           'personal_hub.html'),
        ('suggested-recipes-data.js',    'personal_hub.html'),
        ('uk-politics-news-data.js',     'personal_hub.html'),
        ('us-politics-news-data.js',     'personal_hub.html'),
        ('financial-news-data.js',       'personal_hub.html'),
        ('tech-news-data.js',            'personal_hub.html'),
        ('reads-data.js',                'personal_hub.html'),
        ('films-data.js',                'personal_hub.html'),
        ('rics-log.js',                  'rics-log.html'),
    ]
    version = datetime.datetime.now().strftime('%Y%m%d%H%M')
    changed = []
    seen = set()
    for data_file, html_file in mappings:
        html_path = os.path.join(REPO_DIR, html_file)
        if not os.path.exists(html_path):
            continue
        with open(html_path, 'r', encoding='utf-8') as f:
            content = f.read()
        # Replace both versioned and unversioned script src references
        new_content = re.sub(
            r'(<script src="' + re.escape(data_file) + r')(\?v=[^"]*)?(")',
            lambda m: m.group(1) + f'?v={version}' + m.group(3),
            content
        )
        if new_content != content:
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            if html_file not in seen:
                log(f"  ✓ Cache-busted: {html_file}")
                changed.append(html_file)
                seen.add(html_file)
    return changed

# ── Git ────────────────────────────────────────────────────────────────────────

def git_push(files):
    log("\n── Git commit & push")
    git_env = os.environ.copy()
    git_env['GIT_TERMINAL_PROMPT'] = '0'

    in_actions = os.environ.get('GITHUB_ACTIONS') == 'true'
    if in_actions:
        # Configure git identity for the Actions bot
        subprocess.run(['git', '-C', REPO_DIR, 'config', 'user.email', 'actions@github.com'], env=git_env)
        subprocess.run(['git', '-C', REPO_DIR, 'config', 'user.name', 'github-actions'], env=git_env)
    else:
        # launchd: HOME not set in minimal environment
        git_env['HOME'] = os.path.expanduser('~')

    cmds = [
        (['git', '-C', REPO_DIR, 'add'] + files, 'add'),
        (['git', '-C', REPO_DIR, 'commit', '-m', f'Daily content update {TODAY}'], 'commit'),
        (['git', '-C', REPO_DIR, 'push'], 'push'),
    ]
    for cmd, label in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True, env=git_env)
        if r.returncode != 0 and label != 'commit':
            log(f"  [warning] git {label}: {r.stderr.strip()[:200]}")
        else:
            log(f"  ✓ git {label}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    with open(LOG_FILE, 'a') as f:
        f.write(f"\n{'='*60}\n")
    log(f"Daily Hub Update — {TODAY}")

    updated = []
    PAUSE = 5  # seconds between Claude calls to avoid rate limits

    # ── News files (RSS + AI summarise) ──────────────────────────────────────
    # Fetch all feeds once — every category gets the full pool so cross-category
    # stories (e.g. a Sky News tech story in the UK Politics feed) are considered.
    log("\n── Fetching all RSS feeds")
    all_feed_urls = list(dict.fromkeys(url for feeds in RSS.values() for url in feeds))
    all_articles = fetch_rss(*all_feed_urls)
    log(f"  ✓ {len(all_articles)} articles fetched from {len(all_feed_urls)} feeds")

    news_tasks = [
        ('world',      'WORLD_NEWS',       'world',     ['s1','s2','s3'],   'world-news-data.js',     ''),
        ('uk_politics','UK_POLITICS_NEWS', 'uk',        ['uk1','uk2','uk3'],'uk-politics-news-data.js',''),
        ('us_politics','US_POLITICS_NEWS', 'us',        ['us1','us2','us3'],'us-politics-news-data.js',''),
        ('financial',  'FINANCIAL_NEWS',   'financial', ['fn1','fn2','fn3'],'financial-news-data.js',  'Prioritise stories about businesses, mergers and acquisitions, stocks and shares, and corporate earnings over general economic policy.'),
        ('tech',       'TECH_NEWS',        'tech',      ['tc1','tc2','tc3'],'tech-news-data.js',       ''),
    ]

    for category, var_name, img_key, ids, filename, focus_hint in news_tasks:
        js = gen_news(category, var_name, img_key, ids, focus_hint, all_articles=all_articles)
        if js:
            header = f"// {filename}\n// Auto-updated {TODAY} — do not edit manually\n\n"
            updated.append(write_file(filename, header + js + '\n'))
        time.sleep(PAUSE)

    # ── Pure AI-generated content ─────────────────────────────────────────────
    ai_tasks = [
        (gen_quote,       'quote-data.js'),
        (gen_quiz,        'quiz-data.js'),
        (gen_philosophy,  'philosophy-data.js'),
        (gen_curiosity,   'curiosity-data.js'),
        (gen_rics,        'rics-data.js'),
        (gen_reads,       'reads-data.js'),
        (gen_films,       'films-data.js'),
        (gen_recipes,     'suggested-recipes-data.js'),
    ]

    for generator, filename in ai_tasks:
        js = generator()
        if js is None and filename == 'curiosity-data.js':
            log("  Retrying curiosity corner with extended timeout...")
            js = gen_curiosity()
        if js is None and filename == 'rics-data.js':
            log("  Retrying RICS study with extended timeout...")
            js = gen_rics()
        if js is None and filename == 'suggested-recipes-data.js':
            log("  Retrying recipes with extended timeout...")
            js = gen_recipes()
        if js:
            header = f"// {filename}\n// Auto-updated {TODAY} — do not edit manually\n\n"
            updated.append(write_file(filename, header + js + '\n'))
            # Append RICS lesson to rolling logbook
            if filename == 'rics-data.js':
                log_file = append_rics_log(js)
                if log_file:
                    updated.append('rics-log.js')
        time.sleep(PAUSE)

    # ── Bump cache busters in HTML pages ─────────────────────────────────────
    log("\n── Bumping cache busters")
    html_changed = bump_cache_busters()

    # ── Commit & push ─────────────────────────────────────────────────────────
    if updated or html_changed:
        git_push(updated + html_changed)
        log(f"\nDone — {len(updated)} data files + {len(html_changed)} HTML files updated and pushed.")
    else:
        log("\nNo files were updated (all generators failed or returned nothing).")

if __name__ == '__main__':
    main()
