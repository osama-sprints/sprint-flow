/**
 * Authenticated artifact fetch.
 *
 * A reference carries its content inline only while that content is small
 * enough for post props. Anything larger — React source, a big chart dataset,
 * a later revision — is fetched here, from the plugin's own endpoint on the
 * Mattermost origin. The request is same-origin with the viewer's session
 * cookie, so the server component can check that THIS viewer may read the post
 * the artifact was published in. The artifact id is an identifier, not a
 * capability: it grants nothing without that session.
 */

import {useEffect, useState} from 'react';

import {ArtifactRef} from '../types';

const PLUGIN_ID = 'com.sprintflow.mermaid';

export type FetchState<T> = {
    content: T | null;
    error: string | null;
    loading: boolean;
};

/**
 * Read one artifact's content from the plugin endpoint.
 *
 * @param id The artifact id.
 * @returns The content payload.
 * @throws Error when the viewer may not read it, or the fetch fails.
 */
export const fetchArtifactContent = async (id: string): Promise<unknown> => {
    const response = await fetch(
        `/plugins/${PLUGIN_ID}/api/v1/artifacts/${encodeURIComponent(id)}`,
        {
            credentials: 'same-origin',

            // Mattermost requires this header on cookie-authenticated requests;
            // without it the session is rejected as a CSRF risk.
            headers: {'X-Requested-With': 'XMLHttpRequest'},
        },
    );

    if (response.status === 401 || response.status === 404) {
        // The server answers 404 for "not permitted" as well, so that an
        // unauthorised viewer cannot learn that an id exists.
        throw new Error('This artifact is not available to you.');
    }
    if (!response.ok) {
        throw new Error(`Could not load this artifact (${response.status}).`);
    }

    const body = await response.json() as {content?: unknown};
    return body.content ?? null;
};

/**
 * Resolve an artifact's content: inline when present, fetched otherwise.
 *
 * @param artifact The reference from post props.
 * @returns Loading, error and content state.
 */
export const useArtifactContent = <T>(artifact: ArtifactRef): FetchState<T> => {
    const inline = artifact.inline as T | undefined;
    const [state, setState] = useState<FetchState<T>>({
        content: inline ?? null,
        error: null,
        loading: inline === undefined,
    });

    useEffect(() => {
        if (inline !== undefined) {
            setState({content: inline, error: null, loading: false});
            return undefined;
        }

        let cancelled = false;
        setState({content: null, error: null, loading: true});

        fetchArtifactContent(artifact.id).then((content) => {
            if (!cancelled) {
                setState({content: content as T, error: null, loading: false});
            }
        }).catch((e: unknown) => {
            if (!cancelled) {
                setState({content: null, error: e instanceof Error ? e.message : String(e), loading: false});
            }
        });

        return () => {
            cancelled = true;
        };

        // The revision is part of the key: a worker that finishes an image
        // bumps it, and the card must re-read rather than show stale content.
    }, [artifact.id, artifact.revision, inline]);

    return state;
};
