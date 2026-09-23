# Theme integration

Three edits put the pipeline into a client's site. Two rules make all of them fail silently
if you get them wrong.

---

## The principle behind both rules

**Markup inside a cached page must be static and empty-tolerant. Anything that varies per
job arrives at runtime from the feed.**

Client pages are edge-cached for up to 30 days. A server-rendered list of recent projects
would go stale until somebody ran a purge. So the hub and the town blocks ship as empty
containers plus a script that fetches JSON in the browser — an already-cached page still
shows today's jobs, and a town with no jobs yet renders nothing rather than an empty heading.

That single rule explains the hub design, the `hidden` attribute on the town block, why new
job URLs appear instantly, and why a CSS upload still needs a purge.

---

## Rule 1 — no inline JavaScript in post content

WordPress runs `wpautop` over post content and inserts `<br>` and `<p>` tags at line breaks.
It does this **inside `<script>` blocks**, turning valid JavaScript into a syntax error. The
page still renders, the container is still there, nothing appears, and it reads like a
styling problem.

So page content holds **markup only**, and behaviour is served from the receiver:

```html
<!-- wp:html -->
<div class="jp-hub">
  <div id="jp-map" aria-label="Map of recent project locations"></div>
  <ul class="jp-grid" id="jp-grid"></ul>
  <div class="jp-empty" id="jp-empty" hidden>Recent projects will appear here.</div>
</div>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="<receiver>/hub/<client-id>/hub.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="<receiver>/hub/<client-id>/hub.js"></script>
<!-- /wp:html -->
```

Generate it rather than typing it:

```bash
cd "$JOB_PAGES" && python3 hub_page.py --base <receiver-url> --client <client-id>
```

The `<!-- wp:html -->` wrapper makes Gutenberg treat it as a Custom HTML block, so opening
the page in the editor will not mangle it either.

Serving the JS from the receiver has a second benefit: fixing `hub.js` updates every client
at once, with no page editing.

---

## Rule 2 — a theme CSS upload always needs a purge

WordPress versions stylesheets by file timestamp (`main.css?ver=1790001279`), so the asset
URL is always fresh. But the **page HTML that references it is edge-cached**, and the cached
copy keeps pointing at the previous version. Real visitors get the old stylesheet while the
new one sits there unused.

**Verify with a plain URL.** Any cache-buster (`?v=`, `?cb=`) bypasses the edge and returns
fresh HTML with the current asset version — a test using one cannot reproduce what a visitor
sees. This produced three "fixes" that were confirmed working while the live site still
served pre-fix CSS. The tell was *"wrong on the homepage, right on other pages"*: a CSS bug
cannot be page-specific, but a per-page cache entry can.

```bash
# what a real visitor gets
curl -s https://<site>/ | grep -oE 'main\.css\?ver=[0-9]+'
# what a fresh render gets
curl -s "https://<site>/?fresh=$(date +%s)" | grep -oE 'main\.css\?ver=[0-9]+'
# a mismatch means the edge is serving stale HTML — purge
```

Purge path varies by host. On a GHL-hosted WordPress site, visit
`/wp-admin/index.php?cdn-action=purge` while logged in.

**What does and does not need a purge:**

| Change | Purge? |
|---|---|
| A new job page (new URL) | No — new URLs are not in the edge, and 404s are BYPASS |
| Recent-projects block filling in | No — it renders in the browser from the feed |
| Theme CSS or template edit | **Yes** |
| Anything on an existing page (footer, nav, hub) | **Yes** |

---

## The three edits

### 1. The `/projects/` hub page

Paste the generated markup into the hub page as a Custom HTML block. The parent page is
created automatically on first publish if it does not exist.

The map plots **town centroids, never addresses**. Publishing a map of customers' houses
would be a real privacy problem, which is also why GPS is stripped from published photos.
Jobs in the same town collapse to one pin.

### 2. The reverse-link block on town pages

This is where the SEO compounds — job pages link to the town page, and the town page links
back. One template edit covers every town.

The contract is a host element carrying `data-jp-town`:

