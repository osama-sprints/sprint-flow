/**
 * Headless verification harness for Mattermost rendering.
 *
 * Logs in with the admin account from .env, opens a channel (optionally a
 * thread in the right-hand sidebar), screenshots it, and dumps per-block
 * bidi diagnostics: the tag, the dir attribute, and the COMPUTED direction /
 * text-align / unicode-bidi that the browser actually applied.
 *
 *   npm i puppeteer-core            # once, anywhere on NODE_PATH
 *   node browser.js --out <png> [--diag <json>] [--channel rtl-lab]
 *                   [--thread <post_id>] [--focus <post_id>] [--wait <ms>]
 *
 * Chrome is driven through puppeteer-core against the system google-chrome, so
 * nothing is downloaded at install time.
 */
const fs = require('fs');
const path = require('path');
const puppeteer = require('puppeteer-core');

const REPO = '/home/o/poc/skill-sync';
const BASE = 'http://localhost:8065';

function arg(name, fallback) {
    const i = process.argv.indexOf(`--${name}`);
    return i === -1 ? fallback : process.argv[i + 1];
}

function env() {
    const out = {};
    for (const line of fs.readFileSync(path.join(REPO, '.env'), 'utf8').split('\n')) {
        const trimmed = line.trim();
        if (!trimmed || trimmed.startsWith('#') || !trimmed.includes('=')) continue;
        const [k, ...rest] = trimmed.split('=');
        out[k] = rest.join('=').trim().replace(/^"|"$/g, '');
    }
    return out;
}

// Runs in the page: report what the browser actually computed for every
// rendered block inside every post body.
function collectDiagnostics() {
    const blocks = [];
    document.querySelectorAll('.post-message__text, .post-message__text-container').forEach((body) => {
        const postId = body.closest('[id^="post_"], .post')?.id || '';
        body.querySelectorAll('p, li, ol, ul, h1, h2, h3, blockquote, pre, code').forEach((el) => {
            const cs = getComputedStyle(el);
            blocks.push({
                post: postId,
                tag: el.tagName.toLowerCase(),
                dirAttr: el.getAttribute('dir'),
                direction: cs.direction,
                textAlign: cs.textAlign,
                unicodeBidi: cs.unicodeBidi,
                marginInlineStart: cs.marginInlineStart,
                paddingInlineStart: cs.paddingInlineStart,
                text: (el.textContent || '').trim().slice(0, 48),
            });
        });
    });
    return {
        html: document.documentElement.getAttribute('dir'),
        body: getComputedStyle(document.body).direction,
        blocks,
    };
}

(async () => {
    const e = env();
    const browser = await puppeteer.launch({
        executablePath: '/usr/bin/google-chrome',
        headless: 'new',
        args: ['--no-sandbox', '--disable-dev-shm-usage', '--window-size=1400,1800'],
        defaultViewport: {width: 1400, height: 1800},
    });
    const page = await browser.newPage();

    // Authenticate through the API and inject the session cookies rather than
    // driving the login form: the form is a controlled React component whose
    // state does not settle reliably under automation, and the cookie set is
    // exactly what a real browser session holds.
    const login = await fetch(`${BASE}/api/v4/users/login`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({login_id: e.MM_ADMIN_USERNAME, password: e.MM_ADMIN_PASSWORD}),
    });
    if (!login.ok) throw new Error(`login failed: ${login.status}`);
    const token = login.headers.get('token');
    const user = await login.json();

    await page.setCookie(
        {name: 'MMAUTHTOKEN', value: token, domain: 'localhost', path: '/', httpOnly: true},
        {name: 'MMUSERID', value: user.id, domain: 'localhost', path: '/'},
        {name: 'MMCSRF', value: (login.headers.getSetCookie() || [])
            .find((c) => c.startsWith('MMCSRF='))?.split(';')[0].split('=')[1] || '', domain: 'localhost', path: '/'},
    );

    // Mattermost interposes a "View in App" landing page on first visit; the
    // webapp skips it once this key is set, so the harness never sees it.
    await page.evaluateOnNewDocument(() => {
        try {
            localStorage.setItem('__landingPageSeen__', 'true');
        } catch (err) { /* private mode */ }
    });

    const channel = arg('channel', 'rtl-lab');
    await page.goto(`${BASE}/sprints-community/channels/${channel}`, {waitUntil: 'networkidle2'});
    await page.waitForSelector('.post-message__text', {timeout: 30000});

    // Open the right-hand thread panel. Mattermost has no URL that opens the
    // RHS, so the harness clicks the post's reply affordance; the selector has
    // moved between releases, hence the ordered list of candidates.
    const thread = arg('thread', '');
    if (thread) {
        const opened = await page.evaluate((id) => {
            const post = document.getElementById(`post_${id}`) || document.getElementById(id);
            if (!post) return 'post-not-found';
            const selectors = [
                '.ReplyButton',
                '.post-menu__comment',
                'button[aria-label*="repl" i]',
                '.comment-icon__container',
                '.post__body',
            ];
            for (const sel of selectors) {
                const el = post.querySelector(sel);
                if (el) {
                    el.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                    return sel;
                }
            }
            return 'none';
        }, thread);
        console.error('rhs-open-selector:', opened);
        await new Promise((r) => setTimeout(r, 2500));
    }

    // Bring a specific post into view so a screenshot frames the cases under
    // test rather than whatever happens to be at the bottom of the channel.
    const focus = arg('focus', '');
    if (focus) {
        await page.evaluate((id) => {
            document.getElementById(`post_${id}`)?.scrollIntoView({block: 'center'});
        }, focus);
    }

    await new Promise((r) => setTimeout(r, Number(arg('wait', '2500'))));

    const diag = await page.evaluate(collectDiagnostics);
    const diagPath = arg('diag', '');
    if (diagPath) fs.writeFileSync(diagPath, JSON.stringify(diag, null, 1));

    await page.screenshot({path: arg('out', '/tmp/mm.png'), fullPage: false});
    console.log(JSON.stringify({blocks: diag.blocks.length, html_dir: diag.html, body_dir: diag.body}));
    await browser.close();
})().catch((err) => {
    console.error('harness_failed:', err.message);
    process.exit(1);
});
