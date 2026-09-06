import React, {useLayoutEffect, useRef} from 'react';

import {useMermaid} from '../utils/use_mermaid';
import type {MermaidTheme} from '../utils/theme';

type Props = {
    definition: string;
    theme: MermaidTheme;
};

/**
 * The rendered graph. Owns no state of its own beyond the render result, so it
 * can be reused anywhere a Mermaid definition needs to appear.
 */
const MermaidDiagram = ({definition, theme}: Props) => {
    const container = useRef<HTMLDivElement>(null);
    const {svg, bind, error, loading} = useMermaid(definition, theme, true);

    // Mermaid's interaction bindings need the SVG to already be in the document.
    useLayoutEffect(() => {
        if (svg && bind && container.current) {
            bind(container.current);
        }
    }, [svg, bind]);

    if (error) {
        return (
            <div className='sprintflow-mermaid__error'>
                <b>{'Could not render this diagram.'}</b>
                <pre>{error}</pre>
                <pre>{definition}</pre>
            </div>
        );
    }

    return (
        <div
            ref={container}
            className='sprintflow-mermaid__canvas'
            aria-busy={loading}
            aria-label='Mermaid diagram'
            role='img'

            // Safe: Mermaid runs with securityLevel 'strict', which strips
            // scripts and event handlers from the generated SVG.
            dangerouslySetInnerHTML={{__html: svg}}
        />
    );
};

export default MermaidDiagram;