```html
<section class="section jp-town-projects" data-jp-town="<town name>" hidden>
  <h2>Recent projects in <town name></h2>
  <ul class="jp-grid"></ul>
  <p class="jp-town-all"><a href="<site>/projects/">See all recent projects</a></p>
</section>
<link rel="stylesheet" href="<receiver>/hub/<client-id>/hub.css">
<script src="<receiver>/hub/<client-id>/town.js" defer></script>
```

`town.js` matches on the `data-jp-town` value against the feed's `town` field and reveals the
section only if there are matches. Towns with no jobs render nothing.

If the theme renders town pages from a PHP data array rather than page content, add this in
the **template** after the render call — not in `functions.php`. Smaller file, far less risk.

### 2b. The page template must render a job page

Two theme assumptions break job pages, and both are invisible until the first one
publishes. Check them before the first live text, not after.

**The title must be printed as the `<h1>`.** `publish.py` puts the generated headline in
the WordPress post title and deliberately keeps it out of the body. A theme whose page
template is `the_content()` alone — common in hand-built themes where every page carries
its own hero markup — ships job pages with **no `<h1>` at all**, on pages whose entire
purpose is local SEO.

**Container-less content needs wrapping.** Hand-built pages usually supply their own
`<section class="wrap">`. A job page is plain block content, so the same template renders
it edge-to-edge with no max-width, padding or vertical rhythm.

Both are one conditional:

```php
if ( stripos( get_the_content(), '<section' ) !== false ) {
    the_content();                       // page brings its own layout
} else {
    echo '<section class="jp-page"><div class="wrap"><div class="prose">';
    the_title( '<h1>', '</h1>' );        // publish.py put it in the title
    the_content();
    echo '</div></div></section>';
}
```

Check the existing pages actually contain `<section>` before relying on that test, or they
will be double-wrapped.

**Style the image blocks.** `publish.py` emits Gutenberg markup — `<figure
class="wp-block-image">` with `<figcaption>`. A custom theme often has no rules for these
at all, so photos render unstyled. Scope the additions to the wrapper class so they cannot
reach the hand-built pages:

```css
.jp-page figure, .jp-page .wp-block-image{margin:2rem 0}
.jp-page figure img{width:100%;border-radius:14px}
.jp-page figcaption{margin-top:.6rem;font-size:.92rem;opacity:.8}
.jp-page h1{font-size:clamp(1.9rem,2.6vw + 1rem,2.9rem);text-transform:none}
```

That last rule matters more than it looks: a hero-scale uppercase `h1` turns a headline
like "Mitsubishi 18,000 BTU Mini Split" into shouting, and product names read badly in caps.

---

### 3. The footer link

A link to `/projects/` in the footer gives every page a path to the hub, including ones whose
templates ignore post content.

---

## Working on a theme over FTPS

Where a host blocks zip installs and `functions.php` writes, FTPS is the way in.

1. Download the file, edit locally
2. **Lint PHP before uploading.** A syntax error in a shared template takes down every page
   using it. With no local PHP, use WP Playground:
   ```bash
   npx @wp-playground/cli@latest php --php=8.2 --wordpress-install-mode=do-not-attempt-installing \
     --mount=./lint/src:/lintsrc --mount=./lint/phplint.php:/phplint.php -- /phplint.php
   ```
3. Upload with a `.bak-<timestamp>` copy kept first, and **read the file back and compare
   SHA-256** before trusting the write
4. Purge, then verify on a plain URL

---

## Verifying layout

`resize_window` reports success but does not change the viewport on this machine
(`outerWidth` returns 0). Use a **same-origin iframe** — media queries evaluate against the
iframe width and the document is scriptable:

```js
const f = document.createElement('iframe');
f.style.cssText = 'position:fixed;top:0;left:0;width:390px;height:780px;z-index:999999';
f.src = 'https://<site>/?t=' + Date.now();
document.body.appendChild(f);
await new Promise(r => f.onload = r);
f.contentWindow.innerWidth;              // a real mobile viewport
f.contentDocument.querySelector('...');  // measurable and clickable
```

Note the cache-buster in that `src` is exactly what hides stale-HTML problems. Check a plain
URL separately.

**Scope header and layout fixes to the right breakpoint.** A desktop fix applied at every
width will overflow a phone header and push the menu off-screen. Find the theme's own
breakpoint and match it rather than inventing one.
