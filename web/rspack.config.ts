import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from '@rspack/cli'
import { rspack } from '@rspack/core'

const root = fileURLToPath(new URL('.', import.meta.url))
const production = process.env.NODE_ENV === 'production'

export default defineConfig({
  context: root,
  mode: production ? 'production' : 'development',
  entry: './src/main.tsx',
  output: {
    path: production ? path.join(root, '../agent/web_dist') : path.join(root, 'dist'),
    filename: 'static/[name].[contenthash:8].js',
    chunkFilename: 'static/[name].[contenthash:8].js',
    cssFilename: 'static/[name].[contenthash:8].css',
    cssChunkFilename: 'static/[name].[contenthash:8].css',
    assetModuleFilename: 'static/media/[name].[contenthash:8][ext]',
    clean: true,
  },
  resolve: { extensionAlias: { '.js': ['.ts', '.tsx', '.js'] } },
  module: {
    parser: { 'css/module': { namedExports: false } },
    rules: [
      { test: /\.module\.css$/, type: 'css/module' },
      { test: /\.css$/, exclude: /\.module\.css$/, type: 'css' },
      { test: /\.(?:woff2?|ttf)$/, type: 'asset/resource' },
      {
        test: /\.tsx?$/,
        exclude: /[\\/]node_modules[\\/]/,
        use: {
          loader: 'builtin:swc-loader',
          options: {
            jsc: {
              parser: { syntax: 'typescript', tsx: true },
              transform: { react: { runtime: 'automatic' } },
            },
          },
        },
      },
    ],
  },
  plugins: [
    new rspack.HtmlRspackPlugin({ template: './src/index.html' }),
    new rspack.CopyRspackPlugin({ patterns: [{ from: 'LICENSE' }] }),
  ],
  devServer: { port: 8090 },
})
