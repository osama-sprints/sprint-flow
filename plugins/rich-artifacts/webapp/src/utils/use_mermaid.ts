import {useEffect, useState} from 'react';

import type {MermaidTheme} from './theme';

type BindFunctions = (element: Element) => void;

export type MermaidRender = {
    svg: string;
    bind: BindFunctions | null;
    error: string | null;
    loading: boolean;
};

// Mermaid keys its scratch DOM node by the render id, so the id must be unique
// per render call — the same diagram posted twice in one channel would collide.
let renderSeq = 0;

/**
 * Render a Mermaid definition to an SVG string.
 *
 * `mermaid.render` is async and mounts a temporary node on document.body, so
 * every call is guarded against the post scrolling out of the virtualised post
 * list mid-render, and a failed parse cleans up the error node Mermaid leaves
 * behind (it is styled as a red "Syntax error" box and would otherwise stay on
 * screen forever).
 */
export const useMermaid = (definition: string, theme: MermaidTheme, enabled: boolean): MermaidRender => {
    const [state, setState] = useState<MermaidRender>({svg: '', bind: null, error: null, loading: enabled});

    useEffect(() => {
        if (!enabled || !definition.trim()) {
            setState({svg: '', bind: null, error: null, loading: false});
            return undefined;
        }

        let cancelled = false;
        const id = `sprintflow-mermaid-${renderSeq++}`;

        setState((prev) => ({...prev, loading: true}));

        // Mermaid (and the grammar and layout libraries it drags in) is a lazy
        // chunk: a channel full of ordinary posts never pays for it.
        import(/* webpackChunkName: "mermaid" */ 'mermaid').then(({default: mermaid}) => {
            // securityLevel 'strict' sanitizes the definition and disables click
            // bindings and inline HTML: the source is bot-authored, but it is
            // still untrusted input flowing into the DOM of every channel member.
            mermaid.initialize({
                startOnLoad: false,
                securityLevel: 'strict',
                htmlLabels: false,
                theme,
                fontFamily: 'Open Sans, sans-serif',
            });
            return mermaid.render(id, definition);
        }).then(({svg, bindFunctions}) => {
            if (!cancelled) {
                setState({svg, bind: bindFunctions ?? null, error: null, loading: false});
            }
        }).catch((e: unknown) => {
            document.getElementById(`d${id}`)?.remove();
            if (!cancelled) {
                const message = e instanceof Error ? e.message : String(e);
                setState({svg: '', bind: null, error: message, loading: false});
            }
        });

        return () => {
            cancelled = true;
        };
    }, [definition, theme, enabled]);

    return state;
};
