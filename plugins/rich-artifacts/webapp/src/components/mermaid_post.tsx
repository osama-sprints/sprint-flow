import React, {useMemo, useRef, useState} from 'react';

import MermaidDiagram from './mermaid_diagram';
import {currentMermaidTheme} from '../utils/theme';
import type {PostTypeComponentProps} from '../types';

import './mermaid_post.css';

type View = 'diagram' | 'source';

/** A Mermaid definition longer than this is refused rather than rendered. */
const MAX_DEFINITION_CHARS = 20000;

const MAX_LABEL_CHARS = 300;

/**
 * Read one legacy prop as a bounded string.
 *
 * @param value The raw prop.
 * @param limit Longest accepted length.
 * @returns The trimmed string, or '' when the prop is absent or unusable.
 */
const readProp = (value: unknown, limit: number): string => {
    if (typeof value !== 'string') {
        return '';
    }
    const trimmed = value.trim();
    return trimmed.length > limit ? '' : trimmed;
};

/**
 * The component Mattermost mounts in place of the message body for every post
 * whose `type` is `custom_interactive_mermaid`.
 *
 * All interaction state lives in this component. Nothing here writes back to
 * the post: `post.props` is the bot's immutable payload, and toggling the view
 * leaves the database row untouched (and therefore looks the same to every
 * other channel member).
 */
const MermaidPost = ({post}: PostTypeComponentProps) => {
    const root = useRef<HTMLDivElement>(null);
    const [view, setView] = useState<View>('diagram');
    const [collapsed, setCollapsed] = useState(false);

    // Legacy props are untyped JSON on posts that may predate any validation,
    // so each field is checked rather than assumed: a non-string definition
    // would throw on .trim() and blank the whole post.
    const definition = readProp(post.props?.mermaid_definition, MAX_DEFINITION_CHARS);
    const title = readProp(post.props?.title, MAX_LABEL_CHARS);
    const caption = readProp(post.props?.caption, MAX_LABEL_CHARS);

    // Read once per mount: a theme switch remounts the post list anyway.
    const theme = useMemo(() => currentMermaidTheme(root.current), []);

    // Fall back to the plain message so a post is never blank — the same text
    // mobile clients and search results see, since they do not load plugins.
    if (!definition) {
        return <div className='sprintflow-mermaid'>{post.message}</div>;
    }

    return (
        <div
            ref={root}
            className='sprintflow-mermaid'
        >
            <div className='sprintflow-mermaid__header'>
                <span className='sprintflow-mermaid__title'>{title || 'Diagram'}</span>
                <div className='sprintflow-mermaid__actions'>
                    <button
                        className='sprintflow-mermaid__button'
                        onClick={() => setView((v) => (v === 'diagram' ? 'source' : 'diagram'))}
                        aria-pressed={view === 'source'}
                    >
                        {view === 'diagram' ? 'View source' : 'View diagram'}
                    </button>
                    <button
                        className='sprintflow-mermaid__button'
                        onClick={() => setCollapsed((c) => !c)}
                        aria-expanded={!collapsed}
                    >
                        {collapsed ? 'Expand' : 'Collapse'}
                    </button>
                </div>
            </div>

            {!collapsed && (view === 'diagram' ? (
                <MermaidDiagram
                    definition={definition}
                    theme={theme}
                />
            ) : (
                <pre className='sprintflow-mermaid__source'>{definition}</pre>
            ))}

            {!collapsed && caption && (
                <div className='sprintflow-mermaid__caption'>{caption}</div>
            )}
        </div>
    );
};

export default MermaidPost;
