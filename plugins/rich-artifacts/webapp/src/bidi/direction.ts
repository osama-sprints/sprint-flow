/**
 * Per-block direction resolution.
 *
 * `dir="auto"` — which is what Mattermost puts on the post body — resolves
 * direction from the FIRST strong character of the whole post. That is wrong
 * twice over: it decides once for a post that may mix languages per paragraph,
 * and an Arabic paragraph opening with a Latin product name ("Gemini هو…")
 * resolves to LTR. This module decides per block by weighing the strong
 * characters actually present, so a Latin lead-in cannot flip Arabic prose.
 */

/*
 * Script membership comes from the Unicode property escapes the platform
 * already implements, rather than hand-copied code-point ranges: the ranges
 * were both longer and less accurate (they missed Arabic Extended-B and
 * mis-included punctuation blocks), and they need maintenance as Unicode
 * grows. Requires the `u` flag, which every browser Mattermost 11 supports has.
 */
const RTL_SCRIPTS = /[\p{Script=Arabic}\p{Script=Hebrew}\p{Script=Syriac}\p{Script=Thaana}\p{Script=Nko}\p{Script=Adlam}]/u;

const LTR_SCRIPTS = /[\p{Script=Latin}\p{Script=Greek}\p{Script=Cyrillic}\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\p{Script=Devanagari}]/u;

/**
 * Text that must not vote on direction: code spans, URLs, @mentions, ~channels
 * and :emoji:. A path like `app/services/mattermost.py` is Latin, but it says
 * nothing about the language of the sentence carrying it.
 */
const NEUTRALISE = [
    /`[^`]*`/g,
    /https?:\/\/\S+/gi,
    /\b[\w.-]+\/[\w./-]+/g,
    /[@~][\w.-]+/g,
    /:[a-z0-9_+-]+:/gi,
];

export type Direction = 'rtl' | 'ltr';

/**
 * Strip the runs that should not influence the decision.
 *
 * @param text Raw block text.
 * @returns The text with code, links, mentions and emoji removed.
 */
const meaningful = (text: string): string => NEUTRALISE.reduce((acc, re) => acc.replace(re, ' '), text);

/**
 * Count strong characters of each direction.
 *
 * @param text Text to weigh.
 * @returns Tuple of [rtl, ltr] counts.
 */
const weigh = (text: string): [number, number] => {
    let rtl = 0;
    let ltr = 0;
    for (const ch of text) {
        if (RTL_SCRIPTS.test(ch)) {
            rtl += 1;
        } else if (LTR_SCRIPTS.test(ch)) {
            ltr += 1;
        }
    }
    return [rtl, ltr];
};

/**
 * Resolve the direction of one semantic block.
 *
 * A block is RTL when RTL letters carry at least `threshold` of the strong
 * characters that matter. The default of 0.3 is deliberately generous: Arabic
 * technical prose is full of Latin terms ("قمنا بتحديث الـ pipeline"), and such
 * a sentence is still an Arabic sentence.
 *
 * @param text The block's text content.
 * @param threshold RTL share at which the block counts as RTL.
 * @returns The resolved direction, or null when the block has no strong
 *     characters at all (pure punctuation or digits — leave it inheriting).
 */
export const resolveDirection = (text: string, threshold = 0.3): Direction | null => {
    const [rtl, ltr] = weigh(meaningful(text));
    if (rtl + ltr === 0) {
        return null;
    }
    return rtl / (rtl + ltr) >= threshold ? 'rtl' : 'ltr';
};

/**
 * Whether the browser's own first-strong heuristic would get this block wrong.
 *
 * The stylesheet applies `unicode-bidi: plaintext`, which already resolves each
 * block independently by first-strong character. An explicit `dir` attribute is
 * only worth writing to the DOM where that disagrees with the weighed answer —
 * which keeps the observer's footprint to the handful of blocks that need it.
 *
 * @param text The block's text content.
 * @returns The direction to force, or null when first-strong already agrees.
 */
export const overrideFor = (text: string): Direction | null => {
    const resolved = resolveDirection(text);
    if (!resolved) {
        return null;
    }

    const stripped = meaningful(text).trim();
    let firstStrong: Direction | null = null;
    for (const ch of stripped) {
        if (RTL_SCRIPTS.test(ch)) {
            firstStrong = 'rtl';
            break;
        }
        if (LTR_SCRIPTS.test(ch)) {
            firstStrong = 'ltr';
            break;
        }
    }

    return firstStrong && firstStrong !== resolved ? resolved : null;
};
