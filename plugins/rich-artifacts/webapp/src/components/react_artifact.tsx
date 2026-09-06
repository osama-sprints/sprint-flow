import React, {useEffect, useMemo, useRef, useState} from 'react';

import type {ReactContent} from '../types';
import {currentMermaidTheme} from '../utils/theme';
import {resolveDirection} from '../bidi/direction';

type Props = {
    content: ReactContent;
    expanded: boolean;
    title?: string;
};

/**
 * Agent-generated React, compiled here and run in an isolated sandbox.
 *
 * Sandpack was the first candidate and was rejected for a specific reason: its
 * client posts the code to `bundlerURL ?? "https://preview.sandpack-static-
 * server.codesandbox.io"`, so the default configuration ships private artifact
 * source to a third-party origin, and self-hosting means deploying CodeSandbox's
 * separate bundler application. This runtime is the agreed alternative.
 *
 * The split of trust:
 *
 * * **Compilation** happens inside the sandbox too, with a Babel build the
 *   plugin embeds in the runtime document. Keeping the compiler out of the host
 *   page removes ~1.4 MB from the bundle every reader downloads, and means the
 *   parser only ever sees untrusted source on an opaque origin. Nothing is
 *   compiled on the backend.
 * * **Execution** happens only inside an iframe with `sandbox="allow-scripts"`
 *   and no `allow-same-origin`, so the code runs on an opaque origin with no
 *   cookies, no Mattermost session, no access to this page's DOM or Redux
 *   store, and no reachable Mattermost API.
 * * **The bridge** carries a per-instance nonce. A sandboxed frame reports its
 *   origin as "null", so origin cannot identify it: messages are accepted only
 *   when they come from this frame's own `contentWindow` AND carry this
 *   instance's nonce.
 *
 * What the sandbox does NOT provide is CPU isolation. A same-site sandboxed
 * frame shares the page's main thread, and a runaway `while(true)` froze the
 * whole chat in testing. The runtime compiles every loop with an iteration
 * budget and throws past it, which is a partial safeguard only: recursion and
 * long loop-free synchronous work still block the page until they finish,
 * and no error boundary or timer in this file can interrupt them.
 */

const PLUGIN_ID = 'com.sprintflow.mermaid';
const RUNTIME_PATH = `/plugins/${PLUGIN_ID}/api/v1/runtime`;

/** Compilation is bounded: a component larger than this is refused outright. */
const MAX_SOURCE_CHARS = 20000;

type Bridge =
    | {source: 'sprintflow-runtime'; nonce: string; type: 'ready'}
    | {source: 'sprintflow-runtime'; nonce: string; type: 'height'; payload: {height: number}}
    | {source: 'sprintflow-runtime'; nonce: string; type: 'error'; payload: {message: string}};

const ReactArtifact = ({content, expanded, title}: Props) => {
    const frame = useRef<HTMLIFrameElement>(null);
    const [height, setHeight] = useState(expanded ? 420 : 260);
    const [error, setError] = useState<string | null>(null);

    // One nonce per mounted instance: two artifacts in the same reply, or the
    // same artifact in the centre channel and the thread panel, must not be
    // able to answer for each other.
    const nonce = useMemo(() => Math.random().toString(36).slice(2) + Date.now().toString(36), []);

    const tooLarge = (content.source || '').length > MAX_SOURCE_CHARS;

    useEffect(() => {
        if (tooLarge) {
            setError('This component is too large to run.');
            return undefined;
        }

        const onMessage = (event: MessageEvent) => {
            // Frame identity, not origin: a sandboxed frame's origin is "null".
            if (!frame.current || event.source !== frame.current.contentWindow) {
                return;
            }
            const data = event.data as Bridge | undefined;
            if (!data || data.source !== 'sprintflow-runtime' || data.nonce !== nonce) {
                return;
            }

            switch (data.type) {
            case 'ready':
                frame.current.contentWindow?.postMessage(
                    {source: 'sprintflow-host', nonce, type: 'mount', code: content.source, data: content.data || {}},
                    '*',
                );
                break;
            case 'height':
                setHeight(Math.min(expanded ? 900 : 520, Math.max(120, data.payload.height)));
                break;
            case 'error':
                setError(data.payload.message);
                break;
            default:
                break;
            }
        };

        window.addEventListener('message', onMessage);
        return () => window.removeEventListener('message', onMessage);
    }, [tooLarge, content.source, content.data, expanded, nonce]);

    if (error) {
        return (
            <div className='sf-artifact__error'>
                <b>{'This interface could not run.'}</b>
                <pre className='sf-artifact__source'>{error}</pre>
            </div>
        );
    }

    // The runtime is told the reader's theme, the artifact's writing direction
    // and the UI locale, so the design tokens inside the sandbox match the
    // Mattermost page around it and Arabic interfaces lay out right-to-left.
    const theme = currentMermaidTheme(frame.current) === 'dark' ? 'dark' : 'light';
    const dir = resolveDirection(`${title || ''} ${JSON.stringify(content.data || {})}`) || 'ltr';
    const lang = dir === 'rtl' ? 'ar' : (navigator.language || 'en');
    const source =
        `${RUNTIME_PATH}?nonce=${encodeURIComponent(nonce)}&origin=${encodeURIComponent(window.location.origin)}` +
        `&theme=${theme}&dir=${dir}&lang=${encodeURIComponent(lang)}`;

    return (
        <iframe
            className='sf-artifact__sandbox'
            ref={frame}
            src={source}
            title='Generated interface'
            style={{height: `${height}px`}}

            // No allow-same-origin: the frame gets an opaque origin, so it has
            // no cookies, no storage and no access to anything of ours.
            sandbox='allow-scripts'
            referrerPolicy='no-referrer'
        />
    );
};

export default ReactArtifact;
