/**
 * Text extraction for direction decisions.
 *
 * `textContent` is the wrong input: it includes the text of rendered code
 * elements. In markdown source a path is fenced in backticks and
 * `direction.ts` strips it, but by the time Mattermost has rendered the post
 * the backticks are gone and `app/services/mattermost.py` is just Latin text
 * voting to make an Arabic sentence LTR. This walks the tree instead and skips
 * those subtrees entirely.
 */

/** Rendered elements whose text says nothing about the prose's language. */
const EXCLUDED = 'code, pre, .post-code, .hljs, .codespan__pre-wrap, .sf-artifact__source';

/**
 * Collect an element's text, ignoring rendered code descendants.
 *
 * @param element The block to read.
 * @returns The prose text of the block.
 */
export const proseText = (element: Element): string => {
    let text = '';
    const walk = (node: Node): void => {
        if (node.nodeType === Node.TEXT_NODE) {
            text += node.nodeValue || '';
            return;
        }
        if (node.nodeType !== Node.ELEMENT_NODE) {
            return;
        }
        if ((node as Element).matches(EXCLUDED)) {
            // Contributes a neutral placeholder so surrounding words still
            // separate, but no strong characters.
            text += ' ';
            return;
        }
        node.childNodes.forEach(walk);
    };
    walk(element);
    return text;
};
