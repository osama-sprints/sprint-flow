import React from 'react';
import ReactMarkdown from 'react-markdown';

import {resolveDirection} from '../bidi/direction';

/**
 * The fallback text of a rich reply, rendered as Markdown with per-block
 * direction.
 *
 * Registering a post-type component replaces the whole message body, and the
 * webapp exposes no Markdown renderer to plugins — so a rich reply has to
 * render its own text or lose every list and code block the agent wrote.
 * react-markdown does the parsing (CommonMark, no `dangerouslySetInnerHTML`,
 * so no markup can be injected); the only thing added here is the direction
 * decision, which is ours because it is the same policy the bidi controller
 * applies to ordinary posts.
 */

/**
 * Read the text of a rendered block so its direction can be decided.
 *
 * @param children React children of one block element.
 * @returns The concatenated text.
 */
const textOf = (children: React.ReactNode): string => {
    let text = '';
    React.Children.forEach(children, (child) => {
        if (typeof child === 'string' || typeof child === 'number') {
            text += String(child);
        } else if (React.isValidElement(child)) {
            const element = child as React.ReactElement<{children?: React.ReactNode}>;

            // Inline code says nothing about the language of the sentence
            // carrying it, exactly as in the controller's prose extraction.
            if (element.type !== 'code') {
                text += textOf(element.props.children);
            }
        }
    });
    return text;
};

/**
 * Build a renderer for one block tag that stamps the resolved direction.
 *
 * @param Tag The intrinsic element to render.
 * @returns A react-markdown component renderer.
 */
const directed = (Tag: 'p' | 'ul' | 'ol' | 'li' | 'blockquote' | 'h1' | 'h2' | 'h3') =>
    function DirectedBlock({children}: {children?: React.ReactNode}) {
        const dir = resolveDirection(textOf(children)) || undefined;
        return (
            <Tag
                dir={dir}
                data-sf-dir={dir}
            >{children}</Tag>
        );
    };

const COMPONENTS = {
    p: directed('p'),
    ul: directed('ul'),
    ol: directed('ol'),
    li: directed('li'),
    blockquote: directed('blockquote'),
    h1: directed('h1'),
    h2: directed('h2'),
    h3: directed('h3'),

    // Links open in a new tab and never carry the referrer.
    a: function Anchor({href, children}: {href?: string; children?: React.ReactNode}) {
        const safe = href && (/^https?:\/\//i).test(href) ? href : undefined;
        return safe ? (
            <a
                href={safe}
                rel='noreferrer noopener'
                target='_blank'
            >{children}</a>
        ) : <span>{children}</span>;
    },
};

const MessageBody = ({message}: {message: string}) => (
    <ReactMarkdown components={COMPONENTS}>{message}</ReactMarkdown>
);

export default MessageBody;
