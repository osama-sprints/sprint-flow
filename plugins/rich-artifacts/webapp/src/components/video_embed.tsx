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

const VIDEO_URL = /^https?:\/\/[^\s]+\.(mp4|webm|m4v|ogv)(\?[^\s]*)?$/i;

export type EmbedProps = {
    embed: {type: string; url: string; data?: unknown};
};

/**
 * Decide whether an embed is a direct video link.
 *
 * @param embed The embed Mattermost derived for a post.
 * @returns True for an http(s) URL ending in a browser-playable container.
 */
export const isVideoEmbed = (embed: {type?: string; url?: string}): boolean =>
    embed.type === 'link' && typeof embed.url === 'string' && VIDEO_URL.test(embed.url);

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
