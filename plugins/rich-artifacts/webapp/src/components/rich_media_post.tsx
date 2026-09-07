import React from 'react';

import ArtifactCard from './artifact_card';
import MessageBody from './message_body';
import VideoEmbed, {firstVideoUrlIn} from './video_embed';
import {ENVELOPE_VERSION, PostTypeComponentProps, parseArtifacts} from '../types';

import './rich_media.css';

/**
 * The component Mattermost mounts for `custom_sprintflow_rich_media` posts.
 *
 * The post's `message` is the fallback text and is ALWAYS rendered: it is what
 * mobile clients, search, notifications and any client without this plugin
 * show, so the reply must read correctly with the artifacts removed.
 *
 * A video link in that text is rendered here too. Registering a post-type
 * component replaces the whole message body, which takes Mattermost's embed
 * area — and therefore `registerPostWillRenderEmbedComponent` — out of the
 * picture, and the DOM fallback watches `.post-message__text`, which this
 * component does not render. Without this, a reply that both carries an
 * artifact and links a video showed the link and no player, while the same
 * link in an ordinary post played inline.
 */
const RichMediaPost = ({post}: PostTypeComponentProps) => {
    // A finished image is already in the post as a native Mattermost
    // attachment, with the lightbox and download that come with it. Rendering a
    // card for it as well would duplicate the picture and — worse, before this
    // — claim the plugin could not display it. Its card exists only while the
    // image is being generated or has failed.
    const artifacts = parseArtifacts(post.props).filter(
        (artifact) => !(artifact.kind === 'image' && artifact.status === 'ready'),
    );
    const version = typeof post.props?.sf_envelope_version === 'number' ? post.props.sf_envelope_version : 0;
    const message = (post.message || '').trim();
    const videoUrl = firstVideoUrlIn(message);

    return (
        <div className='sf-rich-media'>
            {message ? (
                <div className='sf-artifact__prose sf-rich-media__message'>
                    <MessageBody message={message}/>
                </div>
            ) : null}

            {videoUrl ? (
                <VideoEmbed embed={{type: 'link', url: videoUrl}}/>
            ) : null}

            {version > ENVELOPE_VERSION ? (
                <div className='sf-artifact__notice'>
                    {'This reply was written by a newer version of SprintFlow. Update the plugin to see everything it contains.'}
                </div>
            ) : null}

            {artifacts.map((artifact) => (
                <ArtifactCard
                    key={artifact.id}
                    artifact={artifact}
                />
            ))}
        </div>
    );
};

export default RichMediaPost;
