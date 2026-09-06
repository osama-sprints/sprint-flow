/**
 * Applies the per-block direction decision to rendered message bodies.
 *
 * Scope and restraint are the whole design here. The controller only ever sets
 * `dir` and a marker attribute on block elements that Mattermost has already
 * rendered inside a post body; it never rewrites text, never reorders nodes,
 * never inserts direction-control characters, and never touches the post
 * header, the avatar, the timestamp or any app chrome.
 *
 * It exists because Mattermost offers no formatting hook that can set an
 * attribute on a rendered block: `registerMessageWillFormatHook` operates on
 * markdown TEXT before rendering, and `registerPostTypeComponent` only applies
 * to custom post types — neither can reach an ordinary post's <p> or <li>.
 */

import {resolveDirection} from './direction';
import {proseText} from './text';

/** Blocks whose direction is decided independently. */
const BLOCK_SELECTOR = 'p, li, ul, ol, h1, h2, h3, h4, h5, h6, blockquote, td, th, dd, dt';

/** Post bodies, and our own artifact prose. */
const ROOT_SELECTOR = '.post-message__text, .sf-artifact__prose';

/** Never decided by content: code keeps the direction the stylesheet gives it. */
const SKIP_SELECTOR = 'pre, code, .post-code, .hljs, .sf-artifact__source';

/**
 * Decide and stamp one block.
 *
 * @param block The rendered block element.
 */
const applyToBlock = (block: Element): void => {
    if (block.closest(SKIP_SELECTOR)) {
        return;
    }

    const direction = resolveDirection(proseText(block));
    if (!direction) {
        return;
    }

    // Idempotent: re-running over an unchanged block writes nothing, so the
    // observer cannot feed itself.
    if (block.getAttribute('data-sf-dir') === direction) {
        return;
    }

    block.setAttribute('dir', direction);
    block.setAttribute('data-sf-dir', direction);
};

/**
 * Decide every block inside one post body.
 *
 * @param root A post body or artifact prose container.
 */
export const applyToRoot = (root: Element): void => {
    root.querySelectorAll(BLOCK_SELECTOR).forEach(applyToBlock);

    // A single-paragraph post can render its text straight into the container.
    if (!root.querySelector(BLOCK_SELECTOR)) {
        applyToBlock(root);
    }
};

/**
 * Start watching the post list for rendered message bodies.
 *
 * @returns A function that stops the observer and leaves the DOM as it is.
 */
export const startBidiController = (): (() => void) => {
    const sweep = (node: ParentNode): void => {
        node.querySelectorAll?.(ROOT_SELECTOR).forEach(applyToRoot);
    };

    sweep(document);

    const observer = new MutationObserver((records) => {
        // Blocks whose text changed without an element being added; resolved
        // to their post body once, after the batch, so a post that mutates
        // several nodes is swept a single time.
        const touched = new Set<Element | null>();

        for (const record of records) {
            for (const added of Array.from(record.addedNodes)) {
                if (added instanceof Element) {
                    if (added.matches(ROOT_SELECTOR)) {
                        applyToRoot(added);
                    }
                    sweep(added);
                    continue;
                }

                // A replaced text node has no element to match on: React swaps
                // the child of an existing <p> when a post is edited or when a
                // pending artifact's text changes, and the block's decision
                // must be recomputed from the new text.
                touched.add(added.parentElement);
            }

            for (const removed of Array.from(record.removedNodes)) {
                if (!(removed instanceof Element)) {
                    touched.add(record.target instanceof Element ? record.target : null);
                }
            }

            // An edited post mutates text inside a body that already exists.
            if (record.type === 'characterData') {
                touched.add(record.target.parentElement);
            }
        }
        for (const element of touched) {
            const root = element?.closest(ROOT_SELECTOR);
            if (root) {
                applyToRoot(root);
            }
        }
    });

    observer.observe(document.body, {childList: true, subtree: true, characterData: true});

    return () => observer.disconnect();
};
