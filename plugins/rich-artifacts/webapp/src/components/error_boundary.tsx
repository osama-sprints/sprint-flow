import React from 'react';

type Props = {
    label: string;
    children: React.ReactNode;
};

type State = {
    message: string | null;
};

/**
 * Contains a render failure to one artifact.
 *
 * A reply may carry several artifacts; a throw inside one of them must not
 * blank the others or the surrounding chat. Note the limit, which is a property
 * of React and not of this code: a boundary catches THROWS during render. It
 * cannot stop an infinite loop, and it cannot catch an async failure — those
 * are handled inside each renderer instead.
 */
export default class ArtifactErrorBoundary extends React.Component<Props, State> {
    public constructor(props: Props) {
        super(props);
        this.state = {message: null};
    }

    public static getDerivedStateFromError(error: unknown): State {
        return {message: error instanceof Error ? error.message : String(error)};
    }

    public componentDidCatch(error: unknown): void {
        // eslint-disable-next-line no-console
        console.error('[sprintflow] artifact render failed', error);
    }

    public render(): React.ReactNode {
        if (this.state.message !== null) {
            return (
                <div className='sf-artifact__error'>
                    <b>{`${this.props.label} could not be displayed.`}</b>
                    <pre className='sf-artifact__source'>{this.state.message}</pre>
                </div>
            );
        }
        return this.props.children;
    }
}
