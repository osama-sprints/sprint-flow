// The webpack build turns `.css` imports into injected stylesheets; TypeScript
// needs the module shape declared for the import in mermaid_post.tsx.
declare module '*.css';
