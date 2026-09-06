/**
 * The contract between the agent backend and this plugin.
 *
 * Two post types are supported, deliberately:
 *
 * * `custom_interactive_mermaid` — the original single-diagram post. Posts of
 *   this type already exist in channel history, so its shape is frozen and its
 *   renderer is kept as-is. Never repurpose it.
 * * `custom_sf_rich_media` — the versioned envelope every new reply
 *   uses. It carries an ORDERED collection of artifact references, so one reply
 *   can hold a diagram and a chart and an image.
 *
 * The short name is forced by the server: `Posts.Type` is `varchar(26)`, so
 * `custom_sprintflow_rich_media` (28 characters) is rejected with a 500 at
 * insert time. The legacy type sits exactly on the limit at 26.
 *
 * Post props are small by contract: a reference names an artifact and may carry
 * a bounded inline payload, but substantial content (React source, chart rows,
 * revisions) lives in ai-core and is fetched by id.
 */

export const MERMAID_POST_TYPE = 'custom_interactive_mermaid';
export const RICH_MEDIA_POST_TYPE = 'custom_sf_rich_media';

/** Envelope schema version. Bump only for a breaking props change. */
export const ENVELOPE_VERSION = 1;

/** Largest inline payload a reference may carry, in characters of JSON. */
export const MAX_INLINE_CHARS = 4096;

/** Most artifacts one reply may render. Beyond this the post is malformed. */
export const MAX_ARTIFACTS = 8;

export type ArtifactKind = 'mermaid' | 'chart' | 'react' | 'image';

export type ArtifactStatus = 'pending' | 'running' | 'ready' | 'failed';

/** A Mermaid artifact's inline payload. */
export type MermaidContent = {
    definition: string;
};

/** A generated interface's payload: component source and its input data. */
export type ReactContent = {
    source: string;
    data: Record<string, unknown>;
};

/** A chart artifact's inline payload: a Vega-Lite spec plus its rows. */
export type ChartContent = {
    spec: Record<string, unknown>;
    data: Array<Record<string, unknown>>;
};

/** One artifact as it appears in post props. */
export type ArtifactRef = {
    id: string;
    kind: ArtifactKind;
    schema_version: number;
    revision: number;
    status: ArtifactStatus;
    title?: string;
    description?: string;
    error?: string;

    /** Bounded payload for artifacts small enough to travel in props. */
    inline?: unknown;
};

/** Props of a `custom_sprintflow_rich_media` post. */
export type RichMediaProps = {
    sf_envelope_version?: number;
    sf_artifacts?: unknown;
};

/** Props of a legacy `custom_interactive_mermaid` post. */
export type MermaidPostProps = {
    mermaid_definition?: string;
    title?: string;
    caption?: string;
};

export type Post = {
    id: string;
    type: string;
    message: string;
    props: (RichMediaProps & MermaidPostProps) & Record<string, unknown>;
};

/** Props Mattermost passes to a component registered for a post type. */
export type PostTypeComponentProps = {
    post: Post;
    isRHS?: boolean;
    compactDisplay?: boolean;
};

const KINDS: ArtifactKind[] = ['mermaid', 'chart', 'react', 'image'];
const STATUSES: ArtifactStatus[] = ['pending', 'running', 'ready', 'failed'];

/**
 * Validate one artifact reference coming off a post.
 *
 * Props are server-authored but arrive as untyped JSON that may have been
 * written by an older or newer backend, so every field is checked before the
 * renderer trusts it. An invalid reference is dropped rather than thrown: one
 * bad artifact must not blank out the rest of the reply.
 *
 * @param value A candidate reference.
 * @returns The typed reference, or null when it is unusable.
 */
export const parseArtifactRef = (value: unknown): ArtifactRef | null => {
    if (typeof value !== 'object' || value === null) {
        return null;
    }
    const raw = value as Record<string, unknown>;

    if (typeof raw.id !== 'string' || !raw.id) {
        return null;
    }
    if (typeof raw.kind !== 'string' || !KINDS.includes(raw.kind as ArtifactKind)) {
        return null;
    }
    if (typeof raw.status !== 'string' || !STATUSES.includes(raw.status as ArtifactStatus)) {
        return null;
    }
    // A reference written by a newer backend is not silently rendered with
    // this version's assumptions about its content.
    if (typeof raw.schema_version !== 'number' || raw.schema_version < 1 || raw.schema_version > ENVELOPE_VERSION) {
        return null;
    }

    // The cap applies to objects too: `inline` is usually {definition: "..."},
    // and a string-only check let an oversized object through untouched.
    let inline = raw.inline;
    if (inline !== undefined && inline !== null) {
        let size = 0;
        try {
            size = typeof inline === 'string' ? inline.length : JSON.stringify(inline).length;
        } catch (e) {
            // Cyclic or unserialisable: not something a post can legitimately
            // carry, so drop the payload rather than trust it.
            size = MAX_INLINE_CHARS + 1;
        }
        if (size > MAX_INLINE_CHARS) {
            inline = undefined;
        }
    }

    return {
        id: raw.id,
        kind: raw.kind as ArtifactKind,
        schema_version: raw.schema_version,
        revision: typeof raw.revision === 'number' ? raw.revision : 1,
        status: raw.status as ArtifactStatus,
        title: typeof raw.title === 'string' ? raw.title : undefined,
        description: typeof raw.description === 'string' ? raw.description : undefined,
        error: typeof raw.error === 'string' ? raw.error : undefined,
        inline,
    };
};

/**
 * Read the ordered artifact collection out of a post's props.
 *
 * @param props The post's props map.
 * @returns Every valid reference, in the order the backend staged them.
 */
export const parseArtifacts = (props: RichMediaProps | undefined): ArtifactRef[] => {
    const raw = props?.sf_artifacts;
    if (!Array.isArray(raw)) {
        return [];
    }
    return raw
        .slice(0, MAX_ARTIFACTS)
        .map(parseArtifactRef)
        .filter((ref): ref is ArtifactRef => ref !== null);
};

/**
 * Read a chart's specification and rows from a validated reference.
 *
 * @param ref The artifact reference.
 * @returns The content, or null when it is absent or malformed.
 */
export const chartContentOf = (ref: ArtifactRef): ChartContent | null => {
    const inline = ref.inline;
    if (typeof inline !== 'object' || inline === null) {
        return null;
    }
    const {spec, data} = inline as ChartContent;
    if (typeof spec !== 'object' || spec === null || !Array.isArray(data)) {
        return null;
    }
    return {spec, data};
};

/**
 * Read a Mermaid definition from a validated reference.
 *
 * @param ref The artifact reference.
 * @returns The definition, or an empty string when absent or malformed.
 */
export const mermaidDefinitionOf = (ref: ArtifactRef): string => {
    const inline = ref.inline;
    if (typeof inline === 'object' && inline !== null) {
        const definition = (inline as MermaidContent).definition;
        return typeof definition === 'string' ? definition.trim() : '';
    }
    return '';
};
