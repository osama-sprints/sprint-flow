import React, {useMemo, useRef, useState} from 'react';

import MermaidDiagram from './mermaid_diagram';
import ChartArtifact from './chart_artifact';
import ReactArtifact from './react_artifact';
import ArtifactErrorBoundary from './error_boundary';
import ExpandedView from './expanded_view';
import {currentMermaidTheme} from '../utils/theme';
import {resolveDirection} from '../bidi/direction';
import {ArtifactRef, ChartContent, MermaidContent, ReactContent} from '../types';
import {useArtifactContent} from '../api/artifacts';

type Props = {
    artifact: ArtifactRef;
};

type View = 'preview' | 'source';

const KIND_LABEL: Record<string, string> = {
    mermaid: 'Diagram',
    chart: 'Chart',
    react: 'Interface',
    image: 'Image',
};

/**
 * Render the body of one artifact, by kind.
 *
 * Unknown or not-yet-supported kinds degrade to a readable notice instead of an
 * empty box: a backend that stages a kind this plugin version does not know is
 * an expected state during a rollout, not a bug.
 */
const ArtifactBody = ({artifact, expanded}: {artifact: ArtifactRef; expanded: boolean}) => {
    const theme = useMemo(() => currentMermaidTheme(null), []);

    // Content is inline for small artifacts and fetched for everything else;
    // the hook resolves both, so the renderer below never has to care which.
    const {content, error: fetchError, loading} = useArtifactContent<MermaidContent | ChartContent | ReactContent>(artifact);

    if (artifact.status === 'failed') {
        return (
            <div className='sf-artifact__error'>
                {artifact.error || 'This artifact could not be generated.'}
            </div>
        );
    }

    if (artifact.status === 'pending' || artifact.status === 'running') {
        return (
            <div
                className='sf-artifact__loading'
                aria-live='polite'
            >
                <span className='sf-artifact__spinner'/>
                {artifact.status === 'pending' ? 'Queued…' : 'Generating…'}
            </div>
        );
    }

    if (loading) {
        return (
            <div
                className='sf-artifact__loading'
                aria-live='polite'
            >
                <span className='sf-artifact__spinner'/>
                {'Loading…'}
            </div>
        );
    }

    if (fetchError) {
        return <div className='sf-artifact__error'>{fetchError}</div>;
    }

    if (artifact.kind === 'mermaid') {
        const definition = ((content as MermaidContent | null)?.definition || '').trim();
        if (!definition) {
            return <div className='sf-artifact__error'>{'This diagram has no definition.'}</div>;
        }
        return (
            <div className={expanded ? 'sf-artifact__body sf-artifact__body--expanded' : 'sf-artifact__body'}>
                <MermaidDiagram
                    definition={definition}
                    theme={theme}
                />
            </div>
        );
    }

    if (artifact.kind === 'chart') {
        const chart = content as ChartContent | null;
        if (!chart || typeof chart.spec !== 'object' || !Array.isArray(chart.data)) {
            return <div className='sf-artifact__error'>{'This chart has no specification.'}</div>;
        }
        return (
            <div className={expanded ? 'sf-artifact__body sf-artifact__body--expanded' : 'sf-artifact__body'}>
                <ChartArtifact
                    content={chart}
                    expanded={expanded}
                />
            </div>
        );
    }

    if (artifact.kind === 'react') {
        const generated = content as ReactContent | null;
        if (!generated || typeof generated.source !== 'string' || !generated.source.trim()) {
            return <div className='sf-artifact__error'>{'This interface has no source.'}</div>;
        }
        return (
            <div className={expanded ? 'sf-artifact__body sf-artifact__body--expanded' : 'sf-artifact__body'}>
                <ReactArtifact
                    content={generated}
                    expanded={expanded}
                    title={artifact.title}
                />
            </div>
        );
    }

    return (
        <div className='sf-artifact__notice'>
            {`This ${KIND_LABEL[artifact.kind] || artifact.kind} needs a newer version of the SprintFlow plugin to display.`}
        </div>
    );
};


/**
 * One artifact: header, inline preview, expand, and an optional source view.
 *
 * All interaction is local to the viewer — expanding or reading the source
 * writes nothing back to the post, so two people looking at the same reply
 * never affect each other.
 */
const ArtifactCard = ({artifact}: Props) => {
    const [view, setView] = useState<View>('preview');
    const [expanded, setExpanded] = useState(false);
    const root = useRef<HTMLDivElement>(null);

    const title = artifact.title || KIND_LABEL[artifact.kind] || 'Artifact';
    // Only inline content can be shown as source without a second fetch;
    // that is exactly the small-artifact case where a source view is useful.
    const inline = artifact.inline as MermaidContent & ChartContent | undefined;
    const source = (() => {
        if (artifact.kind === 'mermaid' && typeof inline?.definition === 'string') {
            return inline.definition;
        }
        if (artifact.kind === 'chart' && inline?.spec) {
            return JSON.stringify({spec: inline.spec, rows: (inline.data || []).length}, null, 2);
        }
        return '';
    })();
    const titleDir = resolveDirection(title) || undefined;
    const descriptionDir = artifact.description ? resolveDirection(artifact.description) || undefined : undefined;

    const body = (
        <ArtifactErrorBoundary label={title}>
            {view === 'source' && source ? (
                <pre className='sf-artifact__source'>{source}</pre>
            ) : (
                <ArtifactBody
                    artifact={artifact}
                    expanded={false}
                />
            )}
        </ArtifactErrorBoundary>
    );

    return (
        <div
            className='sf-artifact'
            ref={root}
            data-artifact-kind={artifact.kind}
        >
            <div className='sf-artifact__header'>
                <span
                    className='sf-artifact__title'
                    dir={titleDir}
                    data-sf-dir={titleDir}
                >{title}</span>
                <div className='sf-artifact__actions'>
                    {source ? (
                        <button
                            className='sf-artifact__button'
                            onClick={() => setView((v) => (v === 'preview' ? 'source' : 'preview'))}
                            aria-pressed={view === 'source'}
                        >{view === 'preview' ? 'Source' : 'Preview'}</button>
                    ) : null}
                    <button
                        className='sf-artifact__button'
                        onClick={() => setExpanded(true)}
                    >{'Expand'}</button>
                </div>
            </div>

            {body}

            {artifact.description ? (
                <div
                    className='sf-artifact__caption sf-artifact__prose'
                    dir={descriptionDir}
                    data-sf-dir={descriptionDir}
                >{artifact.description}</div>
            ) : null}

            {expanded ? (
                <ExpandedView
                    title={title}
                    onClose={() => setExpanded(false)}
                >
                    <ArtifactErrorBoundary label={title}>
                        <ArtifactBody
                            artifact={artifact}
                            expanded={true}
                        />
                    </ArtifactErrorBoundary>
                </ExpandedView>
            ) : null}
        </div>
    );
};

export default ArtifactCard;
