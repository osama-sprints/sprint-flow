import type {ElementType} from 'react';

import MermaidPost from './components/mermaid_post';
import VideoEmbed, {isVideoEmbed} from './components/video_embed';
import {startVideoLinkFallback} from './components/video_links';
import RichMediaPost from './components/rich_media_post';
import {startBidiController} from './bidi/controller';
import {MERMAID_POST_TYPE, RICH_MEDIA_POST_TYPE} from './types';

import './bidi/bidi.css';
import './components/rich_media.css';

type PluginRegistry = {

    /**
     * Replaces the message body of every post carrying `postType` with the
     * given component. Returns an id that must be handed back on uninitialize
     * so a plugin upgrade does not leave the old component registered.
     */
    registerPostTypeComponent(postType: string, component: ElementType): string;

    unregisterPostTypeComponent(componentId: string): void;

    /**
     * Renders `component` in place of Mattermost's own embed for any post
     * whose embed `match` accepts. Applies to ordinary posts, in the timeline
     * and the thread panel; `toggleable` adds Mattermost's collapse control.
     *
     * Argument order is (match, component, toggleable). Passing the component
     * first made Mattermost call it AS the match function: a React hook ran
     * outside a render (minified error #321), and every post carrying a video
     * link was dropped by the post's error boundary.
     */
    registerPostWillRenderEmbedComponent(
        match: (embed: {type: string; url: string; data?: unknown}) => boolean,
        component: ElementType,
        toggleable: boolean,
    ): string;

    unregisterComponent(componentId: string): void;
};

declare global {
    interface Window {
        registerPlugin(pluginId: string, plugin: Plugin): void;
    }
}

const PLUGIN_ID = 'com.sprintflow.mermaid';

class Plugin {
    private registry?: PluginRegistry;
    private componentIds: string[] = [];
    private embedComponentId?: string;
    private stopBidi?: () => void;
    private stopVideoFallback?: () => void;

    /**
     * @param registry Plugin registration API.
     *     The redux store is passed as a second argument for plugins that need
     *     dispatch or selectors; this one is presentational and ignores it.
     */
    public initialize(registry: PluginRegistry): void {
        this.registry = registry;

        // Both types stay registered. Posts of the legacy type are already in
        // channel history and must keep rendering exactly as they did.
        this.componentIds = [
            registry.registerPostTypeComponent(MERMAID_POST_TYPE, MermaidPost),
            registry.registerPostTypeComponent(RICH_MEDIA_POST_TYPE, RichMediaPost),
        ];

        // A bare video link in ANY post plays inline, streamed from its
        // original URL. This is an embed override, not a post type, so the
        // message and its clickable link are untouched.
        this.embedComponentId = registry.registerPostWillRenderEmbedComponent(isVideoEmbed, VideoEmbed, true);

        // Mattermost embeds only BARE urls; a Markdown video link gets no embed
        // and so never reaches the hook above. This mounts the same player
        // under such messages.
        this.stopVideoFallback = startVideoLinkFallback();

        // Ordinary posts are not custom post types, so no component hook can
        // reach them; the bidi controller is what corrects their direction.
        this.stopBidi = startBidiController();
    }

    // Mattermost calls uninitialize() with no arguments, so the registry is
    // kept from initialize to make the teardown possible at all.
    public uninitialize(): void {
        this.stopBidi?.();
        delete this.stopBidi;
        this.stopVideoFallback?.();
        delete this.stopVideoFallback;

        if (this.registry) {
            for (const id of this.componentIds) {
                this.registry.unregisterPostTypeComponent(id);
            }
            if (this.embedComponentId) {
                this.registry.unregisterComponent(this.embedComponentId);
            }
        }
        this.componentIds = [];
        delete this.embedComponentId;
    }
}

window.registerPlugin(PLUGIN_ID, new Plugin());
