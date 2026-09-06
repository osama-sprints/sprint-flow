/**
 * Mermaid theme selection.
 *
 * Mattermost exposes the active theme as CSS custom properties on the root
 * element, so we can pick a light or dark Mermaid theme without pulling in a
 * mattermost-redux selector (and without re-rendering on every store change).
 * The value is read at render time, so a theme switch that remounts the post
 * list picks up the new one.
 */
export type MermaidTheme = 'default' | 'dark';

const FALLBACK_BG = '#ffffff';

/** Parse "#rrggbb", "#rgb" or "rgb(r, g, b)" into [r, g, b]; null if unknown. */
const parseColor = (value: string): [number, number, number] | null => {
    const hex = value.trim().match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
    if (hex) {
        const h = hex[1].length === 3 ? hex[1].replace(/./g, (c) => c + c) : hex[1];
        return [
            parseInt(h.slice(0, 2), 16),
            parseInt(h.slice(2, 4), 16),
            parseInt(h.slice(4, 6), 16),
        ];
    }

    const rgb = value.match(/rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/i);
    if (rgb) {
        return [Number(rgb[1]), Number(rgb[2]), Number(rgb[3])];
    }

    return null;
};

/**
 * Return the Mermaid theme matching the user's Mattermost theme.
 *
 * @param element Any node inside the post, used to resolve the CSS variable in
 *     its own cascade. Falls back to the document root.
 */
export const currentMermaidTheme = (element?: Element | null): MermaidTheme => {
    const target = element ?? document.documentElement;
    const raw = getComputedStyle(target).getPropertyValue('--center-channel-bg') || FALLBACK_BG;
    const rgb = parseColor(raw.startsWith('#') || raw.startsWith('rgb') ? raw : `rgb(${raw})`);
    if (!rgb) {
        return 'default';
    }

    // Rec. 709 luma; anything darker than mid-grey gets the dark diagram theme.
    const luma = (0.2126 * rgb[0]) + (0.7152 * rgb[1]) + (0.0722 * rgb[2]);
    return luma < 128 ? 'dark' : 'default';
};
