import React from 'react';
import ReactDOM from 'react-dom';

import VideoEmbed, {isVideoUrl} from './video_embed';

/**
 * Inline players for video links that Mattermost does not embed.
 *
 * The server derives `metadata.embeds` only from BARE URLs. A Markdown link
 * such as `[architecture video](https://…/final.mp4)` produces no embed at all
 * — reproduced through the composer and the API alike — so the supported
 * `registerPostWillRenderEmbedComponent` hook is never consulted for it and
 * the reader sees only a link. There is no extension point for that case, so
 * this narrowly scoped enhancement watches rendered message bodies, and for a
 * post whose text links to a video file but shows no player, mounts the SAME
 * VideoEmbed component in a container appended after the message text.
 *
 * Restraint: it never edits the message, never touches a post that already
 * has an embed player, mounts at most one player per post (the first video
 * link), and unmounts cleanly when the post leaves the DOM.
 */

const ROOT_SELECTOR = '.post-message__text';
const MARK = 'data-sf-video-fallback';

const mounted = new WeakMap<Element, HTMLElement>();

const firstVideoLink = (body: Element): string | null => {
    for (const anchor of Array.from(body.querySelectorAll('a[href]'))) {
        const href = anchor.getAttribute('href') || '';
        if (isVideoUrl(href)) {
            return href;
        }
    }
    return null;
};

const enhance = (body: Element): void => {
    if (body.hasAttribute(MARK)) {
        return;
    }
    const post = body.closest('.post, [id^="post_"]');
    if (post && post.querySelector('video.sf-video__player')) {
        // Mattermost embedded it and the hook rendered the player already.
        return;
    }
    const url = firstVideoLink(body);
    if (!url) {
        return;
    }

    const container = document.createElement('div');
    container.className = 'sf-video-fallback';
    body.setAttribute(MARK, '1');
    body.insertAdjacentElement('afterend', container);
    ReactDOM.render(<VideoEmbed embed={{type: 'link', url}}/>, container);
    mounted.set(body, container);
};

const cleanup = (body: Element): void => {
    const container = mounted.get(body);
    if (container) {
        ReactDOM.unmountComponentAtNode(container);
        container.remove();
        mounted.delete(body);
    }
};

/**
 * Start watching for message bodies that link to a video without an embed.
 *
 * @returns A function that stops the observer and unmounts every player.
 */
export const startVideoLinkFallback = (): (() => void) => {
    const sweep = (node: ParentNode): void => {
        node.querySelectorAll?.(ROOT_SELECTOR).forEach(enhance);
    };

    // Embeds render a moment after the message; give the hook first refusal.
    const scheduled = new Set<Element>();
    const later = (node: ParentNode): void => {
        if (node instanceof Element && scheduled.has(node)) {
            return;
        }
        if (node instanceof Element) {
            scheduled.add(node);
        }
        window.setTimeout(() => {
            if (node instanceof Element) {
                scheduled.delete(node);
            }
            sweep(node);
        }, 600);
    };

    later(document);
    const observer = new MutationObserver((records) => {
        for (const record of records) {
            for (const added of Array.from(record.addedNodes)) {
                if (added instanceof Element) {
                    later(added);
                }
            }
            for (const removed of Array.from(record.removedNodes)) {
                if (removed instanceof Element) {
                    removed.querySelectorAll?.(ROOT_SELECTOR).forEach(cleanup);
                    if (removed.matches(ROOT_SELECTOR)) {
                        cleanup(removed);
                    }
                }
            }
        }
    });
    observer.observe(document.body, {childList: true, subtree: true});

    return () => {
        observer.disconnect();
        document.querySelectorAll(ROOT_SELECTOR).forEach(cleanup);
    };
};
