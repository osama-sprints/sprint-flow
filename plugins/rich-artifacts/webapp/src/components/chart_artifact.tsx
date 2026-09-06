import React, {useEffect, useRef, useState} from 'react';
import type {VisualizationSpec} from 'vega-embed';

import type {ChartContent} from '../types';

type Props = {
    content: ChartContent;
    expanded: boolean;
};

/**
 * A Vega-Lite chart, rendered with vega-embed.
 *
 * The specification is authored by an agent and validated server-side; this
 * side still controls the parts that decide what the chart may reach:
 *
 * * `data` is injected here from the artifact's rows, so a spec can never point
 *   at a URL and pull something over the network;
 * * `actions` is false, which removes the export / "Open in Vega Editor" menu —
 *   that menu would post the artifact's data to a third-party site;
 * * `loader` is given no base URL and never used, and the container is sized to
 *   the card rather than to anything the spec asks for.
 *
 * Vega normally COMPILES its expressions with `new Function`, which Mattermost's
 * Content-Security-Policy (`script-src 'self'`, no `unsafe-eval`) refuses — every
 * chart failed with "Evaluating a string as JavaScript violates ... CSP" before
 * this. `ast: true` plus Vega's own `expressionInterpreter` evaluates the
 * expression AST instead, so charts render under the stock policy and the CSP
 * is left exactly as Mattermost ships it.
 *
 * The view is finalized on unmount and before every re-render, because a chart
 * that scrolls out of a virtualised post list otherwise leaves its listeners
 * and animation timers behind.
 */
const ChartArtifact = ({content, expanded}: Props) => {
    const container = useRef<HTMLDivElement>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        const element = container.current;
        if (!element) {
            return undefined;
        }

        let finalize: (() => void) | null = null;
        let cancelled = false;

        const spec = {
            $schema: 'https://vega.github.io/schema/vega-lite/v5.json',
            ...content.spec,

            // Injected here, never taken from the spec.
            data: {values: content.data},
            width: 'container',
            autosize: {type: 'fit', contains: 'padding'},
        } as VisualizationSpec;

        // Vega is a lazy chunk, fetched only when a post actually holds a chart.
        Promise.all([
            import(/* webpackChunkName: "vega" */ 'vega-embed'),
            import(/* webpackChunkName: "vega" */ 'vega-interpreter'),
        ]).then(([{default: embed}, {expressionInterpreter}]) => embed(element, spec, {
            // Interpret, never compile: see the CSP note above.
            ast: true,
            expr: expressionInterpreter,
            actions: false,
            tooltip: true,
            renderer: 'svg',
            mode: 'vega-lite',
            height: expanded ? 420 : 240,
        })).then((result) => {
            if (cancelled) {
                result.finalize();
                return;
            }
            finalize = result.finalize;
        }).catch((e: unknown) => {
            if (!cancelled) {
                setError(e instanceof Error ? e.message : String(e));
            }
        });

        return () => {
            cancelled = true;
            finalize?.();
        };
    }, [content, expanded]);

    if (error) {
        return (
            <div className='sf-artifact__error'>
                <b>{'This chart could not be drawn.'}</b>
                <pre className='sf-artifact__source'>{error}</pre>
            </div>
        );
    }

    return (
        <div
            className='sf-artifact__chart'
            ref={container}
        />
    );
};

export default ChartArtifact;
