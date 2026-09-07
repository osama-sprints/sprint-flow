import React, {useState} from 'react';

/**
 * Inline playback for a bare video URL in an ordinary post.
 *
 * Mattermost classifies a direct .mp4 link as a plain `link` embed and shows
 * only the link. This component is registered through the supported
 * `registerPostWillRenderEmbedComponent` hook, so it renders in the timeline
 * and the thread panel alike, for any post — the bot's included — without a
 * custom post type and without copying the file: the <video> streams straight
 * from the original URL with range requests.
 *
 * A plain <video> element needs no CORS for playback and sends no Mattermost
 * credentials to the video host: the request carries only whatever cookies
 * the host itself set, and none exist for a CDN.
 *
 * The approach follows the community "videourl" plugin (a match on video
 * extensions rendering a native <video>) rather than its code, so it lives in
 * this plugin instead of a second one. The link in the message text is left as
 * it is, and a second "Open original" link sits under the player, so a client
 * that cannot play the file still has a click-through.
 */

const VIDEO_EXTENSIONS = /\.(mp4|webm|m4v|ogv)$/i;

export type EmbedProps = {
    embed: {type: string; url: string; data?: unknown};
};

/**
 * Decide whether a URL points at a browser-playable video file.
 *
 * The decision is made on the parsed PATH, so a query string or fragment
 * (`…/final.mp4?t=5#intro`) neither breaks the match nor gets stripped from
 * the URL the player is given.
 *
 * @param url Any string.
 * @returns True for an http(s) URL whose path ends in a playable container.
 */
export const isVideoUrl = (url: unknown): boolean => {
    if (typeof url !== 'string') {
        return false;
    }
    try {
        const parsed = new URL(url);
        return (parsed.protocol === 'https:' || parsed.protocol === 'http:') && VIDEO_EXTENSIONS.test(parsed.pathname);
    } catch (e) {
        return false;
    }
};

/**
 * Decide whether an embed is a direct video link.
 *
 * @param embed The embed Mattermost derived for a post.
 * @returns True for a link embed whose URL is a playable video file.
 */
export const isVideoEmbed = (embed: {type?: string; url?: string}): boolean =>
    embed.type === 'link' && isVideoUrl(embed.url);

/**
 * Find the first playable video URL inside a message's TEXT.
 *
 * The DOM-based fallback cannot help a post whose body this plugin renders
 * itself: registering a post-type component replaces the whole message body,
 * so Mattermost's embed area — and with it the embed hook — is never rendered.
 * A rich reply therefore has to find its own video links, from the Markdown
 * source rather than from anchors that do not exist yet.
 *
 * Markdown link targets are read first, so `[clip](https://…/a.mp4)` yields the
 * URL without the closing parenthesis; bare URLs are then matched with the
 * delimiters Markdown and prose put around them excluded.
 *
 * @param message The post's raw message text.
 * @returns The first playable video URL, or null.
 */
export const firstVideoUrlIn = (message: string): string | null => {
    if (!message) {
        return null;
    }
    const candidates: string[] = [];
    for (const match of message.matchAll(/\]\((https?:\/\/[^\s)]+)\)/gi)) {
        candidates.push(match[1]);
    }
    for (const match of message.matchAll(/https?:\/\/[^\s<>"'`)\]]+/gi)) {
        candidates.push(match[0]);
    }
    for (const candidate of candidates) {
        // Trailing sentence punctuation is not part of the URL.
        const cleaned = candidate.replace(/[.,;:!?]+$/, '');
        if (isVideoUrl(cleaned)) {
            return cleaned;
        }
    }
    return null;
};

const VideoEmbed = ({embed}: EmbedProps) => {
    const [failed, setFailed] = useState(false);
    const url = embed.url;

    if (failed) {
        return (
            <a
                className='sf-video__link'
                href={url}
                rel='noreferrer noopener'
                target='_blank'
            >{'Open video'}</a>
        );
    }

    return (
        <div className='sf-video'>
            <video
                className='sf-video__player'
                controls={true}
                preload='metadata'
                src={url}
                onError={() => setFailed(true)}
            />
            <a
                className='sf-video__link'
                href={url}
                rel='noreferrer noopener'
                target='_blank'
            >{'Open original'}</a>
        </div>
    );
};

export default VideoEmbed;
