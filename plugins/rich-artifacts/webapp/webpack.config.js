/* eslint-env node */

const path = require('path');

// Mattermost hands plugins its own copies of these at runtime through the
// `window` globals below, so bundling them would ship a second React and break
// hooks. `mermaid` and `vega` are NOT in this list on purpose: the webapp does
// not expose them, so they must be bundled.
const EXTERNALS = {
    react: 'React',
    'react-dom': 'ReactDOM',
    redux: 'Redux',
    'react-redux': 'ReactRedux',
    'prop-types': 'PropTypes',
    'react-bootstrap': 'ReactBootstrap',
};

const PLUGIN_ID = 'com.sprintflow.mermaid';

module.exports = {
    entry: './src/index.tsx',
    resolve: {
        extensions: ['.ts', '.tsx', '.js', '.jsx'],
    },
    module: {
        rules: [
            {
                test: /\.(ts|tsx|js|jsx)$/,
                exclude: /node_modules/,
                use: {
                    loader: 'babel-loader',
                    options: {
                        presets: [
                            ['@babel/preset-env', {targets: {chrome: '90', firefox: '90', safari: '15'}}],
                            ['@babel/preset-react', {runtime: 'classic'}],
                            '@babel/preset-typescript',
                        ],
                    },
                },
            },
            {
                test: /\.css$/,
                use: ['style-loader', 'css-loader'],
            },
        ],
    },
    externals: EXTERNALS,
    output: {
        path: path.join(__dirname, 'dist'),

        // Mattermost injects exactly ONE file per plugin: main.js. Everything
        // heavy (mermaid and its grammars, vega) is a lazily loaded chunk, and
        // chunks are fetched from the plugin's OWN endpoint on the Mattermost
        // origin — which is `'self'` under the stock CSP, so nothing has to be
        // relaxed. The Go component embeds and serves them.
        filename: 'main.js',
        chunkFilename: 'chunk.[name].[contenthash:8].js',
        publicPath: `/plugins/${PLUGIN_ID}/api/v1/assets/`,
    },
    optimization: {
        splitChunks: {chunks: 'async'},
        runtimeChunk: false,
    },
    performance: {
        hints: false,
    },
    devtool: 'source-map',
};
